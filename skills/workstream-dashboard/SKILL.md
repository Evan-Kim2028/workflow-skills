---
name: workstream-dashboard
description: Run, change and grow the local agent execution dashboard (agent-dash) that shows every agent run, token use, concurrency and the per-workstream plan progress map with live agents on their tasks. Use for "workstream dashboard", "agent dashboard", "local dashboard", "show agent progress", adding a workstream or data source to it, refreshing a plan map, or changing its charts and layout.
---

# Workstream dashboard (agent-dash)

A local, read-only page that shows what every agent is doing across sessions and tools, per workstream, with charts and a plan progress map.

- Code: `tools/agent-dash/` in this repo. After `./install.sh`, that directory is `~/.claude/tools/agent-dash`. Contract: `CONTRACT.md`. Look: `DESIGN.md`. Read both before changing anything.
- State: `~/.local/share/agent-dash/` (`dash.sqlite`, pidfile, log). Override with `AGENT_DASH_STATE_DIR` or `state_dir` in `config.local.json`.
- Live page: http://127.0.0.1:8765/ . Offline design preview with fixture data: `cd tools/agent-dash && python3 -m http.server 8766 --bind 127.0.0.1`, then http://127.0.0.1:8766/static/index.html?fixtures (stop it by PID when done).

## Run

```bash
cd ~/.claude/tools/agent-dash
python3 agent_dash.py run      # background it with nohup; binds 127.0.0.1:8765, nice 10
python3 agent_dash.py status
python3 agent_dash.py stop     # signals only the pid in the pidfile
python3 agent_dash.py backfill --session <claude-session-uuid>
```

Do not run it with `python3 -I` (the package imports from its own directory). Restart after changing any Python module. `static/` files are read on every request.

Health check after any change: `curl -s http://127.0.0.1:8765/api/health` (every source `last_error: null`), `curl -s 'http://127.0.0.1:8765/api/snapshot?ws=all'`, and `/api/plan?ws=WS1` when that workstream has a plan.

With no `config.local.json`, scratch roots and the hub tracker are empty. Those sources poll nothing and stay healthy. Claude, muse, grok, and cursor roots still default under `$HOME` and are skipped when the directory is missing.

## What it shows

- Overall tab: stat tiles, running-now table, alerts, workstream cards, tokens per hour (by model or workstream), live agents over time (one strip per tool), run timeline grouped by workstream, recently finished runs.
- One tab per workstream that has a plan file. Same views filtered to it, plus the plan progress map: task groups in columns, chips by status, arrows for "waits on". A chip with a running agent pulses and carries a badge (tool color, count). A chip whose run finished in the last 15 minutes gets a green outline. Running runs that map to no task are listed above the map.
- Click any run (table row, timeline bar, live chip) for the details drawer.

## Rules

1. Read-only on every source. Never write to `~/.claude/projects`, `~/.cursor`, `~/.grok`, muse state, scratchpads or repos. Never signal agent processes.
2. Never store or render prompt bodies, argv, secrets or env values. Labels only.
3. Visual first. New information arrives as a chart or diagram that follows `DESIGN.md` (fixed palette and entity colors, one axis per chart, legend for 2+ series, hover on every mark). No new tile rows.
4. Only workstreams with an entry in the plans map get a tab and a card. The example map lists WS1, WS2, and WS7. Others stay hidden, not deleted.
5. One writer per file (`CONTRACT.md` ownership table). Claude owns `static/index.html`, `static/taskmap.json` and `DESIGN.md`. Backend modules are built by workers against the contract.
6. Name things, not numbers. Plan and page text never cites a decision, task, PR, issue or ADR by its number alone; lead with a brief phrase, number optional in parentheses ("the read-path migration decision (D-12)"). Machine keys (task `id`, JSON keys) stay as they are.
7. Charts follow the Charts section below (axes on every chart, compact numbers); `tests/test_page_rules.py` enforces it.
8. Local CI only: run the test files for what changed, `python3 -m unittest discover -s tests -p 'test_<module>*.py'`. Do not run the whole suite for a one-module change. Real-session smoke tests stay skipped unless `AGENT_DASH_SMOKE=1`.

## Charts (every chart on every page: `static/index.html`, `static/plan.html`)

