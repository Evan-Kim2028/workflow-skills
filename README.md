# workflow-skills

Custom skills and small tools for running multi-agent workflows with Claude Code and other coding agents.

| Skill | What it does |
|---|---|
| [`workstream-dashboard`](skills/workstream-dashboard/SKILL.md) | Local dashboard for agent runs: live agents per workstream, token use and cost, a run timeline, and a plan map per workstream. Tool: [`tools/agent-dash`](tools/agent-dash/README.md). |
| [`workstream-planner`](skills/workstream-planner/SKILL.md) | Plan a new project or workstream: plan bundle, visuals, reviewed execution plan, owner approvals, kickoff brief and start prompt. |
| [`workstream-lead`](skills/workstream-lead/SKILL.md) | Run a planned workstream as its lead: waves via `ws-wave`, status moves, approved production steps only, coordination, hand-off. |
| `ws-wave` workflow | Runs one workstream wave in Claude Code's Workflow tool: each lane is built, reviewed and fixed by a registry worker until approved, with Haiku wrappers only. [`tools/agent-dash/workflows/ws-wave.js`](tools/agent-dash/workflows/ws-wave.js), guide: [`docs/workstream-lead.md`](tools/agent-dash/docs/workstream-lead.md). |
| Worker launchers | [`run_worker.sh`](tools/agent-dash/scripts/run_worker.sh) reads the worker registry and starts one worker. [`launch_grok.sh`](tools/agent-dash/scripts/launch_grok.sh) runs headless cursor-agent and logs its billed tokens. |

## Install

```bash
git clone https://github.com/Evan-Kim2028/workflow-skills.git
cd workflow-skills
./install.sh
```

Workers are listed in [`tools/agent-dash/workers.example.json`](tools/agent-dash/workers.example.json). `workers.local.json` replaces that file on one machine, and `AGENT_DASH_WORKERS` overrides both. `scripts/run_worker.sh` takes `default`, a worker name, or `auto`. `default` is the registry's default worker. `auto` starts the first enabled worker in `auto_order` that is under its cap and not cooling down after a quota error. The workflow's `defaultTier` is `default`.

`install.sh` symlinks every `skills/<name>` into `~/.claude/skills/` and every `tools/<name>` into `~/.claude/tools/`, so `git pull` updates them in place. A real directory already at a target is renamed to `*.bak-<epoch>`, never deleted. Re-running it is safe.

## Adding a skill

Put it in `skills/<name>/SKILL.md` (Claude skill format: a `name` and a third-person `description` with its triggers). Put a tool it needs in `tools/<name>/`. Personal config goes in `*.local.json` files, which are gitignored. Never commit session ids, home-directory paths or real run data.
