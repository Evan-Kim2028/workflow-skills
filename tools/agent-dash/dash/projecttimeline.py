"""Project roadmap timeline: one lane per workstream over time.

Pure builder behind ``GET /api/project/timeline``. Inputs are plain data
(bundles, history rows, plan progress, store summaries, the optional
project file), so tests feed synthetics and the server feeds disk plus
the store. No I/O here except the small ``read_*`` helpers the server
uses; ``build_timeline`` itself never touches disk, the network, or the
store.

Honesty rules (owner decisions, never relaxed here):

- Dates come only from recorded evidence: the first mapped run, the
  first history move, state moves, task moves to done. Nothing is
  interpolated and nothing is invented.
- A lane with no date evidence at all (no runs, no history) is flagged
  ``before_history`` — except planned lanes, which are future estimates:
  the card draws band lanes in a labelled "before history" band at the
  left, in plan order, without pretending dates.
- A ``done_at`` taken from the last task move (plan at 100%) instead of
  a recorded state move is marked ``done_inferred``.
- Planned lanes are estimates, never history: dashed future bars from
  today with a width from the plan size, labelled 'estimate: plan size'.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Lane groups, top to bottom. Done and archived celebrate together;
#: paused keeps working beside active; dropped waits with planned.
GROUP_OF = {
    "done": "done",
    "archived": "done",
    "active": "active",
    "paused": "active",
    "planned": "planned",
    "dropped": "planned",
}

GROUP_ORDER = ("done", "active", "planned")

#: States that count as finished when deciding whether a dependency is met.
FINISHED = ("done", "archived")

#: Estimate rule for planned lanes: two days per open task, at least a week.
ESTIMATE_DAYS_PER_TASK = 2
ESTIMATE_DAYS_MIN = 7


def _num(value):
    """Epoch seconds as a float, or None for anything not a real number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        return value if value == value else None
    return None


