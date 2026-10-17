# Configuration

Lookup for the shipped files, the override rules, and the environment variables. The walk-through is the [quickstart](quickstart.md).

A fresh install runs with the shipped examples. You add a `*.local.json` file only when a default is wrong.

## Example files

Four example files ship under `tools/agent-dash/`. `*.local.json` next to an example is gitignored.

### config.example.json

Roots, the optional hub, and the state directory. `config.local.json` deep-merges over it.

| Key | Shipped value |
|---|---|
| `roots.claude_projects` | `$HOME/.claude/projects` |
| `roots.claude_sessions` | `$HOME/.claude/sessions` |
| `roots.muse_sessions` | `$HOME/.local/share/muse/sessions` |
| `roots.grok_sessions` | `$HOME/.grok/sessions` |
| `roots.grok_log` | `$HOME/.grok/logs/unified.jsonl` |
| `roots.cursor_home` | `$HOME/.cursor` |
| `roots.scratchpads` | `$HOME/agent-scratch/*` |
| `hub_dir` | empty |
| `issues_dir` | empty |
| `workstreams_tsv` | empty |
| `state_dir` | `$HOME/.local/share/agent-dash` |
| `dagster.ssh_host` | empty |
| `dagster.graphql_url` | `http://127.0.0.1:3000/graphql` |
| `dagster.enabled` | `true` |

`$HOME` and `~` expand when the file is loaded. A scratch glob that matches no directory becomes an empty list, so those collectors poll nothing. A missing Claude, muse, Grok, or Cursor directory is skipped. An empty `hub_dir` skips the hub collector. A `hub_dir` that points at a missing directory logs a warning and stays healthy.

The hub collector, when configured, expects `issues/*.md` with frontmatter fields `workstream` and `status`, plus `workstreams.tsv`. The TSV header is skipped. Each later row is a workstream code and a name, separated by a tab. Status values it counts are `backlog`, `todo`, `doing`, `review`, `done`, and `cancelled`. `issues_dir` and `workstreams_tsv` override those two paths inside the hub directory.

The Dagster collector is off until `dagster.ssh_host` is set. Each minute it runs one `ssh <host> curl` against `graphql_url` on that host and reads queued, running and recently finished runs (the last 2 h). It keeps the job name, status, times and four allowlisted tags (partition, ops wave, slot class, claim size), and drops every other tag. Dagster jobs show on the timeline and in the running table as prod jobs. They never count as agents and carry no tokens or cost. A failed call shows as that source's `last_error` and leaves stored rows alone.

Code defaults that the example file can override, if you put them in `config.local.json`:

| Key | Default |
|---|---|
| `read_budget.per_file` | 4 MiB |
| `read_budget.per_tick` | 8 MiB |
| `cursor_usage` | unset. The collector then reads `cursor-usage.jsonl` in the state directory. |

`host` and `port` in a config file are ignored. The server always binds to 127.0.0.1 port 8765.

### map.example.json

Workstream attribution. `map.local.json` replaces this file. It does not merge.

| Key | Meaning |
|---|---|
| `exact` | Object of label to workstream id. |
| `regex` | List of pairs. Each pair is a Python regex and a template. `{1}` in the template is the first capture group. |
| `sessions` | Object of session id to workstream id. Used when the label matches nothing. |

The shipped file maps the exact label `notes-only` to WS2. Its regexes are `^ws(\d+)` to `WS{1}`, `WS(\d+)\.` to `WS{1}`, `^g-` to WS1, and `^dash-` to TOOLS. The session ids in the file are zeros, for the sample data.

If the map file is missing, those same regexes apply and the exact and session tables are empty. If the file is not valid JSON, every table is empty.

### plans.example.json

Workstream id to plan path. `plans.local.json` replaces this file. A relative path resolves next to the JSON file. The shipped map is WS1, WS2, and WS7, pointing at `plans/ws1.json`, `plans/ws2.json`, and `plans/ws7.json`. Those files are synthetic graphs so the tabs render.

A workstream bundle appears without an entry here. Add a plans-map row only for a plan file that is not a bundle.

### static/taskmap.example.json

Run label to task id, per workstream. `static/taskmap.local.json` replaces this file. The server serves the resolved file at `/static/taskmap.json` on every request.

A key is an exact label, or a regex written as `"/pattern/"`. The value is the task id. The shipped examples cover a few WS1, WS2, and WS7 labels. The page also has built-in task matchers for those three ids. Every other workstream matches nothing until you add it here. See the [quickstart](quickstart.md) for a WS4 file that keeps the shipped examples.

## Local overrides

Two rules, and they are not the same.

**Config deep-merges.** Load order when `AGENT_DASH_CONFIG` is unset:

1. Code defaults.
2. `config.example.json`.
3. `config.local.json`, unless local files are ignored.
4. Environment variables.

A JSON object merges key by key. A list or a string replaces that key. Setting `roots.scratchpads` in the local file replaces the whole scratch list and leaves the other roots in place.

