# Running a workstream end to end

A workstream is planned once and then executed in waves. muse is the default worker. Grok is the other tier the launcher accepts. One Claude lead session decides, merges, and runs production steps. The dashboard shows the work live.

The install path and the first wave are in the repository [quickstart](../../../docs/quickstart.md). Labels, tokens, and the task map are in [concepts](../../../docs/concepts.md).

## 1. Plan once

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws new WS4 "Notes index"
```

This creates a bundle in `~/.local/share/agent-dash/workstreams/WS4/`. Fill these files:

| File | What goes in it |
|---|---|
| `plan.json` | The decision record. Decisions with alternatives and evidence. Motivation: today, after, costs, and where it is not better. The agent-friendly litmus. The task graph: `dag.cols`, `groups[].tasks` with `id`, `title`, and `status`, plus `edges`. The narrative: thesis, end state, and phases. |
| `visuals.html` | Hand-made diagrams and charts, as `<section class="pl-sec lay"><h2>Title</h2>...</section>`. Every chart has x and y axes and compact numbers. |
| `layout.json` | Which visual sections go in Why, End state, Plan, Agent-friendly and Reference. |

Then render the bundle and validate it:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws render WS4
python3 ~/.claude/tools/agent-dash/agent_dash.py ws validate WS4
```

`ws validate` also checks the naming rule. Cite a decision, task, pull request, ADR, or issue by a short phrase first. The number may follow, in parentheses.

## 2. Execution plan and kickoff, once

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
python3 ~/.claude/tools/agent-dash/agent_dash.py ws approve WS4 notes-cutover pending --step "Publish the notes index" --reversible no --conditions "The index file is on disk."
python3 ~/.claude/tools/agent-dash/agent_dash.py ws approvals WS4
```

Pending items show in the dashboard's "Needs you" card and in the Plan page's Approvals section.

## 4. Execute each wave with the `ws-wave` workflow

In the lead session, call Claude Code's Workflow tool with:

```text
scriptPath: the absolute path of ~/.claude/tools/agent-dash/workflows/ws-wave.js
args: {
  "ws": "WS4", "repo": "/absolute/path/to/your-repo",
  "runDir": "/absolute/path/to/agent-scratch/notes",
  "template": "/absolute/path/to/worker-rules.md",
  "runner": "/absolute/path/to/run_worker.sh",
  "dash": "/absolute/path/to/agent_dash.py",
  "maxFixRounds": 2, "defaultTier": "default", "reviewTier": "default",
  "lanes": [ { "task": "WS4.1", "slug": "notes-index", "branch": "ws4/notes-index",
               "worktree": "/absolute/path/to/wt-notes-index",
               "brief": "Add a notes index. Write only in this worktree.",
               "tests": "the explicit test command for the files this lane changes",
               "review": true } ]
}
```

`runDir` has to be a scratch root the collectors scan. The shipped glob `$HOME/agent-scratch/*` matches only the directories directly inside `$HOME/agent-scratch/`. Use `$HOME/agent-scratch/notes`. `realpath` prints the absolute tool paths. The [quickstart](../../../docs/quickstart.md) has the copy-paste form, including the task map WS4 needs before a chip lights up.

What the workflow does:

- All lanes run in parallel.
- Each lane goes build, then review, then fix and re-review until approved, or until it runs out of fix rounds.
- Every step is a low-effort Haiku wrapper around `scripts/run_worker.sh`. The runner launches muse with model `muse-spark-1.3-contributor` and reasoning effort `xhigh`, or a Grok worker, then waits and logs the run.
- On approval it runs `ws task WS4 WS4.1 pr`.
- It returns `approved` branches, `needsLead` lanes with the last review, and `built` lanes.
- It does not push, open a pull request, merge, or take a production step.

Run labels are `ws4-WS4.1-notes-index`, then `-rev1` and `-fix1`. With a task-map entry for that label, the dashboard pins each attempt on the task chip, and `↻N` counts the attempts. `defaultTier` and `reviewTier` default to `muse`. `run_worker.sh` accepts `muse` or `grok`. The lead skill's `workers.local.json` registry is not loaded by this repository.

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
- Plan and Execution are one shell. The header toggle switches them.

## Choosing workers

Workers come from a registry: [`../workers.example.json`](../workers.example.json), replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Each worker has a kind (muse, cursor-agent, or Grok Build CLI), a model, a concurrency cap and quota patterns. `scripts/run_worker.sh` and the `ws-wave` workflow take `default` (the registry's default worker), a worker name, or `auto`. `auto` starts the first worker in `auto_order` that is under its cap and not cooling down after a quota error, and relaunches a lane on the next one when it hits a limit.
