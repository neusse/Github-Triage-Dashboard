"""Configuration shared by the collector, dashboard, and worker."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUS_ROLES = (
    "triage", "needs-information", "needs-scope-review", "design-needed", "design-review",
    "ready-for-work", "ready-for-human", "in-progress", "waiting-on-author", "waiting-on-dependency",
    "waiting-on-review", "ready-to-merge", "inactive",
)
SPECIAL_ROLES = ("claimed", "tracker", "wontfix", "maintainer_decision", "design_approved")
MERGE_METHODS = ("merge", "squash", "rebase")
MATT_STATUSES = {"triage": "needs-triage", "needs-information": "needs-info",
                 "ready-for-work": "ready-for-agent", "ready-for-human": "ready-for-human"}
MATT_LABELS = {
    "needs-triage": ("d4c5f9", "Needs maintainer triage"),
    "needs-info": ("d876e3", "Waiting for more information"),
    "ready-for-agent": ("0e8a16", "Ready for an agent to implement"),
    "ready-for-human": ("1d76db", "Requires human implementation"),
    "wontfix": ("ffffff", "Will not be implemented"),
}


def read_config(path: Path | None = None) -> dict:
    if path is None:
        path = Path(os.environ.get("TRIAGE_PROJECT_CONFIG") or ROOT / "config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc
    return mapping(data, path.name)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8") as file:
        tmp = Path(file.name)
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def mapping(value, key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def string(value, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string or null")
    return value.strip()


def strings(value, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{key} must be an array of nonempty strings")
    return tuple(v.strip() for v in value)


def flag(options: dict, key: str, default: bool = False) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"workflow.{key} must be true or false")
    return value


def integer(value, key: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{key} must be an integer of at least {minimum}")
    return value


def target_config(data: dict, repo: str) -> dict:
    """Do not carry issue IDs, credentials, or rules into an environment override."""
    configured = string(data.get("repo"), "repo")
    if configured and configured.casefold() != repo.casefold():
        return {"agent": data["agent"]} if "agent" in data else {}
    return data


@dataclass(frozen=True)
class Labels:
    statuses: dict[str, str]
    special: dict[str, str]
    priorities: tuple[str, ...]
    kinds: tuple[str, ...]
    area_prefix: str | None

    def status(self, role: str) -> str | None:
        return self.statuses.get(role)

    def role(self, label: str | None) -> str | None:
        if not isinstance(label, str):
            return None
        return next((r for r, name in self.statuses.items()
                     if name.casefold() == label.casefold()),
                    next((r for r, name in MATT_STATUSES.items() if name == label.casefold()), None))

    def status_labels(self, labels: list[str]) -> list[str]:
        return [label for label in labels if self.role(label)]

    def current_status(self, labels: list[str]) -> str | None:
        return next(iter(self.status_labels(labels)), None)

    def has(self, labels: list[str], role: str) -> bool:
        label = self.special.get(role)
        return bool(label and any(other.casefold() == label.casefold() for other in labels))

    @staticmethod
    def rank(label: str | None, ordered: tuple[str, ...]) -> int:
        return next((i for i, name in enumerate(ordered) if label and name.casefold() == label.casefold()),
                    len(ordered))


@dataclass(frozen=True)
class Workflow:
    require_maintainer_review: bool
    require_up_to_date: bool
    require_resolved_threads: bool
    require_status_label: bool
    required_checks: tuple[str, ...]
    standing_authorization: frozenset[str]
    post_authorization_comment: bool
    merge_method: str | None
    stale_days: int
    gate_commands: tuple[tuple[str, ...], ...]
    policy_files: tuple[str, ...]
    pr_template: str | None
    ci_workflow: str | None
    blocked_title_pattern: re.Pattern | None
    fixture_branch_prefix: str | None

    def excludes(self, title: str) -> bool:
        return bool(self.blocked_title_pattern and self.blocked_title_pattern.search(title))


@dataclass(frozen=True)
class Project:
    repo: str
    base_branch: str | None
    maintainers: frozenset[str]
    labels: Labels
    workflow: Workflow
    reports_issue: int

    @property
    def storage_path(self) -> Path:
        if not self.repo:
            return Path("unconfigured")
        owner, name = self.repo.lower().split("/")
        return Path(f"owner-{owner}", f"repo-{name}")

    def design_enabled(self) -> bool:
        return bool(self.maintainers and self.labels.special.get("design_approved"))


def parse_project(data: dict, override: str | None = None) -> Project:
    repo = string(override if override is not None else data.get("repo"), "repo") or ""
    if repo and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError('Set "repo" to a GitHub repository in owner/name form')
    if repo and (repo.split("/")[1] in (".", "..") or repo.endswith(".")):
        raise ValueError("Invalid GitHub repository name")
    data = target_config(data, repo)
    maintainers = frozenset(v.casefold() for v in strings(data.get("maintainers", []), "maintainers"))
    label_data = mapping(data.get("labels", {}), "labels")
    statuses = {}
    status_mapping = {**MATT_STATUSES, **mapping(label_data.get("statuses", {}), "labels.statuses")}
    for role, value in status_mapping.items():
        if role not in STATUS_ROLES:
            raise ValueError(f"Unknown status role {role!r}")
        if (label := string(value, f"labels.statuses.{role}")) is not None:
            statuses[role] = label
    special = {role: label for role in SPECIAL_ROLES
               if (label := string(label_data.get(role, "wontfix" if role == "wontfix" else None),
                                   f"labels.{role}")) is not None}
    names = [label.casefold() for label in (*statuses.values(), *special.values())]
    if len(names) != len(set(names)):
        raise ValueError("Each configured status and special label must be distinct")
    labels = Labels(statuses, special, strings(label_data.get("priorities", []), "labels.priorities"),
                    strings(label_data.get("kinds", []), "labels.kinds"),
                    string(label_data.get("area_prefix"), "labels.area_prefix"))

    wf = mapping(data.get("workflow", {}), "workflow")
    commands = wf.get("gate_commands", [])
    if not isinstance(commands, list):
        raise ValueError("workflow.gate_commands must be an array of argument arrays")
    argv = tuple(strings(command, "workflow.gate_commands entry") for command in commands)
    if any(not command for command in argv):
        raise ValueError("workflow.gate_commands entries must include an executable")
    standing = frozenset(v.casefold() for v in strings(
        wf.get("standing_authorization", []), "workflow.standing_authorization"))
    if not standing <= maintainers:
        raise ValueError("workflow.standing_authorization must contain configured maintainers")
    merge = string(wf.get("merge_method"), "workflow.merge_method")
    if merge and merge not in MERGE_METHODS:
        raise ValueError("workflow.merge_method must be merge, squash, rebase, or null")
    title = string(wf.get("blocked_title_pattern"), "workflow.blocked_title_pattern")
    try:
        excluded = re.compile(title) if title else None
    except re.error as e:
        raise ValueError(f"Invalid workflow.blocked_title_pattern: {e}") from e
    workflow = Workflow(
        flag(wf, "require_maintainer_review"), flag(wf, "require_up_to_date"),
        flag(wf, "require_resolved_threads"), flag(wf, "require_status_label"),
        strings(wf.get("required_checks", []), "workflow.required_checks"), standing,
        flag(wf, "post_authorization_comment"), merge, integer(wf.get("stale_days", 14), "workflow.stale_days", 1),
        argv, strings(wf.get("policy_files", ["AGENTS.md", "CONTRIBUTING.md", "docs/agents/issue-tracker.md",
                                            "docs/agents/triage-labels.md"]), "workflow.policy_files"),
        string(wf.get("pr_template"), "workflow.pr_template"), string(wf.get("ci_workflow"), "workflow.ci_workflow"),
        excluded, string(wf.get("fixture_branch_prefix"), "workflow.fixture_branch_prefix"),
    )
    if workflow.require_maintainer_review and not maintainers:
        raise ValueError("workflow.require_maintainer_review needs at least one maintainer")
    if workflow.require_status_label and not statuses:
        raise ValueError("workflow.require_status_label needs labels.statuses")

    branch = string(data.get("base_branch"), "base_branch")
    if branch and (branch.startswith(("-", "/")) or re.search(r"[\s~^:?*\[\\]", branch) or ".." in branch):
        raise ValueError("base_branch must be a Git branch name")
    return Project(repo, branch, maintainers, labels, workflow,
                   integer(data.get("triage_reports_issue", 0), "triage_reports_issue"))


@cache
def project() -> Project:
    return parse_project(read_config(), os.environ.get("TRIAGE_REPO"))
