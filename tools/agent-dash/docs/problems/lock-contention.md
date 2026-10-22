# lock-contention: the store lock wait p95 is over budget

Store lock wait p95 over the last 10 minutes is over 200 ms
(`perf.lock.wait_p95_ms` on `/api/health`). Warn on breach; critical
past double.

## Why it matters

Readers hold the one store lock through full recomputes while pollers
write: every wait millisecond is page latency and source lag.

## How to fix

```bash
python3 agent_dash.py status
```

Read `perf.lock` (wait vs hold: a high hold means one long compute
under the lock; a high wait with a low hold means many small
holders). Move the compute out of the lock and serve the result from
a cache keyed on the data sequence (docs/perf.md); never widen the
locked region to fix a race.

## How to verify

`/api/health` shows `perf.lock.wait_p95_ms` back under 200 ms.
