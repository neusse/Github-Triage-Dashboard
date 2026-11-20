import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jobs
import triage
from project_config import parse_project, target_config


class RepositoryConfigurationTest(unittest.TestCase):
    def test_minimal_target_has_no_project_specific_policy(self):
        project = parse_project({"repo": "Team/Example"})
        self.assertIsNone(project.base_branch)
        self.assertFalse(project.maintainers)
        self.assertEqual(project.labels.status("triage"), "needs-triage")
        self.assertEqual(project.labels.status("ready-for-human"), "ready-for-human")
        self.assertEqual(project.labels.special["wontfix"], "wontfix")
        self.assertFalse(project.workflow.gate_commands)
        self.assertFalse(project.design_enabled())

    def test_env_override_cannot_reuse_another_repos_settings(self):
        config = {"repo": "Team/Example", "maintainers": ["alice"], "triage_reports_issue": 42,
                  "agent": "claude", "implement": {"gh_token_env": "EXAMPLE_BOT_TOKEN"}}
        original = parse_project(config)
        switched = parse_project(config, "Team/Other")
        self.assertEqual(switched.repo, "Team/Other")
        self.assertNotEqual(original.storage_path, switched.storage_path)
        self.assertEqual(switched.reports_issue, 0)
        self.assertFalse(switched.maintainers)
        self.assertEqual(target_config(config, switched.repo), {"agent": "claude"})
        self.assertEqual(original.storage_path, parse_project(config, "team/example").storage_path)

    def test_status_roles_match_custom_labels_and_leave_unmapped_labels_alone(self):
        project = parse_project({
            "repo": "Team/Example", "maintainers": ["alice"],
            "labels": {"statuses": {"needs-scope-review": "scope please", "design-review": "proposal submitted"},
                       "design_approved": "approved design"},
        })
        with patch.multiple(triage, PROJECT=project, LABELS=project.labels, WORKFLOW=project.workflow):
            self.assertEqual(triage.issue_stage(["Scope Please"], "needs-scope-review", "An issue",
                                               False, False, False), "scope")
            self.assertEqual(jobs._status_swap(["Scope Please", "status:unmapped"], "design-review"),
                             ["--add-label", "proposal submitted", "--remove-label", "Scope Please"])
            self.assertEqual(jobs._status_swap(["Scope Please"], "in-progress"), [])

    def test_config_rejects_a_maintainer_gate_without_maintainers(self):
        with self.assertRaisesRegex(ValueError, "at least one maintainer"):
            parse_project({"repo": "Team/Example", "workflow": {"require_maintainer_review": True}})


