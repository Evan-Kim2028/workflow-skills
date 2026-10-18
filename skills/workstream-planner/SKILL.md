---
name: workstream-planner
description: Plan a new project or workstream so sub-agents on any harness can execute it and the agent dashboard can track it. It creates the plan bundle (decision record, task DAG, visuals), the reviewed execution plan, the owner approvals list, the lead kickoff brief, and a paste-ready start prompt. Use for "plan a new workstream", "set up a new project", "add WS<n>", "write the kickoff", or turning a design doc into an executable plan.
---

# Workstream planner

Turns intent into a plan that executes with little supervision. The output is a **bundle** that the dashboard renders as a Plan page and tracks live, plus everything a fresh lead session needs. Related skills: `workstream-lead` runs the bundle; `workstream-dashboard` maintains the tool.

Tool: `tools/agent-dash/` in this repo (`~/.claude/tools/agent-dash/` after `./install.sh`). Bundles live in `~/.local/share/agent-dash/workstreams/<ID>/` (or `AGENT_DASH_WORKSTREAMS`) and are private. Reporting, labels, and pricing: [Plug in any agent](../../tools/agent-dash/docs/integrate.md). Schema: `tools/agent-dash/CONTRACT.md`.

## Steps (in order; each one gets an adversarial review before the next)

1. **Scaffold.**
   ```bash
   python3 tools/agent-dash/agent_dash.py ws new WS4 "<title>" --state planned
   ```
   A new workstream starts as `planned` (no tab yet) and names what it waits on in `bundle.json` `depends_on` (workstream ids that must exist; never `OVERVIEW`). For a whole new project with several workstreams, also make a program bundle (`OVERVIEW`) for the cross-workstream story.
2. **Research, then decide.** Collect evidence first, from the code, the docs, and production facts.
   - Then write `plan.json` as a decision record. Each decision gets alternatives with why-not, evidence (file:line), risk, and reversibility.
   - Add a motivation section: today, after, costs, and an honest "where it is not better".
   - Add an agent-friendly litmus, e.g. "one agent adds a lane in 5 or fewer files".
   - Decisions are made from evidence plus a review, not by asking the owner. Ask the owner only about intent and approvals.
3. **Task DAG, with task-level dependencies.**
   - Columns are phases, groups hold tasks `{id, title, status, deps, wave}`, and group edges mean "waits on".
   - **Every task carries `deps`**: the task ids that must be on main (or done, for a production step) before it can start. A task with nothing to wait on has `deps: []`.
   - Derive deps from the execution plan (step 5): a lane in a later wave waits on the tasks its start condition names. Add `wave` when the task is in a lane. Update both whenever the waves change.
   - Task ids are stable machine keys, e.g. `WS4.3`. Statuses are `todo | wip | pr | prog | done | deferred`. A deferred task carries `moved_to` and `note`.
   - Model an outside signal or condition as a `"kind": "gate"` task, and put the condition text in the dependent's `dep_notes` (keyed by the gate's task id).
   - `ws validate` fails on a dep that names an unknown task or the task itself.
   - **Propose merge trains.** Group tasks expected to touch the same files or tests, or that carry migrations, into trains of at most 8; small isolated diffs ship alone. Record them in `plan.json` `trains` with status `planned`. The lead re-cuts them as diffs land.
4. **Visuals first.** The owner reads pictures before text. Put a before-and-after diagram, a chart of the main benefit, and the flow of one unit of work in `visuals.html`, list them in `layout.json`, then:
   ```bash
   python3 tools/agent-dash/agent_dash.py ws render WS4
   python3 tools/agent-dash/agent_dash.py ws validate WS4
   ```
   Every chart has x and y axes and compact numbers.
5. **Execution plan.** Write `docs/execution-plan.md` and `.json`: the file-overlap matrix, waves of lanes that share no file or test file, per lane (worker tier, branch, worktree, brief, explicit test command, review checks, `oneWay` flags), per wave a start condition naming task ids, merge trains with local scoped test lists, the critical path, coordination points, and the remaining production steps with load, pool, and reversibility. Get an adversarial review, then refresh it from main before anyone starts.
6. **Owner approvals.** Put every production step in `approvals.json` (`ws approve WS4 <id> pending --step ... --reversible yes|no --conditions ...`). Ask the owner once, grouped, and record the answers.
7. **Kickoff and start prompt.**
   - `KICKOFF.md` is self-contained: paths, waves, the wave call, run labels `ws4-<task>-<slug>`, `ws task` status moves, merge and test rules, coordination contacts, done criteria, and the naming rule.
   - `START-PROMPT.md` is the short paste-ready prompt that points a fresh session at the brief and the approvals.

