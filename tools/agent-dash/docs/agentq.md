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
agentq gc [--runs-older-than 7d] [--apply]       # reclaim finished run streams
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

## Streams and disk

Each run keeps its worker's pi event stream at
`$AGENTQ_HOME/jobs/<id>/runs/<n>/pi/stream-<label>.ndjson`. Delta events
dominated that file (57,117 of 61,802 lines in the largest measured stream), so
`dash/logfilter.py` now batches them instead of writing line by line:

- writes reach the file at most once every two seconds
  (`AGENTQ_STREAM_FLUSH_S`, default `2`), when the buffer is full, and at process
  exit; only whole lines are written, so a reader polling the file never sees half
  a JSON line;
- back-to-back token deltas of the same event and `contentIndex` are joined into
  one `message_update` line, keeping the object shape (the `delta` field is the
  concatenation); every reader that joins deltas sees the same text, and
  `pi.parse_stream` / `pi.final_text` return what they returned before. Set
  `AGENTQ_STREAM_COALESCE=0` to keep every delta line.

`agentq gc` removes the stream files of jobs that finished more than N days ago
(`30m`, `12h`, `7d`, `2w`, or a bare number of days); `report.md` and the run's
`out-*.md` stay. It is a dry run unless `--apply`, and it never touches a job
that is not `done`/`failed`/`cancelled`, a run folder with a live pid in
`running/`, or a stream modified after the cutoff. A candidate must also be a
plain-hex job id whose `runs` folder resolves inside `$AGENTQ_HOME/jobs`, and
every unlink is re-confined to that folder, so a crafted id or a symlinked
folder cannot reach outside the jobs tree.

The proxy's own log is size-rotated (see [agentq-proxy.md](agentq-proxy.md)).

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

- Fill (`broker.json` `fill`, off by default; `"enabled": true` turns it on). While the
  DeepSeek 5-minute billed average (`input + cache_read + output` of DeepSeek rows in
  `proxy_minutes`) is below `low_frac` (0.9) of `target_billed_per_min` (8M), the
  controller grows the filler by up to `step_workers` (4) every `step_interval_s`
  (120 s) while the average is below the low mark. It tracks a `level`,
  the filler population it has reached. Finished filler is replaced up to `level` at
  once while the gates are open (pacing still applies); growth above `level` is
  limited to the step rate. Growth steps must launch before the next one. At
  `high_frac` (0.97) the level is set to the current filler count, so nothing grows,
  and hysteresis keeps the state between the marks. While a gate is closed nothing is
  submitted; after 10 minutes closed the level decays to the current count. The level
  never exceeds `max_workers` (64), and nothing is killed. No gain is judged and no
  ceiling is learned. The level and the step timer are saved in `filler_state`, so a
  broker restart keeps the step interval.
- Speed. The fill controller reads two rates. The window average is the DeepSeek billed rate over
  `window_minutes` complete minutes (default 2, previously a fixed 5). The live projection is this
  calendar minute's DeepSeek usage in the proxy's total budget (`used_by_family`), scaled to a full
  minute, once at least 15 seconds of the minute have passed. When the projection is below the low
  mark, filling starts at once; when it is at or above the high mark, growth stops at once. Inside the
  marks, or with no projection yet, the window average decides. Gates and steps are unchanged.
  The proxy status now reports `used_by_family` for every budget.
- Gates, all checked each tick (`gate_reason`). Filler is held, with the reason shown
  in the Limited-by headline and the broker log, when:
  - the proxy status is missing or empty ("proxy status unavailable");
  - DeepSeek requests are blocked at the proxy;
  - a DeepSeek 429 came in the last `gate_429_window_s` (120 s); output 429s are included,
    since the proxy records 429s per budget in its log, not in its status;
  - a DeepSeek per-minute budget (output, or uncached input) has used its free allowance
    this calendar minute (its Morph limit times `target_frac`). Usage past the allowance
    drains the burst reserve, which only real work may spend; the reason names the budget;
  - the in-flight limit is binding ("Morph slow: in-flight limit N reached"; the
    shadow mode says so). A limit at its maximum never holds filler here;
  - the laptop is past the filler bars: CPU busy over `fill_max_cpu_busy`, memory free
    under `fill_min_free_pct`, or swap used over `fill_max_swap_frac` (`broker.json`
    `laptop`).
  Real DeepSeek work that is ready always goes first.
