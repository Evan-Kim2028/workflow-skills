# Dashboard performance

Targets (live-shaped copy, idle server): `/api/state` under 150 ms warm and
under 600 ms cold, `/api/health` under 50 ms, first section under 1 s, page
settle under 2 s, idle re-renders under 3 per 30 s.

## Before (pristine code, fixed probe)

Server medians over 20 GETs, temp copy idle (no pollers), then the live
server (read-only GETs). Full table:

| endpoint | temp med | temp p95 | live med | live p95 | bytes |
|---|---|---|---|---|---|
| /api/state | 729 ms | 808 ms | 1553 ms | 2819 ms | 92 KB |
| /api/health | 244 ms | 305 ms | 461 ms | 1904 ms | 7.7 KB |
| /api/workstreams | 140 ms | 154 ms | 230 ms | 858 ms | 8.8 KB |
| /api/risks | 22 ms | 26 ms | 30 ms | 787 ms | 10 KB |
| /api/snapshot | 1 ms | 1 ms | 166 ms | 866 ms | 43 KB |
| /api/concurrency (24 h, step 60) | 3035 ms | — | — | — | 367 KB |
| /api/runs (limit 5000) | 94 ms | 104 ms | — | — | 2.25 MB |
| /api/doctor | 2 ms | 4 ms | 2 ms | 554 ms | 109 KB |

Page (headless Chrome over CDP, `?view=exec&ws=all`):

| | temp before | live before |
|---|---|---|
| first contentful paint | 56 ms | 76 ms |
| first section drawn | 1848 ms | 1082 ms |
| settle | 3363 ms | 2343 ms |
| load requests / bytes | 78 / 5.46 MB | 75 / 5.47 MB |
| drawMain p50 | 10.7 ms | 4.1 ms |
| idle re-renders per 30 s | 0 (no writers) | 10 |

Where the time went (cProfile on the copy): every `/api/state` spawned
half a dozen `ps` children plus per-pid reads (~220 ms), re-read and
re-validated a dozen bundle plans (~60 ms), ran a git child for the tool
version (~13 ms), and recomputed the usage fold plus rollups from scratch.
`/api/health` paid the same `ps` storm plus a full-table usage
`GROUP BY`. `/api/concurrency` ran one `_active` check per run per chart
point (1441 points x thousands of runs). The page parsed the 3.5 MB
mermaid bundle on every load (plan-view only) and redrew on every server
tick even when nothing changed. Live p95s are lock contention: readers
hold the store RLock through full recomputes while four pollers write.

## After (this branch)

Server medians over 20 GETs on the same copy:

| endpoint | med | p95 | bytes | gzip bytes | 304 |
|---|---|---|---|---|---|
| /api/state | 3.4 ms | 4.2 ms | 88 KB | 16 KB | ~1 ms, no recompute |
| /api/health | 14.8 ms | 16.2 ms | 4.8 KB | — | ~15 ms |
| /api/workstreams | 5.9 ms | 7.2 ms | 8.8 KB | 1.7 KB | ~2 ms, no recompute |
| /api/risks | 6.4 ms | 7.5 ms | 10 KB | 1.6 KB | yes |
| /api/snapshot | 1.0 ms | 1.5 ms | 42 KB | 7.9 KB | ~1 ms, no recompute |
| /api/concurrency (24 h, step 60) | 74 ms | — | 367 KB | 53 KB | yes |
| /api/runs (limit 5000) | 72 ms | 76 ms | 666 KB | 339 KB | yes |
| /api/plan | 1.2 ms | 1.7 ms | 30 KB | 5.7 KB | ~1 ms |
| /api/doctor | 1.8 ms | 2.2 ms | 109 KB | 21 KB | ~1 ms |
| /api/sessions | 31 ms | 35 ms | 611 KB | 125 KB | yes |
| /api/decisions | 0.8 ms | 0.9 ms | 15 KB | 6.1 KB | yes |

Cold (unwarmed process, first request): `/api/state` 393 ms, `/api/health`
40 ms, `/api/workstreams` 102 ms. The serve path also warms the hot
caches in a background thread, so first user-visible requests are warm.

Page on the same copy (3 runs): first section 305–318 ms (was 1848 ms),
settle 305–318 ms (was 3363 ms), load 73 requests / ~0.50 MB (was
78 / 5.46 MB), idle re-renders 0–2 per 30 s. `page_check.mjs --perf`
asserts first draw under 1.5 s and idle re-renders under 3 per 30 s.

What changed: sweep-line concurrency lanes (identical output, 2273 ms to
73 ms on the same query), 15 s TTL on the `ps` listing, `data_seq`-cached
usage model stats, mtime-stamped plan/approvals caches, a fingerprinted
`/api/state` body cache (store seqs, worker listing, every file read,
doctor run, 60 s quantum), result caches for snapshot (30 s window grid)
and workstream rows, identity ETags with 304-without-recompute for
state/snapshot/workstreams/runs/concurrency, content ETags everywhere
else, gzip over 8 KB, stat ETags with revalidate for statics, startup
warming, and on the page: conditional refetch with a bounded body cache,
redraw skip on identical snapshots, hidden-tab pause for ticks/warmups/
intervals, boot that overlaps nav with data, and mermaid loading only on
first plan render.

Staleness bounds: store writes invalidate by sequence; file edits retag by
mtime+size; liveness and windowed sums refresh at most every 60 s on a
quiet dashboard. A live server with active writers still re-renders on
real changes; the idle timers alone draw ~1–2 per 30 s.

## Re-measure

```sh
# 1. Snapshot live-shaped data (read-only: sqlite backup API + cp).
# 2. Serve the copy with no pollers on :8895 (see commit message).
# 3. Endpoints: time each URL 20x (min/median/p95) + sizes.
# 4. Page: node scripts/page_check.mjs --url http://127.0.0.1:8895 --perf
```

Host load moves every number: compare before/after at similar `uptime`
load, one profile at a time.
