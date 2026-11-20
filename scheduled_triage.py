"""Per-repository scheduling and draft run history. This module never posts to GitHub."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from project_config import integer, mapping, read_config, strings, write_json

LANES = ("issues", "prs")
UTC = dt.timezone.utc
SLOT_GRACE = dt.timedelta(minutes=5)
INBOX_HISTORY = dt.timedelta(days=7)
ACTIVE = {"queued", "preparing", "running", "gating"}
TERMINAL_STAGES = {"tracker", "wontfix", "excluded", "inactive", "ready-for-human", "approved", "implementing",
                   "ready-to-merge", "in-review"}


@dataclass(frozen=True)
class Schedule:
    enabled: bool
    timezone: str
    times: dict[str, tuple[str, ...]]
    limits: dict[str, int]

    def now(self) -> dt.datetime:
        return self.localize(dt.datetime.now(UTC))

    def localize(self, instant: dt.datetime) -> dt.datetime:
        return instant.astimezone() if self.timezone == "local" else instant.astimezone(self.zone())

    def zone(self):
        return UTC if self.timezone == "UTC" else ZoneInfo(self.timezone)

    def slots_on(self, date: dt.date):
        for lane in LANES:
            for clock in self.times[lane]:
                wall = dt.datetime.combine(date, dt.time.fromisoformat(clock))
                when = wall.astimezone() if self.timezone == "local" else wall.replace(tzinfo=self.zone())
                if self.localize(when.astimezone(UTC)).replace(tzinfo=None) == wall:
                    yield lane, when

    def next_runs(self, now: dt.datetime) -> dict:
        result = dict.fromkeys(LANES)
        if self.enabled:
            for days in (0, 1, 2):
                for lane, when in self.slots_on(now.date() + dt.timedelta(days=days)):
                    if result[lane] is None and when.astimezone(UTC) > now.astimezone(UTC):
                        result[lane] = when.isoformat()
        return result

    def due(self, now: dt.datetime):
        local = self.localize(now)
        for day in (local.date() - dt.timedelta(days=1), local.date()):
            for lane, when in self.slots_on(day):
                if dt.timedelta() <= now.astimezone(UTC) - when.astimezone(UTC) < SLOT_GRACE:
                    yield lane, when.strftime("%Y-%m-%dT%H:%M")


def parse_schedule(raw: dict | None = None) -> Schedule:
    data = mapping(raw if raw is not None else {}, "schedule")
    enabled = data.get("enabled", False)
    zone = data.get("timezone", "local")
    if not isinstance(enabled, bool) or not isinstance(zone, str) or not zone:
        raise ValueError("schedule.enabled must be a boolean and schedule.timezone a timezone name")
    if zone not in ("local", "UTC"):
        try:
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"Timezone {zone!r} unavailable. Use local or install Python tzdata.") from exc
    times, limits = {}, {}
    for lane, defaults, cap in (("issues", ["08:00", "16:00"], 5), ("prs", ["08:30", "16:30"], 1)):
        key = f"{lane}_times"
        times[lane] = tuple(sorted(set(strings(data.get(key, defaults), "schedule." + key))))
        for value in times[lane]:
            try:
                if dt.time.fromisoformat(value).strftime("%H:%M") != value:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError(f"schedule.{key} needs 24-hour HH:MM times") from exc
        limits[lane] = integer(data.get(f"max_{lane}", cap), f"schedule.max_{lane}", 1)
        if limits[lane] > 25:
            raise ValueError(f"schedule.max_{lane} cannot exceed 25 drafts per run")
    if enabled and not any(times.values()):
        raise ValueError("Enter at least one run time to enable the schedule")
    return Schedule(enabled, zone, times, limits)


def schedule_windows(previous: dict, raw: dict, now: dt.datetime) -> list[dict]:
    """Keep enabled intervals, so a pause or time change never invents past runs."""
    windows = [dict(w) for w in previous.get("_schedule_windows", [])
               if not w.get("end") or dt.datetime.fromisoformat(w["end"]) >= now - INBOX_HISTORY]
    settings = parse_schedule(raw.get("schedule"))
    cadence = {"timezone": settings.timezone, **{f"{lane}_times": list(times) for lane, times in settings.times.items()}}
    current = windows[-1] if windows and not windows[-1].get("end") else None
    if current and (not settings.enabled or current["cadence"] != cadence):
        current["end"] = now.isoformat()
        current = None
    if not current and settings.enabled:
        windows.append({"start": now.isoformat(), "cadence": cadence})
    return windows


def occurrence_key(lane: str, zone: str, slot: str) -> str:
    return f"{lane}|{zone}|{slot}"


def missed_slots(windows: list[dict], state: dict, now: dt.datetime):
    """Yield enabled slots whose grace period ended without a scheduled dispatch."""
    for window in windows:
        settings = parse_schedule(window["cadence"])
        start = max(dt.datetime.fromisoformat(window["start"]), now - INBOX_HISTORY)
        end = min(dt.datetime.fromisoformat(window["end"]), now) if window.get("end") else now
        first, last = settings.localize(start).date(), settings.localize(end).date()
        for offset in range((last - first).days + 1):
            for lane, when in settings.slots_on(first + dt.timedelta(days=offset)):
                instant = when.astimezone(UTC)
                if instant < start or instant + SLOT_GRACE > end:
                    continue
                slot = when.strftime("%Y-%m-%dT%H:%M")
                key = occurrence_key(lane, settings.timezone, slot)
                if key in state.get("occurrences", {}):
                    continue
                yield lane, instant


def issue_fingerprint(row: dict, linked_prs: list[dict]) -> str:
    material = [row.get(k) for k in ("title", "labels", "assignees", "milestone", "bodyEditedAt",
                                     "comments", "recentComments", "refs")]
    material.append([(p["number"], p["head"], p.get("verdict")) for p in linked_prs])
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def candidates(lane: str, snap: dict, jobs: list[dict], seen: dict, eligible_prs) -> list[tuple[dict, str]]:
    if lane == "prs":
        existing = {(j["pr"], j["head"], j.get("base")) for j in jobs if j["type"] == "review"
                    and j["status"] in ACTIVE | {"done", "posted"}}
        return [(p, p["head"]) for p in sorted(eligible_prs(snap), key=lambda p: -(p.get("createdAgeDays") or 0))
                if (p["number"], p["head"], p["base"]) not in existing]
    active = {j["pr"] for j in jobs if j["type"] == "triage" and j["status"] in ACTIVE}
    drafted = {j["pr"] for j in jobs if j["type"] == "triage" and j["status"] == "done" and not j.get("dismissed")}
    links = {}
    for pr in sorted(snap["prs"], key=lambda p: p["number"]):
        for number in pr["closes"]:
            links.setdefault(number, []).append(pr)
    out = []
    for row in snap["issues"]["rows"]:
        number = row["number"]
        if number in active or number in drafted or row["stage"] in TERMINAL_STAGES:
            continue
        fingerprint = issue_fingerprint(row, links.get(number, []))
        if seen.get(str(number)) != fingerprint:
            out.append((row, fingerprint))
    return out


class Scheduler:
    def __init__(self, path: Path, config: Schedule, collect, jobs, eligible_prs, enqueue):
        self.path = path
        self.config = config
        self.collect = collect
        self.jobs = jobs
        self.eligible_prs = eligible_prs
        self.enqueue = enqueue
        self.lock = threading.RLock()
        self.running = False
        self.paused = False
        state = self._state()
        interrupted = [run for run in state["runs"] if run["state"] == "collecting"]
        for run in interrupted:
            run.update(state="failed", error="Dashboard stopped during collection")
        if interrupted:
            write_json(self.path, state)

    def _state(self):
        return read_config(self.path) or {"slots": {}, "seen": {}, "runs": []}

    def status(self, include: str | None = None):
        with self.lock:
            state = self._state()
            runs = state["runs"][:10]
            runs += [run for run in state["runs"][10:] if run["id"] == include]
            return {"enabled": self.config.enabled, "timezone": self.config.timezone,
                    "busy": self.running or any(r["state"] == "drafting" for r in state["runs"]),
                    "next": self.config.next_runs(self.config.now()), "runs": runs}

    def _settle(self, state):
        by_id = {j["id"]: j for j in self.jobs()}
        dirty = False
        for run in state["runs"]:
            if run["state"] != "drafting":
                continue
            for item in run["items"]:
                job = by_id.get(item["id"])
                status = job["status"] if job else "missing"
                detail = (job.get("summary") or job.get("error") or "") if job else ""
                if (item["status"], item.get("detail")) != (status, detail):
                    item.update(status=status, detail=detail)
                    dirty = True
            if any(item["status"] in ACTIVE for item in run["items"]):
                continue
            seen = state["seen"].setdefault(run["lane"], {})
            for item in run["items"]:
                if item["status"] in ("done", "posted"):
                    seen[str(item["number"])] = item["fingerprint"]
            run["state"] = "done" if all(item["status"] in ("done", "posted") for item in run["items"]) else "partial"
            dirty = True
        if dirty:
            write_json(self.path, state)

    def start(self, lane: str, slot=None):
        if lane not in LANES:
            raise ValueError("Choose an issue or PR pass")
        with self.lock:
            state = self._state()
            self._settle(state)
            key = occurrence_key(lane, self.config.timezone, slot) if slot else None
            occurrences = state.setdefault("occurrences", {})
            if key and (key in occurrences or
                        not occurrences and state["slots"].get(lane) == slot):
                return None
            if self.paused or self.running or any(r["state"] == "drafting" for r in state["runs"]):
                raise ValueError("A scheduled pass is still running for this repository")
            now = self.config.now()
            if key:
                state["occurrences"] = {k: at for k, at in occurrences.items()
                                        if dt.datetime.fromisoformat(at) >= now - INBOX_HISTORY - SLOT_GRACE}
                state["occurrences"][key] = now.isoformat()
            run = {"id": uuid.uuid4().hex[:12], "lane": lane, "at": now.isoformat(),
                   "trigger": "scheduled" if slot else "manual", "state": "collecting", "items": []}
            state["runs"] = [run, *state["runs"][:19]]
            write_json(self.path, state)
            self.running = True
            threading.Thread(target=self._draft, args=(run["id"], lane), daemon=True).start()
            return run

    def _draft(self, run_id, lane):
        try:
            snap = self.collect()
            with self.lock:
                state = self._state()
                run = next(r for r in state["runs"] if r["id"] == run_id)
                pending = candidates(lane, snap, self.jobs(), state["seen"].get(lane, {}), self.eligible_prs)
                selected = {row["number"]: (row, fp) for row, fp in pending[:self.config.limits[lane]]}
                result = self.enqueue(lane, list(selected)) if selected else {"created": [], "skipped": []}
                run["items"] = [{"id": job["id"], "number": job["pr"], "status": job["status"],
                                 "title": selected[job["pr"]][0]["title"], "url": selected[job["pr"]][0]["url"],
                                 "fingerprint": selected[job["pr"]][1]} for job in result["created"]]
                run["deferred"] = len(pending) - len(selected)
                run["landing"] = [{"number": p["number"], "url": p["url"], "head": p["head"],
                                   "title": p["title"], "verdict": p["verdict"], "blocker": next(iter(p["blockers"]), "")}
                                  for p in snap["prs"][:8]] if lane == "prs" else []
                run["state"] = "drafting" if run["items"] else "quiet"
                write_json(self.path, state)
        except Exception as exc:
            with self.lock:
                state = self._state()
                run = next(r for r in state["runs"] if r["id"] == run_id)
                run.update(state="failed", error=str(exc)[:500])
                write_json(self.path, state)
        finally:
            with self.lock:
                self.running = False

    def loop(self, stop: threading.Event):
        while not stop.is_set():
            try:
                with self.lock:
                    self._settle(self._state())
                    if self.config.enabled and not self.paused:
                        for lane, slot in self.config.due(self.config.now()):
                            self.start(lane, slot)
            except (OSError, ValueError) as exc:
                if not isinstance(exc, ValueError) or "still running" not in str(exc):
                    print(f"[schedule] {exc}", flush=True)
            stop.wait(15)
