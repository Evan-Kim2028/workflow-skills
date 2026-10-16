# agent-dash

The local page for this kit. It reads transcripts, manifests, and process samples on your machine and shows runs, token use, and a plan map per workstream.

The product overview, the ten-minute path, and the configuration reference are in the repository root: [README](../../README.md), [quickstart](../../docs/quickstart.md), [concepts](../../docs/concepts.md), [configuration](../../docs/configuration.md).

![Overall tab in the dark theme](../../docs/screenshots/overview-dark.png)

The Overall tab in the dark theme. The image file is a placeholder until a screenshot is saved there.

The page binds to 127.0.0.1 port 8765. That address is fixed. The store keeps labels, timings, and token counts. Prompt bodies, argv, secrets, and environment values stay unread.

## Install

From the repository root:

```bash
./install.sh
```

The script symlinks this directory to `~/.claude/tools/agent-dash` and symlinks each skill into `~/.claude/skills/`. A second run that already points here prints `ok`. A real directory at the destination is renamed to `*.bak-<epoch>`.

## Run

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py run
python3 ~/.claude/tools/agent-dash/agent_dash.py status
python3 ~/.claude/tools/agent-dash/agent_dash.py stop
python3 ~/.claude/tools/agent-dash/agent_dash.py reprice
python3 ~/.claude/tools/agent-dash/agent_dash.py reconcile
python3 ~/.claude/tools/agent-dash/agent_dash.py ws list
```

`run` prints `running`, a pid, and `url http://127.0.0.1:8765/`. `stop` signals only the pid in the pidfile.

Fixture preview, with no live sessions. From the repository root:

```bash
cd tools/agent-dash
python3 -m http.server 8766 --bind 127.0.0.1
```

Open http://127.0.0.1:8766/static/index.html?fixtures . The plan for the sample workstream is the same page with `&view=plan&ws=WS1`. Stop that server with Control-C.

Regenerate the fixtures with `python3 fixtures/make_fixtures.py` from this directory. The script uses a fixed seed and does not read your sessions.

## Config

Zero config runs. The shipped examples are the defaults. `config.local.json` deep-merges over `config.example.json`. `map.local.json`, `plans.local.json`, and `static/taskmap.local.json` each replace the matching example. Environment variables win over both. The full tables are in [configuration](../../docs/configuration.md).

With the shipped scratch glob, a directory under `$HOME/agent-scratch/` is a scratch root only when it exists. An empty hub directory skips that collector.

## Privacy

Read-only on every source. Labels only. `*.local.json` is gitignored. Keep session ids, home-directory paths, and token dumps out of commits.

## Worker registry

Workers come from a registry: [`workers.example.json`](workers.example.json), replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Shipped tiers are muse, cursor-agent Grok, cursor-agent Composer (`composer-2.5`), and GLM via grok-build (`glm`, `glm-flash`). Each worker has a kind, a model, a concurrency cap, and quota patterns. A cursor tier's cap counts only `cursor-agent` processes whose `--model` is that tier's model.

`scripts/run_worker.sh` takes `default`, a worker name, or `auto`, plus an optional model override. `ws-wave` can set tier and model per lane and per role (build, review, fix), `wrapperModel` for the Claude wrapper, and `deps` / `base` so one run can stack a lane on another. A lane example is in [docs/workstream-lead.md](docs/workstream-lead.md). `auto` starts the first worker in `auto_order` that is under its cap and not cooling down after a quota error, and relaunches a lane on the next one when it hits a limit.
