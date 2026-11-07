# agent-dash: stale-while-revalidate for heavy reads, one fix on the first-paint path

## Summary

Heavy read endpoints (snapshot, workstreams, project graph, state and others)
recomputed inline on the first request after each 1.5 s TTL, so a page polling
every few seconds always paid a full recompute. This change serves the previous
generation at once and runs one background refresh per key.

- Once any generation exists, every reader gets it immediately. One background
  refresh per key, never stacked, at most 4 at once across keys.
- Only a first-ever request, or an entry no reader has touched for 30 s, waits.
- A sliding `since` that rolls onto the next 30 s cell adopts the previous
  cell's generation, so the roll does not wait.
- POST actions still invalidate, so a click shows at once.

## Measured (live-shaped copy, writers on, reads every 4 s, 90 s)

| endpoint | before p50 / p95 ms | after p50 / p95 ms |
|---|---|---|
| snapshot | 778 / 1510 | 19 / 796 |
| workstreams | 1254 / 2593 | 72 / 1424 |
| project graph | 1763 / 3087 | 1 / 2 |
| state | 1368 / 2094 | 4 / 16 |

The freshness bound is the TTL plus compute time plus one page tick. Under
heavy load it can exceed 5 s; CONTRACT.md states the measured bound.

## Not in this PR

- Progressive first draw and payload trimming.
- A faster `ws/progress?ws=all` (a 24-29 s background compute).
- HTTP/1.1 keep-alive.

## Test plan

- `python3 -m pytest tests/test_ttl.py` (19 pass; includes stale-serve without
  waiting, idle inline compute, background cap, window roll).
- `python3 -m pytest tests/test_page_rules.py tests/test_privacy_pin.py tests/test_server.py tests/test_etag.py`.
- `ADASH_PERF=1 python3 -m pytest tests/perf/test_perf_budget.py`.
- `scripts/page_check.mjs --url <trial server>`: desktop and phone pins; tiles match the API.