- Filler jobs are read-only (kind `read`, resource `light`, lane `backlog`). The work
  comes from the `"filler"` key of `$AGENTQ_HOME/backlog-seed.json`: `report_dir`, an
  optional `max_runtime` (default `15m`), `max_target_lines` (4000) and
  `min_target_lines` (300), optional `kinds` (default `["bugs", "tests", "docs", "risk"]`),
  optional `pr_window_h` (48), and `repos`. Each repo has a `workspace`, either `modules`
  or `roots` (the folders directly under each root), and an optional `github` slug
  (`owner/name`) for merged-PR reviews.
- Splitting. A module whose code (`.py`, `.sql`, `.sh`) is over `max_target_lines` is
  split into its child packages, and a child still over the limit into its files of at
  least `min_target_lines`. A module with no code stays whole. Only regular files are
  read (no FIFOs or devices). Generated and vendored folders (`target/`, `dbt_packages/`,
  `build/`, `dist/`, `.venv/`, `node_modules/`, `__pycache__/`, `site-packages/`, hidden
  folders) and git-ignored files are left out. A symlink whose resolved path leaves the
  workspace is rejected. Each module scan has a budget (`scan_max_files` 20000,
  `scan_max_mb` 200, `scan_max_seconds` 5; each must be positive and within a ceiling);
  a scan over budget keeps the module whole. Scans and the GitHub calls run on a
  background thread that refreshes the target list and the merged PRs every 30 minutes
  (or when the seed file changes), so the broker tick only reads the last complete result.
  A failed refresh keeps the previous result, logs `filler refresh failed: <type>: <message>`
  once per distinct error, and retries after 60 s, doubling to 30 minutes; a change to the
  seed file retries at once. Out-of-range values (`pr_window_h` above 720, a non-finite
  number) are refused at validation.
- Audit kinds. `bugs` (correctness defects and dead code), `tests` (risky paths with no
  test, with the test cases described, not written), `docs` (docstrings, runbooks and
  AGENTS.md text that no longer matches the code), `risk` (security issues and expensive
  patterns such as unbounded scans of large tables). A target and kind pair is due when
  it has never run or the target changed since that kind last ran. Due pairs rotate
  across targets, and the kind that ran longest ago goes first. Reports go to
  `<report_dir>/filler/<repo>/<kind>/<module-slug>.md`.
- PR reviews. Each merged PR into `main` in the last `pr_window_h` hours, for a repo with
  a `github` slug, gets one `pr` review job, checking what the merge introduced and
  whether its tests cover it. It is read through one bounded `gh pr list` call per repo
  every 30 minutes (at most 50 PRs per call). Reviewed PRs are recorded in `filler_prs`,
  and a PR is reviewed once. Reports go to `<report_dir>/filler/<repo>/pr/<number>.md`.
- Findings index. Each filler job records where its report belongs (`filler_jobs`,
  schema v6) when it is submitted, so its findings are indexed without the live settings.
  At finish, its top findings (at most five, most severe first) are added to
  `<report_dir>/filler/INDEX.md`, newest first. A failed write is recorded on the job
  (`index_error`) and retried each minute until it succeeds. Writes take an exclusive lock on
  `filler/INDEX.lock`. The index keeps the newest 2000 findings. Blocks that fall out are
  appended to `INDEX-<YYYY-MM>.md` first (flushed and fsynced); only then is `INDEX.md`
  replaced atomically. A block whose job id the archive already holds is not written again,
  so a crash between the two steps cannot duplicate it. Deduplication runs against the
  retained window. Each finding is one
  bullet, ``- **high** `file:line` — title``, and a finding whose file:line and title are
  already in the index is skipped.
