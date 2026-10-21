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

## Stage 2 (this branch): contention plus repeated cold recomputation

Same live-shaped copy (8,221 runs, ~153k usage events), own instance,
real sources polling plus a replayed exec-view pattern (snapshot every
2 s, full burst every 30 s). Before → after, warmed server:

| endpoint | before warm p50/p95 | after warm p50/p95 | after cold |
|---|---|---|---|
| /api/snapshot (all, model) | 28 ms / 31 ms | 8 ms / 9 ms | 210 ms |
| /api/runs | 998 ms / 1041 ms | 134 ms / 179 ms | 140 ms |
| /api/concurrency | 111 ms / 164 ms | 99 ms / 103 ms | 100 ms |
| /api/workstreams | 13 ms / 306 ms | 25 ms / 26 ms | 280 ms |
| /api/workstreams (state=all) | 9 ms / 34 ms | 11 ms / 14 ms | 230 ms |
| /api/state | 6 ms / 6 ms | 8 ms / 9 ms | 510 ms |
| /api/project/phases (24 h) | 4 ms / 7 ms | 4 ms / 6 ms | 340 ms |

Under per-2 s replay ticks with live writes churning (every tick a data
version miss): snapshot p50 0.93 s → 0.21 s, workstreams p50 ~0.5 s →
~0.25 s. Source poll durations on a comparable workload: muse ~0.36 s →
~0.22 s (catch-up 1.23 s → 0.70 s), cursor ~0.4 s steady, lag back under
~1.5x interval.

What changed: the per-run usage fold is incremental (per-run versions,
bulk rewrites bump an epoch; a write refolds only that run); pricing
folds token sums per (model, band, tier, split) shape with identical
totals (per-run costs on all 3,982 folded runs match the old code; 104
runs differ by 1 ulp, all identical after 6-decimal money rounding);
`by_phase` reuses per-run classifications keyed on (rules hash, taskmap
content, plan stamp, run inputs) and classifies only rollup owners
outside the store lock; rate vectors and time bands are cached per price
shape in `dash/pricing.py`; the hourly bucket rebuild reads the catalog
once; concurrent identical cold requests share one computation
(`dash/singleflight.py`, wired by ETag identity on snapshot,
workstreams, runs, concurrency, phases, state). Regression tests:
`tests/test_perf_stage2.py` (incremental-equals-full property test with
fixed seed, grouped-vs-per-event pricing, phase invalidation,
single-flight, classify-without-lock, snapshot/bucket consistency).

Staleness bounds are unchanged: content caches still key on the data
version (any usage/run write retags); per-run caches carry their inputs
key, so a rules, taskmap, or plan edit retags precisely. API payloads
are byte-identical minus timestamps on a frozen DB (see scratchpad
freeze manifests); residual diffs under a live DB are window slides,
reconcile status closes, and live-agent decay only.

Not done: the store lock is still held through snapshot/rollup compute
(step 5) and page polling is uncoalesced (step 6); per-request Python
over ~8k runs (~0.2 s snapshot under churn) and source file scans (~20%
core) remain the floor. If server CPU stays above target, those are the
next levers.

## Perf guard: lighter page load (item 4, measured 2026-10-20)

First-load waterfall of the exec view on a quiet instance (headless
Chrome over CDP, `/`, ~7 s window):

| | before | after |
|---|---|---|
| requests | 89 | 83-84 |
| bytes | 521,351 | ~512,000 |
| boot snapshot fetches | 2 | 1 (takeSeq seeds lastTickView) |
| boot bundles / project fetches | 2 / 2 | 1 / 1 (loadNav single-flight + freshness gate; project chrome rides the shared conditional layer) |
| repeated nav (landing flip, post-action) | 5 x 200 | 0 (2 s freshness gate; actions force) |
| unchanged refetch cost | 200 | 304, headers only (all 16 polled endpoints pinned in tests/test_etag.py) |

gzip verified on the wire: 57,005 B snapshot -> 8,556 B (85% off) with
`Vary: Accept-Encoding` and the ETag intact; plan-progress.js 96,319 B
-> 28,517 B. Below-the-fold plan sums defer to an IntersectionObserver
on the program panel (same renderer once visible; panel-less views stop
refetching after the first load); concurrent identical GETs share one
fetch with per-consumer clones. HTTP/1.0 without keep-alive kept as is
(out of scope). No visual change: tests/test_page_rules.py 127 green,
exec_layout_check green. page_check.mjs shows 8-10 mismatches on
finished workstreams on every instance (token tile 0 vs plan-task
legend: a check-vs-data-shape artifact for workstreams with no 24 h
usage; active workstreams agree exactly); page_settle_check BUSY-IDLE
on the shared laptop (live writers move seq; view_seq holds).

## Perf guard: process split (item 6, measured 2026-10-20)

5x seed, 60 page-like snapshots: idle p50/p95 5/8 ms, with writer
sources 5/9 ms (1.07x/1.11x); lock wait p95 0.001 ms vs the 200 ms
budget. Recommendation: do NOT split; see docs/process-split.md for
numbers, revisit triggers, and the unimplemented sketch.

## Perf guard: TTL coalescing + cheaper cold snapshot (measured 2026-10-20)

Live symptom: with 18 live agents the store moves on every poll, the
`data_seq`-keyed snapshot cache missed on every request, and the page
paid a full cold recompute (~420 ms over ~8.2k runs, 1.4 snapshots/s)
for roughly a whole core. Two changes share the fix.

Server-side TTL coalescing (`dash/respcache.py`, freshness contract in
CONTRACT.md): heavy reads serve the generation computed within the
last `response_ttl_s` (default 1.5 s) regardless of store movement,
single-flight with stale-while-revalidate (hard 5 s max staleness),
explicit POST invalidation, 10 s pinned windows, `response_ttl_s=0`
disables. The `/api/health` `perf` block carries per-endpoint TTL
`hits`/`stale`/`recomputes`/`waits`/`hit_ratio`.

Cheaper cold path (payload-identical, proven by
tests/test_ttl.py counts-equivalence + fragment properties and the
green suites): per-run view-fragment cache keyed on
(run/usage versions, epoch, rss, context, children's usage); one
grouped counting pass replaces 38 per-scope `_agent_counts` walks and
builds running/recent/last-activity/confidence inline; shared
liveness evidence per top call; outcome memo; composite
`(pid|run_id, ts)` and `(run_id, ts)` indexes turn per-group MAX scans
into seeks; per-pid `LIMIT 1` pid starts.

Cold p50 on the live-shaped state copy (this laptop):

| endpoint | before (live) | after (copy) | budget |
|---|---|---|---|
| snapshot | 421 ms | ~80 ms | <100 ms |
| runs | 315 ms | ~60 ms | <100 ms |
| concurrency | 160 ms | ~57 ms | <100 ms |
| workstreams | 981 ms | ~144 ms | <200 ms |

Churn gate (`tests/perf/test_perf_budget.py::test_churn_coalescing_under_budget`,
budgets in the `churn` section of `perf_budgets.json`): writer on 5
live runs every 200 ms with tabs polling, ~20 s window on the 1x seed.
Remaining hot (profile lines): per-row `dict.get` floor over ~8.6k
views, `_all_runs` row materialisation, `_drop_launch_echoes` twin
fill, `_parent_map`. The store lock is still held through compute;
per-request Python over all rows remains the floor — if server CPU
stays above target, a lock-free snapshot copy or row-column layout is
next, not more key caching.
