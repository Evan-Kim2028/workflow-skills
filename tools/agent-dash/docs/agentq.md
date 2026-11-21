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
agentq gc [--runs-older-than 7d] [--keep remove|stub|stripped] [--apply]  # archive, then reclaim
agentq verdicts backfill [--apply] [--limit N]   # re-parse report.md of verdictless jobs
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
- `--max-runtime` sets the job's wall-clock cap. The default is per kind: 21600 s
  (six hours) for `write`, 7200 s (two hours) for `read` and for `review`.
  `broker.json` `max_runtime` overrides a kind's default, and the flag still wins
  over both. The write cap stays at six hours because a 48 h replay found 189 of
  480 good write jobs ran past two hours (p99 353 min): they are workers waiting on
  a real training or gate run, not loops. Read and review jobs finish well inside
  two hours (p99 104 min and 105 min over 48 h, 53 min and 83 min over 7 days).
- `escalate` moves jobs to the `urgent` lane. A running job speeds up on its next
  request through the proxy. Each escalation is logged in `events` with the session.
- `result ID` prints the report path and verdict, and the job detail when it has
  one; `--print` also cats the report. `ls` shows the detail in brackets after a
  finished job's verdict when the job has no verdict.
- `verdicts backfill` re-parses `report.md` for every finished job whose verdict
  is `none` or NULL and reports the verdict each report names, by new verdict.
  It is a dry run unless `--apply` is given, and `--limit N` stops after N jobs.
  A job that gains a verdict also gets its `detail` rewritten to `verdict <word>`.
  See [Verdicts](#verdicts).
- `cancel` drops a queued job, or sends SIGTERM to a running job's process group
  (stored pgid, checked against the stored process start time first).

Typical use from a harness: `agentq submit prompt.md --kind review`, then
`agentq wait <id>`. For "escalate this subagent work", run `agentq escalate`.

## Verdicts

A report opens with its verdict line, and `agentq/verdict.py` is the one parser
for it: the broker, `scripts/launch_pi.py` and the dashboard all call it. The
line may carry markdown emphasis, a `Verdict:` label and trailing prose, so
`PASS`, `**PASS** (no BLOCKED items)`, `Verdict: PARTIAL` and
`**Verdict: REQUEST_CHANGES.** The direction is sound ...` all read their word.
Matching is case-insensitive and the first verdict-looking line wins: the
contract puts the verdict first, and a later bullet that opens with `BLOCKED`
quotes a sub-check, not the run.

| job kind | verdict words |
| --- | --- |
| `read`, `write` | `PASS`, `PARTIAL`, `BLOCKED` |
| `review` | `APPROVE`, `APPROVE_WITH_CHANGES` (`APPROVE_WITH_NITS` reads the same), `REQUEST_CHANGES`, `REJECT` |

The word is stored verbatim in `jobs.verdict`. A report with no verdict line
stores `none` and the job detail says `no verdict line in report`, so
`agentq ls` and `agentq result` show it instead of a silent `none`. A failed
job stores `DEAD`, which is a death, not a verdict. The vocabulary is also in
the worker rules (`templates/worker-common.md`), with a separate line for
review jobs.

The dashboard's finished tile keeps its three buckets and folds the review
words in: `APPROVE`, `APPROVE_WITH_CHANGES` and `APPROVE_WITH_NITS` count as
`PASS`, `REQUEST_CHANGES` and `REJECT` as `PARTIAL`. `BLOCKED`, `DEAD` and
`none` stay out of the totals, as before. `agentq result` exits 0 for any real
verdict, review words included: a `REQUEST_CHANGES` review finished its job.

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

`agentq gc` archives, then reclaims, the stream files of jobs that finished more
than N days ago (`30m`, `12h`, `7d`, `2w`, or a bare number of days); `report.md`
and the run's `out-*.md` stay. It is a dry run unless `--apply`, and it never
touches a job that is not `done`/`failed`/`cancelled`, a run folder with a live
pid in `running/`, or a stream modified after the cutoff. A candidate must also
be a plain-hex job id whose `runs` folder resolves inside `$AGENTQ_HOME/jobs`, and
every path is re-confined to that folder, so a crafted id or a symlinked folder
cannot reach outside the jobs tree.

**Nothing is deleted before it is archived.** `--apply` does not unlink anything
itself: it hands the plan to `agent_dash.py logs archive`, which measures each
file, writes the stripped zstd copy under `<state>/log-archive/agentq/jobs/...`,
ingests the rows into the loopback VictoriaLogs store, verifies the row count and
the token sums, and only then applies `--keep` (default `remove`: the verified
zstd copy is the archive). A file whose archive fails stays where it is and is
reported; an unreachable store stops the run with exit 5 and deletes nothing. The
rows carry `ws="agentq/<workspace basename>"`, `label=<job label>` and the job's
`job_id`, `job_kind`, `tier`, `verdict`, `session` and `attempt`. See
[log-archive.md](log-archive.md).

The proxy's own log is size-rotated (see [agentq-proxy.md](agentq-proxy.md)).

## Disk janitor

Two things grow without bound on the laptop: worker scratch and the job folders.
On 2026-11-19 the data volume reached 99% (17 GB free, falling ~15 GB/hour) with
95 GB of scratch, much of it for jobs that had finished hours earlier, and 36 GB
of job folders. Nothing removed either. The broker now does, from `Broker.tick`
(`agentq/janitor.py`), before the launches, so a reap frees space before a job is
held for it.

**Scratch reaper.** A job that has been `done`/`failed`/`cancelled` for longer
than `janitor.scratch_grace_s` (default `3600`) loses its scratch folder,
`~/.cache/agent-dash/scratch/<group>/<label>/` (or under
`AGENT_DASH_SCRATCH_ROOT`). It only removes a folder that carries the launcher's
owner marker (`.agent-scratch.json`) naming that job's label, whose recorded run
folder sits under that job's own `jobs/<id>` tree, and that resolves inside the
scratch root. So a folder agentq did not create, a folder belonging to another
job that reused the label, and a symlink out of the root are all left alone. A
running or queued job's scratch is never touched, and neither is a job whose run
folder still has a live pid in `running/`. A job opts out with `keep_scratch:
true` in its prompt front matter; an unreadable prompt counts as an opt-out.

**Passed-job worktrees.** A write job's auto worktree (`agentq-<id>` on branch
`agentq/<id>`) goes once the job is `done` with verdict `PASS` and
`janitor.pass_worktree_grace_s` (default `900`) has passed. Any other outcome
(`BLOCKED`, `PARTIAL`, `DEAD`, cancelled) keeps its worktree for the lead. It goes
only when git confirms the folder is a worktree of the job's `orig_workspace` on
`agentq/<id>`, `git status --porcelain` is empty, the run folder has no live pid,
`lsof` finds no process with its cwd inside, and the prompt does not set
`keep_worktree: true`. The branch stays, so `git worktree add <path> agentq/<id>`
brings the checkout back. The folder is renamed into `.agentq-trash/` beside it,
`git worktree prune` drops it, and a detached `nice rm -rf` empties the trash, so
the tick never waits on deleting a `.venv`. At most
`janitor.pass_worktrees_per_pass` (default `8`) go per pass.