- Briefs are output-lean. Every audit and PR-review brief asks for at most 10 findings,
  most severe first, each on one line (severity, `file:line`, title, and one sentence of
  why), a report under 60 lines, no code block longer than 5 lines, and no restating of the
  diff. The line form still parses into the index. A test caps each brief's length.
  There is no per-job `max_tokens`: the launcher sets none, and the proxy's
  `max_tokens_cap` applies to every DeepSeek job, not only filler.
- One submission path. The fill controller decides how many filler jobs to submit (its
  step, refill or level). The generator fills that number, and only that number, from one
  priority-ordered due list: unreviewed merged PRs first, then target and kind pairs, with
  the targets rotating from the cursor. Every filler job, whatever its kind, counts as
  queued filler (its label, or its record in `filler_jobs`) toward the controller's queue
  and the `max_workers` room, so queued reviews hold the controller until they drain.
  Labels come from `label make` as `hub-exec-backlog-filler-<kind>-<repo-short>-<readable>-<hash>`.
  The whole label, `hub-exec-` included, is at most 80 characters (the launcher's limit,
  `agentq.db.LAUNCH_LABEL_MAX`, shared with run_worker and `label check`); the readable part is
  cut to fit and the 8-character hash keeps modules apart. The generator refuses to submit a
  label the launcher would reject, logs it once, and submits the rest of the batch. PR reviews
  use `hub-exec-backlog-filler-pr-<repo-short>-<number>`.
- Repairing bad-label failures. A filler job that failed at launch with `reason=bad-label` audited
  nothing, yet its audit or PR row said it had. `agentq filler repair-failed` lists those jobs and
  the rows they wrote; `--apply` clears those rows so the targets are due again. Nothing else is
  touched. The default is a dry run. Report
  paths and labels use a readable slug plus an 8-character hash of the full path
  (`pkg-a-<hash>`), so long paths that share a prefix never collide. Reports written
  under the earlier unhashed names stay where they are.
- Dry run: `python3 -m agentq.filler --home <copy of the agentq home>` prints the targets,
  the due target and kind pairs by kind, and the merged PRs due for review. It runs the
  store migration, so point it at a copy of the store.
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
- Burst reserve (Morph's published limits, per budget in the proxy's `DEFAULT_CONFIG`): each
  DeepSeek and Kimi per-minute budget (output, uncached input) has a free allowance of its
  per-minute limit times the target, and a burst reserve (DeepSeek output 210k, DeepSeek
  uncached 1.2M, Kimi output 18,486, Kimi uncached 72,870) that refills linearly over 30 minutes.
  Usage past the allowance in a calendar minute drains the reserve. Backlog waits at the
  allowance and never spends the reserve; normal and urgent work spends it and waits only when
  it cannot pay. A 429 naming a budget empties that reserve. The reserve level is saved in
  `proxy_state.json` and decays by the time away on restart. The reserve never raises the
  429 controller's target above the Morph limit. The account-wide requests budget (1,000 a minute)
  has no reserve. `backlog_max_frac` still applies to the rolling budgets (total).
- Commands: `python3 -m agentq.fill --home <dir>` prints one reading (what fill and
  pacing would do now, and why) and never writes; run it on a copy of the store.
  `agentq status` shows `filler: on|off|paused` and the filler jobs running. The
  dashboard shows "Filler runs" under Work and "Holding near target: N filler runs
  filling the gap" under Limited by.

## Thinking levels

Each pi run can carry a thinking level (`pi --thinking <level>`: off, minimal, low, medium,
high, xhigh or max). Unset means pi's own default, which real work keeps unless configured.

- Per tier: `broker.json` `tiers_thinking`, for example `{"pi-deepseek-flash": "high"}`. Levels
  outside pi's list are dropped. Kimi runs at pi's default unless its tier is named here.
