---
name: workstream-planner
description: Plan a new project or workstream so cheap workers can execute it and the agent dashboard can track it. It creates the plan bundle (decision record, task DAG, visuals), the reviewed execution plan, the owner approvals list, the lead kickoff brief and a paste-ready start prompt. Use for "plan a new workstream", "set up a new project", "add WS<n>", "write the kickoff", or turning a design doc into an executable plan.
---

# Workstream planner

Turns intent into a plan that executes with little supervision. The output is a **bundle** that the dashboard renders as a Plan page and tracks live, plus everything a fresh lead session needs. Related skills: `workstream-lead` runs the bundle; `workstream-dashboard` maintains the tool.

Tool: `~/.claude/tools/agent-dash/` (`agent_dash.py`). Bundles live in `~/.local/share/agent-dash/workstreams/<ID>/` and are private (never committed to a public repo). Full walk-through: `tools/agent-dash/docs/workstream-lead.md`.

## Steps (in order; each one gets an adversarial review before the next)

1. **Scaffold.**
   ```bash
   python3 ~/.claude/tools/agent-dash/agent_dash.py ws new WS4 "<title>"
   ```
   For a whole new project with several workstreams, also make a program bundle (`OVERVIEW`) for the cross-workstream story.
2. **Research, then decide.** Collect evidence first, from the code (CodeGraph), the docs and production facts.
   - Then write `plan.json` as a decision record. Each decision gets alternatives with why-not, evidence (file:line), risk and reversibility.
   - Add a motivation section: today, after, costs, and an honest "where it is not better".
   - Add an agent-friendly litmus, e.g. "one agent adds a lane in ≤5 files".
   - Decisions are made from evidence plus a review, not by asking the owner. Ask the owner only about intent and approvals.
3. **Task DAG, with task-level dependencies.**
   - Columns are phases, groups hold tasks `{id, title, status, deps, wave}`, and group edges mean "waits on".
   - **Every task carries `deps`**: the task ids that must be on main (or done, for a production step) before it can start. Deps cross groups. Group edges alone are not enough; the owner reads how tasks relate inside a tile and across tiles. A task with nothing to wait on has `deps: []`.
   - Derive deps from the execution plan (step 5): a lane in a later wave waits on the tasks its start condition names, sequential holds chain one to the next, and a production step waits on the code task it rolls out. Add `wave` (the wave's human name) when the task is in a lane. Update both whenever the waves change.
   - Task ids are stable machine keys, e.g. `WS4.3`.
   - Statuses are `todo | wip | pr | prog | done | deferred`. A deferred task carries `moved_to` and `note`.
   - `ws validate` fails on a dep that names an unknown task or the task itself.
4. **Visuals first.** The owner reads pictures before text. Put these in `visuals.html`:
   - a before and after diagram;
   - a chart of the main benefit;
   - the flow of one unit of work.

   List them in `layout.json`. Every chart has x and y axes and compact numbers. Then:
   ```bash
   python3 agent_dash.py ws render WS4
   python3 agent_dash.py ws validate WS4
   ```
5. **Execution plan.** Write `docs/execution-plan.md` and `.json`. It contains:
   - a file-overlap matrix from the code;
   - waves of lanes that share no file or test file;
   - per lane: worker tier, branch, worktree, brief, explicit test command, and review checks (`reviewFocus`), with `oneWay` lanes flagged;
   - per wave, a start condition that names task ids, so the task `deps` in `plan.json` follow from it;
   - merge trains (a change ships alone only when small, migration-free and sharing no test file; otherwise it joins a train of at most 8), with each train's local scoped test list;
   - the critical path;
   - coordination points with other workstreams;
   - the remaining production steps, with load, pool and reversibility.

   Get an adversarial review, then refresh it with current facts from main and the other leads before anyone starts.
6. **Owner approvals.** Put every production step in `approvals.json` (`ws approve WS4 <id> pending --step ... --reversible yes|no --conditions ...`). Ask the owner once, grouped, and record the answers.
7. **Kickoff and start prompt.**
   - `KICKOFF.md` is self-contained. It covers paths, waves, the `ws-wave` call, run labels `ws4-<task>-<slug>`, `ws task` status moves, merge and test rules, coordination contacts, done criteria and the naming rule.
   - `START-PROMPT.md` is the short paste-ready prompt that points a fresh session at the brief and the approvals.

## Rules

- **Plan at task level.** Strategy, re-plans and wave changes always update task `deps`, not only group edges and prose.
- **Name things, not numbers.** Never cite a decision, task, PR, ADR or issue by its number alone. Put a brief phrase first; the number can follow in parentheses.
- **Use workers for the heavy lifting.** Research, drafting, reviews and patches go to the authorized worker tiers in the worker registry (`workers.local.json`, muse by default). The planner thread decides, unifies and draws.
- **Don't keep the owner waiting.** If something can start safely, ship it with a note to the lead rather than chaining one more improvement in front of the owner's start.
- **Keep bundles private.** Only the tool and the skills are public.