**Job-dir retention.** A terminal job older than `janitor.jobs_keep_days`
(default `7`) keeps `prompt.md`, `report.md` and the run's `out-*.md`, and the
verdict stays in the job row. Its pi streams, pi session logs and err logs are
gzipped in place. A single file over 32 MB is **not** deleted: compressing a
multi-gigabyte stream is not worth a broker tick, and a log leaves its path only
after the log archive has a verified copy, so it is left for the hourly
`logs archive --agentq` pass and counted as `needs_archive`. A file modified
after the cutoff is skipped, and so is a job whose run folder is still live. One
pass touches at most 10 jobs and 20 files per job, so a tick never walks the
whole history. `agentq gc` is still the manual, wider sweep: any age, dry run
unless `--apply`.

**Disk-pressure admission.** When free space on the volume holding the scratch
root is below `slots.disk_floor_gb` (default `25`), new `write` jobs and jobs on
a `tests` slot are held: they stay `queued` with `detail` `hold disk: disk
pressure: <free> GB free, floor <floor> GB`, and they never fail. Read and review
jobs keep launching, so the queue still drains. The gate reopens only at or above
the floor plus 5 GB, so a reap that frees a few hundred MB does not flap it, and
an unreadable volume never holds work. While the gate is closed the reapers run
on every tick instead of every five minutes.

```json
{
  "slots": {"disk_floor_gb": 25},
  "janitor": {"scratch_grace_s": 3600, "jobs_keep_days": 7,
              "pass_worktree_grace_s": 900, "pass_worktrees_per_pass": 8}
}
```

