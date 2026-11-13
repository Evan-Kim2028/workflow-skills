# agentq dash: live dashboard

A read-only page on the laptop that shows the Morph proxy's billed use against
each limit, worker counts, 429s, queue wait and recent jobs.

## URL and process

- Page: `http://127.0.0.1:18791/` (bound to loopback only; `--port` overrides).
- JSON: `http://127.0.0.1:18791/api/summary?hours=1|6|24|168` (default 6; 168 is the 7 d range, other values return 400).
- Log: `http://127.0.0.1:18791/api/log?source=proxy|broker&page=N&q=text` returns 100 lines a page, newest first, read from the last 5 MB of `$AGENTQ_HOME/logs/proxy.err.log` or `logs/broker.log` (the proxy log is size-rotated at 10 MB with three backups, so only the live file is shown). Lines naming an Authorization header or an api key are dropped with their continuation lines. Only those two fixed files are readable.
- Page: `/?range=1h|6h|24h|7d` selects the range on load.
- Run: `python3 tools/agent-dash/agentq/dash.py`. Launchd unit template:
  `tools/agent-dash/agentq/launchd/com.agentq.dash.plist` (placeholders
  `__HOME__` and `__PYTHON__`; logs go to `$AGENTQ_HOME/logs/dash.log`).

## What it reads

- The proxy's `/agentq/status` (2 s timeout). When the proxy does not answer,
  `live` is null and the limit tiles show no proxy target.
- `agentq.db`, opened read-only. The dash never writes it. The Runs and
  Performance sections read the `proxy_jobs` table (per job and model: requests,
  tokens, 429s, wait and upstream time) joined to `jobs` for the label, tier,
  state and verdict. Until that table exists the summary names `runs` under
  `errors` and those sections stay empty; the rest of the page still works.
- `$AGENTQ_HOME/dash.db`, which the dash owns. It holds one worker sample per
  minute and prunes samples older than 7 days.
- `$AGENTQ_HOME/broker.json` for the worker caps (DeepSeek 64, Kimi 2 when the
  file is absent).

A source that fails shows as null or empty in the JSON and is named under
`errors`. It never turns the whole summary into an error.

## Cadence and cost

- The server computes the summary for all four ranges on background threads
  and serves the stored JSON. Cadence: 1 h and 6 h every 5 s, 24 h every 15 s,
  7 d every 60 s. A range nobody has requested for two minutes is not
  recomputed. No request waits for a compute, a reader or the proxy.
- A request with no stored body starts one request build for that range and
  waits at most 0.5 s for it. The build is DB-only (no proxy log, so its 429
  budgets are not counted), with every SQLite wait cut to a 0.8 s budget
  (`partial: true`, with the cut sources named under `errors`). If it has not
  stored a body in time, the request gets a `warming_up` body and the page
  shows "updating…" while the last figures stay on screen. The full compute
  replaces the partial. A range never has two builds at once.
- The 429 budgets (rejections and the five-minute limit flags) come from the
  proxy log's `calib` lines. The live file and its `.1` backup are parsed
  incrementally and cached by inode: each read parses only the bytes appended
  since the last one, and a rotated file keeps its counts. A cold read covers
  at most 5 MB of each file. `proxy_calib` in `agentq.db` records only a
  per-minute 429 count, not the budget, so it cannot replace the log.
- The server accepts up to 64 queued connections (`request_queue_size`), so a
  page that opens several requests at once is not refused.
- A watchdog checks each range's data age. Past 3 times its cadence the server
  logs once and starts a fresh compute thread for that range. The page shows the
  age in amber past twice the cadence ("data 48 s old").
- `SIGUSR1` dumps every thread's stack to stderr (`faulthandler`), for a stalled
  server: `kill -USR1 <pid>` and read `logs/dash.log`.
- The page polls `/api/summary` on the same cadence as the range it shows
  (5 s for 1 h and 6 h, 15 s for 24 h, 60 s for 7 d) and only while the tab is
  visible. Hiding the tab stops polling; showing it again refreshes at once.
  The run log refreshes every 15 s.
- The header shows "updated HH:MM:SS ET", the time the data was computed (the
  summary's `generated_at`). Every time on the page is US Eastern, set
  explicitly with `America/New_York` rather than the browser's zone; the run
  log's times stay in UTC and are labelled so.
- The proxy status probe and the laptop memory reading run on their own threads.
  The summary carries the last value of each with its age (`source_age_s`).
- The sampler runs one `ps -axo pid=,command=` call and two count queries on
  `agentq.db` each minute, whether or not anyone has the page open. That is the
  only standing work. Queue workers are the jobs in the `running` state, not
  an environment count, because `ps -E` is the slow part under load.
- The "Now" tiles (waiting, in flight, runs active, queued, and the 5 and
  15 minute activity) read the latest minutes. Every other tile follows the
  selected range and says so in its label. A minute with no proxy row counts
  as zero, since the proxy writes rows only for minutes with traffic.

## Reading the numbers

- A run is `active` when its last proxy activity is within 120 s. A queue run uses the
  jobs-table state instead (`running` is active).
- Tokens, finished verdicts and 429s are summed over the selected range from
  the billed minutes and queue jobs. Top consumers show lifetime tokens of the
  runs active in the range: `proxy_jobs` keeps one total per job and model, not
  per minute, so a run's tokens cannot be cut to the range.

- Billed % is minute usage over the Morph limit: total pool 10M tokens,
  DeepSeek uncached input 400k, Kimi uncached 24,290. The main capacity chart
  plots only the enforced limits (total pool and DeepSeek uncached input).
  Values above 100% are the proxy overshooting a limit within a minute; the
  y-axis grows to fit them.
- A queue worker is a launch_pi process whose environment has
  `AGENTQ_JOB=<12 hex>`. Every other launch_pi process counts as direct.

## Ranges and buckets

- 24 h aggregates to 5-minute buckets and 7 d to 30-minute buckets: counts are summed, the p95 wait is the worst minute, the % of limit is the mean over the bucket's minutes, worker samples are averaged, and the 429 chart plots the mean per minute (so "429s per minute" holds at every bucket size).
- The token chart stacks cache read, uncached input and output per interval, with the running total as a line on the right axis.
- The total pool % uses tokens admitted in each UTC minute (`proxy_calib.billed`) where that row exists, and falls back to completion-minute totals from `proxy_minutes` otherwise.
- DeepSeek output is a plain count per minute (activity row). Its stated 70k per minute is not enforced by Morph, so it is not charted as a % of limit. Kimi uncached is an estimate computed by the proxy, not Morph's own tally.

## Limited-by headline

When ready DeepSeek jobs carry a broker hold, the headline groups the holds by
kind, not by exact text. Each jobs.detail string is normalised to a kind (launch
pacing under Morph's uncached-input limit, a proxy wait, laptop CPU, memory or
swap, a production ssh slot, a free worker slot, a workspace, the pool cap, or
other). Numbers are left out of the kind, so the same hold with different live
numbers is one clause with a count.

- The headline is the biggest kind; its chip names the kind. The next two kinds
  follow on the secondary line as "Also: ...". No verdict has more than three clauses.
- Launch pacing means the broker is holding jobs back before launch ("waiting to
  launch"). The clause quotes the largest current uncached-input figure in the group.
- "Waiting at the proxy" is used only for requests the proxy itself holds, with
  the queued count from the proxy's status.

