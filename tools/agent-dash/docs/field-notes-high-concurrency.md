# Field notes: running a workstream at 20-worker concurrency

Measured during one large workstream run (15 PRs, ~85 lanes, 20 concurrent workers). Each item cost real time or
nearly shipped a defect, so each is a rule with the evidence behind it. Project-specific names are generalised.

## Dispatch mechanics

**Stagger worker launches, or verify dashboard health once and bypass the preflight.** Fourteen simultaneous
`run_worker.sh` launches made 7 fail with "Dashboard is down" while the dashboard answered in 26 ms. The
preflight budget is 3 s against a single-threaded server, so concurrent probes queue and the back of the queue is
told the dashboard is dead. An 8 s stagger still lost 4 of 6. See issue #6.

**Run labels do not pin to plan tasks.** `agent_dash.py label make` produces `<ws>-<mark>-<slug>`, but the task
matcher does not read the `-<mark>-` infix, so every run reports `task: null` and the plan map looks idle — the
"ghost runs" doctor finding. Workaround: generate `/^ws<n>-(exec|review|plan|coord)-<slug>(-r\d+)?$/` entries
into `static/taskmap.local.json`. See issue #7.

**`ws task` status writes may not persist, which silently freezes the ready frontier.** `ws next` reported 97
ready lanes while only 5 were dispatchable, and later looked exhausted when 14 were ready. Keep your own ledger
(task, state, note) and sync it into `plan.json` statuses, then `ws validate`. Syncing 85 statuses reopened the
frontier immediately. **Treat a suspiciously empty ready queue as a tooling symptom, not a fact.**

**Build is not activation.** Excluding every lane that carries an approval id dropped 58 of 60 remaining lanes,
because most build lanes name an approval for their *later* activation step. A lane is lead-only only when it has
**no files to write** and has activation steps, or its dispatch kind is an aggregate/coordination marker.

**Group lanes that share a file set behind one integration owner.** Two clusters of 12 and 17 lanes each wrote
the same two files. That is 29 lanes that cannot run concurrently at any worker count. Shipping each shared file
set as one integration-owner branch turned a 5-wide ceiling into a 14-wide wave.

**Reap worktrees as branches merge.** 47 merged worktrees had accumulated before anyone noticed.
`scripts/reap_worktrees.sh <ws-slug> <repo>...` removes one only when its branch is an ancestor of
`origin/main`, the tree is clean, and nothing is ahead.

## Verification discipline

**Every branch drifts.** Main moved 9 times during the run, and the first two reviews both caught a stale base.
Rebase and re-run scoped tests before every merge, and compute the real diff from the **merge base** — an
`origin/main..HEAD` diff on a stale branch shows main's newer commits as reversals. One looked like 234 changed
files; the truth was 9.

**The train gate earns its keep.** One member reported PASS and its review said APPROVE_WITH_NITS, but the train
gate ran a contract test the lane's own command never ran and found three models doing a full primary scan per
batch. Eject the red member, ship the rest, fix and re-review.

**Prove "pre-existing" on a clean checkout, and keep one provisioned baseline worktree for exactly that.** Of
four failures investigated this way, two were genuinely pre-existing on main and two were ours.

**Watch for environment-dependent tests.** Three distinct cases in one run: import errors that were really an
unsynced worktree (36 phantom type errors, cleared by a dependency sync); a dbt test that fails under the
workspace toolchain because the repo pins a different version behind its own harness script; and a guard whose
skip condition caught neither a missing adapter nor an unreachable database, so the check was silently inert in
some environments and permanently red in others. **A check that cannot run should skip loudly with instructions
— not pass quietly, and not fail forever.** Both failure modes get the suite ignored.

**Watchers must key on something newer than the log.** A watcher matched pre-existing result lines from two days
earlier and reported completion instantly. Key on today's date or a monotonic marker.

## Judgement, not just mechanics

**Static inventories are unsafe oracles for destructive work.** An allowlist showed *no writer* for two tables
that were taking overwrite snapshots that same morning, because writes arrived through publish-time rebuild hooks
no allowlist entry covered. Gating the edit on a **live catalog probe** is what stopped a deletion that would
have removed the only working leg while its replacement was timing out. The readiness checker built afterwards
was itself rejected for reproducing the blind spot: a literal-name regex misses an f-string identifier, a bare
default and an import-only indirection.

**Check whether "independent" work is actually a chain.** Nine of twelve switch lanes had no producer in the repo
at all, so they correctly returned zero, and 13 of 17 retirement lanes still had live writers *and* live
consumers. The real order was producers → switches → retirements, not three parallel groups.

**Refusing an approved action is often the right outcome.** Of four approved migration applies: one applied, one
refused because the target now showed live traffic, one refused because the statistics window was far too young
to prove disuse, and one surfaced a real bug in the migration itself. A PARTIAL with honest skips beats forcing a
file through.

**Irreversible steps need their recovery proven first.** An irreversible drop required a scratch restore, and the
restore drill was failing — deterministically, because earlier migrations redefined views over relations the
backup never dumped. The three prior "flaky" failures all had distinct real causes, including a count mismatch
that was a backup-window race on hot-writer tables (±0.2% bidirectional, while 51 M-row tables matched exactly).

