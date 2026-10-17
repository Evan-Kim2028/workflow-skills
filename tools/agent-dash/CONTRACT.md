# agent-dash contract (v1)

Local, read-only execution dashboard. One Python 3 process (stdlib only), binds 127.0.0.1:8765.
Code is this directory. `install.sh` links it to `~/.claude/tools/agent-dash`. State: `~/.local/share/agent-dash/` (dash.sqlite WAL, pidfile, log).
Never writes to any source, never signals agent processes, never stores prompt bodies or argv.

## Layout and file ownership (one writer per file)

| Path | Owner |
|---|---|
| `agent_dash.py` (CLI: `run`, `stop`, `status`, `backfill --session <id>`, `reprice`, `reconcile`, `check`, `ws`) | core |
| `dash/store.py` (schema, offsets, upserts, buckets, counts) | core |
| `dash/server.py` (HTTP + SSE, routes below) | core |
| `dash/attribution.py`, `map.example.json` (`map.local.json` replaces it) | core |
| `dash/pricing.py`, `catalog/prices.json` | core |
| `dash/plans.py`, `plans.example.json` (`plans.local.json` replaces it) | core |
| `dash/bundles.py`, `dash/planrender.py`, `SKILL-PLAN.md` | core |
| `scripts/cursor_usage_hook.py` | src-cursor |
| `dash/sources/__init__.py` (registry, scheduler) | core |
| `dash/sources/process.py`, `dash/sources/hub.py` | core |
| `dash/sources/claude.py`, `dash/sources/manifests.py` | src-ai |
| `dash/sources/muse.py`, `dash/sources/grok_cli.py` | src-tools |
| `dash/sources/cursor.py` | src-cursor |
| `static/index.html` (all UI, vanilla JS, inline SVG/canvas charts) | Claude (parent) |
| `fixtures/*.json` (seeded synthetic API responses from `fixtures/make_fixtures.py`) | fixture |
| `tests/test_*.py` (unittest, stdlib) | each owner for its files |

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
store.add_process_sample(dict)# keys: ts, pid, comm, rss_kb, elapsed_s, label, run_id
store.set_ws_counts(list)     # rows: ws, name, backlog, todo, doing, review, done, cancelled
```

`cfg` is the merged config (roots, intervals, read budgets: 4 MB per file, 8 MB per tick). There is no concurrency cap.
Sources must be incremental (byte offsets), budget-bounded, and exception-safe (log, never crash the loop).

Enums. `source`: claude-main, claude-subagent, claude-workflow, muse, glm, grok (cursor-agent), shell.
`tier`: opus, sonnet, haiku, muse, glm, grok. `status`: running, done, killed, died_early, unknown.
`verdict`: PASS, PARTIAL, BLOCKED, APPROVE, APPROVE_WITH_CHANGES, REQUEST_CHANGES, or null.
`clock_source`: transcript, muse_log, manifest, mtime. Times are epoch seconds (float), UTC.
`run_id` is stable: `<source>:<session-or-uuid>:<label-or-agent-id>`.

Workstream attribution happens in core (`attribution.py`), not in sources: label via the map file, exact then regex
(`^ws(\d+)` -> WSn, `WS(\d+)\.` -> WSn, `^g-` -> WS1, `^dash-` -> TOOLS), then session default from the map
`sessions` object, else null. `map.local.json` replaces `map.example.json` when present. Store `workstream` and `ws_confidence`
(`label`, `regex`, `session-default`).

## HTTP API (all GET, JSON, 127.0.0.1 only)

`GET /` -> `static/index.html`.

`GET /api/snapshot?ws=<WSn|all>&since=&until=`
```json
{"now": 0, "seq": 0,
 "counts": {"live_agents": 0, "live_by_tool": {"grok": 0, "muse": 0, "glm": 0, "claude": 0}, "finished_today": 0, "finished_in_range": 0, "finished_by_verdict": {}},
 "running": [Run], "recent": [Run],
 "totals": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0, "unpriced_runs": 0},
 "by_workstream": [{"ws": "WS7", "live_agents": 0, "finished_in_range": 0, "finished_today": 0, "tokens": 0, "cost_usd": 0.0, "last_activity": 0}]}