- Per job: a job's `thinking` field overrides its tier's level when it names a valid level.
- The broker passes the level to the launcher as `AGENTQ_THINKING` (never inherited from the
  broker's own environment). `launch_pi.py` adds `--thinking <level>` to the pi command, for a
  new run and a resumed one, and writes the level to `<run>/pi/thinking-level`.
- The level a job actually launched with is recorded on the job (`jobs.thinking_used`), so the
  report can group runs by it. A run from before levels were recorded reads as `default`.
- `agentq thinking report [--since <iso>]` is read-only. Per level, for DeepSeek jobs: finished
  jobs, the verdict mix, the PASS rate with a 95% Wilson interval, output tokens per job and per
  model turn (proxied requests, from `proxy_jobs`), billed tokens per job, and wall time per job.

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
    tests jobs run. Above the floor it waits while CPU busy (smoothed, see below) is at or
    above `laptop.tests_max_cpu_busy` (0.80), memory free is under `laptop.tests_min_free_pct`
    (35), or swap used is at or above `laptop.max_swap_frac` (0.90). A `light` job waits on the
    same three measures at `laptop.light_max_cpu_busy` (0.92) and `laptop.light_min_free_pct` (20).
    Load average holds nothing: short git, rg and python processes inflate it while the CPU is
    idle. An unreadable measure never holds. `ssh` keeps its fixed cap, since it protects prod hosts.
  - Laptop sampling (`headroom.py`): CPU busy comes from cumulative tick counters (macOS
    `host_processor_info`, Linux `/proc/stat`) and is smoothed by an exponential moving average
    with time constant `laptop.ewma_s` (60), sampled every `laptop.sample_s` (10) seconds. Swap
    comes from `/usr/sbin/sysctl vm.swapusage` (or `/proc/meminfo`) at the same cadence. Memory
    comes from `memory_pressure` (or `MemAvailable`) and is read at most every
    `adaptive.gauge_ttl_s` (15) seconds. The state is shared through `slots/gauge.json`. A reader
    samples only while it holds the exclusive lock `slots/gauge.lock`; a reader that finds the lock
    busy returns the saved state without sampling. Each measure expires 3 `sample_s` after its last
    valid sample, so a failed CPU read never keeps an old high reading alive, and an expired measure
    never holds work. Each hold reason in `jobs.detail` names its measure, for example
    `hold tests: cpu 84% busy >= 80%`. The dashboard's Laptop verdict appears only when the broker
    holds a ready DeepSeek job on one of these bars; raw pressure without a hold is shown in the
    secondary line.
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
| `agentq.db` | SQLite store: `jobs`, `events`, `backlog_templates`, `filler_audits`, `filler_prs`, `filler_targets` (older, read once by the migration), `filler_state`. `proxy_minutes` belongs to the proxy |
| `backlog/<name>.md` | a backlog template's prompt copy |
| `broker.json` | optional slot and tier ceilings, the `adaptive` admission settings, the `auto_worktree` settings, the per-tier `tiers_thinking` levels and the `fill` and `pacing` settings (see above) |
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
  free of at least `laptop.tests_min_free_pct` (35), CPU busy under `laptop.tests_max_cpu_busy`
  (0.80) and swap used under `laptop.max_swap_frac` (0.90). The readings are the same ones the
  broker uses (see Laptop above). An unreadable measure adds no gate, as in the broker.
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

- Absolute paths bypass the shims: `/usr/bin/ssh` or `/usr/bin/python3 -m pytest` run the
  real binary directly. A venv's `.venv/bin/python -m pytest` is covered by the pytest
  plugin below, not by the shims.
- Other `uv` forms (for example `uv run --project DIR pytest`) pass through ungated.
- A shell that sources a login profile reorders PATH and puts `/usr/bin` first. Pi's bash
  tool runs `bash -c` (no login profile), so the shims hold there. A custom
  `shellCommandPrefix` that sources a profile would undo that.
- Commands started outside a pi worker, and the broker's own launches.
- A run that overruns the 30-minute wait goes ahead unslotted by design.

Disable: launch with `AGENTQ_SHIMS=0` (launch_pi then leaves PATH alone), or drop the
shims folder from PATH. Tests: `tests/test_agentq_shims.py`.

## Pytest slot plugin (absolute-path interpreters)

The shims only see `pytest` typed by name. A pi worker that runs a venv interpreter by
absolute path (`~/Documents/projects/<repo>/.venv/bin/python -m pytest`) bypasses them,
and several such runs at 44-71% CPU each were seen on a laptop at load 38-44 on 15 cores.
`agentq/pyplugin/agentq_pytest_slot.py` closes that gap from inside pytest.

- **What it does.** A pytest plugin, stdlib only, Python 3.9+. Its `pytest_configure` takes a
  `pytest` slot in the main process with the same admission rule and 30-minute max wait as
  the shims (`slots.pytest_admission`, `slots.acquire`). The record names `AGENTQ_RUN`
  (else `AGENTQ_JOB`, else `-`) and the pytest arguments. `pytest_unconfigure` releases it.
- **Where it is loaded.** `launch_pi.py` sets, on the pi child unless `AGENTQ_SHIMS=0`:
  `PYTHONPATH` (plugin folder prepended), `PYTEST_ADDOPTS` (`-p agentq_pytest_slot` appended),
  and `PYTEST_XDIST_AUTO_NUM_WORKERS=2` and `LOR_CI_XDIST=2` when unset. Any project venv
  then loads the plugin with no change to the project.
- **xdist worker cap.** `LOR_CI_XDIST` (default 2) is the cap, and the plugin enforces it on
  xdist's parsed worker count, not on the environment: its `pytest_cmdline_main` runs after
  xdist's `tryfirst` one, which resolves `-n auto` / `-n logical` (from
  `PYTEST_XDIST_AUTO_NUM_WORKERS` or the CPU count) and builds `config.option.tx`. So an
  explicit `-n 8`, `-n auto` with a large environment value, and `-n logical` all end up at
  the cap; `-n 0`, `-n 1` and no `-n` are left alone. One `agentq pytest slot: xdist workers
  lowered from N to M (LOR_CI_XDIST)` line goes to stderr when it lowers a value.
