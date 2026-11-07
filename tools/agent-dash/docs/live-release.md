# Live dashboard from a release

The dashboard on 127.0.0.1:8765 should run from a clean release of
`origin/main`, not from the mutable main checkout. This page has the
cutover, the rollback, the loop for shipping a change, and what the
release does and does not isolate.

Paths below: main checkout `~/Documents/projects/workflow-skills`
(the checkout whose gitignored local files the owner edits); state dir
`~/.local/share/agent-dash` (the live one); releases
`~/.local/share/agent-dash/releases/<sha>`.

## What runs from where

| Thing | Runs from | Changed by `deploy`? |
|---|---|---|
| Dashboard server on 8765 | `releases/CURRENT/tools/agent-dash` | yes |
| Workers, labels, hooks, shims (`~/.claude/tools/agent-dash`) | main checkout (symlink target) | no |
| Local overrides `config.local.json`, `workers.local.json`, `map.local.json`, `plans.local.json`, `static/taskmap.local.json` | main checkout, **linked** into each release | links made on deploy and rollback; edits apply at once, no redeploy |
| `phases.local.json`, state-dir `config.local.json`, `project.json` | state dir (read directly) | no |

The release isolates the dashboard server only. Workers keep running
the main checkout's code. Moving the `~/.claude` symlink to follow
`CURRENT` is deliberately not done: the same link feeds worker
launchers and hooks, so an old release would then decide how every
worker is launched. Keep the main checkout at `origin/main` (the lead
moves it), and both paths match after each ship.

`ws` commands printed into worker prompts use the stable
`releases/CURRENT` path when that release is live
(`dash/bundles.py` `_stable_tool_dir`), so those commands run the
release CLI. The worker launcher still runs the main checkout.

## Cutover (already done on the live machine; kept for a rebuild, restarts the live server, needs owner go-ahead)

1. Main checkout at `origin/main`:
   ```bash
   git -C ~/Documents/projects/workflow-skills fetch origin
   git -C ~/Documents/projects/workflow-skills checkout --detach origin/main
   ```
2. Move the stale pointers aside. `releases/CURRENT` names `1b1c53…`
   (Oct 28) and `PREVIOUS` names `133bc39…`; neither is what the live
   process runs, so the first deploy would record them as the release to
   roll back to. `mv` moves the links, not their targets:
   ```bash
   R=~/.local/share/agent-dash/releases
   mv "$R/CURRENT" "$R/CURRENT.stale-20261106" 2>/dev/null
   mv "$R/PREVIOUS" "$R/PREVIOUS.stale-20261106" 2>/dev/null
   ```
3. Deploy from the main checkout:
   ```bash
   cd ~/Documents/projects/workflow-skills/tools/agent-dash
   python3 agent_dash.py deploy --ref origin/main --yes-live
   ```
   Expected last line: `deployed <sha> (build <short>) from .../releases/<sha>`.
4. Verify:
   ```bash
   curl -s http://127.0.0.1:8765/api/health | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ok"], d["build_id"])'
   python3 agent_dash.py status
   python3 agent_dash.py check
   readlink ~/.local/share/agent-dash/releases/CURRENT
   ```
   `build_id` must be the short sha of `origin/main` with no `-dirty`.
   `ok` can be `false` while the doctor reports criticals on real data
   (look at `python3 agent_dash.py doctor`); that is not a build fault.

Why `--yes-live`: the live state dir is the default, and an agent shell
is not a terminal, so `stop`/`restart` refuse to signal the live
dashboard without it (`agent_dash.py` `_refuse_to_signal`). A terminal session
does not need the flag.

## Rollback

```bash
python3 agent_dash.py rollback --yes-live
```

It switches `CURRENT` back to `PREVIOUS`, relinks that release's local
files, restarts it, waits for its build on `/api/health`, and runs
`check`. If the restart or health fails, it restores `CURRENT` and
`PREVIOUS` and reports `kept <current>`.

Caveats, both found while trialling:

