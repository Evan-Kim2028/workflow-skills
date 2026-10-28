---
name: workstream-orchestration
description: Keep parallel agent work moving as a hub or workstream lead on any harness: run the steady-state loop (ready queue to waves to pickup), enforce the twelve parallelism principles, and recognize the measured failure modes from the dashboard before they cost a day. Use when leading a workstream, coordinating several of them, reviewing a lead session, or diagnosing why ready work is idle.
---

# Workstream orchestration

For the hub and every lead, on any harness. The planner skill builds a
parallel plan; the lead skill runs one workstream; this skill is the
steady-state loop that keeps ready work in flight. Reporting, labels, and
pricing: [Plug in any agent](../../tools/agent-dash/docs/integrate.md).
Full evidence: [orchestration principles](../../tools/agent-dash/docs/orchestration.md).

## When to use

- You lead a workstream with more than one lane, or coordinate several leads.
- Ready tasks sit idle, a wave stalls on one slow lane, or launches jump deps.
- You review a lead session (`agent_dash.py lead-profile`) or plan the day.

## The loop

One cycle, every cycle; the details live in the checklists below.

1. `ws next` — list startable tasks, ranked by what they unblock.
2. Keep the slots full from the ready queue: the dispatcher launches every
   ready task that has a prompt, up to `slots`. One writer per file per wave;
   disjoint lanes only.
3. One background `dispatch wait <WS>` per workstream, then end the turn. Never
   wait in the lead and never one watcher per run.
4. On each event, before anything else: read only that report, decide, and
   write the prompts that the finish unblocked. The dispatcher moves status.
5. `doctor --ws` each cycle — fix what it lists before the next launch.

Small work is one wide wave. A wave whose start condition holds launches;
wave numbers never gate it. The dispatcher's one event stream is in
[the dispatcher doc](../../tools/agent-dash/docs/dispatcher.md).

## Principles (ordered by measured impact)

1. **Keep the ready queue empty.** Idle ready tasks were up to 95% of the
   clock in the worst case. Refill on every finish, before any other step.
   Check: ready count on the Plan page is 0, or running count sits at the cap.
2. **Never launch past an unfinished dependency.** Dozens of tasks started
   before a dep finished and paid it back in rework. Check: `ws next` lists
   the task, i.e. every dep is done or merged.
3. **Interface-first tasks.** A frozen spec unblocks a real fan-out; a deploy
   gate serializes everything behind it. Check: any task with many dependents
   names its interface task in `deps`.
4. **One writer per file per wave.** Shared files mean rebases and stacked
   fixes. Check: `ws overlap` reports no collision in the wave.
5. **Disjoint waves from a file-overlap table.** The best plan named one file
   list per task and waved non-overlapping sets. Check: the execution plan
   carries the table; `ws waves` agrees with it.
6. **Numeric acceptance in every brief.** Lanes without a number haggled in
   chat at the end. Check: metric, threshold plus tolerance, probe command.
7. **Cap review rounds and escalate.** Review loops were 74% of runs where
   measured. Check: fix rounds bounded (2 default); third round goes to the lead.
8. **Cut the tail.** One slow fix lane held waves for hours while the rest
   sat done. Check: wave tail (max end minus median end) under one hour, else
   split or reassign the slow lane.
9. **Label every run with its task id.** Half the tasks had no runs mapped,
   so progress was invisible. Check: no unmapped rows above the progress map.
10. **Move status in the same step as the event.** The plan lagged reality
    for days. Check: `wip` on launch, `pr` on open, `done` on merged-and-live.
11. **Gates carry a freshness bound and a named signal.** A gate checked once
    decays silently. Check: `ws gates` shows every gate fresh, each with its
    condition text and owner.
12. **Cross-workstream holds go through the hub with a deadline.** Holds were
    the binding constraint everywhere measured. Check: waiting tasks name the
    hub relay and the date they resume; local ready work runs meanwhile.

## Throughput rules

Added after the measured twelve, so they carry no measurements yet. They
follow from the twelve and the loop. Each one ends in a check that names an
observable. Numbered T1 to T5 so the measured numbering stays stable.

- **T1. Never idle.** On every worker completion, read only that report, move
  its status, and launch the next ready batch in the same turn. Keep running
  workers at the cap while ready work exists; no turn ends with ready work and
  free capacity. Check: ready count and running count on the dashboard, with
  running at the cap or ready at 0.
