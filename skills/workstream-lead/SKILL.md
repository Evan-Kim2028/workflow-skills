---
name: workstream-lead
description: Run a planned workstream end to end as its lead session. Read the bundle's kickoff brief, run each wave with drive_lanes.py so registry workers (muse, cursor-agent Grok and Composer, GLM via grok-build) do the work, use the ws-wave workflow when you want its progress view, move task status so the dashboard tracks it, act only on approved production steps, coordinate with other leads, and hand off cleanly. Use when a session is told "you are the WS<n> lead", is given a START-PROMPT, or picks up a workstream mid-flight.
---

# Workstream lead

The lead plans, decides, verifies and reports. Workers do everything else. Related skills: `workstream-planner` builds the bundle; `workstream-dashboard` maintains the tool.

## Start (or resume)

1. Read `~/.local/share/agent-dash/workstreams/<ID>/KICKOFF.md`, including any addendum, then `approvals.json` and `docs/`.
2. Check reality before acting:
   - `plan.json` statuses;
   - open PRs and `git log origin/main`;
   - live workers on the dashboard (`/static/index.html?ws=<ID>`).

   A resumed workstream may already be partly done.
3. Message the other leads named in the brief for shared files and the production slot. Messages that change production load need the owner's approval, unless `approvals.json` already grants direct coordination.

## Each wave

1. Build the lane list from the execution plan. Lanes in one wave share no file or test file. Use `reviewFocus` for lanes that need explicit checks, and `oneWay: true` for irreversible or production-adjacent lanes.
2. Run the wave with `drive_lanes.py`. That is the default. Pass the same JSON object `ws-wave` takes, as a file. `--only` drives one task. `--start rev` or `--start fix` resumes a lane, and `--findings` is the review file for a fix resume. Each lane can set `tier`, `reviewTier`, `fixTier`, `model`, `reviewModel`, and `fixModel`. Tiers are registry names: `default` (muse), `grok`, `composer`, `glm`, `glm-flash`, or `auto`. `deps` lists task ids in this run, the same name as in `plan.json`. The lane starts when each of those finished `APPROVE*` or `built`. Otherwise the result is `skipped-dep` and names the dependency. `base` of `dep:<task>` cuts the worktree from that task's branch. Independent lanes start together. Unknown ids and cycles throw. Use the `ws-wave` workflow when you want the progress view. See "Two ways to run a wave".

   One wave can mix a Grok build, a Composer review, and a stacked follow-on:

   ```json
   [
     {"task": "WS4.1", "slug": "notes-index", "tier": "grok", "reviewTier": "composer", "reviewModel": "composer-2.5", "branch": "ws4/notes-index", "worktree": "/abs/wt-notes-index", "brief": "Add a notes index.", "tests": "pytest tests/test_notes.py", "review": true},
     {"task": "WS4.2", "slug": "notes-search", "deps": ["WS4.1"], "base": "dep:WS4.1", "tier": "grok", "branch": "ws4/notes-search", "worktree": "/abs/wt-notes-search", "brief": "Add search on the notes-index branch.", "tests": "pytest tests/test_search.py", "review": true}
   ]
   ```

   Read `runs` on each lane for the tier and model the runner actually used.
3. Read the result:
   - `approved`: assemble trains and merge with the repo's merge tool.
   - `needsLead`: read the last review and decide. Fix, re-scope, or defer with `ws task <ID> <task> deferred --note ...`.
   - `skipped`: a dependency ended short of `APPROVE*` or `built`. The row names that dependency.
4. Move status with `python3 ~/.claude/tools/agent-dash/agent_dash.py ws task <ID> <task> <todo|pr|prog|done|deferred>`, as soon as each fact changes. The dashboard and the Plan page read it live.
5. Start the next wave as soon as its start condition holds. Don't wait on lower wave numbers. A task is ready when every id in its `deps` is done or merged.
6. When you re-plan (split, merge, move or add a task, or change a wave), update the task `deps` and `wave` in `plan.json` in the same step, then run `ws validate`.

## Two ways to run a wave

`drive_lanes.py` runs a build, review, and fix loop as a plain process. It calls the runner, waits on the worker, and reads the verdict line. A Claude wrapper can answer a chat message and drop the lane. This driver cannot, and the step costs no Claude tokens.

`ws-wave` is the workflow. Use it when you want the run in the workflow progress view. The script path is `<abs>/agent-dash/workflows/ws-wave.js`. `wrapperModel` defaults to `haiku` and `wrapperEffort` defaults to `low`.

Same lane JSON either way. Exit 0 when nothing needs the lead, and 3 when something does. The driver writes `drive-<task>.json` in the run directory and prints the same result shape as `ws-wave`. It does not push, open a pull request, merge, or touch production.

```bash
python3 <abs>/agent-dash/scripts/drive_lanes.py /absolute/path/lanes.json
python3 <abs>/agent-dash/scripts/drive_lanes.py /absolute/path/lanes.json --only WS4.1 --start rev
```

Stack a dependent lane on its dependency's branch (`base` `dep:<task>`) instead of waiting for the merge, and never rebase or amend a member branch other lanes are stacked on.

Assemble the train while lanes are still in review.

Ship code-only members in their own train ahead of prod-affecting ones so the next wave unblocks.

One writer per worktree, but a second writer on a different branch and worktree may run at once.

## Production steps

- Act only on items marked `approved` in `approvals.json`, and follow their conditions. Run one production step at a time across all workstreams that share the slot.
- For a new production step, record it as pending: `ws approve <ID> <id> pending --step ... --reversible ... --conditions ...`. Ask the owner in your session, record their answer with `ws approve`, and only then act.
- Waits (deploys, soaks, shadow windows) go to a background watcher or a cheap worker that returns one verdict. Never sleep in the lead thread.

## Labels, logging, tokens

- Run labels are `<id lowercase>-<task-id>-<slug>`, plus `-rev<n>` and `-fix<n>` for reviews and fixes. The dashboard pins each run onto its task chip and counts attempts.
- Launch workers only through the tool's launchers (`scripts/run_worker.sh`, `scripts/drive_lanes.py`, or `ws-wave`), which log billed tokens and session manifests. Never launch Grok with `--output-format text`.

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

## Hand-off

At about 250K context, or at a milestone, write `state.md`: done, in flight (labels and PIDs), next wave, blockers and messages owed. Then end. A fresh lead resumes from the brief plus `state.md`.

## Rules

- **Name things, not numbers:** in anything written for the owner, put a short phrase first; the number can follow in parentheses.
- **Use the repo's local scoped tests and merge tool.** Never wait on remote CI to prove a change.
- **The owner's instructions in the session win** over the brief when they differ.
