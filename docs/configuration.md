# Project configuration

Open **Projects** to add or edit a GitHub repository. Settings apply to the selected repository. **Save project** reloads its local worker. If an active run or refresh blocks saving, wait for it to finish and save again.

| Field | How it is used |
| --- | --- |
| **GitHub repository** | One GitHub repository per project, in `owner/repo` form. |
| **Default agent** | Codex by default. Select Claude Code to use it for new jobs in this project. |
| **Base branch** | Leave blank to follow the default branch on GitHub. PRs targeting other branches remain visible. |
| **Maintainers** | GitHub logins for maintainer reviews, standing authorization, and design approval. |

## Label mappings

Projects assume [Matt Pocock’s engineering skills](https://github.com/mattpocock/skills) and the labels `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. **Check labels** reads existing names and loads suggestions for the mapping fields. **Create missing labels** adds the standard labels to GitHub after confirmation.

Map only the workflow roles you use. For example, map **Ready for work** to `ready-for-agent` and **Waiting on author** to `changes requested`. An issue labeled **Ready for human** stays outside the agent job pipeline. Enter one priority or kind label per line, highest first. The first two priority labels count as urgent. Use an area prefix such as `area/` to group area labels.

**More status and special labels** includes mappings for claimed work, tracker issues, declined requests, maintainer decisions, and design approvals. Tracker and `wontfix` labels exclude issues from scheduled drafting. Set **Maintainers** and map **Design approved** to enable design approval and implementation. Map **Design needed** or **Ready for work** to offer design drafting, and map **Design review** to track posted designs with a status label.

## Review requirements and merge settings

| Control | Effect |
| --- | --- |
| **Maintainer review** | Require a clean review of the current commit from a configured maintainer other than the PR author. |
| **Latest base branch commits** | Require the PR to include the current base branch. |
| **Resolved review threads** | Require all review threads to be resolved. |
| **Status label** | Flag open issues and PRs that lack a recognized status label. |
| **Additional required checks** | Require named checks or statuses on the current PR commit. Use one exact GitHub check name per line. |
| **Merge method** | Choose merge, squash, or rebase. Automatic uses the first method allowed by GitHub in that order. |
| **Authors with standing authorization** | Mark passing PRs from selected maintainers as ready under standing authorization. Landing still requires a click. |
| **Authorization comment** | Record the confirmed commit in a comment when landing a PR without standing authorization. Off by default. |

The dashboard reads GitHub rulesets and classic branch protection where the account can access them. GitHub enforces merge eligibility when receiving the request.

## Advanced settings

Use **Advanced settings** for repository-specific options beyond the common form controls. [config.example.json](../config.example.json) shows the project configuration format.

| Key | Purpose |
| --- | --- |
| `workflow.stale_days` | Days before an inactive claim or author wait is flagged. Defaults to 14. |
| `workflow.gate_commands` | Local checks for implementation jobs. Use argument arrays such as `[["python", "-m", "unittest", "discover"]]`. The dashboard runs them in the worktree without a shell. The default empty list runs zero checks. |
| `workflow.policy_files` | Repository-relative files for job prompts when present. Defaults to `AGENTS.md`, `CONTRIBUTING.md`, `docs/agents/issue-tracker.md`, and `docs/agents/triage-labels.md`. |
| `workflow.pr_template` | Optional repository-relative template. Otherwise the dashboard checks `.github/`, the root, and `docs/` for `pull_request_template.md`. |
| `workflow.ci_workflow` | Optional GitHub Actions workflow name, ID, or filename. Otherwise the dashboard shows recent runs on the target branch. |
| `workflow.blocked_title_pattern` | Optional Python regular expression. Issues with matching titles appear in the dashboard and are skipped by the job pipeline. |
| `workflow.fixture_branch_prefix` | Optional prefix for a branch named `<prefix><issue>-<slug>`. A matching fixture branch supplies the implementation starting commit. |
| `triage_reports_issue` | Optional issue number. **Triage reports** can display up to two reports delimited by `<!-- report:start TIMESTAMP -->` and `<!-- report:end -->`. The dashboard reads these reports. |

Configure agent options under `review` for triage, scope, design, and PR review jobs, or under `implement` for implementation jobs. Both support `model`, `effort`, and `timeout_min`. Leave `model` unset to use the agent CLI default. `max_budget_usd` applies to Claude Code. Implementation also supports `gate_timeout_min`, `git_name`, `git_email`, `bot_login`, and `gh_token_env`. Set `gh_token_env` to the name of an optional publishing-token environment variable, then set the token in the environment used to start the dashboard.

See [scheduled triage](scheduled-triage.md) for per-project times, limits, and time zones, and the [CLI reference](development.md#standalone-cli) for local profiles and environment overrides.
