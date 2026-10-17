# agent-dash contract (v1)

Local, read-only execution dashboard. One Python 3 process (stdlib only), binds 127.0.0.1:8765.
Code is this directory. `install.sh` links it to `~/.claude/tools/agent-dash`. State: `~/.local/share/agent-dash/` (dash.sqlite WAL, pidfile, log).
Never writes to any source, never signals agent processes, never stores prompt bodies or argv.
The one argv value ever kept is the worker's `--model` flag value, recorded on process samples and live-process rows so a run whose model is otherwise unknown can be named. Model ids, labels, and token counts are the only worker-derived strings in the store.

## Layout and file ownership (one writer per file)

| Path | Owner |
|---|---|
| `agent_dash.py` (CLI: `run`, `stop`, `restart`, `status`, `state`, `doctor`, `backfill --session <id>`, `reprice`, `reconcile`, `check`, `ws`, `report`) | core |
| `dash/state.py` (`GET /api/state`), `dash/doctor.py` (`doctor`), `dash/errors.py` (exit codes, messages) | cli |
| `dash/store.py` (schema, offsets, upserts, buckets, counts) | core |
| `dash/server.py` (HTTP + SSE, routes below) | core |
| `dash/attribution.py`, `map.example.json` (`map.local.json` replaces it) | core |
| `dash/pricing.py`, `catalog/prices.json` | core |
| `dash/plans.py`, `plans.example.json` (`plans.local.json` replaces it) | core |
| `dash/bundles.py`, `dash/planrender.py`, `SKILL-PLAN.md` | core |
| `scripts/cursor_usage_hook.py` | src-cursor |
| `dash/sources/__init__.py` (registry, scheduler) | core |
| `dash/rundirs.py` (run-folder registration, `run-dirs.txt`) | core |
| `dash/sources/process.py`, `dash/sources/hub.py` | core |
| `dash/sources/claude.py`, `dash/sources/manifests.py`, `dash/sources/generic.py` | src-ai |
| `dash/sources/muse.py`, `dash/sources/grok_cli.py` | src-tools |
| `dash/sources/cursor.py` | src-cursor |
| `static/index.html` (all UI, vanilla JS, inline SVG/canvas charts) | Claude (parent) |
| `fixtures/*.json` (seeded synthetic API responses from `fixtures/make_fixtures.py`) | fixture |
| `tests/test_*.py` (unittest, stdlib) | each owner for its files |
| `docs/roadmap.md` (open work plan) + `docs/design-v2.md` + `docs/design-v2-tokens.css` (proposed v2 spec) | core (docs only; spec builds only on owner approval) |

## Source interface

Each `dash/sources/<name>.py` exposes:

```python
NAME = "claude"            # unique
INTERVAL_S = 2             # poll period
def poll(store, cfg, now: float) -> None: ...
```

`store` methods (implemented by core in `dash/store.py`):

```python
store.get_offset(path) -> (inode, offset) | None
store.set_offset(path, inode, offset, size, mtime)
store.upsert_session(dict)    # keys: session_id, tool, cwd, started_at, status, parent_session_id, label
store.upsert_run(dict)        # keys: run_id, source, tier, model, label, session_id, parent_session_id,
                              #       status, verdict, started_at, ended_at, clock_source, out_bytes, err_bytes, pid, out_path
store.add_usage(dict)         # keys: ts, session_id, run_id, model, tool, message_id,
                              #       input, output, cache_read, cache_write, reasoning   (upsert on tool+session_id+message_id, last wins)
store.add_process_sample(dict)# keys: ts, pid, comm, rss_kb, elapsed_s, label, run_id, model (the `--model` flag value only)
store.set_ws_counts(list)     # rows: ws, name, backlog, todo, doing, review, done, cancelled
```

`cfg` is the merged config (roots, intervals, read budgets: 4 MB per file, 8 MB per tick; poll budget `poll_budget_s`, default 25 s). There is no concurrency cap.
Sources must be incremental (byte offsets), budget-bounded, and exception-safe (log, never crash the loop). Directory listings are cached by (inode, mtime) with a per-pass cap on fresh listings, so repeat polls over a quiet tree cost stats, not walks. Dead offset rows (absolute paths that no longer exist) are pruned a batch per reconcile tick with one VACUUM per pass; only the newest 3 `*.bak-*` stay in the state dir.

Enums. `source`: claude-main, claude-subagent, claude-workflow, muse, glm, grok (cursor-agent), shell, generic.
`tier`: opus, sonnet, haiku, muse, glm, grok. The generic source passes its tier through and defaults to `generic` (from the model name when it holds a known family). `status`: running, done, killed, died_early, unknown.
`verdict`: PASS, PARTIAL, BLOCKED, APPROVE, APPROVE_WITH_NITS, APPROVE_WITH_CHANGES, REQUEST_CHANGES, or null.
`clock_source`: transcript, muse_log, manifest, mtime. Times are epoch seconds (float), UTC.
`run_id` is stable: `<source>:<session-or-uuid>:<label-or-agent-id>`.

