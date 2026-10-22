# Orchestration principles

Why parallel plans still serialize, measured across recent multi-workstream
runs, and what to do about it. Each principle below has the rule, the
aggregate evidence, an invented example, the check, and the fix. The short
form lives in the [workstream-orchestration skill](../../../skills/workstream-orchestration/SKILL.md);
the planner and lead skills point here for detail.

Two pictures first.

## The ready queue

A task is *ready* when every id in its `deps` is done or merged, its wave's
start condition holds, no other live lane owns its files, and its production
step (if any) is approved. The ready queue is the list of ready-but-unstarted
tasks. It is the one number that tells you whether the system is fed:

- ready > 0 with ≤ 1 live worker means the plan parallelized but execution
  serialized — the lead is the bottleneck, not the plan.
- ready = 0 with live workers > 0 means the system is saturated; the next
  question is what unblocks the most downstream work (`ws next` ranks this).

`ws next` prints the queue. The Plan page header shows its length. A healthy
run drives it to zero every cycle.

## How a new workstream appears

No dashboard edits: one bundle per id under `AGENT_DASH_WORKSTREAMS`.

- `python3 agent_dash.py ws new WS4 "Read path"` writes the bundle; it lands on the Planned shelf and in the Planned view.
- The first agent run maps to it; the page shows it under Current with a "first agent started" hint until `python3 agent_dash.py ws state WS4 active` moves it.
- Plan 100% of non-deferred tasks shows a "Ready to archive" chip with `python3 agent_dash.py ws archive WS4` to copy.
- `ws archive` freezes the summary and moves it to Archive; `ws restore` reopens to done.
- Runs with no bundle list under "Unplanned activity" as "Active without a plan: <id>" with `ws new` to copy.

## Burst-plus-tail

The dominant wave shape measured: many lanes launch together, most finish
together, one lane drags for hours while everything else waits to merge.

```text
lanes:  A  B  C  D  E  F  G  H      <- burst: 8 lanes launch at once
time ──────────────────────────────────────►
A      ██████ done
B      ████████ done
C      █████ done
D      ██████████ done
E      ██████ done
F      ███████ done
G      ██████ done
H      ██████████████████████████████ done   <- tail: one fix lane holds the wave
       ▲ burst done              ▲ wave done
       └─ median end             └─ max end; tail = max − median
```

Tails measured ran 0.6–5.6 h and were fix rounds, not first-pass work. The
fix is Principle 8: bound fix rounds, then split or reassign the slow lane
instead of holding the wave.

## Throughput rules

The twelve principles say what must hold; the throughput rules say how the
lead keeps it holding. A lead refills the wave on every worker completion,
runs every wait on a background watcher, and groups production steps by area
so independent areas run in parallel while each area stays serial. Each
production batch carries one approval row and one rollback line. Work is
committed and pushed per task, and the tracker is updated in the same step as
each event. These rules have no measurements yet; each has a check that names
an observable. The short form, T1 to T5, is in the
[workstream-orchestration skill](../../../skills/workstream-orchestration/SKILL.md#throughput-rules).

## The principles

### 1. Keep the ready queue empty

- *Rule:* every cycle launches every ready lane up to the worker caps before
  the lead does anything else.
- *Evidence:* ready tasks starved 46–95% of the clock with ≤ 1 agent running
  in the worst workstreams; median ready-waits ran ~2 h. One workstream ran
  5x its critical path at mean concurrency ~1 despite a parallel plan.
- *Example:* the notes workstream has 6 ready lanes Monday morning; the lead
  launches 2, reads docs, launches 2 more at noon. The queue sat at 4 all
  morning for no reason the plan can explain.
- *Check:* `ws next`; ready count on the Plan page header; `doctor --ws`
  flags idle-with-ready.
- *Fix:* launch the queue now, up to caps. If caps bind, say so on the
  dashboard; if judgment binds, the lead is doing worker jobs (see the
  orchestration skill Anti-patterns).

### 2. Never launch past an unfinished dependency

- *Rule:* a lane starts only when every dep id is done or merged. Edges are
  real or they are cut.
- *Evidence:* 77 tasks launched before a dependency finished; several
  started hours-to-a-day early and paid it back in rework and rebuilds.
- *Example:* lane `WS4.7` (index page) launches while dep `WS4.3` (query
  layer) is still in review; the query shape changes, and the page lane
  rebuilds from scratch.
- *Check:* `ws next` lists only startable tasks; `ws validate` fails unknown
  or self deps; doctor flags lanes whose dep is not done.
- *Fix:* stop the jumped lane if it has not produced reviewable work,
  otherwise rebase it onto the dep's merged shape; cut nominal edges (see the
  dep test in the planner skill) so the next `ws next` is honest.

