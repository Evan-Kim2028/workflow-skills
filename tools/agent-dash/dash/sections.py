"""Plan section for a run: workstream -> section -> label.

A run that maps to a plan task (the ``dash.taskmatch`` rule) belongs to
the plan section holding that task: the ``groups`` entry (in plan order)
whose ``tasks`` contain the task id. ``section`` is the group title (the
group id when it has no title) and ``section_index`` its position in the
plan, so the page orders sections as in the plan without refetching plan
bodies. Runs that map to no task read ``section: null`` and the page
shows them under "Unsectioned".

Machine keys stay as they are (``groups``, ``narrative.phases``,
``phases_line``): only the user-visible name is "section".
"""

from __future__ import annotations

import threading
from pathlib import Path

#: Per-workstream section maps: ``ws -> (stamp, payload)``. The stamp is
#: the bundle ``plan.json`` mtime when the plan is bundle-backed (else the
#: plans-map stamp, else None, which recomputes every request like the
#: other plan caches). A plan edit retags precisely and untouched
#: workstreams never re-read.
_SECTION_CACHE: dict = {}
_SECTION_LOCK = threading.Lock()

#: The resolved run-label to task-id map: ``(path, mtime) -> rules``.
_TASKMAP_CACHE: dict = {}
_TASKMAP_LOCK = threading.Lock()


def section_map_for(ws: str) -> dict:
    """``{"sections", "by_task"}`` for one workstream, cached by plan stamp.

    ``sections`` is the section names in plan order; ``by_task`` maps a
    task id to ``{"section", "section_index"}``. Unknown workstreams (and
    plans with no groups) read ``{"sections": [], "by_task": {}}``.
    Never raises: an unreadable plan is an empty map.
    """
    stamp = _plan_stamp_for(ws)
    key = str(ws)
    if stamp is not None:
        with _SECTION_LOCK:
            hit = _SECTION_CACHE.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
    payload = _build_section_map(ws)
    if stamp is not None:
        with _SECTION_LOCK:
            _SECTION_CACHE[key] = (stamp, payload)
    return payload


def annotate_runs(rows) -> list:
    """Copies of ``rows`` with additive ``section``/``section_index``.

    Never mutates its input: snapshot payloads are cache-owned. Runs that
    map to no plan task keep ``section: None`` and ``section_index:
    None`` ("Unsectioned" on the page). Unknown shapes pass through with
    null sections.
    """
    taskmap = _taskmap_cached()
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        entry = dict(row)
        ws = row.get("workstream")
        label = row.get("label")
        section, index = None, None
        if isinstance(ws, str) and ws:
            try:
                resolved = section_map_for(ws)
            except Exception:
                resolved = None
            if resolved:
                plan_ids = list(resolved.get("by_task") or {})
                task = None
                try:
                    from dash.taskmatch import task_of_verified

                    task = task_of_verified(label, ws, taskmap, plan_ids)
                except Exception:
                    task = None
                if task is not None:
                    hit = (resolved.get("by_task") or {}).get(task)
                    if isinstance(hit, dict):
                        section, index = hit.get("section"), hit.get("section_index")
        entry["section"] = section
        entry["section_index"] = index
        out.append(entry)
    return out


def _build_section_map(ws: str) -> dict:
    try:
        from dash.bundles import resolve_plan
    except Exception:
        return {"sections": [], "by_task": {}}
    try:
        _source, body = resolve_plan(ws)
    except Exception:
        return {"sections": [], "by_task": {}}
    if not isinstance(body, dict):
        return {"sections": [], "by_task": {}}
    groups = body.get("groups")
    if not isinstance(groups, list):
        return {"sections": [], "by_task": {}}
    sections: list = []
    by_task: dict = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        title = group.get("title")
        name = title if isinstance(title, str) and title else group.get("id")
        if not isinstance(name, str) or not name:
            continue
        index = len(sections)
        sections.append(name)
        tasks = group.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = task.get("id")
            if tid is None or tid == "":
                continue
            key = str(tid)
            if key not in by_task:
                by_task[key] = {"section": name, "section_index": index}
    return {"sections": sections, "by_task": by_task}


def _plan_stamp_for(ws: str):
    """Cache stamp for one workstream's plan, or None when unknowable."""
    try:
        from dash.bundles import bundle_dir
    except Exception:
        bundle_dir = None
    if bundle_dir is not None:
        try:
            path = bundle_dir(ws)
            if path is not None:
                target = path / "plan.json"
                stat = target.stat()
                return ("bundle", str(target), stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError):
            pass
    try:
        from dash.bundles import plans_map_stamps

        return ("map", plans_map_stamps())
    except Exception:
        return None


def _taskmap_cached() -> dict:
    """The resolved taskmap, cached by file mtime like the page's fetch."""
    try:
        from dash.locate import layered_path

        base = Path(__file__).resolve().parents[1] / "static"
    except Exception:
        return {}
    try:
        found = layered_path("taskmap", "AGENT_DASH_TASKMAP", base)
    except Exception:
        return {}
    if found is None:
        return {}
    path = Path(found)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    key = str(path)
    if mtime is not None:
        with _TASKMAP_LOCK:
            hit = _TASKMAP_CACHE.get(key)
            if hit is not None and hit[0] == mtime:
                return hit[1]
    try:
        from dash.taskmatch import load_taskmap

        rules = load_taskmap()
    except Exception:
        rules = {}
    if not isinstance(rules, dict):
        rules = {}
    if mtime is not None:
        with _TASKMAP_LOCK:
            _TASKMAP_CACHE[key] = (mtime, rules)
    return rules
