# agent-dash contract (v1)

Local execution dashboard, read-only except the page archive action. One Python 3 process (stdlib only), binds 127.0.0.1:8765.
Code is this directory. `install.sh` links it to `~/.claude/tools/agent-dash`. State: `~/.local/share/agent-dash/` (dash.sqlite WAL, pidfile, log).
Never writes to any source, never signals agent processes, never stores prompt bodies or argv.
Lifecycle controls from the page (`POST /api/workstreams/archive`, `POST /api/workstreams/restore`, `POST /api/workstreams/state`, `POST /api/workstreams/bulk`) are the only writes the dashboard performs, and they exist so a workstream can move through its lifecycle without a terminal round-trip: they write only bundle lifecycle fields (`state`, `state_changed_at`, `archived_at`, the frozen `summary.json`) through the same `dash/lifecycle.py` functions the CLI uses, never sources, plan content, or approvals, and they stay local-only (Host/Origin plus `X-AgentDash-Action: 1`) with a read-only switch (`AGENT_DASH_READONLY=1`, config `readonly: true`).
The one argv value ever kept is the worker's `--model` flag value, recorded on process samples and live-process rows so a run whose model is otherwise unknown can be named. Model ids, labels, token counts, and the session `ws_hint` (a workstream id derived from a bundle tool-use path, never served by any endpoint) are the only worker-derived strings in the store.

## Layout and file ownership (one writer per file)