### 3. Interface-first tasks

- *Rule:* every fan-out over a handful of lanes starts with a frozen
  interface task (spec, shapes fragment, manifest, route list) that
  dependents name in `deps`.
- *Evidence:* interface tasks with 7–20 dependents unblocked the widest
  waves measured; a first-deploy gate with 17 dependents serialized a whole
  middle phase behind it.
- *Example:* freeze `WS4.5` (result shapes) first; then 7 route lanes build
  against it in one wave. Without the freeze, each route guesses the shape
  and 3 of 7 need rewrites.
- *Check:* any task with more than 5 dependents names its interface task;
  `ws next` ranks interface tasks by dependent count.
- *Fix:* insert the interface task, freeze it with review, point dependents
  at it, and re-wave.

### 4. One writer per file per wave

- *Rule:* lanes in one wave share no source file and no test file. A pair
  that shares a file is one lane or runs in different waves.
- *Evidence:* overlapping lanes produced the rebase churn, stacked fixes,
  and repeated pushes measured wherever waves were hand-drawn without a
  file check.
- *Check:* `ws overlap` reports collisions in the wave before launch.
- *Fix:* merge the colliding pair into one lane, or move one to the next
  wave whose start condition names the first lane's task id.

### 5. Disjoint waves from a file-overlap table

- *Rule:* the execution plan carries one file list per task; waves are sets
  of tasks with no pairwise overlap.
- *Evidence:* the best measured plan named a file per task plus a do-not-edit
  list and held across multi-lane waves with no collisions; plans without
  the table drifted from their deps within a wave or two.
- *Example:* wave B runs the search route, the image route, and the filter
  route at once — the table shows three disjoint file sets, so the lead
  launches all three with one command each.
- *Check:* `ws waves` computes greedy waves from deps plus footprints and
  names the dep or file forcing each serialization; the lead accepts or
  overrides with a note.
- *Fix:* re-cut the wave from the table; overrides carry a note naming the
  shared file and who merges first.

### 6. Numeric acceptance in every brief

- *Rule:* every lane brief states metric, threshold plus tolerance, and probe
  command; the reviewer reports evidence; the lead closes on the number.
