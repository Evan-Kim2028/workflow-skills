---
name: workstream-dashboard
description: Run, change, and grow the local agent execution dashboard (agent-dash) that shows every agent run, token use, and the per-workstream plan progress map. Use for "workstream dashboard", "agent dashboard", "local dashboard", or "show agent progress" from any agent harness, and for adding a workstream or data source to it or changing its charts.
---

# Workstream dashboard (agent-dash)

One local shell, two views. Execution shows what every agent is doing, per workstream, with charts and a live plan progress map. Plan shows each workstream's strategy and decisions. The header and tabs render once. Plan | Execution swaps the main view in place.

New here? Read [Plug in any agent](../../tools/agent-dash/docs/integrate.md) first. Contract: `tools/agent-dash/CONTRACT.md`. Look: `tools/agent-dash/DESIGN.md`. Read both before changing anything.

- Code: `tools/agent-dash/` in this repo (`~/.claude/tools/agent-dash` after `./install.sh`).
- State: `~/.local/share/agent-dash/` (`dash.sqlite`, pidfile, log, `generic.jsonl`). Override with `AGENT_DASH_STATE_DIR`.
- Live page: http://127.0.0.1:8765/ (fixed address). Offline fixture preview: `cd tools/agent-dash && python3 -m http.server 8766 --bind 127.0.0.1`, then http://127.0.0.1:8766/static/index.html?fixtures. Stop that server when done.

## Run

```bash
cd tools/agent-dash
python3 agent_dash.py run      # binds 127.0.0.1:8765
python3 agent_dash.py status
python3 agent_dash.py restart  # stop, then run; waits for /api/health
python3 agent_dash.py backfill --session <session-uuid>
```

Do not run it with `python3 -I` (the package imports from its own directory). Restart after changing any Python module. `static/` files are read on every request.

Verify after any change:

```bash
curl -s http://127.0.0.1:8765/api/health
python3 agent_dash.py check
python3 agent_dash.py check --page
```

Health wants every source `last_error: null`. `check` exits 0 when every number reconciles. `check --page` drives the live page in headless Chrome and skips with a clear message when Chrome is missing.

## Report a run (any harness)

```bash
python3 agent_dash.py report start --label ws4-WS4.1-slug --model <model> [--session <id>]
python3 agent_dash.py report usage --label ws4-WS4.1-slug --input N --output N [--model <model>]
python3 agent_dash.py report finish --label ws4-WS4.1-slug --verdict PASS
```

Labels pin runs to workstreams and task chips. Verdicts are `PASS`, `PARTIAL`, `BLOCKED`, or `VERDICT: APPROVE` (and family) as the first matching line. Full shapes: the integration guide.

## Common changes

- **Add a workstream tab from a plan file.** Write the decision JSON (`dag` with `cols`, `groups[].tasks[{id,title,status}]`, `edges`) and add `"WSn": "<path>"` to `plans.local.json`. The tab appears on refresh.
- **Add a workstream bundle.** `python3 agent_dash.py ws new WS4 "Read path"`, fill `plan.json`, then `ws render WS4` and `ws validate WS4`. Bundles live under `AGENT_DASH_WORKSTREAMS` and stay out of the repo.
- **Move a task.** `python3 agent_dash.py ws task <ID> <task> <todo|wip|pr|prog|done|deferred>`. Status edits read live.
- **Record a train or approval.** `ws train` / `ws trains`, `ws approve` / `ws approvals`. The lead reads approvals before any production step.
- **Map a label to a task.** Exact label or `/regex/` to task id under the workstream in `static/taskmap.local.json`. No restart.
- **Add a data source.** New module `dash/sources/<name>.py` with `NAME`, `INTERVAL_S`, `poll(store, cfg, now)`. Incremental by byte offset. Bounded per tick. Exceptions isolated. Add `tests/test_<name>.py`. Restart and check `/api/health`.
- **Change the look.** Edit `DESIGN.md` first, then the page. Check JS syntax, view both themes and the fixture preview, run the page-rules tests.

## Run a wave

Lanes in one wave share no file or test file. Each lane goes build, then review, then fix and re-review until approved or out of fix rounds. Approved lanes move to `pr` via `ws task`. The lead keeps trains, merges, production steps, and starts one wave at a time.

### If you are on Claude Code

`tools/agent-dash/workflows/ws-wave.js` runs one wave in the Workflow tool (`scriptPath` to the file, `args` as documented at its top). Tiers come from the worker registry (`default`, a name, or `auto`). Each step is a low-effort Haiku wrapper around `scripts/run_worker.sh`. Full walk-through: `tools/agent-dash/docs/workstream-lead.md`.

### If you use muse

`scripts/run_worker.sh <tier|default|auto> <label> <workspace> <prompt-file> <run-dir> [max-wait]` runs one worker and prints `STATUS: done|failed|running`. It appends the manifest row the collectors read.

## Rules

1. Read-only on every source. Never write to transcripts, sessions, scratchpads, or repos outside the lane. Never signal agent processes.
2. Never store or render prompt bodies, argv, secrets, or env values. Labels only.
3. Visual first. New information arrives as a chart or diagram per `DESIGN.md`. No new tile rows.
4. One writer per file (`CONTRACT.md` ownership table). The page, the charts, and the design spec change together; backend modules are built against the contract.
5. Name things, not numbers. Never cite a decision, task, PR, issue, or ADR by its number alone; lead with a brief phrase, number optional in parentheses.
6. Charts carry axes, compact numbers, one y axis, a legend for two or more series, and a hover tooltip on every mark; `tests/test_page_rules.py` enforces it. Run it after every page change.
7. Local scoped tests only: `python3 -m unittest discover -s tests -p 'test_<module>*.py'`. Never a whole suite for a one-module change.