Workstream attribution happens in core (`attribution.py`), not in sources: label via the map file, exact then regex
(`^ws(\d+)` -> WSn, `WS(\d+)\.` -> WSn, `^g-` -> WS1, `^dash-` -> TOOLS), then session default from the map
`sessions` object, else null. `map.local.json` replaces `map.example.json` when present. Store `workstream` and `ws_confidence`
(`label`, `regex`, `session-default`).

## HTTP API (all GET, JSON, 127.0.0.1 only)

API errors are JSON `{error, code, hint}`; `hint` names the next
command to run. CLI errors follow the same rule, with exit codes 0
ok, 1 check failed, 2 usage, 3 not found, 4 invalid data, 5
unreachable (docs/integrate.md, For agents).

`GET /` -> `static/index.html`.

`GET /api/snapshot?ws=<WSn|all>&since=&until=&usage_group=<model|workstream|tool>`
```json
{"now": 0, "seq": 0,
 "counts": {"live_agents": 0, "live_by_tool": {"grok": 0, "muse": 0, "glm": 0, "claude": 0}, "prod_jobs": 0, "prod_jobs_finished": 0, "finished_today": 0, "finished_in_range": 0, "finished_by_verdict": {}},
 "running": [Run], "recent": [Run],
 "totals": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0, "unpriced_runs": 0, "unpriced_tokens": 0, "unpriced_models": []},
 "totals_all_time": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0},
 "last_activity": 0,
 "by_workstream": [{"ws": "WS7", "live_agents": 0, "finished_in_range": 0, "finished_today": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "last_activity": 0}],
 "usage": [{"t": 0, "key": "opus", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost_usd": 0.0}]}
```
`usage` is present only when `usage_group` is given: the hourly usage
buckets for the same scope and window, computed under the same lock pass
as `totals`, so the bucket token sum equals `totals` by construction. A
bad `usage_group` is a 400.
`totals` is the windowed scope total; `totals_all_time` is the same scope with
no window, summed by the same helper. `last_activity` is the scope's newest
run stamp, or null when the scope has no runs. The page tiles read these
fields directly and never re-sum buckets on the client.
One function (`Store._summarize_locked`) computes the all-scope counts and totals,
every workstream row, and the `unassigned` row from the same run list, the same
agent rule, and the same window. The `by_workstream` row for a workstream equals
that workstream tab's `counts` and `totals`. `unassigned` carries runs with no
workstream; it appears only when such runs exist. Row sums plus unassigned equal
the all-scope numbers exactly (cost within 1e-6).
`Run` = `{run_id, source, tier, model, label, workstream, ws_confidence, session_id, parent_session_id, status, verdict,
started_at, ended_at, elapsed_s, clock_source, out_bytes, out_growing, err_bytes, pid, rss_kb, tokens: {input, output, cache_read, cache_write}, cost_usd, context_tokens}`.
`context_tokens` is the latest `grok-context` input for that run, or null. It is a context-window estimate. It is not part of `tokens`, totals, buckets, or cost. `elapsed_s` is null when `started_at` is null.
`recent` = runs ended inside the window (one fixed 24 h by default, `since`/`until` narrow it), newest first, max 50. Production jobs (source `dagster`) are never in `recent`; they are the `counts.prod_jobs_finished` count. The page shows it as `Finished (24 h)`.

`GET /api/stream` -> SSE. Event `tick` every 2 s: `{"seq": n, "running": [Run], "sources": [{name, last_poll, lag_s}]}`; the page refetches snapshot when `seq` changes. `sources` mirrors the registry freshness so the page can show staleness per source.

`GET /api/runs?ws=&since=&until=&limit=&include=` -> `{"runs": [Run]}` (Gantt input; workflow children collapsed into one row with `children: n`). Production (dagster) rows are left out by default; `?include=jobs` adds them back for tools that need them (anything else is a 400).

`GET /api/usage?group=model|workstream|tool&bucket=hour|day&ws=&since=&until=` -> `{"buckets": [{"t": 0, "key": "opus", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost_usd": 0.0}]}`

`GET /api/concurrency?ws=&since=&step=60` -> `{"points": [{"t": 0, "grok": 0, "muse": 0, "glm": 0, "claude": 0}]}`

`GET /api/sessions` -> `{"sessions": [{session_id, tool, cwd, label, workstream, started_at, status, runs: n, tokens, cost_usd}]}`

`GET /api/workstreams?since=&until=` -> `{"workstreams": [{ws, name, tk: {backlog, todo, doing, review, done, cancelled}, live_agents, finished_in_range, finished_today, tokens, cost_usd, tokens_all_time, cost_usd_all_time, last_activity, plan: bool, progress: {done, total, counts, pct} | null}]}`.
Rows come from the same rollup as the snapshot, so they match the tabs for the
same window. `tk` is the hub issue board (issue counts, never plan progress).
`progress` is the same object `/api/plan` carries for that workstream, resolved
through the one plan resolver (`dash.bundles.resolve_plan`), so one request
gives the tab bar its completed state; it is null when the workstream has no
plan. An `unassigned` row appears when runs have no workstream.

