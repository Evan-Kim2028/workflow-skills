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
`hits`/`stale`/`recomputes`/`waits`/`hit_ratio`; `/api/health` itself
is uncached so its store-lock watchdog is sampled on every request.

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

## Stage 3 (2026-11-06): live-shaped copy, pollers running, probe-driven

Owner report: "slow to load and not snappy across basically every function".
This stage measures on a live-shaped copy (an SQLite `.backup` of the live
database taken 10:51, 803 MB, with real sources polling this laptop's
transcripts; Linear and Dagster off; `AGENT_DASH_READONLY=1`) and starts from
the same frozen copy for every build, so before and after see the same data.

### Probe

`scripts/perf_probe.py` (stdlib, GET only). Per endpoint: first (cold) hit,
then 20 warm hits, median, p95, bytes, gzip bytes. `--cpu-window 60` drives a
page-like load (snapshot, workstreams and runs every 2 s) and reports server
CPU from the server's own `perf.cpu.cpu_seconds`. `--state-dir` adds SQLite
sizes. Re-run:

```sh
python3 scripts/perf_probe.py --base http://127.0.0.1:8897 --n 20 \
  --cpu-window 60 --pid <server-pid> --state-dir <state-dir> --json out.json
```

### Before and after

HEAD = `bf05361` (the branch base, with the committed live changes). After =
this branch, same frozen data, same probe. Warm numbers for the endpoints
marked (c) are cache hits: their cold compute is unchanged, see "Not done".

| endpoint | HEAD cold / med / p95 ms | after cold / med / p95 ms |
|---|---|---|
| /api/health | 1085 / 639 / 1029 | 877 / **10 / 11** |
| /api/state | 1941 / 3.5 / 9.8 | 1778 / 3.2 / 3.7 |
| /api/snapshot | 717 / 1.1 / 1.5 | 514 / 1.1 / 1.5 |
| /api/workstreams | 1258 / 25 / 29 | 947 / 18 / 23 |
| /api/project/phases | 1929 / 0.6 / 0.7 | 1817 / 0.5 / 0.9 |
| /api/project/graph | 2768 / 2233 / 3871 | 2633 / **0.8 / 1.1 (c)** |
| /api/project/feed | 569 / 499 / 1079 | 540 / **0.5 / 0.7 (c)** |
| /api/archive | 1012 / 940 / 1270 | 1026 / **0.5 / 0.7 (c)** |
| /api/risks | 93 / 56 / 90 | 101 / **0.5 / 0.7 (c)** |
| /api/runs | 539 / 4.4 / 4.7 | 523 / 3.6 / 4.0 |
| /api/usage | 23 / 3.6 / 4.0 | 247 / 2.9 / 3.1 |
| /api/sessions | 307 / 309 / 414 | 265 / 283 / 326 |
| /api/concurrency | 369 / 2.6 / 3.1 | 333 / 1.9 / 2.2 |
| /api/doctor | 8 / 7.5 / 8.5 | 26 / 25 / 32 (noisy run, see below) |
| /api/ws/progress?ws=all | 21940 / 2.3 / 2.5 | 27428 / 2.5 / 3.2 (not fixed) |
| /api/reconcile | 1308 / 0.5 / 0.7 | 1269 / 0.6 / 0.9 |

Server CPU, 60 s page-like window: HEAD 142% and 122% of one core (two runs);
after 111% (range 110–147% over five builds). Target 15%.
Page (`page_check.mjs --perf`, first section drawn): HEAD 7.2 s and 5.1 s;
after 3.8 s, 3.5 s and 4.6 s. Budget 1.5 s. Warm `/api/state` and the snapshot
tick are 1–3 ms when the process is idle; under the pollers' load the
snapshot tick p95 is ~1 s on both builds.
`/api/doctor` warm median moved 8 → 25 ms in the last run only; the doctor
block itself is unchanged, and the repeat run is within noise of HEAD-1 (19 ms).

Live-shaped run notes: `/api/health` p95 fell from 2.2 s to 11 ms; `page_check`
tile/legend parity is OK (`agent_dash.py check --page`); `check` reports one
mismatch on this copy (`stale running`, a dead pid row from the frozen data;
not caused by this branch, not investigated further).

### Ranked causes (measured, not assumed)

Sampled with an in-process stack sampler (5 ms, every thread; the sampler is
a diagnostic wrapper that is not shipped) and cProfile on the copy:

1. **Source pollers walk the scratch trees every 2 s.** Sources were ~80% of
   sampled CPU (~1.4 cores). Largest items: `realpath` per candidate in
   `pi`/`commandcode` outdir discovery, `rundirs._touched` (a scandir and a
   stat per child of every registered run folder, through `scan_roots`, called
   by six sources), `_subdirs` listings, grok `_manifest_dirs`, and
   `attribution` map stats per run upsert. **Fixed in part** (see below).
2. **`/api/health` re-read up to 1 MB of every registered run folder's
   manifests per request** (`rundirs.describe_roots` through
   `Registry.health`). **Fixed:** 15 s memo, 319 ms median → 10 ms.
