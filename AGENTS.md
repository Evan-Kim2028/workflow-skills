# workflow-skills

Agent instructions for this repo. Short on purpose; the skills carry the detail.

## Tests: scoped only

- Run only the test files for the code you changed: `python3 -m pytest tools/agent-dash/tests/<file>.py` and `node tools/agent-dash/scripts/<check>.mjs`.
- Never run the whole `tests/` directory, `pytest` with no path, or every check script.
- The repo has no GitHub Actions; do not add a workflow that runs whole suites.
- Known pre-existing failure on main: `tests/test_page_rules.py::FinalPolishTests::test_plan_sections_term_rule` (leave it, add no new failures).
- The pre-push hook runs `scripts/privacy_audit.sh` only.

## Worktrees: one per task, removed when it finishes

- Create from origin/main as `~/Documents/projects/workflow-skills-wt-<slug>` on branch `<type>/<slug>`.
- Never edit the main checkout: the live install (`~/.claude/tools/agent-dash`) symlinks into it.
- Start temp dashboard servers on ports other than 8765 with a temp state dir; stop each by PID before you finish (never `pkill -f`).
- When the PR merges or the task is abandoned, remove the worktree and its local branch (`git worktree remove <path>`, `git branch -D <branch>`, then `git worktree prune`) the same session, and end with `git worktree list` showing only the main checkout and the dashboard's own release worktrees under `~/.local/share/agent-dash/releases/`.

## Shipping

- Squash-merge PRs; after a merge the lead moves the main checkout to the new origin/main (`git checkout --detach origin/main`).
- Static JS/CSS changes are live on reload (served no-cache); Python changes need the `agent_dash.py serve` process restarted.
