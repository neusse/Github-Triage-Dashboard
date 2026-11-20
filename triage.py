"""GitHub triage dashboard: collector, evaluator and local server.

Use config.json or TRIAGE_REPO to select the repository. Collection is read-only.

Usage:
    python triage.py collect            # write snapshots/latest.json
    python triage.py serve [--port N]   # dashboard on http://127.0.0.1:N
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from project_config import MERGE_METHODS, project, read_config, target_config, write_json

try:
    PROJECT = project()
except ValueError as exc:
    if __name__ == "__main__":
        sys.exit(str(exc))
    raise
REPO = PROJECT.repo
OWNER, _, NAME = REPO.partition("/")
MAINTAINERS = PROJECT.maintainers
LABELS = PROJECT.labels
WORKFLOW = PROJECT.workflow
STALE_DAYS = WORKFLOW.stale_days
HERE = Path(__file__).resolve().parent
SNAPDIR = HERE / "snapshots" / PROJECT.storage_path
WORKDIR = HERE / "work" / PROJECT.storage_path

STATUS_NEXT_ACTOR = {
    "triage": "maintainer (classify)",
    "needs-information": "reporter",
    "design-needed": "maintainer + contributor",
    "design-review": "maintainer (review the design)",
    "needs-scope-review": "maintainer (decide scope)",
    "ready-for-work": "any contributor",
    "ready-for-human": "human contributor",
    "in-progress": "assignee",
    "waiting-on-author": "author",
    "waiting-on-dependency": "dependency owner",
    "waiting-on-review": "reviewer",
    "ready-to-merge": "maintainer",
    "inactive": "author",
}


# --------------------------------------------------------------------------- gh

def gh(args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def gh_json(args: list[str]):
    return json.loads(gh(args) or "null")


def graphql(query: str, **variables) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    data = gh_json(args)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"])[:500])
    return data["data"]


@cache
def repo_info() -> dict:
    if not REPO:
        raise ValueError('Set "repo" in config.json or set TRIAGE_REPO to owner/name.')
    data = graphql("""
        query($owner:String!, $name:String!) {
          repository(owner:$owner, name:$name) {
            defaultBranchRef { name }
            hasIssuesEnabled isEmpty viewerPermission
            mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed
          }
        }""", owner=OWNER, name=NAME)
    if not data.get("repository"):
        raise ValueError(f"GitHub repository {REPO} is unavailable")
    return data["repository"]


def base_branch() -> str:
    info = repo_info()
    if info["isEmpty"]:
        return ""
    branch = info["defaultBranchRef"]
    return PROJECT.base_branch or (branch["name"] if branch else "")


PR_QUERY = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:50, after:$after, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt updatedAt additions deletions
        author { login }
        lastEditedAt editor { login }
        comments(last:6) { totalCount nodes { id url createdAt author { login } bodyText body } }
        headRefName baseRefName headRefOid mergeable mergeStateStatus reviewDecision
        headRepository { nameWithOwner }
        autoMergeRequest { enabledAt mergeMethod }
        labels(first:100) { nodes { name } }
        closingIssuesReferences(first:20) { nodes { number } }
        commits(last:1) { totalCount nodes { commit { oid
          statusCheckRollup { state contexts(first:60) { nodes {
            __typename
            ... on CheckRun { name conclusion status }
            ... on StatusContext { context state }
          } } }
        } } }
        reviews(last:30) { nodes { author { login } state submittedAt body commit { oid } } }
        reviewThreads(first:100) { nodes { isResolved } }
        timelineItems(last:30, itemTypes:[LABELED_EVENT]) { nodes {
          ... on LabeledEvent { createdAt label { name } }
        } }
      }
    }
  }
}"""

ISSUE_QUERY = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    issues(states:OPEN, first:100, after:$after, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt updatedAt milestone { number }
        author { login }
        labels(first:100) { nodes { name } }
        assignees(first:10) { nodes { login } }
        lastEditedAt editor { login }
        comments(last:6) { totalCount nodes { id url createdAt author { login } bodyText } }
        timelineItems(last:30, itemTypes:[LABELED_EVENT]) { nodes {
          ... on LabeledEvent { createdAt label { name } }
        } }
        refs: timelineItems(last:15, itemTypes:[CROSS_REFERENCED_EVENT]) { nodes {
          ... on CrossReferencedEvent { createdAt actor { login } source {
            __typename
            ... on PullRequest { number title url state }
            ... on Issue { number title url state }
          } }
        } }
      }
    }
  }
}"""


def paged(query: str, key: str) -> list[dict]:
    out, after = [], None
    while True:
        conn = graphql(query, owner=OWNER, name=NAME, after=after)["repository"][key]
        out += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return out
        after = conn["pageInfo"]["endCursor"]


# --------------------------------------------------------------------- helpers

def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def age_days(s: str | None) -> float | None:
    t = parse_ts(s)
    return round((now() - t).total_seconds() / 86400, 1) if t else None


def label_names(node) -> list[str]:
    return [l["name"] for l in node["labels"]["nodes"]]


def label_applied_at(node, label: str) -> str | None:
    hits = [e["createdAt"] for e in node["timelineItems"]["nodes"]
            if e and e.get("label", {}).get("name") == label]
    return hits[-1] if hits else None


# A finding: a `**P1**`-style severity tag, or a Comment review that asks for a fix before merge
# ("I recommend fixing item 1 before merge"), which carries no formal Request-changes state.
FINDING_RE = re.compile(r"\*\*P[0-2]\b\s*[—:–-]", re.I)
BOT_RE = re.compile(r"(\[bot\]$|^github-actions$|^dependabot)")


def recent_comments(node) -> list[dict]:
    out = []
    for c in node["comments"]["nodes"]:
        text = re.sub(r"\s+", " ", c["bodyText"] or "").strip()
        # Drop agent attribution footers so the snippet shows the content.
        text = re.sub(r"🤖 Generated with \[?Claude Code\]?.*$", "", text).strip()
        out.append({"id": c["id"], "url": c["url"], "at": c["createdAt"],
                    "by": (c["author"] or {}).get("login", "ghost"), "text": text[:400]})
    return out


# --------------------------------------------------------------------- rules

def read_rules(branch_name: str | None = None) -> dict:
    """Combine GitHub branch restrictions with the project's additional gates."""
    info, base = repo_info(), branch_name or base_branch()
    out = {"required_checks": list(WORKFLOW.required_checks), "strict": WORKFLOW.require_up_to_date,
           "approvals": 0, "thread_resolution": WORKFLOW.require_resolved_threads,
           "maintainer_review": WORKFLOW.require_maintainer_review, "raw_types": [], "warnings": []}
    methods = {method for method, field in zip(MERGE_METHODS, (
        "mergeCommitAllowed", "squashMergeAllowed", "rebaseMergeAllowed")) if info[field]}
    rules = []
    if base and not info["isEmpty"]:
        ref = quote(base, safe="")
        try:
            rules = gh_json(["api", f"repos/{REPO}/rules/branches/{ref}", "--paginate", "--slurp"]) or []
        except RuntimeError as exc:
            out["warnings"].append(f"Unable to read GitHub rulesets: {exc}")
        rules = [r for page in rules for r in page]
        branch = gh_json(["api", f"repos/{REPO}/branches/{ref}"])
        if branch.get("protected"):
            try:
                classic = gh_json(["api", f"repos/{REPO}/branches/{ref}/protection"])
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc) or "Branch not protected" not in str(exc):
                    out["warnings"].append("Classic branch protection details are unavailable to this account.")
            else:
                checks = classic.get("required_status_checks") or {}
                reviews = classic.get("required_pull_request_reviews") or {}
                out["required_checks"] += checks.get("contexts", [])
                out["required_checks"] += [check["context"] for check in checks.get("checks", [])]
                out["strict"] |= bool(checks.get("strict"))
                out["approvals"] = reviews.get("required_approving_review_count", 0)
                out["thread_resolution"] |= (classic.get("required_conversation_resolution") or {}).get("enabled", False)
                if (classic.get("required_linear_history") or {}).get("enabled"):
                    methods.discard("merge")
    for r in rules:
        p = r.get("parameters") or {}
        out["raw_types"].append(r.get("type"))
        if r.get("type") == "required_status_checks":
            out["required_checks"] += [c["context"] for c in p.get("required_status_checks", [])]
            out["strict"] = out["strict"] or p.get("strict_required_status_checks_policy", False)
        elif r.get("type") == "pull_request":
            out["approvals"] = max(out["approvals"], p.get("required_approving_review_count", 0))
            out["thread_resolution"] |= p.get("required_review_thread_resolution", False)
            if "allowed_merge_methods" in p:
                methods &= set(p["allowed_merge_methods"])
        elif r.get("type") == "required_linear_history":
            methods.discard("merge")
    out["required_checks"] = sorted(set(out["required_checks"]))
    out["merge_methods"] = [method for method in MERGE_METHODS if method in methods]
    out["merge_method"] = (WORKFLOW.merge_method or next(iter(out["merge_methods"]), None))
    if out["merge_method"] not in methods:
        out["merge_method"] = None
    return out


