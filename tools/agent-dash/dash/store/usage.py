"""Usage events, hourly buckets, pricing, usage by run.

Owns usage events, process samples, hourly buckets, pricing
application (via ``dash.pricing``), and per-run usage folds.
Public methods run under the facade lock; ``_locked`` helpers
assume it and never take it.
"""

from __future__ import annotations

from dash.attribution import clean_label
from dash.models import model_key
from dash.pricing import cost as price_cost
from dash.pricing import series_key
from dash.pricing import status as price_status

from .util import (_ESTIMATE_TOOL, RANGE_S, SAMPLE_KEEP_S,
                 TOKEN_FIELDS, UNASSIGNED, WS_STATUSES,
                 _hour, _money, _opt_int, _same)


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
            )
            with self._lock:
                if isinstance(run_id, str) and ":cursor/" in run_id:
                    run_id = self._resolve_alias_locked(run_id)
                previous = self._one(
                    """SELECT ts FROM usage_events
                       WHERE tool=? AND session_id=? AND message_id=?""",
                    (tool, session_id, message_id),
                )
                current = self._one(
                    """SELECT ts, run_id, model, input_tokens, output_tokens, cache_read,
                              cache_write, cache_write_5m, cache_write_1h, reasoning, est_cost_usd
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
                }
                if current and _same(current, incoming, incoming.keys()):
                    return
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        """INSERT INTO usage_events(
                               tool, session_id, message_id, ts, run_id, model,
                               input_tokens, output_tokens, cache_read, cache_write,
                               cache_write_5m, cache_write_1h, reasoning, est_cost_usd)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(tool, session_id, message_id) DO UPDATE SET
                             ts=excluded.ts, run_id=excluded.run_id, model=excluded.model,
                             input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                             cache_read=excluded.cache_read, cache_write=excluded.cache_write,
                             cache_write_5m=excluded.cache_write_5m, cache_write_1h=excluded.cache_write_1h,
                             reasoning=excluded.reasoning, est_cost_usd=excluded.est_cost_usd""",
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
        def add_process_sample(self, row: dict) -> None:
            ts = float(row["ts"])
            with self._lock:
                run_id = row.get("run_id")
                if isinstance(run_id, str) and ":cursor/" in run_id:
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
            with self._lock:
                rows = self._conn.execute(
                    """SELECT model AS model, COUNT(*) AS rows,
                              SUM(input_tokens) AS input, SUM(output_tokens) AS output,
                              SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write,
                              SUM(reasoning) AS reasoning
                       FROM usage_events GROUP BY model"""
                ).fetchall()
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
            """Models with usage rows in the window and no catalog rate.

            One entry per model: ``{"model", "rows", "tokens"}``, sorted by
            model. A missing rate never becomes zero: these tokens are left
            out of cost, and health, reconcile, and the cost tile say so
            loudly. Rows with no model id are skipped (data quality, not a
            price gap).
            """
            range_since = now - RANGE_S if since is None else float(since)
            range_until = float(now if until is None else until)
            with self._lock:
                return self._unpriced_models_locked(range_since, range_until, ws)
        def _unpriced_models_locked(self, since: float, until: float, ws: str | None) -> list[dict]:
            """Windowed unpriced models. Caller holds the lock."""
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
            for row in self._conn.execute(sql, args).fetchall():
                model = row["model"]
                if not model:
                    continue
                if int(row["rows"] or 0) <= 0:
                    continue
                if price_status(model)["priced"]:
                    continue
                out.append(
                    {
                        "model": model,
                        "rows": int(row["rows"]),
                        "tokens": int(row["tokens"] or 0),
                    }
                )
            out.sort(key=lambda entry: entry["model"])
            return out
        def usage_events_for_cost(self) -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT tool, session_id, message_id, model,
                              input_tokens, output_tokens, cache_read, cache_write,
                              cache_write_5m, cache_write_1h, reasoning, est_cost_usd
                       FROM usage_events"""
                ).fetchall()
            return [dict(row) for row in rows]
        def write_costs(self, updates: list) -> None:
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
        def _hours_for_run(self, run_id: str) -> set[int]:
            rows = self._conn.execute(
                "SELECT ts FROM usage_events WHERE run_id=? AND ts IS NOT NULL", (run_id,)
            ).fetchall()
            return {_hour(row["ts"]) for row in rows}
        def _rebuild_hour(self, hour_ts: int) -> None:
            self._conn.execute("DELETE FROM usage_buckets WHERE hour_ts=?", (hour_ts,))
            rows = self._conn.execute(
                """SELECT e.model AS model, e.tool AS tool, COALESCE(r.workstream, '') AS ws,
                          e.input_tokens AS input_tokens, e.output_tokens AS output_tokens,
                          e.cache_read AS cache_read, e.cache_write AS cache_write,
                          e.cache_write_5m AS cache_write_5m, e.cache_write_1h AS cache_write_1h,
                          e.reasoning AS reasoning
                   FROM usage_events e
                   LEFT JOIN runs r ON r.run_id = e.run_id
                   WHERE e.ts >= ? AND e.ts < ? AND COALESCE(e.tool, '') != 'grok-context'""",
                (hour_ts, hour_ts + 3600),
            ).fetchall()
            groups: dict = {}
            for row in rows:
                key = (row["model"] or "", row["ws"], row["tool"] or "")
                bucket = groups.setdefault(
                    key,
                    {name: 0 for name in ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning")}
                    | {"missing": 0, "unpriced": 0, "cost": 0.0},
                )
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
                    cache_write_5m=row["cache_write_5m"],
                    cache_write_1h=row["cache_write_1h"],
                    reasoning=row["reasoning"],
                )
                if priced is None:
                    bucket["unpriced"] = 1
                else:
                    bucket["cost"] += priced
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
        def _usage_by_run(self) -> dict:
            """Folded billed usage per run, cached until the data sequence moves (any write bumps it). Returns a shallow copy, because the rollup reassigns entries."""
            cached = getattr(self, "_usage_cache", None)
            if cached is not None and cached[0] == self._data_seq:
                return dict(cached[1])
            out = self._usage_by_run_uncached()
            self._usage_cache = (self._data_seq, out)
            return dict(out)
        def _usage_by_run_uncached(self) -> dict:
            rows = self._conn.execute(
                """SELECT run_id, model, input_tokens, output_tokens, cache_read, cache_write,
                          cache_write_5m, cache_write_1h, reasoning
                   FROM usage_events WHERE run_id IS NOT NULL AND COALESCE(tool, '') != 'grok-context'"""
            ).fetchall()
            grouped: dict = {}
            for row in rows:
                grouped.setdefault(row["run_id"], []).append(row)
            # One catalog read and one price memo per scan: the same
            # (model, token) tuple prices identically wherever it appears, so
            # repeated events share one Decimal computation with no change in
            # output. This keeps the single scan linear in rows, not in pricing.
            from dash.pricing import catalog as _catalog

            table = _catalog()
            memo: dict = {}
            out = {}
            for run_id, events in grouped.items():
                out[run_id] = _fold_events(events, _table=table, _memo=memo)
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
    costs = []
    unpriced = False
    for event in events:
        memo_key = (
            event["model"],
            event["input_tokens"],
            event["output_tokens"],
            event["cache_read"],
            event["cache_write"],
            event["cache_write_5m"],
            event["cache_write_1h"],
            event["reasoning"],
        )
        if _memo is not None and memo_key in _memo:
            priced = _memo[memo_key]
        else:
            priced = price_cost(
                event["model"],
                event["input_tokens"],
                event["output_tokens"],
                event["cache_read"],
                event["cache_write"],
                cache_write_5m=event["cache_write_5m"],
                cache_write_1h=event["cache_write_1h"],
                reasoning=event["reasoning"],
                table=_table,
            )
            if _memo is not None:
                _memo[memo_key] = priced
        costs.append(priced)
        if priced is None and any(event[name] is not None for name in ("input_tokens", "output_tokens", "cache_read", "cache_write")):
            unpriced = True
    if not events:
        return folded, 0.0, False
    if unpriced or all(item is None for item in costs):
        return folded, None, True
    return folded, sum(item for item in costs if item is not None), False
