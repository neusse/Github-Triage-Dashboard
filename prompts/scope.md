Draft a scope recommendation for issue #{number} in {repo}, "{title}".
Read `{ctx}/issue.md`, `{ctx}/labels.txt`, and relevant code on `{base}` at `{main}`.
Project policy files in the checkout: {policy_files}. Current labels: {labels}.

Explain the problem supported by evidence, the smallest useful change, and what remains outside the proposed scope.
Mention any decision the maintainer needs to make. Keep the checkout read-only and offline.
Configured status roles and GitHub labels: {status_mapping}. Recommend the exact configured label if appropriate.

Output exactly:

===COMMENT===
<scope recommendation>
===END===
RECOMMEND_STATUS: <one configured GitHub status label, or none>
SUMMARY: <one line>