`GET /api/reconcile?hours=&since=&until=` -> `{"now": 0, "since": 0, "until": 0, "all": {live_agents, finished_in_range, finished_today, tokens, cost_usd}, "all_time": {tokens, cost_usd}, "rows": [per-workstream rows], "unassigned": {...}, "mismatches": []}`.
Computed in one store pass under the lock. Rows carry `tokens_all_time` and
`cost_usd_all_time` next to the windowed numbers. `mismatches` lists every violated
invariant, windowed and all-time (rows plus unassigned equal the all-scope
numbers, and each row equals its workstream tab), and is empty when consistent.
`python3 agent_dash.py check [--url URL] [--hours N]` prints the rows as a
table and exits 1 on any mismatch, including one `unseen worker: <label>`
mismatch per live worker process (a `[label:]`, muse prompt-file label, or
`run_worker.py` argv label with no running worker row) the dashboard
cannot see. Only the label is reported; argv beyond it is never stored or
printed. `--hours` defaults to 24, kept as an optional override for API
compatibility. `python3 agent_dash.py check --page [--url URL]`
drives the live page in headless Chrome over the DevTools protocol
(`scripts/page_check.mjs`, stdlib only) and compares every rendered tile and
chart legend against the API for the overview and every workstream tab at the
one fixed 24 h window; it skips with a clear message when Chrome is missing.

`GET /api/plan?ws=WS7` -> that workstream's plan with a `progress` object
(plus an `issues` list, the `ws validate` findings, empty when clean; a bad
plan still serves 200 with its issues named, and the progress map shows a
muted "plan needs attention: <first issue>" line instead of drawing the bad
states as if they were fine):
`{"ws": "WS7", "cols": [..], "groups": [{id, col, title, tasks: [{id, title, status, deps, kind, dep_notes}]}], "edges": [[a, b]], "phases": [{name, goal, gate}], "progress": {"done": 0, "total": 0, "counts": {todo, wip, pr, prog, done, deferred}, "pct": 0}, "ready": ["WS7.2"], "critical_path": ["WS7.1", "WS7.2"], "max_parallel": 3}`.
`ready` is the todo-with-satisfied-deps task ids ranked by critical-path impact, `critical_path` is the longest dep chain, and `max_parallel` is the widest dependency wave; all three come from `dash/orchestrate.py` with no store reads, cached by `plan.json` mtime.
A task's `kind` is `task` or `gate` (an outside signal or condition, drawn as a diamond); absent means `task`. `dep_notes` maps one of the task's dep ids to the short condition on that edge. A task may also carry `files` (declared file footprints, a non-empty list of glob strings), and a gate may carry `signal` (what it waits on), `confirmed_at` (ISO-8601 UTC of the last check), and `fresh_hours` (recheck bound; older or missing confirmations read STALE). A deferred task may carry `received_as` (the task id in the receiving workstream that picked it up). All pass through every plan API sanitized like other fields. `/api/bundle` carries the same task shape inside `plan.dag.groups`.
`plans.local.json` replaces `plans.example.json`. Each value is a path. Relative paths resolve next to the plans file. The loader reads `dag` and `narrative.phases`.
One resolver (`dash.bundles.resolve_plan`) picks the plan file: a bundle's
`plan.json` wins over the plans-map file. `/api/plan`, `/api/bundle`, and the
page all read the same body, so the plan view and the execution map agree.
`progress.done` counts done tasks, `progress.total` counts every task except
deferred, `pct` rounds half up. Unknown task statuses are ignored. The page
never computes % done itself; it reads `progress.pct`, `progress.done`, and
`progress.total`.

`GET /api/replay?ws=<ID>` -> one timeline for a sped-up playback of the
workstream's history:
`{"ws": "WS1", "span": {"start": 0, "end": 0}, "tasks": {"<id>": {"status": "done", "first_active": 0, "done_at": 0, "inferred": false, "runs": [{"start": 0, "end": 0, "family": "muse", "label": "...", "verdict": "PASS", "source": "muse", "status": "done", "tokens": 0, "clock": "manifest"}]}}, "events": [{"t": 0, "kind": "active|done", "task": "<id>"}], "observed": 0, "inferred": 0}`.
`events` is sorted by time (active before done on ties); `active` events
carry the run's label and model family, `done` events carry `inferred`.
Run entries carry labels only, never prompts. A missing `ws` is a 400, a
bad id is a 400, and a workstream with no plan is a 404.
The run-to-task mapping is the one shared rule (`dash/taskmatch.py`): the
resolved task map (exact, then `/regex/` case-insensitive), the WS1/WS2/WS7
built-ins, then plan task-id substring. The page uses the same rule, and a
test pins them on sample labels. The timeline is built from one store pass
plus the plan: per task `first_active` is the first mapped run start and
`done_at` for a done task is the end of the first passing run (PASS,
APPROVE, APPROVE_WITH_NITS) after which no implementation run follows,
else the end of its last mapped run. A done task
with no mapped run is inferred: the latest done moment of its deps, else
the plan's earliest observed moment, marked `"inferred": true`. Recorded
history (`history.jsonl` in the bundle) wins over both when present.
`observed` counts tasks with an observed done moment, `inferred` counts
tasks with an inferred one.
In replay mode the page draws the run timeline card directly under the
plan progress map from this same span: both cards paint from the one
`rpCursorTime` playhead (a test pins that), so scrubbing either moves both.
`&clip=1` frames the two cards as one promo composition (title card,
caption strip, counters, closing card); `&clip=4x5` is the vertical
variant. Clip playback compresses idle stretches by cumulative event count
blended with real time; plain replay stays on real time with an optional
"Compress idle" toggle.

