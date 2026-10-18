# agent-dash

The local page for this kit. It reads transcripts, manifests, and process samples on your machine and shows runs, token use, and a plan map per workstream.

<img src="../../docs/img/dashboard-flow.svg" width="900" alt="Flow from agents through collectors to the store and dashboard, plus six steps for scoping work">
Start with [Plug in any agent](docs/integrate.md): install and run in three commands, one reporting path per harness, labels, verdicts, pricing, and troubleshooting. The contract is [CONTRACT.md](CONTRACT.md). The Project home "Planning versus execution" card splits each workstream's tokens and cost into planning, execution, review, coordination, and unclassified from run labels (`catalog/phases.json` rules — the file name is historical — `GET /api/project/phases`). The repository root holds the [landing page](../../README.md), [quickstart](../../docs/quickstart.md), [concepts](../../docs/concepts.md), and [configuration](../../docs/configuration.md).

The page binds to 127.0.0.1 port 8765. That address is fixed. Lifecycle controls from the page are the one write action: local, header-guarded, off with AGENT_DASH_READONLY=1.

## Install

From the repository root:

```bash
./install.sh
```

The script symlinks this directory to `~/.claude/tools/agent-dash` and each skill into `~/.claude/skills/`. A second run is safe.

## Run

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py run
python3 ~/.claude/tools/agent-dash/agent_dash.py status
python3 ~/.claude/tools/agent-dash/agent_dash.py restart
python3 ~/.claude/tools/agent-dash/agent_dash.py check
python3 ~/.claude/tools/agent-dash/agent_dash.py doctor
python3 ~/.claude/tools/agent-dash/agent_dash.py reprice
python3 ~/.claude/tools/agent-dash/agent_dash.py reconcile
python3 ~/.claude/tools/agent-dash/agent_dash.py backfill --session <id>
python3 ~/.claude/tools/agent-dash/agent_dash.py report start --label ws4-WS4.1-slug --model <model> --session <id>
python3 ~/.claude/tools/agent-dash/agent_dash.py ws list
python3 ~/.claude/tools/agent-dash/agent_dash.py project list
```

`run` prints `running`, a pid, and `url http://127.0.0.1:8765/`.

## Config

Zero config runs. `config.local.json` deep-merges over `config.example.json`. The full tables are in [configuration](../../docs/configuration.md).

## Links

- [Plug in any agent](docs/integrate.md)
- [Run DeepSeek through Grok Build](docs/integrate.md#run-deepseek-through-grok-build) ([config](../../docs/configuration.md#grok-configdeepseekexampletoml), [key-free template](catalog/grok-config.deepseek.example.toml))
- [CONTRACT.md](CONTRACT.md)
- [quickstart](../../docs/quickstart.md) and [concepts](../../docs/concepts.md)
- [landing page](../../README.md)
