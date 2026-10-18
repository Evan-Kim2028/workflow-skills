# workflow-skills

<img src="docs/img/dashboard-flow.svg" width="900" alt="Flow from agents through collectors to the store and dashboard, plus six steps for scoping work so it can be tracked">

The dashboard workstream is one local page that tracks many agents working in parallel across tools. With several agents running at once, you lose track of what is planned, running, done, and what it costs. The dashboard reads what agents already write, shows it by workstream, and nudges agents to scope and label work so it stays trackable.

![Execution overview, dark theme](docs/screenshots/overview-dark.png)

## What you get

- Every run on one page: live agents, finished runs, tokens and cost.
- A plan map per workstream: tasks, dependencies, gates, and progress.
- Project, Current, Planned, and Archive views, with a dependency graph and burn-up.
- A doctor command that names what is broken and the exact fix.
- Lifecycle buttons on the page with undo: start, pause, done, archive, restore.
- Local and read-only: labels, timings, and token counts only; data stays on your machine.

## Quickstart

```bash
./install.sh
python3 tools/agent-dash/agent_dash.py run
```

Open http://127.0.0.1:8765 . The ten-minute path with sample data is [docs/quickstart.md](docs/quickstart.md).

## Scope work well

1. One workstream, one outcome.
2. Break it into tasks with ids and depends_on.
3. Define gates (what proves it is done).
4. Label every agent run with its workstream and task.
5. Tag planning, execution, and review runs.
6. Move state as you go; archive when done.

The lead loop is [Running a workstream end to end](tools/agent-dash/docs/workstream-lead.md).

## Links

- [docs/quickstart.md](docs/quickstart.md)
- [docs/concepts.md](docs/concepts.md)
- [docs/configuration.md](docs/configuration.md)
- [tools/agent-dash/docs/integrate.md](tools/agent-dash/docs/integrate.md)
- [tools/agent-dash/CONTRACT.md](tools/agent-dash/CONTRACT.md)
