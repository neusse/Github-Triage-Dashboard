"""Run dashboard jobs with headless Codex or Claude Code.

Safety model
- Reviews and issue drafts use a read-only checkout and pre-fetched GitHub context.
- Implementation edits a local worktree and runs an independent gate before publishing.
- Nothing is posted automatically. `post()` is called only from an explicit user
  action, re-checks that the head has not moved, and never submits an approval.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

import triage as T
from project_config import read_config, target_config

HERE = Path(__file__).resolve().parent
WORK = T.WORKDIR
REPO_DIR = WORK / "repo"
JOBS_DIR = WORK / "jobs"
PROMPT = HERE / "prompts" / "review.md"

DEFAULTS = {
    "model": None,            # None: the selected CLI's default model
    "effort": "high",
    "max_budget_usd": 5.0,    # per review
    "timeout_min": 40,
}
ALLOWED_TOOLS = ["Read", "Grep", "Glob",
                 "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)",
                 "Bash(git range-diff:*)", "Bash(git merge-base:*)", "Bash(git blame:*)",
                 "Bash(git ls-files:*)", "Bash(git rev-parse:*)"]
DENIED_TOOLS = ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Bash(gh:*)",
                "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git reset:*)",
                "Bash(curl:*)", "Bash(rm:*)"]
ACTIVE = ("queued", "preparing", "running", "gating")


IMPL_DEFAULTS = {
    "model": None,
    "effort": "high",
    "max_budget_usd": 25.0,   # per implementation
    "timeout_min": 180,       # agent run; the independent gate has its own timeout
    "gate_timeout_min": 90,
    "git_name": None,         # commit identity for implementation commits (None: the clone's git config)
    "git_email": None,
    "gh_token_env": None,     # name of an env var holding a bot account's token; None: your gh login
    "bot_login": None,        # the bot's GitHub login: its own claims count as ours
}
IMPL_ALLOWED = ["Read", "Grep", "Glob", "Edit", "Write",
                "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
                "Bash(git add:*)", "Bash(git commit:*)", "Bash(git rm:*)", "Bash(git mv:*)", "Bash(git restore:*)",
                "Bash(git blame:*)", "Bash(git ls-files:*)", "Bash(git rev-parse:*)", "Bash(git merge-base:*)",
                "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(mkdir:*)"]
IMPL_DENIED = ["NotebookEdit", "WebFetch", "WebSearch", "Bash(gh:*)", "Bash(git push:*)", "Bash(git fetch:*)",
               "Bash(git pull:*)", "Bash(git remote:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
               "Bash(git reset:*)", "Bash(git rebase:*)", "Bash(git stash:*)", "Bash(git worktree:*)",
               "Bash(git config:*)", "Bash(git branch:*)", "Bash(git tag:*)", "Bash(curl:*)", "Bash(wget:*)",
               "Bash(rm:*)"]


def _config_file() -> dict:
    return target_config(read_config(), T.REPO)


def config(is_impl: bool = False) -> dict:
    settings = _config_file()
    cfg = dict(IMPL_DEFAULTS if is_impl else DEFAULTS)
    cfg.update(settings.get("implement" if is_impl else "review", {}))
    cfg["agent"] = settings.get("agent", "codex")
    if not is_impl:
        cfg["gh_token_env"] = settings.get("implement", {}).get("gh_token_env")
    return cfg


def impl_config() -> dict:
    return config(is_impl=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def git_auth_env(base: dict | None = None) -> dict:
    """Git authenticates to GitHub through the gh login, whatever shell started the server.

    Without this, git falls back to the machine's credential helper, which may have no stored login and
    cannot prompt from a background server.
    """
    env = dict(base if base is not None else os.environ)
    gh = (shutil.which("gh") or "gh").replace("\\", "/")
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_1": f"!'{gh}' auth git-credential"})
    return env


def run(args: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=git_auth_env(env) if args and args[0] == "git" else env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} failed: {(p.stderr or p.stdout).strip()[:600]}")
    return p.stdout


# ------------------------------------------------------------------ job store

_lock = threading.RLock()
_procs: dict[str, subprocess.Popen] = {}
_cancelled: set[str] = set()


def _job_file(jid: str) -> Path:
    return JOBS_DIR / jid / "job.json"


def save(job: dict) -> None:
    with _lock:
        f = _job_file(job["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
        os.replace(tmp, f)


def load(jid: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
        raise ValueError("bad job id")
    return json.loads(_job_file(jid).read_text(encoding="utf-8"))


def update(jid: str, **fields) -> dict:
    with _lock:
        job = load(jid)
        job.update(fields)
        save(job)
        return job


def list_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    out = []
    for f in JOBS_DIR.glob("*/job.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda j: j["created"], reverse=True)


def recover() -> None:
    """Jobs left active by a stopped server are marked interrupted, never silently resumed."""
    for j in list_jobs():
        if j["status"] in ACTIVE:
            update(j["id"], status="interrupted", finished=now_iso(),
                   error="server stopped while this job was active; run it again")


# ------------------------------------------------------------- candidates

def review_candidates(snap: dict) -> list[dict]:
    """Open, non-draft PRs without a clean review of the current head."""
    return [p for p in snap["prs"] if not p["draft"] and not p.get("reviewCurrent")
            and (p.get("statusRole") != "waiting-on-author"
                 or (p.get("lastReview") and not p["lastReview"]["onHead"]))
            and p["stackedOn"] is None]


def enqueue(prs: list[int], force: bool = False) -> dict:
    snap = json.loads((T.SNAPDIR / "latest.json").read_text(encoding="utf-8"))
    by_num = {p["number"]: p for p in snap["prs"]}
    existing = list_jobs()
    created, skipped = [], []
    for n in prs:
        p = by_num.get(n)
        if not p:
            skipped.append({"pr": n, "reason": "not an open PR in the latest snapshot; refresh first"})
            continue
        if p["draft"]:
            skipped.append({"pr": n, "reason": "draft"})
            continue
        dup = next((j for j in existing if j.get("type", "review") == "review" and j["pr"] == n
                    and j["head"] == p["head"] and j["base"] == p["base"]
                    and j["status"] in ACTIVE + ("done", "posted")), None)
        if dup and (not force or dup["status"] in ACTIVE):
            skipped.append({"pr": n, "reason": f"already {dup['status']} for this head (job {dup['id']})"})
            continue
        job = {"id": uuid.uuid4().hex[:12], "type": "review", "pr": n, "title": p["title"],
               "author": p["author"], "url": p["url"], "head": p["head"], "base": p["base"],
               "status": "queued", "created": now_iso(), "started": None, "finished": None,
               "error": None, "costUsd": None, "decision": None, "summary": None, "body": None,
               "posted": None, "dismissed": False}
        save(job)
        created.append(job)
    if created:
        _wake.set()
    return {"created": created, "skipped": skipped}


# ------------------------------------------------------------------ worker

_wake = threading.Event()
_worker_started = False


def start_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    recover()
    # Two lanes: a long implementation never holds up reviews and issue drafts.
    threading.Thread(target=_worker, args=(False,), name="draft-worker", daemon=True).start()
    threading.Thread(target=_worker, args=(True,), name="implement-worker", daemon=True).start()


def _worker(implement_lane: bool) -> None:
    while True:
        queued = sorted((j for j in list_jobs() if j["status"] == "queued"
                         and (j.get("type") == "implement") == implement_lane), key=lambda j: j["created"])
        if not queued:
            _wake.wait(timeout=10)
            _wake.clear()
            continue
        execute(queued[0]["id"])


_repo_lock = threading.Lock()


def ensure_repo() -> None:
    with _repo_lock:
        if not REPO_DIR.resolve().is_relative_to(WORK.resolve()):
            raise RuntimeError("The working clone is outside the repository's local workspace")
        if not (REPO_DIR / ".git").exists():
            WORK.mkdir(parents=True, exist_ok=True)
            run(["gh", "repo", "clone", T.REPO, str(REPO_DIR), "--", "--quiet"])
            run(["git", "config", "core.longpaths", "true"], cwd=REPO_DIR)
        remote = run(["git", "remote", "get-url", "origin"], cwd=REPO_DIR).strip()
        match = re.fullmatch(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([^/]+/[^/]+)",
                             remote.rstrip("/"), re.I)
        if not match or match[1].removesuffix(".git").casefold() != T.REPO.casefold():
            raise RuntimeError(f"Working clone origin does not identify {T.REPO}")
        run(["git", "worktree", "prune"], cwd=REPO_DIR)


def fetch_base(branch: str) -> str:
    ref = f"refs/remotes/origin/{branch}"
    run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{ref}"], cwd=REPO_DIR)
    return run(["git", "rev-parse", ref], cwd=REPO_DIR).strip()


def checkout_file(tree: Path, relative: str) -> Path | None:
    path = (tree / relative).resolve()
    return path if path.is_relative_to(tree.resolve()) and path.is_file() else None


def policies(tree: Path) -> str:
    return ", ".join(f"`{name}`" for name in T.WORKFLOW.policy_files if checkout_file(tree, name)) or "none configured"


def review_policies(ctx: Path, commit: str) -> str:
    """Use policy from the integration branch, not instructions added by the PR."""
    entries = []
    folder = ctx / "policy"
    folder.mkdir(exist_ok=True)
    for number, name in enumerate(T.WORKFLOW.policy_files):
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            continue
        listing = run(["git", "ls-tree", commit, "--", str(path)], cwd=REPO_DIR)
        if not listing.startswith(("100644 blob ", "100755 blob ")):
            continue
        file = folder / f"{number:02d}-{path.name}"
        file.write_text(run(["git", "show", f"{commit}:{path}"], cwd=REPO_DIR), encoding="utf-8")
        entries.append(f"`{path}` → `{file}`")
    return ", ".join(entries) or "none configured"


def prepare(job: dict) -> tuple[Path, Path, dict]:
    jdir = JOBS_DIR / job["id"]
    ctx, tree = jdir / "context", jdir / "tree"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "issues").mkdir(exist_ok=True)
    n, head, base = job["pr"], job["head"], job["base"]

    ensure_repo()
    run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}",
         f"+refs/pull/{n}/head:refs/remotes/pr/{n}"], cwd=REPO_DIR)
    actual = run(["git", "rev-parse", f"refs/remotes/pr/{n}"], cwd=REPO_DIR).strip()
    if actual != head:
        raise RuntimeError(f"PR head moved to {actual[:9]} since the snapshot ({head[:9]}); refresh and rerun")
    main_sha = run(["git", "rev-parse", f"origin/{base}"], cwd=REPO_DIR).strip()
    if tree.exists():
        run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO_DIR, check=False)
    run(["git", "worktree", "add", "--detach", "--quiet", str(tree), head], cwd=REPO_DIR)

    (ctx / "diff.patch").write_text(run(["git", "diff", f"origin/{base}...{head}"], cwd=REPO_DIR), encoding="utf-8")
    log = run(["git", "log", "--format=%H %p%n  %an: %s", f"origin/{base}..{head}"], cwd=REPO_DIR)
    log = re.sub(r"^(\w{40}) (\w{40}) (\w{40})$", r"\1  [MERGE COMMIT]", log, flags=re.M)
    (ctx / "commits.txt").write_text(log, encoding="utf-8")
    behind = int(run(["git", "rev-list", "--count", f"{head}..origin/{base}"], cwd=REPO_DIR).strip() or 0)

    pr = T.gh_json(["pr", "view", str(n), "-R", T.REPO, "--json",
                    "number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,labels,"
                    "closingIssuesReferences,files,comments,mergeable,mergeStateStatus,statusCheckRollup"])
    checks = pr.pop("statusCheckRollup", [])
    (ctx / "pr.json").write_text(json.dumps(pr, indent=1), encoding="utf-8")
    (ctx / "checks.txt").write_text("\n".join(
        f"{c.get('name') or c.get('context')}: {c.get('conclusion') or c.get('state') or c.get('status')}"
        for c in checks) or "No checks reported on this head.", encoding="utf-8")
    rules = T.read_rules(base)
    (ctx / "rules.txt").write_text(json.dumps(rules, indent=1), encoding="utf-8")

    for ref in pr.get("closingIssuesReferences", []):
        iss = T.gh_json(["issue", "view", str(ref["number"]), "-R", T.REPO, "--json",
                         "number,title,body,labels,comments"])
        md = [f"# #{iss['number']} {iss['title']}", "", "Labels: " + ", ".join(l["name"] for l in iss["labels"]),
              "", iss["body"] or "", ""]
        for c in iss["comments"]:
            md += [f"---\n**{c['author']['login']}** ({c['createdAt']}):", c["body"], ""]
        (ctx / "issues" / f"issue-{iss['number']}.md").write_text("\n".join(md), encoding="utf-8")

    reviews = T.gh_json(["api", f"repos/{T.REPO}/pulls/{n}/reviews", "--paginate"])
    md = []
    for r in reviews:
        if (r.get("user") or {}).get("login", "").casefold() in T.MAINTAINERS and (r["body"] or "").strip():
            md += [f"## {r['user']['login']} · {r['state']} · commit {r['commit_id'][:9]} · {r['submitted_at']}",
                   "", r["body"], ""]
    (ctx / "prior-reviews.md").write_text("\n".join(md) or "No prior maintainer reviews.", encoding="utf-8")

    policy_branch = T.base_branch()
    policy_commit = main_sha if not policy_branch or policy_branch == base else fetch_base(policy_branch)
    facts = {"main": main_sha, "behind": behind, "policy_files": review_policies(ctx, policy_commit)}
    return tree, ctx, facts


def build_prompt(job: dict, ctx: Path, facts: dict) -> str:
    behind = facts["behind"]
    return PROMPT.read_text(encoding="utf-8").format(
        number=job["pr"], repo=T.REPO, title=job["title"], author=job["author"],
        head=job["head"], base=job["base"], main=facts["main"], ctx=str(ctx), policy_files=facts["policy_files"],
        behind_text=f"{behind} commit(s) behind origin/{job['base']}" if behind else f"up to date with origin/{job['base']}")


OUT_RE = re.compile(r"===REVIEW===\s*(.*?)\s*===END===\s*DECISION:\s*(COMMENT|REQUEST_CHANGES)\s*(?:SUMMARY:\s*(.*))?", re.S)


def parse(text: str) -> tuple[str, str, str]:
    m = OUT_RE.search(text or "")
    if not m:
        raise RuntimeError("agent output did not follow the review format; see the log")
    return m.group(1).strip(), m.group(2), (m.group(3) or "").strip().splitlines()[0] if m.group(3) else ""


# ------------------------------------------------------------ issue tasks

ISSUE_TASKS = {"triage": {"triage"}, "scope": {"scope"}, "design": {"design-needed", "ready", "design-changes"},
               "implement": {"approved", "implementing"}}


def enqueue_issue(task: str, numbers: list[int], force: bool = False, digest: str | None = None,
                  scheduled: bool = False) -> dict:
    if task not in ISSUE_TASKS:
        raise ValueError(f"unknown issue task {task!r}")
    if task in ("design", "implement") and not T.PROJECT.design_enabled():
        raise ValueError("Configure maintainers and labels.design_approved to enable the design workflow")
    if task == "implement" and (not digest or len(numbers) != 1):
        raise ValueError("implement starts from the design viewer, one issue at a time, pinned to the design you read")
    snap = json.loads((T.SNAPDIR / "latest.json").read_text(encoding="utf-8"))
    by_num = {r["number"]: r for r in snap["issues"]["rows"]}
    existing = list_jobs()
    created, skipped = [], []
    for n in numbers:
        r = by_num.get(n)
        if not r:
            skipped.append({"pr": n, "reason": "not an open issue in the latest snapshot; refresh first"})
            continue
        if r["excluded"]:
            skipped.append({"pr": n, "reason": "excluded by the configured title pattern"})
            continue
        if task == "design" and r["assignees"] and not ours(r["assignees"]):
            skipped.append({"pr": n, "reason": f"claimed by {', '.join(r['assignees'])}; their design to write"})
            continue
        if task == "implement":
            why = impl_block(r, existing)
            if why:
                skipped.append({"pr": n, "reason": why})
                continue
        if scheduled and task == "triage":
            from scheduled_triage import TERMINAL_STAGES
            if r["stage"] in TERMINAL_STAGES:
                skipped.append({"pr": n, "reason": f"stage {r['stage']} is outside scheduled triage"})
                continue
        elif r["stage"] not in ISSUE_TASKS[task]:
            skipped.append({"pr": n, "reason": f"stage is {r['stage']}; {task} applies to {', '.join(sorted(ISSUE_TASKS[task]))}"})
            continue
        dup = next((j for j in existing if j.get("type") == task and j["pr"] == n
                    and j["status"] in ACTIVE + ("done",)), None)
        if dup and (not force or dup["status"] in ACTIVE):
            skipped.append({"pr": n, "reason": f"{task} already {dup['status']} (job {dup['id']})"})
            continue
        job = {"id": uuid.uuid4().hex[:12], "type": task, "target": "issue", "pr": n, "title": r["title"],
               "author": r["author"], "url": r["url"], "head": "", "base": T.base_branch(),
               "status": "queued", "created": now_iso(), "started": None, "finished": None,
               "error": None, "costUsd": None, "decision": None, "summary": None, "body": None,
               "posted": None, "dismissed": False}
        if task == "implement":
            job["designDigest"] = digest
        save(job)
        created.append(job)
        if task == "implement":
            existing.append(job)  # one implementation at a time, even within one request
    if created:
        _wake.set()
    return {"created": created, "skipped": skipped}


def impl_block(r: dict, existing: list[dict]) -> str | None:
    """Why this issue cannot be implemented now, or None."""
    if not r.get("designApproved"):
        return "design not approved"
    if r["assignees"] and not ours(r["assignees"]):
        return f"claimed by {', '.join(r['assignees'])}; theirs to implement"
    if r["prs"]:
        return f"PR {', '.join('#' + str(x) for x in r['prs'])} already closes it"
    busy = next((j for j in existing if j.get("type") == "implement"
                 and (j["status"] in ACTIVE or (j["status"] == "done" and not j.get("dismissed")))), None)
    if busy:
        return (f"one implementation at a time: #{busy['pr']} is {busy['status']}"
                + (" (publish or discard it first)" if busy["status"] == "done" else ""))
    return None


def ours(assignees: list[str]) -> bool:
    """Claimed by you or by the implementation bot (logins compare case-insensitively)."""
    me = {viewer().lower(), (impl_config().get("bot_login") or "").lower()} - {""}
    return any(a.lower() in me for a in assignees)


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40].rstrip("-") or "change"


def _free_branch(branch: str, jid: str) -> None:
    """Release a local branch left by an earlier run so a retry can recreate it.

    git refuses to delete a branch checked out in another worktree, so the leftover worktree goes first.
    Refuses when that worktree belongs to a live job, or when the branch holds commits no remote has."""
    if run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=REPO_DIR, check=False) == "":
        return  # no such branch
    unpushed = run(["git", "rev-list", "--count", f"refs/heads/{branch}", "--not", "--remotes"], cwd=REPO_DIR).strip()
    trees, cur = [], None
    for line in run(["git", "worktree", "list", "--porcelain"], cwd=REPO_DIR).splitlines():
        if line.startswith("worktree "):
            cur = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and cur is not None:
            trees.append(cur)
    for t in trees:
        owner = t.parent.name if t.parent.parent.resolve() == JOBS_DIR.resolve() else None
        if owner == jid:
            continue
        prev = load(owner) if owner and (JOBS_DIR / owner / "job.json").exists() else None
        if prev and (prev["status"] in ACTIVE or (prev["status"] == "done" and not prev.get("dismissed"))):
            raise RuntimeError(f"{branch} belongs to job {owner}, which is {prev['status']}; "
                               "publish or discard it first")
        if not prev:
            raise RuntimeError(f"{branch} is checked out at {t}, which no job owns; remove it by hand")
        if unpushed not in ("", "0"):
            raise RuntimeError(f"{branch} has {unpushed} commit(s) on no remote (job {owner}); "
                               "push or delete them by hand before retrying")
        run(["git", "worktree", "remove", "--force", str(t)], cwd=REPO_DIR, check=False)
        shutil.rmtree(t, ignore_errors=True)
    run(["git", "worktree", "prune"], cwd=REPO_DIR)
    if unpushed not in ("", "0") and not trees:
        raise RuntimeError(f"{branch} has {unpushed} commit(s) on no remote; push or delete them by hand before retrying")
    run(["git", "branch", "-D", branch], cwd=REPO_DIR)


def prepare_implement(job: dict) -> tuple[Path, Path, dict]:
    """Worktree from the target base branch, with the approved design and issue context."""
    jdir = JOBS_DIR / job["id"]
    ctx, tree = jdir / "context", jdir / "tree"
    ctx.mkdir(parents=True, exist_ok=True)
    ensure_repo()
    base_branch = T.base_branch()
    if not base_branch or base_branch != job["base"]:
        raise ValueError("Base branch changed or has no commits; refresh before implementing")
    main_sha = fetch_base(base_branch)
    branch = f"impl/{job['pr']}-{_slug(job['title'])}"
    prefix = T.WORKFLOW.fixture_branch_prefix
    fx = []
    if prefix:
        fx = [line.split("\t")[1] for line in
              run(["git", "ls-remote", "origin", f"refs/heads/{prefix}{job['pr']}-*"],
                  cwd=REPO_DIR).splitlines() if "\t" in line]
    if len(fx) > 1:
        raise RuntimeError(f"several fixture branches for #{job['pr']}: {', '.join(fx)}; keep one")
    base, fixtures = main_sha, None
    if fx:
        run(["git", "fetch", "--quiet", "origin", f"+{fx[0]}:refs/remotes/origin/{fx[0][len('refs/heads/'):]}"], cwd=REPO_DIR)
        base = run(["git", "rev-parse", f"refs/remotes/origin/{fx[0][len('refs/heads/'):]}"], cwd=REPO_DIR).strip()
        behind = run(["git", "rev-list", "--count", f"{base}..{main_sha}"], cwd=REPO_DIR).strip()
        own = run(["git", "log", "--format=%h %s", f"{main_sha}..{base}"], cwd=REPO_DIR).strip()
        files = run(["git", "diff", "--name-only", f"{main_sha}...{base}"], cwd=REPO_DIR).strip()
        fixtures = {"branch": fx[0][len("refs/heads/"):], "sha": base, "commits": own, "files": files,
                    "behindMain": int(behind or 0)}
    if tree.exists():
        run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO_DIR, check=False)
    _free_branch(branch, job["id"])
    run(["git", "worktree", "add", "--quiet", "-b", branch, str(tree), base], cwd=REPO_DIR)
    if fixtures:
        (ctx / "fixtures.md").write_text(
            f"Fixtures for this issue are committed on `{fixtures['branch']}` "
            f"(`{base[:9]}`). Your branch starts from that commit, so they are already in the tree:\n\n"
            f"{fixtures['files']}\n\nCommit(s) on top of `{base_branch}` `{main_sha[:9]}`:\n{fixtures['commits']}\n",
            encoding="utf-8")

    iss = T.gh_json(["issue", "view", str(job["pr"]), "-R", T.REPO, "--json",
                     "number,title,body,labels,comments,author,createdAt,assignees,milestone,state"])
    if iss["state"] != "OPEN":
        raise RuntimeError(f"issue is {iss['state'].lower()}")
    labels = [l["name"] for l in iss["labels"]]
    if not T.LABELS.has(labels, "design_approved"):
        raise RuntimeError("design is not approved")
    designs = design_comments(iss["comments"], job["pr"])
    if not designs:
        raise RuntimeError("no maintainer-posted design proposal on the issue")
    md = [f"# #{iss['number']} {iss['title']}", "",
          f"Opened by {iss['author']['login']} on {iss['createdAt']}. Labels: {', '.join(labels)}. "
          f"Assignees: {', '.join(a['login'] for a in iss['assignees']) or 'none'}.", "", iss["body"] or "", ""]
    for c in iss["comments"]:
        md += [f"---\n**{c['author']['login']}** ({c['createdAt']}):", "", c["body"], ""]
    (ctx / "issue.md").write_text("\n".join(md), encoding="utf-8")
    d = designs[-1]
    if job.get("designDigest") and _design_digest(d["body"]) != job["designDigest"]:
        raise RuntimeError("the design changed since you read it (edited, or a newer proposal was posted); review it again")
    (ctx / "design.md").write_text(f"Approved design, posted by {d['author']['login']} on {d['createdAt']}:\n"
                                   f"{d.get('url', '')}\n\n{d['body']}", encoding="utf-8")
    templates = (T.WORKFLOW.pr_template,) if T.WORKFLOW.pr_template else (
        ".github/pull_request_template.md", "pull_request_template.md", "docs/pull_request_template.md")
    template = next((file for entry in templates if (file := checkout_file(tree, entry))), None)
    if template:
        shutil.copyfile(template, ctx / "pr-template.md")
    elif T.WORKFLOW.pr_template:
        raise RuntimeError(f"Configured PR template is missing: {T.WORKFLOW.pr_template}")
    status = T.LABELS.current_status(labels)
    return tree, ctx, {"main": main_sha, "startHead": base, "branch": branch,
                       "labelsAtStart": labels, "statusAtStart": status,
                       "designUrl": d.get("url"), "fixtures": fixtures, "policy_files": policies(tree)}


def _impl_env(cfg: dict | None = None) -> dict:
    env = dict(os.environ)
    if cfg is None:
        cfg = impl_config()
    if cfg.get("git_name") and cfg.get("git_email"):
        for k in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{k}_NAME"], env[f"GIT_{k}_EMAIL"] = cfg["git_name"], cfg["git_email"]
    return env


PR_OUT_RE = re.compile(r"===PR===\s*(.*?)\s*===END===(.*)", re.S)


def parse_impl(text: str) -> dict:
    m = PR_OUT_RE.search(text or "")
    if not m:  # keep the commits: the PR text can be written by hand
        return {"body": "", "prTitle": "", "summary": "agent output did not follow the format; write the PR text"}
    fields = dict(re.findall(r"^([A-Z_]+):[ \t]*(.*)$", m.group(2), re.M))
    return {"body": m.group(1).strip(), "prTitle": (fields.get("TITLE") or "").strip(),
            "summary": (fields.get("SUMMARY") or "").strip()}


def inspect_branch(tree: Path, main_sha: str) -> dict:
    """What the agent actually produced, read from git, not from its report."""
    commits = [dict(zip(("sha", "subject", "parents"), l.split("\x1f")))
               for l in run(["git", "log", "--reverse", "--format=%H%x1f%s%x1f%P", f"{main_sha}..HEAD"],
                            cwd=tree).splitlines() if l]
    return {"commits": [{"sha": c["sha"], "subject": c["subject"]} for c in commits],
            "mergeCommits": sum(1 for c in commits if len(c["parents"].split()) > 1),
            "uncommitted": run(["git", "status", "--porcelain"], cwd=tree).strip().splitlines(),
            "diffStat": run(["git", "diff", "--stat", f"{main_sha}..HEAD"], cwd=tree).strip(),
            "head": run(["git", "rev-parse", "HEAD"], cwd=tree).strip()}


def run_gate(jid: str, tree: Path) -> list[dict]:
    """Run the local gate ourselves: the agent's claims are not evidence."""
    cfg = impl_config()
    deadline = time.monotonic() + cfg["gate_timeout_min"] * 60
    results = []
    with open(JOBS_DIR / jid / "gate.log", "w", encoding="utf-8") as log:
        for cmd in T.WORKFLOW.gate_commands:
            if jid in _cancelled:
                raise RuntimeError("cancelled")
            left = deadline - time.monotonic()
            if left <= 0:
                results.append({"cmd": " ".join(cmd), "ok": False, "tail": "not run: gate timeout reached"})
                continue
            log.write(f"$ {' '.join(cmd)}\n"); log.flush()
            started = time.monotonic()
            try:
                proc = subprocess.Popen(cmd, cwd=tree, env=_impl_env(), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except OSError as exc:
                out, ok = str(exc), False
            else:
                _procs[jid] = proc
                try:
                    out, _ = proc.communicate(timeout=left)
                    ok = proc.returncode == 0
                except subprocess.TimeoutExpired:
                    kill(proc)
                    out, ok = "timed out", False
                finally:
                    _procs.pop(jid, None)
            log.write(out + "\n"); log.flush()
            tail = "\n".join(out.strip().splitlines()[-12:])
            results.append({"cmd": " ".join(cmd), "ok": ok, "sec": round(time.monotonic() - started),
                            "tail": tail})
            update(jid, gate=results)
    return results


def remove_impl_tree(job: dict) -> None:
    tree = JOBS_DIR / job["id"] / "tree"
    if tree.exists():
        run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO_DIR, check=False)
        shutil.rmtree(tree, ignore_errors=True)
    branch = (job.get("facts") or {}).get("branch")
    if branch:
        run(["git", "branch", "-D", branch], cwd=REPO_DIR, check=False)


