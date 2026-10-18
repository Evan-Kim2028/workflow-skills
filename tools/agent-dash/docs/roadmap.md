# Dashboard roadmap — open work after the robustness + agent-friendly landings

Living plan for agents picking up dashboard work. Each open item is one
worker diff: goal, files touched, acceptance commands, dependencies, and
which items can run beside it. Proof pointers (`file:line`) below were
checked against this branch; re-check them if main has moved.

Related: the [proposed v2 design spec](design-v2.md) (owner approval
pending), the [integration guide](integrate.md), the
[orchestration principles](orchestration.md), and
[CONTRACT.md](../CONTRACT.md).

Conventions for every item: run only the listed explicit test files or
commands, never a bare suite, never GitHub Actions. Behavior oracles:
`python3 agent_dash.py check`, `check --page`,
`python3 -m unittest discover -s tests -p "test_page_rules.py"`.
Gates run from `tools/agent-dash/`.

## What already landed (one line each, with proof)

Robustness audit items 1–3 and 5–11 are done on main:

- Per-source watchdog + lag: a hung source no longer stalls the rest; `health` carries `last_duration_s` and `lag_s` (`dash/sources/__init__.py:48,125,193`, `Registry` at `:56`).
- Bounded scans: tick budgets with skipped-by-budget counts (`dash/sources/cursor.py:306-324`).
- Offset pruning + backup rotation: `Store.prune_offsets` (`dash/store.py:236`).
- Snapshot cost: usage computed once per snapshot, not once per scope (`dash/store.py:1339,1437,1462`, cached via `_usage_by_run` at `:2730`).
- Snapshot cache between writes + freshness surfacing: `snapshot_cached` (`dash/store.py:1390`) served per tick with `last_data_ts`/`last_poll` (`dash/server.py:293,459`); page shows a `stale` badge (`static/index.html:339,348`) and a request-failed banner (`static/index.html:727-736`).
- Unpriced-model alert: `/api/health` lists `unpriced_models` (`dash/server.py:141-144`); missing catalog rows priced.
- Truncation surfaced: finished-section subtitle reads "Newest N of M" from `finished_in_range` (`static/index.html:2281-2290`).
- Disconnects silenced and counted: `disconnects_1h` on `/api/health` (`dash/server.py:79,140`), no tracebacks.
- Plan-write locking: per-bundle `fcntl` lock across read-modify-write (`dash/bundles.py:383`; documented in `CONTRACT.md:348`).
- Validate on read: `/api/plan` and `/api/bundle` attach `issues` instead of drawing bad states as tasks (`dash/bundles.py:264,340`, `validate_bundle` at `:1050`).
- Unseen-worker check + safe restart: the reconcile check flags live-but-invisible workers (`dash/store.py:1553`); `restart`/`stop` wait for real exit so the next `run` can bind (`agent_dash.py:286,361`; see "Install and run" in `integrate.md`).
- `process_samples(run_id)` index: `idx_samples_run` (`dash/store.py:2231`).

Agent-friendly CLI items are done on main:

- One-call state: `GET /api/state` (`dash/server.py:151`, handler `_state` at `:174`) via `state` (`agent_dash.py:441`).
- Always-on doctor with history: `DoctorScheduler` runs the checks in a background thread (`dash/doctor.py:1474`, `run_once` at `:1507`) and persists each run in the `doctor_problems` table (`dash/store.py:2199`, writes at `:2428-2495`). `GET /api/doctor` serves the latest run and never checks inline (`dash/server.py:149,198`).
- Doctor CLI: `doctor` (`agent_dash.py:507`), `doctor --fix` dry run and `--yes` apply of the safe fixes (`agent_dash.py:54,632,657`), `doctor report` digest (`agent_dash.py:55`, `dash/doctor.py:1903`).
- Doctor in the launch path: `run_worker.py` runs a preflight before launching, unless `--no-preflight` (`scripts/run_worker.py:65,154`); `workflows/ws-wave.js` runs a doctor gate that stops the wave on a critical problem (`workflows/ws-wave.js:60-73`).
- Problem pages: one page per problem code under `docs/problems/` (32 files).
- `--json` on `check`, `check --page` (summary), `state`, `status`, `doctor`, `ws validate`, `ws approvals`, `ws trains`, `ws plan` (usage lines `agent_dash.py:49-51`; schema in `integrate.md` "For agents").
- Exit-code norm 0/1/2/3/4/5 plus messages that name the mistake, the valid values, and the next command (`dash/errors.py:1-113`; table in `integrate.md` "For agents").

Orchestration tooling is done on main:

- Pure helpers: `ready_queue` (`dash/orchestrate.py:176`), `critical_path` (`:287`), `waves` and `max_parallel` (`:387,424`), `overlaps` (`:363`), `stale_gates` (`:453`), `deferred_ledger` (`:498`).
- `ws next|waves|overlap|gates|deferred` (usage `dash/bundles.py:68-72`, dispatch `:1939-1988`).
- `agent_dash.py lead-profile` (usage `agent_dash.py:67`, `cmd_lead_profile` at `:757`).
- `/api/plan` carries `ready`, `critical_path`, and `max_parallel` (`dash/server.py:49` calls `orchestrate.plan_extra`, defined at `dash/orchestrate.py:430`; cache note at `dash/server.py:34`).
- Workstream-orchestration skill: `skills/workstream-orchestration/SKILL.md` (principles in [orchestration.md](orchestration.md)).

Run and attribution items are done on main:

- Run aliases: a merged cursor row stays merged across restarts. `run_aliases` table (`dash/store.py:2194`), the merge writes alias to target (`:1150`), lookups follow the alias (`:1047-1053`), and existing databases gain the table on open (`:2255`).
- Production jobs are a count, never a list, in the agent areas: `is_prod_job_run` (`dash/store.py:3262`) keeps them out of the running and recent lists (`:1323,1331,1811`); `counts.prod_jobs_finished` (`dash/store.py:1690-1720`); the page shows the counts (`static/index.html:766-767`).
- Generic reporting path: `agent_dash.py report` (`agent_dash.py:250` dispatch, `cmd_report` at `:937`) appends to `generic.jsonl`, read by the generic source (`dash/sources/generic.py:31,34`).
- Workflow workers are visible wherever their run folder lives: registered run folders (`dash/rundirs.py:8` `run-dirs.txt`, `registered_run_dirs` at `:66`) are scanned with the configured roots (`:107-120`), and `/api/health` reports `scratch_roots`, `run_dirs`, and `warnings` (`dash/rundirs.py:157`; test `tests/test_server.py:470`).
- Source tests no longer read the machine's run-folder registry: `_registry_configured` reads it only when a state dir is named (`dash/rundirs.py:142-155`).

Page items are done on main:

- One fixed rolling 24 h window; no range selector (`static/index.html:521,723`; `docs/concepts.md:142`; `DESIGN.md:9`).
- Tab filter All | Active | Completed and drag-to-reorder, remembered per viewer: mode state in `static/tabs.js:6,142`; the filter group and drag handlers in `static/index.html:615-672`.
- Replay on the progress map: `GET /api/replay` (`dash/server.py:124`, handler `_replay` at `:373`) builds from `dash/replay.py:189` (`build_replay`); the Replay button (`static/index.html:1466-1468`); the timeline uses the shared warp axis (`static/index.html:1502`, pinned by `tests/test_page_rules.py:392-394`). Its history is the bundle's `history.jsonl`, written by `ws task` (`dash/bundles.py:1186,1210`) and read by `dash/replay.py:110`.
- No doctor, health, or warning cards on the page; doctor is CLI and API only (`DESIGN.md:57`; pinned by `tests/test_page_rules.py:525-533`).

Module split S-store is done: `dash/store.py` (~3.7k lines) is now the package `dash/store/` behind the same public API (`Store` facade at `dash/store/__init__.py:196`, mixins `base` 634, `runs` 712, `usage` 553, `running` 498, `summarize` 706, `queries` 327, `util` 353 lines). The facade holds the single lock; concern modules expose mixin methods plus `_locked`-style functions that assume it and never take their own (`snapshot` at `dash/store/summarize.py:27`, `reconcile_numbers` at `:171`, `prune_offsets` at `dash/store/base.py:101`). Proof: all 90 `Store` methods and every module-level name re-exported unchanged (`from dash.store import X` works everywhere, no test changed); 136 functions verified byte-identical modulo one indent level; interleaved ref-vs-new capture over a frozen live-DB copy is identical after wall-clock normalization on snapshot (all + WS7), reconcile, state (except `version.tool`, a checkout artifact), runs (limit 500), usage, workstreams, replay WS1, and doctor, with concurrency inside same-code drift; 502 tests pass across the 18 listed files with no count change, and `python3 agent_dash.py check --url` returns the same mismatches and exit code on both trees.

## Phase 1 — module split (lands before the visual pass)

The page is one large inline script and `dash/store.py` is one
~3.7k-line file. Split both behind stable APIs first, moving code
verbatim with zero behavior change, so later visual and store work has
small files with one writer each. No DOM-id, `data-*`, API-shape, or
lock-semantics change in this phase.