**Backup cruft.** `~/.local/state/agentq` also collects `broker.json.bak-*`,
`agentq.db.bak-*`, `ws-live-edits-*.patch` and `*.plist.bak-*` files that nothing
pruned. `agent_dash.py janitor agentq` zstd-compresses a backup older than 7 days
in place and removes the plain file only after the compressed copy verifies; it
never deletes history. The hourly archive tick runs it with `--apply`. See
[storage-budget.md](storage-budget.md#agentq-backup-cruft).

`agentq status` and `agentq slots` print one line for it: free GB, the floor, how
many jobs the gate is holding, and whether the gate is open or held. Every reap
appends a `reclaimed` row to `slots/events.jsonl` with the bytes, so
`agentq slots --history` shows a `reclaim` column per label and a trailing
`reclaimed X (Y/h) in N reap(s)` line.

## Backlog templates

A backlog template is a prompt the broker resubmits on a timer, so Morph capacity
stays busy when real work runs short. `agentq backlog add` copies the prompt to
`$AGENTQ_HOME/backlog/<name>.md`, so later edits to the source file do not change
the template. `--every` takes `30m`, `6h`, `1d` (or `90s`); `--max-runtime` takes
the same units. A template's own cap is kept when it is lower than the kind's
default and cut to the default when it is higher.

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
  `proxy_minutes`) is below `low_frac` (0.9) of `target_billed_per_min` (9.4M), the
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
- Minute gate. Filler holds while a DeepSeek per-minute budget (output, or uncached input) is at or past its free allowance. The pressure is the largest of this minute's partial count, its live projection (from 15 seconds into the minute), and the last complete minute's DeepSeek total from the store, so a partial minute that reads low early cannot hide a heavy last minute. Filler also holds while the output or uncached reserve is below `fill.reserve_floor_frac` (0.2, so 20%) of its capacity. The per-minute allowance already keeps filler from draining the reserve; the floor only keeps filler off a nearly empty reserve, so the pool does not sit idle for minutes after a reserve use.
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
  across targets, and the kind that ran longest ago goes first. `docs` is the exception:
  it is due at most once per 7 days per target, and only when the target changed since
  the last docs audit, because docstring drift is the least actionable class (55 % of
  high and medium docs findings still pointed at byte-identical code a week later). When
  `tests` and `docs` are both due, `tests` goes first. Reports go to
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
- Reading the findings. `agentq filler findings [--repo R] [--severity high] [--path FILE]
  [--limit N] [--json]` lists the newest findings of that severity, deduplicated by
  file:line and title, each with its report path and whether the cited line is still
  identical at `origin/main` (`same`), differs (`changed`), or is gone (`gone`). The
  dashboard's "Filler findings" card shows the newest eight high ones. The line check
  reads the report's `- commit:` (or a PR review's merge commit) and compares that line
  with the same line at `origin/main`; answers are cached per commit in
  `$AGENTQ_HOME/filler-findings-cache.json`, because a commit's content never changes.
  `same` is the only status that means the finding is still open; `changed` is an upper
  bound on fixed (an unrelated commit can move a line).
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
- Output estimate: by default every budget charges a request the mean of its model's last 20 completions, capped by `max_tokens`. Setting `output_estimate_pct` above 0 charges reserve-backed output budgets the nearest-rank percentile of the last `output_estimate_window` (200) completions instead. The default stays the mean: the 6 h replay found the p80 under-charges more minutes than the mean. Each completion logs a `request_output` line, so a later replay can use per-request outputs. `tools/agent-dash/agentq/output_estimate_replay.py` compares the two estimates on a copy of the store.
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
  - The `tests` ceiling is `slots.tests` for a tier whose worker runs without the shims. The
    tier's worker kind in the workers registry decides, not the session that submitted the job:
    pi and codex workers run their pytest under the shims (`agentq/worker_env.py`), so their
    `tests` jobs are admitted on live pytest slot pressure instead: `slots.tests` is the floor (always admit below it), `slots.tests_ceiling`
    (default `slots.light`) is the hard stop, and between them a job launches while the
    pytest slots have room, that is while held plus waiting stays below the pytest cap.
    A hold reads `hold tests: pytest slots busy (16 held, 3 waiting)` or
    `hold tests: tests ceiling full (20 of 20)`. An unreadable slot state falls back to the
    fixed cap. Recommended: `slots.tests` 16, `slots.tests_ceiling` 20.
  - `"adaptive": {"enabled": false}` restores the fixed caps exactly, including the
    old rule that a `saturated` proxy holds backlog jobs.
