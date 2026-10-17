"""SQLite WAL store: runs, usage, samples, sessions, offsets, hourly buckets."""

from __future__ import annotations

import re
import sqlite3
import threading
import time as _time
from pathlib import Path

from datetime import datetime

from dash.attribution import attribute, clean_label
from dash.liveness import (
    DAGSTER_ACTIVE_FRESH_S,
    STALE_IDLE_S,
    pid_alive as _pid_alive,
    stale_grace_s,
)
from dash.pricing import cost as price_cost
from dash.pricing import series_key
from dash.pricing import status as price_status
from dash.models import model_key, run_model_key

TOOL_KEYS = ("grok", "muse", "glm", "claude")
FINISHED = ("done", "killed", "died_early")
RANGE_S = 24 * 3600
_ESTIMATE_TOOL = "grok-context"
UNASSIGNED = "unassigned"
SAMPLE_FRESH_S = 20
RECENT_S = 24 * 3600
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
        self._last_data_ts: float | None = None
        self._snapshot_cache: dict = {}
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

    @property
    def last_data_ts(self) -> float | None:
        return self._last_data_ts

    def _note_data_write(self) -> None:
        """Stamp the last data-changing write. Caller holds the lock."""
        self._last_data_ts = _time.time()

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
                self._note_data_write()

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
            self._note_data_write()

    def upsert_run(self, row: dict) -> None:
        run_id = row.get("run_id")
        if not run_id:
            raise ValueError("run_id required")
        with self._lock:
            existing = self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
            merged = dict(existing or {})
            merged["run_id"] = run_id
            for key in RUN_INPUT:
                if key not in row:
                    continue
                if (
                    key == "started_at"
                    and row[key] is None
                    and existing
                    and existing.get("started_at") is not None
                ):
                    continue
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
            self._note_data_write()

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
                        row.get("run_id"),
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

    def set_dagster_active(self, run_ids, now: float, full_page: bool = False) -> None:
        """Remember the latest successful Dagster active set for stale checks."""
        import json as _json

        payload = _json.dumps(
            {"ids": sorted(set(run_ids)), "now": float(now), "full_page": bool(full_page)}
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('dagster_active', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (payload,),
            )

    def get_dagster_active(self):
        """Latest successful active set, or None when no poll has succeeded."""
        import json as _json

        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='dagster_active'"
            ).fetchone()
        if not row or not row["value"]:
            return None
        try:
            data = _json.loads(row["value"])
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def note_dagster_seen(self, run_ids, now: float) -> None:
        """Record when each active Dagster run was last seen (for close-out)."""
        if not run_ids:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO dagster_seen(run_id, last_seen) VALUES(?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET last_seen=excluded.last_seen",
                [(rid, float(now)) for rid in set(run_ids)],
            )

    def dagster_last_seen(self, run_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seen FROM dagster_seen WHERE run_id=?", (run_id,)
            ).fetchone()
        return float(row["last_seen"]) if row and row["last_seen"] is not None else None

    def dagster_open_runs(self) -> list:
        """Stored Dagster rows still marked running or queued."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE source='dagster' AND status IN ('running','queued')"
            ).fetchall()
        return [dict(row) for row in rows]

    def stale_running(self, now: float, pid_alive_fn=None) -> list:
        """Running rows with no liveness evidence, each with a reason.

        Reasons: a dead pid; a Dagster run absent from the latest successful
        active set; or no source evidence (usage, process sample, start) for
        longer than STALE_IDLE_S. Dagster rows are stale only when the active
        set is fresh and not a full page; a failed or full-page poll reports
        nothing. An alive-but-idle lead session (``claude-main`` with a live
        pid, quiet for longer than STALE_IDLE_S) is an intended state, not
        stale: it is hidden from views but never reported here and never
        closed. Returns a list of ``(run, reason)``.
        """
        with self._lock:
            rows = [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM runs WHERE status IN ('running','queued')"
                ).fetchall()
            ]
            return self._stale_locked(now, rows, pid_alive_fn=pid_alive_fn)

    def _stale_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None) -> list:
        """Stale subset of ``rows``. Caller holds the lock."""
        now = float(now)
        check = pid_alive_fn or _pid_alive
        dagster_active = self._dagster_active_locked()
        dag_ids = None
        dag_fresh = False
        if dagster_active is not None:
            try:
                poll_time = float(dagster_active.get("now") or 0)
            except (TypeError, ValueError):
                poll_time = 0
            full = bool(dagster_active.get("full_page"))
            if not full and now - poll_time <= DAGSTER_ACTIVE_FRESH_S:
                dag_fresh = True
                dag_ids = set(dagster_active.get("ids") or [])
        open_rows = [
            row for row in rows if (row.get("status") in ("running", "queued"))
        ]
        if not open_rows:
            return []
        if usage_max is None:
            usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
        if sample_max is None:
            sample_max = self._sample_max_locked(open_rows)
        stale = []
        for row in open_rows:
            if _is_idle_lead(row, now, usage_max, sample_max, check):
                continue
            reason = self._stale_reason_locked(
                row, now, dag_ids, dag_fresh, usage_max, sample_max, check
            )
            if reason:
                stale.append((dict(row), reason))
        return stale

    def _idle_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None) -> list:
        """Alive-but-idle lead sessions. Caller holds the lock.

        A ``claude-main`` row with a live pid and no source evidence for
        longer than STALE_IDLE_S is an intended interactive state: the lead
        is still attached but has written nothing. It stays out of every
        view like a stale row, but it is never a mismatch and it is never
        closed, so a later transcript write returns it to Running now.
        """
        now = float(now)
        check = pid_alive_fn or _pid_alive
        open_rows = [
            row for row in rows if (row.get("status") in ("running", "queued"))
        ]
        if not open_rows:
            return []
        if usage_max is None:
            usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
        if sample_max is None:
            sample_max = self._sample_max_locked(open_rows)
        return [
            dict(row)
            for row in open_rows
            if _is_idle_lead(row, now, usage_max, sample_max, check)
        ]

    def _live_open_ids_locked(self, now: float, rows: list, pid_alive_fn=None) -> set:
        """Run ids of open rows with liveness evidence. Caller holds the lock.

        This is the one filtered running set every view uses: the snapshot
        running list, ``counts.live_agents``, ``/api/workstreams``
        ``live_agents``, and the timeline's open bars all derive from it, so
        no view can show a run another hides. Idle leads are excluded here
        too, so they stay out of Running now exactly as before. A
        ``claude-workflow`` wrapper shadowed by a live worker row on the
        same label (one lane, one agent) is excluded here as well; the
        worker carries the lane.
        """
        live, _shadows = self._live_open_with_shadows_locked(now, rows, pid_alive_fn)
        return live

    def _live_open_with_shadows_locked(self, now: float, rows: list, pid_alive_fn=None):
        """The one running set plus the lane-shadow map. Caller holds the lock.

        ``shadows`` maps a shadowed wrapper run id to its live worker run
        id. It is computed against the pre-subtraction open set, so pass
        that open set (``live | set(shadows)``) — not the subtracted set —
        to ``_lane_shadows``/``_annotate_lanes``.
        """
        open_rows = [
            row for row in rows if (row.get("status") in ("running", "queued"))
        ]
        if open_rows:
            usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
            sample_max = self._sample_max_locked(open_rows)
        else:
            usage_max, sample_max = {}, {}
        stale_ids = {
            run["run_id"]
            for run, _reason in self._stale_locked(
                now, rows, pid_alive_fn, usage_max=usage_max, sample_max=sample_max
            )
        }
        idle_ids = {
            row["run_id"]
            for row in self._idle_locked(
                now, rows, pid_alive_fn, usage_max=usage_max, sample_max=sample_max
            )
        }
        open_ids = {
            row["run_id"]
            for row in rows
            if row.get("status") in ("running", "queued")
            and row.get("run_id") not in stale_ids
            and row.get("run_id") not in idle_ids
        }
        shadows = _lane_shadows(rows, open_ids)
        return open_ids - set(shadows), shadows

    def _stale_reason_locked(
        self, run: dict, now: float, dag_ids, dag_fresh: bool,
        usage_max: dict, sample_max: dict, check,
    ):
        source = run.get("source") or ""
        status = run.get("status")
        run_id = run.get("run_id")
        if status not in ("running", "queued"):
            return None
        if source == "dagster":
            if status not in ("running", "queued"):
                return None
            if dag_ids is None or not dag_fresh:
                return None
            short = run_id[len("dagster:"):] if run_id.startswith("dagster:") else run_id
            if short not in dag_ids and run_id not in dag_ids:
                return "dagster run missing from the latest successful active set"
            return None
        pid = run.get("pid")
        dead_pid = False
        if pid is not None:
            try:
                alive = bool(check(pid))
            except Exception:
                alive = False
            dead_pid = not alive
        stamps = []
        started = run.get("started_at")
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            stamps.append(float(started))
        hit = usage_max.get(run_id)
        if hit is not None:
            stamps.append(float(hit))
        hit = sample_max.get(run_id)
        if hit is not None:
            stamps.append(float(hit))
        if pid is not None:
            hit = sample_max.get(f"pid:{pid}")
            if hit is not None:
                stamps.append(float(hit))
        last = max(stamps) if stamps else None
        if dead_pid:
            if last is not None and now - last <= stale_grace_s(source):
                return None
            return f"dead pid {pid}"
        if last is None:
            return None
        if now - last > STALE_IDLE_S:
            return f"no source evidence for longer than {int(STALE_IDLE_S // 3600)}h"
        return None

    def _dagster_active_locked(self):
        import json as _json

        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='dagster_active'"
        ).fetchone()
        if not row or not row["value"]:
            return None
        try:
            data = _json.loads(row["value"])
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _usage_max_locked(self, run_ids: list) -> dict:
        if not run_ids:
            return {}
        found = {}
        step = 500
        for index in range(0, len(run_ids), step):
            chunk = run_ids[index:index + step]
            marks = ",".join("?" for _ in chunk)
            for row in self._conn.execute(
                f"SELECT run_id, MAX(ts) AS ts FROM usage_events "
                f"WHERE run_id IN ({marks}) GROUP BY run_id",
                chunk,
            ).fetchall():
                if row["ts"] is not None:
                    found[row["run_id"]] = float(row["ts"])
        return found

    def _sample_max_locked(self, rows: list) -> dict:
        """Latest process-sample ts by run id and by pid."""
        run_ids = [row.get("run_id") for row in rows if row.get("run_id")]
        pids = [row.get("pid") for row in rows if row.get("pid") is not None]
        found = {}
        if run_ids:
            step = 500
            for index in range(0, len(run_ids), step):
                chunk = run_ids[index:index + step]
                marks = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                    f"SELECT run_id, MAX(ts) AS ts FROM process_samples "
                    f"WHERE run_id IN ({marks}) GROUP BY run_id",
                    chunk,
                ).fetchall():
                    if row["run_id"] and row["ts"] is not None:
                        found[row["run_id"]] = float(row["ts"])
        if pids:
            uniq = sorted({int(pid) for pid in pids})
            step = 500
            for index in range(0, len(uniq), step):
                chunk = uniq[index:index + step]
                marks = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                    f"SELECT pid, MAX(ts) AS ts FROM process_samples "
                    f"WHERE pid IN ({marks}) GROUP BY pid",
                    chunk,
                ).fetchall():
                    if row["ts"] is not None:
                        found[f"pid:{int(row['pid'])}"] = float(row["ts"])
        return found

    def session_runs(self, session_id: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                """SELECT run_id, label, session_id, source, status, started_at
                   FROM runs WHERE session_id = ? COLLATE NOCASE""",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reassign_run(self, src_id: str, dst_id: str) -> bool:
        """Move usage from ``src_id`` onto ``dst_id``, fill empty fields, delete ``src_id``."""
        if not src_id or not dst_id or src_id == dst_id:
            return False
        with self._lock:
            src = self._one("SELECT * FROM runs WHERE run_id=?", (src_id,))
            dst = self._one("SELECT * FROM runs WHERE run_id=?", (dst_id,))
            if not src or not dst:
                return False
            merged = dict(dst)
            for key in (
                "model",
                "started_at",
                "clock_source",
                "verdict",
                "out_bytes",
                "err_bytes",
                "out_path",
                "pid",
            ):
                if merged.get(key) in (None, "") and src.get(key) not in (None, ""):
                    merged[key] = src[key]
            workstream, confidence = attribute(
                merged.get("label"),
                merged.get("session_id"),
                parent_session_id=merged.get("parent_session_id"),
            )
            merged["workstream"] = workstream
            merged["ws_confidence"] = confidence
            hours = self._hours_for_run(src_id) | self._hours_for_run(dst_id)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE usage_events SET run_id=? WHERE run_id=?",
                    (dst_id, src_id),
                )
                self._conn.execute(
                    "UPDATE process_samples SET run_id=? WHERE run_id=?",
                    (dst_id, src_id),
                )
                last = self._conn.execute(
                    "SELECT MAX(ts) AS ts FROM usage_events WHERE run_id=? AND ts IS NOT NULL AND COALESCE(tool, '') != ?",
                    (dst_id, _ESTIMATE_TOOL),
                ).fetchone()
                if (
                    merged.get("status") != "running"
                    and merged.get("ended_at") is not None
                    and last
                    and last["ts"] is not None
                    and float(last["ts"]) > float(merged["ended_at"])
                ):
                    merged["ended_at"] = float(last["ts"])
                self._write_run(merged)
                self._conn.execute("DELETE FROM runs WHERE run_id=?", (src_id,))
                for hour in hours:
                    self._rebuild_hour(hour)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._seq += 1
            self._data_seq += 1
            self._note_data_write()
        return True

    def repair_grok_runs(self, now: float, window_s: float = 900.0) -> tuple[int, int]:
        """Fill NULL grok models from usage and fold manifest twins into chat rows.

        The cursor source calls this every poll. The manifest merge runs
        every time (it is one indexed scan and converges to no work); the
        usage backfill runs at most once a minute. Returns
        ``(models_filled, merged)``.
        """
        merged = self.merge_grok_manifests(window_s)
        filled = self.backfill_grok_models(now)
        return filled, merged

    def backfill_grok_models(self, now: float) -> int:
        """Fill a NULL-model grok run from its latest usage event model.

        Usage-line models win everywhere else, so a run that learned its
        model at the end keeps it here too. Runs with no usage model stay
        NULL ("model not recorded"). Returns how many rows changed.
        """
        now = float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='grok_models_ts'"
            ).fetchone()
            if row and row["value"]:
                try:
                    if now - float(row["value"]) < 60.0:
                        return 0
                except (TypeError, ValueError):
                    pass
            pending = [
                dict(item)
                for item in self._conn.execute(
                    "SELECT run_id FROM runs WHERE source='grok' AND (model IS NULL OR model='')"
                ).fetchall()
            ]
        filled = 0
        for item in pending:
            with self._lock:
                hit = self._conn.execute(
                    """SELECT model FROM usage_events
                       WHERE run_id=? AND model IS NOT NULL AND model != ''
                       ORDER BY ts IS NULL, ts DESC LIMIT 1""",
                    (item["run_id"],),
                ).fetchone()
            if not hit:
                continue
            self.upsert_run({"run_id": item["run_id"], "model": hit["model"]})
            filled += 1
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('grok_models_ts', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(now),),
            )
        return filled

    def merge_grok_manifests(self, window_s: float = 900.0) -> int:
        """Fold launcher manifest rows into the chat row they record.

        A ``cursor-sessions.tsv`` row becomes ``grok:<id>:cursor/<label>``
        while the Cursor chat itself is ``grok:<chat-uuid>:<label>``. A
        manifest row whose label matches a non-manifest grok row that
        started within ``window_s`` (default 15 minutes, wider than the ten
        minutes views use) is that same run: the merged row keeps the
        earliest start, the chat row's status, the model, and the
        manifest's out path; usage and samples move over and the manifest
        row is deleted. A manifest row with no twin stays. Returns how
        many rows merged.
        """
        with self._lock:
            manifests = [
                row
                for row in (
                    dict(item)
                    for item in self._conn.execute(
                        "SELECT * FROM runs WHERE source='grok'"
                    ).fetchall()
                )
                if _is_manifest_twin(row)
            ]
        merged = 0
        for manifest in manifests:
            with self._lock:
                twins = [
                    row
                    for row in (
                        dict(item)
                        for item in self._conn.execute(
                            "SELECT * FROM runs WHERE source='grok' AND label=? AND run_id != ?",
                            (manifest.get("label"), manifest["run_id"]),
                        ).fetchall()
                    )
                    if not _is_manifest_twin(row)
                ]
            twin = _nearest_twin(manifest, twins, window_s)
            if twin is None:
                continue
            if self._merge_run_into(manifest["run_id"], twin["run_id"]):
                merged += 1
        return merged

    def _merge_run_into(self, src_id: str, dst_id: str) -> bool:
        """Fold manifest row ``src_id`` into chat row ``dst_id`` and delete it."""
        if not src_id or not dst_id or src_id == dst_id:
            return False
        with self._lock:
            src = self._one("SELECT * FROM runs WHERE run_id=?", (src_id,))
            dst = self._one("SELECT * FROM runs WHERE run_id=?", (dst_id,))
            if not src or not dst:
                return False
            merged = dict(dst)
            src_start = _epoch(src.get("started_at"))
            dst_start = _epoch(dst.get("started_at"))
            if src_start is not None and (dst_start is None or src_start < dst_start):
                merged["started_at"] = src_start
                if src.get("clock_source"):
                    merged["clock_source"] = src["clock_source"]
            for key in ("model", "verdict", "out_path", "out_bytes", "err_bytes"):
                if merged.get(key) in (None, "", 0) and src.get(key) not in (None, "", 0):
                    merged[key] = src[key]
            src_end = _epoch(src.get("ended_at"))
            dst_end = _epoch(dst.get("ended_at"))
            if (
                merged.get("status") != "running"
                and src_end is not None
                and (dst_end is None or src_end > dst_end)
            ):
                merged["ended_at"] = src_end
            workstream, confidence = attribute(
                merged.get("label"),
                merged.get("session_id"),
                parent_session_id=merged.get("parent_session_id"),
            )
            merged["workstream"] = workstream
            merged["ws_confidence"] = confidence
            hours = self._hours_for_run(src_id) | self._hours_for_run(dst_id)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE usage_events SET run_id=? WHERE run_id=?",
                    (dst_id, src_id),
                )
                self._conn.execute(
                    "UPDATE process_samples SET run_id=? WHERE run_id=?",
                    (dst_id, src_id),
                )
                self._write_run(merged)
                self._conn.execute("DELETE FROM runs WHERE run_id=?", (src_id,))
                for hour in hours:
                    self._rebuild_hour(hour)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._seq += 1
            self._data_seq += 1
        return True

    def repair_muse_orphans(self) -> int:
        """Move orphan muse usage onto the labeled run and delete the orphan."""
        with self._lock:
            rows = [
                dict(row)
                for row in self._conn.execute("SELECT * FROM runs WHERE source='muse'").fetchall()
            ]
        moved = 0
        for src_id, dst_id in muse_orphan_pairs(rows):
            if self.reassign_run(src_id, dst_id):
                moved += 1
        return moved

    def repair_muse_clocks(self) -> int:
        """Fill a missing muse start from the earliest usage event.

        A finished row whose end is earlier than its latest usage event is
        raised to that event. A running row stays open. Returns how many
        rows changed.
        """
        with self._lock:
            runs = [
                dict(row)
                for row in self._conn.execute(
                    "SELECT run_id, status, started_at, ended_at, clock_source FROM runs WHERE source='muse'"
                )
            ]
        changed = 0
        for run in runs:
            with self._lock:
                usage = self._conn.execute(
                    """SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts
                       FROM usage_events
                       WHERE run_id=? AND ts IS NOT NULL AND COALESCE(tool, '') != ?""",
                    (run["run_id"], _ESTIMATE_TOOL),
                ).fetchone()
            patch = {"run_id": run["run_id"]}
            if run.get("started_at") is None and usage and usage["first_ts"] is not None:
                patch["started_at"] = float(usage["first_ts"])
                if not run.get("clock_source"):
                    patch["clock_source"] = "muse_log"
            if (
                run.get("status") != "running"
                and run.get("ended_at") is not None
                and usage
                and usage["last_ts"] is not None
                and float(usage["last_ts"]) > float(run["ended_at"])
            ):
                patch["ended_at"] = float(usage["last_ts"])
            if len(patch) == 1:
                continue
            self.upsert_run(patch)
            changed += 1
        return changed

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

    def snapshot(
        self,
        now: float,
        ws: str = "all",
        since: float | None = None,
        until: float | None = None,
        usage_group: str | None = None,
    ) -> dict:
        """Scope snapshot with counts, totals, and (optionally) usage buckets.

        When ``usage_group`` (``model`` | ``workstream`` | ``tool``) is given,
        the hourly usage buckets for the same scope and window ride along as
        ``usage``, read under the same lock pass as the totals, so the bucket
        token sum equals ``totals`` by construction. Without it the key is
        absent and the payload is the historic shape.
        """
        if usage_group is not None and usage_group not in ("model", "workstream", "tool"):
            raise ValueError("bad usage_group")
        with self._lock:
            shown_all, parents = self._shown_locked(now)
            live_open, shadows = self._live_open_with_shadows_locked(now, shown_all)
            shown_all = _annotate_lanes(shown_all, live_open | set(shadows))
            scoped = [run for run in shown_all if _in_ws(run, ws)]
            running = [
                run
                for run in scoped
                if run["status"] == "running" and run["run_id"] in live_open
            ]
            running.sort(key=lambda run: run["started_at"] or 0, reverse=True)
            range_since = now - RANGE_S if since is None else float(since)
            range_until = float(now if until is None else until)
            recent_all = [
                run
                for run in scoped
                if run["status"] != "running"
                and run["ended_at"] is not None
                and range_since <= float(run["ended_at"]) <= range_until
            ]
            recent_all.sort(key=lambda run: run["ended_at"] or 0, reverse=True)
            recent_total = len(recent_all)
            recent = recent_all[:50]
            usage_once = self._usage_by_run()
            roll = self._summarize_locked(
                shown_all, parents, now, since, until, live_open, usage=usage_once
            )
            if ws is None or ws == "all":
                key = "all"
                scope = roll["all"]
                counts = scope["counts"]
                totals = scope["totals"]
                by_ws = list(roll["rows"])
                if roll["has_unassigned"]:
                    by_ws.append(roll["unassigned"])
            else:
                key = UNASSIGNED if ws == UNASSIGNED else ws
                scope = roll["scopes"].get(key, _empty_scope())
                counts = scope["counts"]
                totals = scope["totals"]
                wanted = [roll["unassigned"]] if key == UNASSIGNED and roll["has_unassigned"] else roll["rows"]
                by_ws = [row for row in wanted if row["ws"] == key]
            scope_ws = None if ws in (None, "all") else ws
            unpriced = self._unpriced_models_locked(range_since, range_until, scope_ws)
            totals = {
                **totals,
                "unpriced_tokens": sum(entry["tokens"] for entry in unpriced),
                "unpriced_models": [entry["model"] for entry in unpriced],
            }
            response = {
                "now": now,
                "seq": self._seq,
                "data_seq": self._data_seq,
                "last_data_ts": self._last_data_ts,
                "counts": counts,
                "running": running,
                "recent": recent,
                "recent_shown": len(recent),
                "recent_total": recent_total,
                "totals": totals,
                "totals_all_time": roll["all_time"][key]
                if key in roll["all_time"]
                else {"tokens": {field: 0 for field in TOKEN_FIELDS}, "cost_usd": 0.0},
                "last_activity": _last_activity(scope["runs"]),
                "by_workstream": by_ws,
            }
            if usage_group is not None:
                # Same scope and window as the totals above, same lock pass:
                # no writer can land between the two reads.
                response["usage"] = self._query_usage_locked(
                    usage_group, "hour", scope_ws, roll["since"], roll["until"]
                )
            return response

    def snapshot_cached(
        self,
        now: float,
        ws: str = "all",
        since: float | None = None,
        until: float | None = None,
        usage_group: str | None = None,
    ) -> dict:
        """Serve reads from the snapshot cache between writes.

        Keyed by the request params plus the store sequences: idle ticks
        with no writes in between return the cached payload without
        recomputing, so idle tabs cost nothing. ``now`` and ``seq`` refresh
        on a hit; the run lists are seconds-stale at most, which the stale
        badge covers. Misses compute one full snapshot and fill the cache.
        """
        key = (ws, since, until, usage_group)
        with self._lock:
            hit = self._snapshot_cache.get(key)
            if (
                hit is not None
                and hit[0] == self._data_seq
                and hit[1] == self._seq
            ):
                payload = dict(hit[2])
                payload["now"] = now
                payload["seq"] = self._seq
                payload["data_seq"] = self._data_seq
                payload["last_data_ts"] = self._last_data_ts
                return payload
        payload = self.snapshot(now, ws, since, until, usage_group=usage_group)
        with self._lock:
            if len(self._snapshot_cache) > 32:
                self._snapshot_cache.clear()
            self._snapshot_cache[key] = (self._data_seq, self._seq, dict(payload))
        return payload

    def ws_rollup(self, now: float, since: float | None = None, until: float | None = None) -> dict:
        """One locked pass over the same run list: all-scope counts and totals,
        one row per workstream, and the unassigned row.

        Row ``ws`` equals ``snapshot(now, ws, since, until)`` counts and totals.
        The rows plus unassigned sum to the all-scope numbers exactly.
        """
        with self._lock:
            shown_all, parents = self._shown_locked(now)
            live_open = self._live_open_ids_locked(now, shown_all)
            usage_once = self._usage_by_run()
            return self._summarize_locked(
                shown_all, parents, now, since, until, live_open, usage=usage_once
            )

    def reconcile_numbers(
        self,
        now: float,
        since: float | None = None,
        until: float | None = None,
        processes=None,
    ) -> dict:
        """Cross-check every number once, under one lock. ``mismatches`` is empty when consistent.

        ``processes`` is the live worker listing for the ``unseen worker``
        check. The store never samples processes itself, so unit tests stay
        hermetic: pass an explicit list (``[]`` for none). The HTTP layer
        (``dash/server.py``) supplies the live listing per request. A
        worker younger than two poll intervals of its source — by process
        age, or first sampled here within that window — is not yet a
        mismatch.
        """
        with self._lock:
            shown_all, parents = self._shown_locked(now)
            live_open = self._live_open_ids_locked(now, shown_all)
            usage_once = self._usage_by_run()
            roll = self._summarize_locked(
                shown_all, parents, now, since, until, live_open, usage=usage_once
            )
            everything = list(roll["rows"]) + [roll["unassigned"]]
            all_counts = roll["all"]["counts"]
            all_totals = roll["all"]["totals"]
            mismatches: list[str] = []
            for metric in ("live_agents", "finished_in_range", "finished_today", "tokens"):
                if metric == "tokens":
                    expect: int = _billed_total(all_totals["tokens"])
                else:
                    expect = all_counts[metric]
                total = sum(row[metric] for row in everything)
                if total != expect:
                    mismatches.append(f"{metric}: workstream rows sum to {total}, overview shows {expect}")
            if abs(sum(row["cost_usd"] for row in everything) - all_totals["cost_usd"]) > 1e-6:
                mismatches.append(
                    "cost_usd: workstream rows sum to "
                    f"{sum(row['cost_usd'] for row in everything):.6f}, "
                    f"overview shows {all_totals['cost_usd']:.6f}"
                )
            # Per-workstream tabs read the same roll: compare each overview
            # row against its already-computed scope instead of re-running a
            # full snapshot per workstream (O(scopes) filtering, not
            # O(scopes) scans). Values agree by construction; the checks stay
            # so a future divergence still reports the same messages.
            for row in roll["rows"]:
                scope = roll["scopes"].get(row["ws"], _empty_scope())
                tab_counts = scope["counts"]
                tab_totals = scope["totals"]
                tab_all_time = roll["all_time"].get(row["ws"])
                for metric in ("live_agents", "finished_in_range", "finished_today"):
                    if row[metric] != tab_counts[metric]:
                        mismatches.append(
                            f"{row['ws']} {metric}: overview row shows {row[metric]}, "
                            f"tab shows {tab_counts[metric]}"
                        )
                if row["tokens"] != _billed_total(tab_totals["tokens"]):
                    mismatches.append(
                        f"{row['ws']} tokens: overview row shows {row['tokens']}, "
                        f"tab shows {_billed_total(tab_totals['tokens'])}"
                    )
                if abs(row["cost_usd"] - tab_totals["cost_usd"]) > 1e-6:
                    mismatches.append(
                        f"{row['ws']} cost_usd: overview row shows {row['cost_usd']:.6f}, "
                        f"tab shows {tab_totals['cost_usd']:.6f}"
                    )
                if tab_all_time is not None:
                    if row["tokens_all_time"] != _billed_total(tab_all_time["tokens"]):
                        mismatches.append(
                            f"{row['ws']} tokens_all_time: overview row shows {row['tokens_all_time']}, "
                            f"tab shows {_billed_total(tab_all_time['tokens'])}"
                        )
                    if abs(row["cost_usd_all_time"] - tab_all_time["cost_usd"]) > 1e-6:
                        mismatches.append(
                            f"{row['ws']} cost_usd_all_time: overview row shows {row['cost_usd_all_time']:.6f}, "
                            f"tab shows {tab_all_time['cost_usd']:.6f}"
                        )
            all_time = roll["all_time"]
            all_time_total = _billed_total(all_time["all"]["tokens"])
            if sum(row["tokens_all_time"] for row in everything) != all_time_total:
                mismatches.append(
                    "tokens_all_time: workstream rows sum to "
                    f"{sum(row['tokens_all_time'] for row in everything)}, "
                    f"overview shows {all_time_total}"
                )
            if (
                abs(
                    sum(row["cost_usd_all_time"] for row in everything)
                    - all_time["all"]["cost_usd"]
                )
                > 1e-6
            ):
                mismatches.append(
                    "cost_usd_all_time: workstream rows sum to "
                    f"{sum(row['cost_usd_all_time'] for row in everything):.6f}, "
                    f"overview shows {all_time['all']['cost_usd']:.6f}"
                )
            hours = (roll["until"] - roll["since"]) / 3600 if roll["until"] > roll["since"] else 0
            for entry in self._unpriced_models_locked(roll["since"], roll["until"], None):
                mismatches.append(
                    f"unpriced model: {entry['model']} ({entry['tokens']} tokens in {hours:g} h)"
                )
            # The Finished table shows the newest 50 and the page names the
            # truncation ("Newest 50 of 1,452"), so a large finished count is
            # visible, not a mismatch; the counts feed that subtitle.
            finished_total = all_counts["finished_in_range"]
            recent_shown = min(50, finished_total)
            for run, reason in self._stale_locked(now, shown_all):
                mismatches.append(f"stale running: {run.get('run_id')} ({reason})")
            mismatches.extend(self._unseen_workers_locked(now, shown_all, processes or []))
            mismatches.extend(
                _duplicate_run_mismatches([dict(row) for row in self._all_runs()])
            )
            return {
                "now": now,
                "since": roll["since"],
                "until": roll["until"],
                "all": {
                    "live_agents": all_counts["live_agents"],
                    "finished_in_range": all_counts["finished_in_range"],
                    "finished_today": all_counts["finished_today"],
                    "tokens": _billed_total(all_totals["tokens"]),
                    "cost_usd": all_totals["cost_usd"],
                },
                "all_time": {
                    "tokens": all_time_total,
                    "cost_usd": all_time["all"]["cost_usd"],
                },
                "rows": roll["rows"],
                "unassigned": roll["unassigned"],
                "recent_shown": recent_shown,
                "recent_total": finished_total,
                "mismatches": mismatches,
            }

    def _unseen_workers_locked(self, now: float, shown_all: list, processes) -> list:
        """``unseen worker`` mismatches: live worker, no running worker row.

        A live worker process (``[label:]``, a muse prompt-file label, or a
        ``run_worker.py`` argv label) with no ``running``/``queued`` worker
        row (``muse``/``glm``/``grok``) on that label is invisible on the
        dashboard: one mismatch per label. Only the label is reported; argv
        beyond it is never stored or printed. Caller holds the lock.

        Grace, like the stale check: a worker younger than two poll
        intervals of its source is not yet a mismatch — by process age, or
        when this dashboard first sampled it within that window (a fresh
        dashboard has only just met it, e.g. ramp-up transients). Older,
        longer-seen workers with no running row still fail the check.
        """
        have = set()
        for run in shown_all:
            if (
                run.get("status") in ("running", "queued")
                and run.get("source") in WORKER_SOURCES
                and run.get("label")
            ):
                have.add(run["label"])
        pending: dict = {}
        for proc in processes or []:
            if not isinstance(proc, dict):
                continue
            label = proc.get("label")
            if not label or label in have or label in pending:
                continue
            pending[label] = proc
        first_seen = self._first_sample_ts_locked(set(pending)) if pending else {}
        out = []
        for label in sorted(pending):
            proc = pending[label]
            grace = stale_grace_s(_unseen_source(proc))
            try:
                age = now - float(proc.get("started_at"))
            except (TypeError, ValueError):
                age = None
            if age is not None and age < grace:
                continue
            seen_at = first_seen.get(label)
            if seen_at is not None and now - seen_at < grace:
                continue
            comm = proc.get("comm") or "worker"
            pid = proc.get("pid")
            detail = f"{comm} pid {pid}" if pid is not None else str(comm)
            out.append(f"unseen worker: {label} ({detail})")
        return out

    def _first_sample_ts_locked(self, labels: set) -> dict:
        """First process-sample time per label. Caller holds the lock."""
        wanted = [label for label in labels if isinstance(label, str) and label]
        if not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            "SELECT label, MIN(ts) AS first_ts FROM process_samples "
            f"WHERE label IN ({marks}) GROUP BY label",
            tuple(wanted),
        ).fetchall()
        return {
            row["label"]: float(row["first_ts"])
            for row in rows
            if row["first_ts"] is not None
        }

    def _shown_locked(self, now: float) -> tuple[list, dict]:
        """All run views with child chats folded away and launch echoes dropped. Caller holds the lock."""
        raw, usage, parents = self._scoped_usage()
        context = self._context_by_run()
        rss = self._rss_maps(now)
        views = [self._run_view(row, now, usage, rss, context) for row in raw]
        shown = _drop_launch_echoes([view for view in views if view["run_id"] not in parents])
        return shown, parents

    def _summarize_locked(self, shown_all: list, parents: dict, now: float, since, until, live_open=None, usage=None) -> dict:
        """Counts, totals, and per-workstream rows from one run list. Caller holds the lock.

        Token and cost sums come from the same hourly usage buckets ``/api/usage``
        reads, counting a bucket when its hour overlaps ``[since, until]``. The
        tokens tile and the tokens-per-hour chart legend sum the same cells, so
        all three agree exactly. Live counts use ``live_open`` (the one filtered
        running set); when None it is computed here. ``usage`` is the folded
        per-run usage for unpriced checks; one call per snapshot, shared by
        every scope, so the snapshot costs O(scopes) Python filtering instead
        of O(scopes) full usage scans.
        """
        if live_open is None:
            live_open = self._live_open_ids_locked(now, shown_all)
        if usage is None:
            usage = self._usage_by_run()
        range_since = now - RANGE_S if since is None else float(since)
        range_until = float(now if until is None else until)
        cells = self._query_usage_locked("workstream", "hour", None, range_since, range_until)
        buckets: dict[str, list] = {}
        for cell in cells:
            buckets.setdefault(cell["key"], []).append(cell)
        scopes: dict[str, dict] = {}
        scopes["all"] = _scope_numbers(
            shown_all, parents, usage, now, since, until, range_since, range_until,
            [cell for group in buckets.values() for cell in group],
            live_open,
        )
        workstreams = sorted({run.get("workstream") or UNASSIGNED for run in shown_all}, key=_ws_sort)
        if UNASSIGNED not in workstreams:
            workstreams.append(UNASSIGNED)
        for ws in workstreams:
            members = [run for run in shown_all if (run.get("workstream") or UNASSIGNED) == ws]
            scopes[ws] = _scope_numbers(
                members, parents, usage, now, since, until, range_since, range_until,
                buckets.get(ws, []),
                live_open,
            )
        # All-time tokens and cost per scope from the same bucket cells with no
        # window, summed by the same helper as the windowed numbers.
        everything = self._query_usage_locked("workstream", "hour", None, None, None)
        timeless: dict[str, list] = {}
        for cell in everything:
            timeless.setdefault(cell["key"] or UNASSIGNED, []).append(cell)
        all_time: dict[str, dict] = {}
        all_time["all"] = _sum_cells(everything)
        for ws in workstreams:
            all_time[ws] = _sum_cells(timeless.get(ws, []))
        rows = [_ws_row(ws, scopes[ws], all_time[ws]) for ws in workstreams if ws != UNASSIGNED]
        unassigned = _ws_row(UNASSIGNED, scopes[UNASSIGNED], all_time[UNASSIGNED])
        return {
            "since": range_since,
            "until": range_until,
            "all": scopes["all"],
            "scopes": scopes,
            "rows": rows,
            "unassigned": unassigned,
            "has_unassigned": bool(scopes[UNASSIGNED]["runs"]),
            "all_time": all_time,
        }

    def query_runs(self, now: float, ws: str | None, since: float | None, until: float | None, limit: int) -> list:
        """Gantt input. Open bars come from the one filtered running set, so a
        stale row the snapshot hides never shows as an open bar here."""
        with self._lock:
            raw, usage, parents = self._scoped_usage()
            context = self._context_by_run()
            rss = self._rss_maps(now)
            bare = [dict(row) for row in raw]
            live_open, shadows = self._live_open_with_shadows_locked(now, bare)
            rows = []
            for row in raw:
                if row["run_id"] in parents:
                    continue
                view = self._run_view(row, now, usage, rss, context)
                if (
                    view.get("status") in ("running", "queued")
                    and view["run_id"] not in live_open
                    and view["run_id"] not in shadows
                ):
                    continue
                if not _in_ws(view, ws):
                    continue
                if since is not None and (view["ended_at"] or now) < since:
                    continue
                if until is not None and (view["started_at"] or 0) > until:
                    continue
                rows.append(view)
            rows = _annotate_lanes(rows, live_open | set(shadows))
        rows = _collapse_workflows(_drop_launch_echoes(rows), now)
        rows.sort(key=lambda run: run["started_at"] or 0)
        return rows[-limit:]

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
        points = []
        count = int(span / step) + 1
        for index in range(count):
            t = since + index * step
            if t > now + 1e-6:
                break
            point = {"t": t, "grok": 0, "muse": 0, "glm": 0, "claude": 0, "models": {}}
            for run in runs:
                key = _tool_key(run)
                if key and _active(run, t, now):
                    point[key] += 1
                    model = run_model_key(run)
                    point["models"][model] = point["models"].get(model, 0) + 1
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
    ) -> list:
        """One row per workstream from the shared rollup: same agent rule, window,
        and bucket attribution as the snapshot. Hub issue counts (``tk``) ride along
        as issue counts; they are never plan progress."""
        import time as _time

        now = float(now if now is not None else _time.time())
        with self._lock:
            counts = [dict(row) for row in self._conn.execute("SELECT * FROM ws_counts").fetchall()]
            shown_all, parents = self._shown_locked(now)
            usage_once = self._usage_by_run()
            roll = self._summarize_locked(
                shown_all, parents, now, since, until, usage=usage_once
            )
        boards = {row["ws"]: row for row in counts}
        seen = set()
        out = []
        for row in roll["rows"]:
            seen.add(row["ws"])
            board = boards.get(row["ws"], {})
            out.append(
                {
                    "ws": row["ws"],
                    "name": board.get("name") or "",
                    "tk": {key: int(board.get(key) or 0) for key in WS_STATUSES},
                    "live_agents": row["live_agents"],
                    "finished_in_range": row["finished_in_range"],
                    "finished_today": row["finished_today"],
                    "tokens": row["tokens"],
                    "cost_usd": row["cost_usd"],
                    "tokens_all_time": row["tokens_all_time"],
                    "cost_usd_all_time": row["cost_usd_all_time"],
                    "last_activity": row["last_activity"],
                    "plan": row["ws"] in plan_ws,
                }
            )
        for ws in sorted(set(boards) - seen, key=_ws_sort):
            board = boards[ws]
            out.append(
                {
                    "ws": ws,
                    "name": board.get("name") or "",
                    "tk": {key: int(board.get(key) or 0) for key in WS_STATUSES},
                    "live_agents": 0,
                    "finished_in_range": 0,
                    "finished_today": 0,
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "tokens_all_time": 0,
                    "cost_usd_all_time": 0.0,
                    "last_activity": None,
                    "plan": ws in plan_ws,
                }
            )
        if roll["has_unassigned"]:
            row = roll["unassigned"]
            out.append(
                {
                    "ws": row["ws"],
                    "name": "Unassigned",
                    "tk": {key: 0 for key in WS_STATUSES},
                    "live_agents": row["live_agents"],
                    "finished_in_range": row["finished_in_range"],
                    "finished_today": row["finished_today"],
                    "tokens": row["tokens"],
                    "cost_usd": row["cost_usd"],
                    "tokens_all_time": row["tokens_all_time"],
                    "cost_usd_all_time": row["cost_usd_all_time"],
                    "last_activity": row["last_activity"],
                    "plan": False,
                }
            )
        out.sort(key=lambda item: _ws_sort(item["ws"]))
        return out

    def _scoped_usage(self):
        """Usage with child-chat tokens folded onto the parent run.

        Caller holds the lock. Child rows stay in ``raw``; ``parents`` maps
        a child run id to the run that should carry its tokens.
        """
        raw = [dict(row) for row in self._all_runs()]
        parents = _parent_map(raw)
        usage = _rollup_usage(self._usage_by_run(), parents)
        return raw, usage, parents

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
              cache_write_5m INTEGER,
              cache_write_1h INTEGER,
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
              run_id TEXT,
              model TEXT
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
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            );
            CREATE TABLE IF NOT EXISTS dagster_seen (
              run_id TEXT PRIMARY KEY,
              last_seen REAL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_ended ON runs(ended_at);
            CREATE INDEX IF NOT EXISTS idx_runs_ws ON runs(workstream);
            CREATE INDEX IF NOT EXISTS idx_runs_pid ON runs(pid);
            CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source);
            CREATE INDEX IF NOT EXISTS idx_runs_label ON runs(label);
            CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_events(run_id);
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON process_samples(ts);
            CREATE INDEX IF NOT EXISTS idx_samples_pid ON process_samples(pid);
            CREATE INDEX IF NOT EXISTS idx_samples_run ON process_samples(run_id);
            """
        )
        self._migrate_usage_splits()
        self._migrate_uncached_input()

    def _migrate_uncached_input(self) -> None:
        """Once: muse and GLM logged input with cached tokens inside it; store uncached input like every other source, then rebuild the hourly buckets."""
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        done = self._conn.execute("SELECT value FROM meta WHERE key='uncached_input_v1'").fetchone()
        if done:
            return
        self._conn.execute(
            """UPDATE usage_events SET input_tokens = MAX(COALESCE(input_tokens, 0) - COALESCE(cache_read, 0), 0)
               WHERE tool IN ('muse', 'glm') AND input_tokens IS NOT NULL"""
        )
        hours = [row[0] for row in self._conn.execute("SELECT DISTINCT CAST(ts / 3600 AS INTEGER) * 3600 FROM usage_events WHERE tool IN ('muse', 'glm') AND ts IS NOT NULL")]
        for hour in hours:
            self._rebuild_hour(int(hour))
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('uncached_input_v1', '1')")
        self._conn.commit()

    def _migrate_usage_splits(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(usage_events)")}
        for name in ("cache_write_5m", "cache_write_1h"):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} INTEGER")
        sample_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(process_samples)")}
        if "model" not in sample_cols:
            self._conn.execute("ALTER TABLE process_samples ADD COLUMN model TEXT")

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

    def _run_view(self, row, now: float, usage: dict, rss: tuple[dict, dict], context: dict | None = None) -> dict:
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
            "context_tokens": None if not context or data["run_id"] not in context else context[data["run_id"]],
        }

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


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _duplicate_run_mismatches(runs: list) -> list:
    """One mismatch per launcher-manifest row with an unmerged twin.

    Two rows with the same label overlapping in time from the same source
    are one run recorded twice: the manifest row the launcher wrote plus
    the worker's own row. The merge (``merge_grok_manifests``) folds them;
    whatever it leaves behind is reported here so it gets fixed instead of
    double-counted. Non-manifest siblings that share a label (a relaunch,
    a probe, workflow children) are legitimate and never flagged: only a
    surviving manifest row with a same-source, same-label, overlapping
    non-manifest twin that is not shadow-linked to it is a mismatch.
    """
    by_key: dict = {}
    for row in runs:
        label = row.get("label")
        source = row.get("source")
        if not label or not source:
            continue
        by_key.setdefault((source, label), []).append(row)
    found = []
    for rows in by_key.values():
        manifests = [row for row in rows if _is_manifest_twin(row)]
        if not manifests:
            continue
        others = [row for row in rows if not _is_manifest_twin(row)]
        if not others:
            continue
        for manifest in manifests:
            for twin in others:
                if twin.get("run_id") == manifest.get("run_id"):
                    continue
                if twin.get("parent_session_id") == manifest.get("run_id"):
                    continue
                if manifest.get("parent_session_id") == twin.get("run_id"):
                    continue
                if _overlap(manifest, twin):
                    found.append(
                        "duplicate run: %s overlaps %s (same label %r)"
                        % (manifest.get("run_id"), twin.get("run_id"), manifest.get("label"))
                    )
                    break
    return found


