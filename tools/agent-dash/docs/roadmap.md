# Dashboard roadmap — open work after the robustness + agent-friendly landings

Living plan for agents picking up dashboard work. Each open item is one
worker diff: goal, files touched, acceptance commands, dependencies, and
which items can run beside it. Proof pointers (`file:line`) below were
checked against `origin/main`; re-check them if main has moved.

Related: the [proposed v2 design spec](design-v2.md) (owner approval
pending), the [integration guide](integrate.md), and
[CONTRACT.md](../CONTRACT.md).

Conventions for every item: run only the listed explicit test files or
commands, never a bare suite, never GitHub Actions. Behavior oracles:
`python3 agent_dash.py check`, `check --page`,
`python3 -m unittest discover -s tests -p "test_page_rules.py"`.
Gates run from `tools/agent-dash/`.

## What already landed (one line each, with proof)

Robustness audit items 1–3 and 5–11 are done on main:

- Per-source watchdog + lag alert: a hung source no longer stalls the rest; `health` carries `last_duration_s` and `lag_s` (`dash/sources/__init__.py:48,125,193`, `Registry` docstring at `:12`).
- Bounded scans: tick budgets with skipped-by-budget counts (`dash/sources/cursor.py:306-324`).
- Offset pruning + backup rotation: `Store.prune_offsets` (`dash/store.py:185`).
- Snapshot cost: usage computed once per snapshot, not once per scope (`dash/store.py:1189,1287,1312`, cached via `_usage_by_run` at `:2284-2289`).
- Snapshot cache between writes + freshness surfacing: `snapshot_cached` served per tick with `last_data_ts`/`last_poll` (`dash/server.py:201,351,361-362`); page shows a `stale` badge and a fetch-error banner (`static/index.html:339-340,348,360`).
- Unpriced-model alert: `/api/health` lists `unpriced_models` (`dash/server.py:111-114`); missing catalog rows priced.
- Truncation surfaced: finished-section subtitle reads "Newest N of M" from `finished_in_range` (`static/index.html:2281-2290`).
- Disconnects silenced and counted: `disconnects_1h` on `/api/health`, no tracebacks (`dash/server.py:49,60,170,387`).
- Plan-write locking: per-bundle `fcntl` lock across read-modify-write (`dash/bundles.py:366`; documented in `CONTRACT.md:348`).
- Validate on read: `/api/plan` and `/api/bundle` attach `issues` instead of drawing bad states as tasks (`dash/bundles.py:259,323`).
- Unseen-worker check + safe restart: `check` flags live-but-invisible workers (`dash/store.py:1355-1403`); `restart`/`stop` wait for real exit so the next `run` can bind (see "Install and run" in `integrate.md`).

Agent-friendly CLI items are done on main:

- One-call state: `GET /api/state` (`dash/server.py:116`) via `state` (`agent_dash.py:426`).
- `doctor`: read-only triage with a fix per problem (`agent_dash.py:492`, `dash/doctor.py`).
- `--json` on `check`, `check --page` (summary), `state`, `status`, `doctor`, `ws validate`, `ws approvals`, `ws trains`, `ws plan` (`agent_dash.py:385-386`; schema in `integrate.md` "For agents").
- Exit-code norm 0/1/2/3/4/5 plus messages that name the mistake, the valid values, and the next command (`dash/errors.py:1-113`; table in `integrate.md` "For agents").

In flight elsewhere — do not duplicate: run-identity aliases for
resurrected manifest rows, and the replay-clip branch. Orchestration tooling
lands on its own branch: `ws next` (rank ready tasks), `ws waves` (max-parallel
waves), `ws overlap` (file collisions), `ws gates` (gate freshness), `ws deferred`
(moved-task ledger), and `agent_dash.py lead-profile`; the rules they enforce are
[orchestration principles](orchestration.md).

## Phase 1 — module split (lands before the visual pass)

The page is one large inline script and `dash/store.py` is one
~2.4k-line file. Split both behind stable APIs first, moving code
verbatim with zero behavior change, so later visual and store work has
small files with one writer each. No DOM-id, `data-*`, API-shape, or
lock-semantics change in this phase.