- A held job records why it waits in `jobs.detail` (for example
  `hold tests: memory free 31% < 35%`). `logs/broker.log` gets one line each time
  that reason changes. A job the launch loop never reaches because
  `MAX_CONCURRENT_LAUNCHES` (8) launches are already in flight gets
  `hold launch-cap: 8 launches in flight`, so a job skipped by the cap shows in the log
  and the dashboard instead of looking like a job with no hold at all.
- Queue wait is the first `started` event minus `submitted_at`, not `jobs.started_at`
  minus it: a requeue after a death or a stall overwrites `started_at`, so a job that
  ran for an hour and was requeued would otherwise count its second start as queue
  wait. `agentq status` prints p50, p90, p99 and max over the last 48 hours, and the
  dashboard's "Jobs queued" tile shows the same figures.
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
- Spin rule, in the same watchdog: a live job whose pi stream keeps growing while
  nothing outside it changes is spending tokens in a loop. Once a minute the broker
  samples the stream's size and a fingerprint of the run folder's report/out files,
  the run's `$AGENT_SCRATCH` folder (two levels deep, at most 500 entries: a
  background test or training run writes its log there while the worker polls it)
  and, for a write job, its worktree's git state (`HEAD` and `git status`). If the
  stream grew at least 8 MB over 45 min with that fingerprint unchanged, the job is
  stopped with `spinning: stream grew N MB in 45 min with no file or git progress`,
  requeued once and then failed, exactly like a stall. The stall check cannot see
  this loop because the stream file itself keeps the run folder fresh. `broker.json`
  `spin` takes `enabled`, `window_s`, `sample_s` and `min_stream_mb`.

  The 8 MB floor is a burn rate, not a size: 1 MB over the window is 22 KB a minute,
  which every active worker clears, and a 48 h replay of 629 finished jobs that ran
  past 45 min tripped 33 of 502 good ones at that setting. 8 MB is 180 KB a minute,
  which only a worker generating tokens without pause reaches; at 8 MB with the
  scratch folder counted the same replay tripped no good job and still caught both
  real loops (6.8 worker-hours saved).