class PullRequestWorkflowTest(unittest.TestCase):
    def test_live_rules_merge_with_local_rules_on_a_branch_with_a_slash(self):
        project = parse_project({"repo": "Team/Example", "base_branch": "release/next",
                                 "workflow": {"required_checks": ["Extra checks"]}})
        response = {
            "repos/Team/Example/rules/branches/release%2Fnext": [[
                {"type": "pull_request", "parameters": {"required_approving_review_count": 2,
                                                       "allowed_merge_methods": ["squash", "rebase"]}},
                {"type": "required_status_checks", "parameters": {
                    "required_status_checks": [{"context": "Build"}],
                    "strict_required_status_checks_policy": True}},
            ]],
            "repos/Team/Example/branches/release%2Fnext": {"protected": True},
            "repos/Team/Example/branches/release%2Fnext/protection": {
                "required_status_checks": {"contexts": ["Integration"]},
                "required_conversation_resolution": {"enabled": True},
            },
        }
        with (patch.multiple(triage, PROJECT=project, WORKFLOW=project.workflow, REPO=project.repo),
              patch.object(triage, "repo_info", return_value={
                  "defaultBranchRef": {"name": "trunk"}, "isEmpty": False,
                  "mergeCommitAllowed": True, "squashMergeAllowed": True, "rebaseMergeAllowed": False}),
              patch.object(triage, "gh_json", side_effect=lambda args: response[args[1]])):
            rules = triage.read_rules()
        self.assertEqual(rules["required_checks"], ["Build", "Extra checks", "Integration"])
        self.assertEqual(rules["merge_method"], "squash")
        self.assertEqual(rules["approvals"], 2)
        self.assertTrue(rules["strict"])
        self.assertTrue(rules["thread_resolution"])

    def test_unprotected_pr_is_landable_and_can_still_get_a_draft_review(self):
        project = parse_project({"repo": "Team/Example", "maintainers": ["bob"]})
        head = "a" * 40
        pr = {
            "number": 4, "headRefOid": head, "baseRefName": "trunk", "headRefName": "feature",
            "title": "Example change", "author": {"login": "alice"}, "url": "https://github.com/Team/Example/pull/4",
            "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": None,
            "additions": 1, "deletions": 0, "labels": {"nodes": [{"name": "unmapped"}]},
            "commits": {"nodes": [{"commit": {"oid": head}}], "totalCount": 1},
            "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}, "closingIssuesReferences": {"nodes": []},
            "comments": {"nodes": [], "totalCount": 0}, "createdAt": None, "updatedAt": None,
            "editor": None, "lastEditedAt": None, "autoMergeRequest": None,
        }
        rules = {"strict": False, "approvals": 0, "required_checks": [], "thread_resolution": False,
                 "maintainer_review": False, "merge_method": "squash"}
        with (patch.multiple(triage, WORKFLOW=project.workflow, LABELS=project.labels, MAINTAINERS=project.maintainers),
              patch.object(triage, "base_branch", return_value="trunk")):
            result = triage.evaluate_pr(pr, rules, {})
            blocked = triage.evaluate_pr({**pr, "mergeStateStatus": "BLOCKED"}, rules, {})
            for login, commit, current in (("bob", head, True), ("bob", "b" * 40, False), ("alice", head, False)):
                with self.subTest(login=login, commit=commit):
                    comment = {
                        "id": "comment-1", "url": pr["url"] + "#issuecomment-1", "createdAt": None,
                        "author": {"login": login}, "bodyText": "",
                        "body": f"<!-- example-exact-head-review:4:{commit} -->",
                    }
                    marked = triage.evaluate_pr({**pr, "comments": {"nodes": [comment], "totalCount": 1}}, rules, {})
                    self.assertEqual(marked["reviewCurrent"], current)
        self.assertEqual(result["verdict"], "needs-auth")
        self.assertEqual(result["mismatch"], [])
        self.assertEqual(jobs.review_candidates({"prs": [result]}), [result])
        self.assertIn("GitHub reports this PR is blocked", blocked["blockers"])

    def test_snapshot_diff_refuses_another_repo_or_branch(self):
        with self.assertRaisesRegex(ValueError, "same repository and base branch"):
            triage.diff({"repo": "Team/Example", "baseBranch": "trunk"},
                        {"repo": "Team/Other", "baseBranch": "trunk"})
        with self.assertRaisesRegex(ValueError, "same repository and base branch"):
            triage.diff({"repo": "Team/Example", "baseBranch": "trunk"},
                        {"repo": "Team/Example", "baseBranch": "release/next"})

    def test_missing_validation_executable_is_a_failed_gate_result(self):
        policy = parse_project({"repo": "Team/Example",
                                "workflow": {"gate_commands": [["nonexistent-workflow-executable-2fc337"]]}}).workflow
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            jid = "a" * 12
            (tree / jid).mkdir()
            with (patch.object(triage, "WORKFLOW", policy), patch.object(jobs, "JOBS_DIR", tree),
                  patch.object(jobs, "update"), patch.object(jobs, "impl_config", return_value={"gate_timeout_min": 1})):
                results = jobs.run_gate(jid, tree)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0]["ok"])


if __name__ == "__main__":
    unittest.main()