```
One function (`Store._summarize_locked`) computes the all-scope counts and totals,
every workstream row, and the `unassigned` row from the same run list, the same
agent rule, and the same window. The `by_workstream` row for a workstream equals
that workstream tab's `counts` and `totals`. `unassigned` carries runs with no
workstream; it appears only when such runs exist. Row sums plus unassigned equal
the all-scope numbers exactly (cost within 1e-6).
`Run` = `{run_id, source, tier, model, label, workstream, ws_confidence, session_id, parent_session_id, status, verdict,
started_at, ended_at, elapsed_s, clock_source, out_bytes, out_growing, err_bytes, pid, rss_kb, tokens: {input, output, cache_read, cache_write}, cost_usd, context_tokens}`.
`context_tokens` is the latest `grok-context` input for that run, or null. It is a context-window estimate. It is not part of `tokens`, totals, buckets, or cost. `elapsed_s` is null when `started_at` is null.
`recent` = runs ended in the last 60 minutes, newest first, max 50.

`GET /api/stream` -> SSE. Event `tick` every 2 s: `{"seq": n, "running": [Run]}`; the page refetches snapshot when `seq` changes.

`GET /api/runs?ws=&since=&until=&limit=` -> `{"runs": [Run]}` (Gantt input; workflow children collapsed into one row with `children: n`).

`GET /api/usage?group=model|workstream|tool&bucket=hour|day&ws=&since=&until=` -> `{"buckets": [{"t": 0, "key": "opus", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost_usd": 0.0}]}`

`GET /api/concurrency?ws=&since=&step=60` -> `{"points": [{"t": 0, "grok": 0, "muse": 0, "glm": 0, "claude": 0}]}`

`GET /api/sessions` -> `{"sessions": [{session_id, tool, cwd, label, workstream, started_at, status, runs: n, tokens, cost_usd}]}`

`GET /api/workstreams?since=&until=` -> `{"workstreams": [{ws, name, tk: {backlog, todo, doing, review, done, cancelled}, live_agents, finished_in_range, finished_today, tokens, cost_usd, last_activity, plan: bool}]}`.
Rows come from the same rollup as the snapshot, so they match the tabs for the
same window. `tk` is the hub issue board (issue counts, never plan progress).
An `unassigned` row appears when runs have no workstream.

`GET /api/reconcile?hours=&since=&until=` -> `{"now": 0, "since": 0, "until": 0, "all": {live_agents, finished_in_range, finished_today, tokens, cost_usd}, "rows": [per-workstream rows], "unassigned": {...}, "mismatches": []}`.
Computed in one store pass under the lock. `mismatches` lists every violated
invariant and is empty when consistent. `python3 agent_dash.py check [--url URL] [--hours N]`
prints the rows as a table and exits 1 on any mismatch.

`GET /api/plan?ws=WS7` -> that workstream's plan with a `progress` object:
`{"ws": "WS7", "cols": [..], "groups": [{id, col, title, tasks: [{id, title, status, deps, kind, dep_notes}]}], "edges": [[a, b]], "phases": [{name, goal, gate}], "progress": {"done": 0, "total": 0, "counts": {todo, wip, pr, prog, done, deferred}, "pct": 0}}`.
A task's `kind` is `task` or `gate` (an outside signal or condition, drawn as a diamond); absent means `task`. `dep_notes` maps one of the task's dep ids to the short condition on that edge. Both pass through every plan API sanitized like other fields. `/api/bundle` carries the same task shape inside `plan.dag.groups`.
`plans.local.json` replaces `plans.example.json`. Each value is a path. Relative paths resolve next to the plans file. The loader reads `dag` and `narrative.phases`.
One resolver (`dash.bundles.resolve_plan`) picks the plan file: a bundle's
`plan.json` wins over the plans-map file. `/api/plan`, `/api/bundle`, and the
page all read the same body, so the plan view and the execution map agree.
`progress.done` counts done tasks, `progress.total` counts every task except
deferred, `pct` rounds half up. Unknown task statuses are ignored. The page
never computes % done itself; it reads `progress.pct`, `progress.done`, and
`progress.total`.

`GET /api/health` -> `{"ok": true, "sources": [{name, last_poll, last_error, lag_s}], "pricing": {...}}`

`GET /static/<file>` serves a file from `static/` or one subdirectory (`vendor/`). `/static/taskmap.json` is the resolved task map (`taskmap.local.json`, else `taskmap.example.json`). Context-window rows use tool `grok-context` and are left out of cost.

## Plan bundles

Plan and Execution are one shell, `static/index.html`. `GET /` and `GET /plan` both serve that file. The page swaps the view in place (`?view=plan|exec&ws=<ID>`). `/static/index.html?ws=<ID>` still opens Execution. Project text stays out of the tool directory, under `AGENT_DASH_WORKSTREAMS` (default `~/.local/share/agent-dash/workstreams`). One directory per bundle id. A directory with `bundle.json` shows up on the plan view and, for `kind: workstream`, on `GET /api/plan`. When both a bundle and the plans map name a workstream, the bundle's `plan.json` wins (`resolve_plan`).

## One number, one truth

An agent is a running row whose source maps to a tool (grok, muse, glm, or
claude). Interactive lead sessions (`claude-main`), shell rows, and Dagster
jobs are not agents. Finished means status `done`, `killed`, or `died_early`
with an end time inside the window. The default window is the last 24 hours;
`since`/`until` (or the page range control) narrow it.

Token and cost sums come from the hourly usage buckets, counting a bucket when
its hour overlaps the window (`hour_ts + 3600 > since` and `hour_ts < until`).
The tokens tile, the tokens-per-hour chart legend, the snapshot totals, and the
per-workstream rows all sum the same `/api/usage` cells, so they agree exactly
for the same scope and window. Runs with no workstream land in the
`unassigned` row, which the Overview tab shows, so the overview total visibly
equals the sum of the parts.

Each bundle:

| File | Role |
|---|---|
| `bundle.json` | `{id, title, short, order, kind}`. `kind` is `program` or `workstream`. The id matches the directory name. |
| `plan.json` | Decision record: `dag`, `decisions`, `motivation`, `agent_friendly`, `narrative`. Task status is `todo`, `pr`, `prog`, `done`, or `deferred`. `deferred` may set `moved_to` and `note`. A task may carry `deps`, `dep_notes` (conditions keyed by dep id), and `kind` (`task` or `gate`; see SKILL-PLAN.md). `ws validate` rejects a bad `kind` and a `dep_notes` key that is not one of the task's `deps`. A `program` bundle may hold only `narrative`. |
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
python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."]
python3 agent_dash.py ws approve <ID> <item-id> <approved|denied|pending> [--note "..."] [--step "..."] [--reversible yes|no] [--conditions "..."]
python3 agent_dash.py ws approvals [<ID>]
```

`ws new` writes a valid template. `ws task` writes `plan.json` through a temp file and `os.replace`. `ws approve` writes `approvals.json` the same way. A new pending item needs `--step`, `--reversible yes|no`, and `--conditions`. `approved` or `denied` sets `decided_at` and `decided_by` to `owner`. `ws approvals` prints a table. `ws validate` checks the schema, including `approvals.json` when that file exists, and the naming rule: a TU-style id, a 3-to-5-digit issue number, or an ADR number with no phrase earlier on the same line is a failure.

HTTP, still GET only, 127.0.0.1:

- `GET /plan` serves the same shell as `GET /`.
- `GET /api/bundles` returns `{bundles: [{id, title, short, order, kind}]}`, program first, then `order`.
- `GET /api/bundle?ws=<ID>` returns `{bundle, plan, progress, approvals, start_prompt}` from that directory. `progress` is the same object `/api/plan` carries. Missing `approvals.json` is an empty item list. `start_prompt` is the text of `START-PROMPT.md`, or null.
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

`run_worker.py <tier|default|auto> <label> <workspace> <prompt-file> <run-dir> [max-wait]` prints `STATUS: running|done|failed out=... err=... tier=<name>`. It records the chosen worker in `<run-dir>/<label>.tier` and the pid in `<run-dir>/<label>.pid`. A later call for that label only waits. `default` uses the registry `default`. `auto` uses the first enabled worker in `auto_order` that is under `cap` (live count from `pgrep`) and not in cooldown. A failed run whose stderr or report matches `quota_patterns` cools that worker down for `cooldown_minutes`. Cooldowns live in `worker-cooldowns.json` in the agent-dash state dir. If the request was `auto` or `default`, the runner archives the report to `out-<label>.<tier>.md` and launches the next eligible worker.

## Checks

Optional, on a machine that already has sessions. Compare one session's transcript totals to that machine's own usage ledger within 0.5%. Peak concurrency over the same window should land within 10% of a hand count. Collector CPU under 10% of one core averaged over 5 minutes. RSS under 300 MB. Real-session smokes stay skipped unless `AGENT_DASH_SMOKE=1`.
