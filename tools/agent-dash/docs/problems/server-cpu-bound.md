# server-cpu-bound: the dashboard process is CPU-bound

Process CPU is over 80% of a core over the last 5 minutes
(`perf.cpu.pct_5m` on `/api/health`). Warn on breach; critical when
the last minute is still over 80% (the breach is ongoing, not decayed).

## Why it matters

A CPU-bound server answers every page tick with a recompute instead
of a cache hit or a 304: page endpoints queue behind each other under
the store lock and sources lag their intervals.

## How to fix

```bash
python3 agent_dash.py status
```

Read `perf.endpoints` (which endpoint's p95 moved) and `perf.caches`
(which cache stopped hitting). The usual causes, in order: a write
that bumps `data_seq` on every poll (offset cursors, heartbeats —
those belong on `seq` only), a full usage refold (see
`slow-endpoint`), lock contention (see `lock-contention`). The
per-endpoint profile hints live in `perf_budgets.json` and
`docs/perf.md`.

## How to verify

`/api/health` shows `perf.cpu.pct_5m` back under 80 with the same
pages open.
