# WS9 progress-history backfill (2026-10-18)

`ws task` records every status transition to `history.jsonl` going forward
(`dash/bundles.py:append_history`), and the replay, burn-up, and the new
progress endpoint all read that file first. WS9's file already held 296
transitions covering 71 of its 72 done tasks, so today's backfill is one
row, not a reconstruction.

## The one gap: WS9.guards

"Guard stamp columns on serving sales tables" (`g-01`, note
"Verify-only: shipped on main") reads `done` in `plan.json` but had no
transition in `history.jsonl`: it entered the plan already done, so no
`ws task` move ever fired.

Appended line (`history.jsonl`, append-only, fsynced like `append_history`):

```json
{"ts": 1792286453.0643363, "task": "WS9.guards", "to": "done", "backfill": "run ws9-guards manifest end (store run_id muse:e706b1ab-76f9-4383-9889-5770ddf2024a:ws9-guards)"}
```

## Evidence (best first)

1. `plan.json` status now: `done` (source of truth for the state, not the time).
2. Store run `ws9-guards` (muse, verdict PARTIAL, `clock_source: manifest`,
   section "Aggregates keyed on dirty scope"): started 1792286243.0, ended
   **1792286453.0643363** — the appended `ts`. The run maps to WS9.guards
   by the shared task rule (plan task-id substring) and is the only run
   ever mapped to it.
3. The run manifests / `done/` markers / `out/report-*.md` mtimes in the
   `ws9run` scratchpad corroborate a busy window around this time but carry
   no per-task clock for this verify-only task (there is no
   `report-ws9-guards.md`; the nearby `ws9-rollup-spool-guard` markers belong
   to a different task), so they date nothing directly.
4. Git merge times on `origin/main` were checked and not used:
   squash-merge subjects name some task ids but not all, and a merge
   time dates the review landing, not the task finishing. The run end is the
   closer clock.

## Limits

- The `ts` is a manifest-derived run end (store poll granularity), not a
  witnessed `ws task` transition; `from` is unknown and omitted.
- Four stale rows name tasks no longer in the plan (`WS9.g-safety`,
  `WS9.g-ws7tape`, `WS9.gate-overlay` → `deleted`; `WS9.retire-ident` →
  `deferred`, moved to WS1). They are renames/cuts, never `done`, so no
  series counts them; they stay in the file as history.
- After this line, WS9 reads 72/72 done moments observed, 0 inferred
  (`GET /api/replay?ws=WS9`), and the burn-up gap for WS9 is gone.
