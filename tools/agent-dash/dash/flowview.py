"""Execution flow: where the work sits and what blocks it.

``GET /api/flow`` folds two payloads the server already caches, the
``/api/state`` workstream rows (each carrying its plan ``progress``
counts) and the latest persisted doctor run, into the one shape the
flow card draws. Nothing here rescans a plan or runs a check, so the
call is a fold over two dicts.

The payload is read-only and carries no prompt text, argv, secrets or
environment values. A blocker keeps its code, workstream, severity,
count and title; its commands are generated here from the workstream
id, never copied from a stored fix string.
"""

from __future__ import annotations

import re

#: The queue order the card draws, left to right. ``done`` is a total,
#: not a queue: finished work is not waiting on anything.
QUEUES = (
    ("todo", "Todo"),
    ("wip", "In progress"),
    ("pr", "Waiting to merge"),
    ("prog", "On prod"),
    ("deferred", "Deferred"),
)

SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}

#: A workstream id that may be interpolated into a command. Anything else
#: falls back to the project-wide command, so a stored id can never smuggle
#: shell text into the payload.
_WS = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def build_flow(state, doctor) -> dict:
    """The ``/api/flow`` payload from an ``/api/state`` body and a doctor body.

    ``state`` is the ``/api/state`` payload (only ``workstreams`` is read);
    ``doctor`` is the ``/api/doctor`` payload (only ``generated_at`` and
    ``problems`` are read). Both may be ``None`` or partial: a missing
    payload yields the empty flow, never an error.
    """
    rows = _workstreams(state)
    queues = [
        {"key": key, "label": label, "count": sum(row["counts"].get(key, 0) for row in rows)}
        for key, label in QUEUES
    ]
    blockers = _blockers(doctor)
    tasks = sum(row["total"] for row in rows)
    done = sum(row["done"] for row in rows)
    return {
        "generated_at": _generated_at(doctor),
        "queues": queues,
        "blockers": blockers,
        "workstreams": rows,
        "totals": {
            "workstreams": len(rows),
            "tasks": tasks,
            "done": done,
            "pct": _pct(done, tasks),
            "blockers": len(blockers),
            "blocked_tasks": sum(item["count"] for item in blockers),
        },
        "empty": not rows,
    }


def _workstreams(state) -> list:
    """One row per workstream that carries plan progress, in state order."""
    out = []
    for row in (state or {}).get("workstreams") or []:
        if not isinstance(row, dict):
            continue
        progress = row.get("progress")
        if not isinstance(progress, dict):
            continue
        raw = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
        counts = {key: _int(raw.get(key)) for key, _label in QUEUES}
        total = _int(progress.get("total"))
        done = _int(progress.get("done"))
        out.append(
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
                "counts": counts,
                "total": total,
                "done": done,
                "pct": _pct(done, total),
            }
        )
    return out


def _blockers(doctor) -> list:
    """Doctor problems grouped by (code, workstream), worst severity first."""
    groups: dict = {}
    for item in (doctor or {}).get("problems") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if not code:
            continue
        ws = item.get("ws")
        ws = str(ws) if ws else None
        severity = str(item.get("severity") or "warn")
        if severity not in SEVERITY_RANK:
            severity = "warn"
        count = _int(item.get("items_total") if item.get("items_total") is not None else item.get("count"))
        if count <= 0:
            count = 1
        group = groups.get((code, ws))
        if group is None:
            groups[(code, ws)] = {
                "code": code,
                "ws": ws,
                "severity": severity,
                "count": count,
                "title": str(item.get("title") or ""),
            }
            continue
        group["count"] += count
        if SEVERITY_RANK[severity] < SEVERITY_RANK[group["severity"]]:
            group["severity"] = severity
    out = []
    for group in groups.values():
        command, nxt = _commands(group["ws"])
        out.append({**group, "command": command, "next": nxt})
    out.sort(key=lambda item: (SEVERITY_RANK[item["severity"]], -item["count"], item["code"], item["ws"] or ""))
    return out


def _commands(ws):
    """The two real CLI commands for one blocker, built from the workstream id."""
    if ws and _WS.match(ws):
        return (
            f"python3 agent_dash.py doctor --ws {ws}",
            f"python3 agent_dash.py ws next {ws}",
        )
    return ("python3 agent_dash.py doctor", None)


def _generated_at(doctor):
    """The doctor run's generation time, or ``None`` when it never ran."""
    value = doctor.get("generated_at") if isinstance(doctor, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _pct(done: int, total: int) -> int:
    return int(100 * done / total + 0.5) if total else 0