`GET /api/health` -> `{"ok": true, "sources": [{name, last_poll, last_error, lag_s, last_duration_s, interval_s, stalled}], "pricing": {...}, "disconnects_1h": 0, "unpriced_models": [{"model", "rows", "tokens"}], "scratch_roots": [...], "run_dirs": [...], "warnings": [...]}`.
`ok` is false when any model with usage rows in the last 24 h has no
catalog rate; those models are listed in `unpriced_models` (absent when
empty) and each is also an `unpriced model: <id> (<tokens> tokens in 24 h)`
mismatch in `/api/reconcile` (so `check` exits 1). A missing rate never
becomes zero: the cost tile reads `totals.unpriced_tokens` /
`totals.unpriced_models` and says `excl. N tokens unpriced (<models>)`.
`disconnects_1h` counts client disconnects (BrokenPipe/ConnectionReset)
in the last hour; they are counted, never traceback-logged.
`ok` is false when any source has an error, is `stalled` (no poll for more than 3x its interval), or overran its poll budget. Each source poll runs in a watchdog thread (`poll_budget_s`, default 25 s) so one hung source cannot stall the others; `/api/reconcile` mismatches include `source stalled: <name> (last ok poll N s ago)` for the same condition.
`scratch_roots` is the configured, resolved `roots.scratchpads` list (a local config that narrows it shows up here); `run_dirs` is the registered run folders. `warnings` names each registered folder outside the configured roots that holds a manifest for a currently live worker, so a run folder the config does not cover is loud instead of silently invisible.
`doctor` is the always-on check summary: `{"summary": {"critical": 0, "warn": 0, "info": 0, "regressed": 0}, "generated_at": 0}` (`generated_at` is null until the first run, 30 s after start). `ok` is false when `summary.critical` is above 0.

`GET /api/doctor` -> the latest persisted doctor run. The server runs the checks in a background thread every `doctor.interval_s` seconds (default 300; first run 30 s after start, after the first full poll) and persists them in the `doctor_problems` table. The endpoint never runs checks inline; `?refresh=1` only queues a run.
```json
{"generated_at": 0, "next_run_at": 0, "interval_s": 300,
 "summary": {"critical": 0, "warn": 0, "info": 0, "regressed": 0},
 "problems": [{"id": "stuck-task:ws:WS4:WS4.1", "code": "stuck-task", "severity": "warn",
   "scope": "ws:WS4", "ws": "WS4", "title": "", "detail": "",
   "fix": {"command": "", "safe_auto": false, "doc": "docs/problems/stuck-task.md"},
   "first_seen": 0, "last_seen": 0, "count": 1, "regressed": false}],
 "resolved_24h": [{"id": "", "code": "", "title": "", "resolved_at": 0}]}
```
`?ws=<ID>` keeps that workstream's problems plus global ones; `?severity=critical|warn|info` is the minimum level shown. A problem absent from a run resolves (`resolved_at` set, kept 14 days); one that reappears after resolution is `regressed`. Critical means the numbers are wrong or the dashboard is blind: source stalled, unseen workers, dashboard down, unpriced model with tokens, reconcile mismatch. Warn is workflow hygiene: stuck task, policy breach, orphan process, stale approval, invalid bundle, and friends. Info is advisory: dirty checkout, run folder outside roots. Every code has a page under `docs/problems/<code>.md`.

`GET /api/state` -> the whole dashboard in one call, computed in one
store pass (`Store.state_view`):
```json
{"schema": 1, "version": {"tool": "<git-sha|unknown>", "schema": 1},
 "now": 0, "window": {"since": 0, "until": 0, "hours": 24},
 "health": {"ok": true, "sources": [{name, last_poll, last_error, lag_s}], "scratch_roots": [], "run_dirs": [], "warnings": []},
 "workstreams": [{"id": "WS7", "title": "", "plan": true, "progress": {"done": 0, "total": 0, "counts": {}, "pct": 0},
   "live_agents": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "last_activity": 0}],
 "unassigned": {"live_agents": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "last_activity": 0},
 "running": {"count": 0, "by_source": {"muse": 0}, "prod_jobs": 0, "prod_jobs_finished": 0},
 "totals": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0, "unpriced_runs": 0, "prod_jobs": 0, "prod_jobs_finished": 0},
 "totals_all_time": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0},
 "reconcile": {"ok": true, "mismatches": []},
 "problems": [{"code": "stale-running", "message": "", "command": "python3 agent_dash.py check"}]}
```
`version.tool` is the git short sha of the tool directory, `"unknown"`
outside git. The window is the one fixed 24 h. `progress` is the same
object `/api/plan` carries, or null. `health` carries the same
`scratch_roots`, `run_dirs`, and `warnings` as `/api/health`.
`problems` is the persisted doctor list: the same entries (same ids)
as `GET /api/doctor`, each additionally carrying the legacy `message`
(`title: detail`) and `command` keys. Fields are only added, never
renamed.
`python3 agent_dash.py state [--json]` prints the same payload.
`doctor` additionally reports each health `warnings` entry as a
`run-dir-outside-roots` problem.