| Path | Owner |
|---|---|
| `agent_dash.py` (CLI: `run`, `stop`, `restart`, `deploy`, `rollback`, `status`, `state`, `doctor`, `backfill --session <id>`, `reprice`, `reconcile`, `check`, `ws`, `project`, `report`; global `--project <id>` for `state`, `check`, `doctor`, `ws ...`, `status`) | core |
| `dash/state.py` (`GET /api/state`), `dash/doctor.py` (`doctor`), `dash/errors.py` (exit codes, messages) | cli |
| `dash/dataroot.py` (data-root resolution: `data_root`, `state_dir`, `workstreams_dir`, `hub_dir`, `project_file`; the `Storage` seam, local only) | core |
| `dash/project.py` (project descriptor: `project.json` read/validate, `project` CLI, `GET /api/project` payload) | core |
| `dash/projects.py` (multi-project registry: `projects.json` read/write, `project add|list|launch|stop|open|remove`, `GET /api/projects` payload, `--project` env resolution) | core |
| `dash/store/` (facade: `Store` with the same public API over concern mixins) | core |
| `dash/store/base.py` (connect, schema, migrations, offsets, doctor persistence) | core |
| `dash/store/runs.py` (run/session upserts, merge aliases, attribution glue, run reads) | core |
| `dash/store/usage.py` (usage events, buckets, pricing application, usage by run) | core |
| `dash/store/running.py` (running set, liveness, stale/idle/unseen logic) | core |
| `dash/store/summarize.py` (snapshots, rollups, reconcile, mismatches) | core |
| `dash/store/queries.py` (state, workstream, concurrency, session reads) | core |
| `dash/workstates.py` (bundle lifecycle states, `?state=` scopes, archive cards) | core |
| `dash/decisions.py` (hub decisions/risks parser, computed risks; read-only, cached by file mtime) | core |
| `dash/projectfeed.py` (what-changed feed builder behind `GET /api/project/feed`; pure `build_feed` over histories, approvals, and runs) | feed |
| `dash/store/util.py` (shared constants, pure helpers) | core |
| `dash/buildinfo.py` (build id + process start time) | core |
| `dash/deploy.py` (clean-release deploy, rollback, `releases/`) | core |
| `dash/server.py` (HTTP + SSE, routes below) | core |
| `dash/actions.py` (the page write actions: guarded `POST /api/workstreams/archive|restore|state|bulk` through the CLI lifecycle functions, local-only, read-only switch; `allowed_actions_for` names each row's legal buttons) | core |
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
| `static/index.html` (page shell, markup, CSS, script tags, wiring, boot, drawMain registry loop) | Claude (parent) |
| `static/cards/tiles.js` (headline stat tiles) | Claude (parent) |
| `static/cards/token-chart.js` (tokens-per-hour stacked bars) | Claude (parent) |
| `static/cards/live-chart.js` (live-agents-over-time strips) | Claude (parent) |
| `static/cards/timeline.js` (run timeline gantt + shared run-classify helpers) | Claude (parent) |
| `static/cards/tables.js` (running and finished run tables) | Claude (parent) |
| `static/cards/ws-cards.js` (Where we are, workstream cards, planSum cache) | Claude (parent) |
| `static/cards/plan-progress.js` (plan progress map, task map, replay) | Claude (parent) |
| `static/cards/registry.js` (ordered card list: adding a card is one file plus one line; scopes `overview\|workstream\|both\|project\|planned`) | Claude (parent) |
| `static/cards/project-home.js` + `project-home.css` (Project home `?view=project`: phases, tiles, Current, Planned shelf, Recently done, chart slots) | Claude (parent) |
| `static/cards/planned.js` + `planned.css` (Planned view `?view=planned`: start-readiness table, gated-but-live, dependency-graph slot) | Claude (parent) |
| `dash/projectgraph.py` (pure cross-workstream graph builder for `GET /api/project/graph`: nodes, depends_on/gate/moved edges, cycles, depths) | depgraph |
| `static/cards/depgraph.js` + `depgraph.css` (dependency graph card on `?view=project` and `?view=planned`, `exec-depgraph` slot on both) | depgraph |
| `static/cards/archive.js` + `archive.css` (Archive shelf `?view=archive`: frozen summary cards, lifetime totals, read-only plan banner style) | Claude (parent) |
| `static/cards/decisions.js` + `decisions.css` (read-only decisions mirror on the Project home, scope `project`, unslotted: reads `GET /api/decisions`, hides entirely when the endpoint is absent or `source.present` is false) | feed |
| `dash/projectfeed.py` (pure feed builder for `GET /api/project/feed`: page API only, no card) | feed |
||||||| parent of 628bd9d (feat(dash): project roadmap swimlane with focus-only dependency connectors)
| `dash/projecttimeline.py` (pure timeline builder + project reader + the `/api/project/timeline` collector: page API only, no card) | core |
||||||| parent of 245220a (feat(dash): project burn-up with an honest history gap, and cost and token burn by workstream)
| `dash/projectburn.py` (`GET /api/project/burnup`: recorded task burn-up plus per-day cost/token burn, UTC days, honest history gaps) | dash-c3 |
| `static/cards/burnup.js` + `burnup.css` (Project burn-up `?view=project` slot `exec-burnup`: tasks area with plan line and gap band, stacked cost/token bars, togglable lanes) | dash-c3 |
| `dash/phases.py` (usage-by-phase classifier: explicit `ws<N>-plan/exec/review/coord` marks, review affixes, plan task matches, planning/review/doing/coordination words, unmapped lead sessions, the pre-task planning window; rules in `catalog/phases.json` with a `phases.local.json` owner override beside `project.json`; `unclassified` is its own bucket) | dash-phases |
| `catalog/phases.json` (shipped phase rules, table-driven and ordered; first match wins; labels only, never prompts) | dash-phases |
| `dash/projectphases.py` (`GET /api/project/phases`: per-workstream and total tokens/cost/agents/runs/hours by phase plus cumulative-by-day series, cached by scope/seq/rules-hash/ignore/plans/taskmap) | dash-phases |
| `static/cards/phases.js` + `phases.css` (Usage by phase `?view=project` slot `exec-phases` right after burn-up, plus the compact workstream-tab variant: stacked bars, tokens/cost toggle, takeaway sentence, cumulative cost by phase over time) | dash-phases |
| `static/core/el.js` (DOM helpers, text/format glue, tooltip, copy button) | Claude (parent) |
| `static/core/state.js` (page state, route read/write, tab filter/order plumbing) | Claude (parent) |
| `static/core/api.js` (fetch and fixtures, SSE stream, refresh orchestration; `apiPost` for the page write actions with the action header) | Claude (parent) |
| `static/core/actions.js` (archive/restore controls: inline confirm popover, undo toast, real buttons with workstream aria-labels, copy-command fallback) | Claude (parent) |
| `static/core/switcher.js` (the calm pinned bar: Overall, the selected chip, one `Workstreams (N)` switcher button with a searchable state-grouped popover, up to three viewer-local pins, Sections, Theme) | Claude (parent) |
| `static/core/manage.js` (the Current management table, the tab heading cluster, confirms, undo toasts, Keys popover; every write through `apiPost`) | Claude (parent) |
| `static/cards/manage.css` (switcher popover, management table, heading cluster, phone sheet) | Claude (parent) |
| `static/core/cache.js` (exec/section keyed caches, keyed-section renderer) | Claude (parent) |
| `static/core/charts.js` (shared chart/axis and token-cell helpers) | Claude (parent) |
| `static/core/drawer.js` (run details drawer) | Claude (parent) |
| `static/core/project.js` (project header/title from `GET /api/project`, once per load and on focus; the header switcher menu from `GET /api/projects`, shown only with 2+ projects) | Claude (parent) |
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

`store` methods (implemented by core in `dash/store/`: the facade `Store` holds the single lock and connection; concern mixins `base`, `runs`, `usage`, `running`, `summarize`, `queries` expose the logic, `util` the shared constants and pure helpers):

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
A source may also expose `health_extra() -> dict`, merged into `/api/health` under its own name (Dagster reports `skipped_closeouts` this way).
Sources must be incremental (byte offsets), budget-bounded, and exception-safe (log, never crash the loop). Directory listings are cached by (inode, mtime) with a per-pass cap on fresh listings, so repeat polls over a quiet tree cost stats, not walks. Dead offset rows (absolute paths that no longer exist) are pruned a batch per reconcile tick with one VACUUM per pass; only the newest 3 `*.bak-*` stay in the state dir.

Enums. `source`: claude-main, claude-subagent, claude-workflow, muse, glm, grok (cursor-agent), shell, generic.
`tier`: opus, sonnet, haiku, muse, glm, grok. The generic source passes its tier through and defaults to `generic` (from the model name when it holds a known family). `status`: running, done, killed, died_early, unknown.
`verdict`: PASS, PARTIAL, BLOCKED, APPROVE, APPROVE_WITH_NITS, APPROVE_WITH_CHANGES, REQUEST_CHANGES, or null.
`clock_source`: transcript, muse_log, manifest, mtime. Times are epoch seconds (float), UTC.
`run_id` is stable: `<source>:<session-or-uuid>:<label-or-agent-id>`.

Workstream attribution happens in core (`attribution.py`, store binding in
`dash/inherit.py`), not in sources: label via the map file, exact then regex
(`^ws(\d+)` -> WSn, `WS(\d+)\.` -> WSn, `^g-` -> WS1, `^dash-` -> TOOLS), then session default from the map
`sessions` object, else null. `map.local.json` replaces `map.example.json` when present. Store `workstream` and `ws_confidence`
(`label`, `regex`, `session-default`, `inherited`, `inferred`).

Attribution rules. (a) A run with an explicit label, map, or regex
workstream keeps it with its confidence unchanged. (b) A subagent or
workflow step with no workstream inherits its parent session's
workstream with confidence `inherited`. (c) A lead session's own
workstream is inferred, in order, from: the dominant workstream of the
workers it launched in the last 6 h (at least 60% share and at least 3
runs), then the git worktree path or branch in its cwd matching a
bundle's known worktree or branch naming (bundle plan trains' branch
names, or a `ws<N>/` branch prefix or path segment), then a workstream
bundle directory it wrote to (a Write/Edit tool-use path under a
bundle dir seen in its transcript tail; the source scans only bytes it
already read and keeps just the bundle id in the session's `ws_hint`).
Otherwise the lead stays unassigned: a lead running several
workstreams keeps none unless one share clears 60%. (d) Inference is
recomputed each poll and may change; it never overwrites an explicit
attribution. The page and reconcile count `inherited`/`inferred` runs
like any workstream row, and every workstream row exposes
`ws_confidence` (the distinct confidences behind it) so agents can see
which rows are inferred.

## HTTP API (all GET, JSON, 127.0.0.1 only)

API errors are JSON `{error, code, hint}`; `hint` names the next
command to run. CLI errors follow the same rule, with exit codes 0
ok, 1 check failed, 2 usage, 3 not found, 4 invalid data, 5
unreachable (docs/integrate.md, For agents).

`GET /` -> `static/index.html`.

`GET /api/snapshot?ws=<WSn|all>&since=&until=&usage_group=<model|workstream|tool>&recent_limit=&recent_offset=`
```json
{"now": 0, "seq": 0,
 "counts": {"live_agents": 0, "live_by_tool": {"grok": 0, "muse": 0, "glm": 0, "claude": 0}, "prod_jobs": 0, "prod_jobs_finished": 0, "finished_today": 0, "finished_in_range": 0, "finished_by_verdict": {}},
 "running": [Run], "recent": [Run],
 "totals": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0, "unpriced_runs": 0, "unpriced_tokens": 0, "unpriced_models": []},
 "prev_24h": {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0},
 "totals_all_time": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0},
 "last_activity": 0,
 "by_workstream": [{"ws": "WS7", "live_agents": 0, "finished_in_range": 0, "finished_today": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "last_activity": 0, "ws_confidence": ["regex"]}],
 "usage": [{"t": 0, "key": "opus", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost_usd": 0.0}]}
```
`usage` is present only when `usage_group` is given: the hourly usage
buckets for the same scope and window, computed under the same lock pass
as `totals`, so the bucket token sum equals `totals` by construction. A
bad `usage_group` is a 400. With `usage_group=workstream` the all-scope
response folds ignored ids into one `"Other"` key, so the stacked chart
still sums to the legend Total; the page paints it neutral
(`var(--other)`), never an entity colour.
`totals` is the windowed scope total; `totals_all_time` is the same scope with
no window, summed by the same helper. `last_activity` is the scope's newest
run stamp, or null when the scope has no runs. The page tiles read these
fields directly and never re-sum buckets on the client.
`prev_24h` (`{tokens, cost_usd, finished, live_agents}`) is the same scope's
numbers for the 24 hours immediately before the window (`[since - 24 h,
since]`; the default window makes that `[now - 48 h, now - 24 h]`), computed
with the same bucket sums and the same agent rule as the windowed totals, so
a delta compares like with like. `tokens` is billed tokens, `cost_usd` the
priced cost under the same unpriced rule, `finished` the finished-in-range
count for that earlier window (agent runs only: lead sessions, shell rows,
and Dagster jobs are not agents), and `live_agents` the point-in-time count
of agents overlapping the window start (`now - 24 h` by default), not a peak.
Each `by_workstream` row carries its own scope's `prev_24h`. The field is
additive: no existing field changed, and the snapshot cache keys it with the
same entry and invalidation as the rest. Retention caveat: when data older
than the window is pruned, `prev_24h` reads as whatever the database holds.
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
`?recent_limit=&recent_offset=` pages that window (defaults 50 and 0, the historic shape): `recent` carries rows `[offset, offset+limit)` newest-first, `recent_shown` their count, and `recent_total` the true window total on every page, so later pages are fetchable without growing the default payload. The snapshot cache and the conditional ETag key both carry the page, and a bad value is a 400. The page pager reads `recent_total` (falling back to `counts.finished_in_range`) and fetches beyond the held 50 only.

`GET /api/stream` -> SSE. Event `tick` every 2 s: `{"seq": n, "running": [Run], "sources": [{name, last_poll, lag_s}]}`; the page refetches snapshot when `seq` changes. `sources` mirrors the registry freshness so the page can show staleness per source.

`GET /api/runs?ws=&since=&until=&limit=&include=` -> `{"runs": [Run]}` (Gantt input; workflow children collapsed into one row with `children: n`). Production (dagster) rows are left out by default; `?include=jobs` adds them back for tools that need them (anything else is a 400).

`GET /api/usage?group=model|workstream|tool&bucket=hour|day&ws=&since=&until=` -> `{"buckets": [{"t": 0, "key": "opus", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost_usd": 0.0}]}`.
With `group=workstream`, buckets for ids on the project's
`ignore_workstreams` list fold into one `"Other"` key (cells are
renamed, not merged; consumers already aggregate by key), so the
per-workstream split still sums to the all-scope total.

`GET /api/concurrency?ws=&since=&step=60` -> `{"points": [{"t": 0, "grok": 0, "muse": 0, "glm": 0, "claude": 0}]}`

`GET /api/sessions` -> `{"sessions": [{session_id, tool, cwd, label, workstream, started_at, status, runs: n, tokens, cost_usd}]}`

`GET /api/workstreams?since=&until=&state=` -> `{"workstreams": [{ws, name, tk: {backlog, todo, doing, review, done, cancelled}, state, state_changed_at, archived_at, depends_on, milestone, live_agents, finished_in_range, finished_today, tokens, cost_usd, tokens_all_time, cost_usd_all_time, last_activity, plan: bool, ws_confidence: [..], progress: {done, total, counts, pct} | null}]}`.
Rows come from the same rollup as the snapshot, so they match the tabs for the
same window, plus one zero-activity row per workstream bundle with no run
or board row (so a planned bundle with no runs still appears with its
bundle state). `tk` is the hub issue board (issue counts, never plan progress).
`progress` is the same object `/api/plan` carries for that workstream, resolved
through the one plan resolver (`dash.bundles.resolve_plan`), so one request
gives the tab bar its completed state; it is null when the workstream has no
plan. An `unassigned` row appears when runs have no workstream.
`state` is the bundle lifecycle state (`planned|active|paused|done|archived|dropped`,
default `active` when the bundle carries none), with `state_changed_at`,
`archived_at`, `depends_on`, and `milestone` beside it. The default scope drops
`archived` and `dropped` rows; `?state=all` keeps every state and
`?state=archived,done` (any comma list of state names) keeps exactly those. A
bad state name is a 400. The existing live/progress fields stay on every row
returned, and the `unassigned` row stays in every scope with null lifecycle
fields (it is not a workstream). Rows for ids on the project's
`ignore_workstreams` list never appear here (nor in the all-scope
snapshot `by_workstream`, the Project home "Active without a plan"
strip, the "Where we are" list, the switcher, or any lane); their runs
still count in the all-scope totals.

`POST /api/workstreams/archive` and `POST /api/workstreams/restore` (JSON
body `{"ws": "<ID>"}` and nothing else) are the only writes the server
accepts; every other POST is a 405. They run the same `dash/lifecycle.py`
`archive_bundle`/`restore_bundle` the CLI uses (frozen `summary.json`,
`state` `archived`, `archived_at`, atomic temp-file-plus-replace writes
under the per-bundle file lock), under the bundle root only, and answer
`{"ok": true, "ws": "<ID>", "state": "archived", "archived_at": 0}` (restore
answers `state: done`, `archived_at: null`). Errors keep the house shape
`{error, code, hint}`: unknown workstream 404 `not_found`, program/`OVERVIEW`
kinds, already-archived (archive), non-archived (restore), dropped, or an
unfinished plan or missing closing report 409 `not_eligible` (archiving an
archived workstream is a 409, never a crash), bad body 400 `bad_request`.
A bundle that moved between the eligibility check and the write lock is
409 `changed` (reload and retry); a non-JSON Content-Type is 415
`unsupported_media_type`; a failed write is 500 `write_failed` (or
`internal`), never a dropped connection.
Guards, all fail-closed: `Host` must be `127.0.0.1:<port>` or
`localhost:<port>`; a present `Origin` must match that local origin; every
call must carry `X-AgentDash-Action: 1` (blocks cross-site form posts and
simple fetches). Disabled with 403 `read_only` when `AGENT_DASH_READONLY=1`
or the config carries `readonly: true`; then `GET /api/project` and
`GET /api/state` report `{"actions": {"archive": false}}` and every page
action button falls back to a copy-command button. Each action is logged
to the dashboard log with the workstream id only, and invalidates the
bundle-state caches (store sequences plus snapshot cache, the server
per-plan caches), so the next poll lists the workstream in its new state.

`POST /api/workstreams/state` (JSON body `{"ws": "<ID>", "state":
"<target>"}` plus an optional `"note"`) moves one workstream between
`planned`, `active`, `paused`, `done`, and `dropped` through the same
`set_state` the CLI uses: the transition table decides, reopening `done`
to `active` needs a note (the page sends one from Undo), `state_changed_at`
and one history line are recorded under the per-bundle lock, and
`archived` is refused here (archiving freezes a summary, so it stays on
the archive endpoint). It answers `{"ok": true, "ws": "<ID>", "state":
"<new>", "prev_state": "<old>", "state_changed_at": 0}` so the page can
Undo by posting the reverse transition. Errors keep the house shape:
unknown workstream 404 `not_found`, program/`OVERVIEW` kinds 409
`not_eligible`, illegal move 409 `invalid_transition` with a hint listing
the allowed next states (and an `allowed` list), bad body 400
`bad_request`.

`POST /api/workstreams/bulk` (JSON body `{"action":
"archive"|"restore"|"state", "state": "<target>" for state moves,
"ws": ["<ID>", ...]}` up to 50 ids, plus an optional `"note"`) runs one
batch through the single-operation paths above: each id is atomic on its
own, one failure never aborts the rest, and the answer is always 200 with
`{"ok": <all ok>, "action": ..., "results": [{ws, ok, state, prev_state |
error, code, hint}], "changed": [<ids>]}`. Successes carry `prev_state`
so one page Undo restores all that changed. The same guards and the
read-only switch apply.

`GET /api/state` exposes `allowed_actions` per workstream row (the page
vocabulary: `start`, `pause`, `resume`, `mark_done`, `reopen`, `archive`,
`restore`, `drop`; `GET /api/workstreams` rows carry the same list), so
the page never offers an invalid button. Program bundles like OVERVIEW
expose no actions anywhere: their rows carry none and every endpoint
refuses them with 409 `not_eligible`.

`GET /api/reconcile?hours=&since=&until=` -> `{"now": 0, "since": 0, "until": 0, "all": {live_agents, finished_in_range, finished_today, tokens, cost_usd, prev_24h: {tokens, cost_usd, finished, live_agents}}, "all_time": {tokens, cost_usd}, "rows": [per-workstream rows], "unassigned": {...}, "archived": {tokens, cost_usd, tokens_all_time, cost_usd_all_time, workstreams: []}, "mismatches": []}`.
`archived` sums the rows in a default-hidden lifecycle state
(`archived`/`dropped`), windowed plus lifetime, so project totals keep
including archived work. One more identity holds and is checked: visible
rows (`active`+`paused`+`done`+`planned`) plus `archived` plus `unassigned`
equal the all-scope numbers (windowed and all-time), with an `archived
...` mismatch line when broken.
Computed in one store pass under the lock. Rows carry `tokens_all_time` and
`cost_usd_all_time` next to the windowed numbers. `mismatches` lists every violated
invariant, windowed and all-time (rows plus unassigned equal the all-scope
numbers, and each row equals its workstream tab), and is empty when consistent.
One more invariant covers the previous window: every row and `unassigned`
carry their scope's `prev_24h`, `all` carries the all-scope `prev_24h`, and
the rows plus unassigned must equal it — tokens to the token, cost to the
cent (`prev_24h_tokens: ...` / `prev_24h_cost_usd: ...` mismatches, reported
like the others).
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
The archive-from-the-page flow has its own check, `node
scripts/archive_action_check.mjs` (boots a temp server with synthetic done
bundles, clicks Archive through Undo and Restore over CDP, takes the
confirm/toast/archive screenshots; run it, don't fold it into `check
--page`: it takes minutes, not seconds).

`GET /api/plan?ws=WS7` -> that workstream's plan with a `progress` object
(plus an `issues` list, the `ws validate` findings, empty when clean; a bad
plan still serves 200 with its issues named, so bad states are never drawn
as tasks; the page does not show the issues, which `ws validate` and this field carry):
`{"ws": "WS7", "cols": [..], "groups": [{id, col, title, tasks: [{id, title, status, deps, kind, dep_notes}]}], "edges": [[a, b]], "phases": [{name, goal, gate}], "progress": {"done": 0, "total": 0, "counts": {todo, wip, pr, prog, done, deferred}, "pct": 0}, "ready": ["WS7.2"], "critical_path": ["WS7.1", "WS7.2"], "max_parallel": 3, "layout": {...}, "map_warnings": [{code, severity, title, detail, fix}]}`.
`ready` is the todo-with-satisfied-deps task ids ranked by critical-path impact, `critical_path` is the longest dep chain, and `max_parallel` is the widest dependency wave; all three come from `dash/orchestrate.py` with no store reads, cached by `plan.json` mtime.
`layout` is the computed map layout from `dash/maplayout.py` (density, per-group collapse state, columns, chip labels, arrows mode, train tags, phases line), and `map_warnings` is the MAP101–MAP111 lint for that plan; both are cached by `plan.json` mtime like the orchestration fields. The page consumes `layout`; `ws layout <ID>` prints the same object without touching the page.
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

`GET /api/health` -> `{"ok": true, "sources": [{name, last_poll, last_error, lag_s, last_duration_s, interval_s, stalled, state, started_at}], "slow_sources": 0, "dagster": {"skipped_closeouts": 0}, "pricing": {...}, "disconnects_1h": 0, "unpriced_models": [{"model", "rows", "tokens"}], "scratch_roots": [...], "run_dirs": [...], "warnings": [...], "build_id": "abc1234", "started_at": 0, "state_hash": "0123abcd"}`.
Each source carries `state`: `starting`, `ok`, `slow`, `stalled`, or `error`, plus `started_at` (the collector process start). A source that never completed a poll — and recorded no failure — is `starting`, not stalled, until max(3x its interval, 90 s) after process start; `ok` stays true while starting, and neither reconcile nor doctor reports a starting source as stalled. Positive failure evidence (an error or a poll-budget overrun) still reports `error` immediately, even on the first poll. A poll that returned but took longer than 15 s is `slow` (counted in `slow_sources`, still ok). `dagster.skipped_closeouts` counts polls that saw a full active page and skipped the close-out pass.

The Dagster poll backs off after ssh failures (timeouts or ssh errors, not GraphQL errors). The first failure keeps the normal 60 s interval; each further consecutive failure doubles the delay (120 s, 240 s) up to `dagster.max_backoff_s` (default 300). Inside the window the poll returns without ssh, `last_error` keeps the last failure, and the source stays `error`. `dagster.backoff_s` reports the current delay (0 when healthy) and `dagster.next_poll_in_s` the seconds until the next attempt (0 when none is pending). Any successful ssh call resets the back-off.
`build_id` is the git short sha of the tool directory plus `-dirty` when the checkout had uncommitted changes at start (`"unknown"` outside git); `started_at` is the process start epoch. Both are captured once at start, never re-read per request. `run` refuses to start when the port answers with a different `build_id` (unless `--force-replace`); `deploy` health-gates the new release on it. `state_hash` is the short sha256 of the server's resolved state dir (never the path); `stop`, `restart`, and `deploy` compare it with the current state dir and refuse a foreign one unless `--yes-live` is passed.
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

`GET /api/doctor` -> the latest persisted doctor run. The server runs the checks in a background thread every `doctor.interval_s` seconds (default 300; first run 30 s after start, after the first full poll) and persists them in the `doctor_problems` table. The endpoint never runs checks inline; `?refresh=1` only queues a run. That flag is the one GET with a side effect: it queues a background doctor run, which writes only the dashboard's own state database (`doctor_problems`), never sources or bundles.
```json
{"generated_at": 0, "next_run_at": 0, "interval_s": 300,
 "summary": {"critical": 0, "warn": 0, "info": 0, "regressed": 0, "items_total": 0},
 "problems": [{"id": "stuck-task:ws:WS4", "code": "stuck-task", "severity": "warn",
   "scope": "ws:WS4", "ws": "WS4", "title": "", "detail": "",
   "fix": {"command": "", "safe_auto": false, "doc": "docs/problems/stuck-task.md"},
   "first_seen": 0, "last_seen": 0, "count": 1, "regressed": false,
   "items_total": 1, "items": [{"title": "", "id": "WS4.1", "since": 0}]}],
 "resolved_24h": [{"id": "", "code": "", "title": "", "resolved_at": 0}]}
```
Per-item checks that can fan out emit one problem per (code, workstream): `dep-jump`, `ghost-runs`, `orphan-worker`, `done-before-deps`, `stuck-task`, `bare-citation`, `model-policy`, `over-cap`. The id is `<code>:ws:<ID>` (workstream) or `<code>:global` (fleet); the title names the workstream and the member count before any ids; the detail lists the worst 3 as title-then-id with `+N more` for the rest; the fix command is the single command that lists all members (`ws next <ID>` or `doctor --ws <ID> --all`, `doctor --all` for global). Severity is the worst member's; `regressed`/`first_seen` track the group. Old per-item ids are never emitted; their rows resolve on the first grouped run (no rewrite needed; `items_json` migrates the table).
Each problem keeps every existing field and gains `items` (up to 20 compact `{title, id, since}` member rows, present only when the problem has members) and `items_total` (the full member count). The summary counts groups and gains `items_total` (present when nonzero). `?ws=<ID>` keeps that workstream's problems plus global ones; `?ws=<ID>&all=1` returns every member row instead of the first 20. `doctor` prints a one-line-per-group digest (severity glyph and word, code, workstream, count, title; top 3 members under critical/warn groups; footer `N groups, M items; doctor --all to expand; doctor --ws <ID>`); `doctor --all` prints every member. `--json` is the API body plus the exit verdict.
`?ws=<ID>` keeps that workstream's problems plus global ones; `?severity=critical|warn|info` is the minimum level shown. A problem absent from a run resolves (`resolved_at` set, kept 14 days); one that reappears after resolution is `regressed`. Critical means the numbers are wrong or the dashboard is blind: source stalled, unseen workers, dashboard down, unpriced model with tokens, reconcile mismatch. Warn is workflow hygiene: stuck task, policy breach, orphan process, stale approval, invalid bundle, a slow-but-returning source poll (`source-slow`), skipped Dagster close-outs (`dagster-closeout-skipped`), and friends. Info is advisory: dirty checkout, run folder outside roots. Every code has a page under `docs/problems/<code>.md`.
`?ws=<ID>` keeps that workstream's problems plus global ones; `?severity=critical|warn|info` is the minimum level shown. A problem absent from a run resolves (`resolved_at` set, kept 14 days); one that reappears after resolution is `regressed`. Critical means the numbers are wrong or the dashboard is blind: source stalled, unseen workers, dashboard down, unpriced model with tokens, reconcile mismatch. Warn is workflow hygiene: stuck task, policy breach, orphan process, stale approval, invalid bundle, and friends, plus the orchestration flags (idle-ready work, dep jumps, stale gates, review spirals, undelegated leads) and a dirty running build (`build-dirty`). Info is advisory: dirty checkout, run folder outside roots, a running build behind `origin/main` (`build-behind`), plus the orchestration advisories (deferred orphans, wave tails, ghost runs). Every code has a page under `docs/problems/<code>.md`.
Orchestration thresholds live under `doctor.*` in config: `idle_ready_minutes` (default 30: a ready queue with fewer than 2 live agents fires once the plan sits unchanged that long), `long_tail_minutes` (default 60: a wave finished except one still-running lane), `fix_rounds_cap` (default 2: distinct `-fixN`/`-revN` rounds on an open task), `recent_hours` (default 24: the recent/old cut for dep jumps and deferred orphans, the ghost-run window, and the no-activity gate — a workstream with no plan, or with no run or plan activity inside it, never fires an orchestration problem; a dep-jump group is `warn` only when it holds a jump started inside the window, else `info`), `ghost_share` (default 0.2: the unmapped-run share that fires ghost-runs).
Lifecycle thresholds live under the same `doctor.*` key: `archive_nag_days` (default 7: a `done` workstream, or an `active` one at 100% of non-deferred tasks, nags past this age without being archived), `active_idle_days` (default 3: an `active` workstream with open tasks and no runs or history moves this long fires), `phase_slip_days` (default 7: a finished phase whose successor has not started this long fires). The lifecycle checks (`done-not-archived`, `active-idle`, `planned-blocked`, `planned-ready`, `planned-live`, `unplanned-activity`) emit one problem per (code, workstream) with the `<code>:ws:<ID>` id and no member rows; `phase-slipping` emits one per phase at global scope (`<code>:global:<kind>:<phase>`). All but `unplanned-activity` are silent for projects without lifecycle data (no lifecycle keys in any `bundle.json` and no `project.json`); absent `state` reads as `active`. `planned-live` fires when a `planned` workstream already has runs (the page shows it under Current with a first-agent hint; the fix is `ws state <ID> active`); `unplanned-activity` fires when runs attribute to an id with no bundle (the fix is `ws new <ID> "<title>"`, or `project ignore add <ID>` for miscellaneous activity). Ids on the project's `ignore_workstreams` list never fire any workstream-scoped finding; rows already stored for them resolve on the next snapshot because they are absent from the run.

`GET /api/state` -> the whole dashboard in one call, computed in one
store pass (`Store.state_view`):
```json
{"schema": 1, "version": {"tool": "<git-sha|unknown>", "build_id": "<sha[-dirty]|unknown>", "started_at": 0, "schema": 1},
 "now": 0, "window": {"since": 0, "until": 0, "hours": 24},
 "health": {"ok": true, "sources": [{name, last_poll, last_error, lag_s}], "scratch_roots": [], "run_dirs": [], "warnings": []},
 "project": {"name": null},
 "decisions": {"total": 0, "open": 0},
 "risks": {"total": 0, "by_severity": {"warn": 0}},
 "workstreams": [{"id": "WS7", "title": "", "state": "active", "plan": true, "progress": {"done": 0, "total": 0, "counts": {}, "pct": 0},
   "live_agents": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "by_phase": {"planning": {"tokens": 0, "cost_usd": 0.0}, "execution": {"tokens": 0, "cost_usd": 0.0}, "review": {"tokens": 0, "cost_usd": 0.0}, "coordination": {"tokens": 0, "cost_usd": 0.0}, "unclassified": {"tokens": 0, "cost_usd": 0.0}}, "last_activity": 0}],
 "unassigned": {"live_agents": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "last_activity": 0},
 "running": {"count": 0, "by_source": {"muse": 0}, "prod_jobs": 0, "prod_jobs_finished": 0},
 "totals": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0, "unpriced_runs": 0, "prev_24h": {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0}, "archived": {"tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "workstreams": []}, "prod_jobs": 0, "prod_jobs_finished": 0},
 "totals_all_time": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0},
 "reconcile": {"ok": true, "mismatches": []},
 "problems": [{"code": "stale-running", "message": "", "command": "python3 agent_dash.py check"}]}
```
`decisions` is the hub decisions summary (`total` rows, `open` rows whose
status is not a closing one such as `accepted`); `risks` is the
`/api/risks` summary (`total` rows, `by_severity` counts). Both read the
same hub files as their endpoints, so the strip and the roll-up agree.
`version.tool` is the git short sha of the tool directory, `"unknown"`
outside git. `version.build_id` adds the `-dirty` flag for a checkout
with uncommitted changes at start; `version.started_at` is the process
start epoch. `project` is the project-identity placeholder (`GET
/api/project` owns it): today `{"name": null}`, filled from
`project.json` by the project worker. Every workstream row carries its
bundle lifecycle `state` (default `active`), and `totals.archived` is the
same archived aggregate `/api/reconcile` carries. The window is the one fixed 24 h. `totals.prev_24h` and each
workstream row's (and `unassigned`'s) `prev_24h` are the snapshot
`prev_24h` for those scopes: the 24 h before the window, same functions
and attribution, `live_agents` point-in-time at the window start.
`progress` is the same
object `/api/plan` carries, or null. `health` carries the same
`scratch_roots`, `run_dirs`, and `warnings` as `/api/health`.
`problems` is the persisted doctor list: the same entries (same ids)
as `GET /api/doctor`, each additionally carrying the legacy `message`
(`title: detail`) and `command` keys. `actions` is `{"archive": bool}`:
whether the page may offer the real archive/restore buttons (false in
read-only mode). Fields are only added, never
renamed.
`python3 agent_dash.py state [--json]` prints the same payload.
`doctor` additionally reports each health `warnings` entry as a
`run-dir-outside-roots` problem.

`GET /api/project` -> the project descriptor plus the computed roll-up:
```json
{"name": "Demo project", "short": "demo", "description": "", "goals": [],
 "started": "2026-10-15", "target": null,
 "phases": [{"id": "p1", "title": "First cut", "summary": "", "gate": ["WS1"],
   "state": "active", "members": [{"ws": "WS1", "state": "active"}]}],
 "links": [], "owner_notes": "",
 "data_root_hash": "0123456789ab",
 "totals_all_time": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0},
 "counts": {"planned": 0, "active": 1, "paused": 0, "done": 0, "archived": 0, "dropped": 0},
 "issues": []}
```
`project.json` lives in the data root (`dash/dataroot.py` `project_file`).
A phase `gate` names member workstream ids (a list) or carries gate text
(a string); `state` is computed, never stored: `done` when every member
is `done`/`archived`, `active` while any member is `active`/`paused`,
`planned` before that, `dropped` when every member is `dropped`. Member
states come from `bundle.json` `state` when present, else `active`.
`totals_all_time` is the same summary the snapshot serves (archived work
included); `counts` tallies bundles by lifecycle state. `data_root_hash`
names the data root without exposing the path. A missing `project.json`
reads `name: null` (the header falls back to "Agent dashboard") with
empty `issues`; a misshapen one still serves 200 with its issues named.
```bash
python3 agent_dash.py project init "Demo project" [--data-root DIR]
python3 agent_dash.py project show [--json]
python3 agent_dash.py project set description "What ships."
```
`init` writes a starter template and refuses to overwrite (exit 2);
`set` takes `name`, `short`, `description`, or `target` (exit 2 on a bad
field, 3 when no `project.json` exists, 4 on invalid data).
`ignore_workstreams` is the project-level ignore list for miscellaneous
workstream ids (`["PLANNING", "TOOLS", "WS0", "WS345"]`); ids match
case-insensitively and a trailing `*` is a prefix (`"SCRATCH*"`).
```bash
python3 agent_dash.py project ignore add <ID...> [--data-root DIR]
python3 agent_dash.py project ignore remove <ID...> [--data-root DIR]
python3 agent_dash.py project ignore list [--json] [--data-root DIR]
```
`ignore` writes only `project.json` (atomic temp-file-plus-rename,
idempotent: adding a present id or removing an absent one still exits
0; 3 when no `project.json` exists). Reads tolerate malformed values:
non-strings and blanks are dropped, a non-list field reads as empty
(validation still flags a non-list field so `project show` reports it).
Ignored ids never produce a
workstream-scoped doctor finding and never appear in a workstream
list, the switcher, or any lane, while their runs still count in the
all-scope totals; charts that must sum to the total fold them into one
muted `"Other"` bucket. `GET /api/project` carries the cleaned list as
`ignore_workstreams`.
`GET /api/project` also carries the instance's project `id` (from
`AGENT_DASH_PROJECT_ID` when launched via `project launch`, else null);
`/api/health` carries the same id as `project_id` (id only, never paths).
Both `GET /api/project` and `GET /api/state` carry `actions`:
`{"archive": bool}`, false when `AGENT_DASH_READONLY=1` or the config
carries `readonly: true` (the page then shows copy-command buttons).

One dashboard instance serves one project: its own data root, state,
port, and name. The registry is `projects.json` (default
`~/.config/agent-dash/projects.json`, override `AGENT_DASH_PROJECTS`,
never inside any data root):
`{"projects": [{"id", "name", "data_root", "port", "added_at"}]}`.
```bash
python3 agent_dash.py project add <id> --data-root DIR [--name NAME] [--port N] [--init] [--json]
python3 agent_dash.py project list [--json]
python3 agent_dash.py project launch <id> [--json]
python3 agent_dash.py project stop <id> [--json]
python3 agent_dash.py project open <id> [--json]
python3 agent_dash.py project remove <id> [--json]
```
`add` defaults to the lowest free port from 8765 up not used by another
registered project (`--init` also creates `project.json` in a missing
data root) and refuses duplicate ids or ports (exit 2). `list --json`
carries the `data_root` basename only (the table shows the full path),
plus `running` (a 1 s `/api/health` probe matched by `state_hash`, so a
different project on that port reads `port taken by another`) and the
`build_id`. `launch` starts the instance exactly as `run` does (with
that project's data root, state dir, port, and project id in the env),
waits for health, and refuses a port serving another state hash (exit
1). `stop` runs the guarded stop with the project's env; `remove`
unregisters only and never touches data. Agents working on a project
pass `--project <id>` (or set the env) for `state`, `check`, `doctor`,
`ws ...`, `status`.
`GET /api/projects` lists the registry as
`{"projects": [{"id", "name", "port", "running", "current"}], "current": <id>}`
(`current` marks this instance), probed per entry over HTTP and cached
10 s; with no registry it holds just this instance. It never reads
another project's data root — only its `/api/health` — and never
exposes a path. The header switcher (menu over these rows, running dot
as glyph plus word, stopped rows showing the launch command to copy) is
shown only when the registry lists 2 or more projects.

`GET /static/<file>` serves a file from `static/` or one subdirectory (`vendor/`). `/static/taskmap.json` is the resolved task map (`taskmap.local.json`, else `taskmap.example.json`). Context-window rows use tool `grok-context` and are left out of cost.

## Plan bundles

Plan and Execution are one shell, `static/index.html`. `GET /` and `GET /plan` both serve that file. The page swaps the view in place (`?view=plan|exec&ws=<ID>`). `/static/index.html?ws=<ID>` still opens Execution. Project text stays out of the tool directory, under `AGENT_DASH_WORKSTREAMS` (default `~/.local/share/agent-dash/workstreams`). One directory per bundle id. A directory with `bundle.json` shows up on the plan view and, for `kind: workstream`, on `GET /api/plan`. When both a bundle and the plans map name a workstream, the bundle's `plan.json` wins (`resolve_plan`).

## Project views

Top-level views beside Plan | Execution: `?view=project` (Project home, the default landing when `GET /api/project` names the project or any workstream left the `active` state, else today's Overview), `?view=current` (alias of the `exec` view: today's execution), `?view=planned` (start-readiness), `?view=archive` (frozen summary cards). `?view=exec&ws=<ID>` and `?view=plan&ws=<ID>` are unchanged, including for archived ids (archived plan pages render read-only with an "Archived \<date\>" banner). The header nav pills (Project | Current | Planned | Archive, with counts on Planned and Archive) mark the active view; the Plan | Execution switch stays for a selected workstream.

The pinned bar stays calm no matter how many workstreams exist: project name, `Overall`, the selected workstream as a chip (only with a selection), one `Workstreams (N)` button, up to three pinned chips. The button opens a searchable switcher popover (type to filter by name or id, arrow keys and Enter to pick, Escape closes, `/` opens it from anywhere) listing workstreams grouped by state (Active, Paused, Planned, Done, then a collapsed Archived group) with a state glyph plus word, progress percent, live-agent dot, and per-row quick actions on hover and focus. Pins are viewer-local (`localStorage` behind try/catch, none by default, starring past three is refused). On phones the switcher becomes a full-width sheet. There is no horizontal tab strip and no drag-reorder: the old viewer-local tab order retired with the strip. `?view=exec&ws=<ID>` still opens a workstream tab, and the switcher routes planned rows to the Planned view and archived rows to the Archive view.

Current is the management table: one row per non-archived, non-dropped workstream with a state pill (glyph plus word), progress bar with percent, live agents, last activity, depends-on chips, and a row action cluster. Above it a toolbar filters by state chips with counts, searches by name or id, and runs bulk actions: `Archive done` archives every done workstream after one confirm that lists them, while `Pause`, `Resume`, and `Mark done` run on checked rows after one confirm each. Rows that are complete but not done carry a `Mark done` primary button; done rows an `Archive` primary; planned rows a `Start`. The same primary appears on the Project home row cards, on the Planned readiness rows, and on the workstream tab heading (a state pill plus the primary as a split button with the remaining actions in its menu); Archive view rows carry `Restore`. Dropping is terminal and says so: its confirm is the stronger one and its toast carries no Undo, as does the Start toast (active has no way back to planned). Every action toasts with Undo kept 10 s, confirms inline (never `window.confirm`), moves focus in and back, names the workstream in every aria-label, disables its buttons while the request is in flight, and documents `/`, arrow keys, and `g` then `p|c|l|a` in a small Keys popover.

The dashboard stays read-only under the switch: every action button becomes a copy-command button (`python3 agent_dash.py ws state <ID> <state>`, `ws archive`, `ws restore`) with a confirmation toast; they never write. Closing reports are basename-only on every payload; Report links open the plan view.

Card registry scopes are `overview`, `workstream`, `both` (the exec tabs), plus `project` (Project home slots `exec-burnup`, `exec-phases`, `exec-health`, `exec-depgraph`) and `planned` (Planned slot `exec-depgraph`). A slotted entry mounts inside its named slot; an unslotted entry renders as its own section after the slots in registry order. A chart needed on both views adds one registry line per scope over the same spec function: still one file plus registry lines, no routing edits. The live example is the dependency graph: `depgraph.js` registers `depgraph` (scope `project`) and `depgraph-plan` (scope `planned`), both slotted `exec-depgraph` over the one `depGraphSpec`, fed by `GET /api/project/graph`.

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
The overview workstream cards read the same snapshot rows the switcher and
the management table read, so each card shows exactly what its workstream
tab shows for the same 24 h window. Runs with no workstream land in the
`unassigned` row, which the Overview shows, so the overview total visibly
equals the sum of the parts. Every async page response applies only when
it still matches the current workstream and grouping (one fixed window),
and the background cache is keyed by workstream and group and expires
after one refresh interval, so no scope ever shows another scope's numbers. A scope with no tokens in the window reads "0 in 24 h" with its
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

One row per muse/glm run. The same fold covers session-less muse and glm launcher rows (`muse:None:<label>@<epoch>`, likewise `glm`): each folds into the same-label session row (`muse:<uuid>:<label>`) starting within 15 minutes, with the same earliest start, model/verdict/out-path gap-fill, usage and sample moves, and durable alias in the same transaction, on the next muse/glm poll. The first poll after an upgrade backfills aliases for pre-existing pairs; later restarts resolve instead of resurrecting. The `duplicate run` rule treats all three families alike: merged pairs never flag, and only a genuinely surviving twin does.

Liveness wins over file evidence. A muse, glm, or grok run with a process sample for its run id or stored pid within `PROCESS_GONE_GRACE_S` (30 seconds, which is what a live pid observably means) is never closed as done by a quiet session file; `ended_at` is set only after the process has been gone for the grace period. A running manifest row folded into a wrongly closed worker row reopens it while its process evidence is fresh.

Exact process matching. A live process matches a run by pid first, then by session id, then by prompt-file or label. The label leg goes to the newest open row with that label only: a relaunched run reuses the prompt file under a new session id, and the dead older row is closed instead of staying running.

Dead-pid grace. The stale check ignores a dead-pid row whose last evidence (start, usage, or sample) is within two poll intervals of its source — 4 s for the 2 s tool sources — because the source closes the row on its next poll. Rows dead longer than that still fail the check.

Unseen-worker grace. The unseen-worker check ignores a live labelled worker younger than two poll intervals of its source — 4 s for the 2 s tool sources — by process age, or first sampled by this dashboard within that window (a fresh dashboard has only just met it, e.g. ramp-up transients). Older, longer-seen workers with no running row still fail the check. Startup grace on top: no unseen-worker problem is emitted for any worker until the dashboard process is older than max(3x the slowest source interval, 90 s) and every source has completed at least one poll since start (the registry `has_completed_poll` / `starting` state); a worker that only becomes visible once a source's first scan lands is silent while that source is still starting. `doctor` and `check` share the rule through one function.

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
| `bundle.json` | `{id, title, short, order, kind}`. `kind` is `program` or `workstream`. The id matches the directory name. Lifecycle fields are additive: `state` (`planned`, `active`, `paused`, `done`, `archived`, `dropped`; default `active` when absent), `state_changed_at` (epoch or null), `archived_at` (epoch or null), `summary` (object frozen by `ws archive`, or null), `depends_on` (workstream ids that must exist; never `OVERVIEW`), `milestone` (phase-name string or null). `ws validate` rejects a bad value (exit 4) with the next command. |
| `plan.json` | Decision record: `dag`, `decisions`, `motivation`, `agent_friendly`, `narrative`. Task status is `todo`, `pr`, `prog`, `done`, or `deferred`. `deferred` may set `moved_to`, `note`, and `received_as`. A task may carry `deps`, `dep_notes` (conditions keyed by dep id), `kind` (`task` or `gate`; see SKILL-PLAN.md), and `files` (declared footprints); a gate may carry `signal`, `confirmed_at`, and `fresh_hours`. `ws validate` rejects a bad `kind`, a `dep_notes` key that is not one of the task's `deps`, a bad `files`/`signal`/`confirmed_at`/`fresh_hours`/`received_as`, and a bare number citation. A `program` bundle may hold only `narrative`. |
| `page.html` | Plan-view fragment. Generated for bundles that have `visuals.html` and `layout.json`. |
| `approvals.json` | Optional. `{ws, decided_at, decided_by, items: [{id, step, status, reversible, conditions}]}`. `status` is `pending`, `approved`, or `denied`. `reversible` is a boolean. `decided_at` is an ISO-8601 UTC string or null. |
| `visuals.html` | Hand-made sections keyed by h2 title. |
| `layout.json` | Which visual sections land in Why, End state, Plan, Agent-friendly, and Reference. |
| `START-PROMPT.md` | Optional. Paste-ready prompt that starts this workstream's lead. `ws new` writes a template. |

`dash/planrender.py` `render(bundle_dir)` writes `page.html` in this order: Approvals, Why, End state, Plan, Progress, Agent-friendly, Reference. There is no Decided section on the plan tab (removed on owner request); plan-level decisions stay in the CLI and the API. Approvals is an empty `data-plan-approvals` node. Progress is an empty `data-plan-progress` node. The page fills both from the live bundle. Deferred tasks stay on the map and leave the counts.

```
python3 agent_dash.py ws list [--state S[,S...]|all] [--json]
python3 agent_dash.py ws new <ID> "<title>" [--state planned|active]
python3 agent_dash.py ws state <ID> <state> [--force] [--note "..."]
python3 agent_dash.py ws archive <ID> [--force] [--note "..."]
python3 agent_dash.py ws restore <ID>
python3 agent_dash.py ws validate <ID>
python3 agent_dash.py ws render <ID>
python3 agent_dash.py ws plan <ID>
python3 agent_dash.py ws layout <ID>
python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."] [--force]
python3 agent_dash.py ws approve <ID> <item-id> <approved|denied|pending> [--note "..."] [--step "..."] [--reversible yes|no] [--conditions "..."]
python3 agent_dash.py ws approvals [<ID>]
```

`ws plan` lists the bundle's task ids, titles, statuses, and deps.
Exit codes are 0 ok, 1 check failed, 2 usage error, 3 not found, 4
invalid data, 5 dashboard unreachable (docs/integrate.md, For agents).

`ws new` writes a valid template (`--state` sets the lifecycle state, default `active`). `ws task` writes `plan.json` through a temp file and `os.replace`, holding a per-bundle `fcntl` lock across the read-modify-write so concurrent writers keep every update. `ws approve` writes `approvals.json` the same way. `ws state`, `ws archive`, and `ws restore` write `bundle.json` under the same lock and append one `{kind: "state"}` line to `history.jsonl`; `ws archive` also freezes `summary.json` and never deletes anything. Moving a `done` or `deferred` task back to `wip` or `pr` needs `--force`. A new pending item needs `--step`, `--reversible yes|no`, and `--conditions`. `approved` or `denied` sets `decided_at` and `decided_by` to `owner`. `ws approvals` prints a table. `ws validate` checks the schema, including `approvals.json` when that file exists, and the naming rule: a TU-style id, a #-prefixed issue number, or an ADR number with no phrase earlier on the same line is a failure.

Map warnings (MAP101–MAP111) are the plan-shape lint from `dash/maplayout.py`: warnings, never errors. `ws validate` prints them after errors under a `warnings:` heading and lists them in `--json` under `warnings`; the exit code follows the schema errors alone. `ws layout <ID> [--json]` prints the computed layout (density, columns, group states, label shortening count, arrows policy) plus the same warnings with fix hints. Run it before kickoff and aim for zero MAP warnings. The codes:

| Code | Warns when | Fix |
|---|---|---|
| MAP101 | a group has more than 14 tasks | split the group into two stage groups |
| MAP102 | more than 5 columns | re-stage into at most 5 columns |
| MAP103 | a column holds groups but no live tasks | drop the column or move a group into it |
| MAP104 | tallest column > 3x shortest with > 40 live tasks | regroup across stages |
| MAP105 | a group title is a bare id | retitle in plain words |
| MAP106 | 3+ columns and a non-first-column group has no waits-on edge | add stage waits-on edges |
| MAP107 | a train has fewer than 2 members | drop the train, keep a task note |
| MAP108 | more than 40 task edges (arrows default to on-focus) | add dep_notes to cross-group edges |
| MAP109 | half or more groups hold exactly 1 task | merge 1-task groups |
| MAP110 | a task id suffix is longer than 12 chars | shorten the id |
| MAP111 | deferred share above 20% (info) | no action; deferred auto-collapse |

HTTP, still GET only, 127.0.0.1:

- `GET /plan` serves the same shell as `GET /`.
- `GET /api/bundles` returns `{bundles: [{id, title, short, order, kind, state, state_changed_at, archived_at, depends_on, milestone, summary}]}`, program first, then `order`. Lifecycle fields are additive; bundles written before them read as `state: active`.
- `GET /api/bundle?ws=<ID>` returns `{bundle, plan, progress, approvals, start_prompt, issues}` from that directory. `bundle` is the raw `bundle.json`, so it carries the lifecycle fields too. `progress` is the same object `/api/plan` carries. Missing `approvals.json` is an empty item list. `start_prompt` is the text of `START-PROMPT.md`, or null. `issues` is the same list `/api/plan` carries.
- `GET /api/archive` returns `{"archive": [{<summary.json contents>, id, title, archived_at, report}], "totals": {workstreams, tokens, cost_usd, runs}}`: one frozen summary card per `archived` workstream, newest first (bundles without `summary.json` still list with their metadata). `report` is the closing-report file's basename only, never a path and never its body — no report prose reaches any endpoint. `ws archive` freezes `closing_report_name` (the basename, `closing-report.md`) and the boolean `closing_report` into `summary.json`; the card resolves `report` in this order: the frozen `closing_report_name` when that file still exists in the bundle's `docs/`, then `docs/closing-report.md`, then the legacy summary report keys, then the newest `reports/*closing*.md`. `totals` is the archived workstreams' lifetime share from the store (same summary functions as every other total: all-time tokens and cost plus the run-row count), so project lifetime numbers keep including archived work.
||||||| parent of 628bd9d (feat(dash): project roadmap swimlane with focus-only dependency connectors)
- `GET /api/archive` returns `{"archive": [{<summary.json contents>, id, title, archived_at, report}], "totals": {workstreams, tokens, cost_usd, runs}}`: one frozen summary card per `archived` workstream, newest first (bundles without `summary.json` still list with their metadata). `report` is the closing-report file's basename only, never a path and never its body — no report prose reaches any endpoint. `totals` is the archived workstreams' lifetime share from the store (same summary functions as every other total: all-time tokens and cost plus the run-row count), so project lifetime numbers keep including archived work.
- `GET /api/archive` returns `{"archive": [{<summary.json contents>, id, title, archived_at, report}], "totals": {workstreams, tokens, cost_usd, runs}}`: one frozen summary card per `archived` workstream, newest first (bundles without `summary.json` still list with their metadata). `report` is the closing-report file's basename only, never a path and never its body — no report prose reaches any endpoint. `totals` is the archived workstreams' lifetime share from the store (same summary functions as every other total: all-time tokens and cost plus the run-row count), so project lifetime numbers keep including archived work.
- `GET /api/project/timeline` returns `{"now", "project": {started, target, phases}, "history_floor", "lanes": [{id, title, state, group, order, started_at, last_active_at, done_at, done_inferred, archived_at, has_history, before_history, progress: {done, total, pct}, milestone, depends_on, blocked_by, runs, tokens, cost_usd, estimate_days}], "order": [ids]}`: one lane per workstream in every state, ordered Done / Active / Planned, topological by `depends_on` then start. Dates come only from recorded evidence (the first mapped run, the first history move, state moves, task moves to done); nothing is interpolated. A lane with no date evidence reads `before_history` (planned lanes excepted: they read `estimate_days`, two days per open task, minimum seven, labelled 'estimate: plan size' on the page). A `done_at` taken from the last task move at a 100% plan instead of a recorded state move reads `done_inferred`. `project` is the `project.json` identity beside the bundles, or nulls when the file is missing. Only names and numbers cross this boundary: no report bodies, no paths, no prompts.
- `GET /api/plan?ws=<ID>` includes `start_prompt` the same way when that bundle has the file, and null otherwise.
- `GET /api/approvals?ws=<ID|all>` returns one record, or `{approvals: [...]}` for `all`.
- `GET /api/decisions` returns `{"decisions": [{id, title, status, owner, date, workstream, area, link, ...short extras}], "counts": {status: n}, "source": {"present": bool}}`, newest first, read from `<hub_dir>/decisions.tsv` (missing file means `[]`, `present: false`). Header-driven and tolerant: unknown columns survive as short extras; any cell over 400 chars and any local-file-path cell never reach the payload.
- `GET /api/risks` returns `{"risks": [{id, title, severity, source, ws, since}]}`: computed risks first (stale plan gates, pending approvals past the doctor `approval_days` window, critical/warn doctor groups on `active` workstreams, `planned` workstreams blocked by an unfinished dependency; severities `warn`/`info`, every title names the workstream before any task id), then the hub `risks.tsv`/`risks.json` rows (`source: "file"`). Archived and dropped workstreams emit no computed risk.
- `GET /api/project/feed?limit=<n>` returns `{"feed": [{ts, day, ws, name, kind, text, target}], "total": n, "limit": n, "earliest_ts": t|null}`: newest-first what-changed lines from bundle `history.jsonl` files (task moves to done/pr/prog and reopenings, state moves), decided `approvals.json` items that carry a date, archive/restore moves, first agent runs, and per-workstream-per-hour run-finish summaries. Bulk task moves inside one minute collapse into one `kind: tasks` line (`N tasks moved to done across <Name> (<WS>) (<count>), ...`). Default 50 lines, max 200; a bad limit is a 400. Lines without a recorded time are omitted, never interpolated; `earliest_ts` is the oldest consumed stamp so the page can say `no history before <date>`. Names and numbers only — no report prose, no prompts, no argv. Never carries doctor output.
- `GET /api/decisions` returns `{decisions: [{id, title, status, owner, date, workstream, area, link}], counts: {<status>: n}, source: {present: bool}}`: the read-only decisions mirror the decisions card reads. When the endpoint is absent or `source.present` is false the card hides entirely; links render as anchors only for `http(s)` URLs, else plain text.
- `GET /api/project/graph` returns `{nodes: [{id, title, state, progress, live_agents, milestone, depth}], edges: [{from, to, kind, status, condition}], cycles: [[id]], generated_at}`: the cross-workstream dependency graph, read-only over local bundles. `kind` is `dependency` (`depends_on`), `gate` (a gate task whose signal, title, or `dep_notes` text names another workstream), or `moved` (a deferred task's `moved_to`); `status` is `met`, `waiting`, or `stale` (stale per the `stale_gates` rule, met when the source is done/archived, the gate is done-like, or the gate is fresh and confirmed). Cycles are reported, never an error. Only ids, titles, states, counts, and plan condition texts cross the boundary — never paths or bodies.
||||||| parent of 245220a (feat(dash): project burn-up with an honest history gap, and cost and token burn by workstream)
- `GET /api/project/burnup` returns `{"burnup": {days, day, first_recorded, workstreams, tasks, cost, tokens}}`: recorded tasks-done cumulative per workstream per UTC day from `history.jsonl` task moves (reverse moves decrement), plan totals from the one plan resolver (deferred tasks excluded), and cost/token burn per UTC day from the same day buckets `/api/usage?group=workstream&bucket=day` serves, so per-day sums equal the all-scope totals for the same days. Done work with no history is a labelled gap (`no history before <date>: N done tasks not dated`), never interpolated. Cached by history/plan file mtimes plus the store sequence. Only ids, counts, money, and dates cross this boundary. Ids on the project's `ignore_workstreams` list leave the task lanes (and the `workstreams` order) entirely; their cost/token cells fold into one `"Other"` series so the per-workstream burn still sums to the project line, and the ignore list joins the cache key.
- `GET /bundle/<ID>/page.html` returns that file only. The resolved path has to stay inside the bundle directory.
- `GET /api/project/phases?scope=24h|7d|all` returns `{"scope", "since", "until", "day": "UTC", "rules_hash", "rules_version", "caption", "workstreams": [{ws, name, by_phase: {planning|execution|review|coordination|unclassified: {tokens: {input, output, cache_read, cache_write, reasoning}, billed_tokens, cost_usd, unpriced_tokens, agents, runs, hours}}, total: {tokens, cost_usd}, days, series: {<phase>: {cost, tokens, cumulative_cost, cumulative_tokens}}}], "total": {tokens, cost_usd}, "unclassified_share"}`: usage by phase for planning versus execution. Each usage event joins its run, the run is classified by `dash/phases.py` (explicit `ws<N>-plan/exec/review/coord` marks, review affixes, plan task matches, label words, unmapped lead sessions, the pre-task planning window), and child-chat events fold onto their parent run like the snapshot. Cost uses the same catalog pricing with the same unpriced rule, so the all-scope total equals the sum of phases to the cent. A bad scope is a 400. Conditional GET: the ETag covers scope, data version, rules hash, ignore list, plan and taskmap stamps. Ids on the project's `ignore_workstreams` list fold into one `"Other"` row, like the burn-up. Only names and numbers cross this boundary: no prompts, no bodies, no paths. `GET /api/workstreams` and `GET /api/state` workstream rows carry a small lifetime `by_phase` (`{<phase>: {tokens, cost_usd}}`, all five phases always present) for the management-table bar and the workstream-tab card; their caches key on the rules hash, so a rules edit retags.

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

## Safe deploy

The live dashboard runs from a release, never a dirty working
checkout. A release is a clean detached worktree of one commit under
`<state-dir>/releases/<sha>`; `releases/CURRENT` and
`releases/PREVIOUS` are symlinks naming the running release and the
one before it. `deploy [--ref origin/main] [--worktree DIR]` records
the previous release, gates the new one (`scripts/gate.sh` when
present, else the page-rules and server test files), switches
`CURRENT`, and restarts from the new release with a health gate
(`/api/health` must report the release build, then `check` must
pass). Any failure restarts the previous release and exits 1 with the
reason. `rollback` performs the same switch back on demand. Both are
read-only on sources and never touch another session's checkout;
deploy resolves the ref as-is, so fetch first when the latest
upstream commit is wanted. `run` refuses to start when the port
answers with a different `build_id` (use `--force-replace` to restart
it instead); `status` prints the running build.

Process control never orphans or mis-signals. The pidfile is written
atomically and removed only by its owner: the exiting server, `stop`,
and `restart` erase it only when it still names their own pid, so an
old server shutting down after a restart cannot erase the new
server's entry. `status`, `stop`, and `restart` find the dashboard
even with a missing or stale pidfile: when the configured port
answers `/api/health` and the listener's command line is the
dashboard server, `status` reports `running (pidfile missing, pid N)`
(`status --repair` records that pid, and only in that case) and
`stop`/`restart` act on that pid. `stop`, `restart`, and `deploy`
refuse a target from another state dir (compared by `state_hash`)
unless `--yes-live` is passed, and always print `stopping dashboard
pid N on port P (state <hash>, build B)` before acting; with the
default (live) state dir, a non-interactive session must also pass
`--yes-live`.

A release checkout is clean, so its own `config.local.json` never
holds the user's settings. Config search order is
`config.example.json`, then the checkout `config.local.json`, then
`<state-dir>/config.local.json`, then the `AGENT_DASH_CONFIG` file
override (which replaces the search when set). The other layered
files keep their env overrides (`AGENT_DASH_MAP`,
`AGENT_DASH_PLANS`, `AGENT_DASH_TASKMAP`, `AGENT_DASH_WORKERS`,
`AGENT_DASH_WORKSTREAMS`). `AGENT_DASH_PORT` trials the server on
another port; the live address stays 127.0.0.1:8765.

## Checks

Optional, on a machine that already has sessions. Compare one session's transcript totals to that machine's own usage ledger within 0.5%. Peak concurrency over the same window should land within 10% of a hand count. Collector CPU under 10% of one core averaged over 5 minutes. RSS under 300 MB. Real-session smokes stay skipped unless `AGENT_DASH_SMOKE=1`.
