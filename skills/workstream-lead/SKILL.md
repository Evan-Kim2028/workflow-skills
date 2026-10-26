---
name: workstream-lead
description: Run a planned workstream end to end as its lead session on any agent harness. Read the bundle's kickoff brief, run each wave of parallel sub-agents to done, move task status so the dashboard tracks it, act only on approved production steps, coordinate with other leads, and hand off cleanly. Use when a session is told "you are the WS<n> lead", is given a START-PROMPT, or picks up a workstream mid-flight.
---

# Workstream lead

The lead plans, decides, verifies, and reports. Workers do everything else. Related skills: `workstream-planner` builds the bundle; `workstream-dashboard` maintains the tool. Reporting, labels, verdicts, and pricing: [Plug in any agent](../../tools/agent-dash/docs/integrate.md).

## Start (or resume)

1. Read `~/.local/share/agent-dash/workstreams/<ID>/KICKOFF.md` (or `AGENT_DASH_WORKSTREAMS`), including any addendum, then `approvals.json` and `docs/`.
2. Activate the workstream at kickoff: `python3 tools/agent-dash/agent_dash.py ws state <ID> active`. A `planned` bundle has no tab; it appears on the dashboard once active.
3. Check reality before acting:
   - `python3 tools/agent-dash/agent_dash.py doctor --ws <ID>` (fix or note what it lists);
   - `plan.json` statuses;
   - open PRs and `git log origin/main`;
   - live workers on the dashboard.
4. Message the other leads named in the brief for shared files and the production slot. Messages that change production load need the owner's approval, unless `approvals.json` already grants direct coordination.

## Each wave

