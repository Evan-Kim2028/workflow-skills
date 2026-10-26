# Running a workstream end to end

A workstream is planned once and then executed in waves. Workers come from the registry; the shipped default is muse, with Grok and GLM beside it. One lead session decides, merges, and runs production steps. The dashboard shows the work live.

The install path and the first wave are in the repository [quickstart](../../../docs/quickstart.md). Labels, tokens, and the task map are in [concepts](../../../docs/concepts.md).

## 1. Plan once

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws new WS4 "Notes index"
```

This creates a bundle in `~/.local/share/agent-dash/workstreams/WS4/`. Fill these files:

| File | What goes in it |
|---|---|
| `plan.json` | The decision record. Decisions with alternatives and evidence. Motivation: today, after, costs, and where it is not better. The agent-friendly litmus. The task graph: `dag.cols`, `groups[].tasks` with `id`, `title`, `status`, `deps` (the task ids it waits on, across sections) and optional `wave`, plus section `edges`. The narrative: thesis, end state, and sections (`narrative.phases`). |
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
- the waves, and what each one waits on, named by task id (these become each task's `deps` in `plan.json`);
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

Pending items show in the dashboard's Start readiness table (Approvals column) and the Project home Planned shelf; the lead reads the full list with `ws approvals`.

## 4. Execute each wave with the `ws-wave` workflow

In the lead session, run one wave. On Claude Code, call the Workflow tool with:

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
- On Claude Code, every step is a low-effort Haiku wrapper around `scripts/run_worker.sh`. On another harness, run one lane per sub-agent with the same build, review, and fix prompts. The runner launches the lane's registry worker, then waits and logs the run.
- On approval it runs `ws task WS4 WS4.1 pr`.
- It returns `approved` branches, `needsLead` lanes with the last review, and `built` lanes.
- It does not push, open a pull request, merge, or take a production step.

Run labels are `ws4-WS4.1-notes-index`, then `-rev1` and `-fix1`. A label that holds the task id pins each attempt on the task chip, and `↻N` counts the attempts; the task map is the override for other labels. `defaultTier` and `reviewTier` default to `default`. Tiers are registry worker names, `default`, or `auto`, read from `workers.example.json`, `workers.local.json`, or `AGENT_DASH_WORKERS`.

## 5. The lead's loop

Each time a wave workflow returns, the lead:

1. Reads the result.
2. Merges approved lanes as trains, using the repo's merge rules.
3. Moves statuses with `ws task`.
4. Runs approved production steps one at a time.
5. Coordinates timing with the other leads.
6. Starts the next wave.

A workflow cannot receive messages from other sessions or wait out multi-day windows. Those stay with the lead.

### Throughput

The lead keeps work flowing: it refills the wave on every completion, runs waits on a background watcher, and groups production steps by area so independent areas run in parallel. Each production batch has one approval row and one rollback line. Commit and push per task, and post results to the tracker as they land. The rules, with their checks, are T1 to T5 in the [orchestration skill](../../../skills/workstream-orchestration/SKILL.md#throughput-rules).

## 6. Watch it

- The Plan page is at `http://127.0.0.1:8765/plan?ws=WS4`.
- The Execution view is at `http://127.0.0.1:8765/static/index.html?ws=WS4`.
- Plan and Execution are one shell. The header toggle switches them.

## Choosing workers

Workers come from a registry: [`../workers.example.json`](../workers.example.json), replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Each worker has a kind (muse, cursor-agent, Grok Build CLI, pi, or Codex), a model, a concurrency cap and quota patterns. `scripts/run_worker.sh` and the `ws-wave` workflow take `default` (the registry's default worker), a worker name, or `auto`. `auto` starts the first worker in `auto_order` that is under its cap and not cooling down after a quota error, and relaunches a lane on the next one when it hits a limit.

## Merging fast: trains and local CI

Production updates go out fastest when every merge is small in CI time, not in scope.

- **Batch into merge trains.** Related approved branches ship as one train branch (`train/<area>-<slug>`) and one PR, so CI and the deploy run once per set.
  - A change ships alone only if it is small (5 files or fewer), has no migrations, and shares no test file with another open branch.
  - Everything else joins a train of at most 8 members.
  - Push a train once, when it is ready; avoid repeated pushes.
- **The merge gate is local, scoped CI.**
  - Each member runs only the explicit test files for the code it changed.
  - A train re-runs only the tests for files that two members both touched, plus one load of shared registries.
  - Never run a whole suite, a bare test-runner invocation, or every pre-commit hook for a partial change.
  - If the scoped selection widens past a few suites, stop and test the file that widened it directly.
- **Never wait on remote CI runners.** Don't trigger, re-run, or poll them to prove a change. Merge with the repo's merge tool once local tests pass, using its flag to skip the remote wait if it has one, and let the deploy workflow verify the cutover.
- **Pre-existing failures.** Prove a failure is pre-existing by re-running that one test on a clean checkout of main. Name it in the PR and move on.
- **Move status as facts change:**
  - `wip` while a branch is being built;
  - `pr` once it is approved or its PR is open;
  - `prog` only after it is merged and a production step remains;
  - `done` when it is merged and live.

  `prog` never means "in progress".

  A worktree whose task reached prog/done/dropped, or whose branch has merged, is removed at the next `ws gc` run (each `ws task` prog/done/dropped move triggers one; leads can run it by hand); leads never `rm -rf` a worktree. `ws task` to prog, done or dropped runs the gc for that workstream (`--no-gc` skips it), and `ws render` ends with a dry-run summary (`worktrees: N live, M removable`).

### Merge trains on the dashboard

The plan's `trains` list is the only source of truth. It is provisional at planning time; the lead re-cuts it as member diffs land. The schema is one object per train: `id` (machine key, unique), `title` (plain phrase, leads), optional `branch` (starts with `train/`), `members` (task ids, at most 8, each in one train at most), `status`, optional `pr`, `shared_files`, `gate`, and `note`.

```
python3 ~/.claude/tools/agent-dash/agent_dash.py ws train WS4 T1 --title "read-path cutover" --add WS4.3,WS4.12
python3 ~/.claude/tools/agent-dash/agent_dash.py ws train WS4 T1 --branch train/api-read-path --status assembling
python3 ~/.claude/tools/agent-dash/agent_dash.py ws train WS4 T1 --status pr --pr 5357
python3 ~/.claude/tools/agent-dash/agent_dash.py ws trains WS4
```

Train status flows `planned` (members may change) → `assembling` → `gate` → `pr` → `merged`, or `dropped` when the train dissolves and its members are reassigned.

Re-cut when each member branch lands: diff it with `git diff --name-only origin/main...<branch>`, intersect the member lists into `shared_files`, set the `gate` to the tests for those files, and move the train status with `ws train`. Set `pr` when the train PR opens and `merged` after it lands. `ws trains` lists every train with its members' task statuses. The Plan page shows the trains; the Execution progress map tags each member chip and adds a legend.
