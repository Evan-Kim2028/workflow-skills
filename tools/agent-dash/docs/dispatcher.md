# Workstream dispatcher

The dispatcher keeps a workstream's worker slots full from its ready queue. One
long-lived process per workstream computes `orchestrate.ready_queue` every tick,
launches each ready task that has a prompt through `scripts/run_worker.py`, records
each outcome, and writes one event stream. The lead writes prompts, reads reports,
decides and merges. It no longer launches, polls or tracks run status by hand.

## Files

Code:

- `dash/dispatch.py`: the loop (`Dispatcher.tick`), state, inbox, wait cursor,
  pidfile lock and the `dispatch` CLI (`cmd_dispatch`).
- `scripts/run_worker.py`: launches a tier. A max-wait of `0` launches and returns;
  `--exclude=<tier>` keeps `auto` off a tier (used for the one retry).
- `tests/test_dispatch.py`: loop cases with a fake launcher and fake clock.

Bundle files, in the workstream bundle directory (`AGENT_DASH_WORKSTREAMS` honoured):

| File | Owner | What it holds |
|---|---|---|
| `prompts/<task-id>.md` | lead | worker prompt; optional front matter `tier`, `workspace`, `kind: write\|read`, `max_wait` (seconds) |
| `dispatch.json` | lead | `slots`, `tier` (default `auto`), `workspace`, `paused`, `merge_wave: {min_ready, max_age_min}` |
| `dispatch-state.json` | dispatcher | pid, heartbeat, per-task run state; written atomically |
| `inbox.jsonl` | dispatcher | append-only events, each with a monotonic `seq` |
| `inbox.cursor` | `dispatch wait` | last seq printed |
| `dispatch.pid`, `dispatch.lock`, `dispatch.log` | dispatcher | one dispatcher per workstream |
| `runs/<label>/` | dispatcher | prompt copy, run_worker out and err files, pid |
| `reports/decisions.tsv` | dispatcher | one row per retry only |

## CLI

```
python3 agent_dash.py dispatch start <WS> [--slots N] [--tier T] [--workspace P]
python3 agent_dash.py dispatch stop <WS>
python3 agent_dash.py dispatch pause|resume <WS>
python3 agent_dash.py dispatch status <WS> [--json]
python3 agent_dash.py dispatch wait <WS> [--after SEQ] [--timeout S]
python3 agent_dash.py dispatch once <WS> [--dry-run]
python3 agent_dash.py dispatch run <WS>
```

- `start` writes any given `--slots`, `--tier` or `--workspace` into `dispatch.json`,
  then starts `dispatch run` detached. A second start reports the live pid.
- `stop` sends SIGTERM to the pid in `dispatch.pid`. Launched workers keep running;
  the dispatcher never kills a worker.
- `pause` and `resume` set `paused` in `dispatch.json`. A paused dispatcher still
  observes finishes and merge waves; it launches nothing.
- `wait` prints events past the cursor, advances the cursor, and exits 0. It exits 3
  on timeout with no events. This is the lead's one background watcher.
- `once --dry-run` decides one tick and prints what it would launch, with no side
  effects. `once` without `--dry-run` runs one real tick.

## Loop

Each tick, and immediately when a tracked worker's pid exits, the dispatcher:

1. Observes finished runs. A PASS on a write task moves it to `pr`; a PASS on a
   read task moves it to `done`. PARTIAL or BLOCKED leaves it `wip` and emits
   `needs-lead`. No verdict (empty report, early death) relaunches once on `auto`
   excluding the failed tier (`retried`); a second miss emits `needs-lead`.
2. Counts live runs (its own plus the dashboard's for the workstream) against `slots`.
3. Launches ready tasks in `ready_queue` order, skipping gates and tasks with a live
   run. A ready task with no prompt emits `needs-prompt` once. A launched task moves
   to `wip` through `bundles.set_task`.
4. Emits `merge-wave` when `pr` tasks reach `min_ready` or the oldest has waited
   `max_age_min`. It never merges.
5. Emits `idle` once when nothing is ready, nothing is live and todo work remains.

Events carry `seq`, `ts`, `type`, `ws`, `task` and `label`, plus type fields:
`launched`, `finished` (verdict, out, tier, duration_s), `retried`, `needs-prompt`,
`needs-lead` (reason), `merge-wave` (tasks), `idle` (todo), `stopped`, `error`
(phase, reason). An error in one task is logged once and the loop goes on.

Restart: state is reloaded from `dispatch-state.json`. Runs whose pid is still alive
are adopted, and a task with a live run is never relaunched.

## How a lead uses it

1. Write `prompts/<task-id>.md` for each task that is ready or about to be.
2. `dispatch start <WS>` (once per workstream, idempotent).
3. Arm one background `dispatch wait <WS> --timeout 900` and end the turn.
4. When it exits 0, act on each event: read the `finished` report, decide on
   `needs-lead` and `merge-wave`, write the next prompts, then re-arm `wait`.
   Exit 3 means nothing happened; re-arm.

Do not run `ws task` to move a task to `wip` or `pr` for a dispatched run; the
dispatcher does it. Do set `done`, `deferred` or a retry-by-hand yourself, and
set a `needs-lead` task back to `todo` only after you rewrite its prompt.

## Limits

- `workspace` has no default from the plan; set it in `dispatch.json` or the prompt
  front matter, or the launch fails with a `no workspace` error event.
- `max_wait` only flags a run as `needs-lead` (`reason: overdue`). It never kills it.
- Gate tasks (`kind: gate`) are never launched.
- A retry is the only automatic relaunch; the tier registry's quota fallback inside
  `run_worker.py` still applies to every launch.
