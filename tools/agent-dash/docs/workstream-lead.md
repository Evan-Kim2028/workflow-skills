# Running a workstream end to end

A workstream is planned once and then executed in waves. `ws-wave` runs any registry harness: muse, cursor-agent Grok, cursor-agent Composer, and GLM via grok-build. One Claude lead session decides, merges, and runs production steps. The dashboard shows the work live.

The install path and the first wave are in the repository [quickstart](../../../docs/quickstart.md). Labels, tokens, and the task map are in [concepts](../../../docs/concepts.md).

## 1. Plan once

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws new WS4 "Notes index"
```

This creates a bundle in `~/.local/share/agent-dash/workstreams/WS4/`. Fill these files:

| File | What goes in it |
|---|---|
| `plan.json` | The decision record. Decisions with alternatives and evidence. Motivation: today, after, costs, and where it is not better. The agent-friendly litmus. The task graph: `dag.cols`, `groups[].tasks` with `id`, `title`, `status`, `deps` (the task ids it waits on, across groups) and optional `wave`, plus group `edges`. The narrative: thesis, end state, and phases. |
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

Pending items show in the dashboard's "Needs you" card and in the Plan page's Approvals section.

## 4. Two ways to run a wave

`drive_lanes.py` is the default for a build, review, and fix loop. It calls the runner, waits on the worker, and reads the verdict line. A Claude wrapper can answer a chat message and drop the lane. This driver cannot, and the step costs no Claude tokens.

`ws-wave` is the workflow. Use it when you want the run in the workflow progress view. Same lane JSON either way. The driver reads that JSON from a file. Exit 0 when nothing needs the lead, and 3 when something does. It writes `drive-<task>.json` in the run directory and prints the same result shape as `ws-wave`. It does not push, open a pull request, merge, or touch production.

```bash
python3 ~/.claude/tools/agent-dash/scripts/drive_lanes.py /absolute/path/lanes.json
python3 ~/.claude/tools/agent-dash/scripts/drive_lanes.py /absolute/path/lanes.json --only WS4.1 --start rev
```

`--only` drives one task. `--start rev` or `--start fix` resumes a lane, and `--findings` is the review file for a fix resume.

Stack a dependent lane on its dependency's branch (`base` `dep:<task>`) instead of waiting for the merge, and never rebase or amend a member branch other lanes are stacked on.

Assemble the train while lanes are still in review.

Ship code-only members in their own train ahead of prod-affecting ones so the next wave unblocks.

One writer per worktree, but a second writer on a different branch and worktree may run at once.

### Workflow progress view

In the lead session, call Claude Code's Workflow tool with:

```text
scriptPath: the absolute path of ~/.claude/tools/agent-dash/workflows/ws-wave.js
args: {
  "ws": "WS4", "repo": "/absolute/path/to/your-repo",
  "runDir": "/absolute/path/to/agent-scratch/notes",
  "template": "/absolute/path/to/worker-rules.md",
  "runner": "/absolute/path/to/run_worker.sh",
  "dash": "/absolute/path/to/agent_dash.py",
  "maxFixRounds": 2, "defaultTier": "default", "wrapperModel": "haiku",
  "lanes": [
    { "task": "WS4.1", "slug": "notes-index", "tier": "grok",
      "reviewTier": "composer", "reviewModel": "composer-2.5",
      "branch": "ws4/notes-index", "worktree": "/absolute/path/to/wt-notes-index",
      "brief": "Add a notes index. Write only in this worktree.",
      "tests": "pytest tests/test_notes.py", "review": true },
    { "task": "WS4.2", "slug": "notes-search", "deps": ["WS4.1"], "base": "dep:WS4.1",
      "tier": "grok", "branch": "ws4/notes-search",
      "worktree": "/absolute/path/to/wt-notes-search",
      "brief": "Add search on the notes-index branch.",
      "tests": "pytest tests/test_search.py", "review": true }
  ]
}
```

`runDir` has to be a scratch root the collectors scan. The shipped glob `$HOME/agent-scratch/*` matches only the directories directly inside `$HOME/agent-scratch/`. Use `$HOME/agent-scratch/notes`. `realpath` prints the absolute tool paths. The [quickstart](../../../docs/quickstart.md) has the copy-paste form, including the task map WS4 needs before a chip lights up.

What the workflow does:

- Independent lanes start at once. A lane with `deps` starts when every named task in this run finished `APPROVE*` or `built`. Any other dependency result skips that lane with final `skipped-dep` and names the dependency. `base: "dep:<task>"` cuts the worktree from that task's branch. Unknown task ids and cycles throw before any lane starts. The build setup marks the task `wip` before it creates the worktree.
- Each lane goes build, then review, then fix and re-review until approved, or until it runs out of fix rounds.
- Build tier is `lane.tier`, else `defaultTier`, else `default`. Review tier is `lane.reviewTier`, else `reviewTier`, else the build tier. Fix tier is `lane.fixTier`, else `fixTier`, else the build tier. `model`, `reviewModel`, and `fixModel` override the registry model for that role. The example above builds WS4.1 with Grok, reviews it with Composer, and stacks WS4.2 on WS4.1's branch.
- Every step is a Claude wrapper around `scripts/run_worker.sh`. `wrapperModel` defaults to `haiku` and `wrapperEffort` defaults to `low`. The runner launches the registry worker and waits. The lane's `runs` list repeats the tier and model from each STATUS line.
- On approval it runs `ws task WS4 WS4.1 pr`.
- It returns `approved` branches, `needsLead` lanes with the last review, `built` lanes, and `skipped` lanes.
- It does not push, open a pull request, merge, or take a production step.

Run labels are `ws4-WS4.1-notes-index`, then `-rev1` and `-fix1`. With a task-map entry for that label, the dashboard pins each attempt on the task chip, and `↻N` counts the attempts. `run_worker.sh` accepts `default`, `auto`, or any worker name in the registry.

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

Workers come from a registry: [`../workers.example.json`](../workers.example.json), replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Shipped tiers: `muse`, `grok` (cursor-agent, model `grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]`), `composer` (cursor-agent, model `composer-2.5`), `glm` and `glm-flash` (grok-build). Each has a concurrency cap and quota patterns. A cursor cap counts only processes for that model. `auto` starts the first worker in `auto_order` that is under its cap and not cooling down after a quota error, and relaunches a lane on the next one when it hits a limit. `composer` is after `grok` in that list.

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
