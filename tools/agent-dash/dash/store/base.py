"""Store foundation: connect, schema, migrations, offsets, doctor.

Owns the connection lifecycle, the schema (runs, usage, buckets,
samples, sessions, offsets, meta, dagster_seen, run_aliases,
doctor_problems, doctor_meta), the migrations, offset CRUD, and
doctor persistence. The single RLock is created in
``BaseMixin.__init__`` and is the facade lock; every method here
uses it, and no ``_locked`` helper takes it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time as _time

from pathlib import Path

from .util import (PRUNE_BATCH, RUN_COLS, SESSION_COLS,
                 _SEV_ORDER)


class BaseMixin:
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
            self._model_stats_cache = None
            self._workstreams_cache: dict = {}
            self._prune_after = 0
            self._pruned_since_vacuum = 0
            self._prune_cycles = 0
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
                if row is None and isinstance(path, str) and ":cursor/" in path:
                    target = self._resolve_alias_locked(path)
                    if target != path:
                        row = self._conn.execute(
                            "SELECT inode, offset FROM file_offsets WHERE path=?", (target,)
                        ).fetchone()
            if row is None:
                return None
            return (row["inode"], row["offset"])
        def set_offset(self, path: str, inode: int, offset: int, size: int, mtime: float) -> None:
            with self._lock:
                if isinstance(path, str) and ":cursor/" in path:
                    path = self._resolve_alias_locked(path)
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
        def prune_offsets(self, batch: int = PRUNE_BATCH) -> int:
            """Delete up to ``batch`` offset rows whose path no longer exists.

            Only absolute file paths are candidates: rotation cursors and scan
            stamps (``muse:fresh-scan``, ``cursor:rotate-hot``, ...) are
            relative and always kept, and per-blob markers (``<db>#b#<id>``)
            are judged by their database path. One call is one tick's worth
            of work: it scans forward from where the last call stopped
            (keyset on rowid, wrapping at the end), so repeated calls at the
            reconcile cadence drain the table without a long pause. Returns
            the number of rows deleted. When a full pass completes and rows
            were deleted along the way, one VACUUM reclaims the space.
            A return of 0 means this batch was clean, not that the table is
            drained; ``prune_cycles`` counts completed passes.
            """
            try:
                limit = int(batch)
            except (TypeError, ValueError):
                limit = PRUNE_BATCH
            if limit <= 0:
                limit = PRUNE_BATCH
            with self._lock:
                rows = self._conn.execute(
                    "SELECT rowid, path FROM file_offsets WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (self._prune_after, limit),
                ).fetchall()
                if not rows:
                    self._prune_after = 0
                    self._prune_cycles += 1
                    if self._pruned_since_vacuum > 0:
                        self._conn.execute("VACUUM")
                        self._conn.commit()
                        self._pruned_since_vacuum = 0
                    return 0
                self._prune_after = rows[-1][0]
                gone = [row[1] for row in rows if _offset_path_gone(row[1])]
                if gone:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._conn.executemany(
                            "DELETE FROM file_offsets WHERE path=?",
                            [(path,) for path in gone],
                        )
                        self._conn.execute("COMMIT")
                    except Exception:
                        self._conn.execute("ROLLBACK")
                        raise
                    self._pruned_since_vacuum += len(gone)
                    self._data_seq += 1
                    self._seq += 1
                return len(gone)
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
                  ws_confidence TEXT,
                  ws_hint TEXT
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
                CREATE TABLE IF NOT EXISTS run_aliases (
                  alias_run_id TEXT PRIMARY KEY,
                  target_run_id TEXT NOT NULL,
                  merged_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS doctor_problems (
                  id TEXT PRIMARY KEY,
                  code TEXT,
                  severity TEXT,
                  scope TEXT,
                  ws TEXT,
                  title TEXT,
                  detail TEXT,
                  fix_command TEXT,
                  safe_auto INTEGER DEFAULT 0,
                  first_seen REAL,
                  last_seen REAL,
                  count INTEGER DEFAULT 1,
                  resolved_at REAL,
                  regressed INTEGER DEFAULT 0,
                  items_json TEXT
                );
                CREATE TABLE IF NOT EXISTS doctor_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_doctor_code ON doctor_problems(code);
                CREATE INDEX IF NOT EXISTS idx_doctor_open ON doctor_problems(resolved_at);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS idx_runs_ended ON runs(ended_at);
                CREATE INDEX IF NOT EXISTS idx_runs_ws ON runs(workstream);
                CREATE INDEX IF NOT EXISTS idx_runs_pid ON runs(pid);
                CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source);
                CREATE INDEX IF NOT EXISTS idx_runs_label ON runs(label);
                CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_session_id);
                CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
                CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
                CREATE INDEX IF NOT EXISTS idx_samples_ts ON process_samples(ts);
                CREATE INDEX IF NOT EXISTS idx_samples_pid ON process_samples(pid);
                CREATE INDEX IF NOT EXISTS idx_samples_run ON process_samples(run_id);
                CREATE INDEX IF NOT EXISTS idx_aliases_target ON run_aliases(target_run_id);
                """
            )
            self._migrate_usage_splits()
            self._migrate_uncached_input()
            self._migrate_run_aliases()
            self._migrate_session_hint()
            self._migrate_doctor_items()
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
        def _migrate_run_aliases(self) -> None:
            """Once: durable merge aliases so merged manifest rows stay merged.

            Existing databases opened after the upgrade gain ``run_aliases``;
            the merge pass (first cursor poll) and the ingest path then
            backfill one alias per folded pair, so restart re-ingests resolve
            to the chat row instead of resurrecting deleted manifest rows.
            """
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS run_aliases (
                     alias_run_id TEXT PRIMARY KEY,
                     target_run_id TEXT NOT NULL,
                     merged_at REAL NOT NULL
                   )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_target ON run_aliases(target_run_id)"
            )
        def _migrate_session_hint(self) -> None:
            """Once: bundle-write evidence for lead workstream inference."""
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
            if "ws_hint" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN ws_hint TEXT")
        def _migrate_usage_splits(self) -> None:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(usage_events)")}
            for name in ("cache_write_5m", "cache_write_1h"):
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} INTEGER")
            sample_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(process_samples)")}
            if "model" not in sample_cols:
                self._conn.execute("ALTER TABLE process_samples ADD COLUMN model TEXT")
        def _migrate_doctor_items(self) -> None:
            """Once: grouped problems carry compact member rows.

            Existing databases gain ``items_json`` on ``doctor_problems``;
            old per-item rows (e.g. ``dep-jump:ws:WS9:WS9.2``) need no
            rewrite: the next snapshot only emits group ids, so absent
            old ids resolve on that first run.
            """
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(doctor_problems)")}
            if "items_json" not in cols:
                self._conn.execute("ALTER TABLE doctor_problems ADD COLUMN items_json TEXT")
        def doctor_snapshot(self, problems: list, now: float, interval_s: float = 300) -> dict:
            """Persist one doctor run and return the ``GET /api/doctor`` payload.

            ``problems`` are fresh dicts with ``id, code, severity, scope, ws,
            title, detail, fix_command, safe_auto`` plus optional ``items``
            (compact ``{title, id, since}`` member rows for grouped
            problems). A problem absent from this run resolves
            (``resolved_at`` set, so old per-item ids resolve on the first
            grouped run); one that reappears after a resolution is
            ``regressed`` (sticky). Resolved rows older than 14 days are
            deleted. Never raises for bad rows: they are skipped.
            """
            now = float(now)
            try:
                interval = float(interval_s)
            except (TypeError, ValueError):
                interval = 300.0
            if interval <= 0:
                interval = 300.0
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    seen: set[str] = set()
                    for item in problems or []:
                        try:
                            pid = str(item.get("id"))
                        except (TypeError, ValueError):
                            continue
                        if not pid:
                            continue
                        seen.add(pid)
                        items_json = _doctor_items_json(
                            item.get("items") if isinstance(item, dict) else None
                        )
                        row = self._conn.execute(
                            "SELECT count, resolved_at, regressed FROM doctor_problems WHERE id=?",
                            (pid,),
                        ).fetchone()
                        if row is None:
                            self._conn.execute(
                                """INSERT INTO doctor_problems(
                                     id, code, severity, scope, ws, title, detail,
                                     fix_command, safe_auto, first_seen, last_seen,
                                     count, resolved_at, regressed, items_json)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    pid,
                                    str(item.get("code") or ""),
                                    str(item.get("severity") or "warn"),
                                    str(item.get("scope") or "global"),
                                    item.get("ws"),
                                    str(item.get("title") or ""),
                                    str(item.get("detail") or ""),
                                    str(item.get("fix_command") or ""),
                                    1 if item.get("safe_auto") else 0,
                                    now,
                                    now,
                                    1,
                                    None,
                                    0,
                                    items_json,
                                ),
                            )
                        else:
                            regressed = 1 if row["regressed"] else 0
                            resolved_at = None
                            if row["resolved_at"] is not None:
                                regressed = 1
                            self._conn.execute(
                                """UPDATE doctor_problems SET
                                     code=?, severity=?, scope=?, ws=?, title=?, detail=?,
                                     fix_command=?, safe_auto=?, last_seen=?,
                                     count=COALESCE(count, 0) + 1,
                                     resolved_at=?, regressed=?, items_json=?
                                   WHERE id=?""",
                                (
                                    str(item.get("code") or ""),
                                    str(item.get("severity") or "warn"),
                                    str(item.get("scope") or "global"),
                                    item.get("ws"),
                                    str(item.get("title") or ""),
                                    str(item.get("detail") or ""),
                                    str(item.get("fix_command") or ""),
                                    1 if item.get("safe_auto") else 0,
                                    now,
                                    resolved_at,
                                    regressed,
                                    items_json,
                                    pid,
                                ),
                            )
                    if seen:
                        marks = ",".join("?" for _ in seen)
                        self._conn.execute(
                            f"""UPDATE doctor_problems SET resolved_at=?
                                WHERE resolved_at IS NULL AND id NOT IN ({marks})""",
                            (now, *sorted(seen)),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE doctor_problems SET resolved_at=? WHERE resolved_at IS NULL",
                            (now,),
                        )
                    self._conn.execute(
                        "DELETE FROM doctor_problems WHERE resolved_at IS NOT NULL AND resolved_at < ?",
                        (now - 14 * 86400,),
                    )
                    self._conn.execute(
                        "INSERT OR REPLACE INTO doctor_meta (key, value) VALUES ('generated_at', ?)",
                        (str(now),),
                    )
                    self._conn.execute(
                        "INSERT OR REPLACE INTO doctor_meta (key, value) VALUES ('interval_s', ?)",
                        (str(interval),),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                return self._doctor_latest_locked(now, None, None)
        def doctor_latest(
            self,
            now: float,
            ws: str | None = None,
            min_severity: str | None = None,
            include_all: bool = False,
        ) -> dict:
            """The ``GET /api/doctor`` payload from the latest persisted run.

            Never runs checks: it only reads. ``ws`` keeps that workstream's
            problems plus global ones. ``min_severity`` (critical|warn|info) is
            the least severe level included. Unknown values yield no filtering.
            ``include_all`` (``?all=1``) returns every member row; by
            default each problem carries up to 20.
            """
            with self._lock:
                return self._doctor_latest_locked(float(now), ws, min_severity, include_all)
        def _doctor_latest_locked(self, now: float, ws, min_severity, include_all: bool = False) -> dict:
            meta = {
                row["key"]: row["value"]
                for row in self._conn.execute("SELECT key, value FROM doctor_meta").fetchall()
            }
            try:
                generated_at = float(meta["generated_at"]) if meta.get("generated_at") else None
            except (TypeError, ValueError):
                generated_at = None
            try:
                interval_s = float(meta.get("interval_s") or 300.0)
            except (TypeError, ValueError):
                interval_s = 300.0
            if interval_s <= 0:
                interval_s = 300.0
            rows = [
                dict(row)
                for row in self._conn.execute(
                    """SELECT * FROM doctor_problems WHERE resolved_at IS NULL
                       ORDER BY
                         CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                         code, id"""
                ).fetchall()
            ]
            summary = {"critical": 0, "warn": 0, "info": 0, "regressed": 0}
            items_total = 0
            for row in rows:
                if row.get("severity") in summary:
                    summary[row["severity"]] += 1
                if row.get("regressed"):
                    summary["regressed"] += 1
                items_total += len(_doctor_items(row))
            if items_total:
                summary["items_total"] = items_total
            resolved = [
                {
                    "id": row["id"],
                    "code": row["code"],
                    "title": row["title"],
                    "resolved_at": row["resolved_at"],
                }
                for row in self._conn.execute(
                    """SELECT id, code, title, resolved_at FROM doctor_problems
                       WHERE resolved_at IS NOT NULL AND resolved_at >= ?
                       ORDER BY resolved_at DESC""",
                    (now - 86400,),
                ).fetchall()
            ]
            problems = [
                _doctor_api_row(row, include_all)
                for row in rows
                if _doctor_visible(row, ws, min_severity)
            ]
            next_run_at = (
                generated_at + interval_s if generated_at is not None else now + interval_s
            )
            return {
                "generated_at": generated_at,
                "next_run_at": next_run_at,
                "interval_s": interval_s,
                "summary": summary,
                "problems": problems,
                "resolved_24h": resolved,
            }
        def doctor_report(self, since: float, now: float) -> dict:
            """Digest for hub reports: new, regressed, resolved, open by severity and workstream."""
            since = float(since)
            now = float(now)
            with self._lock:
                rows = [
                    dict(row) for row in self._conn.execute("SELECT * FROM doctor_problems").fetchall()
                ]
            open_rows = [row for row in rows if row.get("resolved_at") is None]
            by_severity: dict[str, int] = {"critical": 0, "warn": 0, "info": 0}
            by_ws: dict[str, int] = {}
            for row in open_rows:
                if row.get("severity") in by_severity:
                    by_severity[row["severity"]] += 1
                key = row.get("ws") or "global"
                by_ws[key] = by_ws.get(key, 0) + 1
            return {
                "since": since,
                "now": now,
                "open": [_doctor_api_row(row) for row in sorted(
                    open_rows, key=lambda item: (_SEV_ORDER.get(item.get("severity"), 9), item.get("id") or "")
                )],
                "open_by_severity": by_severity,
                "open_by_ws": dict(sorted(by_ws.items())),
                "new": [_doctor_api_row(row) for row in rows if (row.get("first_seen") or 0) >= since],
                "regressed": [_doctor_api_row(row) for row in open_rows if row.get("regressed")],
                "resolved": [
                    {
                        "id": row["id"],
                        "code": row["code"],
                        "title": row["title"],
                        "resolved_at": row["resolved_at"],
                    }
                    for row in rows
                    if row.get("resolved_at") is not None and row["resolved_at"] >= since
                ],
            }
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


def _doctor_items_json(items) -> str | None:
    """Compact member rows for storage, or None when there are none."""
    if not isinstance(items, list) or not items:
        return None
    compact = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("id") or "")
        ident = str(entry.get("id") or "")
        if not ident and not title:
            continue
        try:
            since = float(entry.get("since")) if entry.get("since") is not None else None
        except (TypeError, ValueError):
            since = None
        compact.append({"title": title, "id": ident, "since": since})
    if not compact:
        return None
    import json as _json

    try:
        return _json.dumps(compact)
    except (TypeError, ValueError):
        return None


def _doctor_items(row: dict) -> list:
    """All persisted member rows for one problem (possibly empty)."""
    raw = row.get("items_json") if isinstance(row, dict) else None
    if not raw:
        items = row.get("items") if isinstance(row, dict) else None
        if isinstance(items, list):
            return [
                {"title": str(entry.get("title") or entry.get("id") or ""),
                 "id": str(entry.get("id") or ""),
                 "since": entry.get("since")}
                for entry in items
                if isinstance(entry, dict)
            ]
        return []
    import json as _json

    try:
        data = _json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "title": str(entry.get("title") or entry.get("id") or ""),
                "id": str(entry.get("id") or ""),
                "since": entry.get("since"),
            }
        )
    return out


def _doctor_api_row(row: dict, include_all: bool = False) -> dict:
    """One persisted problem in ``GET /api/doctor`` shape.

    Grouped problems also carry ``items`` (up to 20 compact member
    rows by default, every row with ``?all=1``) and ``items_total``
    (the full member count). Both keys are present only when the
    problem has members, so rows without members keep the old shape.
    """
    code = str(row.get("code") or "")
    out = {
        "id": str(row.get("id") or ""),
        "code": code,
        "severity": str(row.get("severity") or "warn"),
        "scope": str(row.get("scope") or "global"),
        "ws": row.get("ws"),
        "title": str(row.get("title") or ""),
        "detail": str(row.get("detail") or ""),
        "fix": {
            "command": str(row.get("fix_command") or ""),
            "safe_auto": bool(row.get("safe_auto")),
            "doc": f"docs/problems/{code}.md" if code else "",
        },
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
        "count": row.get("count") or 0,
        "regressed": bool(row.get("regressed")),
    }
    members = _doctor_items(row)
    if members:
        out["items_total"] = len(members)
        out["items"] = members if include_all else members[:20]
    return out


def _doctor_visible(row: dict, ws, min_severity) -> bool:
    """Workstream and minimum-severity filters for ``GET /api/doctor``."""
    if ws not in (None, "", "all"):
        scope = str(row.get("scope") or "global")
        if scope != "global" and scope != f"ws:{ws}" and row.get("ws") != ws:
            return False
    if min_severity in _SEV_ORDER:
        rank = _SEV_ORDER.get(str(row.get("severity")), 9)
        if rank > _SEV_ORDER[min_severity]:
            return False
    return True


def _offset_path_gone(path) -> bool:
    """True when an offset row no longer refers to a live file.

    Relative keys are rotation cursors and scan stamps, never files, so
    they are always kept. Per-blob markers carry the database path before
    ``#b#`` and are pruned only when that database is gone.
    """
    if not isinstance(path, str) or not path:
        return False
    if "#b#" in path:
        head = path.split("#b#", 1)[0]
        return os.path.isabs(head) and not os.path.exists(head)
    return os.path.isabs(path) and not os.path.exists(path)
