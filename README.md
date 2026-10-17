# workflow-skills

A local dashboard and a small kit for running many coding agents at once, from any harness. You plan a workstream, send the independent lanes out in a wave, watch the agents on one page, and see what the tokens cost.

![Execution overview, dark theme](docs/screenshots/overview-dark.png)

**Execution overview.** Live agents, finished runs, tokens and cost, workstream progress, and every run. More views are in [docs/screenshots](docs/screenshots).

## 30 seconds

From this directory:

```bash
./install.sh
python3 tools/agent-dash/agent_dash.py run
```

`install.sh` symlinks the skills and the tool into `~/.claude/`; running it again is safe. `run` prints `running`, a pid, and `url http://127.0.0.1:8765/`. Open that URL. The address is fixed.

```bash
curl -s http://127.0.0.1:8765/api/health
```

Look for `"ok": true`. Stop with `python3 tools/agent-dash/agent_dash.py stop`. The ten-minute path with sample data and a first wave is [docs/quickstart.md](docs/quickstart.md).

## Plug in your agents

[Plug in any agent](tools/agent-dash/docs/integrate.md) is the harness-neutral guide: install and run in three commands, one reporting path per harness (Claude Code, muse, cursor-agent, any other CLI, custom code, remote agents), run labels and the task map, verdict lines, token and cost reporting, driving a workstream, and troubleshooting.

## Map

| Piece | Path | What it does |
|---|---|---|
| workstream-dashboard | [skills/workstream-dashboard/SKILL.md](skills/workstream-dashboard/SKILL.md) | Runs and changes the local dashboard. |
| workstream-planner | [skills/workstream-planner/SKILL.md](skills/workstream-planner/SKILL.md) | Turns a goal into a plan bundle, an execution plan, approvals, and a kickoff brief. |
| workstream-lead | [skills/workstream-lead/SKILL.md](skills/workstream-lead/SKILL.md) | Runs that bundle wave by wave and moves task status as facts change. |
| workstream-orchestration | [skills/workstream-orchestration/SKILL.md](skills/workstream-orchestration/SKILL.md) | Keeps parallel work moving: the steady-state loop and the twelve principles from measured runs. |
| agent-dash | [tools/agent-dash](tools/agent-dash/README.md) | The page, the collectors, and the `ws` and `report` commands. State lives in `~/.local/share/agent-dash/`. |
| ws-wave | [tools/agent-dash/workflows/ws-wave.js](tools/agent-dash/workflows/ws-wave.js) | One wave. Each lane is built, reviewed, and fixed until approved or out of rounds. |
| Worker launchers | [run_worker.sh](tools/agent-dash/scripts/run_worker.sh) | Starts one registry worker and writes the row the dashboard reads. |
| Concepts, configuration | [docs/concepts.md](docs/concepts.md), [docs/configuration.md](docs/configuration.md) | Labels, tokens, files, overrides, environment variables. |

## Privacy

The page binds to 127.0.0.1 on port 8765. Configuration cannot move that address. Collectors read logs and process samples. `stop` signals only the dashboard pid in the pidfile. The database keeps labels, timings, and token counts. Prompt bodies, argv, secrets, and environment values stay unread.

Plan bundles live under `~/.local/share/agent-dash/workstreams/` and stay out of this repository. Files named `*.local.json` are gitignored. Do not commit a session id, a home-directory path, or a token dump.

`scripts/privacy_audit.sh` fails when a tracked file contains a personal path, a private project name, or key material. Run it before you push.