Execution follows the [orchestration loop](../workstream-orchestration/SKILL.md#the-loop): `ws next`, launch every ready lane up to caps, one watcher per run, and on each finish read only that report, move status, and launch what it unblocked before anything else. The four rules below were the most violated in measured runs; treat them as hard stops, not advice.

1. Build the lane list from the execution plan. Lanes in one wave share no file or test file. Use explicit review checks for lanes that need them, and flag irreversible or production-adjacent lanes.
2. Run one lane per sub-agent, in parallel. Each lane goes build, then review, then fix and re-review until approved or out of fix rounds.
3. Read the result:
   - `approved`: assemble trains and merge with the repo's merge tool.
   - `needsLead`: read the last review and decide. Fix, re-scope, or defer with `ws task <ID> <task> deferred --note ...`.
4. Move status with `python3 tools/agent-dash/agent_dash.py ws task <ID> <task> <todo|wip|pr|prog|done|deferred>`, in the same step as the event. The dashboard and the Plan page read it live.
5. Start the next wave as soon as its start condition holds. Don't wait on lower wave numbers. A task is ready when every id in its `deps` is done or merged.
6. When you re-plan (split, merge, move or add a task, or change a wave), update the task `deps` and `wave` in `plan.json` in the same step, then run `ws validate`. Model an outside signal as a `"kind": "gate"` task, with the condition in the dependent's `dep_notes`.

### Four hard rules

- **Delegate every multi-step job.** The lead plans, decides, verifies, and reports; workers do everything else. Any read of more than one or two files, any run longer than a few minutes, any wait goes to a worker.
- **Never foreground-wait.** Waits go to one background watcher per run or a cheap worker returning one verdict. Never sleep in the lead thread.
- **Never kill to rescope.** A killed run leaves no report and its cost is lost. Let it finish, or launch a separate run; after 2 failed attempts at the same task, replan.
- **Move status as events happen.** `wip` on launch, `pr` on open, `done` on merged-and-live — in the same step, not at end of day.

At each milestone run `agent_dash.py lead-profile` on the lead session: a LOW-DELEGATION or over-limit flag gets a re-brief or a fresh successor, not a continuation.

### If you are on Claude Code

Run the wave with the `ws-wave` Workflow tool: `scriptPath` is `tools/agent-dash/workflows/ws-wave.js` and the args are in the brief. Tiers come from the worker registry: `default`, a named tier, or `auto` for failover by availability. Each step is a low-effort Haiku wrapper around `scripts/run_worker.sh`. A lane may set `reviewFocus` (checks with evidence) and `oneWay: true` (irreversible lanes, still reviewed). Approved lanes move to `pr` via `ws task`.

## Throughput

The full rules are T1 to T5 in the [orchestration skill](../workstream-orchestration/SKILL.md#throughput-rules). Keep three habits:

- **Refill on every completion.** Read that one report, move its status, launch the next ready batch, then read the next report.
- **Wait in the background.** Each deploy, soak, or watch runs under one watcher; the lead thread never sleeps.
- **Batch production by area.** One approved batch per area per window, with one rollback line each; independent areas run in parallel.

## Production steps

- Act only on items marked `approved` in `approvals.json`, and follow their conditions. Run one production step at a time across all workstreams that share the slot.
- For a new production step, record it as pending: `ws approve <ID> <id> pending --step ... --reversible ... --conditions ...`. Ask the owner in your session, record their answer with `ws approve`, and only then act.
- Waits (deploys, soaks, shadow windows) go to a background watcher or a cheap worker that returns one verdict. Never sleep in the lead thread.

## Labels, logging, tokens

- Run labels are `<id lowercase>-<task-id>-<slug>`, plus `-rev<n>` and `-fix<n>` for reviews and fixes. The dashboard pins each run onto its task chip and counts attempts.
- Report every run: `report start`, `report usage`, `report finish --verdict PASS|PARTIAL|BLOCKED`. First line of a worker report is `PASS`, `PARTIAL`, or `BLOCKED`; reviews start with `VERDICT: APPROVE` (or `APPROVE_WITH_NITS`, `APPROVE_WITH_CHANGES`, `REQUEST_CHANGES`).

## Merging fast: trains and local CI

Production updates go out fastest when every merge is small in CI time, not in scope.

- **Batch into merge trains.** Related approved branches ship as one train branch (`train/<area>-<slug>`) and one PR, so CI and the deploy run once per set.
  - A change ships alone only if it is small (5 files or fewer), has no migrations, and shares no test file with another open branch.
  - Everything else joins a train of at most 8 members.
  - Push a train once, when it is ready; avoid repeated pushes.
- **Re-cut the plan's trains as member diffs land.** Per member `git diff --name-only origin/main...<branch>`, intersect the lists into the train's `shared_files`, and set its `gate` to the tests for those files. Move the train status with `ws train <ID> <train> --status ...` (`planned | assembling | gate | pr | merged | dropped`), set `--pr` when the train PR opens and `merged` after it lands. `--add` moves a task out of any other train. `ws trains <ID>` lists them.
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

  When the workstream is linked to Linear, these commands do not change. Linear follows within a minute. See [Linear as a task backend](../../tools/agent-dash/docs/linear.md).

## Linear

If the workstream is linked to Linear (`ws linear link`), keep the Linear side current with the protocol in [Agents and Linear](../../tools/agent-dash/docs/linear.md#agents-and-linear):

- Change status with `ws task` as usual. Linear follows within a minute.
- Post one standup update before each standup with `ws linear update <ID>` (`--dry-run` to preview). Or set `tracker.update.daily_hour` so the dashboard posts one each day. Both are described in [Standup updates](../../tools/agent-dash/docs/linear.md#standup-updates).
- Put the `Fixes ENG-…` line from `ws trains <ID>` in each train PR body.
- Never edit issue titles or descriptions in Linear. The plan owns them.

## Hand-off

At about 250K context, or at a milestone, run `doctor --ws <ID>` once more, then write `state.md`: done, in flight (labels and PIDs), next wave, blockers and messages owed. Then end. A fresh lead resumes from the brief plus `state.md`. Hub reports include `doctor report --md` output.

## Completion: closing report, then archive

When the plan is done, write `docs/closing-report.md` in the bundle and, after owner review, run `ws archive` (archiving is manual; nothing auto-archives). The report is short:

- What shipped (the lanes and trains that landed, with links to the plan view and the merged PRs).
- Decisions (the decision ids from `plan.json` that still bind, each with its one-line why).
- Residuals (deferred tasks and where they moved, open follow-ups and who owns them).
- Links (kickoff brief, state reports, the dashboard plan view).

```bash
python3 tools/agent-dash/agent_dash.py ws archive <ID>
```

`ws archive` refuses unless the plan is 100% done of non-deferred tasks and the closing report exists (or `--force --note "..."` for a reviewed early archive). It freezes `summary.json` — started/finished dates, duration, task counts by status, trains, approvals counts, lifetime tokens/cost — and flips state to `archived` without deleting anything. `ws restore <ID>` reopens to `done` and keeps the summary.

## Rules

- **Name things, not numbers:** in anything written for the owner, put a short phrase first; the number can follow in parentheses.
- **Use the repo's local scoped tests and merge tool.** Never wait on remote CI to prove a change.
- **Never `rm -rf` a worktree.** A worktree whose task reached prog/done/dropped, or whose branch has merged, is removed at the next `ws gc` run (each `ws task` prog/done/dropped move triggers one; leads can run it by hand); the lead only merges.
- **The owner's instructions in the session win** over the brief when they differ.