- **Skips.** xdist workers (`workerinput` or `PYTEST_XDIST_WORKER`), so the main process
  holds the one slot for the whole run; a run with `AGENTQ_PYTEST_SLOT_HELD` set. The shim
  sets that variable for its child, so a shim-gated pytest takes no second slot. The plugin
  sets it too on acquire, so a pytest started inside the run does not wait.
- **Never fails a run.** Each error (missing or broken `slots.py`, an unwritable home, any
  exception) prints one `agentq pytest slot:` line to stderr and the run continues unslotted.
  `slots.py` is loaded by file path (`AGENTQ_SLOTS_PY` overrides it), and its package root
  is on `sys.path` only while it imports.
- **Not covered.** A pytest run with `PYTEST_ADDOPTS` set but outside a pi launch, where
  `PYTHONPATH` may not name the plugin folder, fails at `-p` load time; that is pytest's
  error and is why the launcher sets both variables together. Runs outside pi are not gated.

Tests: `tests/test_agentq_pytest_plugin.py` (slot acquire and release, skips, error paths,
a live `sys.executable -m pytest` run that checks the slot is held during the run, shim
handoff, launcher env composition, and real xdist runs that count the workers created for
`-n 8`, `-n auto` with `PYTEST_XDIST_AUTO_NUM_WORKERS=8`, `-n logical` and a raised cap).

## Known limits

- Resume: on a requeue the broker resolves the previous attempt's session file
  (`<previous run>/pi/sessions/*<session id>*.jsonl`) and passes its path in
  `AGENTQ_RESUME_SESSION`. If no file matches, the launch starts fresh. The
  launcher must honour the variable for the session to continue.
- Cancel signals the process group. A pi child that moved to its own session
  survives a cancel.
