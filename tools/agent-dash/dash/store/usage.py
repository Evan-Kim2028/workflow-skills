"""Usage events, hourly buckets, pricing, usage by run.

Owns usage events, process samples, hourly buckets, pricing
application (via ``dash.pricing``), and per-run usage folds.
Public methods run under the facade lock; ``_locked`` helpers
assume it and never take it.
"""

from __future__ import annotations

import os as _os

from dash import perfmon as _perf
from dash.attribution import clean_label
from dash.models import harness_of, model_key
from dash.pricing import _band_cached
from dash.pricing import _lookup as _price_lookup
from dash.pricing import _prompt_len as _price_prompt_len
from dash.pricing import _rate_vector
from dash.pricing import _tier_over
from dash.pricing import _write_charge
from dash.pricing import catalog as _price_catalog
from dash.pricing import cost as price_cost
from dash.pricing import series_key
from dash.pricing import status as price_status

from .util import (_ESTIMATE_TOOL, RANGE_S, SAMPLE_KEEP_S,
                 TOKEN_FIELDS, UNASSIGNED, WS_STATUSES,
                 _hour, _manifest_family, _money, _opt_int, _same)


class UsageMixin:
        def add_usage(self, row: dict) -> None:
            tool = row.get("tool")
            session_id = row.get("session_id")
            message_id = row.get("message_id")
            if tool is None or session_id is None or message_id is None:
                raise ValueError("tool, session_id, and message_id required")
            tool, session_id, message_id = str(tool), str(session_id), str(message_id)
            values = {key: _opt_int(row, key) for key in TOKEN_FIELDS}
            write_5m = _opt_int(row, "cache_write_5m")
            write_1h = _opt_int(row, "cache_write_1h")
            from dash.lead_split import clean_ws as _clean_ws

            ws_split = _clean_ws(row.get("ws_split"))
            model = row.get("model")
            ts = row.get("ts")
            run_id = row.get("run_id")
            est = None if tool == _ESTIMATE_TOOL else price_cost(
                model,
                values["input"],
                values["output"],
                values["cache_read"],
                values["cache_write"],
                cache_write_5m=write_5m,
                cache_write_1h=write_1h,
                reasoning=values["reasoning"],
                ts=ts,
            )
            with self._lock:
                if isinstance(run_id, str) and _manifest_family(run_id) is not None:
                    run_id = self._resolve_alias_locked(run_id)
                previous = self._one(
                    """SELECT ts FROM usage_events
                       WHERE tool=? AND session_id=? AND message_id=?""",
                    (tool, session_id, message_id),
                )
                current = self._one(
                    """SELECT ts, run_id, model, input_tokens, output_tokens, cache_read,
                              cache_write, cache_write_5m, cache_write_1h, reasoning, est_cost_usd,
                              ws_split
                       FROM usage_events WHERE tool=? AND session_id=? AND message_id=?""",
                    (tool, session_id, message_id),
                )
                incoming = {
                    "ts": ts,
                    "run_id": run_id,
                    "model": model,
                    "input_tokens": values["input"],
                    "output_tokens": values["output"],
                    "cache_read": values["cache_read"],
                    "cache_write": values["cache_write"],
                    "cache_write_5m": write_5m,
                    "cache_write_1h": write_1h,
                    "reasoning": values["reasoning"],
                    "est_cost_usd": est,
                    "ws_split": ws_split,
                }
                if current and _same(current, incoming, incoming.keys()):
                    return
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        """INSERT INTO usage_events(
                               tool, session_id, message_id, ts, run_id, model,
                               input_tokens, output_tokens, cache_read, cache_write,
                               cache_write_5m, cache_write_1h, reasoning, est_cost_usd, ws_split)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(tool, session_id, message_id) DO UPDATE SET
                             ts=excluded.ts, run_id=excluded.run_id, model=excluded.model,
                             input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                             cache_read=excluded.cache_read, cache_write=excluded.cache_write,
                             cache_write_5m=excluded.cache_write_5m, cache_write_1h=excluded.cache_write_1h,
                             reasoning=excluded.reasoning, est_cost_usd=excluded.est_cost_usd,
                             ws_split=COALESCE(excluded.ws_split, usage_events.ws_split)""",
                        (
                            tool,
                            session_id,
                            message_id,
                            ts,
                            run_id,
                            model,
                            values["input"],
                            values["output"],
                            values["cache_read"],
                            values["cache_write"],
                            write_5m,
                            write_1h,
                            values["reasoning"],
                            est,
                            ws_split,
                        ),
                    )
                    hours = set()
                    if previous and previous["ts"] is not None:
                        hours.add(_hour(previous["ts"]))
                    if ts is not None:
                        hours.add(_hour(ts))
                    for hour in hours:
                        self._rebuild_hour(hour)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                self._note_view_write()
                self._bump_usage_run(run_id)
        def add_process_sample(self, row: dict) -> None:
            ts = float(row["ts"])
            with self._lock:
                run_id = row.get("run_id")
                if isinstance(run_id, str) and _manifest_family(run_id) is not None:
                    run_id = self._resolve_alias_locked(run_id)
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        """INSERT INTO process_samples(ts, pid, comm, rss_kb, elapsed_s, label, run_id, model)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            ts,
                            int(row["pid"]),
                            row.get("comm"),
                            _opt_int(row, "rss_kb"),
                            row.get("elapsed_s"),
                            clean_label(row.get("label")),
                            run_id,
                            row.get("model"),
                        ),
                    )
                    self._conn.execute(
                        "DELETE FROM process_samples WHERE ts < ?", (ts - SAMPLE_KEEP_S,)
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                self._seq += 1
        def set_ws_counts(self, rows: list) -> None:
            clean = []
            for row in rows:
                ws = row.get("ws")
                if not ws:
                    continue
                item = {"ws": str(ws), "name": row.get("name") or ""}
                for key in WS_STATUSES:
                    item[key] = int(row.get(key) or 0)
                clean.append(item)
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute("DELETE FROM ws_counts")
                    self._conn.executemany(
                        """INSERT INTO ws_counts(ws, name, backlog, todo, doing, review, done, cancelled)
                           VALUES(:ws,:name,:backlog,:todo,:doing,:review,:done,:cancelled)""",
                        clean,
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                self._seq += 1
        def query_usage(
            self,
            group: str,
            bucket: str,
            ws: str | None,
            since: float | None,
            until: float | None = None,
        ) -> list:
            with self._lock:
                return self._query_usage_locked(group, bucket, ws, since, until)
        def _query_usage_locked(
            self,
            group: str,
            bucket: str,
            ws: str | None,
            since: float | None,
            until: float | None = None,
        ) -> list:
            """Bucket cells for a scope and window. Caller holds the lock, so
            ``snapshot`` can read these in the same pass as its totals."""
            sql = """SELECT hour_ts, model, workstream, tool, input_tokens, output_tokens,
                            cache_read, cache_write, reasoning, missing, cost_usd FROM usage_buckets"""
            args: list = []
            clauses: list = []
            if since is not None:
                clauses.append("hour_ts + 3600 > ?")
                args.append(since)
            if until is not None:
                clauses.append("hour_ts < ?")
                args.append(until)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            stored = self._conn.execute(sql, args).fetchall()
            cells: dict = {}
            for row in stored:
                if "tool" in row.keys() and row["tool"] == _ESTIMATE_TOOL:
                    continue
                if ws and ws != "all" and (row["workstream"] or UNASSIGNED) != ws:
                    continue
                if group == "model":
                    key = model_key(row["model"], row["tool"])[1]
                    key = "%s@%s" % (key, harness_of({"source": row["tool"]}))
                elif group == "family":
                    key = series_key(row["model"])
                elif group == "workstream":
                    key = row["workstream"] or UNASSIGNED
                else:
                    key = row["tool"] or None
                start = int(row["hour_ts"] // 86400) * 86400 if bucket == "day" else row["hour_ts"]
                cell = cells.setdefault(
                    (start, key),
                    {
                        "t": start,
                        "key": key,
                        "input": 0,
                        "output": 0,
                        "cache_read": 0,
                        "cache_write": 0,
                        "reasoning": 0,
                        "cost_usd": 0.0,
                        "priced": False,
                        "gap": False,
                        "unpriced_tokens": 0,
                    },
                )
                cell["input"] += row["input_tokens"] or 0
                cell["output"] += row["output_tokens"] or 0
                cell["cache_read"] += row["cache_read"] or 0
                cell["cache_write"] += row["cache_write"] or 0
                cell["reasoning"] += row["reasoning"] or 0
                present = any(row[name] for name in ("input_tokens", "output_tokens", "cache_read", "cache_write"))
                if row["missing"] or (row["cost_usd"] is None and present):
                    cell["gap"] = True
                    cell["unpriced_tokens"] += sum(row[name] or 0 for name in ("input_tokens", "output_tokens", "cache_read", "cache_write"))
                elif row["cost_usd"] is not None:
                    cell["cost_usd"] += row["cost_usd"]
                    cell["priced"] = True
            out = []
            for cell in cells.values():
                cost = None if cell["gap"] or not cell["priced"] else _money(cell["cost_usd"])
                out.append(
                    {
                        "t": cell["t"],
                        "key": cell["key"],
                        "input": cell["input"],
                        "output": cell["output"],
                        "cache_read": cell["cache_read"],
                        "cache_write": cell["cache_write"],
                        "reasoning": cell["reasoning"],
                        "cost_usd": cost,
                        "priced_cost_usd": _money(cell["cost_usd"]) if cell["priced"] else None,
                        "unpriced_tokens": cell["unpriced_tokens"],
                    }
                )
            out.sort(key=lambda item: (item["t"], str(item["key"] or "")))
            return out
        def usage_model_stats(self) -> list[dict]:
            """Per-model usage totals, cached by data sequence.

            The full-table ``GROUP BY model`` scan costs tens of ms on a
            large database and ``/api/health`` calls it on every request,
            so idle ticks reuse the cached rows: any usage write bumps
            ``_data_seq`` and invalidates precisely. Callers get fresh
            row dicts, never the cached objects.
            """
            with self._lock:
                hit = self._model_stats_cache
                if hit is not None and hit[0] == self._data_seq:
                    _perf.record_cache("model_stats", "hit")
                    return [dict(row) for row in hit[1]]
                rows = self._conn.execute(
                    """SELECT model AS model, COUNT(*) AS rows,
                              SUM(input_tokens) AS input, SUM(output_tokens) AS output,
                              SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write,
                              SUM(reasoning) AS reasoning
                       FROM usage_events GROUP BY model"""
                ).fetchall()
                # Rolled bundles rejoin as exact integer sums per model.
                rolled = self._conn.execute(
                    """SELECT model AS model, SUM(n_events) AS rows,
                              SUM(input_tokens) AS input, SUM(output_tokens) AS output,
                              SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write,
                              SUM(reasoning) AS reasoning
                       FROM usage_rollup_1h GROUP BY model"""
                ).fetchall()
                if rolled:
                    merged: dict = {}
                    for row in list(rows) + list(rolled):
                        entry = merged.setdefault(
                            row["model"],
                            {"model": row["model"], "rows": 0, "input": 0, "output": 0,
                             "cache_read": 0, "cache_write": 0, "reasoning": 0},
                        )
                        entry["rows"] += row["rows"] or 0
                        for key in ("input", "output", "cache_read", "cache_write", "reasoning"):
                            entry[key] += row[key] or 0
                    rows = list(merged.values())
                stats = self._model_stats_rows(rows)
                self._model_stats_cache = (self._data_seq, stats)
                _perf.record_cache("model_stats", "miss")
                return [dict(row) for row in stats]
        def _model_stats_rows(self, rows) -> list[dict]:
            stats = []
            for row in rows:
                stats.append(
                    {
                        "model": row["model"],
                        "rows": row["rows"],
                        "input": row["input"] or 0,
                        "output": row["output"] or 0,
                        "cache_read": row["cache_read"] or 0,
                        "cache_write": row["cache_write"] or 0,
                        "reasoning": row["reasoning"] or 0,
                    }
                )
            return stats
        def unpriced_models(
            self,
            now: float,
            since: float | None = None,
            until: float | None = None,
            ws: str | None = None,
        ) -> list[dict]:
            """Models with billed tokens in the window and no catalog rate.

            One entry per model: ``{"model", "rows", "tokens"}``, sorted by
            model. A missing rate never becomes zero: these tokens are left
            out of cost, and health, reconcile, and the cost tile say so
            loudly. A model whose windowed rows carry zero billed tokens
            (Claude Code's ``<synthetic>`` interrupt and error markers) is
            not a price gap and is left out. Rows with no model id are
            skipped (data quality, not a price gap).
            """
            range_since = now - RANGE_S if since is None else float(since)
            range_until = float(now if until is None else until)
            with self._lock:
                return self._unpriced_models_locked(range_since, range_until, ws)
        def _unpriced_models_locked(self, since: float, until: float, ws: str | None) -> list[dict]:
            """Windowed unpriced models. Caller holds the lock.

            Cached by data sequence and minute-quantized window: the
            /api/health path calls this on every request, and a sliding
            24 h window moving by seconds is invisible in the sums.
            """
            key = (
                self._data_seq,
                int(float(since) // 60),
                int(float(until) // 60),
                ws,
            )
            hit = self._unpriced_cache.get(key)
            if hit is not None:
                _perf.record_cache("unpriced", "hit")
                return [dict(entry) for entry in hit]
            sql = """SELECT e.model AS model, COUNT(*) AS rows,
                            SUM(COALESCE(e.input_tokens, 0) + COALESCE(e.output_tokens, 0)
                                + COALESCE(e.cache_read, 0) + COALESCE(e.cache_write, 0)) AS tokens
                     FROM usage_events e LEFT JOIN runs r ON r.run_id = e.run_id
                     WHERE e.ts >= ? AND e.ts < ?"""
            args: list = [since, until]
            if ws is not None and ws != "all":
                if ws == UNASSIGNED:
                    sql += " AND (r.workstream IS NULL OR r.workstream = '')"
                else:
                    sql += " AND r.workstream = ?"
                    args.append(ws)
            sql += " GROUP BY e.model"
            out = []
            live = self._conn.execute(sql, args).fetchall()
            # Rolled bundles rejoin with exact row counts and token sums.
            # An open window matches nothing live, so it matches nothing
            # rolled either.
            rolled = []
            if since is not None and until is not None:
                roll_sql = """SELECT model AS model, SUM(n_events) AS rows,
                                     SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                                         + COALESCE(cache_read, 0) + COALESCE(cache_write, 0)) AS tokens
                              FROM usage_rollup_1h WHERE hour_ts >= ? AND hour_ts < ? GROUP BY model"""
                rolled = self._conn.execute(roll_sql, (int(float(since) // 1), int(float(until) // 1))).fetchall()
            if rolled:
                merged: dict = {}
                for row in list(live) + list(rolled):
                    entry = merged.setdefault(row["model"], {"model": row["model"], "rows": 0, "tokens": 0})
                    entry["rows"] += row["rows"] or 0
                    entry["tokens"] += row["tokens"] or 0
                live = list(merged.values())
            for row in live:
                model = row["model"]
                if not model:
                    continue
                tokens = int(row["tokens"] or 0)
                if tokens <= 0:
                    continue
                if price_status(model)["priced"]:
                    continue
                out.append(
                    {
                        "model": model,
                        "rows": int(row["rows"]),
                        "tokens": tokens,
                    }
                )
            out.sort(key=lambda entry: entry["model"])
            if len(self._unpriced_cache) > 32:
                self._unpriced_cache.clear()
            self._unpriced_cache[key] = out
            _perf.record_cache("unpriced", "miss")
            return [dict(entry) for entry in out]
        def usage_events_for_cost(self) -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT tool, session_id, message_id, model,
                              input_tokens, output_tokens, cache_read, cache_write,
                              cache_write_5m, cache_write_1h, reasoning, est_cost_usd, ts
                       FROM usage_events"""
                ).fetchall()
            return [dict(row) for row in rows]
        def usage_events_missing_cost(self, limit: int = 5000) -> list[dict]:
            """Up to ``limit`` usage rows whose cost is still NULL.

            Estimate-tool rows never price, so they are excluded. The
            catalog-change pass prices these with the current catalog
            instead of rescanning priced history.
            """
            try:
                bound = max(int(limit), 1)
            except (TypeError, ValueError):
                bound = 5000
            with self._lock:
                rows = self._conn.execute(
                    """SELECT tool, session_id, message_id, model,
                              input_tokens, output_tokens, cache_read, cache_write,
                              cache_write_5m, cache_write_1h, reasoning, est_cost_usd, ts
                       FROM usage_events
                       WHERE est_cost_usd IS NULL AND COALESCE(tool, '') != ?
                       ORDER BY ts IS NULL, ts
                       LIMIT ?""",
                    (_ESTIMATE_TOOL, bound),
                ).fetchall()
            return [dict(row) for row in rows]
        def write_costs(self, updates: list, hours=None) -> None:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.executemany(
                        """UPDATE usage_events
                           SET est_cost_usd=?,
                               cache_write_5m=COALESCE(?, cache_write_5m),
                               cache_write_1h=COALESCE(?, cache_write_1h)
                           WHERE tool=? AND session_id=? AND message_id=?""",
                        updates,
                    )
                    if hours is None:
                        hours = {
                            _hour(row["ts"])
                            for row in self._conn.execute(
                                "SELECT ts FROM usage_events WHERE ts IS NOT NULL"
                            )
                        }
                    for hour in hours:
                        self._rebuild_hour(hour)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                self._note_view_write()
                self._usage_fold_epoch += 1
        def _hours_for_run(self, run_id: str) -> set[int]:
            rows = self._conn.execute(
                "SELECT ts FROM usage_events WHERE run_id=? AND ts IS NOT NULL", (run_id,)
            ).fetchall()
            hours = {_hour(row["ts"]) for row in rows}
            for row in self._conn.execute(
                "SELECT hour_ts FROM usage_rollup_1h WHERE run_id=?", (run_id,)
            ).fetchall():
                hours.add(int(row["hour_ts"]))
            return hours
        def _rollup_bundles_locked(self, run_ids: list | None = None) -> dict:
            """Stored rollup bundles by run. Caller holds the lock.

            ``run_ids`` narrows to those runs (batched refolds); None
            reads the whole table (full folds). Returns
            ``{run_id: [row, ...]}``; empty when nothing rolled yet.
            """
            if run_ids is not None:
                run_ids = list(run_ids)
                if not run_ids:
                    return {}
                placeholders = ",".join("?" for _ in run_ids)
                rows = self._conn.execute(
                    "SELECT * FROM usage_rollup_1h WHERE run_id IN (%s)" % placeholders,
                    run_ids,
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM usage_rollup_1h").fetchall()
            out: dict = {}
            for row in rows:
                out.setdefault(row["run_id"], []).append(row)
            return out
        def _move_rollup_locked(self, src_id: str, dst_id: str, hours: set | None = None, ws_split: str | None = None) -> None:
            """Move stored bundles from one run to another. Caller holds the lock.

            Run merges and lead-slice moves rename usage rows; bundles
            follow in the same transaction so the fold keeps seeing
            them. On key conflict the bundles merge by addition (token
            sums are exact; costs add like every other fold combine).
            ``hours``/``ws_split`` narrow the move to the rows a slice
            move actually renamed.
            """
            if hours is not None and not hours:
                return
            sql = "SELECT * FROM usage_rollup_1h WHERE run_id=?"
            args: list = [src_id]
            if hours is not None:
                placeholders = ",".join("?" for _ in hours)
                sql += " AND hour_ts IN (%s)" % placeholders
                args.extend(hours)
            if ws_split is not None:
                sql += " AND ws_split=?"
                args.append(ws_split or "")
            rows = self._conn.execute(sql, args).fetchall()
            for row in rows:
                self._conn.execute(
                    """INSERT INTO usage_rollup_1h(
                           run_id, hour_ts, model, tool, ws_split, n_events,
                           input_tokens, output_tokens, cache_read, cache_write,
                           reasoning, missing, unpriced, cost_usd)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id, hour_ts, model, tool, ws_split) DO UPDATE SET
                         n_events = n_events + excluded.n_events,
                         input_tokens = input_tokens + excluded.input_tokens,
                         output_tokens = output_tokens + excluded.output_tokens,
                         cache_read = cache_read + excluded.cache_read,
                         cache_write = cache_write + excluded.cache_write,
                         reasoning = CASE
                           WHEN reasoning IS NULL AND excluded.reasoning IS NULL THEN NULL
                           ELSE COALESCE(reasoning, 0) + COALESCE(excluded.reasoning, 0) END,
                         missing = (missing OR excluded.missing),
                         unpriced = (unpriced OR excluded.unpriced),
                         cost_usd = CASE
                           WHEN (unpriced OR excluded.unpriced) THEN NULL
                           ELSE COALESCE(cost_usd, 0) + COALESCE(excluded.cost_usd, 0) END""",
                    (
                        dst_id, row["hour_ts"], row["model"], row["tool"], row["ws_split"],
                        row["n_events"], row["input_tokens"], row["output_tokens"],
                        row["cache_read"], row["cache_write"], row["reasoning"],
                        row["missing"], row["unpriced"], row["cost_usd"],
                    ),
                )
            self._conn.execute(
                "DELETE FROM usage_rollup_1h WHERE run_id=?" + (
                    " AND hour_ts IN (%s)" % ",".join("?" for _ in hours) if hours is not None else ""
                ) + (" AND ws_split=?" if ws_split is not None else ""),
                args,
            )
        def _rebuild_hour(self, hour_ts: int) -> None:
            self._conn.execute("DELETE FROM usage_buckets WHERE hour_ts=?", (hour_ts,))
            rows = self._conn.execute(
                """SELECT e.model AS model, e.tool AS tool, COALESCE(r.workstream, '') AS ws,
                          e.input_tokens AS input_tokens, e.output_tokens AS output_tokens,
                          e.cache_read AS cache_read, e.cache_write AS cache_write,
                          e.cache_write_5m AS cache_write_5m, e.cache_write_1h AS cache_write_1h,
                          e.reasoning AS reasoning, e.ts AS ts
                   FROM usage_events e
                   LEFT JOIN runs r ON r.run_id = e.run_id
                   WHERE e.ts >= ? AND e.ts < ? AND COALESCE(e.tool, '') != 'grok-context'""",
                (hour_ts, hour_ts + 3600),
            ).fetchall()
            # One catalog read per hour rebuild, not per event: the price
            # vectors resolve from this table object for every row below.
            table = _price_catalog()
            groups: dict = {}

            def _cell(model, ws, tool):
                return groups.setdefault(
                    (model or "", ws, tool or ""),
                    {name: 0 for name in ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning")}
                    | {"missing": 0, "unpriced": 0, "cost": 0.0},
                )

            for row in rows:
                bucket = _cell(row["model"], row["ws"], row["tool"])
                missing = any(row[name] is None for name in ("input_tokens", "output_tokens", "cache_read", "cache_write"))
                if missing:
                    bucket["missing"] = 1
                for name in ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning"):
                    if row[name] is not None:
                        bucket[name] += row[name]
                if missing:
                    continue
                priced = price_cost(
                    row["model"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cache_read"],
                    row["cache_write"],
                    table,
                    cache_write_5m=row["cache_write_5m"],
                    cache_write_1h=row["cache_write_1h"],
                    reasoning=row["reasoning"],
                    ts=row["ts"],
                )
                if priced is None:
                    bucket["unpriced"] = 1
                else:
                    bucket["cost"] += priced
            # Rolled rows for this hour fold back in with their stored
            # sums: token sums are exact; the priced part of a bundle
            # adds exactly like the live events it replaced (a bundle
            # with missing rows still carries its priced part, mirroring
            # the per-event loop above, where missing rows add tokens
            # but never cost).
            for brow in self._conn.execute(
                """SELECT b.*, COALESCE(r.workstream, '') AS ws
                   FROM usage_rollup_1h b LEFT JOIN runs r ON r.run_id = b.run_id
                   WHERE b.hour_ts=?""",
                (hour_ts,),
            ).fetchall():
                bucket = _cell(brow["model"], brow["ws"], brow["tool"])
                for name in ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning"):
                    if brow[name] is not None:
                        bucket[name] += brow[name]
                if brow["missing"]:
                    bucket["missing"] = 1
                if brow["unpriced"]:
                    bucket["unpriced"] = 1
                elif brow["cost_usd"] is not None:
                    bucket["cost"] += brow["cost_usd"]
            payload = []
            for (model, workstream, tool), bucket in groups.items():
                est = None
                if not bucket["missing"] and not bucket["unpriced"]:
                    est = bucket["cost"]
                payload.append(
                    (
                        hour_ts,
                        model,
                        workstream,
                        tool,
                        bucket["input_tokens"],
                        bucket["output_tokens"],
                        bucket["cache_read"],
                        bucket["cache_write"],
                        bucket["reasoning"],
                        bucket["missing"],
                        est,
                    )
                )
            self._conn.executemany(
                """INSERT INTO usage_buckets(
                       hour_ts, model, workstream, tool, input_tokens, output_tokens,
                       cache_read, cache_write, reasoning, missing, cost_usd)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
        def rollup_usage(self, older_than_days=14, batch_runs: int = 200, time_budget_s: float = 2.0, dry_run: bool = False, now: float | None = None) -> dict:
            """Compact old usage rows into hourly bundles, verified per run.

            Rows with ``ts`` older than the cutoff hour roll into
            ``usage_rollup_1h``: one row per (run, hour, model, tool,
            split) with exact token sums and the group cost. Only whole
            hours strictly below the cutoff roll, so hour-aligned reads
            never straddle the boundary. Estimate-context rows and
            NULL-stamp rows stay live (they cannot hour). ``N <= 0``
            disables the job (off by default behind
            ``rollup_after_days``).

            Each run rolls inside one transaction: the bundles insert
            and the source rows delete together, and a verification
            folds the run both ways (monolithic pre-image vs live
            remainder plus bundles) and rolls back on any mismatch in
            exact token sums, exact unpriced flags, or money-rounded
            cost. Buckets are untouched: rebuilt hours reproduce the
            same cells from live rows plus bundles. Small batches under
            a time budget keep this safe beside concurrent writers;
            reruns resume where the last pass stopped. Served payloads
            do not move (costs round to 6 decimals everywhere), so the
            data and view sequences stay — only the poll sequence and
            the per-run fold versions advance.
            """
            import time as _time_mod

            from .util import _hour

            if now is None:
                now = _time_mod.time()
            stats: dict = {
                "older_than_days": older_than_days,
                "cutoff": None,
                "candidate_runs": 0,
                "candidate_rows": 0,
                "rolled_runs": 0,
                "rolled_rows": 0,
                "rolled_bundles": 0,
                "failed_runs": [],
                "elapsed_s": 0.0,
                "dry_run": bool(dry_run),
            }
            try:
                days = float(older_than_days or 0)
            except (TypeError, ValueError):
                days = 0.0
            if days <= 0:
                stats["disabled"] = True
                return stats
            cutoff = int((now - days * 86400) // 3600) * 3600
            stats["cutoff"] = cutoff
            start = _time_mod.monotonic()
            with self._lock:
                candidates = [
                    row["run_id"]
                    for row in self._conn.execute(
                        """SELECT DISTINCT run_id FROM usage_events
                           WHERE ts < ? AND COALESCE(tool, '') != 'grok-context' AND run_id IS NOT NULL
                           LIMIT ?""",
                        (cutoff, int(batch_runs)),
                    ).fetchall()
                ]
                stats["candidate_runs"] = len(candidates)
                if not candidates:
                    stats["elapsed_s"] = _time_mod.monotonic() - start
                    return stats
                placeholders = ",".join("?" for _ in candidates)
                stats["candidate_rows"] = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM usage_events WHERE run_id IN (%s) AND ts < ?" % placeholders,
                    (*candidates, cutoff),
                ).fetchone()["n"]
                if dry_run:
                    stats["elapsed_s"] = _time_mod.monotonic() - start
                    return stats
                rows = self._conn.execute(
                    """SELECT tool, session_id, message_id, ts, run_id, model,
                              input_tokens, output_tokens, cache_read, cache_write,
                              cache_write_5m, cache_write_1h, reasoning, ws_split
                       FROM usage_events WHERE run_id IN (%s) AND ts < ?
                         AND COALESCE(tool, '') != 'grok-context'""" % placeholders,
                    (*candidates, cutoff),
                ).fetchall()
                groups: dict = {}
                for row in rows:
                    key = (
                        row["run_id"],
                        _hour(row["ts"]),
                        row["model"] or "",
                        row["tool"] or "",
                        row["ws_split"] if "ws_split" in row.keys() else "",
                    )
                    groups.setdefault(key, []).append(row)
                table = None
                try:
                    from dash.pricing import catalog as _catalog

                    table = _catalog()
                except Exception:
                    pass
                for run_id in candidates:
                    if _time_mod.monotonic() - start > time_budget_s:
                        break
                    run_groups = {key: members for key, members in groups.items() if key[0] == run_id}
                    if not run_groups:
                        continue
                    ok = self._rollup_run_locked(run_id, run_groups, cutoff, table)
                    if ok:
                        stats["rolled_runs"] += 1
                        stats["rolled_rows"] += sum(len(members) for members in run_groups.values())
                        stats["rolled_bundles"] += len(run_groups)
                    else:
                        stats["failed_runs"].append(run_id)
                stats["elapsed_s"] = _time_mod.monotonic() - start
            return stats
        def _rollup_run_locked(self, run_id: str, run_groups: dict, cutoff: int, table) -> bool:
            """Roll one run's old groups with verification. Caller holds the lock."""
            # The pre-image folds inside the write transaction, so a
            # concurrent write cannot slip between the two folds and
            # fail verification spuriously.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                live = [
                    dict(row)
                    for row in self._conn.execute(
                        """SELECT model, input_tokens, output_tokens, cache_read, cache_write,
                                  cache_write_5m, cache_write_1h, reasoning, ts
                           FROM usage_events WHERE run_id=? AND COALESCE(tool, '') != 'grok-context'""",
                        (run_id,),
                    ).fetchall()
                ]
                pre = _fold_events(live, _table=table)
                for key, members in run_groups.items():
                    _, hour_ts, model, tool, ws_split = key
                    folded, cost, unpriced = _fold_events(members, _table=table)
                    missing = any(
                        member[column] is None
                        for member in members
                        for column in ("input_tokens", "output_tokens", "cache_read", "cache_write")
                    )
                    self._conn.execute(
                        """INSERT INTO usage_rollup_1h(
                               run_id, hour_ts, model, tool, ws_split, n_events,
                               input_tokens, output_tokens, cache_read, cache_write,
                               reasoning, missing, unpriced, cost_usd)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(run_id, hour_ts, model, tool, ws_split) DO UPDATE SET
                             n_events = n_events + excluded.n_events,
                             input_tokens = input_tokens + excluded.input_tokens,
                             output_tokens = output_tokens + excluded.output_tokens,
                             cache_read = cache_read + excluded.cache_read,
                             cache_write = cache_write + excluded.cache_write,
                             reasoning = CASE
                               WHEN reasoning IS NULL AND excluded.reasoning IS NULL THEN NULL
                               ELSE COALESCE(reasoning, 0) + COALESCE(excluded.reasoning, 0) END,
                             missing = (missing OR excluded.missing),
                             unpriced = (unpriced OR excluded.unpriced),
                             cost_usd = CASE
                               WHEN (unpriced OR excluded.unpriced) THEN NULL
                               ELSE COALESCE(cost_usd, 0) + COALESCE(excluded.cost_usd, 0) END""",
                        _bundle_from_fold(
                            run_id, hour_ts, model, tool, ws_split,
                            folded, cost, unpriced, len(members), missing,
                        ),
                    )
                self._conn.execute(
                    "DELETE FROM usage_events WHERE run_id=? AND ts < ? AND COALESCE(tool, '') != 'grok-context'",
                    (run_id, cutoff),
                )
                remaining = [
                    dict(row)
                    for row in self._conn.execute(
                        """SELECT model, input_tokens, output_tokens, cache_read, cache_write,
                                  cache_write_5m, cache_write_1h, reasoning, ts
                           FROM usage_events WHERE run_id=? AND COALESCE(tool, '') != 'grok-context'""",
                        (run_id,),
                    ).fetchall()
                ]
                post_live = _fold_events(remaining, _table=table)
                post_bundles = self._rollup_bundles_locked([run_id]).get(run_id, [])
                post = _merge_rolls(
                    [post_live] + [_bundle_triple(bundle) for bundle in post_bundles]
                )
                if not _fold_equal(pre, post):
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                return False
            self._bump_usage_run(run_id)
            self._seq += 1
            return True
        def db_rollup_stats(self, now: float | None = None) -> dict:
            """Database size, row counts, and rollup coverage for ``db stats``."""
            import time as _time_mod

            if now is None:
                now = _time_mod.time()
            with self._lock:
                live_rows = self._conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"] or 0
                bundle_rows = self._conn.execute("SELECT COUNT(*) AS n FROM usage_rollup_1h").fetchone()["n"] or 0
                bundled_events = self._conn.execute("SELECT COALESCE(SUM(n_events), 0) AS n FROM usage_rollup_1h").fetchone()["n"] or 0
                runs = self._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] or 0
                buckets = self._conn.execute("SELECT COUNT(*) AS n FROM usage_buckets").fetchone()["n"] or 0
                span = self._conn.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM usage_events WHERE ts IS NOT NULL").fetchone()
                roll_span = self._conn.execute("SELECT MIN(hour_ts) AS lo, MAX(hour_ts) AS hi FROM usage_rollup_1h").fetchone()
            try:
                size_mb = _os.path.getsize(self.path) / (1024 * 1024)
            except Exception:
                size_mb = None
            return {
                "size_mb": size_mb,
                "runs": runs,
                "usage_event_rows": live_rows,
                "rollup_bundle_rows": bundle_rows,
                "rolled_events": bundled_events,
                "buckets": buckets,
                "live_ts_min": span["lo"],
                "live_ts_max": span["hi"],
                "rolled_hour_min": roll_span["lo"],
                "rolled_hour_max": roll_span["hi"],
            }
        def _usage_by_run(self) -> dict:
            """Folded billed usage per run, refolding only written runs.

            Every usage write bumps a per-run version (bulk rewrites
            bump a global epoch instead), so a write to one run
            refolds only that run's events instead of all 100k rows.
            Callers hold the facade lock. Returns a shallow copy,
            because the rollup reassigns entries. ``_usage_cache``
            keeps its old ``(data_seq, folds)`` shape for readers.
            """
            if self._usage_fold_built_epoch == self._usage_fold_epoch:
                dirty = [
                    run_id
                    for run_id, version in self._usage_run_versions.items()
                    if self._usage_fold_versions.get(run_id) != version
                ]
                if dirty:
                    self._refold_runs_locked(dirty)
                    for run_id in dirty:
                        self._usage_fold_versions[run_id] = self._usage_run_versions.get(run_id, 0)
                    _perf.record_cache("usage_fold", "incremental")
                else:
                    _perf.record_cache("usage_fold", "hit")
            else:
                out = self._usage_by_run_uncached()
                self._usage_folds = dict(out)
                self._usage_fold_versions = dict(self._usage_run_versions)
                self._usage_fold_built_epoch = self._usage_fold_epoch
                _perf.record_cache("usage_fold", "full")
            self._usage_cache = (self._data_seq, self._usage_folds)
            return dict(self._usage_folds)
        def _refold_runs_locked(self, run_ids: list) -> None:
            """Refold exactly ``run_ids`` in place. Caller holds the lock.

            Runs with no remaining events leave the fold (a merge moved
            their rows away). One shared catalog and memo per batch.
            """
            from dash.pricing import catalog as _catalog
            from .runs import _merge_usage
            table = _catalog()
            memo: dict = {}
            for pos in range(0, len(run_ids), 500):
                chunk = run_ids[pos:pos + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    """SELECT run_id, model, input_tokens, output_tokens, cache_read, cache_write,
                              cache_write_5m, cache_write_1h, reasoning, ts
                       FROM usage_events WHERE run_id IN (%s) AND COALESCE(tool, '') != 'grok-context'"""
                    % placeholders,
                    chunk,
                ).fetchall()
                grouped: dict = {}
                for row in rows:
                    grouped.setdefault(row["run_id"], []).append(row)
                bundles = self._rollup_bundles_locked(chunk)
                for run_id in chunk:
                    events = grouped.get(run_id)
                    triples = []
                    if events:
                        triples.append(_fold_events(events, _table=table, _memo=memo))
                    triples.extend(_bundle_triple(bundle) for bundle in bundles.get(run_id, []))
                    if not triples:
                        self._usage_folds.pop(run_id, None)
                        continue
                    self._usage_folds[run_id] = _merge_rolls(triples)
        def _usage_by_run_uncached(self) -> dict:
            rows = self._conn.execute(
                """SELECT run_id, model, input_tokens, output_tokens, cache_read, cache_write,
                          cache_write_5m, cache_write_1h, reasoning, ts
                   FROM usage_events WHERE run_id IS NOT NULL AND COALESCE(tool, '') != 'grok-context'"""
            ).fetchall()
            grouped: dict = {}
            for row in rows:
                grouped.setdefault(row["run_id"], []).append(row)
            # One catalog read and one price memo per scan; _fold_events
            # prices per price shape instead of per event, so repeated
            # shapes share one Decimal computation with no change in
            # output. This keeps the single scan linear in rows, not in
            # pricing.
            from dash.pricing import catalog as _catalog

            table = _catalog()
            memo: dict = {}
            out = {}
            bundles = self._rollup_bundles_locked()
            for run_id, events in grouped.items():
                triples = [_fold_events(events, _table=table, _memo=memo)]
                triples.extend(_bundle_triple(bundle) for bundle in bundles.get(run_id, []))
                out[run_id] = _merge_rolls(triples)
            for run_id, run_bundles in bundles.items():
                if run_id in out:
                    continue
                out[run_id] = _merge_rolls([_bundle_triple(bundle) for bundle in run_bundles])
            return out
        def phase_events(
            self,
            since: float | None = None,
            until: float | None = None,
        ) -> tuple[list[dict], list[dict]]:
            """``(run rows, usage events)`` for the phases fold, one lock pass.

            Events drop the ``grok-context`` estimate tool, exactly like
            the hourly buckets and the per-run fold, so phase sums match
            the bucket totals. ``since``/``until`` bound event ``ts``
            (NULL stamps never match a bound, as in the bucket rebuild);
            runs always come back whole for classification context.
            """
            with self._lock:
                runs = [dict(row) for row in self._all_runs()]
                sql = """SELECT run_id, ts, model, input_tokens, output_tokens,
                                cache_read, cache_write, reasoning,
                                cache_write_5m, cache_write_1h
                         FROM usage_events WHERE COALESCE(tool, '') != 'grok-context'"""
                args: list = []
                if since is not None:
                    sql += " AND ts >= ?"
                    args.append(since)
                if until is not None:
                    sql += " AND ts < ?"
                    args.append(until)
                events = [dict(row) for row in self._conn.execute(sql, args).fetchall()]
                # Rolled bundles rejoin as pre-folded pseudo-events: the
                # phases fold partitions on ``_rolled`` and merges their
                # stored sums and cost instead of repricing summed tokens
                # (a sum can cross a tier threshold its parts never
                # touched, so repricing would invent money).
                roll_sql = "SELECT run_id, CAST(hour_ts AS REAL) AS ts, model, tool, input_tokens, output_tokens, cache_read, cache_write, reasoning, cost_usd, unpriced, missing, n_events FROM usage_rollup_1h"
                roll_args: list = []
                roll_cond: list = []
                if since is not None:
                    roll_cond.append("hour_ts >= ?")
                    roll_args.append(int(float(since) // 1))
                if until is not None:
                    roll_cond.append("hour_ts < ?")
                    roll_args.append(int(float(until) // 1))
                if roll_cond:
                    roll_sql += " WHERE " + " AND ".join(roll_cond)
                for brow in self._conn.execute(roll_sql, roll_args).fetchall():
                    pseudo = dict(brow)
                    pseudo["_rolled"] = 1
                    events.append(pseudo)
            return runs, events
        def by_phase_all_time(self) -> dict:
            """``{ws: {phase: {tokens, cost_usd}}}`` over rolled-up all-time usage.

            Child-chat usage folds onto its parent run (the same
            ``_parent_map``/``_rollup_usage`` pair the snapshot uses) and
            the owner's phase takes it all. Unpriced runs contribute
            tokens but no cost, like the bucket sums. Classification
            reads the live rules files (cached by mtime); callers key
            their caches on :func:`dash.phases.current_hash`.
            """
            from dash import phases as phases_mod

            from .runs import _parent_map, _rollup_usage
            from .util import BILLED_FIELDS, UNASSIGNED, _money

            with self._lock:
                raw = [dict(row) for row in self._all_runs()]
                rolled = _rollup_usage(dict(self._usage_by_run()), _parent_map([dict(row) for row in raw]))
            by_id = {row["run_id"]: row for row in raw if row.get("run_id")}
            ws_ids = sorted({str(row.get("workstream") or UNASSIGNED) for row in raw if row.get("run_id")})
            plan_tasks = phases_mod.plan_tasks_for(ws_ids)
            try:
                from dash.taskmatch import load_taskmap

                taskmap = load_taskmap()
            except Exception:
                taskmap = {}
            rules, _digest, _version = phases_mod.load_rules(local=phases_mod.local_path())
            try:
                from dash.bundles import plans_stamp as _plans_stamp

                stamp = _plans_stamp()
            except Exception:
                stamp = None
            import hashlib as _hashlib
            import json as _json

            try:
                taskmap_hash = _hashlib.sha256(
                    _json.dumps(taskmap, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:16]
            except (TypeError, ValueError):
                taskmap_hash = None
            phase_of = self._classify_runs_cached(
                raw, taskmap, plan_tasks, rules, (_digest, taskmap_hash, stamp),
                limit=set(rolled),
            )
            out: dict = {}
            for owner_run, entry in rolled.items():
                folded, cost, _unpriced = entry
                row = by_id.get(owner_run)
                if row is None:
                    continue
                ws = str(row.get("workstream") or UNASSIGNED)
                phase, _rule = phase_of.get(owner_run, ("unclassified", "none"))
                if phase not in phases_mod.PHASES:
                    phase = "unclassified"
                bucket = out.setdefault(
                    ws, {item: {"tokens": 0, "cost_usd": 0.0} for item in phases_mod.PHASES}
                )
                bucket[phase]["tokens"] += sum(int(folded.get(part) or 0) for part in BILLED_FIELDS)
                if cost is not None:
                    bucket[phase]["cost_usd"] += cost
            for ws in out:
                for phase in out[ws]:
                    out[ws][phase]["cost_usd"] = _money(out[ws][phase]["cost_usd"])
            return out
        def _classify_runs_cached(self, raw, taskmap, plan_tasks, rules, inputs_key, limit=None) -> dict:
            """``{run_id: (phase, rule)}`` reusing per-run results.

            Cache entries carry their ``inputs_key`` (rules hash,
            taskmap content, plan stamp), and each run's fingerprint
            covers every classification input (label, source, parent,
            start, task id, planning-window edge). Unchanged runs skip
            ``task_of`` and the keyword scans entirely; a rules, taskmap
            or plan edit retags through the key. Entries are
            self-validating, so concurrent readers need no lock and a
            key change only trims memory, never correctness.

            ``limit`` scopes the classification pass to the ids whose
            phase is actually read (the rollup owners: a child chat's
            own phase never leaves this map). The planning-window edge
            still resolves over every run, so limits never move it.
            """
            from dash import phases as phases_mod

            with self._lock:
                if self._phase_inputs_key != inputs_key:
                    self._phase_inputs_key = inputs_key
                    self._phase_task_cache = {}
                    self._phase_class_cache = {}
                task_cache = self._phase_task_cache
                class_cache = self._phase_class_cache
            tasks = plan_tasks or {}
            resolved: dict = {}
            first_task_start: dict = {}
            for run in raw or []:
                if not isinstance(run, dict):
                    continue
                rid = run.get("run_id")
                if not rid:
                    continue
                ws = run.get("workstream")
                ids = tasks.get(ws) if isinstance(tasks, dict) else None
                ids = tuple(ids) if ids else ()
                label = run.get("label")
                tkey = (label if isinstance(label, str) else None, ws, ids)
                hit = task_cache.get(tkey)
                if hit is not None and hit[0] == inputs_key:
                    tid = hit[1]
                else:
                    try:
                        tid = phases_mod.resolve_task(label, ws, taskmap, ids)
                    except Exception:
                        tid = None
                    task_cache[tkey] = (inputs_key, tid)
                resolved[rid] = tid
                if tid is not None:
                    try:
                        start = float(run.get("started_at")) if run.get("started_at") is not None else None
                    except (TypeError, ValueError):
                        start = None
                    if start is not None and isinstance(ws, str) and ws:
                        edge = first_task_start.get(ws)
                        if edge is None or start < edge:
                            first_task_start[ws] = start
            out = {}
            for run in raw or []:
                if not isinstance(run, dict) or not run.get("run_id"):
                    continue
                rid = run["run_id"]
                if limit is not None and rid not in limit:
                    continue
                ws = run.get("workstream")
                edge = first_task_start.get(ws) if isinstance(ws, str) else None
                try:
                    start = float(run.get("started_at")) if run.get("started_at") is not None else None
                except (TypeError, ValueError):
                    start = None
                in_window = edge is not None and start is not None and start < edge
                tid = resolved.get(rid)
                label = run.get("label")
                source = run.get("source")
                parent = run.get("parent_session_id")
                fingerprint = (label, source, parent, start, ws, tid, in_window)
                hit = class_cache.get(rid)
                if hit is not None and hit[0] == inputs_key and hit[1] == fingerprint:
                    out[rid] = hit[2]
                    continue
                try:
                    phase_rule = phases_mod.classify_run(
                        run, {"task_id": tid, "in_window": in_window}, rules=rules
                    )
                except Exception:
                    phase_rule = ("unclassified", "none")
                class_cache[rid] = (inputs_key, fingerprint, phase_rule)
                out[rid] = phase_rule
            return out
        def _context_by_run(self) -> dict:
            """Latest grok-context input per run. Caller holds the lock.

            These rows are a context-window estimate. They stay out of totals.
            """
            rows = self._conn.execute(
                """SELECT run_id, input_tokens FROM usage_events
                   WHERE tool=? AND run_id IS NOT NULL
                   ORDER BY ts ASC, message_id ASC""",
                (_ESTIMATE_TOOL,),
            ).fetchall()
            latest = {}
            for row in rows:
                if row["input_tokens"] is None:
                    continue
                latest[row["run_id"]] = int(row["input_tokens"])
            return latest


def _fold_events(events: list, _table=None, _memo: dict | None = None) -> tuple[dict, float | None, bool]:
    """Fold token sums plus one cost, pricing per price shape not per event.

    Events sharing ``(model, time band, tier side, cache-write split)``
    price linearly in their token sums, so one ``Decimal`` computation
    per shape replaces one per event (about 30x fewer on a large
    database). Every per-event rule of :func:`dash.pricing.cost` is
    preserved shape-by-shape: a missing billed token, a cache-read
    over-bill, a split mismatch, or a missing reasoning token poisons
    only its own shape, and the run-level unpriced rule matches the
    per-event fold exactly. ``_memo`` still caches priced shapes across
    calls when given.
    """
    from decimal import Decimal as _Decimal

    from dash.pricing import _MILLION
    folded = {}
    for key, column in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cache_read", "cache_read"),
        ("cache_write", "cache_write"),
        ("reasoning", "reasoning"),
    ):
        values = [event[column] for event in events]
        if values and all(value is None for value in values):
            folded[key] = None
        else:
            folded[key] = sum(value or 0 for value in values)
    if not events:
        return folded, 0.0, False
    table = _price_catalog() if _table is None else _table
    groups: dict = {}
    rows: dict = {}
    unpriced = False
    for event in events:
        model = event["model"]
        inp = event["input_tokens"]
        outp = event["output_tokens"]
        cred = event["cache_read"]
        cwr = event["cache_write"]
        five = event["cache_write_5m"]
        one = event["cache_write_1h"]
        rea = event["reasoning"]
        billed = inp is not None or outp is not None or cred is not None or cwr is not None
        if inp is None or outp is None or cred is None or cwr is None:
            # :func:`cost` returns None for the event; only an event
            # with billed tokens marks the run unpriced.
            if billed:
                unpriced = True
            continue
        try:
            row = rows[model]
        except KeyError:
            row = rows[model] = _price_lookup(model, table)
        except TypeError:
            row = _price_lookup(model, table)
        if row is None or row.get("input") is None or row.get("output") is None:
            if billed:
                unpriced = True
            continue
        ts = event["ts"] if "ts" in event.keys() else None
        if isinstance(row.get("time_bands"), dict):
            if ts is None:
                band: str | None = _band_cached(table, model, None)
            else:
                try:
                    band = _band_cached(table, model, int(float(ts) // 3600))
                except (TypeError, ValueError, OverflowError, OSError):
                    band = "peak"
        else:
            band = None
        prompt = _price_prompt_len(row, inp, cred, cwr)
        over = _tier_over(row, prompt)
        key = (model, band, over, five, one)
        acc = groups.get(key)
        if acc is None:
            acc = groups[key] = {
                "n": 0, "inp": 0, "outp": 0, "cred": 0, "cwr": 0,
                "five": 0, "one": 0, "rea": 0, "rea_none": False,
                "bad": False, "billed": False, "includes": bool(row.get("input_includes_cache_read")),
            }
        acc["n"] += 1
        acc["billed"] = True
        acc["inp"] += inp
        acc["outp"] += outp
        acc["cred"] += cred
        acc["cwr"] += cwr
        if five is not None:
            acc["five"] += five
        if one is not None:
            acc["one"] += one
        if rea is None:
            acc["rea_none"] = True
        else:
            acc["rea"] += rea
        if acc["includes"] and cred > inp:
            acc["bad"] = True
            unpriced = True
        elif five is not None or one is not None:
            five_n = 0 if five is None else five
            one_n = 0 if one is None else one
            if five_n + one_n != cwr:
                acc["bad"] = True
                unpriced = True
    costs = []
    for key, acc in groups.items():
        model, band, over, five, one = key
        if acc["bad"]:
            costs.append(None)
            continue
        memo_key = (
            model, band, over, five, one,
            acc["inp"], acc["outp"], acc["cred"], acc["cwr"],
            acc["rea"], acc["rea_none"], acc["five"], acc["one"],
        )
        if _memo is not None and memo_key in _memo:
            costs.append(_memo[memo_key])
            if _memo[memo_key] is None and acc["billed"]:
                unpriced = True
            continue
        rates = _rate_vector(table, model, band, over)
        if rates.get("reasoning") is not None and acc["rea_none"]:
            if _memo is not None:
                _memo[memo_key] = None
            costs.append(None)
            unpriced = True
            continue
        bill_input = acc["inp"] - acc["cred"] if acc["includes"] else acc["inp"]
        total = (
            _Decimal(bill_input) * rates["input"]
            + _Decimal(acc["outp"]) * rates["output"]
            + _Decimal(acc["cred"]) * rates["cache_read"]
        ) / _MILLION
        # Summed splits: every event in the shape passed the
        # per-event split check during accumulation, so the sums
        # satisfy it exactly when the shape does, and the charge is
        # linear in the split sums. Unsplit shapes price the summed
        # cache-write exactly like the per-event single-rate path.
        if five is None and one is None:
            write = _write_charge(rates, acc["cwr"], None, None)
        else:
            write = _write_charge(rates, acc["cwr"], acc["five"], acc["one"])
        if write is None:
            if _memo is not None:
                _memo[memo_key] = None
            costs.append(None)
            if acc["billed"]:
                unpriced = True
            continue
        total += write
        if rates.get("reasoning") is not None:
            total += _Decimal(acc["rea"]) * rates["reasoning"] / _MILLION
        priced = float(total)
        if _memo is not None:
            _memo[memo_key] = priced
        costs.append(priced)
    if unpriced or all(item is None for item in costs):
        return folded, None, True
    return folded, sum(item for item in costs if item is not None), False


_ROLLUP_TOKEN_COLS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read"),
    ("cache_write", "cache_write"),
    ("reasoning", "reasoning"),
)


def _bundle_triple(bundle) -> tuple[dict, float | None, bool]:
    """A stored rollup row as a fold triple, combinable with live folds.

    Token sums are exact integers; the cost is the group cost the
    rollup computed with :func:`_fold_events` when the rows were live,
    so merging it with :func:`runs._merge_usage` replays the same
    arithmetic the monolithic fold would do (float sums may differ in
    the last ulp; every served aggregate rounds costs to 6 decimals).
    """
    tokens = {}
    for key, column in _ROLLUP_TOKEN_COLS:
        tokens[key] = bundle[column]
    return tokens, bundle["cost_usd"], bool(bundle["unpriced"])


def _merge_rolls(triples: list) -> tuple:
    """Merge live and bundle fold triples like one monolithic fold.

    :func:`runs._merge_usage` adds costs and ORs flags, but a merged
    ``(cost, unpriced=True)`` pair would misread downstream: every
    consumer branches on ``cost is None`` exactly like
    :func:`_fold_events` returns it (unpriced if and only if the cost
    is None). Normalizing here keeps rolled runs indistinguishable
    from never-rolled ones.
    """
    from .runs import _merge_usage

    merged = triples[0]
    for triple in triples[1:]:
        merged = _merge_usage(merged, triple)
    if merged[2]:
        merged = (merged[0], None, True)
    return merged


def _fold_equal(before: tuple, after: tuple) -> bool:
    """Rollup verification: monolithic fold vs live remainder plus bundles.

    Token sums compare exactly (integers add in any order); unpriced
    flags compare exactly; costs compare after the 6-decimal money
    rounding every served aggregate applies (float sums may differ in
    the last ulp across regroupings — the codebase's own byte-identity
    standard, set by the incremental-fold freeze).
    """
    from .util import _money

    folded_before, cost_before, unpriced_before = before
    folded_after, cost_after, unpriced_after = after
    if bool(unpriced_before) != bool(unpriced_after):
        return False
    for key in ("input", "output", "cache_read", "cache_write"):
        if int(folded_before.get(key) or 0) != int(folded_after.get(key) or 0):
            return False
    if (folded_before.get("reasoning") is None) != (folded_after.get("reasoning") is None):
        return False
    if int(folded_before.get("reasoning") or 0) != int(folded_after.get("reasoning") or 0):
        return False
    if cost_before is None or cost_after is None:
        return (cost_before is None) == (cost_after is None)
    return _money(cost_before) == _money(cost_after)


def _bundle_from_fold(run_id: str, hour_ts: int, model: str, tool: str, ws_split: str, folded: dict, cost, unpriced: bool, n_events: int, missing: bool) -> tuple:
    """Pack a :func:`_fold_events` result into a rollup row tuple."""
    return (
        run_id,
        hour_ts,
        model or "",
        tool or "",
        ws_split or "",
        n_events,
        int(folded.get("input") or 0),
        int(folded.get("output") or 0),
        int(folded.get("cache_read") or 0),
        int(folded.get("cache_write") or 0),
        folded.get("reasoning"),
        1 if missing else 0,
        1 if unpriced else 0,
        cost,
    )
