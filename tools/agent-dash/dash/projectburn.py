"""Project burn-up and cost/token burn, built from recorded data only.

``GET /api/project/burnup`` answers what the burn-up card paints:

- tasks done, cumulative per workstream per UTC day, from each bundle's
  ``history.jsonl`` task moves to ``done`` (and the reverse moves back
  out of ``done``), plus the plan total per workstream (dashed gray on
  the card);
- cost and token burn per workstream per UTC day, from the store's
  hourly usage buckets grouped by workstream (the same attribution as
  ``/api/usage?group=workstream``).

Honesty rule: workstreams or task counts with no history are shown as a
labelled gap (``no history before <date>: N done tasks not dated``) and
are never interpolated. The project burn-up line is the sum of recorded
moves over time; the plan total line is the sum of plan totals.

Windows are calendar days in UTC; the payload says so (``"day": "UTC"``).
Only ids, counts, money, and dates cross this boundary — never a prompt,
a report body, or a path. Results are cached by history/plan file mtimes
plus the store sequence, so idle ticks cost nothing.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

DAY_S = 86400

#: Gap text for done tasks no history line can date. The date is the
#: first recorded history stamp (UTC day); without any recorded history
#: at all the dateless variant names that instead.
GAP_FMT = "no history before {date}: {n} done {tasks} not dated"
GAP_NONE_FMT = "no recorded history: {n} done {tasks} not dated"

_CACHE: dict = {}


def _utc_day(ts: float) -> int:
    """UTC-midnight epoch for an epoch stamp (calendar day, UTC)."""
    return int(ts // DAY_S) * DAY_S


def _day_label(day_ts: int) -> str:
    """UTC calendar day as ``YYYY-MM-DD`` from date parts (never a locale string)."""
    moment = datetime.datetime.fromtimestamp(day_ts, datetime.timezone.utc)
    return "%04d-%02d-%02d" % (moment.year, moment.month, moment.day)


def _ws_sort_key(ws: str):
    import re

    match = re.search(r"(\d+)", ws or "")
    return (int(match.group(1)) if match else 10**9, str(ws))


def _history_mtimes(root: Path, ids: list[str]) -> list[tuple]:
    """(history mtime_ns, plan mtime_ns) per bundle id; -1 when missing."""
    out = []
    for ws in ids:
        hist = root / ws / "history.jsonl"
        plan = root / ws / "plan.json"
        try:
            hmt = hist.stat().st_mtime_ns
        except OSError:
            hmt = -1
        try:
            pmt = plan.stat().st_mtime_ns
        except OSError:
            pmt = -1
        out.append((ws, hmt, pmt))
    return sorted(out)


def _cache_key(store, root: Path, ids: list[str]):
    seq = getattr(store, "seq", None)
    if seq is None:
        seq = getattr(store, "_seq", 0)
    data_seq = getattr(store, "data_seq", None)
    if data_seq is None:
        data_seq = getattr(store, "_data_seq", 0)
    return (str(root), tuple(_history_mtimes(root, ids)), seq, data_seq)


def _bundle_ids() -> tuple[Path, list[str]]:
    """(workstreams root, sorted bundle ids with a bundle.json)."""
    from dash.bundles import list_bundles, workstreams_root

    root = workstreams_root()
    ids = sorted(
        (item["id"] for item in list_bundles()),
        key=_ws_sort_key,
    )
    return root, ids


def _plan_totals(ids: list[str]) -> dict:
    """Plan ``{done, total}`` per id via the one plan resolver; None when no plan."""
    from dash.bundles import resolve_plan

    out = {}
    for ws in ids:
        try:
            _source, body = resolve_plan(ws)
        except Exception:
            body = None
        progress = body.get("progress") if isinstance(body, dict) else None
        if isinstance(progress, dict):
            out[ws] = {
                "done": int(progress.get("done") or 0),
                "total": int(progress.get("total") or 0),
            }
        else:
            out[ws] = None
    return out


def _recorded_moves(ids: list[str]) -> dict:
    """Net done-deltas per (ws, UTC day) plus per-task close, from history.

    A move to ``done`` from anything else (or from an unrecorded state)
    is +1; a move out of a recorded ``done`` is -1. Moves between two
    non-done states change nothing, and a reverse move for a task never
    recorded done is ignored (nothing recorded can be un-recorded).
    State lines (``{kind: state}``) carry no task and never reach here.
    """
    from dash.replay import read_history

    deltas: dict = {}
    first: dict = {}
    last_status: dict = {}
    for ws in ids:
        for row in read_history(ws):
            task = row.get("task")
            to = row.get("to")
            if not isinstance(task, str) or not isinstance(to, str):
                continue
            try:
                ts = float(row["ts"])
            except (TypeError, ValueError):
                continue
            prev = last_status.get((ws, task))
            if to == "done" and prev != "done":
                day = _utc_day(ts)
                deltas[(ws, day)] = deltas.get((ws, day), 0) + 1
            elif prev == "done" and to != "done":
                day = _utc_day(ts)
                deltas[(ws, day)] = deltas.get((ws, day), 0) - 1
            last_status[(ws, task)] = to
            if ws not in first or ts < first[ws]:
                first[ws] = ts
    recorded = {}
    for (ws, _task), status in last_status.items():
        if status == "done":
            recorded[ws] = recorded.get(ws, 0) + 1
    return deltas, first, recorded


def _usage_by_day(store) -> tuple[dict, dict, dict]:
    """(cost, tokens, unpriced) per (ws, UTC day) from day usage buckets.

    The same cells ``/api/usage?group=workstream&bucket=day`` serves, so
    the per-day sums equal the all-scope totals for the same days. Tokens
    are billed tokens (input + output + cache read + cache write); a cell
    with no priced cost contributes its tokens to ``unpriced`` instead.
    """
    cells = store.query_usage("workstream", "day", None, None, None)
    cost: dict = {}
    tokens: dict = {}
    unpriced: dict = {}
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        ws = cell.get("key") or "unassigned"
        try:
            day = int(cell.get("t") or 0)
        except (TypeError, ValueError):
            continue
        day = _utc_day(day)
        billed = sum(int(cell.get(part) or 0) for part in ("input", "output", "cache_read", "cache_write"))
        tokens[(ws, day)] = tokens.get((ws, day), 0) + billed
        price = cell.get("cost_usd")
        if price is None:
            unpriced[(ws, day)] = unpriced.get((ws, day), 0) + (int(cell.get("unpriced_tokens") or 0) or billed)
        else:
            try:
                cost[(ws, day)] = cost.get((ws, day), 0.0) + float(price)
            except (TypeError, ValueError):
                continue
    return cost, tokens, unpriced


def _gap_label(n: int, first_ts) -> str:
    tasks = "task" if n == 1 else "tasks"
    if first_ts is None:
        return GAP_NONE_FMT.format(n=n, tasks=tasks)
    return GAP_FMT.format(date=_day_label(_utc_day(first_ts)), n=n, tasks=tasks)


def build_burnup(store, now: float | None = None) -> dict:
    """Project burn-up payload for ``GET /api/project/burnup``.

    Pure read path: history files plus plan files plus one store usage
    pass. Cached by history/plan mtimes plus the store sequence.
    """
    import time as _time

    moment = float(now if now is not None else _time.time())
    root, ids = _bundle_ids()
    key = _cache_key(store, root, ids)
    hit = _CACHE.get("key")
    if hit is not None and hit == key and "payload" in _CACHE:
        return _CACHE["payload"]

    plans = _plan_totals(ids)
    deltas, first, recorded = _recorded_moves(ids)
    cost_cells, token_cells, unpriced_cells = _usage_by_day(store)

    usage_ws = {ws for (ws, _day) in list(cost_cells) + list(token_cells)}
    order = sorted(set(ids) | set(recorded) | set(usage_ws), key=_ws_sort_key)

    first_recorded = min(first.values()) if first else None
    today = _utc_day(moment)
    starts = [today]
    if deltas:
        starts.append(min(day for (_ws, day) in deltas))
    if cost_cells or token_cells:
        starts.append(min(day for (_ws, day) in list(cost_cells) + list(token_cells)))
    start = min(starts)
    days = []
    day = start
    while day <= today:
        days.append(day)
        day += DAY_S

    by_ws: dict = {}
    project: list[int] = [0] * len(days)
    plan_total = 0
    for ws in order:
        cumulative = []
        running = 0
        for pos, day in enumerate(days):
            running += deltas.get((ws, day), 0)
            cumulative.append(running)
            project[pos] += running
        plan = plans.get(ws)
        plan_done = plan["done"] if plan else 0
        total = plan["total"] if plan else None
        if total is not None:
            plan_total += total
        now_recorded = recorded.get(ws, 0)
        undated = max(0, plan_done - now_recorded) if plan else 0
        ws_first = first.get(ws)
        gap = _gap_label(undated, ws_first if ws_first is not None else first_recorded) if undated else None
        by_ws[ws] = {
            "cumulative": cumulative,
            "plan_total": total,
            "recorded": now_recorded,
            "undated": undated,
            "gap": gap,
            "first_recorded": ws_first,
        }

    cost_ws: dict = {}
    token_ws: dict = {}
    cost_project = [0.0] * len(days)
    token_project = [0] * len(days)
    unpriced_project = [0] * len(days)
    for ws in order:
        costs = [round(cost_cells.get((ws, day), 0.0), 4) for day in days]
        toks = [token_cells.get((ws, day), 0) for day in days]
        cost_ws[ws] = costs
        token_ws[ws] = toks
        for pos in range(len(days)):
            cost_project[pos] += costs[pos]
            token_project[pos] += toks[pos]
    for pos, day in enumerate(days):
        unpriced_project[pos] = sum(count for (ws, when), count in unpriced_cells.items() if when == day)
    cost_project = [round(value, 4) for value in cost_project]

    payload = {
        "days": days,
        "day": "UTC",
        "first_recorded": first_recorded,
        "workstreams": order,
        "tasks": {
            "by_workstream": by_ws,
            "project": project,
            "plan_total": plan_total,
        },
        "cost": {
            "by_workstream": cost_ws,
            "project": cost_project,
            "unpriced_tokens": unpriced_project,
        },
        "tokens": {
            "by_workstream": token_ws,
            "project": token_project,
        },
    }
    _CACHE.clear()
    _CACHE["key"] = key
    _CACHE["payload"] = payload
    return payload


def cache_key(store) -> tuple:
    """Current cache key (history/plan mtimes plus the store sequence)."""
    root, ids = _bundle_ids()
    return _cache_key(store, root, ids)


def clear_cache() -> None:
    """Drop the cached payload (tests only; production invalidates by key)."""
    _CACHE.clear()
