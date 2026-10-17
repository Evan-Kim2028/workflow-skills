"""Workstream history replay: one timeline from runs, plan, and recorded history.

``GET /api/replay?ws=<ID>`` answers what a sped-up playback needs: per
task the first observed activity and the done moment, the mapped runs per
task (labels only, never prompts), and one time-sorted event list. Times
come from three sources, best first:

1. recorded history (``history.jsonl`` in the bundle, written by
   ``ws task`` from now on),
2. observed runs mapped by the shared rule in ``dash.taskmatch``,
3. inference for done tasks with no mapped run: the latest done moment
   of their dependencies, else the plan's earliest observed moment.

A task whose done moment was not observed carries ``"inferred": true``.
"""

from __future__ import annotations

import json
import time

from dash import taskmatch
from dash.bundles import bundle_dir, resolve_plan

REPLAY_LIMIT = 5000


class ReplayNotFound(Exception):
    def __init__(self, ws: str):
        self.ws = ws
        super().__init__(ws)


def read_history(ws: str, root=None) -> list[dict]:
    """Recorded ``{ts, task, from, to}`` transitions, oldest first."""
    path = bundle_dir(ws, root)
    if path is None:
        return []
    try:
        text = (path / "history.jsonl").read_text(encoding="utf-8")
    except OSError:
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
        try:
            ts = float(row["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        task = row.get("task")
        to = row.get("to")
        if not isinstance(task, str) or not task or not isinstance(to, str) or not to:
            continue
        item = {"ts": ts, "task": task, "to": to}
        if isinstance(row.get("from"), str):
            item["from"] = row["from"]
        out.append(item)
    out.sort(key=lambda row: row["ts"])
    return out


def _plan_tasks(body: dict) -> tuple[list[str], dict, dict]:
    """(column titles, task id -> task, task id -> dep list) for a plan body."""
    groups = body.get("groups") if isinstance(body, dict) else None
    by_id: dict[str, dict] = {}
    deps: dict[str, list[str]] = {}
    if isinstance(groups, list):
        for group in groups:
            tasks = group.get("tasks") if isinstance(group, dict) else None
            for task in tasks or []:
                if not isinstance(task, dict) or task.get("id") in (None, ""):
                    continue
                tid = str(task["id"])
                by_id[tid] = task
                raw = task.get("deps")
                deps[tid] = [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
    return by_id, deps


def _final_done(
    tid: str,
    by_id: dict,
    deps: dict,
    done_at: dict,
    earliest: float | None,
    visiting: set,
) -> float | None:
    """Observed done moment, else the latest done moment of the deps (which
    are themselves resolved first), else the plan's earliest moment."""
    if done_at.get(tid) is not None:
        return done_at[tid]
    if by_id[tid].get("status") != "done" or tid in visiting:
        return earliest if tid in visiting else None
    visiting.add(tid)
    try:
        child = [_final_done(dep, by_id, deps, done_at, earliest, visiting) for dep in deps.get(tid, []) if dep in by_id]
        child = [moment for moment in child if moment is not None]
        value = max(child) if child else earliest
    finally:
        visiting.discard(tid)
    done_at[tid] = value
    return value


def build_replay(store, ws: str, now: float | None = None) -> dict:
    """One timeline for a workstream from a single store pass plus its plan."""
    moment = time.time() if now is None else now
    _source, body = resolve_plan(ws)
    if not isinstance(body, dict):
        raise ReplayNotFound(ws)
    by_id, deps = _plan_tasks(body)
    if not by_id:
        raise ReplayNotFound(ws)
    ids = taskmatch.plan_task_ids(body.get("groups"))
    taskmap = taskmatch.load_taskmap()

    runs = store.query_runs(moment, None, None, None, REPLAY_LIMIT)
    per_task: dict[str, list] = {tid: [] for tid in by_id}
    for run in runs:
        if not isinstance(run, dict) or run.get("source") == "dagster":
            continue
        owner = run.get("workstream")
        if owner and owner != ws:
            continue
        tid = taskmatch.task_of(run.get("label"), ws, taskmap, ids)
        if tid is None or tid not in per_task:
            continue
        start = run.get("started_at")
        try:
            start = None if start is None else float(start)
        except (TypeError, ValueError):
            start = None
        end = run.get("ended_at")
        try:
            end = None if end is None else float(end)
        except (TypeError, ValueError):
            end = None
        per_task[tid].append(
            {
                "start": start,
                "end": end,
                "family": taskmatch.run_family(run),
                "label": run.get("label") or run.get("run_id"),
            }
        )

    first_active: dict[str, float | None] = {}
    done_at: dict[str, float | None] = {}
    observed_done: set[str] = set()
    for tid, items in per_task.items():
        starts = [item["start"] for item in items if item["start"] is not None]
        first_active[tid] = min(starts) if starts else None
        done_at[tid] = None
        if by_id[tid].get("status") == "done":
            ends = [item["end"] for item in items if item["end"] is not None]
            if ends:
                done_at[tid] = max(ends)
                observed_done.add(tid)

    history = read_history(ws)
    hist_first: dict[str, float] = {}
    hist_done: dict[str, float] = {}
    for row in history:
        task = row["task"]
        if task not in by_id:
            continue
        hist_first[task] = min(hist_first.get(task, row["ts"]), row["ts"])
        if row["to"] == "done":
            hist_done[task] = max(hist_done.get(task, row["ts"]), row["ts"])
    for tid, ts in hist_first.items():
        current = first_active[tid]
        first_active[tid] = ts if current is None else min(current, ts)
    for tid, ts in hist_done.items():
        done_at[tid] = ts
        observed_done.add(tid)

    earliest: float | None = None
    seen: list[float] = []
    for tid in by_id:
        if first_active[tid] is not None:
            seen.append(first_active[tid])
        if tid in observed_done and done_at[tid] is not None:
            seen.append(done_at[tid])
    if seen:
        earliest = min(seen)

    inferred: set[str] = set()
    for tid in by_id:
        if by_id[tid].get("status") == "done" and tid not in observed_done:
            _final_done(tid, by_id, deps, done_at, earliest, set())
            inferred.add(tid)

    events: list[dict] = []
    for tid, items in per_task.items():
        for item in items:
            if item["start"] is None:
                continue
            events.append(
                {
                    "t": item["start"],
                    "kind": "active",
                    "task": tid,
                    "label": item["label"],
                    "family": item["family"],
                    "end": item["end"],
                }
            )
    for tid in by_id:
        if done_at[tid] is not None:
            events.append({"t": done_at[tid], "kind": "done", "task": tid, "inferred": tid in inferred})
    events.sort(key=lambda row: (row["t"], 0 if row["kind"] == "active" else 1))

    tasks: dict[str, dict] = {}
    for tid in by_id:
        items = sorted(per_task[tid], key=lambda item: (item["start"] is None, item["start"]))
        tasks[tid] = {
            "status": by_id[tid].get("status"),
            "first_active": first_active[tid],
            "done_at": done_at[tid],
            "inferred": tid in inferred,
            "runs": items,
        }
    observed = sum(1 for tid in by_id if done_at[tid] is not None and tid not in inferred)
    missed = sum(1 for tid in by_id if tid in inferred)
    span = {"start": events[0]["t"], "end": events[-1]["t"]} if events else None
    return {
        "ws": ws,
        "span": span,
        "tasks": tasks,
        "events": events,
        "observed": observed,
        "inferred": missed,
    }
