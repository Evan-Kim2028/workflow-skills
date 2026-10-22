# db-growth: the state database is growing past its budget

Severity has two tiers.

- `warn` (soft budget): the database is over 500 MB (`db_warn_mb`), or
  its 90-day linear projection is over 1 GB (`db_crit_mb`). A
  projection is a forecast, not an outage, so it never goes critical
  on its own.
- `critical` (hard limit): the database is over 2 GB (`db_hard_cap_mb`),
  or the free space on the state volume is under 5 GB
  (`db_min_free_gb`). The detail names which limit tripped.

The trend counts usage rows per day over the last 7 days; the
projection is linear and therefore conservative for accelerating
workloads.

## Budgets

All four keys are optional in `perf_budgets.json`. A missing key uses
the default shown; a value that does not parse as a number falls back
to the defaults for all four.

| Key | Default | Meaning |
|---|---|---|
| `db_warn_mb` | 500 | soft: warn when the database is larger |
| `db_crit_mb` | 1000 | soft: warn when the 90-day projection is larger |
| `db_hard_cap_mb` | 2048 | hard: critical when the database is larger |
| `db_min_free_gb` | 5 | hard: critical when the state volume has less free space |

Free space comes from the volume that holds the state database. The
doctor reads it on each run and skips the disk limit when it cannot.

## Why it matters

Every full scan (usage fold, bucket rebuild, session dump) prices in
rows: an unbounded database is unbounded tail latency and an
unbounded backup.

## How to fix

```bash
python3 agent_dash.py db rollup --dry-run
python3 agent_dash.py db stats
```

Compact usage rows older than `rollup_after_days` (default 14) into
one row per (run, model, hour) with exact token and cost totals (see
`docs/perf.md`): per-event detail past the window degrades to hourly
by design. Then re-check the size and the projection.

## How to verify

Doctor reports `db-growth` `ok`, and `/api/health` serves the same
aggregates as before the rollup over any hour-aligned range.