- `rollback` needs `--yes-live` from a non-interactive session on the live
  state dir, like `restart` and `deploy`.
- Right after the cutover there is no `PREVIOUS` (step 2 moved the stale
  one away), so `rollback` says `no previous release recorded`. The way
  back to the pre-release state is a restart from the main checkout,
  which runs the mutable checkout again:
  `cd ~/Documents/projects/workflow-skills/tools/agent-dash && python3 agent_dash.py restart --yes-live`.

## Shipping a change afterwards

1. Merge the PR (squash) to `origin/main`.
2. Move the main checkout: `git -C ~/Documents/projects/workflow-skills fetch origin && git -C ~/Documents/projects/workflow-skills checkout --detach origin/main`.
3. Deploy: `cd …/tools/agent-dash && python3 agent_dash.py deploy --ref origin/main --yes-live`.
4. Verify as in the cutover, step 4. Its `build_id` is the new sha.

Deploy runs from the main checkout so that the deployer's code and the
local files are the same checkout the release is built for. The
deployer is the code that gates and restarts; step 2 keeps it current.

### How `deploy` interacts with restart and `--yes-live`

`deploy` runs these in order:

1. Create or reuse `releases/<sha>` (clean detached worktree; refuses a
   dirty or wrong-commit path).
2. Link the local files into the release (`link_local_files`).
3. Reap dead run rows in the live store (`reap_stale_rows`, a write
   before the gate; it is the only deploy step that writes live data).
4. Gate the release: `scripts/gate.sh` (base `origin/main`).
   **A failed gate stops here and restarts nothing.**
5. Set `CURRENT` to the release, then `restart --yes-live` from the
   release: stop the old server by pid, start the release's
   `agent_dash.py serve`.
6. Wait for `/api/health` to report the release build; wait for every
   source to poll once (up to 60 s); run the release's `check`.
7. On failure after step 5, restart the previous release and say so.
   Set `PREVIOUS` to the old release on success.

`restart` alone does not know about releases. Run from the main checkout
it starts the main checkout's code, so after a cutover a plain
`restart` silently moves the live server back to the mutable checkout.
To change live code, use `deploy`; use `restart` only to bounce the
same release.

## Trials (never the live port)

```bash
T=$(mktemp -d /tmp/agd-trial-XXXX)     # short path: see ControlPath note
P=<free port>
AGENT_DASH_STATE_DIR=$T AGENT_DASH_PORT=$P \
AGENT_DASH_LOCAL_SOURCE=~/Documents/projects/workflow-skills \
  python3 agent_dash.py deploy --ref origin/main
```

- `AGENT_DASH_LOCAL_SOURCE` names the checkout whose local files get
  linked. A deploy run from a worktree has none of them, so set it to
  the main checkout, or the release reads the example files.
- Use a short state dir (`/tmp/…`). The ssh control socket lives under
  the state dir, and macOS limits its path to 104 bytes. A deep scratch
  path made the dagster source fail with `ControlPath too long`.
- Stop a trial server by its pid from `<state>/agent-dash.pid`, after
  checking the command line names the trial dir. Remove each release
  with `git worktree remove <path>` (never `--force`).

## Gate behaviour to know

The deploy gate runs `scripts/gate.sh` with base `origin/main`, so it
gates what differs from `origin/main`. A release of `origin/main` itself
gates only the always-on files (page rules, privacy pin, privacy audit).
Deploying an older commit gates everything changed since `origin/main`:
36 suites on `bd5a65c`. Use `rollback` to return to an older release
rather than deploying an old commit. A better base (the running release)
is a follow-up.

## Known limits (not fixed here)

- The gate base is `origin/main`, not the running release (above).
- `dash/doctor.py` `_local_config_path` writes `config.local.json` in
  the tool dir. In a release with a linked source that writes through
  to the main checkout's file. With no source file it creates a
  release-local file, lost when the release is removed.
- The `~/.claude/tools/agent-dash` symlink and the hooks in
  `~/.claude/settings.json` still point at the main checkout (by design
  here).
