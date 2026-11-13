Scope: the module {module} of the repo {repo}, checked out at {workspace}. This is a read-only risk review (kind: risk) of one module or file: security issues and expensive patterns. Do not edit, commit, push or open PRs, do not run queries or commands against production, and do not use ssh. The module last changed at commit {commit}.

Steps:
1. Read the code under {workspace}/{module}. Look for: injection (SQL, shell, path traversal), secrets or credentials in code or logs, unsafe deserialisation, missing authentication or authorisation on a route, and data written without validation.
2. Look for expensive patterns: SQL that scans a table above one million rows without a bounding predicate or index, a per-row function in a large scan, a loop with no bound or no progress check, a network or subprocess call with no timeout, and retries with no cap.
3. For code lookups prefer the CodeGraph CLI: `codegraph callers <symbol>`, `codegraph impact <symbol>`. If the repo has no CodeGraph index, search with grep. Do not run EXPLAIN or any query against a live database.
4. Report each finding as one bullet, exactly in this form: `- **high** \`path:line\` — one-line title`, with severity high, medium or low by the damage it could do. Under each bullet give the lines you read, the input or condition that triggers it, and a suggested fix. Drop any point you cannot back with a line you read.
5. Write the report to {report_path}. Make the folder if it is missing. Start the file with the repo, module, commit {commit} and the UTC time (`date -u`). Your report file is the only file you may write. If nothing is risky, say so in one line.
6. Aim to finish in under 10 minutes. Stop early rather than run long.
7. End the report with `VERDICT: PASS` when the whole module was read, or `VERDICT: PARTIAL` followed by what was not read.
