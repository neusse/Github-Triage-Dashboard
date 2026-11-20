import datetime as dt
import tempfile
import unittest
from pathlib import Path

from inbox import build_inbox
from project_config import parse_project, write_json
from scheduled_triage import UTC, missed_slots, occurrence_key, parse_schedule, schedule_windows


def at(hour, minute=0, day=19):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def profile(enabled=True, issues=("08:00",), prs=("08:30",)):
    return {"schedule": {"enabled": enabled, "timezone": "UTC", "issues_times": list(issues), "prs_times": list(prs)}}


class InboxTest(unittest.TestCase):
    def test_misses_only_after_grace_and_only_while_enabled(self):
        enabled = profile()
        enabled["_schedule_windows"] = schedule_windows({}, enabled, at(7))
        paused = profile(False)
        windows = schedule_windows(enabled, paused, at(8, 32))
        state = {"runs": [{"lane": "issues", "trigger": "manual", "at": at(8, 1).isoformat()}]}
        self.assertEqual(list(missed_slots(windows, state, at(8, 4))), [])
        self.assertEqual(list(missed_slots(windows, state, at(10))), [("issues", at(8))])
        self.assertEqual(list(missed_slots(windows, state, at(10, day=20))), [("issues", at(8))])

    def test_cadence_change_preserves_only_the_previously_scheduled_slots(self):
        original = profile()
        original["_schedule_windows"] = schedule_windows({}, original, at(7))
        updated = profile(issues=("09:00",), prs=())
        windows = schedule_windows(original, updated, at(8, 15))
        self.assertEqual(list(missed_slots(windows, {}, at(10))), [("issues", at(8)), ("issues", at(9))])

    def test_midnight_grace_matches_scheduler(self):
        cfg = parse_schedule(profile(issues=("23:58",), prs=())["schedule"])
        self.assertEqual(list(cfg.due(at(0, 1, day=20))), [("issues", "2026-11-19T23:58")])

    def test_inbox_keeps_project_identity_and_avoids_double_counting_a_failed_slot(self):
        primary = {"repo": "Team/One", **profile(issues=("08:00", "09:00"), prs=())}
        secondary = {"repo": "Team/Two", **profile(False)}
        primary["_schedule_windows"] = schedule_windows({}, primary, at(7))
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            for p in (primary, secondary):
                scoped = parse_project(p).storage_path
                issue_url = "https://github.com/" + p["repo"] + "/issues/7"
                write_json(root / "work" / scoped / "jobs/123456789abc/job.json", {
                    "id": "123456789abc", "pr": 7, "title": p["repo"], "url": issue_url,
                    "type": "triage", "status": "done", "finished": at(8, 6).isoformat(),
                })
                if p is primary:
                    write_json(root / "snapshots" / scoped / "latest.json", {
                        "repo": p["repo"], "generatedAt": at(9, 30).isoformat(),
                        "issues": {"rows": [
                            {"number": 7, "title": "Choose an approach", "url": issue_url, "decision": True, "priorityRank": 1},
                            {"number": 8, "title": "Regular work", "decision": False},
                        ]},
                    })
                    write_json(root / "work" / scoped / "schedule.json", {
                        "occurrences": {occurrence_key("issues", "UTC", "2026-11-19T08:00"): at(8).isoformat()},
                        "runs": [
                            {"id": "111111111111", "lane": "issues", "at": at(8).isoformat(), "state": "failed",
                             "error": "GitHub unavailable"},
                            {"id": "222222222222", "lane": "issues", "at": at(9).isoformat(), "state": "quiet",
                             "trigger": "manual"},
                        ],
                    })
            data = build_inbox([primary, secondary], root, at(9, 40))
            self.assertEqual([(d["repo"], d["number"]) for d in data["drafts"]], [("Team/One", 7), ("Team/Two", 7)])
            self.assertEqual([d["title"] for d in data["decisions"]], ["Choose an approach"])
            self.assertEqual([(r["repo"], r["status"]) for r in data["runs"]], [("Team/One", "missed"), ("Team/One", "failed")])
            self.assertFalse(data["projects"][0]["stale"])
            self.assertEqual(data["projects"][1]["snapshotAt"], None)