3. **`/api/ws/progress?ws=all` recomputes the whole store once per workstream**
   (`build_wsprogress` → `build_replay` → `query_runs` → full `_scoped_usage`,
   58 full passes for 30 workstreams, ~17 s offline on the copy). **Not fixed**
   (see "Not done"): the `ws` filter applies per row before the lane and
   workflow collapse, so a single pass grouped by workstream is not equal by
   inspection.
4. **`VACUUM` ran under the store lock** from `prune_offsets` once a pass
   drained (4.6 s on a 780 MB copy with no contention; the profile attributes
   92 s of prune time to the window, lock waits included). **Fixed:** no
   VACUUM on the serving path.
5. **WAL growth.** The live WAL was 830 MB with `wal_autocheckpoint` 1000:
   a passive checkpoint cannot reset a log that a reader still holds, and the
   WAL never shrank. **Fixed:** `journal_size_limit` 64 MB, a startup
   TRUNCATE checkpoint, and an off-lock PASSIVE checkpoint once the WAL passes
   16 MB (prune tick).
6. **Navigation awaited `/api/health`** (base commit's `navInflight` Promise.all).
   **Fixed:** health is fetched without being awaited; it only refreshes the
   stale badge.
7. **Expensive heavy reads recomputed on every request**: graph, archive, feed,
   risks. **Fixed:** routed through the existing TTL coalescing (`ENDPOINT_TTL`
   entries, `response_ttl_s`). Their warm numbers are now cache hits (c); cold
   is unchanged. The payloads of these four gain a `generated_at` key, which
   the other TTL-served endpoints already carry (additive).
8. Dead offset rows: prune batch 2,000 → 20,000 (a 504k backlog drains in
   tens of minutes, not hours). On this copy no offset path is gone (see
   retention below), so nothing was pruned.

Checked and ruled out: the store lock (p95 wait 1 ms, hold p95 0.6 ms on the
copy: the bottleneck is GIL time, not the lock); the health-refresh thread
(5 s, data-seq cached); the `cache_size` pragma (kept, one connection).

### What changed (this branch)

- `dash/store/base.py`: no VACUUM in `prune_offsets`; `journal_size_limit`;
  `checkpoint_wal()` (off-lock PASSIVE; TRUNCATE only at startup); the WAL is
  checkpointed at the end of each prune call past 16 MB.
- `dash/store/util.py`: `PRUNE_BATCH` 2,000 → 20,000.
- `static/core/api.js`: health is not awaited in navigation; the stale-badge
  hover names lag in words (page rule).
- `dash/sources/__init__.py`: shared memos: `realpath_memo` (60 s),
  `scan_roots_memo` (10 s), `describe_roots` memo (15 s, used by
  `Registry.health`), `discovery_memo` keyed by the source's `dir_scan_s`
  (thread-local; 0 = per poll).
- `dash/sources/pi.py`, `commandcode.py`, `grok_cli.py`: outdir and manifest
  walks reuse their last listing for `dir_scan_s` (15 s default; same knob as
  manifests and claude); `_subdirs` uses the (inode, mtime) listing cache.
- `dash/sources/{grok_cli,muse,cursor,manifests}.py`: `scan_roots_memo`,
  realpath memo.
- `dash/sources/muse.py`: the clock repair (a full muse-run scan under the lock)
  runs at most every 30 s instead of every poll (idempotent).
- `dash/server.py`, `dash/respcache.py`: archive, risks, project/graph and
  project/feed go through `_ttl_serve`; entries in `ENDPOINT_TTL`.
- `scripts/perf_probe.py` (new): the probe. `scripts/gate_map.json`: maps it.
- Tests: `tests/test_perf_stage3.py` (new, 7 tests: no VACUUM, freelist reuse,
  checkpoint thresholds and modes, off-lock checkpoint, memo windows).
  `tests/test_store.py`: `test_prune_vacuums_once_per_pass` renamed to
  `test_prune_drains_a_pass_without_vacuum` (same assertions; the old name
  described the removed behaviour). `tests/test_pi.py`, `tests/test_commandcode.py`:
  two source fixtures set `dir_scan_s: 0`, because those tests pin a row seen on
  the next poll (per-poll discovery is the contract they test; the knob exists
  for this, muse's tests already use it).
- `perf_budgets.json`: unchanged (no budget was raised or lowered).

Kept on purpose: the `_seq` bump on cursor writes (`set_offset`).
`tests/test_view_seq.py::test_offset_write_moves_seq_only` pins it. Its comment
said the opposite; I briefly removed the bump, the pinned test caught it, and
it is back. `/api/state` keys on `store.seq`, so every cursor advance still
invalidates the state cache. That is the next lever for `/api/state` (see below).

### Retention

- `process_samples`: already bounded by `SAMPLE_KEEP_S` (48 h, pruned on each
  insert). It holds ~700k rows at this sampling rate. Not reduced: the rows back
  run liveness evidence, and a shorter window could change run status for
  older runs, which the brief forbids.
- `file_offsets`: 504k rows on the copy: 486k `<db>#b#<id>` blob markers (their
  databases exist), 15k absolute paths (all exist), 2.5k relative cursor keys
  (always kept). Nothing is prunable on this copy, so the prune change is proven
  by unit tests, not by a copy run. Blob markers are not pruned: they are live
  cursor state, and pruning them needs a per-blob liveness rule. Not done.
- Token totals and run counts: checked offline on the frozen copy with the
  new `Store` (startup checkpoint plus a full offset prune): 791,182 usage
  events, input/output/cache/reasoning token sums, cost (15,684.11 USD), and
  39,801 runs by status are identical before and after. Live-data comparison is
  not possible while the sources keep writing.

### Verification

- `scripts/gate.sh train/dash-storage-perf --list` selects 23 files. The
  worker rule stops a gate that selects more than 3 suites, so I ran the
  explicit files for the changed modules instead. Widened by `dash/sources/*`
  and `dash/store/*` mapping (the gate table lists every source suite).
- Scoped suites, green except the pre-existing ones below: `test_store`,
  `test_pi`, `test_commandcode`, `test_cursor`, `test_muse`, `test_manifests`,
  `test_grok_cli`, `test_server`, `test_etag`, `test_ttl`, `test_perfmon`,
  `test_state`, `test_view_seq`, `test_doctor`, `test_perf_stage2`,
  `test_bundles_api`, `test_perf_stage3`, `test_poll_budgets`: 758 passed, 5
  skipped, 1 failed (below).
- `tests/test_page_rules.py` + `tests/test_privacy_pin.py`: 156 passed, 1
  failed: `test_nav_and_api_share_inflight`, which also fails at HEAD. It
  expects the nav fetch list to be exactly four calls; the base commit added
  `/api/health` to nav (now not awaited, still fetched). `test_hover_text_has_no_raw_numbers`
  was failing at HEAD and is fixed here. `test_plan_sections_term_rule`
  (the known failure) passes in this environment.
- `tests/test_server.py::StopRestartTest::test_stop_returns_1_while_the_port_is_bound`
  fails at HEAD in this environment too (not caused here).
- `ADASH_PERF=1 tests/perf/test_perf_budget.py`: 5 of 6 pass in the runs that
  finished; `test_cold_and_warm_5x` returns 503 on `/api/state` cold (the store
  lock's 5 s busy path while the warm thread holds the lock) in 3 of 4 full-file
  runs on this branch, and passes 3 of 3 when run alone. HEAD passed the full
  file 2 of 2. Measured cold `build_state` at 5x is 4.2–4.3 s on both trees, so
  I do not have evidence of a slower compute. This is an open item, see below.
- `agent_dash.py check --url … --page`: passes (tiles and legends match the API).
- `page_check.mjs --perf`: first-draw budget fails on both trees (above).
- `node --check static/core/api.js`: ok.

### Not done, and why

- **Server CPU 111% vs 15% target.** Most of it is per-poll file work in the
  sources (per-file tails, muse and cursor discovery, attribution stats, the
  process listing). Memoising discovery windows helped 142 → 111%, but the
  remaining cost is spread across per-file reads. The next step is event-driven
  or cadence-driven discovery for each source (manifests already does this with
  `dir_scan_s`), and per-file stat caching in the tails. That is a source-by-source
  change, not one more memo.
- **Page first draw 3.5–4.6 s vs 1.5 s.** Requests queue behind source CPU (GIL).
  It should follow the CPU work, but I did not measure that link directly.
- **`/api/ws/progress?ws=all` cold 16–27 s.** Needs a single store pass for all
  workstreams with the per-ws filter reproduced exactly. Not done: the
  equivalence needs a property test, and I did not have time to write one.
- **Cold `/api/state`, `/api/project/phases`, `/api/project/graph`,
  `/api/workstreams` at 0.9–2.6 s.** Cold compute is unchanged. Warm is fine.
  The `/api/state` cache key includes `store.seq`, which every cursor advance
  bumps; a `view_seq` key would help warm hits, but the state body's
  dependencies need auditing first.
- **`/api/sessions` p95 ~280–330 ms** (budget 300). Not wrapped in the TTL.
- **Doctor** runs every 300 s; each run takes seconds (lifecycle scans, `pgrep`).
  Not profiled in isolation; its run time is not in the health block.
- **File_offsets blob markers** (see retention): no pruning rule.

### Cutover

No schema change, no data migration. The steps:

1. Ship this branch as a release, then restart the `serve` process once (Python
   changes need a restart, `static/core/api.js` is live on reload). Restart
   order: stop the old process by pid, then start the new one; health answers
   within about a second of bind.
2. One-time costs on the first start: the startup TRUNCATE checkpoint of the
   existing WAL. Measured with a synthetic 1 GB WAL on this laptop (idle):
   1.6 s. The live WAL was 830 MB, so expect about 1–2 s, measured on the
   live database at cutover (`ls -l dash.sqlite-wal` before and after).
3. Optional, not needed for correctness: an offline `VACUUM` to shrink the
   803 MB file (the freed pages from pruned rows stay on the freelist and are
   reused). Measured 4.6 s on the 780 MB copy with no load. Run it with the
   server stopped (`sqlite3 dash.sqlite 'VACUUM;'`), never while serving.
4. Rollback: redeploy the previous release. No schema change, no data format
   change. Offsets pruned by the new prune would be re-created by the sources
   on their next poll (none are pruned on this copy). The WAL pragmas are
   harmless to an older build.
5. Verify after cutover: `scripts/perf_probe.py` against the live port (GET
   only), `agent_dash.py check --page`, and `ls -l dash.sqlite-wal` once an hour
   (expect ≤ ~16 MB between checkpoints, ≤ 64 MB always).

### Live incidents observed during this stage (for the owner)

The live `serve` process (port 8765) was replaced twice on 2026-11-06: the old
child (pid 74571) returned at 11:21:38 and pid 32715 started 1 s later; pid
32715 returned at 12:00:27 and pid 64748 started 0.4 s later. Both replacements
run from the main checkout path (`…/projects/workflow-skills/tools/agent-dash`).
I did not signal the live process. My probe and perf runs used port 8897 with a
temp state dir, and `tests/perf` runs in-process on a random port. My
`tests/test_server.py` runs also ran around both times. Those tests isolate port
and state dir, so I do not think they caused it, but this is not proven.
`tests/test_deploy.py` and `tests/test_process_control.py` call `stop`, `restart`
and `deploy`. I did not check whether each of those calls isolates the port, and
I did not run either file. The owner should check other sessions and those two
suites before the next run.

## Stage 4 (2026-11-06): poll cadence, content keys, shared state build

Owner report unchanged. Same method as stage 3: a frozen live-shaped copy (one
`sqlite3 .backup` of the live database, copied once to two temp state dirs),
the same probe, Dagster off, `AGENT_DASH_READONLY=1`, each build on its own
port. Base is `cc57a70` (the stage 3 branch base); head is `3c00db3`. Both
builds ran from `git archive` snapshots, not the checkout.

### Verdict

Partial. Items 2 and 4 are landed and met. Items 1 and 3 are landed but short of target. Item 5 is not done. The targets not met:

- Source poll CPU idle: 101% → 78% of one core (target 15%). Not met.
- `/api/ws/progress?ws=all` cold: 12.3 s → 6.3 s on the copy (target under 1 s). Not met.
- First section: 3.0–3.4 s both builds (target 1.5 s, budget 1.5 s). Not met, and not improved.
- Cold `/api/state` 503: reproduced on base, not on head (see item 4). Met.

### Where the CPU goes now (measured, idle window after the probe)

Per source, CPU as a share of one core, 55–60 s window, same copy:

| source | base | head | ms per poll base → head |
|---|---|---|---|
| muse | 27.6% | 15.5% | 585 → 334 |
| cursor | 25.4% | 13.2% | 561 → 285 |
| manifests | 14.4% | 13.4% | 318 → 288 (not changed) |
| pi / commandcode / grok_cli | 8.3 / 6.8 / 6.8% | 8.4 / 7.2 / 6.8% | not changed |
| codex / process | 5.6 / 4.1% | 6.4 / 4.3% | not changed |
| **total sources** | **101.4%** | **77.7%** | |

A fresh head server, 100 s after start and with no probe run, idles at 68%.
An intermediate build (before the `counts_seq` and `ws=all` commits) read 43%
in a quiet 60 s window with no probe run. The gap to 78% was not isolated; the
laptop's load average was 8–12 during these runs. Treat the absolute figures as
±15%.

The remaining cost is not one hot function. Per-tick work that is still
unconditional: discovery (`_discover`, `_attach_labels`, `_transcript`
stats for ~1,200 cursor chats), `_candidate_files` (a `getmtime` per muse file),
the manifest directory walk (its full scan every 15 s, 288 ms average), and the
`ps` listing. Cutting those needs source-by-source cadence (a directory-change
signal, not a re-walk), which this stage did not reach.

### What changed (one commit each; see `git log` on `perf/dash-snappy-4`)

1. **Attribution read once per poll** (`8cb7d9e`). `upsert_run` re-read the
   attribution map and train branches on every call (~2.4k per cursor poll).
   `Store.attribution_scope()` is thread-local and opened per source poll in
   `_run_poll`. Request threads read fresh as before. Cursor 448 → 271 ms,
   muse 434 → 266 ms in the profiler (same copy).
2. **Settled rows skip** (`b9e85c9`, `0eb9b62`, `79cca91`). A finished cursor
   chat or muse manifest row whose inputs are unchanged is written at most once
   per 120 s (`SettledRows`). Live rows (cursor pid, muse `running`) are never
   settled, so they write every tick. Muse rows with no evidence (791 of 809
   per poll) were re-evaluated every tick; they settle now too. The window was
   30 s first, which re-ran ~2k rows every few ticks; 120 s is the floor.
3. **Muse signature and stat skip** (`d342dfa`). The process signature used
   `started_at` from `ps` elapsed time, which moved by a second per listing, so
   every muse row re-ran each tick (~1,660 misses). Signature is now pid,
   command and label. A session file at end-of-file is skipped until its
   (inode, size, mtime) changes.
4. **Cursor freshness memo** (`59a0ea9`). One offset query per labelled chat per
   tick was skipped while the chat's (inode, size, cursor) stamp is unchanged.
5. **Content key for caches** (`f512195`, `1c6bbb5`). `/api/state`, the
   wsprogress cache and the burn cache keyed on `store.seq`, which every cursor
   advance and prune moves. They key on `Store.content_key` =
   `(data_seq, sample_seq, counts_seq)`. `sample_seq` is new (process samples are
   liveness evidence the state body reads). `counts_seq` moves on `ws_counts`
   writes: the state body names boards from them, but the snapshot's data
   version must not move (test_server pins it). `seq` itself is unchanged: the
   stale badge and the SSE tick need it.
   `/api/project/burnup` warm median 395 → 2.8 ms.
6. **`ws=all` one store pass for replays** (`3c00db3`). `build_project_progress`
   ran `query_runs` once per workstream (30 full passes). The replay query is
   identical across workstreams (same moment, no filter, same limit), so it runs
   once. Equivalence: `scripts/wsprogress_equiv.py --db <copy> --old-root <base>`
   reports 31 of 31 outputs identical on the copy. Cold `ws=all` on the copy:
   12.3 s → 6.3 s. Not under 1 s: the runs-started-per-hour query is still per
   workstream. Sharing it changes WS2 and WS8, because the store attributes some
   inherited runs by its own filter, so the shortcut is not exact. Next step:
   one grouped store query that returns the attribution with each row.
7. **Cold state 503** (`7686dbf`). See item 4 below.

### Item 4: cold `/api/state` 503 (cause verified)

Cause, from the code and a repro (`repro503`: fresh 5x seed per trial, `warm()`,
cold GET immediately): the warm thread called `build_state` outside the shared
build gate, so a cold request started a second full build. The two contended
for the store lock; the loser's 2 s `STATE_STORE_LOCK_TIMEOUT_S` wait raised, and
the request got 503. Base: the cold request returned 503 after 2.34 s on trial 0
of 4 (the lock timeout). Head: 0 of 8 cold trials 503; 4.0–4.6 s each.

Fixes: warm runs through the same gate (a request joins the one build), and
`STATE_BUILD_TIMEOUT_S` is 10 s, not 5 s. The 5 s budget was below the 8 s cold
5x endpoint budget in `perf_budgets.json`, so a legitimate 5–8 s build also 503ed.
`test_state_build_budget_applies_when_response_cache_is_disabled` stays as it
was; it patches the budget and still passes. No budget in `perf_budgets.json`
was changed.

Full-file `ADASH_PERF=1 tests/perf/test_perf_budget.py`, HEAD: 6 of 6 in three
consecutive runs (107 s, 118 s, 136 s). Base: 6 of 6 once (112 s). The 503 in the
docs (3 of 4 full-file runs) did not reproduce in these four runs.

### Item 2: why `test_view_seq` pins what it pins

`set_offset` bumps `_seq` on every real cursor advance because the stale badge
and the SSE tick use `seq` to tell live from dead. `test_offset_write_moves_seq_only`
pins that, and it still holds. The response caches were the problem, not
`seq`. The pin is unchanged.

### Item 5: first paint (not met)

`page_check.mjs --perf`, first section drawn, budget 1500 ms, fresh server, 120 s
warm-up, same copy, same ports:

| build | first section, runs |
|---|---|
| base (`cc57a70`) | 1.8 s (first run); 3.0 s; 3.1 s |
| head (`3c00db3`) | 3.4 s (load average 8); 7.3 s (load average 12); 7.6 s with the gated warm call reverted (load 12) |

Bisect of the head-side commits at a lighter load: `8cb7d9e` 3.6 s, `b9e85c9` 4.1 s,
`f512195` 3.2 s, `d342dfa` 2.3 s, `59a0ea9` 1.9 s (and a 13.5 s outlier on its first run).
The 7–8 s head runs were at load average ~12; the later head run at load ~8 gave
3.4 s. So the regression is not established, and I did not find a commit that
causes one. The first section is still ~2–3× the budget on both builds, and I
did not profile the first draw itself.

The `state.wssAll` render signature (`projSig` in `static/core/api.js`) was not
checked for extra re-renders, and `/api/state` (854 KB) and `/api/runs` (831 KB)
were not cut. Both remain open.

### Not done, and why

- Source CPU target 15%: manifests and cursor discovery are per-tick walks (see
  "Where the CPU goes"). Needs cadence or change signals per source.
- `ws=all` under 1 s: the per-workstream runs-per-hour query (see item 3).
- First section under 1.5 s and idle re-render count: not profiled (item 5).
- `/api/state` and `/api/runs` payload size: not investigated.
- `file_offsets` blob markers: still no pruning rule (stage 3, unchanged).

### Probe, same copy, base vs head (cold / median / p95 ms)

| endpoint | base | head |
|---|---|---|
| /api/health | 1407 / 19.7 / 24.9 | 1569 / 15.0 / 22.7 |
| /api/state | 1848 / 3.0 / 3.3 | 1638 / 3.1 / 3.4 |
| /api/snapshot | 1084 / 1.1 / 1.3 | 1986 / 1.7 / 2.3 |
| /api/workstreams | 1836 / 13.7 / 27.5 | 1895 / 11.5 / 30.4 |
| /api/project/phases | 2200 / 1.0 / 15.6 | 2287 / 0.5 / 0.7 |
| /api/project/burnup | 453 / 394.7 / 646.1 | 366 / 2.8 / 409.8 |
| /api/runs | 767 / 4.0 / 4.1 | 1015 / 3.7 / 3.9 |
| /api/sessions | 865 / 624 / 644 | 833 / 606 / 1005 |
| /api/doctor | 363 / 417 / 725 | 362 / 281 / 421 |
| /api/ws/progress?ws=all | 23193 / 2.3 / 2.4 | 17814 / 1.9 / 2.4 |
| /api/project/graph | 2324 / 0.8 / 0.8 | 2248 / 0.7 / 0.9 |

Server CPU, 60 s page-like window (probe `cpu_pct_of_core`): base 117.6%, head
113.6%. The page-like load (requests on top of the pollers) is the dominant
part of that figure as well, so the window is not a source-only number.

Token totals, run counts and cost: NOT verified on this stage. The probe
compares response sizes only (`/api/usage` 451,963 bytes on both builds;
`/api/runs` 831,091 vs 831,287 bytes, which moves with live running rows). The
stage 3 offline check (791,182 usage events, 15,684.11 USD) was not repeated, so
the prompt's "confirm the totals match" condition is open.

### Cutover

No schema change, no data migration. Python changes need a `serve` restart
(`static/` is live on reload). The new content key and settle memos are in
memory and rebuild on the first poll. Rollback: redeploy the previous release.
Verify after cutover: `scripts/perf_probe.py` against the live port (GET only),
and the source CPU share from `/api/health` once the process has run 2 minutes.

## Stage 6 (2026-11-06): first paint, stale-while-revalidate reads, one fix

Scope: `dash/respcache.py` (SWR), its tests, CONTRACT.md. Not done here: the
page's first-draw gate, payload shrink, keep-alive (see "Not done").

### What changed (commits on perf/first-paint)

- `respcache`: a reader gets the previous generation at once once any exists;
  one background refresh per key; at most 4 refreshes at once; only a
  first-ever request, or an entry idle for 30 s, computes inline.
- Fixed a self-deadlock in the WIP: `bg_skipped` was recorded while holding the
  non-reentrant lock (`tests/test_ttl.py` hung on the capped-refresh test).
- Sliding `since` roll: a new 30 s cell adopts the previous cell's generation,
  so the roll no longer waits (the 1 in ~7 reads that were 1.4-2.4 s).
- Compute-time stats per endpoint in `/api/health` `perf.ttl`
  (`compute_last_ms`, `compute_max_ms`), and served-age stats were already there.

### Reads every 4 s under live writes (`cadence.py`, 90 s, same writer rate)

Writers: one `Store` upsert and usage row per 1.5 s per copy. Both copies from
the same `sqlite3 .backup`, both builds on their own port. Load average 7-9.

| endpoint | base p50 / p95 ms | head p50 / p95 ms | bytes |
|---|---|---|---|
| `/api/snapshot?usage_group=model` | 778 / 1510 | 18.6 / 796 | 105 KB / 98 KB |
| `/api/workstreams` | 1254 / 2593 | 71.7 / 1424 | 21 KB |
| `/api/project/graph` | 1763 / 3087 | 1.0 / 1.6 | 64 KB |
| `/api/state` | 1368 / 2094 | 3.6 / 15.7 | 711 KB |

A second base run gave 612 / 1646, 832 / 2244, 2173 / 4005, 1757 / 2085 ms.
Head p50s: graph 1 ms, state 4 ms, snapshot 13-19 ms, workstreams 40-72 ms
(single-digit ms for snapshot in the quieter first run). p95 is not
single-digit: the tail is compute time under GIL contention, not waits (see below).

### Freshness bound, measured (`/api/health` `perf.ttl`)

| endpoint | compute last / max ms | max served age ms | over 5 s |
|---|---|---|---|
| snapshot | 273-3235 / 7501 | 11143 | 6 |
| workstreams | 1383-3857 / 9645 | 12214 | 11 |
| project/graph | 2385-5194 / 11934 | 13223 | 11 |
| state | 1281-1788 / 4693 | 5906 | 2 |
| ws/progress?ws=all | 23063 / 28649 | 489777 | 2 |

So the bound is TTL + compute + one tick, not TTL + one tick. The 5 s rule holds
only while a heavy compute finishes in ~3.5 s. Not met under this load.

### First draw (`timeline.mjs`, Chrome CDP, fetch and parse hooks)

| build, state | first `[data-sec]` ms |
|---|---|
| base, writers on | 4134, 5812 |
| head, writers on | 5304, 5610 |
| base, writers off, after pause | 6554, 1228 |
| head, writers off, after pause | 7536, 8169 |
| base, `page_check --perf`, after pause | 7708 |
| head, `page_check --perf`, warm (right after a settle run) | 231 |

Timeline finding (head, after pause): nothing draws until the `Promise.all` in
`refreshAll` (snapshot, runs, concurrency, nav, approvals) and the nav
requests have all returned. Their responses are queued behind each other on
Chrome's six connections, one of which the SSE stream holds. The page waits for
the slowest, and after a pause that is a cold compute per key (idle rule), with
`/api/project` (nav) at 4.8 s, `/api/workstreams` at 7.5 s and `/api/snapshot`
at 4.5 s in the same run. Server-side warm hits are 1-70 ms, so the first draw
is compute, queueing and machine load, not payload alone. Head and base first
draws are not distinguishable at this load (machine load 7-9 on 15 cores,
other agents' workers running): I did not claim a first-draw win.

### Idle re-renders (writers off)

`page_check.mjs --perf`, head, warm: 2 idle re-renders per 30 s (budget under 3,
pass). `page_settle_check.mjs` reports "never settled" for the exec view on both
builds; that is not from this change (same on base).

### Ranked causes still open (measured)

1. `/api/ws/progress?ws=all`: 23-29 s compute, refreshed in the background on
   every read past its TTL. It holds the GIL most of the time, so every other
   read's tail is queued behind it. Fix: a set-based runs-started-per-hour query
   and an incremental build. Largest single item left.
2. Cold computes after a pause (idle rule): 1-8 s per key on this laptop. Fix:
   cheaper computes (the state and workstreams paths rebuild per call).
3. Payloads the first draw waits on: `/api/runs?limit=5000` 472 KB,
   `/api/approvals?ws=all` 272 KB, `/api/concurrency` 254 KB, `/api/state` 711 KB.
   Not cut in this stage.
4. Page gate: `refreshAll` draws only after all six requests (see timeline).
   Progressive draw needs per-section data guards and was not done.

### Not done, and why

- Shell first and per-section progressive draw: needs placeholders for
  sections whose data is still in flight, a look-visible change; deferred.
- HTTP/1.1 keep-alive: the server is `protocol_version = "HTTP/1.0"` with
  `ThreadingHTTPServer`, so every request is a new connection. Keep-alive needs
  Content-Length on every response path (including 304, errors and the
  `/api/stream` SSE, which must close), plus per-connection timeouts for idle
  sockets held by threaded handlers. It is a real gain for the six-connection
  queue, but the timeline shows the queue is dominated by compute time. Left for
  a follow-up with a test per path.
- Payload split (`limit`/`fields` and details endpoint): the first view's fields
  are not yet mapped to consumers, so no cut was made without a regression check.
- `ws/progress` rewrite: item 1 above.

### Targets

| target | result |
|---|---|
| first section under 1 s | not met after a pause (6-8 s); 231 ms warm |
| page settle under 2 s | not measured as a pass (settle check "never settled" on both) |
| under 3 idle re-renders per 30 s | met (2) on head, warm |
| clicks within 100 ms | not measured in this stage |
| `/api/state`, `/api/health` budgets | `ADASH_PERF=1 tests/perf/test_perf_budget.py`: 6 passed |

Verification: `tests/test_ttl.py` (19), `test_page_rules.py`, `test_privacy_pin.py`,
`test_server.py`, `test_etag.py` (289 passed with `test_ttl`, under memcap 4096);
`scripts/page_check.mjs --url <head>` (desktop and phone pins, one tile matches
the API); `ADASH_PERF=1` perf budgets (6 passed).

## Stage 7 (2026-11-06): bounded background refresh, shared ws=all pass, idle serve

Scope: `dash/respcache.py`, `dash/store/runs.py` (`query_runs` split),
`dash/wsprogress.py`, tests (`test_ttl.py`), CONTRACT.md. Budgets unchanged.
Commits on `perf/first-paint-2`: refresh period, ws=all pass, idle serve.

### What changed

- **Refresh period scales with compute.** A served generation is due a
  background refresh after `max(ttl, min(10 x last compute, ceiling))`. Ceiling
  5 s for live endpoints (the product rule wins over the 10% share, see below)
  and 60 s for `ws/progress`. A global budget caps non-live background compute
  at 30 s per rolling 60 s.
- **`/api/ws/progress?ws=all`: one shared run preparation.** Before, the
  project total called `query_runs` once per workstream (30 full passes, 7.8 s
  of a 9.8 s profile). `query_runs` is now `_runs_prep` (liveness, usage fold,
  open evidence, parent map: ws-independent, once) plus `_runs_select` (the
  per-filter rows, the same code as before). `query_runs_many` returns one list
  per filter. The per-workstream attribution (`_in_ws`) and the collapse and
  launch-echo steps still run per filter, so WS2 and WS8 inherited runs come
  out as before.
- **Idle keys serve their recent generation.** An idle key (no reader for
  `max(30 s, 10 x compute)`) serves its last generation with its visible age and
  starts one refresh, while the generation is at most 120 s old. Older than
  that, its reader computes inline.

### Equivalence (`scripts/wsprogress_equiv.py`, backup copy, same `now`)

31 keys (30 workstreams and the `ws=all` total): identical JSON, old (origin/main)
vs new. `ws=all` cold: 6.8 s -> 1.0 s on the copy (idle machine, no profiler).

### Before and after (same copy, `perf_probe.py --n 10 --cpu-window 40`, load 9-11)

| measure | origin/main | this stage |
|---|---|---|
| `/api/ws/progress?ws=all` cold | 21 540 ms | 2 387 ms (warm med 2.2 / 2.4 ms) |
| `/api/state` cold | 2 827 ms | 1 640 ms (warm med 2.9 / 9.4 ms) |
| `/api/workstreams` cold | 1 953 ms | 1 307 ms (warm med 13.2 / 4.0 ms) |
| `/api/snapshot` warm p95 | 1.7 ms | 11.8 ms (median 1.3 / 2.8 ms) |
| tick p95 under page-like load, snapshot | 1 809 ms | 33 ms |
| tick p95 under page-like load, workstreams | 2 729 ms | 93 ms |
| tick p95 under page-like load, runs | 1 209 ms | 6 ms |
| server CPU, 40 s page-like window | 105% of a core | 76% of a core |
| `page_check --perf` first section (one sample each) | 5 302 ms | 2 138 ms |

The snapshot warm p95 rose: a refresh now lands on about 1 in 5 warm reads
(period 5 s, compute 1-1.8 s), where before it was a stale serve on 1.5 s
ttl. It is the cost of the 5 s bound, not a regression in tick latency.

### Freshness bound (per endpoint, `/api/health` `perf.ttl` on head)

| endpoint | ceiling | bound: period + compute | compute last / max ms |
|---|---|---|---|
| snapshot, runs, concurrency, state, workstreams, graph, reconcile | 5 s | 5 s + compute | 1 115 / 1 115 (snapshot) |
| ws/progress?ws=all | 60 s | min(10 x compute, 60 s) + compute | 2 381 / 2 381 |

`over_max` counts on head (7-8 per live key) come from idle reads after the
probe's pauses: those serves carry their visible age (up to 20 s) by design.
Under a steady reader they stay within the bound.

### Steady-state CPU: what remains

With a page-like reader, live keys refresh every 5 s: snapshot (1.1 s compute),
workstreams (1.8 s), runs (0.4 s), about 74% of a core over five minutes. The
10% share would need compute under 0.5 s per live key at a 5 s period. The
product rule keeps live status at 5 s, so the remaining CPU is live compute,
not background waste. Cutting it is a compute change (workstreams and state
rebuild per call), not a scheduling one.

### First draw, idle re-renders, clicks

- First section after a pause: 2.1 s on head (one sample, load 11), not under
  1 s. Measured causes: the draw waits on `refreshAll`'s `Promise.all`, which
  includes `loadNav()` (four requests, `/api/workstreams` cold 1.3 s), and on the
  snapshot, runs, concurrency and approvals fetches, each on the six Chrome
  connections the SSE stream shares.
- Idle re-renders: 13 per 30 s on head in one sample, but the copy's pollers
  were writing: `view_seq` changed 7 times (base) and 10 times (head) in 30 s.
  The idle budget needs a copy with the pollers off. Not measured as a pass.
- Clicks: not measured; the click path is unchanged.

### Shell and progressive draw (not done)

`refreshAll` still draws once, after every request in its `Promise.all`. A
per-section draw needs loading states in about eight card files (tiles, activity,
planned, production-log, plan-progress, tables and the ws cards), each reading
`runs`, `conc`, `approvals` or `plan` directly. Taking `loadNav()` out of the
gate also needs a redraw when workstreams land, plus a check that the landing
flip still holds. Both are look-visible and were not validated in this pass.

### Startup warm (partly in place)

`server.py` already runs a `dash-warm` thread at startup that precomputes the
default all-scope snapshot, the 24 h windows and both workstream scopes into the
store caches. No new keys were added: the page's since-cell and group keys
depend on client state, so a warm of keys it may not ask for is wasted CPU.

### Keep-alive (measured, left out)

The server is HTTP/1.0. A tiny endpoint (`/api/project`) answers in a 0.5 ms
median including the new connection, so connection setup is a fraction of a
millisecond per request. The first-draw cost is compute and queueing, not
connect cost. Keep-alive would need Content-Length on every path plus idle
timeouts on threaded handlers, for no measured gain. Not implemented.

### Payload trimming (not done)

Sizes on the copy: `/api/state` 606 KB, `/api/runs?limit=5000` 830 KB,
`/api/concurrency` 279 KB, `/api/sessions` 2.6 MB (not on the first view).
No field-to-consumer map was made, so no field was removed.

### Verification

- `test_ttl` (25, including the new period, budget and idle-serve tests);
  `test_page_rules`, `test_privacy_pin`, `test_server`, `test_etag`,
  `test_wsprogress`, `test_planning_phase` (326 together, one process, memcap 4096).
- Gate-selected suites for `dash/store/runs.py` (run directly, the gate stopped on
  its 12-suite width): `test_doctor`, `test_migration`,
  `test_no_cold_fold_on_request_path`, `test_orchestrate`, `test_perfmon`,
  `test_poll_budgets`, `test_rollup`, `test_state`, `test_store`, `test_view_seq`,
  plus the two above: 455 tests, OK.
- `ADASH_PERF=1 tests/perf/test_perf_budget.py`: 6 passed. No budget changed.
- `scripts/page_check.mjs --url <head>`: both views, desktop and phone pins, tiles
  and legends match the API.

### Targets

| target | result |
|---|---|
| first section under 1 s after a pause | not met: 2.1 s (one sample) |
| page settle under 2 s | not measured |
| under 3 idle re-renders per 30 s | not measured as a pass (copy churn) |
| clicks under 100 ms | not measured |
| `ws=all` under 2 s cold | met on the copy (1.0 s); 2.4 s through the loaded server |
| a 25 s compute does not re-run every few seconds | met: `ws/progress` period is 10 x compute, capped at 60 s |
| served age bound stated per endpoint | done (above) |
