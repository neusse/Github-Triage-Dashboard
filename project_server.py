"""Local project registry and router for independently scoped dashboard workers."""

from __future__ import annotations

import http.client
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from project_config import MATT_LABELS, ROOT, mapping, parse_project, read_config, target_config, write_json
from inbox import build_inbox, timestamp
from scheduled_triage import UTC, parse_schedule, schedule_windows

PROFILES = ROOT / ".triage-dashboard"
MAX_BODY = 1_000_000


def public_profile(profile: dict) -> dict:
    return {key: value for key, value in profile.items() if key != "_schedule_windows"}


def gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:500])
    return result.stdout


def profile_path(repo: str, root: Path = PROFILES) -> Path:
    if not isinstance(repo, str) or not repo or repo != repo.strip():
        raise ValueError("Enter a GitHub repository in owner/name form")
    return root / parse_project({"repo": repo}).storage_path / "profile.json"


@dataclass
class Worker:
    process: subprocess.Popen
    port: int
    token: str
    lock: threading.RLock = field(default_factory=threading.RLock)

    def request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            conn.request(method, path, body, {**(headers or {}), "X-Triage-Internal": self.token})
            response = conn.getresponse()
            return response.status, response.read(), response.getheader("Content-Type", "application/json")
        finally:
            conn.close()

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=4)


