# Process split: recommendation (item 6, measured 2026-10-20)

## Recommendation: do NOT split

On the 5x synthetic seed (42,155 runs / 787k usage rows / 1.2M samples /
355 MB) a server with writer sources polling every 2 s serves a
page-like snapshot load (one `/api/snapshot` per second for 60 s) at
**p50 5 ms / p95 9 ms**, versus **p50 5 ms / p95 8 ms** with an empty
registry: **1.07x / 1.11x**, inside noise. Store lock wait p95 is
**0.001 ms** against the 200 ms doctor budget; lock hold p95 stays under
0.5 ms. The item-1 gate already holds steady-state CPU under 25% of a
core and every source poll under 1 s on the same seed.

Why there is nothing to split: request serving is warm-cache reads
(single-flight + incremental caches, items from passes 1-2), source
writes are incremental single-run refolds (pass 2) plus hourly rollup
bundles (item 3), and the two meet only at a sub-millisecond lock hold.
The GIL is not the bottleneck; a second process would add SQLite
writer/reader coordination and cache-duplication costs for no measured
gain.

Caveats: the probe used one writer source at one row per 2 s (the same
`add_usage` path real sources use). Live runs about ten sources, but the
lock-wait headroom is five orders of magnitude, so even 50x the write
rate stays two orders under budget.

## Revisit when

- `lock-contention` doctor check warns (lock wait p95 over 200 ms), or
- steady-state CPU approaches the 25% budget with margin under 1.3x, or
- a new source family does full-table writes per poll.

## If it ever splits: design sketch (not implemented)

Ingest process owns all SQLite writes (sources + rollup + checkpoints);
the serving process opens the DB read-only with its own caches and
single-flight. Migration: WAL mode already on (readers never block the
writer); move `Registry.tick` into the ingest loop; serve `seq`/`view_seq`
from a shared poll of the DB change clock; risks are split-brain deploys
(two builds must version-match: extend the `build_id` replace-guard to
both processes) and doubled memory for the fold caches. Revisit only on
the triggers above.
