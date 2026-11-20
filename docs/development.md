# Development and CLI reference

The dashboard process listens on loopback and manages local repository profiles and workers. Each saved repository gets a separate loopback worker when needed. `dashboard.html` and its JavaScript modules call the local API. Workers collect GitHub data using `gh` and use local Git checkouts for agent jobs.

## Standalone CLI

From the repo root, set `TRIAGE_REPO` to the GitHub repo you want to inspect. In PowerShell, run:

```powershell
$env:TRIAGE_REPO = 'owner/repo'
python triage.py collect
```

In Bash or Zsh, use `export TRIAGE_REPO=owner/repo` before running the commands.

| Command | Behavior |
| --- | --- |
| `python triage.py serve --port 8766 --no-browser` | Serve the dashboard on `127.0.0.1:8766` without opening a tab. The default port is `8765`. |
| `python triage.py collect` | Read GitHub and save a local snapshot. |
| `python triage.py diff [base]` | Compare the latest snapshot with its predecessor, or with the named snapshot file. |
| `python triage.py review --pr 42` | Prepare a local review draft. Repeat `--pr` to select multiple PRs. |
| `python triage.py review --all-waiting` | Prepare local review drafts for PRs waiting on a review of their current head. |

The standalone CLI reads `config.json` by default. Set `TRIAGE_PROJECT_CONFIG` to a saved profile path to use its complete configuration. `TRIAGE_REPO` takes precedence over the configured `repo`. If they differ, only the agent choice carries over from the file.

The dashboard imports an existing `config.json` into a saved profile on startup when there is no saved profile for that repository. Use **Projects** for routine edits afterward. On Windows, `ensure-dashboard.ps1 -Port 8766` can start the dashboard in the background when nothing is listening on that port.

## Local files

| Location | Contents |
| --- | --- |
| `.triage-dashboard/owner-<owner>/repo-<name>/` | `profile.json` and `worker.log` for one repository. |
| `snapshots/owner-<owner>/repo-<name>/` | The latest snapshot and up to 300 timestamped snapshots. |
| `work/owner-<owner>/repo-<name>/` | Git checkout, schedule history, and job artifacts. |
| `work/owner-<owner>/repo-<name>/jobs/<job-id>/` | Job metadata, draft files, agent logs, and applicable validation logs. |
| `dashboard.log` and `dashboard.err.log` | Server output when started using `ensure-dashboard.ps1`. |

## Troubleshoot

| Symptom | Check |
| --- | --- |
| GitHub snapshot fails or is stale | Check `gh auth status`, repository access, and the repository worker log. Then refresh the repository. |
| Draft job fails | Read the result in **Work → Runs** and the job’s `agent.log`. Verify that the project’s selected agent is installed and signed in from the server’s environment. |
| Implementation checks fail | Read the job’s `gate.log`. Confirm the commands from **Projects → Advanced settings** are installed and runnable in the target worktree. |
| Dashboard cannot bind its port | Use `--port` to select a free loopback port. |
| Schedule shows a missed slot | Check that the dashboard was running during the slot and check **Projects → Scheduled triage runs**. Use a manual pass to collect a fresh set of drafts. |

## Run tests

From the repo root, run the Python suite and the workflow editor tests. The latter requires Node.js.

```sh
python -m unittest discover -s tests
node --test tests/test_workflow_editor.js
```