- **T2. Break sequential chains.** Run read-only research, review and
  verification beside the writers. Split a long serial task into one interface
  task plus parallel implementers (principle 3). Put every wait on a background
  watcher, never in the foreground. Check: critical path length in `ws next`
  or the Plan page is shorter than the previous wave's; no foreground sleeps in
  any lead session.
- **T3. Production in batches by area.** Group production steps by area: host,
  service, table family, or schedule. Run independent areas in parallel and
  serialize only within an area. Each batch has one approval row, one
  rollback line, and a named verification signal, and lands in a quiet window.
  Check: every production step belongs to a batch with an area, an approval
  row, and a rollback line; no two batches touch the same area at once.
- **T4. Ship continuously.** Commit per task. Push the branch or train when its
  local gate passes, open the PR, merge on the green local gate, and move status
  in the same step (principle 10). Finished work never sits unpushed. Check: no
  `done` task without a merged PR or a recorded reason; `git log origin/main`
  contains the work.
- **T5. Keep the tracker in step.** On a Linear-linked workstream, change status
  with `ws task`, post results with `ws linear comment`, and post the standup
  update before standup. Check: `ws linear status` shows an empty outbox and no
  error.

## Anti-patterns (symptom → rule broken)

Each names its doctor code; `doctor --ws <ID>` flags them on the live
board.

- **Serialized execution:** 1 live worker, open lanes. One workstream ran 5x
  its critical path this way. → Principles 1, 2, 5.
- **Starving queue:** ready count > 0 while live workers ≤ 1.
  Doctor: `idle-ready`. → Principle 1.
- **Jumped dep:** a lane starts whose dep is not done. Doctor flags the
  edge: `dep-jump`. → Principle 2.
- **Review spiral:** attempt count climbs past the fix cap.
  Doctor: `review-spiral`. → Principle 7.
- **Burst-plus-tail:** wave done except one lane for hours.
  Doctor: `long-tail`. → Principle 8.
- **Ghost work:** runs above the map map to no task.
  Doctor: `ghost-runs`. → Principle 9.
- **Stale plan:** merged work still `wip`; finished runs unread. → Principle 10.
- **Stale gate:** `ws gates` shows an expired check treated as green.
  Doctor: `stale-gate`. → Principle 11.
- **Idle hold:** zero live workers while waiting on another group. →
  Principle 12.
- **Orphaned handoff:** a deferred task with no receiving end; the sender
  counts it gone and nobody counts it at all. Doctor: `deferred-orphan`.
  → Principle 12.
- **Lead does worker jobs:** every profiled lead session passed the context
  limit doing reads and runs directly. `agent_dash.py lead-profile` flags it.
  Doctor: `lead-undelegated`. → Delegate every multi-step job; hand off
  at the limit (next section).
- **Foreground waiting and kill-to-rescope:** sleeps in the lead thread;
  killed runs leave no report and their cost is lost. → Never wait, never
  kill to rescope; launch a separate run.

## Linear reporting

On a Linear-linked workstream, each worker reports its verdict and PR link with `ws linear comment <ID> <task-id> "<verdict and PR link>"`. Comments never change status; move the status with `ws task`. Keep each comment to a verdict and links, because the text is sent to Linear. See [Agents and Linear](../../tools/agent-dash/docs/linear.md#agents-and-linear).

## Handoff and context

At ~250k context or a milestone, write `reports/<ID>/state.md`: done,
in-flight labels and PIDs, next wave, blockers, messages owed. Then end; the
hub starts a fresh lead from the brief plus `state.md`. Run
`agent_dash.py lead-profile` at each milestone: a LOW-DELEGATION or
over-limit flag gets a re-brief or a successor, not a continuation.

## Checklists

**Before a wave:** `ws next` lists the lanes; `ws waves` confirms the max
parallelism; `ws overlap` is clean; every lane has numeric acceptance and a
review tier; trains re-cut from member diffs (`ws deferred` shows nothing the
wave assumed). Write the prompts, `dispatch start`, then one `dispatch wait`.

**After each event:** read only that finished report → decide → write the
prompts it unblocked → re-arm `dispatch wait`. Finished runs are picked up
before any new prompt.

**End of day:** `doctor report --md`; deferred ledger reviewed (`ws deferred`
names each receiving end); `state.md` current; tomorrow's ready set named.