**Map, plans, and task map replace.** `layered_path` picks one file. An environment path wins. Otherwise `*.local.json` wins over `*.example.json`. Otherwise a plain `*.json` of that name, if it exists. The chosen file is the whole rule set.

`AGENT_DASH_IGNORE_LOCAL` set to anything other than empty or `0` hides every `*.local.json`. Tests use that so they see the shipped examples.

`AGENT_DASH_CONFIG` points at one JSON file. That file merges onto the code defaults. The example and `config.local.json` are not read. Environment variables still apply afterward. Map, plans, and task map are unchanged by this variable. They have their own path variables.

Restart the dashboard after a config change. `run` while a pid is live will not reload. `stop`, then `run`. Task status, `plan.json`, and the task map are read per request and do not need a restart.

State files in the state directory:

| File | Role |
|---|---|
| `dash.sqlite` | Runs, usage, sessions, offsets. |
| `agent-dash.pid` | The dashboard pid. |
| `agent-dash.log` | The forked process log. |
| `cursor-usage.jsonl` | Billed Grok usage lines. |
| `generic.jsonl` | Harness-neutral run and usage lines. Any agent writes it; `report` appends one line. |
| `tmp/` | Scratch copies of read-only databases. |

## Environment variables

Set variables override the merged config. An empty variable is ignored.

| Variable | Effect |
|---|---|
| `AGENT_DASH_CONFIG` | Config file to merge onto code defaults. Skips the example and `config.local.json`. |
| `AGENT_DASH_CLAUDE_PROJECTS` | Claude project transcripts root. |
| `AGENT_DASH_CLAUDE_SESSIONS` | Claude session registry root. |
| `AGENT_DASH_MUSE_SESSIONS` | muse session root. |
| `AGENT_DASH_GROK_SESSIONS` | Grok CLI session root. GLM usage is read here. |
| `AGENT_DASH_GROK_LOG` | Grok CLI unified log, used when a session has no `usage.json`. |
| `AGENT_DASH_CURSOR_HOME` | Cursor home. Chats live under `chats/`. |
| `AGENT_DASH_SCRATCH_GLOBS` | Scratch roots, separated by the platform path separator. Replaces `roots.scratchpads`. |
| `AGENT_DASH_HUB_DIR` | Directory that contains `issues/` and `workstreams.tsv`. |
| `AGENT_DASH_ISSUES_DIR` | Issues directory, instead of `<hub_dir>/issues`. |
| `AGENT_DASH_WORKSTREAMS_TSV` | Workstream catalog, instead of `<hub_dir>/workstreams.tsv`. |
| `AGENT_DASH_STATE_DIR` | Pidfile, log, database, and default usage file. |
| `AGENT_DASH_WORKSTREAMS` | Plan bundle root. Default `~/.local/share/agent-dash/workstreams`. |
| `AGENT_DASH_MAP` | Map file. Replaces example and local. |
| `AGENT_DASH_PLANS` | Plans map file. Replaces example and local. |
| `AGENT_DASH_TASKMAP` | Task map file. Replaces example and local. |
| `AGENT_DASH_IGNORE_LOCAL` | Hide `*.local.json` when the value is not empty and not `0`. |
| `AGENT_DASH_DB` | Database path for `reconcile` only. The server uses the state directory. |
| `AGENT_DASH_CURSOR_USAGE` | Where the stop hook and `launch_grok.sh` append billed lines. Default is `cursor-usage.jsonl` in the state directory. The collector does not read this variable. It reads the state-directory file, or `cursor_usage` in config. Point them at the same path. |

Test-only variables. Smoke tests stay skipped unless `AGENT_DASH_SMOKE` is `1`. Each test also needs its own path variable, and still reads only the path you set.

| Variable | Test that reads it |
|---|---|
| `AGENT_DASH_SMOKE` | Gate for every smoke test. |
| `AGENT_DASH_SMOKE_TRANSCRIPT` | Claude transcript smoke. |
| `AGENT_DASH_SMOKE_MUSE_ROOT`, `AGENT_DASH_SMOKE_MUSE_SID` | muse session smoke. |
| `AGENT_DASH_SMOKE_SCRATCH` | Scratchpad smoke. |
| `AGENT_DASH_SMOKE_GROK_SESSIONS`, `AGENT_DASH_SMOKE_GLM_SID` | GLM usage smoke. |
| `AGENT_DASH_SMOKE_MANIFEST`, `AGENT_DASH_SMOKE_CURSOR_HOME` | Cursor home smoke. |
| `AGENT_DASH_HUB_DIR` | Also a product variable. The hub smoke uses it when smoke is on. |

Tier selection is the `defaultTier`, `reviewTier`, and lane `tier` fields on a wave: a registry worker name, `default`, or `auto`. See [Concepts](concepts.md). The registry file is `workers.example.json`, replaced by `workers.local.json` or the `AGENT_DASH_WORKERS` path.