- **F-core.** Extract `static/core/` (`el`, `state`, `api`, `cache`, `charts`, `drawer`). Touch: `static/index.html` (script tags + delete moved fns), new `static/core/*.js`. Accept: page loads; `python3 -m unittest discover -s tests -p "test_page_rules.py"`.
- **F-cards.** One file per card under `static/cards/` (`tiles`, `token-chart`, `live-chart`, `timeline`, `tables`, `ws-cards`, `plan-progress`, `needs`) plus `static/cards/registry.js` (ordered card list; adding a card is one file plus one line). Touch: `static/index.html`, new card files. Accept: page-rules tests + `python3 agent_dash.py check --page`. Note: keep two-space `function` indent so the test splitter keeps matching, or extend the splitter in the same step.
- **S-store.** `dash/store.py` becomes a facade package: `base` (connect, schema, migrations), `runs`, `usage`, `running`, `summarize`, `queries`, `util`. The facade holds the single lock; concern modules expose `_locked`-style functions taking the connection, never their own lock. External callers (`server.py`, `agent_dash.py`, `sources/*`) unchanged. Accept: `python3 -m unittest discover -s tests -p "test_store.py"` + `python3 agent_dash.py check`.
- **D-own.** Update the `CONTRACT.md` ownership table per file (page shell, `static/core/*.js`, each card file, each store concern) once F and S land. Reviewer diff only.

Dependencies: F-core before F-cards. After: none. Parallel: F-core and S-store run beside each other (disjoint trees: `static/` vs `dash/`); F-cards parallel after F-core; D-own last, alone.

## Phase 2 — correctness (ordered by value)

- **Startup grace.** Goal: the first poll after a restart must not briefly report every source stalled. Touch: `dash/sources/__init__.py` (or the registry health path), new `tests/test_restart_grace.py`. Accept: restart-then-poll test shows no source flagged stalled within the grace window; `check` green. After: none. Parallel: yes.
- **Workstream inheritance for leads and helpers.** Goal: a lead session and its helper runs get the lead's workstream (inferred from the worktree path or bundle writes) instead of landing unassigned. Touch: `dash/attribution.py`, one source or matcher, `tests/test_attribution.py`. Accept: synthetic lead + helper fixtures attribute to the lead's workstream; existing label-regex behavior unchanged. After: none. Parallel: yes.
- **Retention + checkpoint policy.** Goal: bound `runs`, `usage_events`, `usage_buckets`, `sessions`, and the seen-set beyond N days (default 90); checkpoint on idle; document the policy in `CONTRACT.md`. Touch: `dash/store.py` (or the `summarize` concern after Phase 1), `agent_dash.py`, `CONTRACT.md`, `tests/test_store.py`. Accept: time-travel test asserts old rows gone and DB size bounded; `check` green. After: Phase 1 if it touches snapshot paths (coordinate or land after). Parallel: yes, except against S-store.
- **Dagster slowness visibility.** Goal: duration metric per poll and a surfaced (not silent) slow-but-returning poll; count skipped close-outs at the active-set limit. Touch: `dash/sources/dagster.py`, health payload, `tests/test_dagster.py` if present. Accept: slow stub poll records duration; health exposes it; `check` green. After: none. Parallel: yes.
- **Clock-skew and evidence-age invariants.** Goal: flag `ended_at < started_at`, future starts, usage far outside run bounds, per-source freshness drift, and run-count drops between polls. Touch: `dash/store.py` (or `summarize` concern), `agent_dash.py check`, one new test file. Accept: synthetic skewed fixtures fail `check` with named rows. After: Phase 1 preferred. Parallel: yes.
- **`process_samples(run_id)` index.** Goal: index the `(run_id)` lookup before the 10x cliff. Touch: `dash/store.py` schema + migration, `tests/test_store.py`. Accept: `EXPLAIN` shows index use; suite green. After: none. Parallel: yes.
- **Privacy pin test.** Goal: a test asserting no prompt/argv/env/secret field appears in any endpoint payload. Touch: `tests/test_server.py` (or one new file). Accept: new test green; `sh scripts/privacy_audit.sh` from the repo root stays clean. After: none. Parallel: yes.