def _overlap(first: dict, second: dict) -> bool:
    start = _epoch(first.get("started_at"))
    end = _epoch(first.get("ended_at"))
    other_start = _epoch(second.get("started_at"))
    other_end = _epoch(second.get("ended_at"))
    low = float("-inf") if start is None else start
    high = float("inf") if end is None else end
    other_low = float("-inf") if other_start is None else other_start
    other_high = float("inf") if other_end is None else other_end
    return low <= other_high and other_low <= high


def _is_manifest_twin(row: dict) -> bool:
    """A launcher-manifest grok row (``grok:<id>:cursor/<label>``).

    Chat run ids are ``grok:<chat-uuid>:<label>`` and labels never produce
    a ``:cursor/`` segment, so the marker names manifest rows exactly.
    """
    run_id = row.get("run_id")
    return (
        isinstance(run_id, str)
        and ":cursor/" in run_id
        and row.get("clock_source") == "manifest"
    )


def _nearest_twin(manifest: dict, twins: list, window_s: float):
    """Closest non-manifest twin by start time within ``window_s``.

    Both starts must be known. Shadow links (one row's parent pointing at
    the other run) are not twins.
    """
    base = _epoch(manifest.get("started_at"))
    if base is None or not manifest.get("label"):
        return None
    best = None
    best_delta = None
    for twin in twins:
        if twin.get("run_id") == manifest.get("run_id"):
            continue
        if twin.get("parent_session_id") == manifest.get("run_id"):
            continue
        if manifest.get("parent_session_id") == twin.get("run_id"):
            continue
        start = _epoch(twin.get("started_at"))
        if start is None:
            continue
        delta = abs(start - base)
        if delta <= window_s and (best is None or delta < best_delta):
            best = twin
            best_delta = delta
    return best