## Commands

From `~/.claude/tools/agent-dash`, or with that path on the command:

```bash
python3 agent_dash.py run
python3 agent_dash.py stop
python3 agent_dash.py status
python3 agent_dash.py backfill --session <id>
python3 agent_dash.py reprice
python3 agent_dash.py reconcile
python3 agent_dash.py check [--url URL] [--hours N]
python3 agent_dash.py check --page [--url URL]
python3 agent_dash.py report start --label <label> [--model M] [--session S]
python3 agent_dash.py report finish --label <label> --verdict PASS
python3 agent_dash.py report usage --label <label> --input N --output N [--model M]
python3 agent_dash.py ws list
python3 agent_dash.py ws new <ID> "<title>"
python3 agent_dash.py ws validate <ID>
python3 agent_dash.py ws render <ID>
python3 agent_dash.py ws task <ID> <task-id> <status>
python3 agent_dash.py ws train <ID> <train-id> [--title T] [--status S] [--add id,id]
python3 agent_dash.py ws trains <ID>
python3 agent_dash.py ws approve <ID> <item-id> pending --step "<step>" --reversible yes --conditions "<conditions>"
python3 agent_dash.py ws approvals
python3 agent_dash.py ws approvals <ID>
```

`ws task` status is `todo`, `wip`, `pr`, `prog`, `done`, or `deferred`. It accepts `--moved-to WSn` and `--note "..."` only with status `deferred`. `ws approve` status is `pending`, `approved`, or `denied`. `--reversible` is `yes` or `no`. `backfill` requires `--session`. With no subcommand, `agent_dash.py` starts the server, the same as `run`.

`reconcile` marks grok, muse, and glm rows whose process is gone. It prints `reconcile ended <count>`.

`check` asks a running dashboard for `GET /api/reconcile`, prints one table row
per workstream plus the unassigned row, and exits 1 on any mismatch. `--url`
defaults to `http://127.0.0.1:8765` and `--hours` defaults to 12.

`check --page` is the browser regression check. It drives the live page in
headless Chrome over the DevTools protocol (`tools/agent-dash/scripts/page_check.mjs`,
Node stdlib only, no npm installs) through the overview and every workstream
tab at each range (6 h, 12 h, 24 h, 3 d), waits for each view to settle, and
compares the rendered token and cost tiles plus the chart legend totals against
`/api/snapshot` and `/api/usage` for the same scope and range. It exits 1 with
a table on any mismatch and skips with a clear message when Chrome is missing.
`node tools/agent-dash/scripts/page_check.mjs --url URL [--ws WS2] [--hours 12]`
runs the same harness directly, limited to one tab or range when given.

`render_replay.mjs` records a workstream's history replay to video. It opens
the execution tab with the replay running (`?view=exec&ws=<ID>&replay=1&autoplay=1`),
captures frames with `Page.startScreencast`, and writes an H.264 MP4 plus a
palette GIF about 1000 px wide, with three still PNGs (start, middle, end).
Light theme by default, dark with `--dark`. It needs Chrome and ffmpeg.

```bash
node tools/agent-dash/scripts/render_replay.mjs --url http://127.0.0.1:8765 --ws WS1 --out <dir> [--dark] [--seconds 15|30] [--fps 15] [--width 1000]
```

The GIF target is under about 15 MB; the script retries smaller when the
first pass overshoots. The page's `Replay` button plays the same timeline
live; `?fixtures&ws=WS1` replays offline from `fixtures/replay-WS1.json`.

## Add a data source

A collector is a module in `tools/agent-dash/dash/sources/`. The registry imports every module in that package that is not named with a leading underscore. A module is a source when it defines `NAME`, `INTERVAL_S`, and `poll(store, cfg, now)`. An import error is recorded on `/api/health` and that module is skipped. An exception inside `poll` is caught, stored as `last_error`, and the other sources keep polling.

`poll` tails files with `store.get_offset(path)` and `store.set_offset(path, inode, offset, size, mtime)`. Stay inside `read_budget.per_file` and `read_budget.per_tick`. Do not write to the source tree. Do not store prompt bodies, argv, secrets, or environment values. Store a label, a timestamp, and token counts.

Add `tests/test_<name>.py` for the module. Restart the dashboard. Then:

```bash
curl -s http://127.0.0.1:8765/api/health
```

The new source should appear under `sources` with `last_error` set to null.

## Worker registry

Workers come from a registry: [`../tools/agent-dash/workers.example.json`](../tools/agent-dash/workers.example.json), replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Each worker has a kind (muse, cursor-agent, or Grok Build CLI), a model, a concurrency cap and quota patterns. `scripts/run_worker.sh` and the `ws-wave` workflow take `default` (the registry's default worker), a worker name, or `auto`. `auto` starts the first worker in `auto_order` that is under its cap and not cooling down after a quota error, and relaunches a lane on the next one when it hits a limit.
