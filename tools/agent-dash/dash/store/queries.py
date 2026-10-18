"""Single-row reads and server read endpoints.

Small run/session reads plus the state, workstream,
concurrency, and session endpoints over the shared rollup
helpers. Every method runs under the facade lock.
"""

from __future__ import annotations

from bisect import bisect_right

from dash.attribution import clean_label
from dash.models import run_model_key

from .runs import _parent_map
from .summarize import _scope_tab
from .util import (BILLED_FIELDS, RANGE_S, WS_STATUSES,
                 _drop_launch_echoes, _is_counted_agent,
                 _money, _sum_costs, _tool_key, _ws_sort)


def _by_phase_row(phase_map: dict, ws: str) -> dict:
    """Lifetime ``{phase: {tokens, cost_usd}}`` for one row, zero-filled.

    Every phase is always present so the page can paint the stacked bar
    without branching; workstreams with no spend read all zeros.
    """
    try:
        from dash.phases import PHASES
    except Exception:
        PHASES = ("planning", "execution", "review", "coordination", "unclassified")
    row = (phase_map or {}).get(ws) if isinstance(phase_map, dict) else None
    out = {}
    for phase in PHASES:
        cell = row.get(phase) if isinstance(row, dict) else None
        out[phase] = {
            "tokens": int((cell or {}).get("tokens") or 0),
            "cost_usd": float((cell or {}).get("cost_usd") or 0.0),
        }
    return out