## Phase 3 — v2 visual build (after Phase 1; spec approval pending)

Builds [design-v2.md](design-v2.md). Needs the tile-semantics decision
(calendar vs rolling per range) before the tiles step.

- **V-tokens.** Land `design-v2-tokens.css` as the page token set (dark default, light toggle), replacing the old palette block. Touch: `static/tokens.css` (or its successor), one card for eyeball check. Accept: page-rules tests + fixture-preview eyeball in both themes.
- **V-tiles.** KPI tiles with 24-point sparklines, delta chips, tertiary lines (spec §4 for the math). Touch: tiles card file. Accept: `check --page` tile assertions pass.
- **V-charts.** Token bars (rounded, labelled peaks, top-3 values) + live-agent strips (gradient areas, `pk N` annotations). Touch: token-chart + live-chart card files. Accept: `check --page` chart-sum assertions pass.
- **V-timeline.** Lanes by family with `N runs` labels; **draws every run in the window, never a sample** (parent review requirement); running bars reach the now-rule. Touch: timeline card file. Accept: fixture with more runs than any cap renders all of them; `check --page` green.
- **V-rows + plan map.** Two-line where-we-are rows, ws-card meta split, always-visible plan legend row, arrows-on-focus chip tracing. Touch: ws-cards + plan-progress files. Accept: spec mockup parity by eyeball; `check --page` green.
- **V-responsive.** Phone rules (no horizontal scroll, essential columns, 2-up tiles, stacked charts/maps, ≥36px targets), drawer focus contract, `aria-busy` + section min-heights. Touch: tables/drawer/shell files, tokens. Accept: `check --page` green; 390px eyeball check.
- **`prev_24h` snapshot field.** Goal: `GET /api/snapshot` (and `/api/state` rollup) gains `prev_24h: {tokens, cost_usd, finished, live_agents}` — exact rolling values from 24 h ago so deltas match headlines to the digit and the live-agent delta survives retention changes. Touch: `dash/store.py` (or `summarize` concern), `dash/server.py`, `tests/test_server.py`. Accept: totals equal a recomputed rolling window on fixtures; sparkline tooltips label bucket vs rolling. After: retention (Phase 2) must not break it — land after or together. Parallel: yes, except against retention.

Dependencies: V-tokens before V-tiles/V-charts; `prev_24h` after retention. Parallel: V-timeline, V-rows, V-responsive run beside each other once V-tokens lands.

## Phase 4 — agent-friendly remainder + safe deploy

- **Raw `data-*` on charts.** Goal: `data-key`/`data-v` legend entries, `data-t`/`data-v` bar samples, `data-run-id`/`data-outcome` timeline bars, `data-task`/`data-status` plan chips, exact token values on cells; one `data-*` sum-vs-API spot check per chart in `page_check.mjs`. Touch: card files, `scripts/page_check.mjs`, `tests/test_page_rules.py`. Accept: page-rules + `check --page` green. After: Phase 1 (card files exist). Parallel: yes.
- **Stale + health surfacing.** Goal: reconcile-only signals (stale-running rows, source lag, unpriced models) render on the page, not just in `check`. Touch: tiles/cards files, health payload if needed. Accept: synthetic stale/lag fixtures show the banner; `check --page` green. After: none. Parallel: yes.
- **Safe deploy flow.** Goal: installs from a clean worktree (never the dirty checkout); `build_id` (git sha + dirty flag) in `/api/health` and the page footer; `run` refuses when the port answers with a different `build_id`; document rollback (previous worktree + `run`); record the supervisor decision as a follow-up. Touch: `agent_dash.py`, `dash/server.py`, `static/index.html`, `docs/integrate.md`. Accept: mismatched-`build_id` `run` refuses with the fix command; footer shows the sha; docs describe rollback. After: none. Parallel: yes.

## Decisions needed (owner)

- Tile semantics per range (calendar vs rolling) — blocks V-tiles.
- Supervisor choice for the dashboard process — follow-up to safe deploy.
- v2 spec approval — blocks Phase 3 build; see `design-v2.md`.
