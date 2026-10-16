# workflow-skills

Custom skills and small tools for running multi-agent workflows with Claude Code and other coding agents.

| Skill | What it does |
|---|---|
| [`workstream-dashboard`](skills/workstream-dashboard/SKILL.md) | Local dashboard for agent runs: live agents per workstream, token use and cost, a run timeline, and a plan map per workstream. Tool: [`tools/agent-dash`](tools/agent-dash/README.md). |

## Install

```bash
git clone https://github.com/Evan-Kim2028/workflow-skills.git
cd workflow-skills
./install.sh
```

`install.sh` symlinks every `skills/<name>` into `~/.claude/skills/` and every `tools/<name>` into `~/.claude/tools/`, so `git pull` updates them in place. A real directory already at a target is renamed to `*.bak-<epoch>`, never deleted. Re-running it is safe.

## Adding a skill

Put it in `skills/<name>/SKILL.md` (Claude skill format: a `name` and a third-person `description` with its triggers). Put a tool it needs in `tools/<name>/`. Personal config goes in `*.local.json` files, which are gitignored. Never commit session ids, home-directory paths or real run data.