def _zeros() -> dict:
    return {key: 0 for key in TOKEN_FIELDS}


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
    if ws == UNASSIGNED:
        return not run.get("workstream")
    return run.get("workstream") == ws


def _billed_total(tokens: dict) -> int:
    return sum((tokens or {}).get(key) or 0 for key in BILLED_FIELDS)


def _run_overlaps_window(run: dict, since: float, until: float, now: float) -> bool:
    """Same overlap rule as the run list: a run with no end time is still open."""
    if (run.get("ended_at") if run.get("ended_at") is not None else now) < since:
        return False
    return (run.get("started_at") or 0) <= until


def _is_idle_lead(run: dict, now: float, usage_max: dict, sample_max: dict, check) -> bool:
    """True for an alive-but-idle interactive lead session.

    A ``claude-main`` row whose pid is still alive but whose newest source
    evidence (start, usage, process sample) is older than STALE_IDLE_S is
    an intended state: the lead is attached but has written nothing. It is
    hidden from views like a stale row, but it is never a mismatch and it
    is never closed. A dead pid is not idle (it stays a stale ``dead pid``
    mismatch); a row with no pid or no evidence at all is not idle either,
    so both are still reported exactly as before.
    """
    if (run.get("source") or "") != "claude-main":
        return False
    if run.get("status") not in ("running", "queued"):
        return False
    pid = run.get("pid")
    if pid is None:
        return False
    try:
        alive = bool(check(pid))
    except Exception:
        alive = False
    if not alive:
        return False
    run_id = run.get("run_id")
    stamps = []
    started = run.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        stamps.append(float(started))
    hit = usage_max.get(run_id)
    if hit is not None:
        stamps.append(float(hit))
    hit = sample_max.get(run_id)
    if hit is not None:
        stamps.append(float(hit))
    hit = sample_max.get(f"pid:{pid}")
    if hit is not None:
        stamps.append(float(hit))
    if not stamps:
        return False
    return now - max(stamps) > STALE_IDLE_S


