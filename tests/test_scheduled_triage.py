import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_server import DashboardProjects
from scheduled_triage import Scheduler, candidates, issue_fingerprint, parse_schedule


def issue(number, stage="triage", **fields):
    return {"number": number, "title": f"Issue {number}", "stage": stage, "url": f"https://github.com/team/demo/issues/{number}",
            "labels": [], "assignees": [], "comments": 0, **fields}


class SchedulingTest(unittest.TestCase):
    def test_issues_skip_unchanged_drafted_and_human_owned_work(self):
        unchanged, changed, already_drafted, human = issue(1), issue(2), issue(3), issue(4, "ready-for-human")
        old = issue_fingerprint(changed, [])
        changed["comments"] = 1
        snap = {"issues": {"rows": [unchanged, changed, already_drafted, human]}, "prs": []}
        prior = {"1": issue_fingerprint(unchanged, []), "2": old}
        drafts = [{"type": "triage", "pr": 3, "status": "done"}]
        rows = candidates("issues", snap, drafts, prior, None)
        self.assertEqual([r["number"] for r, _ in rows], [2])

    def test_pr_pass_selects_oldest_unreviewed_head_once(self):
        def pr(number, age, head="b"):
            return {"number": number, "head": head, "base": "main", "createdAgeDays": age}
        prs = [pr(1, 1), pr(2, 50), pr(3, 20), pr(4, 3)]
        existing = [{"type": "review", "pr": 2, "head": "b", "base": "main", "status": "done"},
                    {"type": "review", "pr": 3, "head": "a", "base": "main", "status": "posted"}]
        rows = candidates("prs", {"prs": prs}, existing, {}, lambda snap: snap["prs"])
        self.assertEqual([r["number"] for r, _ in rows], [3, 4, 1])

    def test_schedule_slot_is_persisted_and_does_not_repeat(self):
        schedule = parse_schedule({"enabled": True, "timezone": "UTC", "issues_times": ["08:00"], "prs_times": ["08:30"]})
        row = issue(1)
        jobs = []
        def enqueue(lane, numbers):
            self.assertEqual((lane, numbers), ("issues", [1]))
            created = {"id": "draft-1", "pr": 1, "status": "done", "type": "triage"}
            jobs.append(created)
            return {"created": [created]}
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "schedule.json"
            def scheduler():
                return Scheduler(file, schedule, lambda: {"issues": {"rows": [row]}, "prs": []},
                                 lambda: jobs, None, enqueue)
            first = scheduler()
            now = dt.datetime(2026, 11, 19, 8, 1, tzinfo=dt.timezone.utc)
            [(lane, slot)] = list(schedule.due(now))
            with patch("scheduled_triage.threading.Thread") as thread:
                thread.side_effect = lambda **kwargs: type("Immediate", (), {
                    "start": staticmethod(lambda: kwargs["target"](*kwargs["args"]))})()
                first.start(lane, slot)
                second = scheduler()
                self.assertIsNone(second.start(lane, slot))
                self.assertEqual(second.status()["runs"][0]["state"], "done")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(list(schedule.due(now.replace(minute=6))), [])

    def test_empty_enabled_schedule_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one run time"):
            parse_schedule({"enabled": True, "issues_times": [], "prs_times": []})

    def test_profiles_preserve_settings_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = DashboardProjects(Path(temp))
            with patch("project_server.gh", side_effect=['{"nameWithOwner":"Team/One"}', '{"nameWithOwner":"Team/Two"}']):
                registry.save({"repo": "team/one", "agent": "claude"})
                registry.save({"repo": "Team/Two", "agent": "codex", "maintainers": ["alice"]})
            registry.save({**registry.read("team/one"), "maintainers": ["bob"]})
            self.assertEqual(registry.read("TEAM/ONE")["maintainers"], ["bob"])
            self.assertEqual(registry.read("Team/Two")["maintainers"], ["alice"])
            self.assertEqual(len(registry.entries()), 2)
