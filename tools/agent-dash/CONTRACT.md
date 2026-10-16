# agent-dash contract (v1)

Local, read-only execution dashboard. One Python 3 process (stdlib only), binds 127.0.0.1:8765.
Code is this directory. `install.sh` links it to `~/.claude/tools/agent-dash`. State: `~/.local/share/agent-dash/` (dash.sqlite WAL, pidfile, log).
Never writes to any source, never signals agent processes, never stores prompt bodies or argv.

## Layout and file ownership (one writer per file)

| Path | Owner |
|---|---|
| `agent_dash.py` (CLI: `run`, `stop`, `backfill --session <id>`, `status`) | core |
| `dash/store.py` (schema, offsets, upserts, buckets) | core |
| `dash/server.py` (HTTP + SSE, routes below) | core |
| `dash/attribution.py`, `map.example.json` (`map.local.json` replaces it) | core |
| `dash/pricing.py`, `prices.json` | core |
| `dash/plans.py`, `plans.example.json` (`plans.local.json` replaces it) | core |
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

`cfg` is the parsed `config.json` (roots, intervals, caps, read budgets: 4 MB per file, 8 MB per tick).
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

`GET /api/snapshot?ws=<WSn|all>`
```json
{"now": 0, "seq": 0,
 "caps": {"grok": {"live": 0, "cap": 10}, "muse": {"live": 0, "cap": 10}, "glm": {"live": 0, "cap": 5}, "claude": {"live": 0, "cap": null}},
 "running": [Run], "recent": [Run],
 "alerts": [{"kind": "died_early|killed|long_running|over_cap", "run_id": "...", "label": "...", "detail": "...", "ts": 0}],
 "totals": {"tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost_usd": 0.0, "unpriced_runs": 0},
 "by_workstream": [{"ws": "WS7", "running": 0, "done": 0, "tokens": 0, "cost_usd": 0.0, "last_activity": 0}]}
```
`Run` = `{run_id, source, tier, model, label, workstream, ws_confidence, session_id, parent_session_id, status, verdict,
started_at, ended_at, elapsed_s, clock_source, out_bytes, out_growing, err_bytes, pid, rss_kb, tokens: {input, output, cache_read, cache_write}, cost_usd}`.
`recent` = runs ended in the last 60 minutes, newest first, max 50.

`GET /api/stream` -> SSE. Event `tick` every 2 s: `{"seq": n, "running": [Run], "alerts_count": n}`; the page refetches snapshot when `seq` changes.

`GET /api/runs?ws=&since=&until=&limit=` -> `{"runs": [Run]}` (Gantt input; workflow children collapsed into one row with `children: n`).

`GET /api/usage?group=model|workstream|tool&bucket=hour|day&ws=&since=` -> `{"buckets": [{"t": 0, "key": "opus", "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}]}`

`GET /api/concurrency?ws=&since=&step=60` -> `{"points": [{"t": 0, "grok": 0, "muse": 0, "glm": 0, "claude": 0}]}`

`GET /api/sessions` -> `{"sessions": [{session_id, tool, cwd, label, workstream, started_at, status, runs: n, tokens, cost_usd}]}`

`GET /api/workstreams` -> `{"workstreams": [{ws, name, tk: {backlog, todo, doing, review, done, cancelled}, running, tokens, cost_usd, plan: bool}]}`

`GET /api/plan?ws=WS7` -> the plan DAG file named in the plans map for that ws, passed through:
`{"ws": "WS7", "cols": [..], "groups": [{id, col, title, tasks: [{id, title, status}]}], "edges": [[a, b]], "phases": [{name, goal, gate}]}`.
`plans.local.json` replaces `plans.example.json`. Each value is a path. Relative paths resolve next to the plans file. The loader reads `dag` and `narrative.phases`.

`GET /api/health` -> `{"ok": true, "sources": [{name, last_poll, last_error, lag_s}]}`

## Checks

Optional, on a machine that already has sessions. Compare one session's transcript totals to that machine's own usage ledger within 0.5%. Peak concurrency over the same window should land within 10% of a hand count. Collector CPU under 10% of one core averaged over 5 minutes. RSS under 300 MB.
