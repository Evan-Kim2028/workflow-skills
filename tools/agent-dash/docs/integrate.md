# Plug in any agent

The dashboard shows agent runs, tokens, cost, and plan progress. Any harness can feed it. The two paths are the `report` command (three lines per run) and the manifest files (one TSV row plus report files). Detail lives in [CONTRACT.md](../CONTRACT.md).

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
`doctor` exits 0 when clean, 1 when problems found. `doctor --fix` is a
dry run of the safe automatic fixes; `doctor --fix --yes` applies only
safe, local, reversible ones (register a run dir, add a missing scratch
root to `config.local.json` with a backup, prune, restart a stalled
dashboard, reprice and backfill models). `doctor report --since 24h
[--md]` digests history (new, regressed, resolved, open by severity
and workstream) for hub reports. Every problem code has a page under
`docs/problems/` (for example `docs/problems/stuck-task.md`) with what
it means, the exact fix commands, and how to verify.

These commands take `--json` for machine output on stdout only (human
text moves to stderr when `--json` is set): `check`, `check --page`
(summary), `state`, `status`, `doctor`, `ws validate`, `ws approvals`,
`ws trains`, `ws plan <ID>`. The schema only gains fields, never
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

State lives in `~/.local/share/agent-dash/` (`dash.sqlite`, pidfile, log, `generic.jsonl`). Point `AGENT_DASH_STATE_DIR` at an empty directory to trial the dashboard without touching live state. Point `AGENT_DASH_WORKSTREAMS` at another directory to trial bundles.

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

That is the whole minimum: label, model, workstream (read from the label), task (read from the label), status (`running`, then `done`), verdict (`PASS`), tokens (100 in, 50 out), cost (priced from the catalog). `--session` ties the three lines to one run id (`generic:abc:ws4-WS4.1-notes-index`); omit it and the id is `generic:<label>`. `--run-id` overrides both. `finish --status` is `done`, `killed`, or `died_early` (default `done`). `finish` without `--model` keeps the model from `start`.

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

Status comes from the files: growing output means running, a SIGTERM trace in the err file means killed, an empty out file with a real error means died early, else done. The verdict comes from the first matching line of the out file (next section). Tokens do not travel this path; pair it with a `report usage` line or `generic.jsonl` usage lines. The shipped scratch glob is `$HOME/agent-scratch/*`, one directory deep; add deeper paths to `roots.scratchpads` and restart.

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
| Worker invisible, wrapper shows alone | Run folder outside `roots.scratchpads` and not registered | Check `/api/health` `scratch_roots`, `run_dirs`, `warnings`; register the folder (`run-dirs.txt` line or `report --run-dir`); `check` reports live-but-invisible workers as `unseen worker` |
| Lane counts twice | Wrapper and worker share a label but the worker row is missing | Same as above: the lane counts once the worker row is live |
| Wrong workstream | Label regex missed; session fallback claimed it | Check `/api/runs` `ws_confidence`; add a `map.local.json` exact entry |
| Run not on its chip | Label does not hold the task id and no task-map entry covers it | Add the entry; reload; `taskmap.json` serves resolved |
| Unpriced run | Model string differs from the catalog, or a rate is null | Match the string exactly; add the row; cost counts unpriced, never `$0` |
| Stale running run | Process died without a finish line, or the source stopped reporting it | Send `report finish` (a generic run with no `report` line for 6 h counts as stale, so long runs send `report usage` as a heartbeat); the collector closes rows whose process is gone or that the source (Dagster) no longer lists, and `check` reports any `stale running` row it finds (CONTRACT.md, Liveness) |
| Critical problem before a wave | Dashboard blind, numbers wrong, or model unpriced | Run `doctor --json --severity critical`; the `ws-wave` workflow does this once before the wave and stops on any critical problem (pass `skipDoctor: true` to skip) |
| Worker will not launch | Preflight blocks the launch | `run_worker.py` runs a doctor preflight (dashboard reachable, run dir visible, tier model priced and policy-compliant) and exits 1 with the problem and fix unless `--no-preflight` is passed |
| Numbers disagree | Tile, chart, and rows read one snapshot; a custom sum re-sums buckets | Run `check`; it exits 1 with the mismatch table |
| Page blank | Dashboard down, or fixture URL on the live port | `status`; open `http://127.0.0.1:8765/`; fixtures need the `http.server` preview URL |

Plan `% done` comes from the server (`progress` on `/api/plan` and `/api/bundle`); the page never computes it. Hub issue counts are issue counts, never progress.

## 8. Working on the dashboard itself

Picking up dashboard work (not just feeding it runs)? Read [roadmap.md](roadmap.md) first: what landed with proof, and the open worker-sized tasks in value order. The visual future is the [proposed v2 design spec](design-v2.md) (owner approval pending) with its [tokens](design-v2-tokens.css). Ownership of every file is the table in [CONTRACT.md](../CONTRACT.md). Coordinating the work itself is the [workstream-orchestration skill](../../../skills/workstream-orchestration/SKILL.md) and its [principles](orchestration.md).

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
