# agentq: one laptop job queue

agentq runs agent work for every harness session on this laptop (Claude Code,
codex, muse, pi) through one durable queue. A session submits a prompt file, the
broker launches a pi worker through `scripts/run_worker.py`, and the report comes
back as a file. The queue survives broker, harness and laptop restarts.

Code: `agentq/db.py` (schema, the only writer of `jobs` and `events`),
`agentq/broker.py` (the daemon loop), `agentq/cli.py` (the commands),
`scripts/agentq` (wrapper), `agentq/launchd/com.agentq.broker.plist` (template,
not installed). Tests: `tests/test_agentq_broker.py`.

## The five commands

Each command ends with one `ANSWER:` line. Exit codes: 0 ok, 1 negative fact
(unknown job, nothing to escalate, a failed job), 3 timeout or store unreachable.

```
agentq submit PROMPT_FILE [--kind read|write|review] [--urgent|--backlog]
              [--workspace PATH] [--tier T] [--label L] [--res light|tests|ssh]
              [--max-runtime SECONDS]
agentq wait [ID ...|--mine] [--timeout S]        # default 1800 s
agentq escalate [ID|--mine]                      # no argument: this session's open jobs
agentq ls [--mine|--all] [--json]
agentq result ID [--print]
agentq cancel ID
agentq status
agentq daemon                                    # the broker loop; launchd runs this
```

- `submit` copies the prompt into `$AGENTQ_HOME/jobs/<id>/prompt.md`, records the
  submitting session and harness, and prints the job id. Defaults: kind `read`,
  lane `normal`, resource `light`, workspace the current directory. Tier: `review`
  goes to `pi-kimi`, everything else to `pi-deepseek-flash`, unless `--tier` is given.
  The label comes from `agent_dash.py label make` unless `--label` is given.
- `wait` blocks until every named job (or every open job of this session with
  `--mine`) is done, failed or cancelled. It prints `id verdict report_path` per
  job. It can be called from any process, at any time.
- `--max-runtime` sets the job's wall-clock cap (default 21600 s, six hours).
- `escalate` moves jobs to the `urgent` lane. A running job speeds up on its next
  request through the proxy. Each escalation is logged in `events` with the session.
- `result ID` prints the report path and verdict; `--print` also cats the report.
- `cancel` drops a queued job, or sends SIGTERM to a running job's process group
  (stored pgid, checked against the stored process start time first).

Typical use from a harness: `agentq submit prompt.md --kind review`, then
`agentq wait <id>`. For "escalate this subagent work", run `agentq escalate`.

## Lanes and ordering

- Lanes: `urgent` > `normal` > `backlog`. FIFO inside each lane.
- A normal job queued for more than 15 minutes sorts with urgent jobs.
- Urgent jobs hold at most half of the total slots while another job waits
  (never less than one slot).
- Slots and tiers: `broker.json` `slots` gives caps per resource (defaults `light`
  40, `tests` 6, `ssh` 8). `tiers` gives caps per tier (defaults `pi-kimi` 2,
  `pi-deepseek-flash` 48). Both are ceilings, read on every tick.
