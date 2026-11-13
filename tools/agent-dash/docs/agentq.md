# agentq: one laptop job queue

agentq runs agent work for every harness session on this laptop (Claude Code,
codex, muse, pi) through one durable queue. A session submits a prompt file, the
broker launches a pi worker through `scripts/run_worker.py`, and the report comes
back as a file. The queue survives broker, harness and laptop restarts.

Code: `agentq/db.py` (schema, the only writer of `jobs` and `events`),
`agentq/broker.py` (the daemon loop), `agentq/cli.py` (the commands),
`agentq/backlog.py` (backlog templates) and `agentq/backlog_seed.py` (the standing
templates), `scripts/agentq` (wrapper), `agentq/launchd/com.agentq.broker.plist`
(template, not installed). Tests: `tests/test_agentq_broker.py`,
`tests/test_agentq_backlog.py`.

## The five commands

Each command ends with one `ANSWER:` line. Exit codes: 0 ok, 1 negative fact
(unknown job, nothing to escalate, a failed job), 3 timeout or store unreachable.

```
agentq submit PROMPT_FILE [--kind read|write|review] [--urgent|--backlog]
              [--workspace PATH] [--in-place] [--tier T] [--label L]
              [--res light|tests|ssh] [--max-runtime SECONDS]
agentq wait [ID ...|--mine] [--timeout S]        # default 1800 s
agentq escalate [ID|--mine]                      # no argument: this session's open jobs
agentq ls [--mine|--all] [--json]
agentq result ID [--print]
agentq cancel ID
agentq status
agentq backlog add NAME PROMPT_FILE --every 6h [--kind read|write|review]
              [--res light|tests|ssh] [--workspace PATH] [--tier T] [--max-runtime 2h]
agentq backlog ls | rm NAME | pause NAME | resume NAME
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

## Backlog templates

A backlog template is a prompt the broker resubmits on a timer, so Morph capacity
stays busy when real work runs short. `agentq backlog add` copies the prompt to
`$AGENTQ_HOME/backlog/<name>.md`, so later edits to the source file do not change
the template. `--every` takes `30m`, `6h`, `1d` (or `90s`); `--max-runtime` takes
the same units.

- Each tick, after the launches, the broker submits one job for every enabled
  template that is due (never run, or its interval has passed). The job goes in
  the `backlog` lane, so every normal and urgent job launches first.
- One job per template at a time: a template with a queued or running job waits.
  The job's label is `hub-<mark>-backlog-<name>` (mark `review` for a review
  template, `exec` otherwise), which is how the template finds its open and last
  job after a restart.
- The Morph gate is the launch gate for a backlog job (`morph_hold`): a template
  is submitted only while its tier's proxy budgets queue nothing. With the proxy
  down there is no headroom to read, so nothing is submitted.
- A read or review job gets the preamble "This is a recurring low-priority backlog
  job. Read-only unless the brief says otherwise..." above the template text. A
  write template gets no preamble.
- `agentq backlog ls` shows each template's interval, state, last submission, next
  due time and last job with its verdict. `agentq status` counts the enabled
  templates and the open template jobs.
- `pause NAME` stops new submissions for one template; a running job finishes.
  `pause` with no name pauses every template and the filler (below); `resume` with no
  name resumes them all. `rm` deletes the template and its prompt copy; its past jobs
  stay in the history.
- The standing templates are `merged-review-lor` and `merged-review-ws` (every 6h,
  read-only reviews of the commits merged to origin/main in the last six hours).
  `python3 -m agentq.backlog_seed` creates them once; a second run changes nothing.
  The broker never runs the seed. Their folders are not in the repo: the seed reads
  `$AGENTQ_HOME/backlog-seed.json` (or `--config PATH`), a JSON object keyed by
  template name with `workspace` and `report_dir` for each. The prompts in
  `agentq/backlog_prompts/` take those two values as `{workspace}` and `{report_dir}`.

## DeepSeek fill and launch pacing

Two controls in the broker keep the DeepSeek pool near Morph's total-token limit
instead of letting it swing between idle and burst.

- Fill (`broker.json` `fill`, off by default; `"enabled": true` turns it on). The
  controller judges only probes. A probe is a submission that takes busy DeepSeek
  workers above the highest count reached since the ceiling was set, or above the
  ceiling. Each probe is judged `step_interval_s` (300 s) later, by the billed gain per
  worker it actually started, against the long-run per-worker rate (median over
  `rate_window_s`, 1800 s, of samples taken before the probe). A gain counts as good
  only if it is positive and at least `min_marginal_frac` (0.5) of that rate. A poor
  probe records a ceiling of max(the count before the probe, the current ceiling),
  never below the real (non-filler) DeepSeek count, for `ceiling_ttl_s` (1800 s). The
  ceiling survives a broker restart. Refills at or below the peak are plain launches:
  they are not judged and never change the ceiling. After a ceiling expires the
  controller may probe one step past it, and no further. While the long-run rate is
  unknown (fewer than `rate_min_samples` samples, so no busy workers yet) or below
  `rate_min` (10k), the controller holds, with one exception. With no busy DeepSeek
  worker at all, it takes one bootstrap step of `step_workers` (4), judged against the
  prior `bootstrap_rate` (150k per worker). Zero gain sets a ceiling at the real-job
  count for `ceiling_ttl_s`, and no retry comes before that expires and the pool has
  drained, so poor bootstraps cannot pile up idle workers. Good gain hands over to
  measured probes once samples exist. It
  adds at most `step_workers` (4) per probe, and `max_workers` (64) caps everything.
  Above `high_frac` (0.97) of `target_billed_per_min` (8M) it stops, and it never kills
  a running worker. Filler is held with the reason "proxy status unavailable" when the
  proxy's status is missing or empty. Filler waits while real DeepSeek work is ready,
  while the proxy holds
  a DeepSeek request, after a DeepSeek 429 in the last `gate_429_window_s` (120), and
  while the laptop is short of memory (`laptop_min_free_pct`, 20) or load
  (`laptop_max_load_per_cpu`, 1.0).
- Filler jobs are read-only audits (kind `read`, resource `light`, lane `backlog`).
  The targets come from the `"filler"` key of `$AGENTQ_HOME/backlog-seed.json`:
  `report_dir`, an optional `max_runtime` (default `15m`), and `repos`, each with a
  `workspace` and either `modules` (paths) or `roots` (the folders directly under each
  root). Each audit writes `<report_dir>/filler/<repo>/<module-slug>.md`. A cursor
  rotates the targets, and a module unchanged since its last audit is skipped; the
  audited commit is recorded when the job is submitted, so a failed audit is not
  retried until the module changes. Labels come from `label make` as
  `hub-exec-backlog-filler-<repo>-<module>`.
- Pacing (`broker.json` `pacing`, on by default). A DeepSeek job launches only while
  the current Morph minute's uncached input, plus an estimate for each DeepSeek launch
  whose first request the proxy has not recorded yet, stays under `uncached_frac`
  (0.8) of `uncached_limit` (400k). The current minute comes from the proxy's live
  `uncached_ds` budget (in-flight charges included). When the proxy is unavailable it
  falls back to the store's latest minute, the last known numbers. Pending launches are
  running DeepSeek jobs with no `proxy_jobs` row, read from `jobs.started_at`. With the
  proxy available they count only within `first_request_grace_s` (120); while it is
  unavailable every such job counts, since nothing can be ruled out. A broker restart
  keeps the count. The first-request cost is the `measure_percentile`
  (75th) of single-request DeepSeek jobs in `proxy_jobs`, once `min_measured_jobs` (20)
  are seen; otherwise `first_request_uncached` (40k). One urgent DeepSeek job per tick
  launches even when the allowance is spent.
- Proxy reserve (`proxy.json` `backlog_max_frac`, 0.9): a backlog request waits once
  its budget passes that fraction of the target, so normal and urgent work keep the
  rest. The reserve does not raise the 429 controller's target.
- Commands: `python3 -m agentq.fill --home <dir>` prints one reading (what fill and
  pacing would do now, and why) and never writes; run it on a copy of the store.
  `agentq status` shows `filler: on|off|paused` and the filler jobs running. The
  dashboard shows "Filler runs" under Work and "Holding near target: N filler runs
  filling the gap" under Limited by.

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
- Auto worktrees: a `write` job whose workspace is the main checkout of a git
  repo (with an `origin` remote) gets its own worktree when it launches, not when
  it is submitted. The worktree is `<root>/<repo>/agentq-<job id>` on a new branch
  `agentq/<job id>` from `origin/main` (fetched first; if the fetch fails, the
  local `origin/main` ref is used and the log says so). The job's `workspace`
  becomes that path and `orig_workspace` keeps the checkout. The worker's prompt
  starts with one line that names the worktree and says not to edit the checkout.
  `ls` and `result` show the worktree, and the finish log line carries its path
  and branch. Worktrees are not removed automatically: the lead merges or discards
  each branch and removes the worktree.
- Opt out per job with `--in-place`; a workspace that is a linked worktree, a
  folder that is not a git repo, or a repo with no `origin` remote is used as
  given. `broker.json` `"auto_worktree": {"enabled": false}` turns the step off, and
  `"root"` moves the worktree folder (default `~/Documents/worktrees`). Only the
  main checkout's own folder is used: a subfolder of a checkout is not a main
  checkout and runs in place.
- A failed worktree creation (git error, no `origin/main`, path taken) leaves the
  job queued with `detail` `auto-worktree failed: <error>` and retries it 60 s
  later. The third failure fails the job with `auto-worktree failed after 3 tries`.
  It never falls back to writing in the checkout.
- One write job per workspace at a time; a second waits. Auto worktrees have a
  unique path, so they run in parallel with each other and with in-place jobs on
  the checkout, which still wait for one another.
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
| `agentq.db` | SQLite store: `jobs`, `events`, `backlog_templates`, `filler_targets`, `filler_state`. `proxy_minutes` belongs to the proxy |
| `backlog/<name>.md` | a backlog template's prompt copy |
| `broker.json` | optional slot and tier ceilings, the `adaptive` admission settings, the `auto_worktree` settings and the `fill` and `pacing` settings (see above) |
| `broker.pid` | the daemon's flock'd pidfile |
| `jobs/<id>/prompt.md` | the submitted prompt |
| `jobs/<id>/runs/<n>/` | the run folder of each launch (run_worker's files live here) |
| `jobs/<id>/report.md` | the finished report |
| `logs/broker.log` | broker log |

Install the LaunchAgent (not done by the repo): fill `__PYTHON__` and `__HOME__` in
`agentq/launchd/com.agentq.broker.plist`, copy it to
`~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) <plist>`.

## Runtime ssh and pytest slots (PATH shims)

The queue limits whole jobs by their `--res` label. What a worker runs inside a job
was unguarded, so `launch_pi.py` puts `agentq/shims/` first on PATH for the pi child.
Five shell wrappers (`ssh`, `pytest`, `python`, `python3`, `uv`) in `agentq/shims/`
decide in sh, with no Python start-up on the common path (`python3 -c pass` through the
shim is about 31 ms, against 25 ms bare, measured interleaved). Only a pytest argv, or an ssh argv naming a gated
host, reaches `agentq/shims/dispatch.py`. Everything else is exec'd with the same argv.
Stdlib only; slots live in `agentq/slots.py`.

The ssh fast path reads `$AGENTQ_HOME/slots/ssh_hosts`, one host pattern per line.
`launch_pi.py` writes it at launch from `broker.json`, and every dispatch run rewrites it.
Without that file, ssh is not gated, so the launch step matters.

- **ssh** to a host in `shims.ssh_gated_hosts` holds one of `slots.ssh` (8) slots for the
  life of the command. The list has no default in the repo, so production aliases are set
  in the local `$AGENTQ_HOME/broker.json`, for example
  `"shims": {"ssh_gated_hosts": ["<prod-alias>", "<prod-alias-2>"]}` (glob patterns allowed).
  Until that list is set, no ssh is gated. ssh to any other host, such as git over ssh to
  github.com, passes through.
- **pytest**: `pytest`, `python[3] -m pytest`, `uv run pytest` and `uv run python -m pytest`
  hold one of the pytest slots, then run the real command under `memcap.sh 8192`.
  The ceiling is `slots.tests` when `broker.json` sets it, else 16. The broker's own
  `tests` cap of 6 governs broker jobs and is not reused. A new run starts at once
  while fewer than `adaptive.tests_floor` (4) are busy. Above that it also needs memory
  free of at least `tests_min_free_pct` (35) and load per CPU under `tests_max_load_per_cpu`
  (1.0), read through `memory_pressure` at most every `gauge_ttl_s` (15) seconds and cached
  in `slots/gauge.json`. An unreadable gauge adds no gate, as in the broker.
- **Slots** are `$AGENTQ_HOME/slots/<kind>/slot-N` files. A holder takes
  `flock(LOCK_EX | LOCK_NB)` and keeps it while its child runs, so a crashed holder frees
  its slot. The holder's pid, label and command are written into the slot. Waiters back
  off from 0.5 s to 5 s with jitter. After 30 minutes a waiter runs anyway, unslotted,
  and appends a line to `slots/overruns.log`: a stuck slot slows a worker but never
  deadlocks it.
- A pytest started inside a held slot (a test that runs pytest) skips the gate, through
  `AGENTQ_SLOT_HELD`, so it cannot wait on its parent's slot.
- `agentq slots` lists the holders per kind and ends with an `ANSWER:` line.

Not enforced:

- Absolute paths bypass the shims: `/usr/bin/ssh`, `/usr/bin/python3 -m pytest`, or a
  venv's `.venv/bin/pytest` run the real binary directly.
- Other `uv` forms (for example `uv run --project DIR pytest`) pass through ungated.
- A shell that sources a login profile reorders PATH and puts `/usr/bin` first. Pi's bash
  tool runs `bash -c` (no login profile), so the shims hold there. A custom
  `shellCommandPrefix` that sources a profile would undo that.
- Commands started outside a pi worker, and the broker's own launches.
- A run that overruns the 30-minute wait goes ahead unslotted by design.

Disable: launch with `AGENTQ_SHIMS=0` (launch_pi then leaves PATH alone), or drop the
shims folder from PATH. Tests: `tests/test_agentq_shims.py`.

## Known limits

- Resume: on a requeue the broker resolves the previous attempt's session file
  (`<previous run>/pi/sessions/*<session id>*.jsonl`) and passes its path in
  `AGENTQ_RESUME_SESSION`. If no file matches, the launch starts fresh. The
  launcher must honour the variable for the session to continue.
- Cancel signals the process group. A pi child that moved to its own session
  survives a cancel.