def _scope_numbers(
    runs: list,
    parents: dict,
    usage: dict,
    now: float,
    since,
    until,
    range_since: float,
    range_until: float,
    cells: list,
    live_open=None,
) -> dict:
    """Counts and totals for one scope (all, one workstream, or unassigned).

    Counts use the agent rule (lead sessions and Dagster jobs are not agents)
    and the window. Token and cost sums add up the ``/api/usage`` cells for the
    same window, so the snapshot, the tiles, and the chart legend agree.
    Live counts use ``live_open`` (the one filtered running set).
    """
    counts = _agent_counts(runs, parents, now, since, until, live_open)
    summed = _sum_cells(cells)
    unpriced_runs = sum(
        1
        for run in runs
        if _run_overlaps_window(run, range_since, range_until, now)
        and _usage_unpriced(usage.get(run["run_id"]))
    )
    return {
        "runs": runs,
        "counts": counts,
        "totals": {
            "tokens": summed["tokens"],
            "cost_usd": summed["cost_usd"],
            "unpriced_runs": unpriced_runs,
        },
    }


def _sum_cells(cells: list) -> dict:
    """Token and cost sums over ``/api/usage`` cells, windowed or not.

    Both the windowed snapshot totals and the all-time totals go through
    this one helper, so they reconcile by construction.
    """
    tokens = {key: 0 for key in TOKEN_FIELDS}
    for cell in cells:
        for key in TOKEN_FIELDS:
            tokens[key] += cell.get(key) or 0
    priced = [
        cell["priced_cost_usd"] if cell["priced_cost_usd"] is not None else cell["cost_usd"]
        for cell in cells
    ]
    priced = [value for value in priced if value is not None]
    return {"tokens": tokens, "cost_usd": _money(sum(priced)) if priced else 0.0}