def _open_prs_closing(number: int) -> list[int]:
    prs = T.gh_json(["pr", "list", "-R", T.REPO, "--state", "open", "--limit", "200",
                     "--json", "number,closingIssuesReferences"])
    return [p["number"] for p in prs if any(c["number"] == number for c in p["closingIssuesReferences"])]


def publish_impl(jid: str, title: str, body: str, opts: dict) -> dict:
    """Explicit user action only: push the branch and open a PR, then claim the issue."""
    job = load(jid)
    if job.get("type") != "implement" or job["status"] != "done":
        raise ValueError(f"job is {job['status']}; only a finished implementation can be published")
    facts = job.get("facts") or {}
    tree = JOBS_DIR / jid / "tree"
    if not tree.exists():
        raise ValueError("the working tree is gone; run the implementation again")
    title, body = (title or "").strip(), (body or "").strip()
    if not title or not body:
        raise ValueError("title and body are required")
    if not re.search(rf"\b(Closes|Part of) #{job['pr']}\b", body):
        raise ValueError(f"the body must say 'Closes #{job['pr']}' (or 'Part of #{job['pr']}' for a partial PR)")
    found = inspect_branch(tree, facts["main"])
    if found["head"] != job.get("implHead"):
        raise ValueError("the branch changed since the gate ran; run it again")
    if not found["commits"] or found["mergeCommits"] or found["uncommitted"]:
        raise ValueError("branch must have commits, no merge commits and no uncommitted changes")
    failed = [g["cmd"] for g in job.get("gate") or [] if not g["ok"]]
    if failed and not opts.get("allowFailing"):
        raise ValueError("local gate failed: " + "; ".join(failed))
    draft = bool(opts.get("draft", True)) or bool(failed)  # a failing gate can only go up as a draft
    n = str(job["pr"])
    live = T.gh_json(["issue", "view", n, "-R", T.REPO, "--json", "state,labels,assignees"])
    labels = [l["name"] for l in live["labels"]]
    assignees = [a["login"] for a in live["assignees"]]
    if live["state"] != "OPEN":
        raise ValueError(f"issue is {live['state'].lower()}")
    if not T.LABELS.has(labels, "design_approved"):
        raise ValueError("The configured design approval label was removed from the issue")
    if job["base"] != T.base_branch():
        raise ValueError("Base branch changed since implementation; start a fresh run")
    if assignees and not ours(assignees):
        raise ValueError(f"issue is now claimed by {', '.join(assignees)}")
    other = _open_prs_closing(job["pr"])
    if other:
        raise ValueError(f"PR {', '.join('#' + str(x) for x in other)} now closes this issue")

    cfg, env = impl_config(), dict(os.environ)
    remote = "origin"
    if cfg.get("gh_token_env"):
        token = os.environ.get(cfg["gh_token_env"])
        if not token:
            raise ValueError(f"config names {cfg['gh_token_env']} for the bot token, but it is not set")
        env["GH_TOKEN"] = token
        remote = f"https://x-access-token:{token}@github.com/{T.REPO}.git"
    branch = facts["branch"]
    p = subprocess.run(["git", "push", remote, f"HEAD:refs/heads/{branch}"], cwd=tree, env=git_auth_env(env),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout)[-400:]
        if env.get("GH_TOKEN"):
            msg = msg.replace(env["GH_TOKEN"], "***")
        raise RuntimeError("push failed: " + msg)
    f = JOBS_DIR / jid / "pr-body.md"
    f.write_text(body, encoding="utf-8")
    args = ["gh", "pr", "create", "-R", T.REPO, "--base", job["base"], "--head", branch, "--title", title,
            "--body-file", str(f)] + (["--draft"] if draft else [])
    p = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"branch {branch} was pushed, but opening the PR failed: {(p.stderr or p.stdout)[-400:]}")
    url = p.stdout.strip().splitlines()[-1]
    actions = [f"pushed {branch} @{found['head'][:9]}", "opened " + ("draft PR" if draft else "PR")]
    if review_status := T.LABELS.status("waiting-on-review"):
        e = subprocess.run(["gh", "pr", "edit", url, "-R", T.REPO, "--add-label", review_status], env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        actions.append(f"PR → {review_status}" if e.returncode == 0
                       else "labelling the PR FAILED: " + (e.stderr or e.stdout)[-200:])
    edit = []
    me = viewer()  # the maintainer running the dashboard; "@me" would be the bot, since env carries its token
    if me.lower() not in (a.lower() for a in assignees):
        edit += ["--add-assignee", me]
    bot = impl_config().get("bot_login") or ""
    if bot and bot.lower() in (a.lower() for a in assignees):
        edit += ["--remove-assignee", bot]
    edit += _special_add(labels, "claimed")
    edit += _status_swap(labels, "in-progress")
    if edit:
        e = subprocess.run(["gh", "issue", "edit", n, "-R", T.REPO, *edit], env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        actions.append("issue updated" if e.returncode == 0
                       else "claiming the issue FAILED: " + (e.stderr or e.stdout)[-200:])
    job = update(jid, status="posted", prTitle=title, body=body,
                 posted={"at": now_iso(), "url": url, "by": viewer(), "actions": actions, "draft": draft})
    remove_impl_tree(job)
    return job


BLOCKER_ROLES = ("needs-information", "waiting-on-dependency", "design-review", "needs-scope-review")


def post_blocker(jid: str, body: str, opts: dict) -> dict:
    """Explicit user action only: post why an implementation stopped, optionally moving the issue's status."""
    job = load(jid)
    if job.get("type") != "implement" or job["status"] != "stopped":
        raise ValueError(f"job is {job['status']}; only a stopped implementation has a blocker to post")
    body = (body or "").strip()
    if not body:
        raise ValueError("the comment is empty")
    n = str(job["pr"])
    live = T.gh_json(["issue", "view", n, "-R", T.REPO, "--json", "state,labels"])
    if live["state"] != "OPEN":
        raise ValueError(f"issue is {live['state'].lower()}")
    labels = [l["name"] for l in live["labels"]]
    status = T.LABELS.current_status(labels)
    start = (job.get("facts") or {}).get("statusAtStart")
    if status != start:
        raise ValueError(f"issue status changed from {start} to {status} since this run; read the issue again")
    new = opts.get("status")
    role = T.LABELS.role(new)
    if new and role not in BLOCKER_ROLES:
        raise ValueError(f"unexpected status {new}")
    f = JOBS_DIR / jid / "blocker.md"
    f.write_text(body, encoding="utf-8")
    url = T.gh(["issue", "comment", n, "-R", T.REPO, "--body-file", str(f)]).strip()
    actions = ["comment"]
    if role:
        edit = _status_swap(labels, role)
        if role == "design-review" and T.LABELS.has(labels, "design_approved"):
            edit += ["--remove-label", T.LABELS.special["design_approved"]]
        if edit:
            T.gh(["issue", "edit", n, "-R", T.REPO, *edit])
            actions.append(f"status → {new}")
    return update(jid, status="posted", body=body, posted={"at": now_iso(), "url": url or job["url"],
                                                           "by": viewer(), "actions": actions})


def prepare_issue(job: dict) -> tuple[Path, Path, dict]:
    jdir = JOBS_DIR / job["id"]
    ctx, tree = jdir / "context", jdir / "tree"
    ctx.mkdir(parents=True, exist_ok=True)
    main_sha = ""
    if T.base_branch():
        ensure_repo()
        main_sha = fetch_base(T.base_branch())
        if tree.exists():
            run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO_DIR, check=False)
        run(["git", "worktree", "add", "--detach", "--quiet", str(tree), main_sha], cwd=REPO_DIR)
    else:
        run(["git", "init", "--quiet", str(tree)])

    iss = T.gh_json(["issue", "view", str(job["pr"]), "-R", T.REPO, "--json",
                     "number,title,body,labels,comments,author,createdAt,assignees,milestone,state"])
    if iss["state"] != "OPEN":
        raise RuntimeError("Issue closed before drafting")
    if iss["state"] != "OPEN":
        raise RuntimeError(f"issue is {iss['state'].lower()}")
    labels = [l["name"] for l in iss["labels"]]
    md = [f"# #{iss['number']} {iss['title']}", "",
          f"Opened by {iss['author']['login']} on {iss['createdAt']}. Labels: {', '.join(labels) or 'none'}. "
          f"Assignees: {', '.join(a['login'] for a in iss['assignees']) or 'none'}. "
          f"Milestone: {(iss.get('milestone') or {}).get('title', 'none')}.", "", iss["body"] or "", ""]
    for c in iss["comments"]:
        md += [f"---\n**{c['author']['login']}** ({c['createdAt']}):", "", c["body"], ""]
    (ctx / "issue.md").write_text("\n".join(md), encoding="utf-8")
    latest = T.SNAPDIR / "latest.json"
    snap = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {}
    linked = [p for p in snap.get("prs", []) if job["pr"] in p["closes"]]
    row = next((r for r in snap.get("issues", {}).get("rows", []) if r["number"] == job["pr"]), {})
    (ctx / "linked-work.json").write_text(json.dumps({"pullRequests": linked, "references": row.get("refs", [])}, indent=2),
                                          encoding="utf-8")
    label_pages = T.gh_json(["api", f"repos/{T.REPO}/labels", "--paginate", "--slurp"]) or []
    repo_labels = [item for page in label_pages for item in page]
    (ctx / "labels.txt").write_text("\n".join(f"{l['name']}: {l['description'] or ''}" for l in repo_labels),
                                    encoding="utf-8")
    status = T.LABELS.current_status(labels)
    return tree, ctx, {"main": main_sha, "labelsAtStart": labels, "statusAtStart": status,
                       "repoLabels": [l["name"] for l in repo_labels], "policy_files": policies(tree)}


def build_issue_prompt(job: dict, ctx: Path, facts: dict, agent: str) -> str:
    commit_instructions = (
        "Leave your changes uncommitted. Return the PR body, title, and summary below. "
        "The dashboard will create a local commit using your title and summary."
        if agent == "codex" else
        "Commit with `git add` and `git commit` and leave the worktree clean. "
        "Each commit should build. The body says what and why. "
        "End each commit message with `Co-Authored-By: Claude <noreply@anthropic.com>`."
    )
    return (HERE / "prompts" / f"{job['type']}.md").read_text(encoding="utf-8").format(
        number=job["pr"], repo=T.REPO, title=job["title"], main=facts["main"], ctx=str(ctx),
        base=job["base"] or "no branch yet", policy_files=facts["policy_files"],
        labels=", ".join(facts["labelsAtStart"]) or "none", today=dt.date.today().isoformat(),
        status_mapping=", ".join(f"`{role}` → `{label}`" for role, label in T.LABELS.statuses.items()) or "none configured",
        protected_labels=", ".join(T.LABELS.special.values()) or "none configured",
        gate_commands="\n".join(f"- `{shlex.join(cmd)}`" for cmd in T.WORKFLOW.gate_commands) or "None configured.",
        commit_instructions=commit_instructions)


ISSUE_OUT_RE = re.compile(r"===COMMENT===\s*(.*?)\s*===END===(.*)", re.S)


def parse_issue(text: str) -> dict:
    m = ISSUE_OUT_RE.search(text or "")
    if not m:
        raise RuntimeError("agent output did not follow the format; see the log")
    fields = dict(re.findall(r"^([A-Z_]+):\s*(.*)$", m.group(2), re.M))
    split = lambda v: [x.strip() for x in (v or "").split(",") if x.strip() and x.strip().lower() != "none"]
    return {"body": m.group(1).strip(), "summary": fields.get("SUMMARY", "").strip(),
            "labelsAdd": split(fields.get("LABELS_ADD")), "labelsRemove": split(fields.get("LABELS_REMOVE")),
            "recommendStatus": (fields.get("RECOMMEND_STATUS") or "").strip() or None}


def agent_command(executable: str, cfg: dict, ctx: Path, jdir: Path, is_impl: bool, tree: Path) -> list[str]:
    if cfg["agent"] == "claude":
        allowed = [*IMPL_ALLOWED, *(f"Bash({shlex.join(cmd)})" for cmd in T.WORKFLOW.gate_commands)] if is_impl else ALLOWED_TOOLS
        args = [executable, "-p", "--output-format", "json", "--no-session-persistence",
                "--permission-mode", "dontAsk", "--strict-mcp-config",
                "--add-dir", str(ctx),
                "--allowedTools", *allowed,
                "--disallowedTools", *(IMPL_DENIED if is_impl else DENIED_TOOLS),
                "--max-budget-usd", str(cfg["max_budget_usd"])]
        if not is_impl:
            args += ["--safe-mode", "--tools", "Read,Grep,Glob,Bash"]
        if cfg.get("effort"):
            args += ["--effort", cfg["effort"]]
    else:
        args = [executable, "-a", "never", "exec", "--ignore-user-config", "--ephemeral",
                "--disable", "hooks", "-c", 'web_search="disabled"',
                "-c", "sandbox_workspace_write.network_access=false",
                "--sandbox", "workspace-write" if is_impl else "read-only",
                "--output-last-message", str(jdir / "agent-output.txt")]
        if not is_impl:
            args += ["-c", "project_doc_max_bytes=0",
                     "-c", f'projects.{json.dumps(str(tree.resolve()))}.trust_level="untrusted"']
        if os.name == "nt":
            args += ["--enable", "prefer_mxc"]
        if cfg.get("effort"):
            args += ["-c", "model_reasoning_effort=" + json.dumps(cfg["effort"])]
    if cfg.get("model"):
        args += ["--model", cfg["model"]]
    if cfg["agent"] == "codex":
        args.append("-")  # read the prompt from stdin
    return args


def agent_environment(cfg: dict, is_impl: bool) -> dict:
    env = _impl_env(cfg) if is_impl else dict(os.environ)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", cfg.get("gh_token_env")):
        if name:
            env.pop(name, None)
    if cfg["agent"] == "codex":
        # Run a standalone CLI even when the dashboard was launched from a Codex tool shell.
        for name in tuple(env):
            if name.startswith(("CODEX_APP_", "CODEX_TASK_")) or name in (
                "CODEX_PERMISSION_PROFILE", "CODEX_SHELL", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "CODEX_TOOL_CALL_ID", "CODEX_TECTONIC_PATH", "CODEX_WINDOWS_REGISTERED_CORE",
                "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY",
            ):
                env.pop(name)
    return env


def agent_result(agent: str, stdout: str, returncode: int, jdir: Path) -> dict:
    if agent == "claude":
        (jdir / "agent.json").write_text(stdout, encoding="utf-8")
        try:
            result = json.loads(stdout)
            if isinstance(result, dict):
                return result
        except ValueError:
            pass
        raise RuntimeError(f"Claude exited {returncode} without a JSON result; see agent.log")

    final = jdir / "agent-output.txt"
    result = {"result": final.read_text(encoding="utf-8") if final.is_file() else "",
              "is_error": returncode != 0}
    (jdir / "agent.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    if not result["result"].strip() and not returncode:
        raise RuntimeError("Codex returned no final message; see agent.log")
    return result


def commit_codex_changes(tree: Path, draft: dict, env: dict) -> None:
    if not draft["prTitle"] or not draft["body"]:
        raise RuntimeError("Codex left changes without a PR title and body; see agent.json")
    run(["git", "add", "--all", "--", "."], cwd=tree, env=env)
    message = ["-m", draft["summary"]] if draft["summary"] else []
    run(["git", "commit", "-m", draft["prTitle"], *message], cwd=tree, env=env)


def execute(jid: str) -> None:
    job = update(jid, status="preparing", started=now_iso())
    jdir = JOBS_DIR / jid
    tree = None
    is_review = job.get("type", "review") == "review"
    is_impl = job.get("type") == "implement"
    keep_tree = False
    try:
        cfg = config(is_impl)
        agent = cfg["agent"]
        if agent not in ("claude", "codex"):
            raise ValueError('Set "agent" in config.json to "codex" or "claude"')
        update(jid, agent=agent)
        executable = shutil.which(agent)
        if not executable:
            raise RuntimeError(f"{agent} CLI was not found on PATH")
        tree, ctx, facts = prepare(job) if is_review else prepare_implement(job) if is_impl else prepare_issue(job)
        if not is_review:
            job = update(jid, head=facts["main"])
        if is_impl:
            update(jid, facts=facts)  # the branch name is needed to clean up even if the run fails
        if jid in _cancelled:
            raise RuntimeError("cancelled")
        prompt = build_prompt(job, ctx, facts) if is_review else build_issue_prompt(job, ctx, facts, agent)
        (jdir / "prompt.md").write_text(prompt, encoding="utf-8")
        args = agent_command(executable, cfg, ctx, jdir, is_impl, tree)
        env = agent_environment(cfg, is_impl)
        update(jid, status="running", facts=facts)
        with open(jdir / "agent.log", "w", encoding="utf-8") as errlog:
            proc = subprocess.Popen(args, cwd=tree, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=errlog, text=True, encoding="utf-8", errors="replace",
                                    env=env)
            _procs[jid] = proc
            try:
                out, _ = proc.communicate(prompt, timeout=cfg["timeout_min"] * 60)
            except subprocess.TimeoutExpired:
                kill(proc)
                raise RuntimeError(f"timed out after {cfg['timeout_min']} min")
            finally:
                _procs.pop(jid, None)
        if jid in _cancelled:
            raise RuntimeError("cancelled")
        res = agent_result(agent, out, proc.returncode, jdir)
        cost = res.get("total_cost_usd")
        if res.get("is_error") or proc.returncode != 0:
            update(jid, costUsd=cost)
            detail = res.get("result") or res.get("subtype") or "see agent.log"
            raise RuntimeError(f"{agent} error (exit {proc.returncode}): {str(detail)[:400]}")
        if is_impl:
            out = parse_impl(res.get("result", ""))
            found = inspect_branch(tree, facts["main"])
            if agent == "codex" and found["uncommitted"]:
                commit_codex_changes(tree, out, env)
                found = inspect_branch(tree, facts["main"])
            keep_tree = True  # kept for inspection and publishing; removed by Discard or Publish
            update(jid, costUsd=cost, turns=res.get("num_turns"), branch=found, **out)
            if found["head"] == facts["startHead"]:
                # A deliberate stop (the design cannot be built as written) is a result, not a crash:
                # keep the agent's reasoning so the maintainer can read it and post the blocker.
                text = res.get("result", "") or ""
                explanation = text.split("===PR===")[0].strip() or out["summary"] or "The agent made no commits and gave no reason."
                draft = (f"Implementation stopped at `{job['base']}` `{facts['main'][:7]}`.\n\n"
                         + (out["body"] or explanation))
                update(jid, status="stopped", finished=now_iso(), explanation=explanation, body=draft,
                       summary=out["summary"] or "stopped without commits")
                return
            (jdir / "diff.patch").write_text(run(["git", "diff", f"{facts['main']}..HEAD"], cwd=tree), encoding="utf-8")
            update(jid, status="gating")
            gate = run_gate(jid, tree)
            update(jid, status="done", finished=now_iso(), gate=gate, implHead=found["head"],
                   gateOk=all(g["ok"] for g in gate) and not found["uncommitted"] and not found["mergeCommits"])
        elif is_review:
            body, decision, summary = parse(res.get("result", ""))
            update(jid, status="done", finished=now_iso(), costUsd=cost, body=body,
                   decision=decision, summary=summary, turns=res.get("num_turns"))
        else:
            out = parse_issue(res.get("result", ""))
            if job["type"] == "triage":  # never let the agent claim, track or close
                protected = {name.casefold() for name in T.LABELS.special.values()}
                out["labelsAdd"] = [l for l in out["labelsAdd"]
                                    if l in facts["repoLabels"] and l.casefold() not in protected]
            update(jid, status="done", finished=now_iso(), costUsd=cost, turns=res.get("num_turns"), **out)
    except Exception as e:  # recorded on the job, never swallowed
        status = "cancelled" if jid in _cancelled else "failed"
        update(jid, status=status, finished=now_iso(), error=str(e)[:800])
    finally:
        if is_impl and jid in _cancelled:
            keep_tree = False
        _cancelled.discard(jid)
        if keep_tree:
            pass
        elif is_impl:
            remove_impl_tree(load(jid))
        elif tree is not None and tree.exists():
            if REPO_DIR.exists():
                run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO_DIR, check=False)
            shutil.rmtree(tree, ignore_errors=True)


def kill(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def cancel(jid: str) -> dict:
    job = load(jid)
    if job["status"] not in ACTIVE:
        raise ValueError(f"job is {job['status']}, not active")
    _cancelled.add(jid)
    if job["status"] == "queued":
        return update(jid, status="cancelled", finished=now_iso(), error="cancelled before start")
    proc = _procs.get(jid)
    if proc:
        kill(proc)
    return load(jid)


def dismiss(jid: str) -> dict:
    job = load(jid)
    if job["status"] in ACTIVE:
        raise ValueError("cancel it first")
    if job.get("type") == "implement" and job["status"] != "posted":
        remove_impl_tree(job)  # Discard: the local branch and worktree go too
    return update(jid, dismissed=True)


# --------------------------------------------------------------------- post

_viewer: str | None = None


def viewer() -> str:
    global _viewer
    if not _viewer:
        _viewer = T.gh(["api", "user", "--jq", ".login"]).strip()
    return _viewer


def post(jid: str, decision: str, body: str, set_label: bool) -> dict:
    """Explicit user action only. Re-verifies the exact head; never approves."""
    job = load(jid)
    if job.get("target") == "issue":
        return post_issue(jid, body, {})
    if job["status"] not in ("done",):
        raise ValueError(f"job is {job['status']}; only a finished, unposted draft can be posted")
    if decision not in ("COMMENT", "REQUEST_CHANGES"):
        raise ValueError("only COMMENT or REQUEST_CHANGES can be posted from here")
    body = (body or "").strip()
    if not body:
        raise ValueError("empty review body")
    live = T.gh_json(["pr", "view", str(job["pr"]), "-R", T.REPO, "--json", "headRefOid,baseRefName,state,author,labels"])
    if live["state"] != "OPEN":
        raise ValueError(f"PR is {live['state'].lower()}")
    if live["headRefOid"] != job["head"]:
        raise ValueError(f"PR head moved to {live['headRefOid'][:9]} after this review of {job['head'][:9]}; "
                         "run a new review")
    if live["baseRefName"] != job["base"]:
        raise ValueError("The PR base branch changed since this review; run a new review")
    f = JOBS_DIR / jid / "posted-body.md"
    f.write_text(body, encoding="utf-8")
    own = live["author"]["login"] == viewer()
    actions = []
    if own:
        # GitHub rejects reviews of your own PR; the workflow uses an ordinary comment.
        url = T.gh(["pr", "comment", str(job["pr"]), "-R", T.REPO, "--body-file", str(f)]).strip()
        actions.append("comment (own PR)")
    else:
        flag = "--comment" if decision == "COMMENT" else "--request-changes"
        T.gh(["pr", "review", str(job["pr"]), "-R", T.REPO, flag, "--body-file", str(f)])
        reviews = T.gh_json(["api", f"repos/{T.REPO}/pulls/{job['pr']}/reviews", "--paginate"])
        mine = [r for r in reviews if r["user"]["login"] == viewer() and r["commit_id"] == job["head"]]
        url = mine[-1]["html_url"] if mine else job["url"]
        actions.append(f"{decision.lower().replace('_', ' ')} review")
    if set_label and decision == "REQUEST_CHANGES":
        edit = _status_swap([label["name"] for label in live["labels"]], "waiting-on-author")
        if edit:
            T.gh(["pr", "edit", str(job["pr"]), "-R", T.REPO, *edit])
            actions.append(f"status → {T.LABELS.status('waiting-on-author')}")
    return update(jid, status="posted", body=body, decision=decision,
                  posted={"at": now_iso(), "url": url, "by": viewer(), "actions": actions})


def _status_swap(labels: list[str], role: str) -> list[str]:
    new_status = T.LABELS.status(role)
    if not new_status:
        return []
    current = T.LABELS.status_labels(labels)
    args = [] if any(T.LABELS.role(label) == role for label in current) else ["--add-label", new_status]
    for label in current:
        if T.LABELS.role(label) != role:
            args += ["--remove-label", label]
    return args


def _special_add(labels: list[str], role: str) -> list[str]:
    label = T.LABELS.special.get(role)
    return ["--add-label", label] if label and not T.LABELS.has(labels, role) else []


def post_issue(jid: str, body: str, opts: dict) -> dict:
    """Explicit user action only. Refuses if the issue moved on since the draft."""
    job = load(jid)
    if job["status"] != "done" or job.get("target") != "issue":
        raise ValueError(f"job is {job['status']}; only a finished issue draft can be posted")
    n = str(job["pr"])
    live = T.gh_json(["issue", "view", n, "-R", T.REPO, "--json", "state,labels,assignees"])
    if live["state"] != "OPEN":
        raise ValueError(f"issue is {live['state'].lower()}")
    labels = [l["name"] for l in live["labels"]]
    status = T.LABELS.current_status(labels)
    start = (job.get("facts") or {}).get("statusAtStart")
    if status != start:
        raise ValueError(f"issue status changed from {start} to {status} since this draft; run it again")
    actions, url = [], job["url"]
    body = (body or "").strip()
    if body and job["type"] == "design" and viewer().casefold() not in T.MAINTAINERS:
        raise ValueError("A configured maintainer must post the design proposal")
    if body:
        f = JOBS_DIR / jid / "posted-body.md"
        f.write_text(body, encoding="utf-8")
        url = T.gh(["issue", "comment", n, "-R", T.REPO, "--body-file", str(f)]).strip() or url
        actions.append("comment")
    edit: list[str] = []
    if job["type"] == "triage":
        known = set((job.get("facts") or {}).get("repoLabels", []))
        add = [l for l in opts.get("labelsAdd", []) if l in known and l not in labels]
        rem = [l for l in opts.get("labelsRemove", []) if l in labels]
        if T.LABELS.status_labels(add):
            rem += [l for l in T.LABELS.status_labels(labels) if l not in add and l not in rem]
        for l in add:
            edit += ["--add-label", l]
        for l in rem:
            edit += ["--remove-label", l]
        if add or rem:
            actions.append("labels " + ", ".join([f"+{l}" for l in add] + [f"-{l}" for l in rem]))
    elif job["type"] == "scope":
        rec = opts.get("applyStatus") and job.get("recommendStatus")
        if rec and (role := T.LABELS.role(rec)):
            edit += _status_swap(labels, role)
            actions.append(f"status → {rec}")
        if opts.get("decisionMade") and T.LABELS.has(labels, "maintainer_decision"):
            edit += ["--remove-label", T.LABELS.special["maintainer_decision"]]
            actions.append("decision recorded")
    elif job["type"] == "design":
        if body:
            edit += _status_swap(labels, "design-review")
        if opts.get("claim"):
            edit += ["--add-assignee", "@me", *_special_add(labels, "claimed")]
            actions.append("claimed")
    if edit:
        T.gh(["issue", "edit", n, "-R", T.REPO, *edit])
    if not actions:
        raise ValueError("nothing to post: empty comment and no label changes")
    return update(jid, status="posted", body=body,
                  posted={"at": now_iso(), "url": url, "by": viewer(), "actions": actions})


DESIGN_STAGES = {"design-review", "design-needed", "ready-for-work"}


def design_comments(comments: list[dict], number: int) -> list[dict]:
    marker = re.compile(rf"^\s*Design proposal for #{number}\b", re.I)
    return [c for c in comments if (c.get("author") or {}).get("login", "").casefold() in T.MAINTAINERS
            and marker.search(c.get("body") or "")]


def _design_digest(body: str) -> str:
    import hashlib
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def read_design(number: int) -> dict:
    """The current design proposal on the issue, live, with the discussion since it was posted."""
    if not T.PROJECT.design_enabled():
        raise ValueError("The design workflow is not configured")
    live = T.gh_json(["issue", "view", str(number), "-R", T.REPO, "--json",
                      "state,labels,comments,title,url,assignees"])
    comments = live["comments"]
    designs = design_comments(comments, number)
    if not designs:
        raise ValueError("no maintainer-posted design proposal found on the issue")
    d = designs[-1]
    after = comments[comments.index(d) + 1:]
    labels = [l["name"] for l in live["labels"]]
    assignees = [a["login"] for a in live["assignees"]]
    approval = next(({"by": c["author"]["login"], "at": c["createdAt"], "url": c["url"]}
                     for c in reversed(after)
                     if (c.get("author") or {}).get("login", "").casefold() in T.MAINTAINERS
                     and (c["body"] or "").startswith("Design approved by @")), None)
    approved = T.LABELS.has(labels, "design_approved")
    status = T.LABELS.current_status(labels)
    design_status_configured = any(T.LABELS.status(role) for role in DESIGN_STAGES)
    stage_allows_approval = not design_status_configured or T.LABELS.role(status) in DESIGN_STAGES
    impl_reason = None
    if live["state"] != "OPEN" or T.WORKFLOW.excludes(live["title"]):
        impl_reason = "the issue is closed or excluded from jobs"
    elif not approved:
        impl_reason = "the design is not approved yet"
    elif not T.base_branch():
        impl_reason = "the repository has no base commit"
    else:
        other = _open_prs_closing(number)
        impl_reason = impl_block({"designApproved": True, "assignees": assignees, "prs": other}, list_jobs())
    return {"number": number, "title": live["title"], "issueUrl": live["url"], "state": live["state"].lower(),
            "labels": labels, "status": status, "designApproved": approved, "assignees": assignees,
            "design": {"by": d["author"]["login"], "at": d["createdAt"], "url": d["url"], "body": d["body"],
                       "edited": bool(d.get("includesCreatedEdit")), "digest": _design_digest(d["body"])},
            "earlierDesigns": len(designs) - 1,
            "after": [{"by": (c.get("author") or {}).get("login", "ghost"),
                       "at": c["createdAt"], "url": c["url"], "body": c["body"]} for c in after],
            "approval": approval, "implementable": impl_reason is None, "implementReason": impl_reason,
            "approvable": (not approved and live["state"] == "OPEN" and stage_allows_approval
                           and not T.WORKFLOW.excludes(live["title"]) and viewer().casefold() in T.MAINTAINERS)}


def approve_design(number: int, digest: str | None = None) -> dict:
    """Record a maintainer's design approval: label, status, and a pointer comment.

    `digest` pins the approval to the exact design text the maintainer read.
    """
    if not digest:
        raise ValueError("read the design first: approval is pinned to the version you read")
    if not T.PROJECT.design_enabled() or viewer().casefold() not in T.MAINTAINERS:
        raise ValueError("A configured maintainer must approve the design")
    n = str(number)
    live = T.gh_json(["issue", "view", n, "-R", T.REPO, "--json", "state,labels,comments,title,assignees"])
    if live["state"] != "OPEN":
        raise ValueError(f"issue is {live['state'].lower()}")
    if T.WORKFLOW.excludes(live["title"]):
        raise ValueError("Issue is excluded by the configured title pattern")
    designs = design_comments(live["comments"], number)
    labels = [l["name"] for l in live["labels"]]
    if not designs:
        raise ValueError("no maintainer-posted design proposal found on the issue")
    if any(T.LABELS.status(role) for role in DESIGN_STAGES) and not any(
            T.LABELS.role(label) in DESIGN_STAGES for label in labels):
        raise ValueError("issue is not waiting on a design review")
    if T.LABELS.has(labels, "design_approved"):
        raise ValueError("design is already approved")
    if _design_digest(designs[-1]["body"]) != digest:
        raise ValueError("the design changed since you read it (edited, or a newer proposal was posted); reopen it and read again")
    approval_label = T.LABELS.special["design_approved"]
    existing = T.gh_json(["label", "list", "-R", T.REPO, "--search", approval_label, "--json", "name"])
    if not any(l["name"].casefold() == approval_label.casefold() for l in existing):
        T.gh(["label", "create", approval_label, "-R", T.REPO, "--color", "0e8a16",
              "--description", "Approved design, ready for implementation"])
    design_url = designs[-1]["url"]
    note = JOBS_DIR / f"approve-{number}.md"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    claimed = bool(live["assignees"]) or T.LABELS.has(labels, "claimed")
    next_role = "in-progress" if claimed else "ready-for-work"
    note.write_text(f"Design approved by @{viewer()}: {design_url}\n\n"
                    + ("Next: the assignee can implement it." if claimed else "Next: a contributor can implement it."),
                    encoding="utf-8")
    T.gh(["issue", "comment", n, "-R", T.REPO, "--body-file", str(note)])
    edit = ["--add-label", approval_label, *_status_swap(labels, next_role)]
    T.gh(["issue", "edit", n, "-R", T.REPO, *edit])
    return {"number": number, "approved": design_url, "status": T.LABELS.status(next_role)}


def land_pr(number: int, head: str) -> dict:
    """Recheck the live gates and submit a merge pinned to the head the user confirmed."""
    if not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        raise ValueError("head must be a full 40-character commit SHA")
    T.repo_info.cache_clear()
    if T.repo_info()["viewerPermission"] not in ("ADMIN", "MAINTAIN", "WRITE"):
        raise ValueError("The signed-in GitHub account does not have merge access")
    raw = T.paged(T.PR_QUERY, "pullRequests")
    pr = next((p for p in raw if p["number"] == number), None)
    if not pr:
        raise ValueError(f"PR #{number} is not open")
    rules = T.read_rules()
    ev = T.evaluate_pr(pr, rules, {})
    if ev["head"] != head:
        raise ValueError(f"head moved: you saw {head[:9]}, the PR is now at {ev['head'][:9]}; refresh and look again")
    if ev["verdict"] not in ("ready", "needs-auth"):
        raise ValueError("not landable on this head: " + "; ".join(ev["blockers"] or [ev["verdict"]]))
    n = str(number)
    authorized = None
    method = rules["merge_method"]
    if ev["verdict"] == "needs-auth" and T.WORKFLOW.post_authorization_comment:
        note = JOBS_DIR / f"land-{number}.md"
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        note.write_text(
            f"@{viewer()} authorized landing commit `{head}` into `{T.base_branch()}` "
            f"using the {method} merge method.",
            encoding="utf-8")
        authorized = T.gh(["pr", "comment", n, "-R", T.REPO, "--body-file", str(note)]).strip()
    T.gh(["pr", "merge", n, "-R", T.REPO, f"--{method}", "--match-head-commit", head])
    after = T.gh_json(["pr", "view", n, "-R", T.REPO, "--json", "state,mergeCommit,autoMergeRequest"])
    if after["state"] == "OPEN":
        queued = bool(after["autoMergeRequest"])
        if not queued:
            data = T.graphql("""
                query($owner:String!, $name:String!, $number:Int!) {
                  repository(owner:$owner, name:$name) { pullRequest(number:$number) { mergeQueueEntry { id } } }
                }""", owner=T.OWNER, name=T.NAME, number=number)
            queued = bool(data["repository"]["pullRequest"]["mergeQueueEntry"])
        if queued:
            return {"number": number, "head": head, "queued": True, "authorization": authorized}
    if after["state"] != "MERGED":
        raise RuntimeError(f"merge command returned but PR #{number} is {after['state'].lower()}")
    closed = {}
    for c in ev["closes"]:
        closed[c] = T.gh_json(["issue", "view", str(c), "-R", T.REPO, "--json", "state"])["state"].lower()
    return {"number": number, "head": head, "merge": after["mergeCommit"]["oid"],
            "authorization": authorized, "issues": closed}


def _live_pr(number: int, head: str) -> dict:
    """The open PR, refused unless it is still at the exact head the maintainer saw."""
    if not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        raise ValueError("head must be a full 40-character commit SHA")
    pr = T.gh_json(["pr", "view", str(number), "-R", T.REPO,
                    "--json", "state,isDraft,headRefOid,mergeStateStatus,baseRefName,author"])
    if pr["state"] != "OPEN":
        raise ValueError(f"PR #{number} is {pr['state'].lower()}")
    if pr["headRefOid"] != head:
        raise ValueError(f"head moved: you saw {head[:9]}, the PR is now at {pr['headRefOid'][:9]}; refresh and look again")
    return pr


def mark_ready(number: int, head: str) -> dict:
    """Take a draft out of draft, under the maintainer's account, pinned to the head they saw."""
    pr = _live_pr(number, head)
    if not pr["isDraft"]:
        return {"number": number, "head": head, "changed": False}
    permitted = {viewer().casefold(), (impl_config().get("bot_login") or "").casefold()} - {""}
    if (pr.get("author") or {}).get("login", "").casefold() not in permitted:
        raise ValueError("Only drafts by the signed-in user or configured bot can be marked ready here")
    T.gh(["pr", "ready", str(number), "-R", T.REPO])
    return {"number": number, "head": head, "changed": True}


def rebase_pr(number: int, head: str) -> dict:
    """Rebase the PR onto its base (GitHub's update-branch). This creates a new head, so its
    checks rerun and it needs a review of that new head before it can land."""
    pr = _live_pr(number, head)
    if pr["baseRefName"] != T.base_branch():
        raise ValueError(f"PR #{number} does not target {T.base_branch()}")
    if pr["mergeStateStatus"] == "DIRTY":
        raise ValueError(f"PR #{number} conflicts with {T.base_branch()}; the author must resolve it")
    T.gh(["pr", "update-branch", str(number), "-R", T.REPO, "--rebase"])
    after = T.gh_json(["pr", "view", str(number), "-R", T.REPO, "--json", "headRefOid"])["headRefOid"]
    if after == head:
        raise RuntimeError("GitHub accepted the rebase but the head did not change yet; refresh in a minute")
    return {"number": number, "from": head, "head": after}


def run_sync(prs: list[int]) -> list[dict]:
    """For the command line and a future scheduler: enqueue and execute now, drafts only."""
    res = enqueue(prs)
    for j in res["created"]:
        execute(j["id"])
    return [load(j["id"]) for j in res["created"]] + [{"skipped": s} for s in res["skipped"]]
