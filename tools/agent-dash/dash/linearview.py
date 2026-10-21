"""Payload for the Linear card on the Execution view (``GET /api/linear``).

Pure read over the linked bundles: each linked workstream's ``linear.json``
(sync state, project link, pending approval refs), its ``history.jsonl``
(status moves, ``by: "linear"`` rows counted as Linear, the rest as local),
its ``approvals.json`` (pending items) and ``plan.json`` (task titles). Never
writes, never calls the network, never includes a token: the sign-in is the
method word only (``oauth``, ``key`` or ``none``).
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from dash import bundles, linear, linear_auth

WINDOWS = (("changes_24h", 24 * 3600), ("changes_7d", 7 * 24 * 3600))
RECENT_LIMIT = 20
REFRESH_S = 15.0

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"key": None, "at": 0.0, "body": None}


def _history_rows(path: Path) -> list[dict]:
    """Every ``history.jsonl`` row with a numeric ``ts``, oldest first. Unreadable lines are skipped."""
    try:
        text = (path / "history.jsonl").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        ts = row.get("ts") if isinstance(row, dict) else None
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        rows.append(row)
    rows.sort(key=lambda row: row["ts"])
    return rows


def _plan_body(directory: Path | None) -> dict:
    """The bundle's ``plan.json`` as a dict, or {} when absent or unreadable."""
    if directory is None:
        return {}
    data = bundles._read_json(directory / "plan.json")
    return data if isinstance(data, dict) else {}


def _task_titles(plan) -> dict[str, str]:
    """{task id: title} from the plan's dag groups; empty when the plan has none."""
    titles: dict[str, str] = {}
    dag = plan.get("dag")
    groups = dag.get("groups") if isinstance(dag, dict) else None
    for group in groups or []:
        for task in (group.get("tasks") or []) if isinstance(group, dict) else []:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                titles[task["id"]] = task.get("title") if isinstance(task.get("title"), str) else task["id"]
    return titles


def _source(row: dict) -> str:
    return "linear" if row.get("by") == "linear" else "local"


def _counts(rows: list[dict], now: float, window: int) -> dict[str, int]:
    """Status moves in the window, by source."""
    counts = {"local": 0, "linear": 0}
    for row in rows:
        if now - window <= row["ts"] <= now:
            counts[_source(row)] += 1
    return counts


def _recent(rows: list[dict], titles: dict[str, str]) -> list[dict]:
    """The newest status moves, newest first, at most ``RECENT_LIMIT``."""
    out = []
    for row in reversed(rows[-RECENT_LIMIT:]):
        task = row.get("task") if isinstance(row.get("task"), str) else ""
        out.append({
            "ts": row["ts"],
            "task": task,
            "title": titles.get(task, task),
            "from": row.get("from") if isinstance(row.get("from"), str) else None,
            "to": row.get("to") if isinstance(row.get("to"), str) else None,
            "by": _source(row),
        })
    return out


def _pending(ws: str, link: dict, root: Path | None) -> list[dict]:
    """Pending approvals with their Linear identifier and https url, from the link's approval refs."""
    record = bundles.approvals_for(ws, root) or {}
    refs = link.get("approvals") if isinstance(link.get("approvals"), dict) else {}
    out = []
    for item in record.get("items") or []:
        if not isinstance(item, dict) or item.get("status") != "pending" or not isinstance(item.get("id"), str):
            continue
        ref = refs.get(item["id"]) if isinstance(refs.get(item["id"]), dict) else {}
        identifier = ref.get("identifier") if isinstance(ref.get("identifier"), str) else None
        out.append({
            "id": item["id"],
            "step": item.get("step") if isinstance(item.get("step"), str) else item["id"],
            "identifier": identifier,
            "url": linear._https_url(ref.get("url")),
        })
    return out


def _workstream(bundle: dict, root: Path | None, now: float) -> dict | None:
    ws = bundle["id"]
    link = linear.read_link(ws, root)
    if link is None:
        return None
    state = linear.sync_state(ws, root) or {}
    page = linear.page_link(ws, root) or {}
    directory = bundles.bundle_dir(ws, root)
    rows = _history_rows(directory) if directory is not None else []
    titles = _task_titles(_plan_body(directory))
    body = {
        "ws": ws,
        "title": bundle.get("title") or ws,
        "url": page.get("url"),
        "synced_at": state.get("synced_at"),
        "last_error": state.get("last_error"),
        "outbox": int(state.get("outbox") or 0),
        "last_update_at": link.get("last_update_at") if isinstance(link.get("last_update_at"), (int, float)) else None,
        "recent": _recent(rows, titles),
        "pending_approvals": _pending(ws, link, root),
    }
    for key, window in WINDOWS:
        body[key] = _counts(rows, now, window)
    return body


def build(now: float, root: Path | None = None) -> dict:
    """The ``/api/linear`` payload: one entry per linked workstream, in bundle order. Never a token."""
    method = linear_auth.status().get("method")
    workstreams = []
    for bundle in bundles.list_bundles(root):
        entry = _workstream(bundle, root, now)
        if entry is not None:
            workstreams.append(entry)
    return {"signed_in": method if method in ("oauth", "key") else "none", "workstreams": workstreams}


def _cache_key(root: Path | None) -> tuple:
    """Stamps of every linked bundle's files the payload reads, plus the sign-in method."""
    parts = [linear_auth.status().get("method")]
    for bundle in bundles.list_bundles(root):
        ws = bundle["id"]
        if not linear.is_linked(ws, root):
            continue
        directory = bundles.bundle_dir(ws, root)
        stamps = [bundles._file_stamp(directory / name) for name in ("history.jsonl", "approvals.json", "plan.json")] if directory else []
        parts.append((ws, linear.stamp(ws, root), tuple(stamps)))
    return tuple(parts)


def cached(now: float, root: Path | None = None) -> dict:
    """``build`` behind a cache keyed by the bundle stamps, kept for one refresh interval.

    Returns a copy, so callers never mutate the cached body.
    """
    key = _cache_key(root)
    with _CACHE_LOCK:
        if _CACHE["body"] is not None and _CACHE["key"] == key and now - _CACHE["at"] < REFRESH_S:
            return copy.deepcopy(_CACHE["body"])
    body = build(now, root)
    with _CACHE_LOCK:
        _CACHE.update({"key": key, "at": now, "body": body})
    return copy.deepcopy(body)