`GET /static/<file>` serves a file from `static/` or one subdirectory (`vendor/`). `/static/taskmap.json` is the resolved task map (`taskmap.local.json`, else `taskmap.example.json`). Context-window rows use tool `grok-context` and are left out of cost.

## Plan bundles

Plan and Execution are one shell, `static/index.html`. `GET /` and `GET /plan` both serve that file. The page swaps the view in place (`?view=plan|exec&ws=<ID>`). `/static/index.html?ws=<ID>` still opens Execution. Project text stays out of the tool directory, under `AGENT_DASH_WORKSTREAMS` (default `~/.local/share/agent-dash/workstreams`). One directory per bundle id. A directory with `bundle.json` shows up on the plan view and, for `kind: workstream`, on `GET /api/plan`. When both a bundle and the plans map name a workstream, the bundle's `plan.json` wins (`resolve_plan`).

## One number, one truth

Production jobs (source `dagster`) are never in agent lists; they are a
count. Snapshot `running` and `recent`, `/api/runs` by default, and the timeline
never hold a dagster row (`Store.is_prod_job_run`). `counts.prod_jobs` (running
now) and `counts.prod_jobs_finished` (finished in the window) ride on every
snapshot scope, and the live tile reads them (`N prod jobs`, `N prod jobs ran`).
`?include=jobs` on `/api/runs` adds the dagster rows back for tools that need
them. Lead sessions (`claude-main`) and shell rows keep their existing places in
the lists and are never counted as agents.

An agent is a running row whose source maps to a tool (grok, muse, glm, or
claude). Interactive lead sessions (`claude-main`), shell rows, and Dagster
jobs are not agents. Finished means status `done`, `killed`, or `died_early`
with an end time inside the window. The window is one fixed rolling 24 hours
(`since = now - 24 h` at each refresh); `since`/`until` stay for API
compatibility and narrow it when given. The page tiles read
`finished_in_range` (`Agents finished (24 h)`); `finished_today` (local
midnight) stays in the API for compatibility and is not shown.

Token and cost sums come from the hourly usage buckets, counting a bucket when
its hour overlaps the window (`hour_ts + 3600 > since` and `hour_ts < until`).
One function sums those cells for the window and, with no window, for all time.
The tokens tile and the chart legend Total read the server snapshot totals for
their scope and the fixed 24 h window; the tiles never re-sum buckets on the
client. The chart draws the same response's `usage` buckets, so tile, legend
and chart agree by construction; when the bucket sum ever differs from the tile
the page still shows a muted cross line naming both numbers instead of hiding
it.
`/api/usage` stays for compatibility but the page no longer reads it.
The overview workstream cards read the same snapshot rows the tabs read, so
each card shows exactly what its workstream tab shows for the same 24 h
window. Runs with no workstream land in the `unassigned` row, which the
Overview tab shows, so the overview total visibly equals the sum of the parts.
Every async page response applies only when it still matches the current tab
and grouping (one fixed window), and the background tab cache is keyed by tab
and group and expires after one refresh interval, so no tab ever shows another
tab's numbers. A scope with no tokens in the window reads "0 in 24 h" with its
last run and all-time total underneath. `Running now` is live and unwindowed;
all-time sublines are not windowed.

## Liveness

A running row is a claim that its source still sees it. Every source may
only emit `running` while it holds fresh liveness evidence, and one shared
helper (`dash/liveness.py` `pid_alive`, reap-safe like the worker runner)
decides whether a stored pid is still alive. A running row whose pid is
dead is closed with `ended_at` at the last evidence (transcript mtime or
last usage/sample), never left running.

