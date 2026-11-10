PASS

Root cause: the pi source never read the stream for usage. It relied on
`launch_pi.py`'s `generic.jsonl` lines, written only after the child exits,
so a launcher that dies first leaves the run tokenless. The split layout
made it worse: `run_worker.py` writes the manifest in the run folder while
the launcher writes the stream in `<run_dir>/pi/`, so the manifest row's
pad had no stream.

Fix (worktree `dash/pi-deepseek-cost`):
- `dash/sources/pi.py:526` `_apply_stream` reads `message_end` usage from
  the stream, byte-cursored per file (`grok_cli._read_chunk`), and emits
  `store.add_usage` rows. `_stream_file` (`:356`) and `_first_file`
  (`:517`) fall back to `<pad>/pi/` for stream, out and err. A per-stream
  index key (`:584`) keeps `message_id` = `<session>-<n>` across chunks,
  matching the launcher's full-stream ids, so the two readers upsert one
  row instead of double counting.
- `catalog/prices.json`: four Morph rows (`morph/morph-dsv41flash-1017`,
  `morph/morph-dsv41flash`, and the bare ids pi reports), provider
  `morph`, published rates from https://morphllm.com/pricing (retrieved
  2026-11-10): input 0.12, cache_read 0.008, output 0.80 USD/1M.
- Family: one predicate `dash/models.py:55` `is_deepseek_alias`
  (`dsv41flash`), reused by `model_key`/`model_name`, `pricing.series_key`
  (`:95`), `taskmatch.run_family` (`:292`), `store/runs._family_tier`
  (`:1064`) and `pi._model_tier` (`:416`); `static/models.js` mirrors it.
  `morph-dsv41flash*` reads family `deepseek`, key `deepseek-flash`,
  display `DeepSeek V4.1 Flash`; tier `pi-deepseek-flash` groups as DeepSeek.

Backfill: the stream cursor starts at offset 0 on the first poll after
deploy, so finished runs re-read their whole stream once. Verified on live
data: `ws33-review-foldexternal-fix3` -> 97 rows, 101,466 in / 16,003 out /
3,863,552 cache_read, $0.0559, 0 null costs; `ws33-ghosts-verify` (21 MB,
6 chunks) -> 812 rows, ids 1..812, $0.3347, matching its 812
`generic.jsonl` usage lines exactly.

Tests (`cd tools/agent-dash`, explicit files, all OK): `test_pi.py` 88,
`test_deepseek.py` 32, `test_pricing.py` 32, `test_models.py` 16,
`test_store.py` 159 (1 skip), `test_server.py` 109, `test_generic.py` 15,
`test_taskmatch.py` 20, `test_projectfeed.py` 11, `test_rollup.py` 7,
`test_poll_budgets.py` 25, `test_phases.py` 41, `test_labels.py` 23,
`test_etag.py` 2, `test_fixtures.py` 23, `test_view_seq.py` 12,
`test_state.py` 18, `test_privacy_pin.py` 6, `test_doctor.py` 136,
`test_migration.py` 3, `test_orchestrate.py` 40, `test_perfmon.py` 21,
`test_aggregate_rules.py` 5, `test_page_rules.py` 153,
`test_attribution.py` 28, `test_incremental_chunk_replay.py` 4,
`test_launch_cmd.py` 15. `node scripts/model_names_check.mjs` PASS,
`exec_layout_check.mjs` PASS, `tables_check.mjs` PASS (one flake, clean on
re-run). `test_no_cold_fold_on_request_path.py` fails on
`_concurrency_base -> _all_runs`, a caller I did not touch; pre-existing.

Reload (hub, after merge): `python3 ~/.claude/tools/agent-dash/agent_dash.py
deploy --ref origin/main --yes-live`, then `... agent_dash.py reprice` (the
catalog signature also auto-reprices NULL-cost rows).
Check: `curl -s 'http://127.0.0.1:8765/api/runs?ws=WS33' | python3 -c
"import json,sys; rs=json.load(sys.stdin)['runs']; print([(r['run_id'],
r['tokens']['output'],r['cost_usd'],r['model']) for r in rs if
r['source']=='pi' and r['tokens']['output']>0 and r['cost_usd']>0][:3])"`
Expected: a pi run with output > 0, cost > 0, model `morph/...` (family
deepseek).

Not done: the pre-existing duplicate pi run (run_worker's pre-generated
session id vs the stream's) is not merged; the run-folder row keeps the
tokens and the stream-id twin stays at 0. No push/PR (hub does that).
