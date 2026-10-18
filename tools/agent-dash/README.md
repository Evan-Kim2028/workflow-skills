# agent-dash

The local page for this kit. It reads transcripts, manifests, and process samples on your machine and shows runs, token use, and a plan map per workstream.

Start with [Plug in any agent](docs/integrate.md): install and run in three commands, one reporting path per harness, labels, verdicts, pricing, and troubleshooting. The contract is [CONTRACT.md](CONTRACT.md). The repository root holds the [landing page](../../README.md), [quickstart](../../docs/quickstart.md), [concepts](../../docs/concepts.md), and [configuration](../../docs/configuration.md).

![Overall tab in the dark theme](../../docs/screenshots/overview-dark.png)

The Overall tab in the dark theme, rendered from the bundled sample data.

The page binds to 127.0.0.1 port 8765. That address is fixed. The store keeps labels, timings, and token counts. Prompt bodies, argv, secrets, and environment values stay unread. Archiving from the page (inline confirm, Undo toast) is the only write action; everything else copies its CLI command. Several projects run as one instance each via the project registry (`project add|list|launch|stop|open|remove`, `GET /api/projects`, header switcher); see "Running several projects" in [Plug in any agent](docs/integrate.md).

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
python3 ~/.claude/tools/agent-dash/agent_dash.py restart
python3 ~/.claude/tools/agent-dash/agent_dash.py reprice
python3 ~/.claude/tools/agent-dash/agent_dash.py reconcile
python3 ~/.claude/tools/agent-dash/agent_dash.py ws list
```

`run` prints `running`, a pid, and `url http://127.0.0.1:8765/`. `restart` stops (waiting up to 10 s, SIGKILL only for the pidfile pid), starts again in the background, and returns 0 once `/api/health` answers. Prefer it over separate `stop` and `run` calls: one command, and it waits for health before returning.

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

Workers come from a registry: [`workers.example.json`](workers.example.json), replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Each worker has a kind (muse, cursor-agent, or Grok Build CLI), a model, a concurrency cap and quota patterns. `scripts/run_worker.sh` and the `ws-wave` workflow take `default` (the registry's default worker), a worker name, or `auto`. `auto` starts the first worker in `auto_order` that is under its cap and not cooling down after a quota error, and relaunches a lane on the next one when it hits a limit.
