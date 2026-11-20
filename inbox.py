"""Read-only projection of the local per-repository stores for the shared Inbox."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from project_config import ROOT, parse_project, read_config
from scheduled_triage import INBOX_HISTORY, UTC, missed_slots

FRESH_FOR = dt.timedelta(minutes=20)
ID_RE = re.compile(r"[0-9a-f]{12}")


def timestamp(value):
    try:
        time = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return time if time.tzinfo else time.replace(tzinfo=UTC)


def run_items(repo: str, profile: dict, state: dict, now: dt.datetime):
    for run in state.get("runs", []):
        at = timestamp(run.get("at"))
        if not at or at < now - INBOX_HISTORY or run["state"] not in ("failed", "partial"):
            continue
        detail = run.get("error") or next((item["detail"] for item in run.get("items", [])
                                          if item["status"] not in ("done", "posted") and item.get("detail")), "")
        yield {"repo": repo, "id": run["id"], "lane": run["lane"], "status": run["state"],
               "at": at.isoformat(), "detail": detail}
    for lane, at in missed_slots(profile.get("_schedule_windows", []), state, now):
        yield {"repo": repo, "lane": lane, "status": "missed", "at": at.isoformat(),
               "detail": "No run started within five minutes of this scheduled time."}


def build_inbox(profiles: list[dict], root: Path = ROOT, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(UTC)
    drafts, decisions, runs, projects = [], [], [], []
    for profile in profiles:
        project = parse_project({"repo": profile["repo"]})
        repo = project.repo
        snapshot_path = root / "snapshots" / project.storage_path / "latest.json"
        work = root / "work" / project.storage_path
        coverage = {"repo": repo, "snapshotAt": None, "stale": True, "error": ""}
        projects.append(coverage)

        try:
            snap = read_config(snapshot_path)
            if snap and str(snap.get("repo", "")).casefold() != repo.casefold():
                raise ValueError("The local snapshot belongs to a different repository")
        except (OSError, ValueError) as exc:
            coverage["error"] = str(exc)
        else:
            at = timestamp(snap.get("generatedAt"))
            coverage.update(snapshotAt=at.isoformat() if at else None, stale=not at or now - at > FRESH_FOR)
            for issue in snap.get("issues", {}).get("rows", []):
                if issue.get("decision"):
                    decisions.append({"repo": repo, "number": issue["number"], "title": issue["title"],
                                      "url": issue["url"], "status": issue.get("status"), "priority": issue.get("priority"),
                                      "priorityRank": issue.get("priorityRank"), "assignees": issue.get("assignees", []),
                                      "at": issue.get("updatedAt") or ""})

        for file in (work / "jobs").glob("*/job.json"):
            if not ID_RE.fullmatch(file.parent.name):
                continue
            try:
                job = read_config(file)
            except (OSError, ValueError):
                coverage["error"] = coverage["error"] or "Some local draft records could not be read."
                continue
            if job.get("status") != "done" or job.get("dismissed") or job.get("id") != file.parent.name:
                continue
            drafts.append({"repo": repo, "id": job["id"], "number": job["pr"], "title": job["title"],
                           "type": job.get("type", "review"), "agent": job.get("agent"), "url": job.get("url"),
                           "detail": job.get("summary") or "", "at": job.get("finished") or job.get("created") or ""})
        try:
            runs.extend(run_items(repo, profile, read_config(work / "schedule.json"), now))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            coverage["error"] = coverage["error"] or f"Could not read schedule history: {exc}"

    earliest = dt.datetime.min.replace(tzinfo=UTC)
    by_date = lambda item: timestamp(item["at"]) or earliest
    drafts.sort(key=by_date, reverse=True)
    runs.sort(key=by_date, reverse=True)
    decisions.sort(key=lambda item: (item["priorityRank"] is None, item["priorityRank"] or 0, by_date(item)))
    return {"drafts": drafts, "decisions": decisions, "runs": runs, "projects": projects}