class QueriesMixin:
        def get_run(self, run_id: str):
            with self._lock:
                return self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        def running_tool_runs(self) -> list:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE status='running' AND source IN ('grok','muse','glm')"
                ).fetchall()
            return [dict(row) for row in rows]
        def unknown_tool_runs(self) -> list:
            """Open muse rows still marked unknown, so reconcile can adopt or close them."""
            with self._lock:
                rows = self._conn.execute(
                    """SELECT * FROM runs
                       WHERE status='unknown' AND source='muse' AND ended_at IS NULL"""
                ).fetchall()
            return [dict(row) for row in rows]
        def session_runs(self, session_id: str) -> list:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT run_id, label, session_id, source, status, started_at
                       FROM runs WHERE session_id = ? COLLATE NOCASE""",
                    (session_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        def last_sample_ts(self, run_id: str, pid=None, label=None):
            """Latest process-sample time for a run id, pid, or label.

            Scopes to the run id first; the pid leg also requires the
            label so a reused pid from another worker cannot vouch for
            this run. Unknown stores lack this method and keep the old
            close behavior. Returns an epoch float or None.
            """
            if pid is not None:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    pid = None
            with self._lock:
                hit = self._conn.execute(
                    "SELECT MAX(ts) AS ts FROM process_samples WHERE run_id=?", (run_id,)
                ).fetchone()
                if hit and hit["ts"] is not None:
                    return float(hit["ts"])
                if pid is None:
                    return None
                if label:
                    hit = self._conn.execute(
                        "SELECT MAX(ts) AS ts FROM process_samples WHERE pid=? AND label=?",
                        (pid, label),
                    ).fetchone()
                else:
                    hit = self._conn.execute(
                        "SELECT MAX(ts) AS ts FROM process_samples WHERE pid=?", (pid,)
                    ).fetchone()
            if hit and hit["ts"] is not None:
                return float(hit["ts"])
            return None
        def last_activity(self, run_id: str, started_at=None):
            """Latest evidence for a run: usage event, process sample, or start.

            A manifest worker whose process was seen alive (a process sample
            matched it by pid or label) ends at that last sighting — or the
            out/err file mtime, whichever the caller finds later — never at
            the start time.
            """
            with self._lock:
                row = self._conn.execute(
                    "SELECT MAX(ts) AS ts FROM usage_events WHERE run_id=?", (run_id,)
                ).fetchone()
                sample = self._conn.execute(
                    "SELECT MAX(ts) AS ts FROM process_samples WHERE run_id=?", (run_id,)
                ).fetchone()
            stamps = []
            if row and row["ts"] is not None:
                stamps.append(float(row["ts"]))
            if sample and sample["ts"] is not None:
                stamps.append(float(sample["ts"]))
            if started_at is not None:
                stamps.append(float(started_at))
            return max(stamps) if stamps else None
        def match_run(self, pid: int | None, label: str | None) -> str | None:
            with self._lock:
                if pid is not None:
                    rows = self._conn.execute(
                        "SELECT run_id FROM runs WHERE pid=? AND status='running'", (int(pid),)
                    ).fetchall()
                    if len(rows) == 1:
                        return rows[0]["run_id"]
                text = clean_label(label)
                if text:
                    rows = self._conn.execute(
                        """SELECT run_id FROM runs WHERE label=? AND status='running'
                           ORDER BY started_at IS NULL, started_at DESC, rowid DESC LIMIT 1""",
                        (text,),
                    ).fetchall()
                    if rows:
                        return rows[0]["run_id"]
            return None
        def newer_open_label_row(self, label, source, started_at, exclude_run_id):
            """Newest open row with the same label and source, or None.

            Open means ``running``, or ``unknown`` with no ``ended_at``. Newer
            means a strictly greater ``started_at`` (missing counts as 0). The
            process matcher uses this so a prompt-file or label match goes to
            the newest row: a relaunched run reuses the label under a new
            session id, and the dead older row must not claim its process.
            """
            text = clean_label(label)
            if not text or not source or not exclude_run_id:
                return None
            base = started_at if isinstance(started_at, (int, float)) and not isinstance(started_at, bool) else 0.0
            with self._lock:
                rows = self._conn.execute(
                    """SELECT * FROM runs
                       WHERE label=? AND source=? AND run_id != ?
                       AND (status='running' OR (status='unknown' AND ended_at IS NULL))""",
                    (text, source, exclude_run_id),
                ).fetchall()
            best = None
            best_start = base
            for row in rows:
                candidate = dict(row)
                start = candidate.get("started_at")
                start = float(start) if isinstance(start, (int, float)) and not isinstance(start, bool) else 0.0
                if start > best_start:
                    best = candidate
                    best_start = start
            return best
        def state_view(
            self,
            now: float,
            since: float | None = None,
            until: float | None = None,
            processes=None,
        ) -> dict:
            """One locked pass for ``GET /api/state``: the rollup rows, the
            reconcile mismatches, the live running count by source, the board
            names, and the stale running rows. No nested snapshot calls."""
            with self._lock:
                shown_all, parents = self._shown_locked(now)
                live_open = self._live_open_ids_locked(now, shown_all)
                roll = self._summarize_locked(shown_all, parents, now, since, until, live_open)
                mismatches = self._mismatches_locked(
                    roll, shown_all, now, lambda ws: _scope_tab(roll, ws), processes
                )
                by_source: dict[str, int] = {}
                for run in shown_all:
                    if run.get("status") == "running" and run.get("run_id") in live_open:
                        key = run.get("source") or "unknown"
                        by_source[key] = by_source.get(key, 0) + 1
                range_since = now - RANGE_S if since is None else float(since)
                range_until = float(now if until is None else until)
                prod_jobs = 0
                prod_jobs_finished = 0
                for run in shown_all:
                    if run.get("source") != "dagster":
                        continue
                    if run.get("status") in ("running", "queued") and run.get("run_id") in live_open:
                        prod_jobs += 1
                    if (
                        run.get("status") != "running"
                        and run.get("ended_at") is not None
                        and range_since <= float(run["ended_at"]) <= range_until
                    ):
                        prod_jobs_finished += 1
                boards = {
                    row["ws"]: row["name"] or ""
                    for row in self._conn.execute("SELECT ws, name FROM ws_counts").fetchall()
                }
                stale = [
                    {
                        "run_id": run.get("run_id"),
                        "source": run.get("source"),
                        "label": run.get("label"),
                        "reason": reason,
                    }
                    for run, reason in self._stale_locked(now, shown_all)
                ]
                return {
                    "roll": roll,
                    "mismatches": mismatches,
                    "running_by_source": by_source,
                    "prod_jobs": prod_jobs,
                    "prod_jobs_finished": prod_jobs_finished,
                    "boards": boards,
                    "stale": stale,
                }
        def query_concurrency(self, now: float, ws: str | None, since: float, step: float) -> list:
            step = float(step) if step and step >= 1 else 60.0
            span = max(0.0, now - since)
            if span / step > 5000:
                step = span / 5000
            with self._lock:
                runs = [dict(row) for row in self._all_runs()]
                live_open = self._live_open_ids_locked(now, runs)
            parents = _parent_map(runs)
            runs = _drop_launch_echoes([run for run in runs if run["run_id"] not in parents and _is_counted_agent(run)])
            runs = [
                run
                for run in runs
                if not (run.get("status") in ("running", "queued") and run["run_id"] not in live_open)
            ]
            if ws and ws != "all":
                runs = [run for run in runs if run["workstream"] == ws]
            # Sweep-line over start/end events: one bisect per (tool, model)
            # lane per point instead of one _active check per run per point.
            # Boundaries match _active exactly: a start at t counts
            # (start <= t), a finish at t does not (t < end), and a running
            # run stays open through now (t <= now). Lanes keep first-run
            # order so the per-point models dict reads identically.
            lanes: dict = {}
            for run in runs:
                key = _tool_key(run)
                if not key:
                    continue
                start = run.get("started_at")
                if start is None:
                    continue
                lane = lanes.get((key, run_model_key(run)))
                if lane is None:
                    lane = lanes[(key, run_model_key(run))] = ([], [], [])
                if run.get("status") == "running":
                    lane[2].append(start)
                else:
                    end = run.get("ended_at")
                    if end is None:
                        continue
                    lane[0].append(start)
                    lane[1].append(end)
            for starts, ends, _open in lanes.values():
                starts.sort()
                ends.sort()
            for _starts, _ends, open_starts in lanes.values():
                open_starts.sort()
            points = []
            count = int(span / step) + 1
            for index in range(count):
                t = since + index * step
                if t > now + 1e-6:
                    break
                live = t <= now
                point = {"t": t, "grok": 0, "muse": 0, "glm": 0, "claude": 0, "models": {}}
                for (key, model), (starts, ends, open_starts) in lanes.items():
                    active = bisect_right(starts, t) - bisect_right(ends, t)
                    if live:
                        active += bisect_right(open_starts, t)
                    if active:
                        point[key] += active
                        point["models"][model] = point["models"].get(model, 0) + active
                points.append(point)
            return points
        def query_sessions(self) -> list:
            with self._lock:
                raw, usage, parents = self._scoped_usage()
                runs = [row for row in raw if row["run_id"] not in parents]
                sessions = [dict(row) for row in self._conn.execute("SELECT * FROM sessions").fetchall()]
            by_session: dict = {}
            for run in runs:
                by_session.setdefault(run["session_id"], []).append(run)
            out = []
            for session in sessions:
                members = by_session.get(session["session_id"], [])
                tokens = 0
                costs = []
                any_tokens = False
                for run in members:
                    folded, cost, _unpriced = usage.get(run["run_id"], ({}, 0.0, False))
                    for key in BILLED_FIELDS:
                        value = folded.get(key)
                        if value:
                            tokens += value
                            any_tokens = True
                    costs.append(cost)
                cost_usd = _sum_costs(costs, any_tokens)
                out.append(
                    {
                        "session_id": session["session_id"],
                        "tool": session["tool"],
                        "cwd": session["cwd"],
                        "label": session["label"],
                        "workstream": session["workstream"],
                        "started_at": session["started_at"],
                        "status": session["status"],
                        "runs": len(members),
                        "tokens": tokens,
                        "cost_usd": _money(cost_usd) if cost_usd is not None else None,
                    }
                )
            out.sort(key=lambda item: item["started_at"] or 0, reverse=True)
            return out
        def query_workstreams(
            self,
            plan_ws: set[str],
            since: float | None = None,
            until: float | None = None,
            now: float | None = None,
            bundle_meta: dict | None = None,
            allowed_states: set[str] | None = None,
        ) -> list:
            """One row per workstream from the shared rollup: same agent rule, window,
            and bucket attribution as the snapshot. Hub issue counts (``tk``) ride along
            as issue counts; they are never plan progress.

            Rows carry the bundle lifecycle fields (``state``,
            ``state_changed_at``, ``archived_at``, ``depends_on``,
            ``milestone``), defaulting to ``active`` when the bundle has
            none. ``allowed_states`` scopes the rows: None keeps the
            default scope (drops ``archived``/``dropped``); a set from
            ``dash.workstates.parse_state_param`` overrides it. The
            ``unassigned`` row always stays.

            Rows serve from a small result cache keyed by
            :meth:`workstreams_key`: the rollup costs ~140 ms on a large
            database and the page fetches two scopes per refresh. Copies
            on the way out: the server adds per-row progress in place.
            """
            import copy as _copy
            import time as _time

            from dash.workstates import filter_rows as _filter_rows
            from dash.workstates import state_of as _state_of

            now = float(now if now is not None else _time.time())
            key = self.workstreams_key(plan_ws, since, until, now, bundle_meta, allowed_states)
            if key is not None:
                with self._lock:
                    hit = self._workstreams_cache.get(key)
                    if hit is not None:
                        return _copy.deepcopy(hit)
            with self._lock:
                counts = [dict(row) for row in self._conn.execute("SELECT * FROM ws_counts").fetchall()]
                shown_all, parents = self._shown_locked(now)
                usage_once = self._usage_by_run()
                roll = self._summarize_locked(
                    shown_all, parents, now, since, until, usage=usage_once
                )
            # Lifetime usage split by phase for every row below. Outside
            # the lock (it takes it itself); a rules edit retags through
            # workstreams_key, which carries the rules hash.
            try:
                phase_map = self.by_phase_all_time()
            except Exception:
                phase_map = {}
            boards = {row["ws"]: row for row in counts}
            seen = set()

            def _lifecycle(ws: str) -> dict:
                item = bundle_meta.get(ws) if isinstance(bundle_meta, dict) else None
                if not isinstance(item, dict):
                    item = {}
                return {
                    "state": _state_of(ws, bundle_meta),
                    "state_changed_at": item.get("state_changed_at"),
                    "archived_at": item.get("archived_at"),
                    "depends_on": list(item.get("depends_on") or []),
                    "milestone": item.get("milestone"),
                }

            out = []
            for row in roll["rows"]:
                seen.add(row["ws"])
                board = boards.get(row["ws"], {})
                out.append(
                    {
                        "ws": row["ws"],
                        "name": board.get("name") or "",
                        "tk": {key: int(board.get(key) or 0) for key in WS_STATUSES},
                        **_lifecycle(row["ws"]),
                        "live_agents": row["live_agents"],
                        "finished_in_range": row["finished_in_range"],
                        "finished_today": row["finished_today"],
                        "tokens": row["tokens"],
                        "cost_usd": row["cost_usd"],
                        "tokens_all_time": row["tokens_all_time"],
                        "cost_usd_all_time": row["cost_usd_all_time"],
                        "by_phase": _by_phase_row(phase_map, row["ws"]),
                        "last_activity": row["last_activity"],
                        "plan": row["ws"] in plan_ws,
                        "ws_confidence": list(row.get("ws_confidence") or []),
                    }
                )
            for ws in sorted(set(boards) - seen, key=_ws_sort):
                board = boards[ws]
                out.append(
                    {
                        "ws": ws,
                        "name": board.get("name") or "",
                        "tk": {key: int(board.get(key) or 0) for key in WS_STATUSES},
                        **_lifecycle(ws),
                        "live_agents": 0,
                        "finished_in_range": 0,
                        "finished_today": 0,
                        "tokens": 0,
                        "cost_usd": 0.0,
                        "tokens_all_time": 0,
                        "cost_usd_all_time": 0.0,
                        "by_phase": _by_phase_row(phase_map, ws),
                        "last_activity": None,
                        "plan": ws in plan_ws,
                        "ws_confidence": [],
                    }
                )
            if roll["has_unassigned"]:
                row = roll["unassigned"]
                out.append(
                    {
                        "ws": row["ws"],
                        "name": "Unassigned",
                        "tk": {key: 0 for key in WS_STATUSES},
                        "state": None,
                        "state_changed_at": None,
                        "archived_at": None,
                        "depends_on": [],
                        "milestone": None,
                        "live_agents": row["live_agents"],
                        "finished_in_range": row["finished_in_range"],
                        "finished_today": row["finished_today"],
                        "tokens": row["tokens"],
                        "cost_usd": row["cost_usd"],
                        "tokens_all_time": row["tokens_all_time"],
                        "cost_usd_all_time": row["cost_usd_all_time"],
                        "by_phase": _by_phase_row(phase_map, row["ws"]),
                        "last_activity": row["last_activity"],
                        "plan": False,
                        "ws_confidence": list(row.get("ws_confidence") or []),
                    }
                )
            out = _filter_rows(out, allowed_states, bundle_meta)
            out.sort(key=lambda item: _ws_sort(item["ws"]))
            if key is not None:
                with self._lock:
                    if len(self._workstreams_cache) > 8:
                        self._workstreams_cache.clear()
                    self._workstreams_cache[key] = _copy.deepcopy(out)
            return out
        def workstreams_key(
            self,
            plan_ws,
            since: float | None,
            until: float | None,
            now: float,
            bundle_meta: dict | None,
            allowed_states,
        ) -> tuple | None:
            """Identity of a workstream-rows request for caches and ETags.

            The data sequence (real writes bump it), the quantized window
            and a 60 s ``now`` quantum (liveness moves with time; anything
            finer-grained recomputes every tick), the plan set, the scope,
            a digest of the caller's metadata map, and the plan-file stamps
            behind row progress. ``_seq`` stays out: polling bumps it on
            every cycle (process samples, counts) without moving any row,
            so tagging it would 200 every tick. None when an input is
            unhashable: the caller then recomputes without caching.
            """
            import hashlib as _hashlib

            try:
                from dash.bundles import plans_stamp as _plans_stamp

                stamp = _plans_stamp()
            except Exception:
                stamp = None
            try:
                meta_digest = (
                    None
                    if bundle_meta is None
                    else _hashlib.sha1(repr(bundle_meta).encode("utf-8")).hexdigest()
                )
            except Exception:
                return None
            try:
                plans = None if plan_ws is None else frozenset(plan_ws)
                allowed = None if allowed_states is None else frozenset(allowed_states)
            except TypeError:
                return None
            try:
                from dash import phases as phases_mod

                phases_digest = phases_mod.current_hash()
            except Exception:
                phases_digest = None
            try:
                return (
                    self._data_seq,
                    None if since is None else int(float(since) // 30),
                    None if until is None else int(float(until) // 30),
                    int(float(now) // 60),
                    plans,
                    allowed,
                    meta_digest,
                    stamp,
                    phases_digest,
                )
            except (TypeError, ValueError):
                return None
