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
| `runs/<label>/` | dispatcher | prompt copy, run_worker out and err files, pid (durable; see Scratch) |
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
  on timeout with no events. The default timeout is 1800 s, so a watcher never blocks
  forever. This is the lead's one background watcher.
- `once --dry-run` decides one tick and prints what it would launch, with no side
  effects. `once` without `--dry-run` runs one real tick under the same pidfile lock
  as `run`, and exits 4 when a dispatcher already owns the workstream.

## Scratch: what goes where

A run folder is durable record. Only prompts, reports, out and err files, and decisions stay in it.
Anything that grows and can be rebuilt goes to the run's scratch folder, `$AGENT_SCRATCH`:

| Goes in the run folder (`runs/<label>/`) | Goes in `$AGENT_SCRATCH` |
|---|---|
| prompt copy, `out-*.md`, `err-*.log`, `*.pid`, `*.meta.json` | Python environments (`.venv`, `venv`) |
| the report and any decision file | local Postgres clusters and their data directories |
| small notes the lead reads | decompressed caches and downloaded archives |
| | downloaded or rendered images |

`run_worker.py` creates the folder before every launch and exports `AGENT_SCRATCH` to every tier
(muse, cursor, grok-build, pi, codex). The path is `~/.cache/agent-dash/scratch/<WS or hub>/<label>/`,
or `$AGENT_DASH_SCRATCH_ROOT/<WS or hub>/<label>/` when that is set. The folder holds an owner marker
(`.agent-scratch.json`) that names the label and the run folder; a folder without one is never
removed.

Reclaim scratch from finished runs:

```
python3 agent_dash.py scratch status            # total size and the largest folders
python3 agent_dash.py scratch gc                # dry run: what would go, and what is kept and why
python3 agent_dash.py scratch gc --apply        # remove finished runs' folders (default: 7 days idle)
```

`scratch gc` keeps a folder when any check cannot run or fails: a live launcher names its label, a
run's `running/<label>` or `<label>.pid` names a live process, a process has a file open in it or has
it as its cwd, the newest file is under the cutoff, or the folder has no owner marker. Each candidate
is checked again just before removal.

`ws archive <ID>` cleans up after a successful archive: it runs `logs archive --ws <ID> --apply` when
that service is installed, then removes the workstream's scratch folders. Cleanup is refused, and the
archive still succeeds, while the workstream has live runs. `--keep-scratch` skips the scratch removal.

The doctor warns (`run-folder-scratch`) when a run folder under the hub root holds a Python
environment, a Postgres data directory, or more than 200 MB of caches or images. It only reads.
Move such content to `$AGENT_SCRATCH` and leave the folder with prompts, reports and decisions.

## Loop

Each tick, and immediately when a tracked worker's pid exits, the dispatcher:

1. Observes finished runs. A PASS on a write task moves it to `pr`; a PASS on a
   read task moves it to `done`. PARTIAL or BLOCKED leaves it `wip` and emits
   `needs-lead`. No verdict (empty report, early death) relaunches once on `auto`
   excluding the failed tier (`retried`); a second miss emits `needs-lead`. While
   paused, or while `dispatch.json` is broken, a due retry waits as `retry-pending`
   and goes out once the config is fixed or the pause lifts.
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
are adopted, and a task with a live run is never relaunched. A run directory with no
state record is claimed the same way: a live one is adopted, and a finished one from
an earlier round is skipped, so an old report never counts as this launch.

If the dashboard run list cannot be read, live runs are unknown, so the dispatcher
launches nothing and emits one `error` (phase `dashboard`) until it answers again.
A crash in the loop writes an `error` (phase `fatal`) and a `stopped` event with
`reason=crash` before the process exits.

## How a lead uses it

1. Write `prompts/<task-id>.md` for each task that is ready or about to be.
2. `dispatch start <WS>` (once per workstream, idempotent).
3. Arm one background `dispatch wait <WS>` (default timeout 1800 s) and end the turn.
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