- **F-core.** Extract `static/core/` (`el`, `state`, `api`, `cache`, `charts`, `drawer`). Touch: `static/index.html` (script tags + delete moved fns), new `static/core/*.js`. Accept: page loads; `python3 -m unittest discover -s tests -p "test_page_rules.py"`.
- **F-cards.** One file per card under `static/cards/` (`tiles`, `token-chart`, `live-chart`, `timeline`, `tables`, `ws-cards`, `plan-progress`, `needs`) plus `static/cards/registry.js` (ordered card list; adding a card is one file plus one line). Touch: `static/index.html`, new card files. Accept: page-rules tests + `python3 agent_dash.py check --page`. Note: keep two-space `function` indent so the test splitter keeps matching, or extend the splitter in the same step.
- **S-store (LANDED).** `dash/store.py` became the facade package `dash/store/` (`__init__.py:196`); see the landed entry under "What already landed".
- **D-own.** Update the `CONTRACT.md` ownership table per file (page shell, `static/core/*.js`, each card file, each store concern) once F and S land. Reviewer diff only.

Dependencies: F-core before F-cards. After: none. Parallel: F-core and S-store run beside each other (disjoint trees: `static/` vs `dash/`); F-cards parallel after F-core; D-own last, alone.

## Phase 2 — correctness (ordered by value)

- **Startup grace.** Landed: a source that never completed a poll (and recorded no failure) reports `starting`, not stalled, until max(3x its interval, 90 s) after process start; `health.ok` stays true while starting and neither reconcile nor doctor flags it. Proof: `dash/sources/__init__.py:37` (`START_GRACE_S`), `:251` (`source_state`), `:237` (`has_completed_poll`, also used by the doctor scheduler at `dash/doctor.py:1605`), `tests/test_poll_budgets.py` (`RestartGraceTests`: slow first scan is starting-then-ok, grace boundaries, first-poll error still `error`), `tests/test_doctor.py` (starting source emits no `source-stalled`). Note: `dash/liveness.py:49` (`stale_grace_s`) is the dead-pid grace, not a startup grace; it does not cover this. After: none. Parallel: yes.
- **Workstream inheritance for leads and helpers.** Goal: a lead session and its helper runs get the lead's workstream (inferred from the worktree path or bundle writes) instead of landing unassigned. Touch: `dash/attribution.py`, one source or matcher, `tests/test_attribution.py`. Accept: synthetic lead + helper fixtures attribute to the lead's workstream; existing label-regex behavior unchanged. After: none. Parallel: yes.
- **Retention + checkpoint policy.** Goal: bound `runs`, `usage_events`, `usage_buckets`, `sessions`, and the seen-set beyond N days (default 90); checkpoint on idle; document the policy in `CONTRACT.md`. Touch: `dash/store.py` (or the `summarize` concern after Phase 1), `agent_dash.py`, `CONTRACT.md`, `tests/test_store.py`. Accept: time-travel test asserts old rows gone and DB size bounded; `check` green. After: Phase 1 if it touches snapshot paths (coordinate or land after). Parallel: yes, except against S-store. Status: no retention code on this branch yet.
- **Dagster slowness visibility.** Landed: a poll that returns after more than 15 s reports state `slow` (counted in `health.slow_sources`, still ok) and doctor warns `source-slow` (`docs/problems/source-slow.md`); full-active-page close-out skips are counted in `health.dagster.skipped_closeouts` with a doctor warn `dagster-closeout-skipped` (`docs/problems/dagster-closeout-skipped.md`). Proof: `dash/sources/__init__.py:40` (`SLOW_AFTER_S`), `:194` (`slow_sources`), `dash/sources/dagster.py:94` (`health_extra`), `:181` (skip count), `dash/doctor.py:312,340` (warn checks), `tests/test_dagster.py` (full page counts skips in health, 16 s poll is slow-not-error, skips raise the doctor warn), `tests/test_doctor.py` (slow and closeout-skipped warn problems). After: none. Parallel: yes.
- **Clock-skew and evidence-age invariants.** Goal: flag `ended_at < started_at`, future starts, usage far outside run bounds, per-source freshness drift, and run-count drops between polls. Touch: `dash/store.py` (or `summarize` concern), `agent_dash.py check`, one new test file. Accept: synthetic skewed fixtures fail `check` with named rows. After: Phase 1 preferred. Parallel: yes. Status: no skew checks on this branch yet.
- **Privacy pin test.** Goal: a test asserting no prompt/argv/env/secret field appears in any endpoint payload. Touch: `tests/test_server.py` (or one new file). Accept: new test green; `sh scripts/privacy_audit.sh` from the repo root stays clean. After: none. Parallel: yes.

## Phase 3 — v2 visual build (after Phase 1; spec approval pending)