- Repetition guard, in the launcher (`scripts/launch_pi.py` with
  `agentq/repetition_guard.py`): a pi worker that repeats one tool call 8 times in
  a row, or 12 times in its last 30 calls, or compacts 20 times, is stopped by the
  launcher itself. It writes a `BLOCKED` report naming the call, so the job
  finishes with a verdict instead of dying and being requeued into the same loop.
  `AGENTQ_REPEAT_RUN`, `AGENTQ_REPEAT_COUNT`, `AGENTQ_REPEAT_WINDOW` and
  `AGENTQ_REPEAT_COMPACTIONS` move the thresholds; `AGENTQ_REPEAT_GUARD=0` turns
  the guard off.
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
| `broker.json` | optional slot and tier ceilings, the `adaptive` admission settings, the `auto_worktree` settings, the per-kind `max_runtime` caps, the `spin` rule, the per-tier `tiers_thinking` levels, the `fill` and `pacing` settings, and the `janitor` knobs (see above) |
| `broker.pid` | the daemon's flock'd pidfile |
| `jobs/<id>/prompt.md` | the submitted prompt |
| `jobs/<id>/runs/<n>/` | the run folder of each launch (run_worker's files live here) |
| `jobs/<id>/report.md` | the finished report |
| `slots/events.jsonl` | slot acquire/release rows, plus one `reclaimed` row per janitor reap |
| `logs/broker.log` | broker log |

Install the LaunchAgent (not done by the repo): fill `__PYTHON__` and `__HOME__` in
`agentq/launchd/com.agentq.broker.plist`, copy it to
`~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) <plist>`.

## Runtime ssh and pytest slots (PATH shims)

The queue limits whole jobs by their `--res` label. What a worker runs inside a job
was unguarded, so `launch_pi.py` and `launch_codex.py` put `agentq/shims/` first on PATH for the
worker child (`agentq/worker_env.py`).
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
  `tests` cap is the floor for a pi job, not its ceiling: above the floor the broker admits a
  `tests` job while these slots have room (see Adaptive admission above). A new run starts at
  once while fewer than `adaptive.tests_floor` (4) are busy. Above that it also needs memory
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
- A run that has to wait writes `slots/<kind>/waiters/<pid>` and drops it on acquire, on
  timeout and on any exit, so the broker sees the queue as well as the holders. A waiter
  whose pid is gone is reaped by the next reader and recorded as `reaped`.
- Every acquire, release, timeout and reaped waiter appends one row to
  `slots/events.jsonl` (rotated to `events.jsonl.1` at 8 MB): epoch, kind, label, pid, wait
  and hold milliseconds, outcome, and the test file paths of the command (at most three,
  cut to 120 characters; never an environment value). It is jsonl rather than a table in
  `agentq.db` because `slots.py` is loaded by file path in arbitrary interpreters and must
  never fail a run.
- `agentq slots` lists the holders and waiters per kind and ends with an `ANSWER:` line.
  `agentq slots --history [--since 6h] [--json]` prints per-label totals (runs, hold and
  wait time, p50/p95) and the count of identical re-runs with no release in between.
- `agentq status` prints one slot line from the last hour: pytest utilization (held time
  over capacity), median wait, and a cap hint. It recommends raising `slots.tests` only when
  the median wait is at or above 60 s and the laptop has headroom at the tests bars, and it
  never changes the config.

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

Disable: launch with `AGENTQ_SHIMS=0` (the launchers then leave PATH alone), or drop the
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
  `PYTHONPATH` (plugin folder prepended), `PYTEST_ADDOPTS` (`-p agentq_pytest_slot` and
  `-p agentq_pytest_cache` appended), and `PYTEST_XDIST_AUTO_NUM_WORKERS=2` and
  `LOR_CI_XDIST=2` when unset. Any project venv then loads the plugins with no change to
  the project.
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

## Pytest result cache (repeat runs)

A pi worker re-runs the same one-file pytest command many times per job. Measured over the
last 300 `pi-deepseek-flash` write jobs: 3012 invocations, 12.3 per job, median one file;
85 % of jobs re-ran an identical file set, and 85 % of those repeats had no edit at all in
between (24.9 h of 57.6 h of pytest wall time). 98 % of invocations pass, so a pass-keyed
cache hits almost always. `agentq/pyplugin/agentq_pytest_cache.py` turns such a repeat into
one line and exit 0.

- **Where it is enforced.** The same plugin folder and `PYTEST_ADDOPTS` as the slot plugin,
  so it also covers the 31 % of invocations that run a venv interpreter by absolute path and
  never reach the PATH shims.
- **Hook.** `pytest_cmdline_main`, before collection, because the expensive part of the top
  files is a module-level import (a deselect after collection would save nothing). When every
  positional target is a recorded pass it prints one line to stderr and returns 0:
  `agentq pytest cache: 2 file(s) unchanged since a recorded pass on this tree, skipped
  (a.py, b.py); re-run with LOR_PYTEST_CACHE=0, or change a file in the tree`.
- **The key is the whole tree**, not an import closure. An import walk cannot see a data
  file, a script a test runs through `subprocess`, a fixture directory, a directory listing
  or a plugin named by a string, so it produced false hits on 52 % of one real suite. The
  whole tree has no such blind spot and costs one `git ls-files` plus one `git status`
  (measured: 0.2–0.5 s on a 10 000-file repo, ~1 s on a 14 000-file repo with 3 500
  gitignored files):
  - **Tree content.** `git ls-files -s` (index blob shas) plus the content sha of every path
    `git status --porcelain=v1 -z --untracked-files=all` reports as modified, deleted or
    untracked, so any edit anywhere in the repo, committed or not, changes the key. A
    submodule is keyed by its own `HEAD` and `status`. A tracked file is always keyed; an
    untracked path under a tool cache (`.pytest_cache/`, `__pycache__/`, `.venv/`,
    `node_modules/`, `.mypy_cache/`, `.ruff_cache/`) is not, because pytest writes one on
    every run and the key would never repeat.
  - **Ignored inputs.** Size and mtime of every gitignored file under the repo, because a
    generated input (a dbt `target/manifest.json`, a `.env`, a built parquet) is gitignored
    and is still an input. The walk skips the tool caches above, `.git/`, the cache's own
    dir, and a nested checkout (a directory with a `.git` entry: another checkout of this or
    another repo, not an input of a run here, and walking the ones under a `.worktrees/`
    folder cost 26 s on a 14 000-file repo). Over `IGNORED_MAX_FILES` (200 000) files or
    `IGNORED_MAX_S` (3 s) the run is not cached.
  - **argv**, the whole environment minus `ENV_SKIP` (the volatile variables that cannot
    change what a test does: `AGENTQ_*` run ids, `TERM`, `SHLVL`, `PWD`, `OLDPWD`, `_`, the
    terminal and session ids, `COLUMNS`/`LINES`, `PYTEST_CURRENT_TEST`, `PYTEST_XDIST_*` and
    `LOR_PYTEST_CACHE` itself), the interpreter realpath and its `sys.version`, and the cwd.
  - Every `conftest.py` from each target's directory up to the filesystem root, keyed by
    absolute path (pytest loads a conftest above the git toplevel too, and that file is not
    in the tree sha), and the content of a `-c`/`--config-file` file.

  A miss is cheap; a false hit is a lie, so a run that cannot be keyed runs normally and is
  not cached: no target, a missing or directory target, a state-dependent selector (`--lf`,
  `--ff`, `--nf`, `--sw`: the target set comes from `.pytest_cache` state, not from the
  argv, so a recorded pass could cover a subset of what the same key later means), a random
  test order (`pytest-randomly` with no `--randomly-seed`), no git tree sha, and an
  ignored-input walk over the bound. The reason goes in the `why` field of the event row.

  Two inputs the tree sha cannot see, stated rather than claimed away: the content behind a
  data path outside the repo (`DATA_ROOT=/elsewhere` is keyed by the path string, not by what
  is under it), and the files inside a nested checkout under the repo (keyed as one skipped
  directory). Both are a miss only if the path string or the checkout's own tree changes.
- **Recorded only on exit 0** with at least one passed test, in the main process only. A
  failure, an error, a skip-only run and a `-x` early exit write nothing.
- **The guard, folded in.** The shim's `pytest_reason` refusals (a bare pytest, a directory
  target, more than 25 files) are applied here too, with the shim's message and exit 2, so the
  absolute-path invocations the shim never sees get them. `AGENTQ_GUARD=0` turns that off.
