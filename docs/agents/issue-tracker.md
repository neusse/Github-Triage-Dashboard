# Issue tracker

Use GitHub Issues in `neusse/Github-Triage-Dashboard` for project issues and specs. The Git `origin` identifies this repo. The dashboard's `TRIAGE_REPO` setting identifies the repository it monitors.

- List open issues with `gh issue list --state open`. Use `--label` to filter.
- Fetch a ticket with `gh issue view <number> --comments`.
- Publish a ticket with `gh issue create --title "..." --body-file <path>`.
- Use `gh issue comment` to reply, `gh issue edit` to update labels or assignees, and `gh issue close` to close.

**PRs as a request surface: no.** If enabled, use the same triage labels and states for external PRs. Inspect them with `gh pr view <number> --comments` and `gh pr diff <number>`.

## Wayfinding

Use an issue labeled `wayfinder:map` for the map. Link tickets as sub-issues and use GitHub issue dependencies to record blockers. If either feature is unavailable, use a task list in the map for membership or `Blocked by: #<number>` in a ticket for dependencies. Claim the first unassigned ticket with no open blockers. On resolution, record the answer, close the ticket, and link the decision from the map.