| Source | Running rule |
|---|---|
| `dagster` | Live only while in the latest successful active set (`QUEUED`, `STARTING`, `STARTED`, `CANCELING`, limit 200). Each successful poll reconciles: stored `running`/`queued` rows absent from the active set are fetched by id (read-only `runIds` filter, batched) and stored with their real terminal status, verdict, and `endTime`. A full active page skips close-out; a failed poll closes nothing. A run Dagster no longer knows closes as `finished`/`UNKNOWN` with `ended_at` at the last seen time. |
| `claude-main` | Live only while the registry says `busy` AND the pid is alive AND the transcript was written within `running_mtime_s` (15 min). A dead pid closes the row at the last transcript/registry evidence. An idle-but-alive interactive session (no transcript write for longer than the window) is not running: it stays out of Running now until it writes again. A stored `running` lead row whose pid is still alive but whose newest source evidence is older than `STALE_IDLE_S` (6 h) is that same intended idle state, not a stale row: it stays hidden from every view, is never a `stale running` mismatch, and is never closed, so a later transcript write returns it to Running now. Lead sessions are never agents; `live_agents` is unchanged. |
| `claude-subagent`, `claude-workflow` | Live only while the transcript was written within `running_mtime_s` and the last event is not a result. A quiet file is `done`, never left running. |
| `muse`, `glm` | Live only while a live process matches pid, `[label:]`, prompt-file label, or session uuid (`process.match_agent`). A new row with no match opens as `unknown`, never `running`. |
| `grok` (cursor-agent) | Live only while a live `cursor-agent` process carries the run's pid or `[label:]`. A chat whose process is gone is `done` (or `unknown` without a timestamp). |
| `shell` (exec) | Live only while the out file grew since the previous poll or a live label was sampled within `process_ttl_s` (30 s). Out growth and the live label are the evidence; rows carry no pid. |
| `generic` (`generic.jsonl`, `agent_dash.py report`) | No pid. Live from `report start` until `report finish`; each `report usage` line is fresh evidence. A run with no line for longer than `STALE_IDLE_S` (6 h) is `stale running`: hidden from every view and reported by `check`. Long runs send `report usage` as a heartbeat. |
| `manifests` (muse/glm/grok rows) | Same as the tool rows above: `running` only on out growth or a live label; otherwise `done`/`unknown`. A terminal row ends at the last time it was seen alive (usage event or process sample) or the out/err/report file mtime, whichever is later — never the start time. |

Model on every run. A usage-line model always wins. Until one arrives, a run takes the launcher manifest's model column (`cursor-sessions.tsv` carries it fourth, like `glm-sessions.tsv`), then the live worker's `--model` flag value. Ended runs with no usage model keep `model not recorded`.

One row per cursor run. A launcher manifest row (`grok:<id>:cursor/<label>`) and the Cursor chat row (`grok:<chat-uuid>:<label>`) with the same label starting within 15 minutes are one run: the store folds the manifest row into the chat row (earliest start, model, manifest out path) and deletes it, on the next cursor poll. The fold records a durable alias (manifest id to chat id) in the same transaction; later writes for an aliased id (restart TSV re-reads, usage, samples, offsets) gap-fill the chat row instead of recreating the manifest row, so merged rows stay merged across restarts. Views already drop the manifest twin (ten-minute window). The `duplicate run` rule: a manifest row the merge would fold (a same-label non-manifest twin starting within 15 minutes) is skipped because the next merge pass folds it; only a surviving manifest row with an overlapping same-source twin the merge would not fold is a reconcile mismatch.

Exact process matching. A live process matches a run by pid first, then by session id, then by prompt-file or label. The label leg goes to the newest open row with that label only: a relaunched run reuses the prompt file under a new session id, and the dead older row is closed instead of staying running.

Dead-pid grace. The stale check ignores a dead-pid row whose last evidence (start, usage, or sample) is within two poll intervals of its source — 4 s for the 2 s tool sources — because the source closes the row on its next poll. Rows dead longer than that still fail the check.

Unseen-worker grace. The unseen-worker check ignores a live labelled worker younger than two poll intervals of its source — 4 s for the 2 s tool sources — by process age, or first sampled by this dashboard within that window (a fresh dashboard has only just met it, e.g. ramp-up transients). Older, longer-seen workers with no running row still fail the check.

One filtered running set backs every view: the snapshot `running` list,
`counts.live_agents`, `/api/workstreams` `live_agents`, and the timeline's
open bars (`/api/runs`, `/api/concurrency`) all derive from
`Store._live_open_ids_locked`, so no view can show a run another hides.
Production jobs are the one source kept out of every agent list (see One number, one truth).
One lane, one agent: when a `claude-workflow` wrapper row and a worker row
(`muse`/`glm`/`grok`) share a label and the worker row is live, the lane
counts once — the worker. The wrapper id leaves the running set (the
worker carries it); Running now shows the worker with the wrapper run id
as its parent (via the workflow step, a Haiku wrapper), while the wrapper
row stays visible in the drawer and on the timeline (marked
`lane_shadow`, so the strip counts the lane once). With no live worker
row for the wrapper's label (still launching, or the worker died), the
wrapper counts as the lane itself.
`/api/reconcile` (and `agent_dash.py check`) reports every running row with
no liveness evidence as a `stale running` mismatch: a dead pid, a Dagster
run missing from the fresh active set, or no source evidence (usage,
process sample, start) for longer than 6 h (`STALE_IDLE_S`). The one
exception is an alive-but-idle `claude-main` lead (pid alive, quiet for
longer than 6 h): intended state, never a mismatch, never closed. A stale row,
and an idle lead, is excluded from all four views at once.

Each bundle:

