# Getting started

The dashboard runs locally and connects to GitHub through the GitHub CLI. Each project has its own settings, snapshots, drafts, and schedule.

## Start the dashboard

1. Install Python 3.11 or newer, Git, and the GitHub CLI (`gh`). Run `gh auth login` with an account that can read the repositories you want to manage. Posting, publishing, and merging require the corresponding GitHub permissions.
2. From the dashboard checkout, run `python triage.py serve`. On Windows, you can also run `start-dashboard.cmd`.
3. Open [http://127.0.0.1:8765](http://127.0.0.1:8765) if the browser tab does not open automatically. Use `--port 8766` if the default port is in use. Use `--no-browser` to skip opening a tab.
4. Open **Projects → Add repository**, enter the GitHub repository as `owner/repo`, and select **Save project**. Repeat for each repository. Use the selector at the top of the dashboard to change repositories and **Inbox** to see work across all of them.

Keep the dashboard running for [scheduled triage](scheduled-triage.md). Press Ctrl+C in the terminal when you want to stop it.

## Create your first draft

1. Install and sign in to the `codex` or `claude` CLI on the same computer. In **Projects**, set **Default agent** for the repository and save. Projects assume [Matt Pocock’s engineering skills](https://github.com/mattpocock/skills) are available to the agents that work on them.
2. Select **Check labels** to compare the repository with the baseline triage labels. Use [Label mappings](configuration.md#label-mappings) to adapt the dashboard to your project. Select **Create missing labels** if you want to add the standard labels to GitHub.
3. Open **Work → Issue pipeline** and select **Draft triage** on an eligible issue. For pull requests, use **Work → PR reviews** and select **Review selected** or **Review all waiting**. You can also preview a whole pass with **Projects → Scheduled triage runs** once the project is saved.
4. Open **Work → Runs** or **Inbox**, select **Open draft**, and read or edit the result. Select **Post** when you want to send the draft to GitHub.

See the [workflow guide](workflows.md) for queue eligibility and the other draft types.
