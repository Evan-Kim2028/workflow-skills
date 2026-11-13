Scope: the module {module} of the repo {repo}, checked out at {workspace}. This is a read-only review (kind: docs) of one module or file and the documentation that describes it: docstrings, README files, runbooks under docs/, and AGENTS.md or CLAUDE.md text that names it. Do not edit, commit, push or open PRs, and do not use ssh. The module last changed at commit {commit}.

Steps:
1. Read the code under {workspace}/{module}, then the docstrings and docs that describe it. Find the documentation by searching the repo for the module's names and file paths.
2. For each documented behaviour, check it against the code: a function that changed its arguments or result, a flag or config key that is gone, a command that no longer exists, a step that is out of order, a claim about a limit or default that the code no longer uses.
3. For code lookups prefer the CodeGraph CLI: `codegraph explore "<question>"`. If the repo has no CodeGraph index, search with grep.
4. Report each mismatch as one bullet, exactly in this form: `- **medium** \`path:line\` — one-line title`, where path:line is the documentation line, with severity high, medium or low by how wrong a reader would be misled. Under each bullet quote the doc line, quote the code that disagrees, and give the corrected sentence.
5. Write the report to {report_path}. Make the folder if it is missing. Start the file with the repo, module, commit {commit} and the UTC time (`date -u`). Your report file is the only file you may write. If the docs match the code, say so in one line.
6. Aim to finish in under 10 minutes. Stop early rather than run long.
7. End the report with `VERDICT: PASS` when the whole module and its docs were read, or `VERDICT: PARTIAL` followed by what was not read.
