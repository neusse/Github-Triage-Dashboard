Draft a design proposal for issue #{number} in {repo}, "{title}".
Read `{ctx}/issue.md` and the relevant code on `{base}` at `{main}`.
Project policy files in the checkout: {policy_files}. Current labels: {labels}.

Explain demonstrated behavior, the smallest viable approach, and affected code. Include acceptance criteria, relevant edge cases,
compatibility and migration impact, and a verification plan. Cite files or code at `https://github.com/{repo}/blob/{main}/<path>#L<line>`.
Mark what remains uncertain. Keep the checkout read-only and offline. The maintainer will review the proposal.

Output exactly:

===COMMENT===
Design proposal for #{number}

Based on `{base}` at `{main}`.

<proposal>
===END===
SUMMARY: <one line>
