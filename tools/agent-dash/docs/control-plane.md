# Control plane

The control plane is the part of agent-dash that decides whether a launch may
start and whether a finished run may advance. It is four small modules behind
one dispatcher loop, and each one answers a single question:

| Module | Question | State |
|---|---|---|
| `dash/admission.py` | May this launch start, given the files it declares and the launches already live? | `admission.json` + `admission.lock` |
| `dash/evidence.py` | Does this PASS hold against the committed tree? | `receipts/<task>.json` |
| `dash/dispatch.py` | What does the workstream do next? | `dispatch-state.json`, `inbox.jsonl` |
| `dash/flowview.py` | Where does the work sit and what blocks it? | none (a fold) |

The seam is the dispatcher. It is the only caller of the lease store and the
evidence module, so a launcher, a wave runner and a manual launch all ask the
same question through the same code and get the same answer. `flowview` reads
only the cached `/api/state` and doctor payloads, so the flow card never pays a
second collection.

## Task contract fields

A plan task is legacy until it carries `contract_version`. A version-1 contract
is these fields, and only these fields are hashed:

| Field | Meaning |
|---|---|
| `contract_version` | `1`. Any other value is a contract error and keeps the task out of the ready queue. |
| `acceptance.metric` | What the probe measures, as a non-empty string. |
| `acceptance.threshold` | The pass line, as a non-empty string. |
| `acceptance.tolerance` | The allowed slack, as a non-empty string. |
| `acceptance.probe` | One command string, or a list of them, run in order. Split with `shlex.split`, never through a shell. |
| `acceptance.cwd` | Optional. A relative directory inside the workspace; absolute, `~` and `..` are refused. |
| `files` | Required for a write task (`kind: task`). Plain relative paths, no globs, no `..`. |
| `deps` | Task ids that must be done first. |
| `kind` | `task` (write) or `gate`. A gate is never launched. |

`orchestrate.task_contract_issues` is the one validator. The CLI, the plan
validator and the ready queue all call it, so a task that is not ready for a
contract reason is reported the same way everywhere.

## Lease semantics

A lease is a claim over a set of files, held by one owner token, with a pid and
an expiry. It lives in `admission.json` under the state dir and every
read-modify-write runs under `fcntl.flock` on `admission.lock`, landing through
a temp file plus `os.replace`. A crash never leaves a half-written file, and two
claimers never both win.

- **Declarations are exact.** A path is canonicalized to a repository-relative
  POSIX path before it is compared, so `./a/b.py`, `a//b.py` and `a/../a/b.py`
  are one file. An absolute path, a `~` path, a glob or a path that escapes the
  root is refused as `invalid`, because a lease that silently covers a pattern
  is a lease nobody can reason about.
- **Identity is the absolute real path.** The relative name is only what the
  dashboard shows. Two worktrees that each declare `src/mod.py` are two files.
- **A lease is live while it is unexpired and its pid is not provably dead.** A
  dead pid is reaped at once. An unknown pid is never reaped, because a lease
  that outlives its process is cheaper than a stolen file.
- **Renew, release and reap are idempotent.** The dispatcher renews on every
  live observation, releases when the run finishes, fails or is handed to the
  lead, and reaps on every read.
- **A restart adopts its own run.** The owner token is deterministic
  (`dispatch:<ws>:<task>:<label>`), so a restarted dispatcher drops its own
  stale lease first instead of blocking on itself.

`LeaseStore.admit_launch` does the capacity check and the conflict check under
one lock, so a launch that would exceed `max_concurrent` never takes a file
lease on the way to being refused. `admit_capacity` takes a lease that covers no
file, which is how a launcher that owns no file still counts against the cap.

## Receipt validity

A worker's PASS is a claim. `evidence.verify_task` turns it into a receipt that
binds three things: the task contract, the probe that was run, and the git tree
the probe ran against.

1. The contract hash covers `contract_version`, `acceptance`, `files`, `deps`
   and `kind` in canonical JSON. Title, status and owner are presentation and
   never change it.
2. The probe hash covers the probe commands and their cwd.
3. The tree hash is `HEAD^{tree}` of a clean worktree. A dirty worktree, a
   directory that is not a git worktree, or a repository with no commit is
   refused, because none of them names a committed tree.

