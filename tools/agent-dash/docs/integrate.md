# Plug in any agent

The dashboard shows agent runs, tokens, cost, and plan progress. Any harness can feed it. The two paths are the `report` command (three lines per run) and the manifest files (one TSV row plus report files). Detail lives in [CONTRACT.md](../CONTRACT.md).

## Fresh agent checklist

Ten commands, verified end to end. Run from the checkout root. They touch
only `/tmp/dash-try` (state, bundles) and port 8815 — never the live
dashboard, never your sessions. `stop` at the end frees the port.

1. `./install.sh`
2. `mkdir -p /tmp/dash-try/state /tmp/dash-try/ws && export AGENT_DASH_STATE_DIR=/tmp/dash-try/state AGENT_DASH_WORKSTREAMS=/tmp/dash-try/ws AGENT_DASH_PORT=8815`
3. `python3 tools/agent-dash/agent_dash.py run` — expect `running`, a pid, `url http://127.0.0.1:8815/`
4. `curl -s http://127.0.0.1:8815/api/health` — expect `"ok": true`
5. `python3 tools/agent-dash/agent_dash.py doctor` — expect `no problems found` (on a shared machine, warnings about other agents' workers are theirs, not yours)
6. `python3 tools/agent-dash/agent_dash.py ws new WS9 "Probe"` — expect `created` and a path ending in `WS9`
7. `python3 tools/agent-dash/agent_dash.py report start --label ws9-WS9.1-probe --model claude-haiku-5-5 --session s1` — expect `reported start`
8. `python3 tools/agent-dash/agent_dash.py report usage --label ws9-WS9.1-probe --model claude-haiku-5-5 --session s1 --input 100 --output 50 --message-id turn-1` — expect `reported usage`
9. `python3 tools/agent-dash/agent_dash.py report finish --label ws9-WS9.1-probe --verdict PASS --session s1` — expect `reported finish`
10. `sleep 4 && python3 tools/agent-dash/agent_dash.py check` — expect `no mismatches`

Then `python3 tools/agent-dash/agent_dash.py stop` and delete `/tmp/dash-try`.
Any model id from `tools/agent-dash/catalog/prices.json` works in steps
7-8; an id outside the catalog reports fine but stays unpriced (step 10
names it). The label in step 7 must hold a task id from step 6's plan
(the scaffold already has `WS9.1`).

## For agents

Two commands tell you everything. Run them first, in this order:

```bash
python3 tools/agent-dash/agent_dash.py doctor   # what's broken, and the fix for each
python3 tools/agent-dash/agent_dash.py state    # the whole dashboard in one call
```

`state` reads `GET /api/state`: version, window, health per source,
workstreams with progress and tokens, the unassigned row, the running
count by source (plus the compact `prod_jobs` / `prod_jobs_finished`
numbers), the reconcile verdict, and a `problems` list where
every entry names the exact next command. Production jobs (source
`dagster`) are never in agent lists: `/api/snapshot` running/recent and
`/api/runs` hold no dagster rows, and `?include=jobs` adds them back on
`/api/runs` when a tool needs them. The server runs the doctor
checks in a background thread every `doctor.interval_s` seconds
(default 300) and persists them; `GET /api/doctor` serves the latest
run (never inline; `?refresh=1` queues one), with `?ws=<ID>` and
`?severity=critical|warn|info` filters. `doctor` reads that endpoint
when the server is up and otherwise runs the same checks locally.
`doctor` exits 0 when clean, 1 when problems found. The served run is at
most `doctor.interval_s` seconds old (default 300): right after a change,
`check` sees it first and `doctor` catches up on the next background run;
the header carries the generation epoch. `doctor --fix` is a
dry run of the safe automatic fixes; `doctor --fix --yes` applies only
safe, local, reversible ones (register a run dir, add a missing scratch
root to `config.local.json` with a backup, prune, restart a stalled
dashboard, reprice and backfill models). `doctor report --since 24h
[--md]` digests history (new, regressed, resolved, open by severity
and workstream) for hub reports. Every problem code has a page under
`docs/problems/` (for example `docs/problems/stuck-task.md`) with what
it means, the exact fix commands, and how to verify. The CLI prints the
fix command, not the page path: when the fix command only re-diagnoses
(such as `check` for an unseen worker), open that code's page.

These commands take `--json` for machine output on stdout only (human
text moves to stderr when `--json` is set): `check`, `check --page`
(summary), `state`, `status`, `doctor`, `ws validate`, `ws approvals`,
`ws trains`, `ws plan <ID>`, `ws layout <ID>`. The schema only gains fields, never
renames them.

Exit codes are the same in `agent_dash.py` and every `ws` subcommand:

| Code | Meaning | Example next step |
|---|---|---|
| 0 | ok | — |
| 1 | check failed (mismatches found, or doctor found problems) | `python3 agent_dash.py check` |
| 2 | usage error (bad flags or values) | re-run with a listed value |
| 3 | not found (workstream, task, train member, approval, bundle) | `python3 agent_dash.py ws list` |
| 4 | invalid data (validation failed) | `python3 agent_dash.py ws validate <ID>` |
| 5 | dashboard unreachable | `python3 agent_dash.py run` |

Every error message says what was wrong, the valid choices when the
set is small, and the next command. `stop`/`status` keep their older
contract: they print `stopped` (plus the `run` command) and exit 1
when no live dashboard answers.

## 1. Install and run

Three commands, from any checkout of this repo:

```bash
./install.sh
python3 tools/agent-dash/agent_dash.py run
curl -s http://127.0.0.1:8765/api/health
```

`install.sh` symlinks the skills and the tool into `~/.claude/`; repeat runs are safe. Skip it when your harness reads straight from the checkout: every command below takes a repo path, and the dashboard needs no `~/.claude` entry. `run` prints `running`, a pid, and the URL. The address is fixed at `127.0.0.1:8765`; no config moves it.

Verify:

```bash
curl -s http://127.0.0.1:8765/api/health
python3 tools/agent-dash/agent_dash.py check --hours 1
python3 tools/agent-dash/agent_dash.py check --page
```

Health prints `"ok": true` with one entry per source, each `last_error: null`. `check` prints one table row per workstream and exits 1 on any mismatch. `check --page` drives the live page in headless Chrome and compares every tile against the API; it skips with a clear message when Chrome is missing. Restart with `python3 tools/agent-dash/agent_dash.py restart`: it stops (SIGTERM, up to 10 s of polling, SIGKILL only for the pidfile pid), starts again in the background, and returns 0 once `/api/health` answers. `stop` alone waits the same way and returns 0 only once the port is free, so an immediate `run` can bind.

### Process control and the state-dir guard

Never run stop or restart in tests without AGENT_DASH_STATE_DIR and AGENT_DASH_PORT.

`stop`, `restart`, and `deploy` never signal a dashboard from another state dir. Every server serves a `state_hash` on `/api/health` (a short hash of its state dir, never the path); before acting, the CLI compares it with the current state dir and refuses unless `--yes-live` is passed, printing which pid on which port it would have signaled:

```bash
python3 tools/agent-dash/agent_dash.py stop
# stop refused: dashboard pid 1234 on 127.0.0.1:8765 belongs to another state dir (state abc..., not def...). ...
python3 tools/agent-dash/agent_dash.py stop --yes-live
# stopping dashboard pid 1234 on port 8765 (state abc..., build cafef00d)
# stopped
```

The default state dir (no `AGENT_DASH_STATE_DIR`) is the live one, so a test or worker that sets it can never take down the live dashboard — and a non-interactive call without it must also pass `--yes-live`. The pidfile is only ever removed by its owner: an exiting server, `stop`, and `restart` erase it only when it still names their own pid, so a restart racing a start can no longer orphan the new server. `status` finds a running dashboard even with a missing or stale pidfile (`running (pidfile missing, pid N)`); `status --repair` records that pid in the pidfile, and only in that case.

State lives in `~/.local/share/agent-dash/` (`dash.sqlite`, pidfile, log, `generic.jsonl`). Point `AGENT_DASH_STATE_DIR` at an empty directory to trial the dashboard without touching live state. Point `AGENT_DASH_WORKSTREAMS` at another directory to trial bundles. Point `AGENT_DASH_PORT` at a free port (such as 8815) to trial beside a live dashboard: every command (`run`, `status`, `stop`, `check`, `state`, `doctor`, and the `ws` live reads) follows it. Set `AGENT_DASH_URL` explicitly only to point the `ws` live reads at yet another dashboard. Config-file `host`/`port` stay ignored.

### Data root and project name
One directory owns every data path (`dash/dataroot.py` resolves all of
them, precedence environment, then config, then the default): the project
descriptor (`project.json`), the bundles (`workstreams/`), the sqlite
state, the run-folder registry, and the hub checkout. The default data
root is today's `~/.local/share/agent-dash`, so nothing moves. Point
`AGENT_DASH_DATA_ROOT` at another local path — or a mounted/synced cloud
folder — to move everything at once; the narrower overrides
(`AGENT_DASH_STATE_DIR`, `AGENT_DASH_WORKSTREAMS`, `AGENT_DASH_HUB_DIR`,
config `data_root`, `state_dir`, `hub_dir`) keep working. The hub is read
directly from the data-root `hub` directory when it exists (no sync
command, no mirror). Real cloud clients are future work; the seam is the
tiny `Storage` protocol in `dash/dataroot.py` (local filesystem only).
No project data is ever committed to the dashboard repo.
Name the project once; the page header and `<title>` follow it:
```bash
python3 tools/agent-dash/agent_dash.py project init "Demo project"
python3 tools/agent-dash/agent_dash.py project show
python3 tools/agent-dash/agent_dash.py project set description "What ships."
```
`project.json` holds `name`, `short`, `description`, `goals`, `started`,
`target`, `phases` (each with `id`, `title`, `summary`, and a `gate` of
member workstream ids or gate text), `links`, and `owner_notes`.
`GET /api/project` serves the file plus the computed roll-up: per-phase
`state` (from the member workstreams, never stored), lifetime
`totals_all_time`, `counts` by workstream state, and `data_root_hash`
(the digest, never the path). A missing file is fine: the name reads
null and the header falls back to "Agent dashboard".
### Data root and hub
All project data is local. The dashboard repo is app code only: bundles live under `AGENT_DASH_WORKSTREAMS`, and the project decisions log lives in the hub directory (`AGENT_DASH_HUB_DIR`, the same knob the hub issue source reads). The dashboard reads two files there and never writes them:
- `decisions.tsv` — the decision store. `GET /api/decisions` serves it newest first with counts by status.
- `risks.tsv` or `risks.json` (optional) — lead-written risks, appended after the computed ones on `GET /api/risks`.
Both parse header-driven and tolerant: column names may vary (`decision`/`title`/`text`, `ws`/`workstream`, `state`/`status`), unknown columns survive as short extras, and a missing file means an empty list. Only short fields are served: any cell over 400 chars and any local-file-path cell never reach a payload (only basenames would be safe, so paths are dropped outright). A computed risk needs no file: stale plan gates, pending approvals past the doctor `approval_days` window, critical/warn doctor groups on active workstreams, and planned workstreams blocked by an unfinished dependency. `/api/state` carries the roll-up (`decisions: {total, open}`, `risks: {total, by_severity}`).
```text
date	id	decision	status	owner
2026-10-14	D-101	one public id for cards	accepted	ann
2026-10-15	D-102	freeze the route list	accepted	ben
2026-10-16	D-103	retire the old deal routes	proposed	ann
2026-10-17	D-104	single metering point	proposed	ben
```

### Running several projects

One dashboard instance serves one project: its own data root, state, port, and name.
The registry is projects.json (AGENT_DASH_PROJECTS overrides; default ~/.config/agent-dash/projects.json).
python3 tools/agent-dash/agent_dash.py project add demo --data-root /tmp/demo-data --init
python3 tools/agent-dash/agent_dash.py project add second --data-root /tmp/second-data
python3 tools/agent-dash/agent_dash.py project list
python3 tools/agent-dash/agent_dash.py project launch demo
python3 tools/agent-dash/agent_dash.py project launch second
python3 tools/agent-dash/agent_dash.py --project demo state
python3 tools/agent-dash/agent_dash.py project stop second
python3 tools/agent-dash/agent_dash.py project remove second

### Safe deploy (run the live dashboard from a clean release)

Every server captures a `build_id` (git short sha plus `-dirty` when the checkout had uncommitted changes at start) and its start time, served on `/api/health` (`build_id`, `started_at`, plus `state_hash`, a short hash of the server's state dir that the state-dir guard compares) and `/api/state` (`version` block) and printed by `status`. `run` refuses to start when the port already answers with a different build, naming both builds and the fix:

```bash
python3 tools/agent-dash/agent_dash.py run
# run refused: 127.0.0.1:8765 already serves build abc1234, this checkout is build def5678-dirty. ...
python3 tools/agent-dash/agent_dash.py run --force-replace  # restart it instead
```

Ship a clean release instead of running a dirty checkout:

```bash
git fetch origin  # deploy resolves --ref as-is; fetch first when the latest commit is wanted
python3 tools/agent-dash/agent_dash.py deploy [--ref origin/main] [--worktree DIR]
python3 tools/agent-dash/agent_dash.py rollback  # back to the previous release
```

`deploy` makes (or reuses) a clean detached worktree of the ref under `<state-dir>/releases/<sha>` (pass `--worktree DIR` to choose the path), records the previous release in `releases/CURRENT` and `releases/PREVIOUS` symlinks, gates the new release (`scripts/gate.sh` when present, else the page-rules and server test files), restarts from it with a health gate (`/api/health` must report the release build, then `check` must pass), and on any failure restarts the previous release automatically and exits 1 with the reason. Both commands only read the source repo and never touch another session's checkout. The doctor warns on a dirty running build (`build-dirty`, see `docs/problems/build-dirty.md`) and advises when the running build is older than `origin/main` (`build-behind`, see `docs/problems/build-behind.md`).

A release checkout is clean, so its own `config.local.json` never holds your settings: keep them in `<state-dir>/config.local.json` (read after the checkout copy, so a release runs unchanged) or point `AGENT_DASH_CONFIG` at a file (replaces the search when set). The other layered files keep their env overrides (`AGENT_DASH_MAP`, `AGENT_DASH_PLANS`, `AGENT_DASH_TASKMAP`, `AGENT_DASH_WORKERS`, `AGENT_DASH_WORKSTREAMS`). Point `AGENT_DASH_PORT` at another port to trial a release without touching the live 127.0.0.1:8765 dashboard.

## 2. Make your sub-agents show up

| Harness | Dashboard reads | You write | Minimum |
|---|---|---|---|
| Claude Code | Transcripts under `~/.claude/projects`, registry under `~/.claude/sessions` | Nothing; label each run | First prompt line `[label: ws4-WS4.1-slug]` |
| muse | Sessions under `~/.local/share/muse/sessions`, `muse-sessions.tsv` on a scratch root | One TSV row per run | `label`, session uuid, start epoch |
| cursor-agent / Grok | Chats under `~/.cursor`, `cursor-sessions.tsv` on a scratch root, `cursor-usage.jsonl` in state dir | TSV row plus usage lines | Same TSV row with the model fourth; hook appends usage |
| Codex or any other CLI | `generic.jsonl` in the state dir | Three `report` lines per run | Label, verdict, input/output counts |
| Custom Python or shell | Same `generic.jsonl`, or a TSV row plus `out-<label>.md` on a scratch root | Same as above | Same as above |
| Remote or cloud agent | Same `generic.jsonl`, written by a forwarder on this machine | Same three lines, sent home first | Same as above |

The universal path is `report`. One line when the run starts, one with token counts, one when it ends:

```bash
DASH=tools/agent-dash/agent_dash.py
python3 $DASH report start --label ws4-WS4.1-notes-index --model demo-model --session abc
python3 $DASH report usage --label ws4-WS4.1-notes-index --model demo-model --session abc --input 100 --output 50 --message-id turn-1
python3 $DASH report finish --label ws4-WS4.1-notes-index --verdict PASS --session abc
```

That is the whole minimum: label, model, workstream (read from the label), task (read from the label), status (`running`, then `done`), verdict (`PASS`), tokens (100 in, 50 out), cost (priced from the catalog). `demo-model` above is a placeholder, not a catalog row: the three lines report fine, but the run stays unpriced (`check` names it) until `--model` names a catalog id or you add the §5 row. List the priced ids with:

```bash
python3 -c "import json; print('\n'.join(m['model'] for m in json.load(open('tools/agent-dash/catalog/prices.json'))['models']))"
```

`--session` ties the three lines to one run id (`generic:abc:ws4-WS4.1-notes-index`); omit it and the id is `generic:<label>`. `--run-id` overrides both. `finish --status` is `done`, `killed`, or `died_early` (default `done`). `finish` without `--model` keeps the model from `start`. `finish --verdict` must be one of `PASS`, `PARTIAL`, `BLOCKED`, `APPROVE`, `APPROVE_WITH_NITS`, `APPROVE_WITH_CHANGES`, `REQUEST_CHANGES`; anything else exits 2 and writes nothing.

Raw JSONL works the same when the CLI is out of reach. Append to `<state-dir>/generic.jsonl`, one object per line:

```json
{"type": "run", "label": "ws4-WS4.1-notes-index", "status": "running", "model": "demo-model", "session_id": "abc", "started_at": 1700000000.0}
{"type": "usage", "session_id": "abc", "run_id": "generic:abc:ws4-WS4.1-notes-index", "tool": "generic", "message_id": "turn-1", "model": "demo-model", "input": 100, "output": 50, "cache_read": 0, "cache_write": 0, "reasoning": 0}
{"type": "run", "label": "ws4-WS4.1-notes-index", "status": "done", "verdict": "PASS", "session_id": "abc", "ended_at": 1700000060.0}
```

The collector tails the file and picks lines up within a poll. Bad lines are skipped. There is no field for prompts or secrets; never invent one.

The file path alternative is the scratchpad manifest. The dashboard scans each scratch root for `muse-sessions.tsv`, `glm-sessions.tsv`, `grok-sessions.tsv`, and `cursor-sessions.tsv` — including a manifest nested one level under a scratchpad (a lead child folder) — then reads `out-<label>.md`, `err-<label>.log`, and `report-<label>.md` from the `muse/`, `glm/`, `grok/`, `cursor/`, or `exec/` folder beside the TSV. Columns are `label`, `session_id`, start epoch, optional `model`, tab-separated; a header row is skipped. A run folder registers itself: `run_worker.py` records its run folder in `<state-dir>/run-dirs.txt` before launching, and the manifest sources scan every registered folder in addition to the configured scratch roots, wherever the lead put it. A flat run folder (TSV plus `out-`/`err-` files directly inside, no `muse/`/`glm/` subfolders) reads the same way. A harness that writes manifests elsewhere registers its folder with one line in `run-dirs.txt`, or with `report --run-dir <dir>`. The cursor launcher writes the model fourth (`label`, `-`, epoch, `model`); a run takes that model until a usage line names one:

```text
ws4-WS4.1-notes-index	550e8400-e29b-41d4-a716-446655440000	1700000000	demo-model
```

Status comes from the files: growing output means running, a SIGTERM trace in the err file means killed, an empty out file with a real error means died early, else done. The verdict comes from the first matching line of the out file (next section). Tokens do not travel this path; pair it with a `report usage` line or `generic.jsonl` usage lines. The shipped scratch glob is `$HOME/agent-scratch/*`, one directory deep; add deeper paths to `roots.scratchpads` and restart. Manifest rows prove liveness through out-file growth or a live sample of a shipped worker binary: a custom binary whose out file stays quiet reads `done` while it lives, and registering the folder does not revive it. When your binary is not one of the shipped launchers, report liveness with `report start` / `report usage` heartbeats / `report finish` instead of relying on the manifest alone.

The live-agents tile counts the `grok`, `muse`, `glm`, and `claude` buckets. A generic run shows in the running table, on its task chip, and in tokens and cost either way; set `--tier` to the closest bucket only when the tile must count it.

## 3. Labels and the task map

A run label is `ws<N>-<task-id>-<slug>`, lowercase workstream first:

- `ws4-WS4.1-notes-index` is workstream WS4 (the shipped `^ws(\d+)` regex).
- Reviews append `-rev1`, `-rev2`. Fixes append `-fix1`, `-fix2`.
- The workstream id in the label must match the bundle id (`WS4`, not `ws4`).

The page pins a run to the task chip when the label holds the task id. That covers `ws4-WS4.1-notes-index` to `WS4.1` with no extra file; WS1, WS2, and WS7 have extra built-in matchers. `static/taskmap.local.json` (replaces the example file, no restart) is the override for labels that do not hold the id. Keys are exact labels or `/regex/` strings; values are task ids:

```json
{
  "WS4": {
    "nightly-index": "WS4.1",
    "/^ws4-legacy-/": "WS4.2"
  }
}
```

The server serves the resolved file at `/static/taskmap.json`. A run that maps to a workstream but no task is listed above the map as not mapped; that list is the signal to add an entry.

## 4. Verdict lines

Write the verdict as the first matching line of the report or out file:

- Worker reports: first line `PASS`, `PARTIAL`, or `BLOCKED`.
- Reviews: first line `VERDICT: APPROVE`, `VERDICT: APPROVE_WITH_NITS`, `VERDICT: APPROVE_WITH_CHANGES`, or `VERDICT: REQUEST_CHANGES`.
- `report finish --verdict` takes the word without the prefix: `--verdict PASS`, `--verdict APPROVE_WITH_CHANGES`.

Any later line that matches also counts, but the first one wins. `PARTIAL`, `BLOCKED`, `APPROVE_WITH_CHANGES`, and `REQUEST_CHANGES` mark the attempt as sent back on the `↻` count.

## 5. Tokens and cost

A usage record carries `input`, `output`, `cache_read`, `cache_write`, and `reasoning`. Billed Grok lines live in `cursor-usage.jsonl` (the stop hook appends them). Generic lines live in `generic.jsonl` (the `report usage` helper appends them). Claude, muse, and GLM usage comes from their own session logs. Rates live in `tools/agent-dash/catalog/prices.json` as USD per million tokens. The lookup is an exact string match on `model`.

Add a model by appending a row:

```json
{"provider": "example", "model": "demo-model", "input": 1.0, "output": 4.0,
 "cache_read": 0.2, "cache_write_5m": null, "cache_write_1h": null, "reasoning": null,
 "input_includes_cache_read": false, "billing_mode": "api"}
```

A model with no input rate or no output rate stays unpriced: the cost tile counts unpriced runs instead of showing `$0`. Zero tokens cost zero even with no rate for that class. A `reasoning` rate requires reasoning counts, else the run stays unpriced. `billing_mode` (`api`, `plan-included`, `subscription`, `unknown`) is a note; the numeric rates still apply. A `long_context` block prices prompts past its threshold at the higher rates.

## 6. Drive a workstream

Create the bundle, fill the plan, render, validate:

```bash
python3 tools/agent-dash/agent_dash.py ws new WS4 "Notes index"
python3 tools/agent-dash/agent_dash.py ws render WS4
python3 tools/agent-dash/agent_dash.py ws validate WS4
```

`ws new` writes `bundle.json`, `plan.json`, `layout.json`, `visuals.html`, and `START-PROMPT.md` under `AGENT_DASH_WORKSTREAMS` (default `~/.local/share/agent-dash/workstreams/WS4/`). The template already validates. A minimal valid `plan.json` that passes `ws validate`:

```json
{
  "dag": {
    "cols": ["Build", "Ship"],
    "groups": [
      {"id": "build", "col": 0, "title": "Build", "tasks": [
        {"id": "WS4.1", "title": "Write the notes index", "status": "todo", "deps": []},
        {"id": "WS4.2", "title": "Wait for the site freeze", "status": "todo", "kind": "gate"}
      ]},
      {"id": "ship", "col": 1, "title": "Ship", "tasks": [
        {"id": "WS4.3", "title": "Publish the notes index", "status": "todo",
         "deps": ["WS4.1", "WS4.2"], "dep_notes": {"WS4.2": "run only after the freeze lifts"}}
      ]}
    ],
    "edges": [["build", "ship"]]
  },
  "narrative": {
    "thesis": "A notes index is one page made from the notes directory.",
    "end_state": "The index lists every note and links to it.",
    "phases": [{"name": "Build", "goal": "Index on disk.", "gate": "Index file exists."}]
  },
  "trains": []
}
```

Leading a workstream (or several)? Follow the [orchestration loop](../../../skills/workstream-orchestration/SKILL.md#the-loop): `ws next` lists startable tasks ranked by what they unblock, `ws waves` checks the plan's max parallelism, `ws overlap` flags file collisions before launch, `ws gates` shows gate freshness, and `ws deferred` tracks the receiving end of moved tasks. The rules behind them are [orchestration principles](orchestration.md).

Move a task as the fact changes:

```bash
python3 tools/agent-dash/agent_dash.py ws task WS4 WS4.1 wip
python3 tools/agent-dash/agent_dash.py ws task WS4 WS4.1 pr
python3 tools/agent-dash/agent_dash.py ws task WS4 WS4.1 deferred --moved-to WS6 --note "waits on the freeze"
```

Move the workstream itself the same way (`planned -> active -> done -> archived`, plus `paused` and `dropped`; reopening `done` to `active` needs `--force --note`):

```bash
python3 tools/agent-dash/agent_dash.py ws new WS4 "Notes index" --state planned
python3 tools/agent-dash/agent_dash.py ws state WS4 active
python3 tools/agent-dash/agent_dash.py ws state WS4 done
python3 tools/agent-dash/agent_dash.py ws archive WS4   # needs docs/closing-report.md in the bundle and a 100% plan (or --force --note)
python3 tools/agent-dash/agent_dash.py ws restore WS4   # archived -> done, keeps summary.json
python3 tools/agent-dash/agent_dash.py ws list --state all
```

`ws archive` freezes `summary.json` (final stats, lifetime tokens/cost when the dashboard is reachable) and never deletes anything. The archive card's Report link names `docs/closing-report.md` (basename only, body never served), so write the report there rather than only under `reports/`. The full state rules are in [SKILL-PLAN.md](../SKILL-PLAN.md#workstream-lifecycle). The dashboard page is a lifecycle control surface: `POST /api/workstreams/state` with body `{"ws": "<ID>", "state": "<planned|active|paused|done|dropped>"}` moves one workstream through the same transition table (reopening `done` to `active` needs a `"note"`), and `POST /api/workstreams/bulk` with body `{"action": "archive"|"restore"|"state", "state": "<target>", "ws": ["<ID>", ...]}` runs up to 50 ids with per-id results that never abort midway; both answer the previous state so the page can Undo, and `GET /api/state` exposes `allowed_actions` per workstream so the page never offers an invalid button (program bundles like OVERVIEW expose none). All four endpoints run the same lifecycle functions behind local-only guards (`Host`/`Origin` plus `X-AgentDash-Action: 1`) and a read-only switch (`AGENT_DASH_READONLY=1`, config `readonly: true`); the page confirms archive, drop, and bulk inline with an Undo toast, and falls back to copy-command buttons when writes are off. They are the only write actions; see CONTRACT.md.

Status is `todo`, `wip`, `pr`, `prog`, `done`, or `deferred`. `wip` is work on a branch; `pr` is approved or open; `prog` is merged with a production step left; `done` is merged and live. `--moved-to` and `--note` belong only on `deferred`. Status edits read live; `render` is only for prose and drawings. `ws validate` fails on a dep that names an unknown task or the task itself, a `dep_notes` key outside the task's `deps`, and a bare number citation (name things first: `the notes cutover (WS4.3)`).

Gates model outside signals. A task with `"kind": "gate"` draws as a diamond; the dependent names the condition in `dep_notes`, keyed by the gate id, 280 chars or fewer. Trains ship related tasks as one branch and one PR:

```bash
python3 tools/agent-dash/agent_dash.py ws train WS4 T1 --title "notes cutover" --add WS4.1,WS4.3
python3 tools/agent-dash/agent_dash.py ws train WS4 T1 --branch train/notes-cutover --status assembling
python3 tools/agent-dash/agent_dash.py ws train WS4 T1 --status pr --pr 100
python3 tools/agent-dash/agent_dash.py ws trains WS4
```

Train status flows `planned`, `assembling`, `gate`, `pr`, `merged`, or `dropped`. At most 8 members; one task in one train. Approvals gate production steps:

```bash
python3 tools/agent-dash/agent_dash.py ws approve WS4 notes-cutover pending --step "Publish the notes index" --reversible no --conditions "The index file is on disk."
python3 tools/agent-dash/agent_dash.py ws approvals WS4
```

The lead records `pending`; the owner answers `approved` or `denied`. The lead reads `ws approvals` before the step and runs one production step at a time.

Orchestrate from the plan instead of re-reading it. Each takes `--json` (machine JSON on stdout, human text on stderr) and exits 0 ok, 2 usage, 3 not found, or 4 invalid data:

```bash
python3 tools/agent-dash/agent_dash.py ws next WS4 --limit 3
python3 tools/agent-dash/agent_dash.py ws waves WS4
python3 tools/agent-dash/agent_dash.py ws overlap WS4
python3 tools/agent-dash/agent_dash.py ws gates WS4
python3 tools/agent-dash/agent_dash.py ws deferred WS4
python3 tools/agent-dash/agent_dash.py lead-profile --ws WS4
```

`ws next` lists ready tasks (all deps done, gates included) ranked by critical-path impact, each with what it unblocks, its train, and its live run; it also warns on started tasks with no live run (stalled) and tasks started before a dep finished (dep-jump). `ws waves` prints max-parallel waves (split on declared `files` footprints), the critical path by task count and by observed duration, best span vs actual, and max parallelism. `ws overlap` warns on colliding footprints among open tasks (`--all` checks the next wave instead; `--cross` scans across workstreams; branch diffs join in when the dashboard is reachable). `ws gates` reports every gate's signal, last confirmation, STALE state, and blocked dependents. `ws deferred` tracks moved tasks to their receiving workstream and flags orphans nobody picked up. `lead-profile` counts every worker a lead launched (workflow wrappers and manifest rows included), with concurrency, idle-ready share, tails, and LOW-DELEGATION / IDLE-READY / LONG-TAIL / CONTEXT>250k flags.

Every `ws task` move is also appended to the bundle's `history.jsonl`; the progress map's Replay button plays that history (plus agent runs) back, and `scripts/render_replay.mjs` records it to MP4 and GIF (docs/configuration.md).

Who writes what (one writer per file): the lead owns the bundle (`plan.json` via `ws task`/`ws train`, `approvals.json` via `ws approve`); workers own their branches and their report lines; the dashboard never writes sources. Read-only on every source; labels only, never prompts or secrets.

## 7. Troubleshooting

| Symptom | Likely cause | Command |
|---|---|---|
| Run missing | Label has a `/`, `\`, leading dot, or newline; or the collector has not polled yet | Fix the label; wait one poll; check `/api/health` `last_error` |
| Worker invisible, wrapper shows alone | Run folder outside `roots.scratchpads` and not registered | Register the folder (`python3 agent_dash.py report --run-dir <dir>`, or one line in `run-dirs.txt`); check `/api/health` `scratch_roots`, `run_dirs`, `warnings`; `check` reports live-but-invisible workers as `unseen worker` ([page](problems/unseen-worker.md)) |
| Lane counts twice | Wrapper and worker share a label but the worker row is missing | Same as above: the lane counts once the worker row is live |
| Wrong workstream | Label regex missed; session fallback claimed it | Check `/api/runs` `ws_confidence`; add a `map.local.json` exact entry |
| Run not on its chip | Label does not hold the task id and no task-map entry covers it | Add the entry; reload; `taskmap.json` serves resolved |
| Unpriced run | Model string differs from the catalog, or a rate is null | Match the string exactly or add the §5 row, then `reprice` (or `doctor --fix --yes`); cost counts unpriced runs separately, never `$0` ([page](problems/unpriced-model.md)) |
| Stale running run | Process died without a finish line, or the source stopped reporting it | Send `report finish` (a generic run with no `report` line for 6 h counts as stale, so long runs send `report usage` as a heartbeat); the collector closes rows whose process is gone or that the source (Dagster) no longer lists, and `check` reports any `stale running` row it finds (CONTRACT.md, Liveness) |
| Critical problem before a wave | Dashboard blind, numbers wrong, or model unpriced | Run `doctor --json --severity critical`; the `ws-wave` workflow does this once before the wave and stops on any critical problem (pass `skipDoctor: true` to skip) |
| Worker will not launch | Preflight blocks the launch | `run_worker.py` runs a doctor preflight (dashboard reachable, run dir visible, tier model priced and policy-compliant) and exits 1 with the problem and fix unless `--no-preflight` is passed |
| Numbers disagree | Tile, chart, and rows read one snapshot; a custom sum re-sums buckets | Run `check`; it exits 1 with the mismatch table |
| Page blank | Dashboard down, or fixture URL on the live port | `status`; open `http://127.0.0.1:8765/`; fixtures need the `http.server` preview URL |

Plan `% done` comes from the server (`progress` on `/api/plan` and `/api/bundle`); the page never computes it. Hub issue counts are issue counts, never progress.

## 8. Working on the dashboard itself

Picking up dashboard work (not just feeding it runs)? Read [roadmap.md](roadmap.md) first: what landed with proof, and the open worker-sized tasks in value order. The visual future is the [proposed v2 design spec](design-v2.md) (owner approval pending) with its [tokens](design-v2-tokens.css). Ownership of every file is the table in [CONTRACT.md](../CONTRACT.md). Coordinating the work itself is the [workstream-orchestration skill](../../../skills/workstream-orchestration/SKILL.md) and its [principles](orchestration.md).

### Adding a card

One file plus one registry line. Put the renderer in a new IIFE file under `static/cards/` (copy the header docblock: what it renders, what it reads from `state`, what it exports), attach the render function to `window.AdPage.cards` and keep it as a plain global, add the `<script src="/static/cards/<name>.js">` tag in `static/index.html` before `registry.js`, and add one line to `static/cards/registry.js`:

```js
/* static/cards/decisions.js */
(function (global) {
  "use strict";
  function decisionsCard() {
    var c = el("div", {class: "card"});
    (state.decisions || []).forEach(function (b) {
      c.appendChild(el("p", {text: b.ws}));
    });
    return c;
  }
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.cards = AdPage.cards || {};
  AdPage.cards.decisions = global.decisionsCard = decisionsCard;
})(window);
{id: "decisions", order: 58, scope: "project", spec: decisionsSpec}, // registry.js
```

`scope` is `overview` (only on `ws=all`), `workstream` (only off it), or `both`. Two more scopes mount on the project views: `project` (the Project home, `?view=project`) and `planned` (the Planned view, `?view=planned`). A project/planned entry with a `slot` naming one of the view's slot containers (`exec-burnup`, `exec-health`, `exec-depgraph` on the home; `exec-depgraph` on Planned) renders inside that slot; one without a `slot` renders as its own section after the slots, in registry order. A chart needed on both views adds one registry line per scope over the same spec function:

```js
{id: "depgraph", order: 62, scope: "project", slot: "exec-depgraph", spec: depGraphSpec},
{id: "depgraph-plan", order: 62, scope: "planned", slot: "exec-depgraph", spec: depGraphSpec},
```

The live example is the burn-up chart (`static/cards/burnup.js`, fed by
`GET /api/project/burnup` from `dash/projectburn.py`). Its card file
fetches its own endpoint (or `../fixtures/burnup.json` under `?fixtures`)
with an in-flight guard, so the shared refresh paths stay untouched;
a failed fetch renders "request failed", never zeros. A new endpoint module
stays pure — plain data in, JSON out — with a small `collect(store, now)`
the server hook calls, builder tests in `tests/test_projectburn.py`,
endpoint tests in `tests/test_server.py`, a `burnup.json` writer in
`fixtures/make_fixtures.py`, and the endpoint path added to
`tests/test_privacy_pin.py` (names and numbers only, never bodies or paths).

`spec` returns one `applySections` spec `[name, key, render, style]`, or null when the card does not apply. Keep the two-space `function name(` indent: `tests/test_page_rules.py` splits the logical script (core + cards in tag order + inline) on it. The `decisions.js` card is the worked example for the project scope: registered once (`scope: "project"`), rendered as its own section after the slots, hiding entirely when its source is absent. New views obey `DESIGN.md`: status as colour plus symbol plus word, numbers through the `fmt` helpers only, hover tooltips, axes on charts (the shared `axes()` helper).

### Server-fed project cards (decisions and friends)
A project card that needs its own endpoint keeps the endpoint beside the card: a pure builder module under `dash/` (see `dash/projectfeed.py`: plain data in, JSON out, no I/O in `build_feed`) plus a small `GET` hook in `dash/server.py` that parses params and serves it. The card file stays read-only: it loads through the shared `api()` helper (never raw `fetch`, never a non-GET method), so fixture mode (`?fixtures`) keeps working via `fixtures/<name>.json`. Because the load resolves after the first paint, the loader deletes its own `secCache` section and calls `drawMain()` once, guarded by an in-flight flag and the current `state.seq`, so a slow endpoint never flashes and never refetches per tick. A card whose source is optional (the decisions card over `GET /api/decisions`) returns null from its spec while loading and whenever the endpoint is absent or reports `source.present` as false: the section stays an empty node, never a "nothing here" placeholder. The card carries no doctor output: no status counts, no problem words anywhere in the card or its stylesheet.
A card with its own endpoint fetches it itself: `static/cards/depgraph.js`
is the worked example. It reads `GET /api/project/graph` (built by the
pure `dash/projectgraph.py` builder over local bundles: `depends_on`,
gate signals naming workstreams, deferred `moved_to` links) with one GET
per 30 s, renders from its cache, and falls back gracefully when the
endpoint is absent. It registers one line per scope over the same spec
(`scope: "project"` and `scope: "planned"`, both `slot: "exec-depgraph"`).
`fixtures/graph.json` (from `fixtures/make_fixtures.py`) feeds the
`?fixtures` preview. Cards never write, except the lifecycle controls
(`static/core/actions.js`, `static/core/manage.js` over `apiPost`): the page
write actions, behind the server guards, with copy-command fallback when
writes are off. The pinned bar holds no tab strip: one `Workstreams (N)`
switcher button (`static/core/switcher.js`) opens the state-grouped popover,
and Current leads with the management table (`static/core/manage.js`).
### Project burn-up
The worked example for a project card with its own endpoint is `static/cards/burnup.js` (slot `exec-burnup`, `scope: "project"`), fed by `GET /api/project/burnup` (`dash/projectburn.py`, pure builder plus a small hook in `dash/server.py`). The card fetches its endpoint itself with a loading state and a distinct `request failed` state (project refresh paths stay on the one snapshot response), and reads `../fixtures/burnup.json` under `?fixtures` (built by `fixtures/make_fixtures.py`). History that was never recorded is a labelled gap (`no history before <date>: N done tasks not dated`), never interpolated dates; days are UTC calendar days. New endpoints get tests in `tests/test_server.py` plus a privacy-pin path entry (names and numbers only, never file bodies or paths).
### Page settle on tiny dashboards
A brand-new project (no bundles, or a couple of bundles with no plans yet) must load exactly like a big one. The rule for every re-render path is: idempotent and terminating. The overview loads the workstream plan sums once per workstream set (a done-marker keyed by the set, in the `drawMain` wiring in `static/index.html`); an empty result still marks the set done, and the follow-up redraw happens only when the load actually added plan data. Section keys (`keyOf(...)` in `static/cards/registry.js` and the project views) hold no per-render volatile values — the one allowed clock read is the minute bucket behind the live charts. `drawMain` counts renders on `window.__adRenders` (one increment per call) so automation can see a loop.
Prove it with the settle probe (stdlib only, headless Chrome):
```bash
node tools/agent-dash/scripts/page_settle_check.mjs --url http://127.0.0.1:8815
```
It checks the exec overview and the project home: after a 2 s grace a 3 s sample of the render count must look quiescent (a tight loop raises it in nearly every 250 ms poll; a healthy live page only re-renders on server ticks, seconds apart), and the following 10 idle seconds must stay within a small render bound. `python3 tools/agent-dash/agent_dash.py check --page` runs the same sampled gate inside `scripts/page_check.mjs` before its tab loop. `tests/test_page_rules.py` pins the root cause statically (one-shot plan load, empty-settling `loadPlanSums`, volatile-free keys, `applySections` idempotency, the render counter).

### Before you push

Run the scoped gate from `tools/agent-dash`. It maps your changed files to test files (`scripts/gate_map.json`), runs each selected test file on its own (never a bare suite), checks changed scripts, and runs the repo privacy audit:

```bash
cd tools/agent-dash
sh scripts/gate.sh --list   # what it would run, without running it
sh scripts/gate.sh          # one verdict per file, then GATE PASS/FAIL
```

`test_privacy_pin` and `test_page_rules` always run: the first asserts no prompt, argv, env, or secret material reaches any endpoint payload (only the worker's `--model` flag value is ever kept), the second pins the page rules. To gate every push in a checkout, opt in once (never installed automatically):

```bash
sh tools/agent-dash/scripts/install_hooks.sh   # idempotent pre-push hook; refuses foreign hooks
```