def _progress_of(raw) -> dict:
    """``{done, total, pct}`` with the server shape, zeros when unknown."""
    try:
        done = int((raw or {}).get("done") or 0)
    except (TypeError, ValueError):
        done = 0
    try:
        total = int((raw or {}).get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        pct = int((raw or {}).get("pct") or 0)
    except (TypeError, ValueError):
        pct = 0
    return {"done": max(0, done), "total": max(0, total), "pct": max(0, min(100, pct))}


def lane_dates(history_rows, first_run_at=None, last_run_at=None, progress=None) -> dict:
    """Date evidence for one lane from recorded facts only.

    ``history_rows`` holds raw ``history.jsonl`` rows: task moves
    ``{ts, task, from, to}`` and state lines ``{kind: state, ts, ws,
    from, to}``. Rows without a numeric ``ts`` are ignored.
    """
    task_stamps: list[float] = []
    done_task_stamps: list[float] = []
    state_done_stamps: list[float] = []
    any_stamps: list[float] = []
    for row in history_rows or []:
        if not isinstance(row, dict):
            continue
        ts = _num(row.get("ts"))
        if ts is None:
            continue
        any_stamps.append(ts)
        if row.get("kind") == "state":
            if row.get("to") == "done":
                state_done_stamps.append(ts)
        elif isinstance(row.get("task"), str) and isinstance(row.get("to"), str):
            task_stamps.append(ts)
            if row["to"] == "done":
                done_task_stamps.append(ts)
    first_history = min(any_stamps) if any_stamps else None
    last_history = max(any_stamps) if any_stamps else None
    first_run = _num(first_run_at)
    last_run = _num(last_run_at)
    candidates = [value for value in (first_run, first_history) if value is not None]
    started_at = min(candidates) if candidates else None
    tail = [value for value in (last_run, last_history) if value is not None]
    last_active_at = max(tail) if tail else None
    state_done = min(state_done_stamps) if state_done_stamps else None
    done_at = state_done
    done_inferred = False
    if done_at is None and done_task_stamps and (progress or {}).get("pct") == 100:
        done_at = max(done_task_stamps)
        done_inferred = True
    return {
        "started_at": started_at,
        "last_active_at": last_active_at,
        "done_at": done_at,
        "done_inferred": done_inferred,
        "has_history": bool(any_stamps),
    }


def lane_for(bundle, history_rows=None, progress=None, usage=None, states=None, now=None) -> dict:
    """One timeline lane from one bundle's metadata plus its evidence maps."""
    bundle = bundle if isinstance(bundle, dict) else {}
    ws = str(bundle.get("id") or "")
    state = bundle.get("state")
    if state not in GROUP_OF:
        state = "active"
    depends = [item for item in (bundle.get("depends_on") or []) if isinstance(item, str)]
    milestone = bundle.get("milestone")
    if not isinstance(milestone, str):
        milestone = None
    try:
        order = int(bundle.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    usage = usage if isinstance(usage, dict) else {}
    dates = lane_dates(history_rows, (usage or {}).get("first_run_at"), (usage or {}).get("last_run_at"), _progress_of(progress))
    prog = _progress_of(progress)
    open_tasks = max(0, prog["total"] - prog["done"])
    group = GROUP_OF[state]
    blocked_by = []
    table = states if isinstance(states, dict) else {}
    for dep in depends:
        dep_state = table.get(dep)
        if dep_state is None:
            blocked_by.append(dep)
        elif dep_state not in FINISHED:
            blocked_by.append(dep)
    try:
        runs = int(usage.get("runs") or 0)
    except (TypeError, ValueError):
        runs = 0
    tokens = _num(usage.get("tokens")) or 0
    cost = _num(usage.get("cost")) or 0.0
    estimate_days = None
    if group == "planned":
        estimate_days = max(ESTIMATE_DAYS_MIN, ESTIMATE_DAYS_PER_TASK * max(1, open_tasks))
    return {
        "id": ws,
        "title": bundle.get("title") if isinstance(bundle.get("title"), str) else "",
        "state": state,
        "group": group,
        "order": order,
        "started_at": dates["started_at"],
        "last_active_at": dates["last_active_at"],
        "done_at": dates["done_at"],
        "done_inferred": dates["done_inferred"],
        "archived_at": _num(bundle.get("archived_at")),
        "has_history": dates["has_history"],
        "before_history": dates["started_at"] is None and state != "planned",
        "progress": prog,
        "milestone": milestone,
        "depends_on": depends,
        "blocked_by": blocked_by,
        "runs": max(0, runs),
        "tokens": int(tokens),
        "cost_usd": cost,
        "estimate_days": estimate_days,
    }


def order_lanes(lanes) -> list[str]:
    """Lane ids top to bottom: groups Done / Active / Planned; inside a
    group dependencies first (topological), then earliest start, then plan
    order, then id. Cycles fall back to plan order so every lane keeps a
    row."""
    by_id = {lane["id"]: lane for lane in lanes if lane.get("id")}
    grouped: dict[str, list[dict]] = {group: [] for group in GROUP_ORDER}
    for lane in lanes:
        grouped.get(lane.get("group"), grouped["planned"]).append(lane)
    out: list[str] = []
    for group in GROUP_ORDER:
        members = grouped[group]
        ids = {lane["id"] for lane in members}
        edge_count = {lane["id"]: 0 for lane in members}
        downstream: dict[str, list[str]] = {lane["id"]: [] for lane in members}
        for lane in members:
            for dep in lane.get("depends_on") or []:
                if dep in ids and dep != lane["id"]:
                    edge_count[lane["id"]] += 1
                    downstream[dep].append(lane["id"])

        def sort_key(lane):
            started = lane.get("started_at")
            return (
                0 if started is not None else 1,
                started if started is not None else 0,
                lane.get("order") or 0,
                lane.get("id") or "",
            )

        ready = sorted([lane for lane in members if not edge_count[lane["id"]]], key=sort_key)
        placed: list[str] = []
        while ready:
            lane = ready.pop(0)
            placed.append(lane["id"])
            for child in sorted(downstream[lane["id"]]):
                edge_count[child] -= 1
                if edge_count[child] <= 0:
                    ready.append(by_id[child])
            ready.sort(key=sort_key)
        missing = [lane["id"] for lane in sorted(members, key=sort_key) if lane["id"] not in placed]
        out.extend(placed + missing)
    return [lane_id for lane_id in out if lane_id in by_id]


def build_timeline(bundles, histories=None, progresses=None, usages=None, project=None, now=None) -> dict:
    """Assemble the ``GET /api/project/timeline`` payload from plain data.

    ``bundles`` is the workstream bundle list (``id``, ``title``,
    ``order``, ``kind``, ``state``, ``archived_at``, ``depends_on``,
    ``milestone``); program bundles are skipped. ``histories``,
    ``progresses`` and ``usages`` are keyed by workstream id. ``project``
    is the ``{started, target, phases}`` block (or None when no
    project file exists). ``now`` is epoch seconds.
    """
    import time as _time

    moment = float(now) if _num(now) is not None else _time.time()
    rows = [bundle for bundle in (bundles or []) if isinstance(bundle, dict) and bundle.get("kind", "workstream") == "workstream" and bundle.get("id")]
    states = {}
    for bundle in rows:
        state = bundle.get("state")
        states[str(bundle.get("id"))] = state if state in GROUP_OF else "active"
    lanes = []
    for bundle in rows:
        ws = str(bundle.get("id"))
        lanes.append(
            lane_for(
                bundle,
                (histories or {}).get(ws),
                (progresses or {}).get(ws),
                (usages or {}).get(ws),
                states,
                moment,
            )
        )
    order = order_lanes(lanes)
    floor = None
    stamps: list[float] = []
    for ws_rows in (histories or {}).values():
        for row in ws_rows or []:
            ts = _num(row.get("ts")) if isinstance(row, dict) else None
            if ts is not None:
                stamps.append(ts)
    if stamps:
        floor = min(stamps)
    proj = project if isinstance(project, dict) else {}
    phases = [phase for phase in (proj.get("phases") or []) if isinstance(phase, dict)]
    return {
        "now": moment,
        "project": {
            "started": proj.get("started") if isinstance(proj.get("started"), str) else None,
            "target": proj.get("target") if isinstance(proj.get("target"), str) else None,
            "phases": phases,
        },
        "history_floor": floor,
        "lanes": lanes,
        "order": order,
    }


def workstreams_root(root=None) -> Path:
    """Bundle directory, honoring the same override as the bundle reader."""
    if root is not None:
        return Path(root)
    raw = os.environ.get("AGENT_DASH_WORKSTREAMS")
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return Path.home() / ".local" / "share" / "agent-dash" / "workstreams"


def read_history_raw(ws: str, root=None) -> list[dict]:
    """Every ``history.jsonl`` row with a numeric ``ts``, oldest first.

    Unlike the replay reader (task moves only), this keeps the
    ``{kind: state}`` lifecycle lines too, so state moves date the lanes.
    Never raises: a missing or unreadable file reads as no history.
    """
    base = workstreams_root(root)
    try:
        text = (base / ws / "history.jsonl").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if _num(row.get("ts")) is None:
            continue
        out.append(row)
    out.sort(key=lambda row: float(row["ts"]))
    return out


def read_project(root=None) -> dict | None:
    """``{started, target, phases}`` from ``project.json`` beside the bundles.

    Returns None when the file is missing or unreadable, so the endpoint
    falls back gracefully on servers whose project file has not landed.
    Only identity and phase fields cross this boundary: names and
    milestones, never paths or bodies.
    """
    base = workstreams_root(root)
    try:
        raw = (base / "project.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    phases = []
    for phase in data.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        entry: dict = {}
        for key in ("id", "title", "name", "summary", "goal", "gate", "state", "status"):
            value = phase.get(key)
            if isinstance(value, str):
                entry[key] = value
        if entry:
            phases.append(entry)
    project: dict = {"phases": phases}
    for key in ("started", "target", "name", "short"):
        value = data.get(key)
        project[key] = value if isinstance(value, str) else None
    return project


def collect(store, now=None, root=None) -> dict:
    """Server entry: bundles plus histories plus plans plus store summaries."""
    import time as _time

    from dash.bundles import list_bundles, resolve_plan
    from dash.workstates import STATES, all_bundle_meta

    moment = float(now) if _num(now) is not None else _time.time()
    bundles = [bundle for bundle in list_bundles(root) if bundle.get("kind", "workstream") == "workstream"]
    meta = all_bundle_meta(root)
    histories = {bundle["id"]: read_history_raw(bundle["id"], root) for bundle in bundles}
    progresses = {}
    for bundle in bundles:
        ws = bundle["id"]
        try:
            _source, body = resolve_plan(ws)
        except Exception:
            body = None
        progress = body.get("progress") if isinstance(body, dict) else None
        progresses[ws] = dict(progress) if isinstance(progress, dict) else {"done": 0, "total": 0, "pct": 0}
    try:
        from dash.bundles import workstream_plan_ids

        plan_ids = workstream_plan_ids()
    except Exception:
        plan_ids = {bundle["id"] for bundle in bundles}
    try:
        rows = store.query_workstreams(plan_ids, None, None, now=moment, bundle_meta=meta, allowed_states=set(STATES))
    except Exception:
        rows = []
    by_ws = {row.get("ws"): row for row in rows if isinstance(row, dict)}
    run_first: dict[str, float] = {}
    run_last: dict[str, float] = {}
    run_count: dict[str, int] = {}
    try:
        runs = store.query_runs(moment, None, None, None, 5000)
    except Exception:
        runs = []
    for run in runs or []:
        if not isinstance(run, dict) or run.get("source") == "dagster":
            continue
        ws = run.get("workstream")
        if not ws or ws == "unassigned":
            continue
        start = _num(run.get("started_at"))
        end = _num(run.get("ended_at"))
        stamp = end if end is not None else start
        if start is not None:
            if ws not in run_first or start < run_first[ws]:
                run_first[ws] = start
        if stamp is not None:
            if ws not in run_last or stamp > run_last[ws]:
                run_last[ws] = stamp
        run_count[ws] = run_count.get(ws, 0) + 1
    usages = {}
    for bundle in bundles:
        ws = bundle["id"]
        row = by_ws.get(ws) or {}
        last_activity = _num(row.get("last_activity"))
        last = run_last.get(ws, last_activity)
        usages[ws] = {
            "runs": run_count.get(ws, 0),
            "tokens": row.get("tokens_all_time") or 0,
            "cost": row.get("cost_usd_all_time") or 0.0,
            "first_run_at": run_first.get(ws),
            "last_run_at": last,
        }
    return build_timeline(bundles, histories, progresses, usages, read_project(root), moment)