| File | Role |
|---|---|
| `bundle.json` | `{id, title, short, order, kind}`. `kind` is `program` or `workstream`. The id matches the directory name. |
| `plan.json` | Decision record: `dag`, `decisions`, `motivation`, `agent_friendly`, `narrative`. Task status is `todo`, `pr`, `prog`, `done`, or `deferred`. `deferred` may set `moved_to`, `note`, and `received_as`. A task may carry `deps`, `dep_notes` (conditions keyed by dep id), `kind` (`task` or `gate`; see SKILL-PLAN.md), and `files` (declared footprints); a gate may carry `signal`, `confirmed_at`, and `fresh_hours`. `ws validate` rejects a bad `kind`, a `dep_notes` key that is not one of the task's `deps`, a bad `files`/`signal`/`confirmed_at`/`fresh_hours`/`received_as`, and a bare number citation. A `program` bundle may hold only `narrative`. |
| `page.html` | Plan-view fragment. Generated for bundles that have `visuals.html` and `layout.json`. |
| `approvals.json` | Optional. `{ws, decided_at, decided_by, items: [{id, step, status, reversible, conditions}]}`. `status` is `pending`, `approved`, or `denied`. `reversible` is a boolean. `decided_at` is an ISO-8601 UTC string or null. |
| `visuals.html` | Hand-made sections keyed by h2 title. |
| `layout.json` | Which visual sections land in Why, End state, Plan, Agent-friendly, and Reference. |
| `START-PROMPT.md` | Optional. Paste-ready prompt that starts this workstream's lead. `ws new` writes a template. |

`dash/planrender.py` `render(bundle_dir)` writes `page.html` in this order: Approvals, Why, End state, Plan, Progress, Decided, Agent-friendly, Reference. Approvals is an empty `data-plan-approvals` node. Progress is an empty `data-plan-progress` node. The page fills both from the live bundle. Deferred tasks stay on the map and leave the counts.

```
python3 agent_dash.py ws list
python3 agent_dash.py ws new <ID> "<title>"
python3 agent_dash.py ws validate <ID>
python3 agent_dash.py ws render <ID>
python3 agent_dash.py ws plan <ID>
python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."] [--force]
python3 agent_dash.py ws approve <ID> <item-id> <approved|denied|pending> [--note "..."] [--step "..."] [--reversible yes|no] [--conditions "..."]
python3 agent_dash.py ws approvals [<ID>]
```

`ws plan` lists the bundle's task ids, titles, statuses, and deps.
Exit codes are 0 ok, 1 check failed, 2 usage error, 3 not found, 4
invalid data, 5 dashboard unreachable (docs/integrate.md, For agents).

`ws new` writes a valid template. `ws task` writes `plan.json` through a temp file and `os.replace`, holding a per-bundle `fcntl` lock across the read-modify-write so concurrent writers keep every update. `ws approve` writes `approvals.json` the same way. Moving a `done` or `deferred` task back to `wip` or `pr` needs `--force`. A new pending item needs `--step`, `--reversible yes|no`, and `--conditions`. `approved` or `denied` sets `decided_at` and `decided_by` to `owner`. `ws approvals` prints a table. `ws validate` checks the schema, including `approvals.json` when that file exists, and the naming rule: a TU-style id, a 3-to-5-digit issue number, or an ADR number with no phrase earlier on the same line is a failure.

HTTP, still GET only, 127.0.0.1:

- `GET /plan` serves the same shell as `GET /`.
- `GET /api/bundles` returns `{bundles: [{id, title, short, order, kind}]}`, program first, then `order`.
- `GET /api/bundle?ws=<ID>` returns `{bundle, plan, progress, approvals, start_prompt, issues}` from that directory. `progress` is the same object `/api/plan` carries. Missing `approvals.json` is an empty item list. `start_prompt` is the text of `START-PROMPT.md`, or null. `issues` is the same list `/api/plan` carries.
- `GET /api/plan?ws=<ID>` includes `start_prompt` the same way when that bundle has the file, and null otherwise.
- `GET /api/approvals?ws=<ID|all>` returns one record, or `{approvals: [...]}` for `all`.
- `GET /bundle/<ID>/page.html` returns that file only. The resolved path has to stay inside the bundle directory.

`static/vendor/mermaid.min.js` is mermaid 11.16.1. The plan page runs it on `pre.mermaid`. No CDN.

Run labels for a task are `ws<N>-<task-id>-<slug>`. `SKILL-PLAN.md` is the short how-to. `fixtures/bundles/` is a synthetic OVERVIEW and one workstream for tests.

## Worker registry

`workers.example.json` is the shipped registry. `workers.local.json`, when present, replaces it. `AGENT_DASH_WORKERS` replaces both. Resolution is `dash/locate.py` `layered_path`. `scripts/run_worker.py` reads the file. `scripts/run_worker.sh` execs that script.

```json
{
  "default": "<worker name>",
  "auto_order": ["<worker name>", "..."],
  "cooldown_minutes": 30,
  "workers": {
    "<name>": {
      "enabled": true,
      "kind": "muse | cursor | grok-build",
      "model": "<model id>",
      "extra_args": [],
      "cap": 25,
      "pgrep": "<bracket-guarded pattern>",
      "manifest": "<name>-sessions.tsv",
      "columns": ["label", "session_id", "epoch"],
      "quota_patterns": ["rate limit", "quota", "429", "usage limit"],
      "cost_rank": 1
    }
  }
}
```

