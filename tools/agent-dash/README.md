# agent-dash (workstream dashboard)

A local page for agent runs. It reads transcripts, manifests, and process samples on your machine and shows runs, token use, concurrency, and a plan map per workstream.

![Workstream dashboard](docs/screenshot.png)

That image is a placeholder. After you run the fixture preview, save a browser shot at `docs/screenshot.png`.

The page binds to 127.0.0.1. Reads stay on this machine. It stores labels, timings, and token counts, and leaves prompt bodies, argv, secrets, and env values unread.

## Install

```bash
git clone https://github.com/Evan-Kim2028/workflow-skills.git
cd workflow-skills
./install.sh
```

`install.sh` is idempotent. It symlinks:

- `~/.claude/skills/workstream-dashboard` to `skills/workstream-dashboard`
- `~/.claude/tools/agent-dash` to `tools/agent-dash`

If either target already exists and is not a symlink, the script renames it to `*.bak-<epoch>` and then links. A second run that already points at this repo prints `ok` and leaves it.

## Run

```bash
cd ~/.claude/tools/agent-dash
python3 agent_dash.py run
python3 agent_dash.py status
python3 agent_dash.py stop
python3 agent_dash.py reprice
python3 agent_dash.py reconcile
python3 agent_dash.py ws list
```

Open http://127.0.0.1:8765/ . `stop` signals only the pid in the pidfile.

Fixture preview, no live sessions:

```bash
cd tools/agent-dash
python3 -m http.server 8766 --bind 127.0.0.1
```

Then open http://127.0.0.1:8766/static/index.html?fixtures . Plan is the same page with `&view=plan&ws=WS1`. Stop that server by its PID when you are done.

Regenerate the fixtures with `python3 fixtures/make_fixtures.py`. The script uses a seeded RNG and does not read your sessions.

## Config

Zero config runs. Scratch roots and the hub tracker default to empty, so those sources poll nothing. Claude, muse, grok, and cursor roots default under `$HOME` and are ignored when the directory is missing.

Copy an example file to `*.local.json` when you want your own values. A local file replaces the example for maps, plans, the task map, and the worker registry. `config.local.json` deep-merges over `config.example.json` (a list you set replaces that list).

| Example | What you set |
|---|---|
| `tools/agent-dash/config.example.json` | Roots, hub directory, state directory |
| `tools/agent-dash/map.example.json` | Label rules and session defaults |
| `tools/agent-dash/plans.example.json` | Which workstreams have a plan file |
| `tools/agent-dash/static/taskmap.example.json` | Label to plan-task overrides |
| `tools/agent-dash/workers.example.json` | Worker registry: default, auto order, caps |

`plans.example.json` points at `plans/ws1.json`, `plans/ws2.json`, and `plans/ws7.json`. Those are synthetic DAGs so the tabs render before you add your own.

Environment variables override the files:

- `AGENT_DASH_CLAUDE_PROJECTS`, `AGENT_DASH_CLAUDE_SESSIONS`
- `AGENT_DASH_MUSE_SESSIONS`
- `AGENT_DASH_GROK_SESSIONS`, `AGENT_DASH_GROK_LOG`
- `AGENT_DASH_CURSOR_HOME`
- `AGENT_DASH_SCRATCH_GLOBS` (`os.pathsep`-separated directory globs; default empty)
- `AGENT_DASH_HUB_DIR` (optional directory with `issues/` and `workstreams.tsv`)
- `AGENT_DASH_ISSUES_DIR`, `AGENT_DASH_WORKSTREAMS_TSV` (override the two hub paths)
- `AGENT_DASH_STATE_DIR`
- `AGENT_DASH_WORKSTREAMS` (plan bundles, default `~/.local/share/agent-dash/workstreams`)
- `AGENT_DASH_MAP`, `AGENT_DASH_PLANS`, `AGENT_DASH_TASKMAP`, `AGENT_DASH_CONFIG`
- `AGENT_DASH_WORKERS` (worker registry path; otherwise `workers.local.json`, else `workers.example.json`)

The example scratch value is `$HOME/agent-scratch/*`. A glob that matches nothing is the same as an empty list.

## Tests

```bash
cd tools/agent-dash
python3 -m unittest discover -s tests
```

Smoke tests that would read a real session stay skipped unless `AGENT_DASH_SMOKE=1`, and they still need the path variables named in each test.

## Privacy

Read-only on every source. Labels only. `*.local.json` is gitignored. Do not commit a file that contains a real session id, a home-directory path, or a token dump.
