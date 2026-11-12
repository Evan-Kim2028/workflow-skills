Scope: the repo at {workspace}. Review the commits merged to origin/main in the last 6 hours. This job is read-only: do not edit, commit, push, open PRs or change any file in the repo. Fetching is allowed.

Steps:
1. Run `git -C {workspace} fetch -q origin`, then `git -C {workspace} log --since='6 hours ago' --format='%H %s' origin/main`.
2. For each commit, read its diff with `git -C {workspace} show <sha>`. Look for: correctness bugs; changed behaviour with no test; risky SQL on large tables (flag unbounded scans, per-row functions, and OR across unindexed columns); docs the change made wrong.
3. Use the CodeGraph CLI for context, not grep: `codegraph explore "<question>"`, `codegraph callers <symbol>`, `codegraph impact <symbol>`.
4. Write the report to {report_dir}/merged-review-lor-<UTC timestamp>.md. Make the folder if it is missing; build the timestamp with `date -u +%Y%m%dT%H%MZ`. Use one section per finding with: commit, file:line, severity (high, medium or low), evidence (the lines you read), and a suggested fix. If nothing was merged in the window, write a one-line report that says so.
5. End the report with the final line `VERDICT: PASS` when every commit in the window was read. Use `VERDICT: PARTIAL` if any commit could not be read, and say which.