- *Evidence:* lanes without numbers ended in chat haggling ("close enough,
  mark it done") instead of a check, and near-misses shipped as done.
- *Example:* brief says "listing query p95 ≤ 80 ms with 10% tolerance,
  probe `scripts/probe_list.sh`"; reviewer reports 75.6 ms → done, no debate.
- *Check:* `ws validate` fails a lane brief without acceptance; review
  verdicts cite the measured number.
- *Fix:* add the number before launch; a lane already running gets its
  acceptance from the lead within the hour or it stops at review.

### 7. Cap review rounds and escalate

- *Rule:* fix rounds are bounded (2 default); the third round goes to the
  lead for fix, re-scope, or defer — never a silent fourth attempt.
- *Evidence:* review loops were 74% of runs in the busiest workstream, with
  nearly every task multi-round; unbounded rounds are where tails grow.
- *Example:* lane `WS4.9` fails review twice on the same auth check; the
  lead re-scopes the check to a follow-up task instead of ordering round 3.
- *Check:* attempt count (`↻N`) on the task chip; doctor warns past the cap.
- *Fix:* lead decides within one turn: fix directly, narrow the scope, or
  defer with a note naming the receiving work.

### 8. Cut the tail

- *Rule:* wave tail (max end minus median end) stays under one hour; a lane
  past it is split or reassigned.
- *Evidence:* tails of 0.6–5.6 h held waves whose other lanes were done;
  the tail lane was almost always a fix round, not new work.
- *Example:* 7 of 8 lanes done by 15:00, one auth-fix lane still open at
  18:00: split the fix (ship the approved part as a train, move the auth
  remainder to its own lane with a fresh worker).
- *Check:* wave timings; `doctor --ws` flags a wave whose tail exceeds the
  bound.
- *Fix:* split the slow lane, reassign it to a fresh worker with a narrower
  brief, or defer its remainder; merge what is approved without waiting.

### 9. Label every run with its task id

- *Rule:* run labels are `<id>-<task-id>-<slug>`, reviews `-rev<n>`, fixes
  `-fix<n>`; nothing runs under a bare wave or model name.
- *Evidence:* half the tasks had no runs mapped; one workstream's whole wave
  used a label scheme without task ids and vanished from every per-task
  metric.
- *Example:* `ws4-WS4.3-query-layer` pins to the query-layer chip and counts
  attempts; `wave-b-run-2` pins to nothing and shows as ghost work.
- *Check:* no unmapped rows above the progress map; `check` reports them.
- *Fix:* add the task-map entry for legacy labels; relabel live runs;
  the launcher refuses labels without a task id.

### 10. Move status in the same step as the event

- *Rule:* `wip` on launch, `pr` on open, `prog` only after merge with a
  production step left, `done` on merged-and-live — each in the same step
  as the fact, not batched later.
- *Evidence:* plan status lagged reality for days; finished runs sat unread
  while the map still showed them live; "in progress" covered merged,
  stalled, and idle alike.
- *Example:* the query lane's PR opens → `ws task WS4 WS4.3 pr` in the same
  message that announces the PR, before reading any other report.
- *Check:* history timestamps track event times; doctor flags long-lived
  `wip` with a merged branch.
- *Fix:* sweep once now (every chip against open PRs and main), then keep
  the same-step discipline; finished-run pickup comes before new launches.

### 11. Gates carry a freshness bound and a named signal

- *Rule:* every outside signal is a `"kind": "gate"` task with its condition
  in the dependent's `dep_notes`, plus a freshness bound; an expired gate
  blocks starts with STALE-GATE.
- *Evidence:* gates checked once at kickoff decayed silently; the good
  measured cases carried wall-clock receipts ("restamp finished 21:52",
  "export succeeded 22:29") that dependents cited.
- *Example:* gate `WS4.G1` "nightly export done" with a 24 h bound; at 25 h
  `ws next` holds dependent lanes as STALE-GATE until the watcher re-checks.
- *Check:* `ws gates` shows every gate fresh, each with condition text and
  owner; doctor warns on gates without a bound.
- *Fix:* re-measure the signal, stamp the receipt, resume dependents; while
  gated, run every unblocked lane (never idle-hold).

### 12. Cross-workstream holds go through the hub with a deadline

- *Rule:* waits on another group go to the hub as a named relay with a
  resume date; local ready work runs meanwhile; a cheap watcher holds the
  gate.
- *Evidence:* cross-group holds were the binding constraint everywhere
  measured; leads that idled on them showed zero live workers while local
  lanes sat ready.
- *Check:* waiting tasks name the hub relay and the date they resume;
  `ws deferred` names each receiving end; live-worker count stays > 0 while
  tasks remain.
- *Fix:* file the relay today, launch the local ready set, arm one watcher
  on the signal; on the date, either resume or re-file with a new deadline.
