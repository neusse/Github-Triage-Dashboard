# Workflow guide

Use the repository selector to focus on one GitHub repository. **Overview** summarizes its active queues. **Issues**, **PR queue**, and **CI & rules** show the underlying items. **Changes** compares snapshots and lets you mark the current one as seen.

## Work across repositories

Open **Inbox** to see pending work across saved repositories. Filter by repository or select **Refresh all repositories** to update coverage. The Inbox reports stale, missing, and failed snapshot refreshes so you can judge how current the decision queue is.

| Inbox entry | Where it takes you |
| --- | --- |
| Draft awaiting review | **Open draft** opens the editor in the correct repository. |
| Maintainer decision | Links to the repository’s decision queue and the issue on GitHub. |
| Failed or incomplete triage run | Opens the run history for diagnosis. |
| Missed automatic run | Opens the affected project’s schedule settings. |

Failed, incomplete, and missed triage runs appear for seven days. A manual run can prepare fresh drafts when an automatic slot was missed.

## Triage an issue and review a design

1. Open **Work → Issue pipeline**. The available stages depend on your [label mappings](configuration.md#label-mappings). Select a stage and choose **Draft triage**, **Draft scope**, or **Draft design** on an eligible issue.
2. Open **Work → Runs** to inspect the agent’s result. Edit the comment and review any proposed label changes before clicking **Post**. Items marked `ready-for-human` are reserved for people.
3. For projects that use design approval, select **Review design** to read the posted design and discussion. A configured maintainer can approve the current proposal or request changes. Approval enables implementation when the issue is eligible.
4. Start implementation from the approved design. The agent works in a local worktree. The dashboard runs configured validation commands, then presents the diff and editable PR details in **Runs**. Choose **Publish PR** to push the branch, open a PR, and apply the configured GitHub updates.

## Review and land a pull request

1. Open **Work → PR reviews**. Check the PRs you want to review and select **Review selected**, or use **Review all waiting**. A PR in the main list may also offer **Draft review**.
2. Open the completed draft in **Runs**. Edit the review and choose **Comment** or **Request changes**. A review of your own PR uses **Comment**. Posting requires your confirmation and checks that the reviewed head and base are current. Approval remains a separate action on GitHub.
3. A PR becomes eligible for **Land** after it passes the dashboard’s configured review and merge checks. Confirm the displayed commit to land it. The dashboard checks the live gates and current head again before asking GitHub to merge or queue the PR.

[Configure review and merge requirements](configuration.md#review-requirements-and-merge-settings) for each repository.