class DashboardProjects:
    def __init__(self, root: Path = PROFILES):
        self.root = root
        self.lock = threading.RLock()
        self.workers: dict[str, Worker] = {}
        self.preferred = None
        self.refresh_lock = threading.Lock()
        self.refresh_pending: set[str] = set()
        self.refresh_errors: dict[str, str] = {}
        self.closing = False

    def entries(self):
        return [read_config(path) for path in sorted(self.root.glob("owner-*/repo-*/profile.json"))]

    def default(self):
        choices = [p["repo"] for p in self.entries()]
        return next((r for r in choices if r.casefold() == (self.preferred or "").casefold()),
                    choices[0] if choices else None)

    def read(self, repo: str) -> dict:
        entry = read_config(profile_path(repo, self.root))
        if not entry:
            raise FileNotFoundError(f"Add {repo} in Projects first")
        return entry

    def import_legacy(self):
        data = read_config()
        repo = parse_project(data, os.environ.get("TRIAGE_REPO")).repo
        if repo:
            self.preferred = repo
            path = profile_path(repo, self.root)
            if not path.exists():
                raw = {**target_config(data, repo), "repo": repo}
                parse_schedule(raw.get("schedule"))
                write_json(path, raw)
        now = dt.datetime.now(UTC)
        for profile in self.entries():
            if "_schedule_windows" not in profile:
                profile["_schedule_windows"] = schedule_windows({}, profile, now)
                write_json(profile_path(profile["repo"], self.root), profile)

    def save(self, raw: dict):
        raw = public_profile(raw)
        repo = parse_project(raw).repo
        path = profile_path(repo, self.root)
        parse_schedule(raw.get("schedule"))
        if raw.get("agent", "codex") not in ("codex", "claude"):
            raise ValueError("Choose Codex or Claude Code")
        if any(key in raw for key in ("gh_token", "token", "github_token")):
            raise ValueError("Set credentials through gh auth or environment variables")
        if not path.exists():
            meta = json.loads(gh(["repo", "view", repo, "--json", "nameWithOwner"]))
            if meta["nameWithOwner"].casefold() != repo.casefold():
                raise ValueError(f"Use the repository's current name: {meta['nameWithOwner']}")
            raw = {**raw, "repo": meta["nameWithOwner"]}
        key = repo.casefold()
        with self.lock:
            old = read_config(path)
            if public_profile(old) == raw:
                return old
            worker = self.workers.get(key)
            if worker and worker.process.poll() is None:
                with worker.lock:
                    code, body, _ = worker.request("POST", "/api/internal/quiesce", b"{}")
                    if code != 200:
                        raise ValueError(json.loads(body).get("error", "Wait for active jobs before saving"))
                    worker.close()
            self.workers.pop(key, None)
            raw["_schedule_windows"] = schedule_windows(old, raw, dt.datetime.now(UTC))
            write_json(path, raw)
        if parse_schedule(raw.get("schedule")).enabled:
            self.get_worker(raw["repo"])
        return raw

    def get_worker(self, repo: str):
        with self.lock:
            if self.closing:
                raise RuntimeError("The dashboard is shutting down.")
            profile = self.read(repo)
            key = profile["repo"].casefold()
            worker = self.workers.get(key)
            if worker and worker.process.poll() is None:
                return worker
            path = profile_path(profile["repo"], self.root)
            token = secrets.token_hex(24)
            ready = path.with_name(f"worker-{token}.port")
            env = {**os.environ, "TRIAGE_PROJECT_CONFIG": str(path.resolve()), "TRIAGE_REPO": profile["repo"],
                   "TRIAGE_WORKER_TOKEN": token}
            with path.with_name("worker.log").open("ab") as log:
                process = subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / "triage.py"), "serve", "--worker", "--no-browser",
                     "--port", "0", "--ready-file", str(ready)],
                    cwd=ROOT, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        break
                    if ready.exists():
                        port = ready.read_text(encoding="ascii").strip()
                        if port.isdigit():
                            worker = Worker(process, int(port), token)
                            self.workers[key] = worker
                            return worker
                    time.sleep(0.05)
                raise RuntimeError(f"Worker for {repo} failed to start. See {path.with_name('worker.log')}.")
            finally:
                ready.unlink(missing_ok=True)
                if key not in self.workers or self.workers[key].process is not process:
                    if process.stdin:
                        process.stdin.close()
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=4)

    def start_enabled(self):
        for p in self.entries():
            if parse_schedule(p.get("schedule")).enabled:
                try:
                    self.get_worker(p["repo"])
                except (ValueError, RuntimeError, OSError) as exc:
                    print(f"[projects] {exc}", file=sys.stderr)

    def close(self):
        with self.lock:
            self.closing = True
            workers = list(self.workers.values())
        for worker in workers:
            worker.close()

    def inbox(self):
        data = build_inbox(self.entries())
        with self.refresh_lock:
            data["refreshing"] = sorted(self.refresh_pending)
            for item in data["projects"]:
                item["error"] = self.refresh_errors.get(item["repo"]) or item["error"]
        return data

    def refresh_inbox(self):
        repos = [entry["repo"] for entry in self.entries()]
        with self.refresh_lock:
            if not self.refresh_pending and repos:
                self.refresh_pending.update(repos)
                self.refresh_errors.clear()
                threading.Thread(target=self._refresh_inbox, args=(repos,), daemon=True).start()
            return {"refreshing": sorted(self.refresh_pending)}

    def _refresh_inbox(self, repos):
        with ThreadPoolExecutor(max_workers=min(len(repos), 4)) as pool:
            list(pool.map(self._refresh_inbox_project, repos))

    def _refresh_inbox_project(self, repo):
        error = ""
        try:
            worker = self.get_worker(repo)
            began = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
            for _ in range(25):
                with worker.lock:
                    code, body, _ = worker.request("POST", "/api/refresh", headers={"X-Triage": "1"})
                if code == 200:
                    break
                if code != 409:
                    raise RuntimeError(json.loads(body).get("error") or "Snapshot refresh failed")
                # A new or already active worker may be collecting the same snapshot.
                time.sleep(0.6)
                snapshot = ROOT / "snapshots" / parse_project({"repo": repo}).storage_path / "latest.json"
                at = timestamp(read_config(snapshot).get("generatedAt"))
                if at and at >= began:
                    break
            else:
                raise RuntimeError("A refresh for this repository is still running.")
        except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
            error = str(exc)[:500]
        finally:
            with self.refresh_lock:
                if error:
                    self.refresh_errors[repo] = error
                self.refresh_pending.discard(repo)

    def labels(self, repo: str, create: bool = False) -> dict:
        repo = self.read(repo)["repo"]
        get_labels = lambda: set(json.loads(gh(["label", "list", "-R", repo, "--limit", "1000",
                                               "--json", "name", "--jq", "[.[].name]"])))
        existing = get_labels()
        created = []
        if create:
            for label, (color, description) in MATT_LABELS.items():
                if label.casefold() not in {name.casefold() for name in existing}:
                    gh(["label", "create", label, "-R", repo, "--color", color, "--description", description])
                    created.append(label)
            existing = get_labels()
        present = {name.casefold() for name in existing}
        return {"missing": [n for n in MATT_LABELS if n.casefold() not in present], "created": created,
                "labels": sorted(existing, key=str.casefold)}


