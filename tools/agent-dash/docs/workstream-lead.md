# Running a workstream end to end

A workstream is planned once and executed in waves by workers from the registry (`default` is the usual tier, `auto` fails over), with one thin Claude lead session that decides, merges, and runs production steps. The dashboard shows the whole thing live.

## 1. Plan (once)

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws new WS4 "Deals on the public API"
```

This creates a bundle in `~/.local/share/agent-dash/workstreams/WS4/`. Fill these files:

| File | What goes in it |
|---|---|
| `plan.json` | The decision record: decisions with alternatives and evidence, motivation (today, after, costs, where it is not better), the agent-friendly litmus, the task DAG (`dag.cols`, `groups[].tasks[{id,title,status}]`, `edges`), and the narrative (thesis, end state, phases). |
| `visuals.html` | Hand-made diagrams and charts, as `<section class="pl-sec lay"><h2>Title</h2>...</section>`. Every chart has x and y axes and compact numbers. |
| `layout.json` | Which visual sections go in Why, End state, Plan, Agent-friendly and Reference. |

Then render and check it:

```bash
python3 agent_dash.py ws render WS4 && python3 agent_dash.py ws validate WS4
```

`ws validate` also checks the naming rule: never cite a decision, task, PR, ADR or issue by its number alone. Lead with a short phrase; the number can follow in parentheses.

## 2. Execution plan and kickoff (once)

Write the execution plan into `docs/` in the bundle:

- the file-overlap matrix, because lanes in the same wave must not share a file or a test file;
- the waves, and what each one waits on;
- the merge trains;
- the critical path;
- coordination points with other workstreams;
- the remaining production steps.

Have it reviewed adversarially before anyone starts.

Then write `KICKOFF.md`, a self-contained brief for a fresh lead session. It must cover:

- where everything lives;
- the waves;
- run labels;
- how to move status;
- merge rules;
- who to coordinate with;
- the done criteria.

## 3. Owner approvals

List every production step in `approvals.json`. The owner approves or denies each one, and the lead reads it before acting.

```bash
python3 agent_dash.py ws approve WS4 api-roll pending --step "Roll the API after the train merges" --reversible yes --conditions "one prod step at a time"
python3 agent_dash.py ws approvals WS4
```

Pending items show in the dashboard's "Needs you" card and in the Plan page's Approvals section.

## 4. Execute each wave with the `ws-wave` workflow

In the lead session, call Claude Code's Workflow tool with:

```
scriptPath: ~/.claude/tools/agent-dash/workflows/ws-wave.js   (absolute path)
args: {
  "ws": "WS4", "repo": "<abs repo>", "runDir": "<abs scratch>/runs",
  "template": "<abs worker rules template>",
  "runner": "<abs>/agent-dash/scripts/run_worker.sh", "dash": "<abs>/agent-dash/agent_dash.py",
  "maxFixRounds": 2, "defaultTier": "default", "reviewTier": "default",
  "lanes": [ { "task": "WS4.3", "slug": "deal-route", "branch": "ws4/deal-route",
               "worktree": "<abs worktree path>", "brief": "<self-contained task>",
               "tests": "<explicit test command>", "review": true } ]
}
```

What the workflow does:

- All lanes run in parallel.
- Each lane goes build, then review, then fix and re-review until approved, or until it runs out of fix rounds.
- Every step is a low-effort Haiku wrapper around `scripts/run_worker.sh`. The runner reads the worker registry (`workers.example.json`, or `workers.local.json` when that file is present) and launches the requested tier once. The tier is `default`, `auto`, or a worker name. `auto` skips a worker that is at its cap or cooling down after a quota error, and tries the next name in `auto_order`. The run is logged for the dashboard. The lane result's `tier` is the name the runner actually used.
- On approval it runs `ws task WS4 WS4.3 pr`.
- It returns `approved` branches, `needsLead` lanes (with the last review), and `built` lanes.
- It never pushes, opens PRs, merges, or touches production.

Run labels are `ws4-WS4.3-deal-route`, then `-rev1` and `-fix1`. The dashboard pins each attempt onto the task chip, and `↻N` counts the attempts.

## 5. The lead's loop

Each time a wave workflow returns, the lead:

1. Reads the result.
2. Merges approved lanes as trains, using the repo's merge rules.
3. Moves statuses with `ws task`.
4. Runs approved production steps one at a time.
5. Coordinates timing with the other leads.
6. Starts the next wave.

A workflow cannot receive messages from other sessions or wait out multi-day windows. Those stay with the lead.

## 6. Watch it

- The Plan page is at `http://127.0.0.1:8765/plan?ws=WS4`.
- The Execution view is at `http://127.0.0.1:8765/static/index.html?ws=WS4`.
- Both have Plan | Execution in one shell.
