# agentq dash: live dashboard

A read-only page on the laptop that shows the Morph proxy's billed use against
each limit, worker counts, 429s, queue wait and recent jobs.

## URL and process

- Page: `http://127.0.0.1:18791/` (bound to loopback only; `--port` overrides).
- JSON: `http://127.0.0.1:18791/api/summary?hours=1|6|24|168` (default 6; 168 is the 7 d range, other values return 400).
- Log: `http://127.0.0.1:18791/api/log?source=proxy|broker&page=N&q=text` returns 100 lines a page, newest first, read from the last 5 MB of `$AGENTQ_HOME/logs/proxy.err.log` or `logs/broker.log`. Lines naming an Authorization header or an api key are dropped with their continuation lines. Only those two fixed files are readable.
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

- The page polls `/api/summary` every 30 s (every 120 s on the 7 d range), and only while the tab is visible.
  Hiding the tab stops polling; showing it again refreshes at once.
- The sampler runs one `ps -E -axo pid=,command=` call and one count query on
  `agentq.db` each minute, whether or not anyone has the page open. That is the
  only standing work. Nothing else runs between requests.
- The billed % tiles average the five most recent complete minutes. A minute
  with no proxy row counts as zero, since the proxy writes rows only for
  minutes with traffic.

## Reading the numbers

- A run is `active` when its last proxy activity is within 120 s. A queue run uses the
  jobs-table state instead (`running` is active).
- `Runs seen today` and the token totals use the local day. A run that spans midnight
  counts in full on the day it last ran.

- Billed % is minute usage over the Morph limit: total pool 10M tokens,
  DeepSeek output 70k, Kimi uncached 24,290. Values above 100% are the
  proxy overshooting a limit within a minute; the y-axis grows to fit them.
- A queue worker is a launch_pi process whose environment has
  `AGENTQ_JOB=<12 hex>`. Every other launch_pi process counts as direct.

## Ranges and buckets

- 24 h aggregates to 5-minute buckets and 7 d to 30-minute buckets: counts are summed, the p95 wait is the worst minute, the % of limit is the mean over the bucket's minutes, and worker samples are averaged.
- The token chart stacks cache read, uncached input and output per interval, with the running total as a line on the right axis.
- The total pool % uses tokens admitted in each UTC minute (`proxy_calib.billed`) where that row exists, and falls back to completion-minute totals from `proxy_minutes` otherwise.
- DeepSeek output is measured against the stated 70k per minute. Morph has not enforced that per-minute output limit, so values above 100% are marked as such rather than as rejections. Kimi uncached is an estimate computed by the proxy, not Morph's own tally.