# ------------------------------------------------------------------ evaluate PR

def behind_by(base: str, head: str) -> int | None:
    """Commits on `base` missing from `head` (mergeStateStatus hides this for drafts)."""
    try:
        return int(gh(["api", f"repos/{REPO}/compare/{quote(base, safe='')}...{head}",
                       "--jq", ".behind_by"]).strip())
    except (RuntimeError, ValueError):
        return None


def evaluate_pr(pr: dict, rules: dict, prs_by_head: dict) -> dict:
    labels = label_names(pr)
    base = base_branch()
    require_current = rules["strict"]
    conflicting = pr["mergeable"] == "CONFLICTING" or pr["mergeStateStatus"] == "DIRTY"
    status = LABELS.status_labels(labels)
    status_label = status[0] if status else None
    status_role = LABELS.role(status_label)
    head = pr["headRefOid"]
    behind = behind_by(pr["baseRefName"], head) if require_current else None
    author = (pr["author"] or {}).get("login", "ghost")
    is_maint = author.casefold() in MAINTAINERS

    commit = pr["commits"]["nodes"][0]["commit"] if pr["commits"]["nodes"] else {}
    if commit.get("oid") != head:
        commit = {}
    rollup = commit.get("statusCheckRollup") or {}
    results = {}
    for c in (rollup.get("contexts") or {}).get("nodes", []):
        if c["__typename"] == "CheckRun":
            results[c["name"]] = c["conclusion"] or c["status"]
        else:
            results[c["context"]] = c["state"]
    required = rules["required_checks"]
    missing = [n for n in required if n not in results]
    failed = [n for n in required if results.get(n) in
              ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")]
    pending = [n for n in required if n in results and n not in failed
               and results[n] not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
    checks_ok = not (missing or failed or pending)

    maint_reviews = [r for r in pr["reviews"]["nodes"]
                     if r["author"] and r["author"]["login"].casefold() in MAINTAINERS
                     and r["author"]["login"].casefold() != author.casefold() and r["state"] != "DISMISSED"]
    head_reviews = [r for r in maint_reviews if r["commit"] and r["commit"]["oid"] == head]
    last_review = maint_reviews[-1] if maint_reviews else None
    head_review = head_reviews[-1] if head_reviews else None
    findings = None
    if head_review:
        # Each maintainer's latest review on this head counts: one maintainer's clean review
        # does not clear what the other asked to fix. A later review by the same person does.
        latest_by = {r["author"]["login"].casefold(): r for r in head_reviews}
        findings = any(r["state"] == "CHANGES_REQUESTED" or FINDING_RE.search(r["body"] or "")
                       for r in latest_by.values())

    unresolved = sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"])
    stacked_on = None
    if pr["baseRefName"] != base:
        base_pr = prs_by_head.get(pr["baseRefName"])
        stacked_on = base_pr["number"] if base_pr else pr["baseRefName"]

    decision = pr.get("reviewDecision")
    native_review_ok = decision not in ("REVIEW_REQUIRED", "CHANGES_REQUESTED")
    if rules["approvals"] and decision != "APPROVED":
        native_review_ok = False
    clean_review = bool(head_review) and not findings
    # Existing review comments can use a repository-specific prefix.
    marker_format = r"(?:claude-triage:pr|github-triage-review|[a-z0-9-]{1,64}-exact-head-review)"
    exact_marker = re.compile(rf"<!--\s*{marker_format}:{pr['number']}:{head}\s*-->")
    assessed = any(
        (entry.get("author") or {}).get("login", "").casefold() in MAINTAINERS
        and exact_marker.search(entry.get("body") or "")
        for entry in (*pr["reviews"]["nodes"], *pr["comments"]["nodes"]) if entry
    )
    thread_ok = not rules["thread_resolution"] or unresolved == 0
    unknown = pr["mergeable"] == "UNKNOWN" or pr["mergeStateStatus"] == "UNKNOWN"
    gates = {
        "draft": {"ok": not pr["isDraft"], "text": "draft" if pr["isDraft"] else "ready"},
        "base": {"ok": stacked_on is None,
                 "text": base if stacked_on is None else f"stacked on #{stacked_on}"
                 if isinstance(stacked_on, int) else f"on {stacked_on}"},
        "uptodate": {"ok": not conflicting and not unknown and (not require_current or behind == 0),
                     "text": "conflicting" if conflicting else
                     "mergeability unknown" if unknown else
                     "up-to-date check not required" if not require_current else
                     "unknown" if behind is None else
                     f"{behind} behind" if behind else "up to date"},
        "checks": {"ok": checks_ok,
                   "text": (f"{len(required) - len(missing) - len(failed) - len(pending)}/{len(required)}"
                   + (f" failed: {', '.join(failed)}" if failed else "")
                   + (f" pending {len(pending)}" if pending else "")
                   + (f" missing {len(missing)}" if missing and not failed else "")) if required else "none required"},
        "review": {"ok": native_review_ok and (clean_review or not rules["maintainer_review"]),
                   "text": (decision or "GitHub approval required").lower().replace("_", " ") if not native_review_ok
                   else "clean review on this head" if clean_review
                   else "maintainer review required" if rules["maintainer_review"]
                   else "approved by GitHub" if decision == "APPROVED" else "none required"},
        "threads": {"ok": thread_ok,
                    "text": f"{unresolved} open" if unresolved else "resolved" if rules["thread_resolution"] else "none required"},
    }

    blockers = []
    if pr["isDraft"]:
        blockers.append("Draft: author must mark it ready")
    if stacked_on is not None:
        blockers.append(f"Targets {pr['baseRefName']} instead of {base}")
    if conflicting:
        blockers.append(f"Conflicts with {pr['baseRefName']}")
    elif behind:
        blockers.append(f"Behind {pr['baseRefName']}: refresh when it reaches the front")
    elif unknown or (require_current and behind is None):
        blockers.append("Mergeability unknown (GitHub still computing): refresh again")
    if failed:
        blockers.append(f"Required checks failed: {', '.join(failed)}")
    elif missing or pending:
        blockers.append("Required checks not complete on this head")
    if not native_review_ok:
        blockers.append("GitHub review requirements are not met")
    if rules["maintainer_review"] and not clean_review:
        blockers.append("Review findings outstanding on this head" if findings else
                        "Maintainer review of this head required")
    if not thread_ok:
        blockers.append(f"{unresolved} unresolved review thread(s)")
    waiting = status_role == "waiting-on-author"
    if waiting and not findings:
        blockers.append(f"Labelled {status_label}")
    if pr["mergeStateStatus"] in ("BLOCKED", "BEHIND") and not blockers:
        blockers.append("GitHub reports this PR is blocked" if pr["mergeStateStatus"] == "BLOCKED"
                        else f"GitHub requires this PR to be up to date with {base}")
    if not rules["merge_method"]:
        blockers.append("No configured merge method is enabled for this branch")

    refresh_only = len(blockers) == 1 and blockers[0].startswith("Behind ")
    if not blockers:
        verdict = "ready" if author.casefold() in WORKFLOW.standing_authorization else "needs-auth"
    elif refresh_only:
        verdict = "refresh"
    elif pr["isDraft"] or stacked_on is not None:
        verdict = "parked"
    elif (rules["maintainer_review"] and findings) or waiting or decision == "CHANGES_REQUESTED":
        verdict = "author"
    else:
        verdict = "review"

    ready_label = status_role == "ready-to-merge"
    mismatch = []
    if ready_label and verdict not in ("ready", "needs-auth"):
        mismatch.append(f"Labelled {status_label} but the gate fails on this head")
    if len(status) > 1 or (WORKFLOW.require_status_label and not status):
        mismatch.append(f"{len(status)} status labels (expected 1)")

    return {
        "number": pr["number"], "title": pr["title"], "url": pr["url"],
        "author": author, "maintainerAuthor": is_maint,
        "draft": pr["isDraft"], "head": head, "base": pr["baseRefName"],
        "headRef": pr["headRefName"], "stackedOn": stacked_on,
        "size": f"+{pr['additions']}/-{pr['deletions']}",
        "commits": pr["commits"]["totalCount"], "behindBy": behind,
        "labels": labels, "status": status_label, "statusRole": status_role,
        "statusAgeDays": age_days(label_applied_at(pr, status_label)) if status_label else None,
        "nextActor": STATUS_NEXT_ACTOR.get(status_role, "—"),
        "closes": [n["number"] for n in pr["closingIssuesReferences"]["nodes"]],
        "createdAgeDays": age_days(pr["createdAt"]),
        "updatedAgeDays": age_days(pr["updatedAt"]), "updatedAt": pr["updatedAt"],
        "commentCount": pr["comments"]["totalCount"], "recentComments": recent_comments(pr),
        "bodyEditedAt": pr["lastEditedAt"], "bodyEditor": (pr["editor"] or {}).get("login"),
        "autoMerge": bool(pr["autoMergeRequest"]),
        "gates": gates, "blockers": blockers, "verdict": verdict, "mismatch": mismatch,
        "reviewCurrent": clean_review or decision == "APPROVED" or assessed,
        "lastReview": None if not last_review else {
            "by": last_review["author"]["login"], "state": last_review["state"],
            "at": last_review["submittedAt"],
            "commit": (last_review["commit"] or {}).get("oid", "")[:9],
            "onHead": bool(head_review) and last_review is head_review,
        },
    }


VERDICT_RANK = {"ready": 0, "needs-auth": 1, "refresh": 2, "review": 3, "author": 4, "parked": 5}


def landing_order(prs: list[dict]) -> list[dict]:
    """Ready work first, stacked PRs after their base, older work first within a group."""
    by_num = {p["number"]: p for p in prs}
    roots = sorted((p for p in prs if not isinstance(p["stackedOn"], int) or p["stackedOn"] not in by_num),
                   key=lambda p: (VERDICT_RANK[p["verdict"]], -(p["createdAgeDays"] or 0)))
    children: dict[int, list[dict]] = {}
    for p in prs:
        if isinstance(p["stackedOn"], int) and p["stackedOn"] in by_num:
            children.setdefault(p["stackedOn"], []).append(p)
    out: list[dict] = []

    def visit(p):
        out.append(p)
        for c in sorted(children.get(p["number"], []), key=lambda c: c["number"]):
            visit(c)

    for r in roots:
        visit(r)
    return out


# --------------------------------------------------------------- evaluate issues

DESIGN_RE = re.compile(r"Design proposal for #\d+")
STAGES = [  # workflow order: (key, label, task)
    ("triage", "Triage", "triage"),
    ("info", "Needs information", None),
    ("scope", "Scope review", "scope"),
    ("design-needed", "Design needed", "design"),
    ("ready", "Ready for work", "design"),
    ("ready-for-human", "Ready for a human", None),
    ("design-posted", "Design in review", "approve"),
    ("design-changes", "Design changes requested", "design"),
    ("approved", "Design approved", "implement"),
    ("implementing", "In progress", "implement"),
    ("in-review", "PR in review", None),
    ("ready-to-merge", "PR ready to merge", "land"),
    ("blocked", "Waiting on dependency", None),
    ("tracker", "Trackers", None),
    ("wontfix", "Won't fix", None),
    ("inactive", "Inactive", None),
    ("excluded", "Excluded from jobs", None),
]


def posted_designs() -> set[int]:
    """Issues whose design was posted from this dashboard (comments can scroll out of view)."""
    out = set()
    for f in (WORKDIR / "jobs").glob("*/job.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if j.get("type") == "design" and j.get("status") == "posted":
            out.add(j["pr"])
    return out


def issue_stage(labels: list[str], status_role: str | None, title: str,
                design_posted: bool, linked_pr: bool, landable: bool) -> str:
    if WORKFLOW.excludes(title):
        return "excluded"
    if LABELS.has(labels, "tracker"):
        return "tracker"
    if LABELS.has(labels, "wontfix"):
        return "wontfix"
    if landable:
        return "ready-to-merge"
    if status_role == "needs-information":
        return "info"
    if status_role == "needs-scope-review" or LABELS.has(labels, "maintainer_decision"):
        return "scope"
    if status_role == "waiting-on-dependency":
        return "blocked"
    if status_role == "inactive":
        return "inactive"
    if status_role == "ready-for-human" or "ready-for-human" in (label.casefold() for label in labels):
        return "ready-for-human"
    if PROJECT.design_enabled():
        if LABELS.has(labels, "design_approved") and not linked_pr:
            return "implementing" if status_role == "in-progress" else "approved"
        if status_role == "waiting-on-author" and design_posted and not linked_pr:
            return "design-changes"
        if status_role == "design-review" or (design_posted and not linked_pr):
            return "design-posted"
    if linked_pr or status_role in ("waiting-on-review", "ready-to-merge", "waiting-on-author"):
        return "in-review"
    return {"design-needed": "design-needed", "ready-for-work": "ready",
            "in-progress": "implementing", "design-review": "design-posted"}.get(status_role, "triage")


def evaluate_issues(issues: list[dict], prs: list[dict]) -> dict:
    dashboard_designs = posted_designs()
    pr_by_issue = {}
    for p in prs:
        for n in p["closes"]:
            pr_by_issue.setdefault(n, []).append(p)

    rows, alerts = [], []
    report_number = reports_issue()
    for i in issues:
        if i["number"] == report_number:
            continue  # the scheduled-triage report log, not work
        labels = label_names(i)
        status = LABELS.status_labels(labels)
        role = LABELS.role(status[0]) if status else None
        prio = min((l for l in labels if LABELS.rank(l, LABELS.priorities) < len(LABELS.priorities)),
                   key=lambda l: LABELS.rank(l, LABELS.priorities), default=None)
        prio_rank = LABELS.rank(prio, LABELS.priorities) if prio else None
        assignees = [a["login"] for a in i["assignees"]["nodes"]]
        upd = age_days(i["updatedAt"])
        row = {
            "number": i["number"], "title": i["title"], "url": i["url"],
            "author": (i["author"] or {}).get("login", "ghost"),
            "labels": labels, "status": status[0] if status else None, "statusRole": role,
            "priority": prio, "priorityRank": prio_rank,
            "areas": [l for l in labels if LABELS.area_prefix and l.casefold().startswith(LABELS.area_prefix.casefold())],
            "assignees": assignees, "milestone": (i.get("milestone") or {}).get("number"),
            "comments": i["comments"]["totalCount"],
            "updatedAgeDays": upd, "createdAgeDays": age_days(i["createdAt"]), "updatedAt": i["updatedAt"],
            "recentComments": recent_comments(i),
            "bodyEditedAt": i["lastEditedAt"], "bodyEditor": (i["editor"] or {}).get("login"),
            "refs": [{"number": e["source"]["number"], "url": e["source"]["url"], "title": e["source"]["title"],
                      "kind": "PR" if e["source"]["__typename"] == "PullRequest" else "issue",
                      "state": e["source"]["state"].lower(), "at": e["createdAt"],
                      "by": (e["actor"] or {}).get("login", "ghost")}
                     for e in i["refs"]["nodes"] if e and e.get("source") and e["source"].get("number")],
            "decision": LABELS.has(labels, "maintainer_decision") or role == "needs-scope-review",
            "tracker": LABELS.has(labels, "tracker"),
            "prs": [p["number"] for p in pr_by_issue.get(i["number"], [])],
            "flags": [],
        }
        kind = min((l for l in labels if LABELS.rank(l, LABELS.kinds) < len(LABELS.kinds)),
                   key=lambda l: LABELS.rank(l, LABELS.kinds), default=None)
        design_posted = i["number"] in dashboard_designs or any(
            DESIGN_RE.search(c["text"]) for c in row["recentComments"] if c["by"].casefold() in MAINTAINERS)
        has_claim = LABELS.has(labels, "claimed")
        linked = pr_by_issue.get(i["number"], [])
        row.update({
            "kind": kind, "excluded": WORKFLOW.excludes(i["title"]),
            "designPosted": design_posted, "designApproved": LABELS.has(labels, "design_approved"),
            "stage": issue_stage(labels, role, i["title"], design_posted, bool(linked),
                                 any(p["verdict"] in ("ready", "needs-auth") for p in linked)),
            "order": [LABELS.rank(prio, LABELS.priorities), LABELS.rank(kind, LABELS.kinds),
                      -(row["createdAgeDays"] or 0)],
        })
        if len(status) > 1 or (WORKFLOW.require_status_label and not status):
            row["flags"].append(f"{len(status)} status labels")
        if prio_rank in (0, 1) and role == "ready-for-work" and not assignees:
            row["flags"].append(f"{prio} ready, unassigned")
        if (has_claim or role == "in-progress") and upd is not None and upd > STALE_DAYS:
            row["flags"].append(f"claim quiet {int(upd)}d")
        if has_claim and not assignees:
            row["flags"].append("claimed but unassigned")
        for p in linked:
            if p.get("statusRole") and p["statusRole"] != role and role in (
                    "waiting-on-review", "ready-to-merge", "waiting-on-author"):
                row["flags"].append(f"Issue says {row['status']}, PR #{p['number']} says {p['status']}")
        rows.append(row)
        for f in row["flags"]:
            alerts.append({"kind": "issue", "number": i["number"], "url": i["url"], "text": f})

    def count(key_fn):
        out: dict[str, int] = {}
        for r in rows:
            for k in key_fn(r):
                out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))

    rows.sort(key=lambda r: r["order"])
    design_tasks = {"design", "approve", "implement"}
    stages = [{"key": k, "label": label, "task": task if task not in design_tasks or PROJECT.design_enabled() else None,
               "count": sum(r["stage"] == k for r in rows)} for k, label, task in STAGES]
    return {
        "rows": rows, "alerts": alerts, "stages": [s for s in stages if s["count"] or s["key"] == "triage"],
        "byStatus": count(lambda r: [r["status"] or "(none)"]),
        "byPriority": count(lambda r: [r["priority"] or "(none)"]),
        "byArea": count(lambda r: r["areas"] or ["(none)"]),
    }


# ----------------------------------------------------------------------- CI

def ci_health() -> dict:
    branch = base_branch()
    if not branch:
        return {"main": [], "recentFailures": 0, "recentTotal": 0, "startupFailures": 0}
    workflow = ["--workflow", WORKFLOW.ci_workflow] if WORKFLOW.ci_workflow else []
    runs = gh_json(["run", "list", "-R", REPO, "--branch", branch, *workflow, "--limit", "8",
                    "--json", "databaseId,workflowName,status,conclusion,createdAt,headSha,url,event"])
    recent = gh_json(["run", "list", "-R", REPO, "--limit", "30",
                      "--json", "conclusion,status,createdAt"])
    bad = [r for r in recent if r["conclusion"] in ("startup_failure", "failure", "cancelled")]
    return {"main": runs, "workflow": WORKFLOW.ci_workflow, "recentFailures": len(bad), "recentTotal": len(recent),
            "startupFailures": sum(1 for r in recent if r["conclusion"] == "startup_failure")}


# --------------------------------------------------------------------- collect

def reports_issue() -> int:
    return PROJECT.reports_issue


def triage_reports() -> dict:
    n = reports_issue()
    if not n:
        raise ValueError("set triage_reports_issue in config.json")
    p = subprocess.run(["gh", "api", f"repos/{REPO}/issues/{n}"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f"could not read issue #{n}: " + (p.stderr or p.stdout).strip()[-300:])
    issue = json.loads(p.stdout)
    body = (issue.get("body") or "").replace("\r\n", "\n")
    reports = [{"at": at, "markdown": md.strip()} for at, md in
               re.findall(r"<!-- report:start (\S+) -->\n(.*?)\n<!-- report:end -->", body, re.S)]
    return {"issue": n, "url": issue.get("html_url"), "updatedAt": issue.get("updated_at"), "reports": reports[:2]}


def collect() -> dict:
    started = now()
    repo_info.cache_clear()
    info, branch = repo_info(), base_branch()
    rules = read_rules()
    main_sha = (gh_json(["api", f"repos/{REPO}/commits/{quote(branch, safe='')}", "--jq",
                         "{sha:.sha,date:.commit.committer.date,msg:.commit.message}"])
                if branch and not info["isEmpty"] else {"sha": "", "date": None, "msg": ""})
    raw_prs = [] if info["isEmpty"] else paged(PR_QUERY, "pullRequests")
    by_head = {p["headRefName"]: p for p in raw_prs
               if (p.get("headRepository") or {}).get("nameWithOwner", "").casefold() == REPO.casefold()}
    prs = [evaluate_pr(p, rules, by_head) for p in raw_prs]
    order = landing_order(prs)
    issues = evaluate_issues(paged(ISSUE_QUERY, "issues") if info["hasIssuesEnabled"] else [], prs)
    alerts = []
    for p in prs:
        for m in p["mismatch"]:
            alerts.append({"kind": "pr", "number": p["number"], "url": p["url"], "text": m})
        if p["statusRole"] == "waiting-on-author" and (p["statusAgeDays"] or 0) > STALE_DAYS:
            alerts.append({"kind": "pr", "number": p["number"], "url": p["url"],
                           "text": f"waiting on author {int(p['statusAgeDays'])}d (target {STALE_DAYS}d)"})
    try:
        ci = ci_health()
    except RuntimeError as e:  # a check that could not run is BLOCKED, never a silent pass
        ci = {"error": str(e)}
    if ci.get("startupFailures"):
        alerts.append({"kind": "ci", "number": None, "url": f"https://github.com/{REPO}/actions",
                       "text": f"{ci['startupFailures']} recent runs failed to start (runner/billing?)"})
    snap = {
        "repo": REPO, "baseBranch": branch, "generatedAt": started.isoformat(timespec="seconds"),
        "durationSec": round((now() - started).total_seconds(), 1),
        "maintainers": sorted(MAINTAINERS), "rules": rules, "main": main_sha,
        "canLand": info["viewerPermission"] in ("ADMIN", "MAINTAIN", "WRITE"),
        "workflow": {"statuses": LABELS.statuses, "labels": LABELS.special,
                     "standingAuthorization": sorted(WORKFLOW.standing_authorization),
                     "postAuthorizationComment": WORKFLOW.post_authorization_comment,
                     "reportsEnabled": bool(reports_issue()), "issuesEnabled": info["hasIssuesEnabled"]},
        "prs": [p for p in order], "issues": issues, "ci": ci,
        "alerts": [a for a in alerts] + issues["alerts"],
    }
    SNAPDIR.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    snap["file"] = f"snapshot-{stamp}.json"
    write_json(SNAPDIR / snap["file"], snap)
    write_json(SNAPDIR / "latest.json", snap)
    prune_snapshots()
    return snap


# ------------------------------------------------------------------- history

SNAP_RE = re.compile(r"^snapshot-\d{8}T\d{6}Z\.json$")
KEEP_SNAPSHOTS = 300  # ~5 months at two scheduled runs a day plus manual refreshes


def snapshot_files() -> list[Path]:
    return sorted(p for p in SNAPDIR.glob("snapshot-*.json") if SNAP_RE.match(p.name))


def prune_snapshots() -> None:
    for p in snapshot_files()[:-KEEP_SNAPSHOTS]:
        p.unlink(missing_ok=True)


def list_snapshots() -> list[dict]:
    out = []
    for p in snapshot_files():
        ts = dt.datetime.strptime(p.name[9:25], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        out.append({"file": p.name, "generatedAt": ts.isoformat(timespec="seconds")})
    return out


def load_snapshot(name: str) -> dict:
    if not SNAP_RE.match(name) or not (SNAPDIR / name).exists():
        raise ValueError(f"unknown snapshot {name!r}")
    snap = json.loads((SNAPDIR / name).read_text(encoding="utf-8"))
    snap["file"] = name  # snapshots from before change tracking lack it
    return snap


_gone_cache: dict[tuple[str, int], str] = {}


def gone_state(kind: str, number: int) -> str:
    """What happened to a PR or issue that left the open set (merged, closed, ...)."""
    key = (kind, number)
    if key not in _gone_cache:
        try:
            if kind == "pr":
                d = gh_json(["pr", "view", str(number), "-R", REPO, "--json", "state,mergedAt,mergedBy"])
                _gone_cache[key] = ("merged" + (f" by {d['mergedBy']['login']}" if d.get("mergedBy") else "")
                                    if d["state"] == "MERGED" else d["state"].lower())
            else:
                d = gh_json(["issue", "view", str(number), "-R", REPO, "--json", "state,stateReason"])
                _gone_cache[key] = f"{d['state'].lower()} ({(d.get('stateReason') or '').lower().replace('_', ' ')})".replace(" ()", "")
        except RuntimeError:
            _gone_cache[key] = "no longer open (state BLOCKED: lookup failed)"
    return _gone_cache[key]


HIGH, MED, LOW = 0, 1, 2
VERDICT_WORDS = {"ready": "ready (standing auth)", "needs-auth": "ready, needs your OK",
                 "refresh": "needs a refresh", "review": "needs attention",
                 "author": "waiting on author", "parked": "parked"}


def status_story(a: str | None, b: str | None) -> str:
    return f"status {a or 'none'} → {b or 'none'}"


LABEL_STORY = {
    ("+", "maintainer_decision"): "maintainer decision requested",
    ("-", "maintainer_decision"): "maintainer decision recorded",
    ("+", "claimed"): "claimed",
    ("-", "claimed"): "claim released",
}


def label_changes(a_labels, b_labels, skip) -> tuple[list[str], list[str]]:
    """(stories for meaningful label moves, plain +/- list for the rest)."""
    la, lb = set(a_labels), set(b_labels)
    stories, plain = [], []
    for l in sorted((lb - la) | (la - lb)):
        if skip(l):
            continue
        sign = "+" if l in lb else "-"
        role = next((role for role, name in LABELS.special.items() if l.casefold() == name.casefold()), None)
        if (sign, role) in LABEL_STORY:
            stories.append(LABEL_STORY[(sign, role)])
        else:
            plain.append(("+" if sign == "+" else "−") + l)
    return stories, plain


def diff(old: dict, new: dict) -> dict:
    if old.get("repo", "").casefold() != new.get("repo", "").casefold() or old.get("baseBranch") != new.get("baseBranch"):
        raise ValueError("Choose snapshots from the same repository and base branch")
    changes: list[dict] = []
    since = parse_ts(old["generatedAt"])

    def add(sev, kind, item, text, **extra):
        changes.append({"severity": sev, "kind": kind,
                        "number": item.get("number") if item else None,
                        "url": item.get("url") if item else None,
                        "title": item.get("title") if item else None, "text": text, **extra})

    def conversation(kind, a, b):
        """New comments (with who and what), description edits. Returns True if any."""
        found = False
        if "recentComments" in a and "recentComments" in b:
            seen = {c["id"] for c in a["recentComments"]}
            fresh = [c for c in b["recentComments"] if c["id"] not in seen and parse_ts(c["at"]) > since]
            for c in fresh:
                add(LOW if BOT_RE.search(c["by"]) else MED, kind, b, f"comment by {c['by']}",
                    quote={"by": c["by"], "text": c["text"], "url": c["url"], "at": c["at"]})
            old_n = a.get("commentCount", a.get("comments", 0))
            new_n = b.get("commentCount", b.get("comments", 0))
            hidden = (new_n - old_n) - len(fresh)
            if hidden > 0:
                add(LOW, kind, b, f"+{hidden} more comment{'s' if hidden > 1 else ''} (open it to read)")
            found = bool(fresh) or hidden > 0
        elif b.get("comments", 0) > a.get("comments", 0):
            k = b["comments"] - a["comments"]
            add(LOW, kind, b, f"{k} new comment{'s' if k > 1 else ''}")
            found = True
        if "bodyEditedAt" in a and b.get("bodyEditedAt") and b["bodyEditedAt"] != a.get("bodyEditedAt"):
            what = "description edited" + (f" by {b['bodyEditor']}" if b.get("bodyEditor") else "")
            add(MED, kind, b, what + (" (acceptance criteria may have changed)" if kind == "issue" else ""))
            found = True
        return found

    if old["main"]["sha"] != new["main"]["sha"]:
        add(MED, "main", None, f"{new['baseBranch']} moved {old['main']['sha'][:9]} → {new['main']['sha'][:9]}: "
                               f"{new['main']['msg'].partition(chr(10))[0][:90]}")

    op = {p["number"]: p for p in old["prs"]}
    np = {p["number"]: p for p in new["prs"]}
    for n in sorted(np.keys() - op.keys()):
        p = np[n]
        add(MED, "pr", p, f"opened by {p['author']} ({VERDICT_WORDS[p['verdict']]})")
    for n in sorted(op.keys() - np.keys()):
        p = op[n]
        state = gone_state("pr", n)
        add(HIGH if state.startswith("merged") else MED, "pr", p, state)
    for n in sorted(op.keys() & np.keys()):
        a, b = op[n], np[n]
        if a["head"] != b["head"]:
            add(HIGH, "pr", b, f"new head {a['head'][:9]} → {b['head'][:9]}: earlier review no longer covers it")
        if a["verdict"] != b["verdict"]:
            up = VERDICT_RANK[b["verdict"]] < VERDICT_RANK[a["verdict"]]
            sev = HIGH if b["verdict"] in ("ready", "needs-auth") else MED
            add(sev, "pr", b, f"{'▲' if up else '▼'} {VERDICT_WORDS[a['verdict']]} → {VERDICT_WORDS[b['verdict']]}")
        for k, g in b["gates"].items():
            ga = a["gates"].get(k)
            if ga and ga["ok"] != g["ok"]:
                sev = HIGH if k == "checks" and not g["ok"] and "failed" in g["text"] else MED
                add(sev, "pr", b, f"{k} gate {'passed' if g['ok'] else 'failed'}: {g['text']}")
        ra, rb = a.get("lastReview"), b.get("lastReview")
        if rb and (not ra or (ra["at"], ra["commit"]) != (rb["at"], rb["commit"])):
            add(MED, "pr", b, f"new review by {rb['by']} ({rb['state'].lower()}) on {rb['commit']}")
        if a["status"] != b["status"]:
            add(MED, "pr", b, status_story(a["status"], b["status"]))
        stories, plain = label_changes(a["labels"], b["labels"], LABELS.role)
        for st in stories:
            add(MED, "pr", b, st)
        if plain:
            add(LOW, "pr", b, "labels " + ", ".join(plain))
        talked = conversation("pr", a, b)
        if (not talked and a.get("updatedAt") and b.get("updatedAt") and a["updatedAt"] != b["updatedAt"]
                and a["head"] == b["head"] and a["status"] == b["status"] and ra == rb):
            add(LOW, "pr", b, "other activity (edits or reactions)")

    oi = {i["number"]: i for i in old["issues"]["rows"]}
    ni = {i["number"]: i for i in new["issues"]["rows"]}
    for n in sorted(ni.keys() - oi.keys()):
        i = ni[n]
        detail = ', '.join(value for value in (i.get('priority'), i.get('status')) if value)
        add(HIGH if i.get("priorityRank") == 0 else MED, "issue", i,
            f"opened by {i['author']}" + (f" ({detail})" if detail else ""))
        for c in i.get("recentComments", []):
            if c["by"] != i["author"] and parse_ts(c["at"]) > since:
                add(MED, "issue", i, f"comment by {c['by']}",
                    quote={"by": c["by"], "text": c["text"], "url": c["url"], "at": c["at"]})
    for n in sorted(oi.keys() - ni.keys()):
        add(MED, "issue", oi[n], gone_state("issue", n))
    for n in sorted(oi.keys() & ni.keys()):
        a, b = oi[n], ni[n]
        if a["status"] != b["status"]:
            add(MED, "issue", b, status_story(a["status"], b["status"]))
        if a["priority"] != b["priority"]:
            add(HIGH if b.get("priorityRank") == 0 else MED, "issue", b, f"priority {a['priority'] or 'none'} → {b['priority'] or 'none'}")
        if a["assignees"] != b["assignees"]:
            gained = [x for x in b["assignees"] if x not in a["assignees"]]
            lost = [x for x in a["assignees"] if x not in b["assignees"]]
            add(MED, "issue", b, "; ".join(
                ([f"picked up by {', '.join(gained)}"] if gained else []) +
                ([f"unassigned {', '.join(lost)}"] if lost else [])))
        for f in sorted(set(b["flags"]) - set(a["flags"])):
            add(MED, "issue", b, f"new flag: {f}")
        stories, plain = label_changes(a["labels"], b["labels"],
                                       lambda l: bool(LABELS.role(l)) or l in LABELS.priorities)
        for st in stories:
            add(MED, "issue", b, st)
        if plain:
            add(LOW, "issue", b, "labels " + ", ".join(plain))
        if "refs" in a and "refs" in b:
            seen = {(r["number"], r["at"]) for r in a["refs"]}
            for r in b["refs"]:
                if (r["number"], r["at"]) not in seen and parse_ts(r["at"]) > since:
                    add(MED if r["kind"] == "PR" else LOW, "issue", b,
                        f"referenced from {r['kind']} #{r['number']} ({r['state']}): {r['title'][:70]}",
                        link=r["url"])
        conversation("issue", a, b)

    def main_ci(s):
        runs = (s.get("ci") or {}).get("main", [])
        return runs[0] if runs else None
    ca, cb = main_ci(old), main_ci(new)
    if cb and (not ca or ca["databaseId"] != cb["databaseId"]) and cb.get("conclusion") not in (None, "", "success"):
        add(HIGH, "ci", {"url": cb["url"]}, f"{cb['workflowName']} on {new['baseBranch']}: {cb['conclusion']}")

    changes.sort(key=lambda c: (c["severity"], c["kind"] != "main", c["number"] or 0))
    touched = sorted({c["number"] for c in changes if c["kind"] == "pr" and c["number"] in np})
    return {"base": {"file": old.get("file"), "generatedAt": old["generatedAt"]},
            "latest": {"file": new.get("file"), "generatedAt": new["generatedAt"]},
            "changes": changes, "touchedPrs": touched,
            "touchedIssues": sorted({c["number"] for c in changes if c["kind"] == "issue" and c["number"] in ni})}


# ---------------------------------------------------------------------- server

_lock = threading.Lock()
AUTO_REFRESH_MIN = 10
_scheduler = None
_worker_token = os.environ.get("TRIAGE_WORKER_TOKEN")
_worker_paused = False


def refresh_in_background() -> None:
    """Take a new snapshot without blocking the caller; skipped if one is already running."""
    def work():
        if _worker_paused or not _lock.acquire(blocking=False):
            return
        try:
            collect()
        except Exception as e:  # logged; the next refresh tries again
            print(f"[triage] background refresh failed: {e}", file=sys.stderr)
        finally:
            _lock.release()
    threading.Thread(target=work, daemon=True).start()


def _auto_refresh_loop() -> None:
    import time
    while True:
        time.sleep(AUTO_REFRESH_MIN * 60)
        refresh_in_background()


class Handler(BaseHTTPRequestHandler):
    def _allowed_host(self) -> bool:  # DNS-rebinding guard
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and (
            not _worker_token or self.headers.get("X-Triage-Internal") == _worker_token)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, b"forbidden", "text/plain")
        url = urlparse(self.path)
        include = (parse_qs(url.query).get("include") or [None])[0]
        if url.path == "/api/schedule" and _scheduler:
            return self._send(200, json.dumps(_scheduler.status(include)).encode(), "application/json")
        if url.path == "/api/jobs":
            import jobs
            latest = SNAPDIR / "latest.json"
            snap = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {"prs": []}
            try:
                viewer = jobs.viewer()
            except RuntimeError:
                viewer = ""
            visible = [j for j in jobs.list_jobs() if not j.get("dismissed")]
            recent = visible[:100] + [j for j in visible[100:] if j["id"] == include]
            ui_files = ("dashboard.html", "projects.js", "inbox.js", "workflow-editor.js")
            body = {"repo": REPO, "jobs": recent,
                    "candidates": [p["number"] for p in jobs.review_candidates(snap)],
                    "config": jobs.config(), "implConfig": jobs.impl_config(),
                    "viewer": viewer,
                    "uiVersion": str(max((HERE / name).stat().st_mtime_ns for name in ui_files)),
                    "snapshotAt": snap.get("generatedAt")}  # the page reloads the snapshot when this moves
            return self._send(200, json.dumps(body).encode(), "application/json")
        m = re.fullmatch(r"/api/issues/(\d+)/design", url.path)
        if m:
            import jobs
            try:
                return self._send(200, json.dumps(jobs.read_design(int(m.group(1)))).encode(), "application/json")
            except (ValueError, RuntimeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/diff", url.path)
        if m:
            f = WORKDIR / "jobs" / m.group(1) / "diff.patch"
            if not f.exists():
                return self._send(404, b"no diff for this job", "text/plain")
            return self._send(200, f.read_bytes(), "text/plain; charset=utf-8")
        if url.path == "/api/triage-reports":
            try:
                return self._send(200, json.dumps(triage_reports()).encode(), "application/json")
            except (ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
                return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
        if url.path == "/api/snapshots":
            return self._send(200, json.dumps(list_snapshots()).encode(), "application/json")
        if url.path == "/api/changes":
            base = (parse_qs(url.query).get("base") or [""])[0]
            try:
                latest = json.loads((SNAPDIR / "latest.json").read_text(encoding="utf-8"))
                result = diff(load_snapshot(base), latest)
            except (ValueError, FileNotFoundError, KeyError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            return self._send(200, json.dumps(result).encode(), "application/json")
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/vendor/marked.min.js", "/vendor/purify.min.js"):  # fixed list: no path traversal
            f = HERE / url.path.lstrip("/")
            if not f.exists():
                return self._send(404, b"missing vendor file", "text/plain")
            return self._send(200, f.read_bytes(), "text/javascript; charset=utf-8")
        if self.path == "/api/snapshot":
            f = SNAPDIR / "latest.json"
            if not f.exists():
                return self._send(404, b'{"error":"no snapshot yet"}', "application/json")
            return self._send(200, f.read_bytes(), "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        global _worker_paused
        if self.path == "/api/internal/quiesce" and _worker_token and self._allowed_host():
            import jobs
            with _scheduler.lock:
                if _lock.locked() or _scheduler.running or any(j["status"] in jobs.ACTIVE for j in jobs.list_jobs()):
                    return self._send(409, b'{"error":"Wait for this repository\'s active jobs or refresh to finish."}',
                                      "application/json")
                _worker_paused = _scheduler.paused = True
                return self._send(200, b"{}", "application/json")
        # Custom header forces a CORS preflight, which this server never answers,
        # so other web pages cannot trigger refreshes.
        if _worker_paused or not self._allowed_host() or self.headers.get("X-Triage") != "1":
            return self._send(403, b"forbidden", "text/plain")
        if self.path == "/api/schedule/run" and _scheduler:
            if (self.headers.get("X-Triage-Repo") or "").casefold() != REPO.casefold():
                return self._send(409, b'{"error":"Reload this project before requesting a run."}', "application/json")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                return self._send(202, json.dumps(_scheduler.start(req.get("lane"))).encode(), "application/json")
            except (ValueError, TypeError, AttributeError) as exc:
                return self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        if self.path.startswith(("/api/jobs", "/api/issues/", "/api/prs/")):
            if (self.headers.get("X-Triage-Repo") or "").casefold() != REPO.casefold():
                return self._send(409, b'{"error":"The repository changed. Reload the dashboard and try again."}',
                                  "application/json")
            return self._jobs_post()
        if self.path == "/api/markdown":
            return self._markdown_post()
        if self.path != "/api/refresh":
            return self._send(404, b"not found", "text/plain")
        if not _lock.acquire(blocking=False):
            return self._send(409, b'{"error":"refresh already running"}', "application/json")
        try:
            snap = collect()
            self._send(200, json.dumps(snap).encode(), "application/json")
        except Exception as e:  # report, never pretend success
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        finally:
            _lock.release()

    def _jobs_post(self):
        import jobs
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("request too large")
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            parts = self.path.strip("/").split("/")  # api/jobs[/<id>/<action>] | api/issues/<n>/approve-design
            if parts[:2] == ["api", "issues"] and len(parts) == 4 and parts[3] == "approve-design":
                result = jobs.approve_design(int(parts[2]), str(req.get("digest") or ""))
            elif parts[:2] == ["api", "prs"] and len(parts) == 4 and parts[3] == "land":
                result = jobs.land_pr(int(parts[2]), str(req.get("head") or ""))
            elif parts[:2] == ["api", "prs"] and len(parts) == 4 and parts[3] == "ready":
                result = jobs.mark_ready(int(parts[2]), str(req.get("head") or ""))
            elif parts[:2] == ["api", "prs"] and len(parts) == 4 and parts[3] == "rebase":
                result = jobs.rebase_pr(int(parts[2]), str(req.get("head") or ""))
            elif len(parts) == 2 and req.get("task"):
                nums = req.get("issues")
                if req.get("allInStage"):
                    snap = json.loads((SNAPDIR / "latest.json").read_text(encoding="utf-8"))
                    stages = jobs.ISSUE_TASKS.get(req["task"], set())
                    nums = [r["number"] for r in snap["issues"]["rows"]
                            if r["stage"] in stages and not r["excluded"]][: int(req.get("limit") or 50)]
                if not isinstance(nums, list) or not all(isinstance(x, int) for x in nums):
                    raise ValueError("issues must be a list of issue numbers")
                result = jobs.enqueue_issue(req["task"], nums, force=bool(req.get("force")),
                                            digest=req.get("digest"))
            elif len(parts) == 2:
                prs = req.get("prs")
                if req.get("allWaiting"):
                    snap = json.loads((SNAPDIR / "latest.json").read_text(encoding="utf-8"))
                    prs = [p["number"] for p in jobs.review_candidates(snap)]
                if not isinstance(prs, list) or not all(isinstance(x, int) for x in prs):
                    raise ValueError("prs must be a list of PR numbers")
                result = jobs.enqueue(prs, force=bool(req.get("force")))
            elif len(parts) == 4 and parts[3] == "cancel":
                result = jobs.cancel(parts[2])
            elif len(parts) == 4 and parts[3] == "dismiss":
                result = jobs.dismiss(parts[2])
            elif len(parts) == 4 and parts[3] == "post":
                job = jobs.load(parts[2])
                if job.get("type") == "implement" and job["status"] == "stopped":
                    result = jobs.post_blocker(parts[2], req.get("body", ""), req.get("opts") or {})
                elif job.get("type") == "implement":
                    result = jobs.publish_impl(parts[2], req.get("title", ""), req.get("body", ""), req.get("opts") or {})
                elif job.get("target") == "issue":
                    result = jobs.post_issue(parts[2], req.get("body", ""), req.get("opts") or {})
                else:
                    result = jobs.post(parts[2], req.get("decision", ""), req.get("body", ""), bool(req.get("setLabel")))
            else:
                return self._send(404, b'{"error":"not found"}', "application/json")
            if parts[-1] in ("post", "approve-design", "land", "ready", "rebase"):
                refresh_in_background()  # these change labels/assignees on GitHub; show it without a manual Refresh
            return self._send(200, json.dumps(result).encode(), "application/json")
        except (ValueError, FileNotFoundError) as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        except RuntimeError as e:
            return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")

    def _markdown_post(self):
        """Render Markdown exactly as GitHub would (POST /markdown, gfm mode in this repo's context).
        Read-only: GitHub stores nothing. The page still sanitizes the HTML before showing it."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("request too large")
            text = str((json.loads(self.rfile.read(n) or b"{}") if n else {}).get("text") or "")
            p = subprocess.run(["gh", "api", "markdown", "--input", "-"],
                               input=json.dumps({"text": text, "mode": "gfm", "context": REPO}),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            if p.returncode != 0:
                raise RuntimeError("GitHub could not render it: " + (p.stderr or p.stdout).strip()[-300:])
            return self._send(200, json.dumps({"html": p.stdout}).encode(), "application/json")
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("[triage] " + fmt % args + "\n")


def main() -> int:
    global _scheduler
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
    d = sub.add_parser("diff", help="print changes between a snapshot and the latest")
    d.add_argument("base", nargs="?", help="snapshot file name (default: the previous one)")
    rv = sub.add_parser("review", help="draft reviews now (never posts)")
    g = rv.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, action="append", help="PR number (repeatable)")
    g.add_argument("--all-waiting", action="store_true", help="every PR waiting on review of its current head")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.cmd == "serve" and not a.worker:
        from project_server import serve
        serve(a.port, not a.no_browser)
        return 0
    if not REPO:
        ap.error('Set "repo" in config.json or set TRIAGE_REPO to owner/name.')
    if a.cmd == "collect":
        snap = collect()
        counts = {}
        for p in snap["prs"]:
            counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1
        print(f"{snap['generatedAt']}  {len(snap['prs'])} PRs {counts}  "
              f"{len(snap['issues']['rows'])} issues  {len(snap['alerts'])} alerts  "
              f"({snap['durationSec']}s)")
        return 0
    if a.cmd == "diff":
        files = [f["file"] for f in list_snapshots()]
        base = a.base or (files[-2] if len(files) > 1 else None)
        if not base:
            print("need at least two snapshots")
            return 1
        latest = json.loads((SNAPDIR / "latest.json").read_text(encoding="utf-8"))
        r = diff(load_snapshot(base), latest)
        print(f"changes {r['base']['generatedAt']} → {r['latest']['generatedAt']}: {len(r['changes'])}")
        for c in r["changes"]:
            ref = f"#{c['number']}" if c["number"] else ""
            print(f"  {'!·.'[c['severity']]} {c['kind']:5} {ref:6} {c['text']}")
            if c.get("quote"):
                print(f"{'':16}“{c['quote']['text'][:140]}”")
        return 0
    if a.cmd == "review":
        import jobs
        snap = collect()
        prs = a.pr or [p["number"] for p in jobs.review_candidates(snap)]
        print(f"reviewing {prs or 'nothing (no PR is waiting on review)'}")
        for r in jobs.run_sync(prs):
            if "skipped" in r:
                print(f"  #{r['skipped']['pr']} skipped: {r['skipped']['reason']}")
            else:
                cost = f"${r['costUsd']:.2f}, " if r.get("costUsd") is not None else ""
                print(f"  #{r['pr']} {r['status']}: {r.get('decision') or ''} {r.get('summary') or r.get('error') or ''}"
                      f"  ({cost}job {r['id']})")
        return 0
    import jobs
    from scheduled_triage import Scheduler, parse_schedule

    def collect_for_schedule():
        if _worker_paused:
            raise RuntimeError("The project is reloading")
        with _lock:
            return collect()

    def enqueue_for_schedule(lane, numbers):
        if lane == "issues":
            return jobs.enqueue_issue("triage", numbers, force=True, scheduled=True)
        return jobs.enqueue(numbers)

    cfg = parse_schedule(target_config(read_config(), REPO).get("schedule"))
    stop = threading.Event()
    _scheduler = Scheduler(WORKDIR / "schedule.json", cfg, collect_for_schedule, jobs.list_jobs,
                           jobs.review_candidates, enqueue_for_schedule)
    jobs.start_worker()
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if a.ready_file:
        a.ready_file.write_text(str(srv.server_port), encoding="ascii")
        def watch_parent():
            sys.stdin.buffer.read()
            srv.shutdown()
        threading.Thread(target=watch_parent, daemon=True).start()
    print(f"[triage] serving {REPO} on {srv.server_port}", flush=True)
    threading.Thread(target=_scheduler.loop, args=(stop,), daemon=True).start()
    refresh_in_background()
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.modules.setdefault("triage", sys.modules[__name__])
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit(str(exc))