**Resource claims are real.** One disk-heavy operation at a time per host: a rehearsal was scoped to read-only
preflight while a restore held the claim, and a read-only role's connection cap saturated under concurrent probe
lanes, producing false "role lacks schema access" reports. Reclaiming one 154 GB scratch database moved free disk
from 281 GB to 435 GB and cleared a rehearsal's abort floor.

**Do not race another actor.** Five files were re-pinned to an old state within 6–9 s of every reset by a live
watcher this session did not own. Restoring them was futile; the fix was to document the divergence in git so the
holder could drop the pin against a record.

**Log corrections.** A `DROP DATABASE` bundled with a `SET` failed inside a transaction block after the ops log
had already recorded success. The log carries the correction and the retry.

## Shell and environment traps

- **zsh does not word-split unquoted `$VAR`.** Cost three silent no-op loops. Use arrays: `X=(a b); cmd "${X[@]}"`.
- `timeout` is absent on macOS; `systemd-run` and `free` are Linux-only.
- A DSN in `"$VAR"` expands **empty** inside `bash -lc "..."`; pipe the script via a `bash -s` heredoc instead.
- `DROP DATABASE` cannot run in a transaction block, so never bundle it with a `SET`.
- `ls` aliased to a modern replacement breaks `ls <path> && cmd`; use `test -f`.
- A merge tool that waits for named remote checks waits forever on a docs-only PR that starts none — use its
  override flag or merge directly.

## Round two: dispatch, deploy and serving lessons

Measured during a later orchestration pass over the same large run. Same format — symptom, cause, rule — and the
same generalisation: patterns, not project specifics.

**Slicing a sibling's prompt leaks the sibling's scope.** Symptom: workers commit to the wrong branch and refuse
their real task as out of scope. Cause: deriving a new lane prompt by slicing a sibling's prompt copies the
sibling's branch, worktree, unit title, lane id, write paths and test command. Rule: generate lane headers from
the shared policy only; the task body is the sole authority on scope.

**Reused run labels replay stale results.** Symptom: reusing a run label returns the previous run's result
instantly; worse, a label whose slug collides with an unrelated plan task id can resolve to that task and return
its stale output. Cause: results are keyed on the label string, and a slug that overlaps a task id resolves to
the wrong owner. Rule: labels unique per run; check a new label against existing runs and task ids for collisions
before launching.

**Live unit edits ahead of merge trip the drift gate.** Symptom: applying service-unit changes live before the
change merges makes the deploy drift gate block every later deploy (live looks ahead of the stale checkout), and
the gate's suggested remedy syncs repo → live, reverting the live change. Cause: the gate compares live state
against the checkout, so an unmerged live edit reads as drift in the wrong direction. Rule: never apply unit
changes ahead of merge, or deploy immediately after so live and the checkout agree.

**A service running a copy outside the checkout drifts silently.** Symptom: merged fixes never go live, with no
error anywhere. Cause: the service executes a copy staged outside the deployed checkout, so the repo path the
review approved is not the path that runs. Rule: execute the repo path; test for it — a deploy check that the
running artifact resolves inside the checkout.

**A registry flip without a live source serves from nothing.** Symptom: merging a registry or alias flip before
its source exists or has ever produced makes serving load from nothing. Cause: the flip was reviewed as a pointer
edit while nobody verified the target. Rule: a live preflight that checks the source exists, has rows and is
fresh; and a self-referential source must fail closed until the loader's behavior for it is proven.

**Load-time freshness reads green while serving is frozen.** Symptom: a freshness signal that measures load time
rather than source-data recency reports green while serving is frozen. Cause: the timestamp advances on every
load attempt, including loads that changed nothing. Rule: alarm on source recency, and make a degraded probe
visible as its own state rather than letting it read as "no breach".

**An unbounded worker process can take down the database.** Symptom: a worker's unbounded data process on a shared
production box grew until the host-level OOM killer fired — on a box where the database holds much of the RAM,
the killer may pick the database. Cause: no memory bound on worker data processes, and whole-table loads into
memory. Rule: memory-cap every data process; a cgroup kill of your own process is acceptable, a host OOM is not.
Stream or chunk instead of loading whole tables, check free memory first, and never scan a whole table on a
shared box.

**Static writer inventories miss live writers.** Symptom: static allowlists and literal-name detectors report "no
writer" for targets with live writers reached through imports, f-strings or CLI flags. Cause: the inventory only
sees the spellings it enumerates, while real writes arrive under synonyms and indirections. Rule: treat a static
"no writer" as a hypothesis; confirm with a gate that flags undecidable references rather than passing them.

**A stale base manufactures blockers.** Symptom: a review that judges a branch against its own stale base raises a
blocker that a rebase dissolves. Cause: the base moved under the branch, so the diff under review describes a
world that no longer exists. Rule: check the base before acting on a blocker — rebase and re-derive the diff from
the merge base first.
