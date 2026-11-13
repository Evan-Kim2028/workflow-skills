Scope: the merged pull request #{pr_number} of the repo {repo} ("{pr_title}", {pr_url}). This is a read-only review of what the merge introduced. Do not edit, commit, push, approve, comment on or merge anything, and do not use ssh. The merge commit is {commit}.

Steps:
1. Read the diff: `gh pr diff {pr_number} --repo {pr_slug}`, or `git -C {workspace} show {commit}` when that fails. Read the changed files around each hunk, not only the hunk.
2. Look for defects the change introduced: wrong results, unhandled errors, state that can drift, races, a changed default or contract that callers still rely on, and removed checks.
3. Check whether the PR's own tests cover those defects. Find the test files it changed or added, and say which new behaviour has no test.
4. For code lookups prefer the CodeGraph CLI: `codegraph callers <symbol>`, `codegraph impact <symbol>`. If the repo has no CodeGraph index, search with grep.
5. Report each finding as one bullet, exactly in this form: `- **high** \`path:line\` — one-line title`, with severity high, medium or low. Under each bullet give the lines you read and a suggested fix. Drop any point you cannot back with a line you read.
6. Write the report to {report_path}. Make the folder if it is missing. Start the file with the repo, PR number, merge commit {commit} and the UTC time (`date -u`). Your report file is the only file you may write. If the change is clean, say so in one line.
7. Aim to finish in under 10 minutes. Stop early rather than run long.
8. End the report with `VERDICT: PASS` when the whole diff was read, or `VERDICT: PARTIAL` followed by what was not read.