The probe runs without a shell, in `workspace/acceptance.cwd`, under one bounded
timeout. A receipt is written only when the probe exits 0 and the receipt still
matches all three hashes. `receipt_valid` re-checks them, so an amended commit
changes the tree hash, a dirty worktree has no tree, and an edited contract or
probe changes its hash. Any of those invalidates.

A receipt carries the task id, the three hashes, the exit code, the duration and
the sha256 of stdout and stderr. It never carries their text, the environment,
the prompt or a credential.

## Launch admission

The dispatcher asks for a lease before it starts a worker, rooted at the task
workspace so a declaration is read the way the worker reads it.

- A task with no `files` takes no file lease and launches as before.
- A task whose files are held stays `todo` and emits one `admission-blocked`
  event per distinct block, naming the conflicting lease's owner, workstream,
  task and files. The event repeats only when the block changes.
- A task whose declaration is invalid is blocked the same way with
  `reason: invalid` and the canonicalizer's message.

`scripts/run_worker.py` adds the second half: when the registry's `admission`
block (or an `AGENT_DASH_ADMISSION_*` variable) sets `max_active_runs`, every
launch takes a capacity lease and writes it to
`<run-dir>/<label>.admission-lease`. A waited run releases its own lease when
the process exits. A detached run keeps it, and the dispatcher releases it when
it observes the finish, then deletes the file. The TTL is the backstop for a
launcher that dies without releasing.

## Flow API

`GET /api/flow` folds the cached `/api/state` body and the latest persisted
doctor run into the one shape the flow card draws. It never rescans a plan and
never runs a check.

```json
{"generated_at": 0,
 "queues": [{"key": "todo", "label": "Todo", "count": 0}],
 "blockers": [{"code": "", "ws": null, "severity": "warn", "count": 1, "title": "", "command": "", "next": null}],
 "workstreams": [{"id": "WS7", "title": "", "counts": {}, "total": 0, "done": 0, "pct": 0}],
 "totals": {"workstreams": 0, "tasks": 0, "done": 0, "pct": 0, "blockers": 0, "blocked_tasks": 0},
 "empty": true}
```

`queues` is the fixed order `todo`, `wip`, `pr`, `prog`, `deferred`; `done` is a
total, not a queue. `blockers` groups doctor problems by (code, workstream),
worst severity first, and its `command` and `next` are generated from the
workstream id, never copied from a stored fix string. An id that is not a plain
`[A-Za-z0-9_.-]` token falls back to the project-wide command, so a stored id
can never smuggle shell text into the payload.

## Legacy compatibility

A task with no `contract_version` is legacy. It has no acceptance probe, so it
gets a legacy receipt and is never probed, and its PASS advances exactly as it
did before: a write task to `pr`, a read task to `done`. A legacy receipt is
valid only while the task is still legacy; a task that gains a contract needs a
real receipt.

A task with no `files` takes no lease. A registry with no `admission` block
takes no capacity lease. Both paths are the old behavior, unchanged.

## Exact-token follow-up

The capacity lease counts live launches, not tokens. It never claims to know a
run's token cost, and the prompt cap counts bytes rather than tokens because no
tokenizer is consulted. Exact provider-token telemetry is the next slice: read
the provider's own usage numbers in the tier launchers and fold them into the
run row, so a cost is exact rather than inferred from stream heuristics. It is
listed under "Control plane follow-ups" in [roadmap.md](roadmap.md).

## Rollback

Every piece is additive and independently reversible.

- **Stop the dispatcher.** `python3 agent_dash.py dispatch stop <WS>`. Launched
  workers keep running; the dispatcher never kills a worker.
- **Drop the leases.** Delete `admission.json` under the state dir, or call
  `LeaseStore.release_owner(<owner>)`. A lease is a claim, not a record: losing
  it costs one re-claim, never a file.
- **Drop the receipts.** Delete `receipts/` under the state dir. A contract task
  then re-probes on its next PASS; a legacy task is unaffected.
- **Revert the code.** The four modules are new files, and the dispatcher's
  hooks are guarded by `contract_version` and `files`, so reverting the branch
  leaves every plan and bundle readable. No migration runs and no state file is
  required to exist.