- **Off outside a worker.** It does nothing unless `AGENTQ_JOB` or `AGENTQ_RUN` is set, and
  `LOR_PYTEST_CACHE=0` turns it off. A gate or CI run that has to be a real run must set that
  variable explicitly rather than rely on `AGENTQ_JOB` being unset; this repo's own gate runs
  `python3 -m unittest`, which never loads this plugin.
- **Never fails a run.** Any error prints one `agentq pytest cache:` line to stderr and pytest
  runs normally. Stdlib only, Python 3.9+, and it imports nothing from agentq: the home is
  `$AGENTQ_HOME` or `~/.local/state/agentq`, and it appends the event row itself.
- **Visibility.** One row per decision in `slots/events.jsonl` with `cache=hit|miss` (and
  `why` when a run could not be keyed), counted per label by `agentq slots --history`.
- **Entries** live under `$AGENTQ_HOME/pytest-cache/entries/<xx>/<key>.json` (a recorded
  pass; the file is a marker, its content is never read). Entries are pruned by age (30 days)
  and count (20 000) at most once an hour.

Tests: `tests/test_agentq_pytest_cache.py` (key material and each miss trigger, every
false-hit repro from the review — a data file, a `subprocess` script, a directory listing, a
fixture dir, an env var outside the old allowlist, a gitignored generated input, a computed
import, a plugin named by a string, a new file that changes import resolution, a conftest
above the git toplevel, a `-c` file outside the repo, a dirty submodule, a state-dependent
selector — the key cost on a 10 000-file repo, the event rows, gating, the guard, the
never-fails paths, and real `sys.executable -m pytest` runs for a hit, an edit, a data-file
edit, `--lf`, a failing run, a skip-only run and the switches).

## Known limits

- Resume: on a requeue the broker resolves the previous attempt's session file
  (`<previous run>/pi/sessions/*<session id>*.jsonl`) and passes its path in
  `AGENTQ_RESUME_SESSION`. If no file matches, the launch starts fresh. The
  launcher must honour the variable for the session to continue.
- Cancel signals the process group. A pi child that moved to its own session
  survives a cancel.
- The janitor's disk gate is in memory: a broker restart re-reads the volume and
  starts from that reading, so a restart while the disk is full holds work at
  once, and a restart after a reap releases it at once. The reapers themselves
  are stateless and safe to run twice.
