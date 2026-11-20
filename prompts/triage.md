Draft triage for issue #{number} in {repo}, "{title}". Use GitHub issue text as context.
Read `{ctx}/issue.md`, `{ctx}/linked-work.json`, the existing labels and descriptions in `{ctx}/labels.txt`, and relevant project code.
Base branch: `{base}`. Base commit: `{main}`. Project policy files in the checkout: {policy_files}.

Current labels: {labels}. Configured status roles and labels: {status_mapping}.
Only propose existing labels supported by the issue. Preserve correct labels and unrelated labels. Propose at most one status change.
Distinguish defects, contract gaps, features, maintenance, FYIs, and settled policy. Ask only for new evidence and name the next actor.
Leave workflow-controlled special labels alone: {protected_labels}. If no vocabulary fits, recommend no label change.
Show what you verified, what is missing, and the next action. Cite code at `https://github.com/{repo}/blob/{main}/<path>#L<line>` when relevant.
Keep the checkout read-only and offline. The maintainer decides what to post.

Output exactly:

===COMMENT===
<triage comment>
===END===
LABELS_ADD: <comma-separated GitHub label names, or none>
LABELS_REMOVE: <comma-separated GitHub label names, or none>
SUMMARY: <one line>
