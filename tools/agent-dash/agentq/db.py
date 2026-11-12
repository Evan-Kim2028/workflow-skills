"""agentq schema, migrations and the job and event write helpers.

This module is the only code that writes the ``jobs`` and ``events`` tables.
The broker daemon and the CLI both call it. A state change is one conditional
``UPDATE ... WHERE state IN (...)`` inside one ``BEGIN IMMEDIATE`` transaction
(``transition``), so a change that lost a race (a cancel, a finish) matches no
row, writes no event, and the caller sees ``False``.
"""

from __future__ import annotations

import contextlib
import os
import random
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 2
LANES = ("urgent", "normal", "backlog")
KINDS = ("read", "write", "review")
RESOURCES = ("light", "tests", "ssh")
OPEN_STATES = ("queued", "running")
TERMINAL_STATES = ("done", "failed", "cancelled")
AGING_S = 15 * 60
MAX_RUNTIME_S = 6 * 3600
DEFAULT_HOME = "~/.local/state/agentq"
BUSY_TIMEOUT_MS = 30000
CONNECT_TIMEOUT_S = 30
RETRY_BUDGET_S = 60.0
BACKOFF_MIN_S = 0.05
BACKOFF_MAX_S = 1.0

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
        orig_workspace TEXT,
        in_place INTEGER NOT NULL DEFAULT 0,
        session TEXT NOT NULL,
        harness TEXT NOT NULL,
        prompt_path TEXT NOT NULL,
        run_dir TEXT,
        pid INTEGER,
        pgid INTEGER,
        pid_start TEXT,
        pi_session TEXT,
        attempt INTEGER NOT NULL DEFAULT 0,
        rate_attempts INTEGER NOT NULL DEFAULT 0,
        watchdog_trips INTEGER NOT NULL DEFAULT 0,
        capacity_refusals INTEGER NOT NULL DEFAULT 0,
        max_runtime_s REAL NOT NULL DEFAULT 21600,
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

# Each step upgrades a store from the version it names to the next one.
_UPGRADES = {
    1: (
        "ALTER TABLE jobs ADD COLUMN orig_workspace TEXT",
        "ALTER TABLE jobs ADD COLUMN in_place INTEGER NOT NULL DEFAULT 0",
    ),
}

JOB_COLUMNS = frozenset(
    {
        "id", "queue_seq", "lane", "state", "kind", "res", "tier", "label",
        "workspace", "orig_workspace", "in_place", "session", "harness", "prompt_path", "run_dir", "pid",
        "pgid", "pid_start", "pi_session", "attempt", "rate_attempts",
        "watchdog_trips", "capacity_refusals", "max_runtime_s", "not_before",
        "verdict", "report_path", "detail", "submitted_at", "started_at",
        "finished_at", "priority_age_at",
    }
)  # fmt: skip


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


def _is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def retrying(action, *, sleep=time.sleep, clock=time.monotonic, jitter=random.random):
    """Run ``action`` and retry on lock contention until ``RETRY_BUDGET_S`` is spent.

    Backoff starts at ``BACKOFF_MIN_S`` and doubles to ``BACKOFF_MAX_S``, with
    jitter so concurrent processes do not retry in step. Any other error, and a
    busy error after the budget, is raised. Only use it around work that has no
    effect when it fails (opening, a pragma, ``BEGIN``).
    """
    deadline = clock() + RETRY_BUDGET_S
    delay = BACKOFF_MIN_S
    while True:
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or clock() >= deadline:
                raise
        sleep(min(BACKOFF_MAX_S, delay * (0.5 + jitter())))
        delay = min(BACKOFF_MAX_S, delay * 2)


def connect(home) -> sqlite3.Connection:
    """Open (and create or migrate) the store. Autocommit; writes go through ``tx``.

    A connection that finds the schema and WAL already in place takes no write lock.
    """
    Path(home).mkdir(parents=True, exist_ok=True)

    def open_ready():
        conn = sqlite3.connect(db_path(home), timeout=CONNECT_TIMEOUT_S, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            ensure_wal(conn)
            migrate(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    return retrying(open_ready)


def ensure_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL only when it is not already set. The switch needs an exclusive
    lock and returns busy without waiting, so it runs under the retry."""
    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
        return
    retrying(lambda: conn.execute("PRAGMA journal_mode = WAL").fetchone())


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema once, or upgrade an older store step by step.

    A current schema is read without a write lock.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return
    with tx(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            for statement in _STATEMENTS:
                conn.execute(statement)
        else:
            for step in range(version, SCHEMA_VERSION):
                for statement in _UPGRADES[step]:
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """One write transaction; rolls back on any exception. Do not nest.

    Only ``BEGIN IMMEDIATE`` is retried on contention: it has not started anything
    when it fails. Callers must not hold a transaction across a subprocess or slow I/O.
    """
    retrying(lambda: conn.execute("BEGIN IMMEDIATE"))
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
        "max_runtime_s": MAX_RUNTIME_S,
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


def transition(conn, job_id: str, from_states, now: float, kind: str | None, note: str, **fields) -> bool:
    """Set ``fields`` only if the job is still in one of ``from_states``.

    Returns False and writes nothing when the job has moved on (a cancel or a
    finish won the race). ``kind=None`` updates without an event row. One
    transaction; the caller must not hold ``tx``.
    """
    unknown = set(fields) - JOB_COLUMNS
    if unknown:
        raise ValueError(f"not a jobs column: {sorted(unknown)}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    marks = ", ".join("?" for _ in from_states)
    sql = f"UPDATE jobs SET {assignments} WHERE id = ? AND state IN ({marks})"
    with tx(conn):
        cur = conn.execute(sql, (*fields.values(), job_id, *from_states))
        if cur.rowcount != 1:
            return False
        if kind is not None:
            add_event(conn, job_id, kind, note, now)
    return True


def count_events(conn, job_id: str, kind: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM events WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
    return row[0]


def front_queue_seq(conn, lane: str) -> int:
    """A queue position ahead of every queued job in ``lane``."""
    row = conn.execute(
        "SELECT COALESCE(MIN(queue_seq), 0) - 1 FROM jobs WHERE lane = ? AND state = 'queued'",
        (lane,),
    ).fetchone()
    return row[0]


def get_job(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def select_jobs(conn, where: str = "", params=()) -> list[dict]:
    """Jobs matching a SQL ``WHERE`` clause (no keyword), oldest submission first."""
    clause = f"WHERE {where}" if where else ""
    rows = conn.execute(f"SELECT * FROM jobs {clause} ORDER BY submitted_at, rowid", tuple(params))
    return [dict(row) for row in rows.fetchall()]
