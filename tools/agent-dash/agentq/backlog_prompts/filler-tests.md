Scope: the module {module} of the repo {repo}, checked out at {workspace}. This is a read-only review (kind: tests) of one module or file. Do not write, edit or commit test code, do not run tests or pytest, do not edit, push or open PRs, and do not use ssh. The module last changed at commit {commit}.

Steps:
1. Read the code under {workspace}/{module} and find its risky paths: error handling, state changes, money or data writes, concurrency, parsing of external input, and any branch a change could silently break.
2. Find the tests that exercise the module (search the repo's test folders for its names). Decide which risky paths no test covers.
3. For code lookups prefer the CodeGraph CLI: `codegraph callers <symbol>`, `codegraph explore "<question>"`. If the repo has no CodeGraph index, search with grep.
4. Report each gap as one bullet, exactly in this form: `- **high** \`path:line\` — one-line title`, with severity high, medium or low by the damage an uncovered break would do. Under each bullet describe the concrete test cases that should exist: the input, the expected result, and the existing test file it belongs in. Do not write the test code itself.
5. Write the report to {report_path}. Make the folder if it is missing. Start the file with the repo, module, commit {commit} and the UTC time (`date -u`). Your report file is the only file you may write. If every risky path is covered, say so in one line.
6. Aim to finish in under 10 minutes. Stop early rather than run long.
7. End the report with `VERDICT: PASS` when the whole module was read, or `VERDICT: PARTIAL` followed by what was not read.