## Map checklist (before kickoff)

The plan-progress map stays readable when the plan follows these rules. Run `ws layout WS4` before kickoff and aim for zero MAP warnings (`ws validate` reports the same codes; warnings never fail the exit code).

- Stage 4 to 5 columns with one- or two-word plain names. Never more than 5 (MAP102), never an empty column (MAP103).
- Keep 2 to 4 groups per column and 3 to 12 tasks per group. Merge 1-task groups (MAP109); split groups above 14 (MAP101).
- Title each group with the outcome in plain words (at most 40 chars), never a bare id (MAP105).
- Keep task id suffixes short and unique inside the workstream (at most 12 chars, MAP110).
- Give every group outside the first column a waits-on edge to a group on its left (MAP106).
- Mark outside signals as `kind: gate` diamonds with `signal`, `confirmed_at`, and `fresh_hours`.
- Keep trains at 2 to 8 members; a 1-member train renders untagged (MAP107).
- Put one short condition (at most 80 chars) in `dep_notes` on each cross-group dep (MAP108).
- Keep done tasks in the dag; leave deferred tasks where planned with `moved_to` plus `note` (MAP111).

## Decompose for parallelism

Size work so waves fan out. Detail and evidence: [orchestration principles](../../tools/agent-dash/docs/orchestration.md).

- **Footprint-sized tasks.** S: ≤5 files, no migration, no shared test file, ships alone. M: 6–15 files or one migration, train candidate. L: more, or a generated file, split or single-writer. Write each task's file list down.
- **A file-overlap table per wave.** One row per task naming its files; lanes in one wave share no source or test file; generated files have exactly one writer per wave.
- **Interface-first tasks.** Every fan-out starts with a frozen interface (spec, shapes, manifest, route list) that dependents name in `deps`. A deploy gate is not an interface: everything behind it serializes.
- **Deps that are real.** A dep is a task id that must be on main before the lane starts. Test one by asking: if the dep's branch vanished, would this lane rebuild on wrong ground? If no, cut the edge. Group edges never substitute — except a scope declared `mostly_landed` (already on main, small disjoint adds), which may skip task deps.
- **Gates for outside signals.** Anything owned elsewhere is a `"kind": "gate"` task with the condition in the dependent's `dep_notes`; every gate carries a freshness bound, and an expired gate blocks starts.
- **Trains grouped by shared files.** Batch tasks expected to touch the same files or carry migrations, 2 to 8 members; re-cut from member diffs as they land. A single-member train renders untagged with a MAP107 warning: one table, one migration number, or one drop ships alone as a task note, not a train.
- **Definition of ready and done.** Ready: deps done or merged, wave start condition holds, no other lane owns its files, production step approved. Done: merged to main and live where the task says live (`wip` → `pr` → `prog` → `done`; `prog` only after merge with a production step left).
- **Acceptance numeric, with tolerance.** Every task names metric, threshold plus tolerance, and probe command; the reviewer reports evidence and the lead closes on the number, not in chat.
- **Check max parallelism before kickoff.** Run `ws waves`: if the plan's widest wave is 1 where lanes could be disjoint, re-split before anyone starts.

## Rules

- **Plan at task level.** Strategy, re-plans, and wave changes always update task `deps`, not only group edges and prose.
- **Name things, not numbers.** Never cite a decision, task, PR, ADR, or issue by its number alone. Put a brief phrase first; the number can follow in parentheses.
- **Use workers for the heavy lifting.** Research, drafting, reviews, and patches go to sub-agents; the planner thread decides, unifies, and draws.
- **Don't keep the owner waiting.** If something can start safely, ship it with a note to the lead rather than chaining one more improvement in front of the owner's start.
- **Keep bundles private.** Only the tool and the skills are public.

### If you are on Claude Code

Code search starts with CodeGraph; lanes run through the `ws-wave` Workflow with Haiku wrappers around `run_worker.sh`; tiers come from the worker registry (`default`, a name, or `auto`). The wave call and the tier shapes are in `tools/agent-dash/docs/workstream-lead.md`.