`kind` selects the launch, not the model string. `muse` runs `muse exec` with `--model`, `--workspace`, `--prompt-file`, `--session-id`, then `extra_args`. `cursor` runs `scripts/launch_grok.sh` so billed tokens are logged. `grok-build` runs `grok -p` with the prompt, a run rule appended, `-m`, `-s`, `--permission-mode bypassPermissions`, `--cwd`, and `--output-format plain`. `columns` is the manifest row. `glm-sessions.tsv` uses `label`, `session_id`, `epoch`, `model`. `manifests.py` and `grok_cli.py` read that shape. `cost_rank` is a lower-is-cheaper hint. `auto` follows `auto_order`, not `cost_rank`.

`run_worker.py <tier|default|auto> <label> <workspace> <prompt-file> <run-dir> [max-wait]` prints `STATUS: running|done|failed out=... err=... tier=<name>`. It records the chosen worker in `<run-dir>/<label>.tier` and the pid in `<run-dir>/<label>.pid`. A later call for that label only waits. Before launching, it records `<run-dir>` in `<state-dir>/run-dirs.txt` (one absolute path per line, deduped, atomic; honours `AGENT_DASH_STATE_DIR`), so the manifest sources scan that folder wherever the lead put it. `default` uses the registry `default`. `auto` uses the first enabled worker in `auto_order` that is under `cap` (live count from `pgrep`) and not in cooldown. A failed run whose stderr or report matches `quota_patterns` cools that worker down for `cooldown_minutes`. Cooldowns live in `worker-cooldowns.json` in the agent-dash state dir. If the request was `auto` or `default`, the runner archives the report to `out-<label>.<tier>.md` and launches the next eligible worker. Registered folders that no longer exist are skipped; folders untouched for 14 days are dropped from the file (capped at 256 entries). A harness that writes manifests elsewhere registers its folder with one line in `run-dirs.txt`, or with `agent_dash.py report --run-dir <dir>`.

## Generic reporting (any harness)

`generic.jsonl` in the state directory is the harness-neutral drop file.
Each line is one JSON object with `type` `run` or `usage`. The generic
source tails it with byte offsets like every other source. Labels only:
the schema has no field for prompts, argv, secrets, or environment.

Run line. `label` is required. `run_id` defaults to
`generic:<session_id>:<label>`, or `generic:<label>` with no session.
`status` defaults to `running`. `tier` defaults from the model family,
else `generic`. `started_at`/`ended_at` are epoch seconds; a terminal
status without `ended_at` stamps now. Workstream attribution reads the
label through the map file, the same as every source.

```json
{"type": "run", "label": "ws4-WS4.1-notes-index", "status": "running",
 "model": "my-model", "session_id": "abc", "started_at": 1700000000.0}
{"type": "run", "label": "ws4-WS4.1-notes-index", "status": "done",
 "verdict": "PASS", "session_id": "abc", "ended_at": 1700000060.0}
```

Usage line. `session_id`, `tool`, and `message_id` are required by the
store (upsert on that triple, last wins). `run_id` should match the run
so tokens join its workstream. `model` names the price row; a model with
no input and output rate stays unpriced, never zero.

```json
{"type": "usage", "session_id": "abc", "run_id": "generic:abc:ws4-WS4.1-notes-index",
 "tool": "generic", "message_id": "turn-1", "model": "my-model",
 "input": 100, "output": 50, "cache_read": 0, "cache_write": 0, "reasoning": 0}
```

Helper. `python3 agent_dash.py report` appends one valid line:

```
python3 agent_dash.py report start --label <label> [--model M] [--session S] [--run-id R] [--tier T] [--run-dir <dir>]
python3 agent_dash.py report finish --label <label> --verdict PASS [--session S] [--run-id R] [--status done]
python3 agent_dash.py report usage --label <label> --input N --output N [--model M] [--session S] [--run-id R]
```

`--run-dir` registers a manifest folder the harness writes elsewhere, so the manifest sources scan it (same as one line in `run-dirs.txt`).

`finish --status` is `done`, `killed`, or `died_early`. `finish` without
`--model` keeps the model from `start`. Verdicts are `PASS`, `PARTIAL`,
`BLOCKED`, `APPROVE`, `APPROVE_WITH_CHANGES`, `APPROVE_WITH_NITS`, or
`REQUEST_CHANGES`. The live-agents tile counts the `grok`, `muse`,
`glm`, and `claude` buckets; a generic run shows in the running table,
on its task chip, and in tokens and cost either way.

## Checks

Optional, on a machine that already has sessions. Compare one session's transcript totals to that machine's own usage ledger within 0.5%. Peak concurrency over the same window should land within 10% of a hand count. Collector CPU under 10% of one core averaged over 5 minutes. RSS under 300 MB. Real-session smokes stay skipped unless `AGENT_DASH_SMOKE=1`.