- **Axes on every chart.** A visible x axis and y axis: axis line, ticks with labels, and a short title or unit ("tokens", "agents", "time", "hour"). A categorical axis labels each category; lane and row names are its tick labels. Small multiples each carry a y axis; the shared x axis labels sit under the last strip.
- **One helper.** Draw axes only with `axes(svg, {L, T, pw, ph, yMax, yTicks, yFmt, xTicks, xTitle, yTitle})`; use `timeTicks(t0, t1, X, n)` for time axes. Never hand-draw tick labels.
- **Compact numbers everywhere.** Use `fmtN` for counts and tokens (Intl compact: 950, 1.2K, 3.4M, 1.5B), `fmtUsd` for money ($0.42, $12, $1.2K), `fmtDur` for durations (45s, 3m 10s, 2h 5m), and `fmtDate` for timestamps. Never put raw `toFixed` or `toLocaleString` on a number outside those helpers. This applies to axes, tooltips, tiles, tables and the drawer.
- **One y axis per chart.** Different scales get separate charts or small multiples.
- **Colour follows the entity**, from the fixed palette in `DESIGN.md`. Status colours mean state only, and always ship with a symbol or word.
- **Legend for two or more series**, unless rows already name them. Prefer labelled rows to a colour legend; the run timeline has one row per model, coloured by outcome.
- **Hover tooltip on every mark**; bars and rows open the details drawer on click.
- **Enforced** by `tests/test_page_rules.py`, which fails any chart function without `axes(` and any number formatted outside the helpers. Run it after every page change: `python3 -m unittest discover -s tests -p 'test_page_rules.py'`.

## Config

Shipped `*.example.json` files are the defaults. A `*.local.json` next to the example replaces that file entirely. Both names are gitignored for `*.local.json` only.

| File | Role |
|---|---|
| `config.example.json` | Roots, hub dir, state dir. `config.local.json` deep-merges over it. |
| `map.example.json` | Exact labels, regexes, session defaults. |
| `plans.example.json` | Workstream id to plan path. Relative paths resolve next to the file. |
| `static/taskmap.example.json` | Label or `/regex/` to plan task id. The server serves the resolved file at `/static/taskmap.json`. |

Environment variables override the merged config. See the repo README for the full list. The ones you will actually set:

- `AGENT_DASH_SCRATCH_GLOBS`, `os.pathsep`-separated globs. Default empty. The example file shows `$HOME/agent-scratch/*`.
- `AGENT_DASH_HUB_DIR`, a directory with `issues/*.md` and `workstreams.tsv`. Default empty.
- `AGENT_DASH_MAP`, `AGENT_DASH_PLANS`, `AGENT_DASH_TASKMAP`, `AGENT_DASH_CONFIG`, `AGENT_DASH_STATE_DIR`.

Claude project and session roots default to `~/.claude/projects` and `~/.claude/sessions`. Muse defaults to `~/.local/share/muse/sessions`. Grok defaults to `~/.grok/sessions` and `~/.grok/logs/unified.jsonl`. Cursor defaults to `~/.cursor`.

## Common changes

**Add a workstream tab.** Write a plan file in the decision-JSON shape (`dag` with `cols`, `groups[].tasks[{id,title,status}]`, `edges`; optional `narrative.phases`) and add `"WSn": "<path>"` to `plans.local.json` (copy `plans.example.json` first if you do not have one). The tab and card appear on the next refresh. Add task-id patterns for its run labels to `taskOf()` in `static/index.html` or overrides to `static/taskmap.local.json`.

**Refresh a plan map from an HTML artifact.** When task state lives in a saved artifact page:

`python3 tools/agent-dash/scripts/pull_plan_from_artifact.py <saved_artifact.html> <plan path from the plans map>`

Do this whenever you publish that artifact. The script only reads the HTML you pass it.

**Map a run label to a task.** Add `"label": "task-id"` (or `"/regex/": "task-id"`) under the workstream in `static/taskmap.local.json`. No restart.

**Add a data source.** New module `dash/sources/<name>.py` exposing `NAME`, `INTERVAL_S`, `poll(store, cfg, now)`. Incremental by byte offset or per-item watermark. Bounded per tick. Exceptions isolated. Add `tests/test_<name>.py`. Restart and check `/api/health`.

**Change the look.** Edit `DESIGN.md` first, then `static/index.html`. Check JS syntax (`node --check` on the extracted script), then view the page in both themes and on the fixture preview.

## Building with workers

Backend work goes to workers, each prompt starting with a `[label: dash-<name>]` line plus the worker rules, `CONTRACT.md`, and the files that worker owns. Keep the page, the charts, and the design spec in one place, and verify every change on the live page before calling it done.

## Known gaps (update as they close)

- cursor-agent token counts depend on decoding blobs under the configured cursor home (`chats/<md5(cwd)>/<uuid>/store.db` by default under `~/.cursor`).
- Plan status is whatever the plan file says. It is not live unless something rewrites that file.
- No launchd agent yet. Start it by hand.
