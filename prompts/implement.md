Implement the approved design for issue #{number} in {repo}, "{title}", in this local worktree.
Read `{ctx}/issue.md`, `{ctx}/design.md`, and `{ctx}/fixtures.md` if present. The base is `{base}` at `{main}`.
Read these project policy files in the checkout: {policy_files}. Current labels: {labels}.

Make the smallest change that satisfies the approved design, with appropriate tests and documentation.
Run available project checks and report which ones passed, failed, or could not be run. The dashboard runs these configured commands independently:
{gate_commands}

{commit_instructions}
Keep the title concise. Use the project's commit and PR conventions when available. Do not push, fetch, merge, rebase, switch branches, or edit Git config.

Use `{ctx}/pr-template.md` if present. Otherwise explain the change, test results, and any limitations.
Include `Closes #{number}` when the change fully resolves the issue, or `Part of #{number}` for an approved partial implementation.
If the design cannot be implemented as approved, leave the worktree unchanged with no new commits. Leave the PR body and TITLE empty and explain why in SUMMARY.

Output exactly:

===PR===
<PR body>
===END===
TITLE: <PR title>
SUMMARY: <one line>