def _usage_unpriced(entry) -> bool:
    return bool(entry) and bool(entry[2])


def _last_activity(runs: list):
    last = 0
    for run in runs:
        for stamp in (run.get("ended_at"), run.get("started_at")):
            if stamp and stamp > last:
                last = stamp
    return last or None


def _ws_row(ws: str, scope: dict, all_time: dict | None = None) -> dict:
    row = {
        "ws": ws,
        "live_agents": scope["counts"]["live_agents"],
        "finished_in_range": scope["counts"]["finished_in_range"],
        "finished_today": scope["counts"]["finished_today"],
        "tokens": _billed_total(scope["totals"]["tokens"]),
        "cost_usd": scope["totals"]["cost_usd"],
        "last_activity": _last_activity(scope["runs"]),
    }
    if all_time is not None:
        row["tokens_all_time"] = _billed_total(all_time["tokens"])
        row["cost_usd_all_time"] = all_time["cost_usd"]
    return row


def _empty_scope() -> dict:
    return {
        "runs": [],
        "counts": {
            "live_agents": 0,
            "live_by_tool": {key: 0 for key in TOOL_KEYS},
            "live_by_model": {},
            "prod_jobs": 0,
            "finished_today": 0,
            "finished_in_range": 0,
            "finished_by_verdict": {},
        },
        "totals": {
            "tokens": {key: 0 for key in TOKEN_FIELDS},
            "cost_usd": 0.0,
            "unpriced_runs": 0,
        },
    }


