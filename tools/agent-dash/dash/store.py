"""SQLite WAL store: runs, usage, samples, sessions, offsets, hourly buckets."""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from dash.attribution import attribute, clean_label
from dash.pricing import cost as price_cost
from dash.pricing import series_key

CAP_KEYS = ("grok", "muse", "glm", "claude")
DEFAULT_CAPS = {"grok": 10, "muse": 10, "glm": 5, "claude": None}
DEFAULT_LONG_RUNNING_S = {
    "haiku": 20 * 60,
    "sonnet": 45 * 60,
    "muse": 45 * 60,
    "grok": 45 * 60,
    "glm": 30 * 60,
}
COMM_TO_CAP = {
    "cursor-agent": "grok",
    "muse": "muse",
    "muse-bin": "muse",
    "grok": "glm",
    "claude": "claude",
}
SAMPLE_FRESH_S = 20
RECENT_S = 3600
SAMPLE_KEEP_S = 48 * 3600
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")
BILLED_FIELDS = ("input", "output", "cache_read", "cache_write")

RUN_COLS = (
    "run_id",
    "source",
    "tier",
    "model",
    "label",
    "session_id",
    "parent_session_id",
    "workstream",
    "ws_confidence",
    "status",
    "verdict",
    "started_at",
    "ended_at",
    "clock_source",
    "out_bytes",
    "out_growing",
    "err_bytes",
    "pid",
    "out_path",
)
RUN_INPUT = (
    "source",
    "tier",
    "model",
    "label",
    "session_id",
    "parent_session_id",
    "status",
    "verdict",
    "started_at",
    "ended_at",
    "clock_source",
    "out_bytes",
    "err_bytes",
    "pid",
    "out_path",
)
SESSION_COLS = (
    "session_id",
    "tool",
    "cwd",
    "started_at",
    "status",
    "parent_session_id",
    "label",
    "workstream",
    "ws_confidence",
)
WS_STATUSES = ("backlog", "todo", "doing", "review", "done", "cancelled")
_WF = re.compile(r"(wf_[A-Za-z0-9-]+)")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=5
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._seq = 0
        self._data_seq = 0
        self._create()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def data_seq(self) -> int:
        return self._data_seq

    def get_offset(self, path: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT inode, offset FROM file_offsets WHERE path=?", (path,)
            ).fetchone()
        if row is None:
            return None
        return (row["inode"], row["offset"])

    def set_offset(self, path: str, inode: int, offset: int, size: int, mtime: float) -> None:
        with self._lock:
            previous = self.get_offset(path)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO file_offsets(path, inode, offset, size, mtime)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                         inode=excluded.inode, offset=excluded.offset,
                         size=excluded.size, mtime=excluded.mtime""",
                    (path, int(inode), int(offset), int(size), float(mtime)),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            if previous != (int(inode), int(offset)):
                self._data_seq += 1
                self._seq += 1

    def upsert_session(self, row: dict) -> None:
        session_id = row.get("session_id")
        if not session_id:
            raise ValueError("session_id required")
        with self._lock:
            existing = self._one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
            merged = dict(existing or {})
            merged["session_id"] = session_id
            for key in ("tool", "cwd", "started_at", "status", "parent_session_id", "label"):
                if key in row:
                    merged[key] = row[key]
            merged["label"] = clean_label(merged.get("label"))
            workstream, confidence = attribute(
                merged.get("label"),
                merged.get("session_id"),
                parent_session_id=merged.get("parent_session_id"),
            )
            merged["workstream"] = workstream
            merged["ws_confidence"] = confidence
            if existing and _same(existing, merged, SESSION_COLS):
                return
            self._write_session(merged)
            self._seq += 1
            self._data_seq += 1

    def upsert_run(self, row: dict) -> None:
        run_id = row.get("run_id")
        if not run_id:
            raise ValueError("run_id required")
        with self._lock:
            existing = self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
            merged = dict(existing or {})
            merged["run_id"] = run_id
            for key in RUN_INPUT:
                if key in row:
                    merged[key] = row[key]
            merged["label"] = clean_label(merged.get("label"))
            if not merged.get("status"):
                merged["status"] = "unknown"
            workstream, confidence = attribute(
                merged.get("label"),
                merged.get("session_id"),
                parent_session_id=merged.get("parent_session_id"),
            )
            merged["workstream"] = workstream
            merged["ws_confidence"] = confidence
            if "out_bytes" in row:
                old = (existing or {}).get("out_bytes") or 0
                new = row.get("out_bytes") or 0
                merged["out_growing"] = 1 if existing and new > old else 0
            elif "out_growing" not in merged:
                merged["out_growing"] = 0
            changed_ws = not existing or existing.get("workstream") != merged.get("workstream")
            if existing and _same(existing, merged, RUN_COLS):
                return
            self._write_run(merged)
            if changed_ws:
                for hour in self._hours_for_run(run_id):
                    self._rebuild_hour(hour)
            self._seq += 1
            self._data_seq += 1

    def add_usage(self, row: dict) -> None:
        tool = row.get("tool")
        session_id = row.get("session_id")
        message_id = row.get("message_id")
        if tool is None or session_id is None or message_id is None:
            raise ValueError("tool, session_id, and message_id required")
        tool, session_id, message_id = str(tool), str(session_id), str(message_id)
        values = {key: _opt_int(row, key) for key in TOKEN_FIELDS}
        model = row.get("model")
        ts = row.get("ts")
        run_id = row.get("run_id")
        est = price_cost(model, values["input"], values["output"], values["cache_read"], values["cache_write"])
        with self._lock:
            previous = self._one(
                """SELECT ts FROM usage_events
                   WHERE tool=? AND session_id=? AND message_id=?""",
                (tool, session_id, message_id),
            )
            current = self._one(
                """SELECT ts, run_id, model, input_tokens, output_tokens, cache_read,
                          cache_write, reasoning, est_cost_usd
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
                           reasoning, est_cost_usd)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(tool, session_id, message_id) DO UPDATE SET
                         ts=excluded.ts, run_id=excluded.run_id, model=excluded.model,
                         input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                         cache_read=excluded.cache_read, cache_write=excluded.cache_write,
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

    def add_process_sample(self, row: dict) -> None:
        ts = float(row["ts"])
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO process_samples(ts, pid, comm, rss_kb, elapsed_s, label, run_id)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        ts,
                        int(row["pid"]),
                        row.get("comm"),
                        _opt_int(row, "rss_kb"),
                        row.get("elapsed_s"),
                        clean_label(row.get("label")),
                        row.get("run_id"),
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
                    "SELECT run_id FROM runs WHERE label=? AND status='running'", (text,)
                ).fetchall()
                if len(rows) == 1:
                    return rows[0]["run_id"]
        return None

    def snapshot(self, now: float, ws: str = "all", caps: dict | None = None, long_running_s: dict | None = None) -> dict:
        caps = _caps(caps)
        long_running_s = long_running_s or DEFAULT_LONG_RUNNING_S
        with self._lock:
            usage = self._usage_by_run()
            rss = self._rss_maps(now)
            runs = [self._run_view(row, now, usage, rss) for row in self._all_runs()]
            visible = [run for run in runs if _in_ws(run, ws)]
            running = [run for run in visible if run["status"] == "running"]
            running.sort(key=lambda run: run["started_at"] or 0, reverse=True)
            recent = [
                run
                for run in visible
                if run["status"] != "running"
                and run["ended_at"] is not None
                and run["ended_at"] >= now - RECENT_S
            ]
            recent.sort(key=lambda run: run["ended_at"] or 0, reverse=True)
            recent = recent[:50]
            alerts = self._alerts(visible, now, caps, long_running_s)
            totals = _totals(visible)
            totals["tokens"]["reasoning"] = sum(
                (usage.get(run["run_id"], (_zeros(), 0.0, False))[0].get("reasoning") or 0) for run in visible
            )
            by_ws = _by_workstream(visible)
            live = self._live_counts(now)
            return {
                "now": now,
                "seq": self._seq,
                "caps": {key: {"live": live.get(key, 0), "cap": caps.get(key)} for key in CAP_KEYS},
                "running": running,
                "recent": recent,
                "alerts": alerts,
                "totals": totals,
                "by_workstream": by_ws,
            }

    def query_runs(self, now: float, ws: str | None, since: float | None, until: float | None, limit: int) -> list:
        with self._lock:
            usage = self._usage_by_run()
            rss = self._rss_maps(now)
            rows = []
            for row in self._all_runs():
                view = self._run_view(row, now, usage, rss)
                if not _in_ws(view, ws):
                    continue
                if since is not None and (view["ended_at"] or now) < since:
                    continue
                if until is not None and (view["started_at"] or 0) > until:
                    continue
                rows.append(view)
        rows = _collapse_workflows(rows, now)
        rows.sort(key=lambda run: run["started_at"] or 0)
        return rows[:limit]

    def query_usage(self, group: str, bucket: str, ws: str | None, since: float | None) -> list:
        with self._lock:
            sql = """SELECT hour_ts, model, workstream, tool, input_tokens, output_tokens,
                            cache_read, cache_write, missing FROM usage_buckets"""
            args: list = []
            if since is not None:
                sql += " WHERE hour_ts + 3600 > ?"
                args.append(since)
            stored = self._conn.execute(sql, args).fetchall()
        cells: dict = {}
        for row in stored:
            if ws and ws != "all" and (row["workstream"] or None) != ws:
                continue
            if group == "model":
                key = series_key(row["model"])
            elif group == "workstream":
                key = row["workstream"] or None
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
                    "cost_usd": 0.0,
                    "priced": False,
                },
            )
            cell["input"] += row["input_tokens"] or 0
            cell["output"] += row["output_tokens"] or 0
            cell["cache_read"] += row["cache_read"] or 0
            cell["cache_write"] += row["cache_write"] or 0
            if row["missing"]:
                continue
            priced = price_cost(
                row["model"], row["input_tokens"], row["output_tokens"], row["cache_read"], row["cache_write"]
            )
            if priced is not None:
                cell["cost_usd"] += priced
                cell["priced"] = True
        out = []
        for cell in cells.values():
            cost = _money(cell["cost_usd"]) if cell["priced"] else None
            out.append(
                {
                    "t": cell["t"],
                    "key": cell["key"],
                    "input": cell["input"],
                    "output": cell["output"],
                    "cache_read": cell["cache_read"],
                    "cache_write": cell["cache_write"],
                    "cost_usd": cost,
                }
            )
        out.sort(key=lambda item: (item["t"], item["key"] or ""))
        return out

    def query_concurrency(self, now: float, ws: str | None, since: float, step: float) -> list:
        step = float(step) if step and step >= 1 else 60.0
        span = max(0.0, now - since)
        if span / step > 5000:
            step = span / 5000
        with self._lock:
            runs = [dict(row) for row in self._all_runs()]
        if ws and ws != "all":
            runs = [run for run in runs if run["workstream"] == ws]
        points = []
        count = int(span / step) + 1
        for index in range(count):
            t = since + index * step
            if t > now + 1e-6:
                break
            point = {"t": t, "grok": 0, "muse": 0, "glm": 0, "claude": 0}
            for run in runs:
                key = _tool_key(run)
                if key and _active(run, t, now):
                    point[key] += 1
            points.append(point)
        if points and (not ws or ws == "all"):
            live = self.live_counts(now)
            if any(live.values()):
                for key in CAP_KEYS:
                    points[-1][key] = live.get(key, 0)
        return points

    def query_sessions(self) -> list:
        with self._lock:
            usage = self._usage_by_run()
            runs = [dict(row) for row in self._all_runs()]
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

    def query_workstreams(self, plan_ws: set[str]) -> list:
        with self._lock:
            counts = [dict(row) for row in self._conn.execute("SELECT * FROM ws_counts").fetchall()]
            runs = [dict(row) for row in self._all_runs()]
            usage = self._usage_by_run()
        by_ws = {row["ws"]: row for row in counts}
        for run in runs:
            if run["workstream"] and run["workstream"] not in by_ws:
                by_ws[run["workstream"]] = {"ws": run["workstream"], "name": ""}
        out = []
        for ws, row in by_ws.items():
            members = [run for run in runs if run["workstream"] == ws]
            tokens = 0
            costs = []
            any_tokens = False
            running = 0
            for run in members:
                if run["status"] == "running":
                    running += 1
                folded, cost, _unpriced = usage.get(run["run_id"], ({}, 0.0, False))
                for key in BILLED_FIELDS:
                    value = folded.get(key)
                    if value:
                        tokens += value
                        any_tokens = True
                costs.append(cost)
            tk = {key: int(row.get(key) or 0) for key in WS_STATUSES}
            cost_usd = _sum_costs(costs, any_tokens)
            out.append(
                {
                    "ws": ws,
                    "name": row.get("name") or "",
                    "tk": tk,
                    "running": running,
                    "tokens": tokens,
                    "cost_usd": _money(cost_usd) if cost_usd is not None else None,
                    "plan": ws in plan_ws,
                }
            )
        out.sort(key=lambda item: _ws_sort(item["ws"]))
        return out

    def live_counts(self, now: float) -> dict:
        with self._lock:
            return self._live_counts(now)

    def _create(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              source TEXT,
              tier TEXT,
              model TEXT,
              label TEXT,
              session_id TEXT,
              parent_session_id TEXT,
              workstream TEXT,
              ws_confidence TEXT,
              status TEXT,
              verdict TEXT,
              started_at REAL,
              ended_at REAL,
              clock_source TEXT,
              out_bytes INTEGER,
              out_growing INTEGER DEFAULT 0,
              err_bytes INTEGER,
              pid INTEGER,
              out_path TEXT
            );
            CREATE TABLE IF NOT EXISTS usage_events (
              tool TEXT NOT NULL,
              session_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              ts REAL,
              run_id TEXT,
              model TEXT,
              input_tokens INTEGER,
              output_tokens INTEGER,
              cache_read INTEGER,
              cache_write INTEGER,
              reasoning INTEGER,
              est_cost_usd REAL,
              PRIMARY KEY (tool, session_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS usage_buckets (
              hour_ts REAL NOT NULL,
              model TEXT NOT NULL,
              workstream TEXT NOT NULL,
              tool TEXT NOT NULL,
              input_tokens INTEGER,
              output_tokens INTEGER,
              cache_read INTEGER,
              cache_write INTEGER,
              reasoning INTEGER,
              missing INTEGER NOT NULL DEFAULT 0,
              cost_usd REAL,
              PRIMARY KEY (hour_ts, model, workstream, tool)
            );
            CREATE TABLE IF NOT EXISTS process_samples (
              ts REAL NOT NULL,
              pid INTEGER NOT NULL,
              comm TEXT,
              rss_kb INTEGER,
              elapsed_s REAL,
              label TEXT,
              run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
              session_id TEXT PRIMARY KEY,
              tool TEXT,
              cwd TEXT,
              started_at REAL,
              status TEXT,
              parent_session_id TEXT,
              label TEXT,
              workstream TEXT,
              ws_confidence TEXT
            );
            CREATE TABLE IF NOT EXISTS ws_counts (
              ws TEXT PRIMARY KEY,
              name TEXT,
              backlog INTEGER,
              todo INTEGER,
              doing INTEGER,
              review INTEGER,
              done INTEGER,
              cancelled INTEGER
            );
            CREATE TABLE IF NOT EXISTS file_offsets (
              path TEXT PRIMARY KEY,
              inode INTEGER NOT NULL,
              offset INTEGER NOT NULL,
              size INTEGER,
              mtime REAL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_ended ON runs(ended_at);
            CREATE INDEX IF NOT EXISTS idx_runs_ws ON runs(workstream);
            CREATE INDEX IF NOT EXISTS idx_runs_pid ON runs(pid);
            CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_events(run_id);
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON process_samples(ts);
            CREATE INDEX IF NOT EXISTS idx_samples_pid ON process_samples(pid);
            """
        )

    def _one(self, sql: str, args: tuple):
        row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all_runs(self) -> list:
        return self._conn.execute("SELECT * FROM runs").fetchall()

    def _write_run(self, merged: dict) -> None:
        values = [merged.get(col) for col in RUN_COLS]
        placeholders = ",".join("?" for _ in RUN_COLS)
        updates = ",".join(f"{col}=excluded.{col}" for col in RUN_COLS if col != "run_id")
        self._conn.execute(
            f"INSERT INTO runs({','.join(RUN_COLS)}) VALUES({placeholders}) "
            f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
            values,
        )

    def _write_session(self, merged: dict) -> None:
        values = [merged.get(col) for col in SESSION_COLS]
        placeholders = ",".join("?" for _ in SESSION_COLS)
        updates = ",".join(f"{col}=excluded.{col}" for col in SESSION_COLS if col != "session_id")
        self._conn.execute(
            f"INSERT INTO sessions({','.join(SESSION_COLS)}) VALUES({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {updates}",
            values,
        )

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
                      e.reasoning AS reasoning
               FROM usage_events e
               LEFT JOIN runs r ON r.run_id = e.run_id
               WHERE e.ts >= ? AND e.ts < ?""",
            (hour_ts, hour_ts + 3600),
        ).fetchall()
        groups: dict = {}
        for row in rows:
            key = (row["model"] or "", row["ws"], row["tool"] or "")
            bucket = groups.setdefault(
                key,
                {name: 0 for name in ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning")}
                | {"missing": 0},
            )
            missing = any(row[name] is None for name in ("input_tokens", "output_tokens", "cache_read", "cache_write"))
            if missing:
                bucket["missing"] = 1
            for name in ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning"):
                if row[name] is not None:
                    bucket[name] += row[name]
        payload = []
        for (model, workstream, tool), bucket in groups.items():
            est = None
            if not bucket["missing"]:
                est = price_cost(model, bucket["input_tokens"], bucket["output_tokens"], bucket["cache_read"], bucket["cache_write"])
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
        rows = self._conn.execute(
            """SELECT run_id, model, input_tokens, output_tokens, cache_read, cache_write, reasoning
               FROM usage_events WHERE run_id IS NOT NULL"""
        ).fetchall()
        grouped: dict = {}
        for row in rows:
            grouped.setdefault(row["run_id"], []).append(row)
        out = {}
        for run_id, events in grouped.items():
            out[run_id] = _fold_events(events)
        return out

    def _rss_maps(self, now: float) -> tuple[dict, dict]:
        rows = self._conn.execute(
            """SELECT pid, rss_kb, run_id, ts FROM process_samples
               WHERE ts >= ? ORDER BY ts DESC""",
            (now - SAMPLE_FRESH_S,),
        ).fetchall()
        by_run = {}
        by_pid = {}
        for row in rows:
            if row["run_id"] and row["run_id"] not in by_run:
                by_run[row["run_id"]] = row["rss_kb"]
            if row["pid"] not in by_pid:
                by_pid[row["pid"]] = row["rss_kb"]
        return by_run, by_pid

    def _live_counts(self, now: float) -> dict:
        rows = self._conn.execute(
            "SELECT MAX(ts) AS ts FROM process_samples WHERE ts >= ?",
            (now - SAMPLE_FRESH_S,),
        ).fetchone()
        counts = {key: 0 for key in CAP_KEYS}
        if not rows or rows["ts"] is None:
            return counts
        samples = self._conn.execute(
            "SELECT comm FROM process_samples WHERE ts=?", (rows["ts"],)
        ).fetchall()
        for sample in samples:
            key = COMM_TO_CAP.get(sample["comm"] or "")
            if key:
                counts[key] += 1
        return counts

    def _run_view(self, row, now: float, usage: dict, rss: tuple[dict, dict]) -> dict:
        data = dict(row)
        present = data["run_id"] in usage
        folded, cost, _unpriced = usage.get(data["run_id"], (_zeros(), 0.0, False))
        by_run, by_pid = rss
        rss_kb = by_run.get(data["run_id"])
        if rss_kb is None and data.get("pid") is not None:
            rss_kb = by_pid.get(data["pid"])
        if present:
            tokens = {key: folded.get(key) for key in BILLED_FIELDS}
            cost_usd = None if cost is None else _money(cost)
        else:
            tokens = {key: 0 for key in BILLED_FIELDS}
            cost_usd = 0.0
        return {
            "run_id": data["run_id"],
            "source": data.get("source"),
            "tier": data.get("tier"),
            "model": data.get("model"),
            "label": data.get("label"),
            "workstream": data.get("workstream"),
            "ws_confidence": data.get("ws_confidence"),
            "session_id": data.get("session_id"),
            "parent_session_id": data.get("parent_session_id"),
            "status": data.get("status"),
            "verdict": data.get("verdict"),
            "started_at": data.get("started_at"),
            "ended_at": data.get("ended_at"),
            "elapsed_s": _elapsed(data, now),
            "clock_source": data.get("clock_source"),
            "out_bytes": data.get("out_bytes"),
            "out_growing": bool(data.get("out_growing")),
            "err_bytes": data.get("err_bytes"),
            "pid": data.get("pid"),
            "rss_kb": rss_kb,
            "tokens": tokens,
            "cost_usd": cost_usd,
        }

    def _alerts(self, runs: list, now: float, caps: dict, long_running_s: dict) -> list:
        alerts = []
        for run in runs:
            if run["status"] in ("died_early", "killed") and run["ended_at"] is not None and run["ended_at"] >= now - RECENT_S:
                alerts.append(
                    {
                        "kind": run["status"],
                        "run_id": run["run_id"],
                        "label": run["label"],
                        "detail": "run died early" if run["status"] == "died_early" else "run was killed",
                        "ts": run["ended_at"],
                    }
                )
            cap = _long_cap(run.get("tier"), long_running_s)
            elapsed = run.get("elapsed_s")
            if run["status"] == "running" and cap is not None and elapsed is not None and elapsed > cap:
                alerts.append(
                    {
                        "kind": "long_running",
                        "run_id": run["run_id"],
                        "label": run["label"],
                        "detail": f"elapsed {int(elapsed)}s exceeds {run.get('tier')} cap {cap}s",
                        "ts": now,
                    }
                )
        live = self._live_counts(now)
        for key in CAP_KEYS:
            cap = caps.get(key)
            if cap is not None and live.get(key, 0) > cap:
                alerts.append(
                    {
                        "kind": "over_cap",
                        "run_id": f"cap:{key}",
                        "label": key,
                        "detail": f"{live[key]} live parents, cap {cap}",
                        "ts": now,
                    }
                )
        alerts.sort(key=lambda item: item["ts"] or 0, reverse=True)
        return alerts[:200]


def _hour(ts: float) -> int:
    return int(ts // 3600) * 3600


def _opt_int(row: dict, key: str):
    if key not in row or row[key] is None:
        return None
    return int(row[key])


def _same(existing: dict, merged: dict, cols) -> bool:
    for col in cols:
        if existing.get(col) != merged.get(col):
            return False
    return True


def _zeros() -> dict:
    return {key: 0 for key in TOKEN_FIELDS}


def _fold_events(events: list) -> tuple[dict, float | None, bool]:
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
        priced = price_cost(
            event["model"],
            event["input_tokens"],
            event["output_tokens"],
            event["cache_read"],
            event["cache_write"],
        )
        costs.append(priced)
        if priced is None and any(event[name] is not None for name in ("input_tokens", "output_tokens", "cache_read", "cache_write")):
            unpriced = True
    if not events:
        return folded, 0.0, False
    if all(item is None for item in costs):
        return folded, None if unpriced or events else 0.0, unpriced or bool(events)
    return folded, sum(item for item in costs if item is not None), unpriced


def _money(value):
    if value is None:
        return None
    return round(float(value), 6)


def _elapsed(run: dict, now: float):
    start = run.get("started_at")
    if start is None:
        return None
    if run.get("status") == "running" or run.get("ended_at") is None:
        end = now
    else:
        end = run["ended_at"]
    return max(0.0, float(end) - float(start))


def _in_ws(run: dict, ws: str | None) -> bool:
    if not ws or ws == "all":
        return True
    return run.get("workstream") == ws


def _caps(caps: dict | None) -> dict:
    merged = dict(DEFAULT_CAPS)
    if caps:
        merged.update(caps)
    return merged


def _long_cap(tier: str | None, table: dict):
    if not tier:
        return None
    return table.get(str(tier).lower())


def _tool_key(run: dict) -> str | None:
    source = run.get("source") or ""
    tier = (run.get("tier") or "").lower()
    if source == "grok" or tier == "grok":
        return "grok"
    if source == "muse" or tier == "muse":
        return "muse"
    if source == "glm" or tier == "glm":
        return "glm"
    if source.startswith("claude") or tier in ("opus", "sonnet", "haiku"):
        return "claude"
    return None


def _active(run: dict, t: float, now: float) -> bool:
    start = run.get("started_at")
    if start is None or start > t:
        return False
    if run.get("status") == "running":
        return t <= now
    end = run.get("ended_at")
    if end is None:
        return False
    return t < end


def _totals(runs: list) -> dict:
    tokens = {key: 0 for key in TOKEN_FIELDS}
    cost = 0.0
    any_cost = False
    unpriced = 0
    for run in runs:
        for key in BILLED_FIELDS:
            tokens[key] += (run.get("tokens") or {}).get(key) or 0
        if run.get("cost_usd") is None:
            unpriced += 1
        else:
            cost += run["cost_usd"] or 0
            any_cost = True
    return {
        "tokens": {key: tokens[key] for key in ("input", "output", "cache_read", "cache_write", "reasoning")},
        "cost_usd": _money(cost) if any_cost or not unpriced else 0.0,
        "unpriced_runs": unpriced,
    }


def _by_workstream(runs: list) -> list:
    groups: dict = {}
    for run in runs:
        ws = run.get("workstream")
        if not ws:
            continue
        group = groups.setdefault(
            ws,
            {"ws": ws, "running": 0, "done": 0, "tokens": 0, "cost": 0.0, "priced": False, "last_activity": 0},
        )
        if run["status"] == "running":
            group["running"] += 1
        if run["status"] == "done":
            group["done"] += 1
        for key in BILLED_FIELDS:
            group["tokens"] += (run.get("tokens") or {}).get(key) or 0
        if run.get("cost_usd"):
            group["cost"] += run["cost_usd"]
            group["priced"] = True
        for stamp in (run.get("ended_at"), run.get("started_at")):
            if stamp and stamp > group["last_activity"]:
                group["last_activity"] = stamp
    out = []
    for group in groups.values():
        out.append(
            {
                "ws": group["ws"],
                "running": group["running"],
                "done": group["done"],
                "tokens": group["tokens"],
                "cost_usd": _money(group["cost"]) if group["priced"] else 0.0,
                "last_activity": group["last_activity"] or None,
            }
        )
    out.sort(key=lambda item: _ws_sort(item["ws"]))
    return out


def _sum_costs(costs: list, any_tokens: bool):
    if any(item is not None and item != 0 for item in costs) or any(item == 0 for item in costs):
        priced = [item for item in costs if item is not None]
        if not priced:
            return None if any_tokens else 0.0
        return sum(priced)
    if any_tokens:
        return None
    return 0.0


def _ws_sort(ws: str):
    import re

    match = re.fullmatch(r"WS(\d+)", ws or "")
    if match:
        return (0, int(match.group(1)), "")
    return (1, 0, ws or "")


def _collapse_workflows(runs: list, now: float) -> list:
    groups: dict = {}
    order: list = []
    solo = []
    for run in runs:
        match = _WF.search(run["run_id"] or "")
        if not match:
            solo.append(run)
            continue
        key = (run.get("session_id") or "", match.group(1))
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(run)
    collapsed = list(solo)
    for key in order:
        members = groups[key]
        wf = key[1]
        roots = [run for run in members if (run["run_id"] or "").endswith(":" + wf)]
        root = roots[0] if roots else min(members, key=lambda run: run["started_at"] or 0)
        row = dict(root)
        row["tokens"] = dict(root.get("tokens") or {})
        row["children"] = len(members) - 1
        if len(members) > 1:
            starts = [run["started_at"] for run in members if run["started_at"] is not None]
            if starts:
                row["started_at"] = min(starts)
            if any(run["status"] == "running" for run in members):
                row["status"] = "running"
                row["ended_at"] = None
            else:
                ends = [run["ended_at"] for run in members if run["ended_at"] is not None]
                if ends:
                    row["ended_at"] = max(ends)
            row["out_bytes"] = sum((run.get("out_bytes") or 0) for run in members)
            row["err_bytes"] = sum((run.get("err_bytes") or 0) for run in members)
            row["out_growing"] = any(run.get("out_growing") for run in members)
            tokens = {name: 0 for name in BILLED_FIELDS}
            costs = []
            for run in members:
                for name in BILLED_FIELDS:
                    tokens[name] += (run.get("tokens") or {}).get(name) or 0
                costs.append(run.get("cost_usd"))
            row["tokens"] = tokens
            priced = [item for item in costs if item is not None]
            row["cost_usd"] = _money(sum(priced)) if priced else None
            row["elapsed_s"] = _elapsed(row, now)
        collapsed.append(row)
    return collapsed
