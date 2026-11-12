"""agentq schema, migrations and the job and event write helpers.

This module is the only code that writes the ``jobs`` and ``events`` tables.
The broker daemon and the CLI both call it. Every state change runs inside one
``BEGIN IMMEDIATE`` transaction (``tx``), so processes sharing the WAL file
serialize on the write lock and no reader sees half a change.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
LANES = ("urgent", "normal", "backlog")
KINDS = ("read", "write", "review")
RESOURCES = ("light", "tests", "ssh")
OPEN_STATES = ("queued", "running")
TERMINAL_STATES = ("done", "failed", "cancelled")
AGING_S = 15 * 60
DEFAULT_HOME = "~/.local/state/agentq"

_STATEMENTS = (
    """CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        queue_seq INTEGER NOT NULL,
        lane TEXT NOT NULL,
        state TEXT NOT NULL,
        kind TEXT NOT NULL,
        res TEXT NOT NULL,
        tier TEXT NOT NULL,
        label TEXT NOT NULL,
        workspace TEXT NOT NULL,
        session TEXT NOT NULL,
        harness TEXT NOT NULL,
        prompt_path TEXT NOT NULL,
        run_dir TEXT,
        pid INTEGER,
        pgid INTEGER,
        pid_start TEXT,
        pi_session TEXT,
        attempt INTEGER NOT NULL DEFAULT 0,
        not_before REAL NOT NULL DEFAULT 0,
        verdict TEXT,
        report_path TEXT,
        detail TEXT,
        submitted_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        priority_age_at REAL NOT NULL
    )""",
    "CREATE INDEX jobs_queue ON jobs(state, lane, queue_seq)",
    "CREATE INDEX jobs_session ON jobs(session, state)",
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        job_id TEXT,
        kind TEXT NOT NULL,
        detail TEXT
    )""",
)

JOB_COLUMNS = frozenset(
    {
        "id", "queue_seq", "lane", "state", "kind", "res", "tier", "label",
        "workspace", "session", "harness", "prompt_path", "run_dir", "pid",
        "pgid", "pid_start", "pi_session", "attempt", "not_before", "verdict",
        "report_path", "detail", "submitted_at", "started_at", "finished_at",
        "priority_age_at",
    }
)


def home_dir(env=None) -> Path:
    """The agentq home: ``$AGENTQ_HOME``, else ``~/.local/state/agentq``."""
    env = os.environ if env is None else env
    return Path(os.path.expanduser(env.get("AGENTQ_HOME") or DEFAULT_HOME))


def db_path(home) -> Path:
    return Path(home) / "agentq.db"


def job_dir(home, job_id: str) -> Path:
    """Per-job folder: ``prompt.md``, ``report.md`` and ``runs/<n>/`` attempts."""
    return Path(home) / "jobs" / job_id


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def connect(home) -> sqlite3.Connection:
    """Open (and create or migrate) the store. Autocommit; use ``tx`` to write."""
    Path(home).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(home), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema once. ``user_version`` is checked under the write lock."""
    with tx(conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            for statement in _STATEMENTS:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """One write transaction; rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def add_event(conn, job_id, kind: str, detail: str | None, ts: float) -> None:
    conn.execute(
        "INSERT INTO events(ts, job_id, kind, detail) VALUES (?, ?, ?, ?)",
        (ts, job_id, kind, detail),
    )


def insert_job(conn, job: dict, now: float) -> None:
    """Queue a new job at the back of the queue. Call inside ``tx``."""
    seq = conn.execute("SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM jobs").fetchone()[0]
    row = {
        **job,
        "queue_seq": seq,
        "state": "queued",
        "attempt": 0,
        "not_before": 0.0,
        "submitted_at": now,
        "priority_age_at": now + AGING_S,
    }
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({marks})", tuple(row.values()))
    add_event(conn, job["id"], "submit", f"{job['kind']} {job['tier']} {job['lane']}", now)


def front_queue_seq(conn, lane: str) -> int:
    """A queue position ahead of every queued job in ``lane``."""
    row = conn.execute(
        "SELECT COALESCE(MIN(queue_seq), 0) - 1 FROM jobs WHERE lane = ? AND state = 'queued'",
        (lane,),
    ).fetchone()
    return row[0]


def update_job(conn, job_id: str, **fields) -> None:
    """Set columns on one job. Call inside ``tx``."""
    unknown = set(fields) - JOB_COLUMNS
    if unknown:
        raise ValueError(f"not a jobs column: {sorted(unknown)}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))


def get_job(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def select_jobs(conn, where: str = "", params=()) -> list[dict]:
    """Jobs matching a SQL ``WHERE`` clause (no keyword), oldest submission first."""
    clause = f"WHERE {where}" if where else ""
    rows = conn.execute(f"SELECT * FROM jobs {clause} ORDER BY submitted_at, rowid", tuple(params))
    return [dict(row) for row in rows.fetchall()]
