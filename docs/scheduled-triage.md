# Scheduled triage

Each repository can run recurring issue triage and PR review passes. A pass collects a fresh GitHub snapshot and prepares local drafts. A person reviews each draft in **Work → Runs** before posting or changing anything on GitHub.

## Configure and preview a schedule

1. Open **Projects**, select a saved repository, and confirm its default agent works. Set maintainer logins and any additional policy files needed for the project. [Project configuration](configuration.md) covers those settings and the baseline triage labels.
2. In **Scheduled triage runs**, select **Draft issue triage now** or **Draft PR reviews now** to preview a pass. These buttons work while the schedule is paused.
3. Under **Scheduled triage**, enter one or more comma-separated `HH:MM` times for each pass and set the draft limits. Clear a pass’s times to disable it.
4. Select **Enable scheduled drafts while this dashboard is running**, then **Save project**. Confirm the upcoming times in **Scheduled triage runs**.

| Setting | Default |
| --- | --- |
| Enabled | Paused |
| Time zone | `local`, using the dashboard computer’s time zone |
| Issue times | `08:00, 16:00` |
| PR review times | `08:30, 16:30` |
| Draft limit per pass | 5 issues or 1 PR, configurable up to 25 |

For named zones, enter `UTC` or an IANA name such as `America/Los_Angeles`. If the system lacks the named zone, run `python -m pip install tzdata` or use `local` or `UTC`.

## What the passes consider

- **Issue triage** examines unseen or materially changed open issues. A change in comments, labels, assignments, milestones, or linked PRs can trigger another assessment. The pass skips excluded, human-only, or settled work, items with a pending local draft, and assessments that are still current. The draft recommends the next action and who should take it.
- **PR review** prioritizes the oldest eligible open PRs that still need review. A draft is tied to the current head and considers the diff, associated issue, prior review, checks, and current rules. It separates standards findings from specification findings. Current local drafts and recognized reviews from configured maintainers prevent duplicate drafting. The run also summarizes landing candidates and blockers.

## Read run history

The run history shows candidates, drafts, skipped items, and errors. Use **Open draft** to review the proposed text or inspect a PR review’s commit. Passes that find no work still appear in history.

Keep the dashboard running to start scheduled passes. An enabled slot is missed if an automatic pass does not start within five minutes of its scheduled time. Missed slots appear in **Inbox** for seven days. The dashboard skips past slots on startup and excludes paused periods from missed-run accounting. A manual pass is recorded separately and leaves the original slot’s status in history.
