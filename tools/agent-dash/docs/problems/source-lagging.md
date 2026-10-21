# source-lagging: a source lags over 3x its interval

A source's lag is over 3 times its poll interval for 3 consecutive
polls (`perf.sources.<name>.lag_breaches` on `/api/health`). Warn at
3; critical at 6.

## Why it matters

The source's scan overruns its interval, so its numbers age while the
tick keeps serving them: the page looks live on stale data.

## How to fix

```bash
python3 agent_dash.py status
```

Read `perf.sources.<name>` (last and p95 poll seconds) and the
source's `last_duration_s` on `/api/health`. A file source is usually
a grown tree outrunning its scan budget: shrink its roots or raise
`poll_budget_s`. A remote source (Dagster) is usually the link or the
far end.

## How to verify

`/api/health` shows the source back in state `ok` with lag under its
interval.
