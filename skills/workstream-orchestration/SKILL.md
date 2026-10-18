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
2. Launch every ready lane up to the worker caps in one wave. One writer per
   file per wave; disjoint lanes only.
3. One background watcher per run, then end the turn. Never wait in the lead.
4. On each finish, before anything else: read only that report, move its
   status (`ws task`), launch what it unblocked.
5. `doctor --ws` each cycle — fix what it lists before the next launch.

Small work is one wide wave. A wave whose start condition holds launches;
wave numbers never gate it.

## Principles (ordered by measured impact)

1. **Keep the ready queue empty.** Idle ready tasks were up to 95% of the
   clock in the worst case. Check: ready count on the Plan page is 0.
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
wave assumed). Launch up to caps, one watcher per run.

**After each finish:** read only that report → move status (`ws task`) →
launch what it unblocked → then read the next report. Finished runs are
picked up before any new launch.

**End of day:** `doctor report --md`; deferred ledger reviewed (`ws deferred`
names each receiving end); `state.md` current; tomorrow's ready set named.
