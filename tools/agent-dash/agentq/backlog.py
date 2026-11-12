"""Recurring backlog templates: a prompt the broker resubmits on a timer.

A template is one row of ``backlog_templates`` plus a copy of its prompt under
``$AGENTQ_HOME/backlog/<name>.md``. The broker submits one job per due, enabled
template into the ``backlog`` lane, so real work always launches first, and only
while the Morph proxy has no queued request for the template's budgets (see
``Broker.submit_backlog``). A template's jobs carry the label
``hub-<mark>-backlog-<name>``, which is how the template finds its open and last job.

This module owns the ``backlog_templates`` table. The broker writes jobs through
``db.insert_job`` as usual.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from agentq import db

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DURATION_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
PREAMBLE = (
    "This is a recurring low-priority backlog job. Read-only unless the brief says "
    "otherwise. Write your report to the path the brief names; do not open PRs or push branches."
)
SESSION = "backlog"


def parse_duration(text: str) -> float:
    """Seconds from ``30m``, ``6h``, ``1d`` (also ``90s``). Raises ValueError otherwise."""
    match = DURATION_PATTERN.match((text or "").strip().lower())
    if not match:
        raise ValueError(f"not a duration: {text!r} (use a number and s, m, h or d, e.g. 6h)")
    seconds = float(match.group(1)) * UNIT_SECONDS[match.group(2)]
    if seconds <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return seconds


def format_duration(seconds: float) -> str:
    """The shortest of ``Nd``, ``Nh``, ``Nm`` or ``Ns`` that holds ``seconds`` exactly, else seconds."""
    for unit in ("d", "h", "m", "s"):
        step = UNIT_SECONDS[unit]
        if seconds >= step and seconds % step == 0:
            return f"{int(seconds // step)}{unit}"
    return f"{seconds:g}s"


def label_slug(name: str) -> str:
    """The slug passed to ``label make`` for a template's jobs."""
    return f"backlog-{name}"


def job_labels(name: str) -> tuple[str, str]:
    """The labels a template's jobs can carry: one per mark (exec and review).

    These mirror ``label_for``: ``hub-<mark>-<slug>``. ``tests/test_agentq_backlog.py``
    checks that the helper returns exactly these strings.
    """
    slug = label_slug(name)
    return f"hub-exec-{slug}", f"hub-review-{slug}"


def tier_for(template: dict) -> str:
    """The tier a template's jobs run on: its own tier, else the kind's default."""
    return template["tier"] or db.default_tier(template["kind"])


def is_due(template: dict, now: float) -> bool:
    """True when the template is enabled and its interval has passed since its last submission."""
    if not template["enabled"]:
        return False
    last = template["last_submitted"]
    return last is None or now - last >= template["every_s"]


def preamble_for(kind: str, body: str) -> str:
    """The prompt a backlog job runs: the read-only preamble, then the template body.

    A write template gets no preamble: its brief decides what it may change.
    """
    if kind == "write":
        return body
    return f"{PREAMBLE}\n\n{body}"


def select_all(conn) -> list[dict]:
    """Every template, by name."""
    rows = conn.execute("SELECT * FROM backlog_templates ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def get(conn, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM backlog_templates WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def open_job(conn, name: str) -> dict | None:
    """The queued or running job of this template, if any."""
    found = db.select_jobs(conn, "label IN (?, ?) AND state IN ('queued', 'running')", job_labels(name))
    return found[0] if found else None


def last_job(conn, name: str) -> dict | None:
    """The newest job of this template, whatever its state."""
    found = db.select_jobs(conn, "label IN (?, ?)", job_labels(name))
    return found[-1] if found else None


def open_backlog_jobs(conn) -> int:
    """How many template jobs are queued or running (any template)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE session = ? AND state IN ('queued', 'running')", (SESSION,)
    ).fetchone()
    return row[0]


def add(conn, home, *, name: str, prompt_file, every_s: float, kind: str, res: str,
        workspace: str, tier: str | None, max_runtime_s: float | None, now: float) -> dict:
    """Store a template and copy its prompt into ``<home>/backlog/<name>.md``.

    Raises ValueError with a message for a bad name, an existing name, a missing
    prompt file or an unknown kind or resource. Call inside ``db.tx``.
    """
    if not NAME_PATTERN.match(name):
        raise ValueError(f"name must be lowercase letters, digits and dashes: {name!r}")
    if kind not in db.KINDS:
        raise ValueError(f"kind must be one of {', '.join(db.KINDS)}")
    if res not in db.RESOURCES:
        raise ValueError(f"res must be one of {', '.join(db.RESOURCES)}")
    source = Path(prompt_file).expanduser()
    if not source.is_file():
        raise ValueError(f"no prompt file at {source}")
    if get(conn, name) is not None:
        raise ValueError(f"backlog template {name} already exists")
    dest = Path(home) / "backlog" / f"{name}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    row = {
        "name": name, "prompt_path": str(dest), "kind": kind, "res": res, "tier": tier,
        "workspace": workspace, "every_s": every_s, "max_runtime_s": max_runtime_s,
        "enabled": 1, "last_submitted": None, "created_at": now,
    }
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO backlog_templates ({', '.join(row)}) VALUES ({marks})", tuple(row.values()))
    return row


def remove(conn, home, name: str) -> bool:
    """Delete a template and its prompt copy. Jobs it submitted stay in the history."""
    if get(conn, name) is None:
        return False
    conn.execute("DELETE FROM backlog_templates WHERE name = ?", (name,))
    (Path(home) / "backlog" / f"{name}.md").unlink(missing_ok=True)
    return True


def set_enabled(conn, name: str, enabled: bool) -> bool:
    """Pause (``False``) or resume (``True``) a template. Returns False when it does not exist."""
    cur = conn.execute("UPDATE backlog_templates SET enabled = ? WHERE name = ?", (int(enabled), name))
    return cur.rowcount == 1


def mark_submitted(conn, name: str, now: float) -> None:
    conn.execute("UPDATE backlog_templates SET last_submitted = ? WHERE name = ?", (now, name))