def _tool_key(run: dict) -> str | None:
    source = run.get("source") or ""
    if source == "dagster":
        return None
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


def _is_counted_agent(run: dict) -> bool:
    """Interactive Claude sessions and shell launch rows are not agents."""
    source = run.get("source") or ""
    if source in ("claude-main", "shell", "dagster", ""):
        return False
    return _tool_key(run) in TOOL_KEYS


WORKER_SOURCES = frozenset({"muse", "glm", "grok"})
WRAPPER_SOURCES = frozenset({"claude-workflow"})


def _lane_shadows(rows: list, live_ids: set) -> dict:
    """Wrapper run id -> worker run id for lanes with a live worker.

    A ``claude-workflow`` wrapper (the Haiku step a ws-wave lane runs in)
    and a ``muse``/``glm``/``grok`` worker row share the lane label. When
    the worker row is live, the lane counts once — the worker — and the
    wrapper id is shadowed out of the one running set. With no live
    worker row for the wrapper's label (still launching, or the worker
    died), the wrapper is not shadowed and counts as the lane itself.
    """
    live_worker: dict = {}
    for run in rows:
        if (
            run.get("run_id") in live_ids
            and run.get("source") in WORKER_SOURCES
            and run.get("label")
        ):
            live_worker.setdefault(run["label"], run["run_id"])
    shadows = {}
    for run in rows:
        if (
            run.get("run_id") in live_ids
            and run.get("source") in WRAPPER_SOURCES
            and run.get("label")
            and run["label"] in live_worker
        ):
            shadows[run["run_id"]] = live_worker[run["label"]]
    return shadows