Builds [design-v2.md](design-v2.md).

- **V-tokens.** Land `design-v2-tokens.css` as the page token set (dark default, light toggle), replacing the old palette block. Touch: `static/tokens.css` (or its successor), one card for eyeball check. Accept: page-rules tests + fixture-preview eyeball in both themes.
- **V-tiles.** KPI tiles with 24-point sparklines, delta chips, tertiary lines (spec §4 for the math). Touch: tiles card file. Accept: `check --page` tile assertions pass.
- **V-charts.** Token bars (rounded, labelled peaks, top-3 values) + live-agent strips (gradient areas, `pk N` annotations). Touch: token-chart + live-chart card files. Accept: `check --page` chart-sum assertions pass.
- **V-timeline.** Lanes by family with `N runs` labels; **draws every run in the window, never a sample** (parent review requirement); running bars reach the now-rule. Touch: timeline card file. Accept: fixture with more runs than any cap renders all of them; `check --page` green.
- **V-rows + plan map.** Two-line where-we-are rows, ws-card meta split, always-visible plan legend row, arrows-on-focus chip tracing. Touch: ws-cards + plan-progress files. Accept: spec mockup parity by eyeball; `check --page` green.
- **V-responsive.** Phone rules (no horizontal scroll, essential columns, 2-up tiles, stacked charts/maps, ≥36px targets), drawer focus contract, `aria-busy` + section min-heights. Touch: tables/drawer/shell files, tokens. Accept: `check --page` green; 390px eyeball check.
- **`prev_24h` snapshot field.** Goal: `GET /api/snapshot` (and `/api/state` rollup) gains `prev_24h: {tokens, cost_usd, finished, live_agents}` — exact rolling values from 24 h ago so deltas match headlines to the digit and the live-agent delta survives retention changes. Touch: `dash/store.py` (or `summarize` concern), `dash/server.py`, `tests/test_server.py`. Accept: totals equal a recomputed rolling window on fixtures; sparkline tooltips label bucket vs rolling. After: retention (Phase 2) must not break it — land after or together. Parallel: yes, except against retention.

Dependencies: V-tokens before V-tiles/V-charts; `prev_24h` after retention. Parallel: V-timeline, V-rows, V-responsive run beside each other once V-tokens lands.

## Phase 4 — agent-friendly remainder + safe deploy

- **Raw `data-*` on charts.** Goal: `data-key`/`data-v` legend entries, `data-t`/`data-v` bar samples, `data-run-id`/`data-outcome` timeline bars, `data-task`/`data-status` plan chips, exact token values on cells; one `data-*` sum-vs-API spot check per chart in `page_check.mjs`. Touch: card files, `scripts/page_check.mjs`, `tests/test_page_rules.py`. Accept: page-rules + `check --page` green. After: Phase 1 (card files exist). Parallel: yes. Status: tiles carry some `data-*` (`data-tile`, `data-hours`, `data-cost-usd`); the chart and timeline attributes do not exist yet.
- **Source lag on the page.** Goal: a source whose `lag_s` keeps growing shows on the page, not only in `check` and `/api/health` (`lag_s` is not drawn anywhere in `static/index.html` today). Reconcile-only stale-running rows stay off the page by owner decision, and doctor output stays CLI and API only. Touch: tiles/cards files, health payload if needed. Accept: synthetic lag fixture shows the banner; `check --page` green. After: none. Parallel: yes.
- **Safe deploy flow.** Goal: installs from a clean worktree (never the dirty checkout); `build_id` (git sha + dirty flag) in `/api/health` and the page footer; `run` refuses when the port answers with a different `build_id`; document rollback (previous worktree + `run`); record the supervisor decision as a follow-up. Touch: `agent_dash.py`, `dash/server.py`, `static/index.html`, `docs/integrate.md`. Accept: mismatched-`build_id` `run` refuses with the fix command; footer shows the sha; docs describe rollback. After: none. Parallel: yes. Status: no `build_id` on this branch yet.

## Decisions needed (owner)

- Supervisor choice for the dashboard process — follow-up to safe deploy.
- v2 spec approval — blocks Phase 3 build; see `design-v2.md`.
- Merge of the promo clip branch `feat/dash-ws1-replay` (local, not merged; see below).
- Which of the proposed edits to the owner's global CLAUDE.md to apply (the proposals live outside the repo).

## In flight elsewhere

The promo clip work (trim to the busiest 85%, camera, active-work blinking borders, a ticking token/cost/subagent counter, and the GIF/MP4 improvements in `scripts/render_replay.mjs`) lives on the local branch `feat/dash-ws1-replay`, is not merged, and the owner decides when to merge it.
