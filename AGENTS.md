# workflow-skills

Agent instructions for this repo. Short on purpose; the skills carry the detail.

## Tests: scoped only

- Run only the test files for the code you changed: `python3 -m pytest tools/agent-dash/tests/<file>.py` and `node tools/agent-dash/scripts/<check>.mjs`.
- Never run the whole `tests/` directory, `pytest` with no path, or every check script.
- The repo has no GitHub Actions; do not add a workflow that runs whole suites.
- The pre-push hook runs `scripts/privacy_audit.sh` only.

## Worktrees: one per task, removed when it finishes

- Create from origin/main at `~/Documents/worktrees/<repo>/<task>` (here: `~/Documents/worktrees/workflow-skills/<task>`) on branch `<type>/<slug>`.
- Never edit the main checkout: the live install (`~/.claude/tools/agent-dash`) symlinks into it.
- Start temp dashboard servers on ports other than 8765 with a temp state dir; stop each by PID before you finish (never `pkill -f`).
- When the PR merges or the task is abandoned, remove the worktree and its local branch (`git worktree remove <path>`, `git branch -D <branch>`, then `git worktree prune`) the same session, and end with `git worktree list` showing only the main checkout and the dashboard's own release worktrees under `~/.local/share/agent-dash/releases/`.

## Worker prompts, scratch, logs

- A worker prompt holds only the task. `scripts/run_worker.py` adds the `[label: <label>]` line and `templates/worker-common.md` (the worker rules) before launch, and does not add either twice. Launch rules: [docs/workstream-lead.md](tools/agent-dash/docs/workstream-lead.md#prompts). Local memcap rules: [docs/install.md](docs/install.md#local-memcap-rules).
- Scratch: each worker gets `$AGENT_SCRATCH`, which is disposable. `agent_dash.py scratch status` shows what is held, `scratch gc` is a dry run until `--apply`, and `ws archive <ID>` removes that workstream's scratch unless `--keep-scratch`. See [docs/dispatcher.md](tools/agent-dash/docs/dispatcher.md).
- Logs: launchers strip base64 images from their streams. `agent_dash.py logs archive` is a dry run until `--apply` and moves finished streams to zstd copies. It needs `log_archive.roots` in `<state>/config.local.json`, and the search store comes from `scripts/vlogs_install.sh`. The page's log search card reads it. See [docs/log-archive.md](tools/agent-dash/docs/log-archive.md).

## Shipping

- Squash-merge the PR. Then ship in three steps: move the main checkout (`git checkout --detach origin/main`), deploy from `tools/agent-dash` (`python3 agent_dash.py deploy --ref origin/main --yes-live`), and verify (`status`, `check`, and `/api/health`: `build_id` is the new short sha with no `-dirty`). Full runbook: [docs/live-release.md](tools/agent-dash/docs/live-release.md).
- The live dashboard on 127.0.0.1:8765 runs from a clean release under `~/.local/share/agent-dash/releases/`, not from the mutable checkout. `restart` from the main checkout moves it back to the checkout. Use `deploy` to change live code and `restart` only to bounce the same release.
- Static JS/CSS is served no-cache, so a deployed change shows on reload. Python changes need a deploy.
- Trial servers use another port and a temp state dir, and are stopped by PID.