def _annotate_lanes(views: list, live_open: set) -> list:
    """Mark lane shadows on run views (view-only; stored rows are untouched).

    A shadowed wrapper view carries ``lane_shadow: True`` so the timeline
    strip counts the lane once while the bar stays clickable. A live
    worker view with no parent names its wrapper run id as
    ``parent_session_id``, so Running now shows the worker via its
    workflow step.
    """
    shadows = _lane_shadows(views, live_open)
    if not shadows:
        return views
    worker_to_wrapper: dict = {}
    for wrapper_id, worker_id in shadows.items():
        worker_to_wrapper.setdefault(worker_id, wrapper_id)
    for view in views:
        run_id = view.get("run_id")
        if run_id in shadows:
            view["lane_shadow"] = True
        elif run_id in worker_to_wrapper and not view.get("parent_session_id"):
            view["parent_session_id"] = worker_to_wrapper[run_id]
    return views


def _unseen_source(proc: dict) -> str | None:
    """Worker source for a live-worker row, for the grace interval.

    Inverts the process listing's comm map; an unknown comm (a
    ``run_worker.py`` launcher, a bare ``[label:]`` token) takes the
    default interval. Every worker source polls at 2 s today, so this is
    4 s either way; the lookup keeps it honest if one diverges.
    """
    try:
        from dash.sources.process import SOURCE_COMMS
    except Exception:
        SOURCE_COMMS = {}
    comm = proc.get("comm") if isinstance(proc, dict) else None
    for source, comms in (SOURCE_COMMS or {}).items():
        try:
            if comm in comms:
                return source
        except TypeError:
            continue
    return None


def muse_orphan_pairs(runs: list) -> list:
    """Return ``(orphan_run_id, labeled_run_id)`` for muse sessions that have both.

    An orphan is an unlabeled ``muse:<sid>:<sid>`` row. The labeled side is the
    latest sibling that has a label.
    """
    groups: dict = {}
    for row in runs:
        source = row.get("source")
        if source not in (None, "muse"):
            rid = row.get("run_id")
            if not (isinstance(rid, str) and rid.startswith("muse:")):
                continue
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        groups.setdefault(sid.lower(), []).append(row)
    pairs = []
    for group in groups.values():
        labeled = [row for row in group if row.get("label")]
        orphans = [row for row in group if _muse_orphan(row)]
        if not labeled or not orphans:
            continue
        labeled.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
        dst = labeled[0].get("run_id")
        if not dst:
            continue
        for src in orphans:
            src_id = src.get("run_id")
            if src_id and src_id != dst:
                pairs.append((src_id, dst))
    return pairs