- Adaptive admission: `broker.json` `adaptive` (on by default; an absent key means
  on with the defaults below). Above the ceilings, a job waits only for real headroom.
  - Morph (`pi-deepseek-flash`, `pi-kimi`): a normal job waits while a budget its
    model uses has more than `max_waiting_requests` (default 8) requests queued in
    `/agentq/status`. DeepSeek uses `total` and `output`; Kimi uses `total` and
    `uncached`. A backlog job also waits until those budgets queue nothing. Urgent
    jobs ignore this gate. If the proxy is down, no Morph gate applies.
  - Laptop: a `tests` job always launches while fewer than `tests_floor` (default 4)
    tests jobs run. Above the floor it waits while memory free is under
    `tests_min_free_pct` (default 35), or while 1-minute load per CPU is at or above
    `tests_max_load_per_cpu` (default 1.0). A `light` job waits only while memory free
    is under `light_min_free_pct` (default 20). Memory comes from `memory_pressure`
    and is read at most every `gauge_ttl_s` (default 15) seconds. An unreadable gauge
    means no extra gate. `ssh` keeps its fixed cap, since it protects prod hosts.
  - The `tests` ceiling is `slots.tests`. Recommended: 16 (the owner's setting is 10).
  - `"adaptive": {"enabled": false}` restores the fixed caps exactly, including the
    old rule that a `saturated` proxy holds backlog jobs.
- A held job records why it waits in `jobs.detail` (for example
  `hold tests: memory free 31% < 35%`). `logs/broker.log` gets one line each time
  that reason changes.
- One write job per workspace at a time; a second waits.
- A pool or tier cap that is full (run_worker reports `reason=none-available`) is
  a wait, not a failure: the job stays queued, no attempt is counted, and it
  retries 20 s later. One `capacity-wait` event is logged per refusal streak.

## Session identity

The session is the first of `CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID`,
`MUSE_SESSION_ID`, `AGENTQ_SESSION`. Otherwise the CLI walks parent pids to the
first harness process and uses `<name>:<pid>:<cwd>`. The harness name is recorded
with each job.

## Durability

- Every job state change is one SQLite transaction (`BEGIN IMMEDIATE`, WAL,
  busy timeout 30 s). Opening the store, switching to WAL and `BEGIN` retry lock
  contention for up to 60 s with jittered backoff (50 ms to 1 s). No transaction is
  held across a subprocess call or other slow I/O. Concurrent submits from many processes do not lose jobs.
- Workers run in their own session, so they outlive the broker.
- Each tick reconciles running jobs first:
  - process alive with a matching start time: adopted, nothing changes;
  - dead with a report: finished, the report is copied to `jobs/<id>/report.md`;
  - dead with a QUOTA marker naming this job's label, or a structured 429 in its
    err log (`429 {json}` or `Rate limit exceeded`): requeued at the front of its
    lane after 60 s, counted in `rate_attempts`. After 20 such requeues the job
    fails with reason `rate-limited`. No attempt is counted;
  - dead without a report: `attempt` goes up by one and the job is requeued at
    the front of its lane, resuming from its pi session file when one exists.
    After 3 such deaths the job is `failed`.
  - unknown (`ps` failed or timed out): the job is skipped that tick. Nothing is
    requeued on an unknown probe.
- Identity: a recorded start time must match the process exactly. A pid found in
  a run folder is adopted only when its command line names the job's label or run
  folder.
- Watchdog, for a live worker: past `max_runtime_s` it is killed and the job fails
  with the reason. Silent for 45 min (no file in the run folder changed) it is
  killed and requeued once if it has no report; a second stall fails it.
- Every state change is conditional on the state it was decided in. A cancel
  that lands during a launch stops the new worker; a terminal state is never
  overwritten.
- The broker writes `logs/broker.log`. A single daemon holds the flock on
  `broker.pid`. A second `agentq daemon` logs `second daemon exits` and exits 0,
  so launchd does not spin (the plist also throttles restarts to 30 s).

## Files

Under `$AGENTQ_HOME` (default `~/.local/state/agentq`):

| Path | What it is |
|---|---|
| `agentq.db` | SQLite store: `jobs`, `events`. `proxy_minutes` belongs to the proxy |
| `broker.json` | optional slot and tier ceilings, and the `adaptive` admission settings (see above) |
| `broker.pid` | the daemon's flock'd pidfile |
| `jobs/<id>/prompt.md` | the submitted prompt |
| `jobs/<id>/runs/<n>/` | the run folder of each launch (run_worker's files live here) |
| `jobs/<id>/report.md` | the finished report |
| `logs/broker.log` | broker log |

Install the LaunchAgent (not done by the repo): fill `__PYTHON__` and `__HOME__` in
`agentq/launchd/com.agentq.broker.plist`, copy it to
`~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) <plist>`.

## Known limits

- Resume: on a requeue the broker resolves the previous attempt's session file
  (`<previous run>/pi/sessions/*<session id>*.jsonl`) and passes its path in
  `AGENTQ_RESUME_SESSION`. If no file matches, the launch starts fresh. The
  launcher must honour the variable for the session to continue.
- Cancel signals the process group. A pi child that moved to its own session
  survives a cancel.