def serve(port: int, show_browser: bool):
    projects = DashboardProjects()
    projects.import_legacy()

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def respond(self, status, data):
            self.send(status, json.dumps(data).encode())

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
                return self.respond(403, {"error": "Forbidden host"})
            method = self.command
            if method == "POST" and self.headers.get("X-Triage") != "1":
                return self.respond(403, {"error": "Missing dashboard action header"})
            url = urlsplit(self.path)
            path = url.path
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= MAX_BODY:
                    raise ValueError("Request body is too large")
                raw = self.rfile.read(length) if length else None
                query = parse_qs(url.query)
                if path == "/api/projects" and method == "GET":
                    return self.respond(200, {"projects": [{"repo": p["repo"], "agent": p.get("agent", "codex"),
                                                           "scheduled": parse_schedule(p.get("schedule")).enabled}
                                                          for p in projects.entries()]})
                if path == "/api/inbox" and method == "GET":
                    return self.respond(200, projects.inbox())
                if path == "/api/inbox/refresh" and method == "POST":
                    return self.respond(202, projects.refresh_inbox())
                if path == "/api/project-config":
                    data = (projects.save(mapping(json.loads(raw or b"{}"), "profile")) if method == "POST" else
                            projects.read(query.get("repo", [""])[0]))
                    return self.respond(200, {"profile": public_profile(data)})
                if path == "/api/project-labels":
                    repo = (mapping(json.loads(raw or b"{}"), "label request").get("repo", "") if method == "POST"
                            else query.get("repo", [""])[0])
                    return self.respond(200, projects.labels(repo, create=method == "POST"))
                if path in ("/vendor/marked.min.js", "/vendor/purify.min.js", "/projects.js", "/inbox.js",
                            "/workflow-editor.js") and method == "GET":
                    return self.send(200, (ROOT / path.lstrip("/")).read_bytes(), "text/javascript; charset=utf-8")
                if path == "/docs/assets/github-triage-dashboard-icon.svg" and method == "GET":
                    icon = ROOT / path.lstrip("/")
                    return self.send(200, icon.read_bytes(), "image/svg+xml")
                if path == "/" and method == "GET":
                    if repo := projects.default():
                        return self.redirect(f"/p/{repo}/")
                    return self.send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
                target, endpoint = None, path
                if path.startswith("/p/"):
                    parts = path.split("/", 4)
                    if len(parts) < 4:
                        return self.respond(404, {"error": "Project not found"})
                    target = f"{parts[2]}/{parts[3]}"
                    projects.read(target)
                    if len(parts) == 4 and method == "GET":
                        return self.redirect(path + "/")
                    endpoint = "/" + parts[4] if len(parts) > 4 else "/"
                    if endpoint == "/" and method == "GET":
                        return self.send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
                else:
                    target = projects.default()
                if not endpoint.startswith("/api/") or endpoint.startswith("/api/internal/"):
                    return self.respond(404, {"error": "Unknown route"})
                if not target:
                    return self.respond(404, {"error": "Add a project first"})
                worker = projects.get_worker(target)
                if url.query:
                    endpoint += "?" + url.query
                headers = {k: self.headers[k] for k in ("X-Triage", "X-Triage-Repo", "Content-Type") if k in self.headers}
                with worker.lock:
                    status, body, content_type = worker.request(method, endpoint, raw, headers)
                return self.send(status, body, content_type)
            except FileNotFoundError as exc:
                return self.respond(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                return self.respond(400, {"error": str(exc)})
            except (OSError, RuntimeError, subprocess.TimeoutExpired, http.client.HTTPException) as exc:
                return self.respond(502, {"error": str(exc)})

        def redirect(self, path):
            self.send_response(302)
            self.send_header("Location", path)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        threading.Thread(target=projects.start_enabled, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"[triage] serving projects at {url}", flush=True)
        if show_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            projects.close()