def _muse_orphan(row: dict) -> bool:
    if row.get("label"):
        return False
    sid = row.get("session_id")
    rid = row.get("run_id")
    if not isinstance(sid, str) or not isinstance(rid, str):
        return False
    parts = rid.split(":")
    if len(parts) != 3 or parts[0] != "muse":
        return False
    return parts[1].lower() == sid.lower() and parts[2].lower() == sid.lower()


def _parent_map(runs: list) -> dict:
    """Child run id -> the run that should carry its tokens.

    A grok child is a chat whose ``parent_session_id`` is another grok run
    (or that run's chat id), or an unlabeled row for a chat that already has
    a labeled run. Hub session ids are not grok chat ids, so a launched agent
    keeps its own count.

    A muse shadow is the unlabeled ``muse:<uuid>:<uuid>`` row for a session
    that also has a labeled run. Its tokens roll onto that labeled run.
    A labeled muse run whose parent is a hub session id stays its own row.
    """
    by_id = {run["run_id"]: run for run in runs}
    grok_by_session = {}
    grok_by_sid = {}
    for run in runs:
        if run.get("source") != "grok" or not run.get("session_id"):
            continue
        grok_by_sid.setdefault(run["session_id"], []).append(run)
        prev = grok_by_session.get(run["session_id"])
        if prev is None or (not prev.get("label") and run.get("label")):
            grok_by_session[run["session_id"]] = run
    parents = {}
    for run in runs:
        if run.get("source") != "grok":
            continue
        parent = run.get("parent_session_id")
        host = by_id.get(parent) if parent else None
        if host and host["run_id"] != run["run_id"]:
            parents[run["run_id"]] = host["run_id"]
            continue
        if parent:
            host = grok_by_session.get(parent)
            if host and host["run_id"] != run["run_id"]:
                parents[run["run_id"]] = host["run_id"]
                continue
        if not run.get("label") and run.get("session_id"):
            labeled = [
                item
                for item in grok_by_sid.get(run["session_id"], [])
                if item.get("label") and item["run_id"] != run["run_id"]
            ]
            if labeled:
                parents[run["run_id"]] = labeled[0]["run_id"]
    muse_by_sid = {}
    for run in runs:
        if run.get("source") != "muse" or not run.get("session_id"):
            continue
        key = run["session_id"].lower() if isinstance(run["session_id"], str) else run["session_id"]
        muse_by_sid.setdefault(key, []).append(run)
    for run in runs:
        if run.get("source") != "muse":
            continue
        parent = run.get("parent_session_id")
        host = by_id.get(parent) if isinstance(parent, str) else None
        if host and host.get("source") == "muse" and host["run_id"] != run["run_id"]:
            parents[run["run_id"]] = host["run_id"]
            continue
        if run.get("label") or not run.get("session_id"):
            continue
        key = run["session_id"].lower() if isinstance(run["session_id"], str) else run["session_id"]
        labeled = [
            item
            for item in muse_by_sid.get(key, [])
            if item.get("label") and item["run_id"] != run["run_id"]
        ]
        if not labeled:
            continue
        labeled.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
        parents[run["run_id"]] = labeled[0]["run_id"]
    return parents


def _rollup_usage(usage: dict, parents: dict) -> dict:
    for child, parent in parents.items():
        if child == parent or child not in usage:
            continue
        usage[parent] = _merge_usage(usage.get(parent), usage[child])
        usage[child] = (_zeros(), 0.0, False)
    return usage


def _merge_usage(left, right):
    if left is None:
        return right
    if right is None:
        return left
    left_tokens, left_cost, left_unpriced = left
    right_tokens, right_cost, right_unpriced = right
    folded = {}
    for key in TOKEN_FIELDS:
        left_value = left_tokens.get(key)
        right_value = right_tokens.get(key)
        if left_value is None and right_value is None:
            folded[key] = None
        else:
            folded[key] = (left_value or 0) + (right_value or 0)
    if left_cost is None and right_cost is None:
        cost = None
    else:
        cost = (left_cost or 0) + (right_cost or 0)
    return folded, cost, bool(left_unpriced or right_unpriced)


def _local_midnight(now: float) -> float:
    moment = datetime.fromtimestamp(float(now)).astimezone()
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _agent_counts(runs: list, parents: dict, now: float, since, until, live_open=None) -> dict:
    range_since = now - RANGE_S if since is None else float(since)
    range_until = float(now if until is None else until)
    today = _local_midnight(now)
    live_by = {key: 0 for key in TOOL_KEYS}
    live_by_model: dict = {}
    seen = set()
    live_n = 0
    prod_jobs = 0
    finished_today = 0
    finished_range = 0
    by_verdict = {}
    for run in runs:
        if run["run_id"] in parents:
            continue
        if run.get("source") == "dagster" and run.get("status") in ("running", "queued"):
            if live_open is None or run["run_id"] in live_open:
                prod_jobs += 1
        if not _is_counted_agent(run):
            continue
        if run.get("status") == "running":
            if live_open is not None and run["run_id"] not in live_open:
                continue
            tool = _tool_key(run)
            key = (tool, run.get("label") or run["run_id"])
            if key in seen:
                continue
            seen.add(key)
            live_by[tool] += 1
            model = run_model_key(run)
            live_by_model[model] = live_by_model.get(model, 0) + 1
            live_n += 1
            continue
        if run.get("status") not in FINISHED or run.get("ended_at") is None:
            continue
        ended = float(run["ended_at"])
        if today <= ended <= now:
            finished_today += 1
        if range_since <= ended <= range_until:
            finished_range += 1
            verdict = run.get("verdict") or "none"
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
    return {
        "live_agents": live_n,
        "live_by_tool": live_by,
        "live_by_model": live_by_model,
        "prod_jobs": prod_jobs,
        "finished_today": finished_today,
        "finished_in_range": finished_range,
        "finished_by_verdict": by_verdict,
    }


def _drop_launch_echoes(rows: list) -> list:
    """Merge launch-manifest rows into the run they record.

    A launcher writes a manifest row for every worker it starts, and the
    worker's own transcript (Cursor chat, muse session) makes a second row for
    the same run. A manifest row whose label matches a non-manifest run from
    the same source that started within ten minutes is that same run (a
    ws-wave Haiku wrapper shares its muse run's label but is not it): it is dropped, and its verdict
    and output size fill any gap in the twin. A manifest row with no twin stays.
    """
    by_label: dict = {}
    for row in rows:
        by_label.setdefault(row.get("label"), []).append(row)
    out = []
    for row in rows:
        start = row.get("started_at")
        twin = None
        if row.get("clock_source") == "manifest" and start is not None and row.get("label"):
            for other in by_label.get(row.get("label"), []):
                if (
                    other is not row
                    and other.get("clock_source") != "manifest"
                    and other.get("source") == row.get("source")
                    and other.get("started_at") is not None
                    and abs(other["started_at"] - start) <= 600
                ):
                    twin = other
                    break
        if twin is None:
            out.append(row)
            continue
        for key in ("verdict", "out_bytes", "workstream"):
            if not twin.get(key) and row.get(key):
                twin[key] = row[key]
    return _one_run_per_process(out)


def _one_run_per_process(rows: list) -> list:
    """Keep one running row per (label, pid).

    Two chats started in the same second can both be matched to one label, so
    one live process shows twice. The row with a model id and the most output
    is the real one.
    """
    best: dict = {}
    for row in rows:
        if row.get("status") != "running" or not row.get("pid"):
            continue
        key = (row.get("label"), row.get("pid"))
        rank = (bool(row.get("model")), row.get("out_bytes") or 0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, id(row))
    keep = {value[1] for value in best.values()}
    return [
        row
        for row in rows
        if row.get("status") != "running" or not row.get("pid") or id(row) in keep
    ]


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
