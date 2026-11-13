Scope: the module {module} of the repo {repo}, checked out at {workspace}. This is a read-only audit (kind: bugs) of one module or file. Do not edit, commit, push, open PRs, run tests or pytest, or use ssh. The module last changed at commit {commit}.

Steps:
1. Read the files under {workspace}/{module}. Use `git -C {workspace} show <sha>` or `git -C {workspace} log -p -- {module}` only when recent history explains a suspicious line.
2. Look for correctness defects: wrong results, unhandled errors, races, state that can drift, and dead code (defined or exported and never called from inside the repo).
3. For code lookups prefer the CodeGraph CLI: `codegraph explore "<question>"`, `codegraph callers <symbol>`. If the repo has no CodeGraph index, search with grep.
4. Report each defect as one line in the form `- **high** \`path:line\` — title — why: one sentence`, with severity high, medium or low by the damage it could do. Drop any point you cannot back with a line you read. Limits: report at most 10 findings, most severe first. Keep the whole report under 60 lines. No code block longer than 5 lines, and do not restate the diff or the code you read: cite lines by number.
5. Write the report to {report_path}. Make the folder if it is missing. Start the file with the repo, module, commit {commit} and the UTC time (`date -u`). Your report file is the only file you may write. If nothing is wrong, say so in one line.
6. Aim to finish in under 10 minutes. Stop early rather than run long.
7. End the report with `VERDICT: PASS` when the whole module was read, or `VERDICT: PARTIAL` followed by what was not read.
