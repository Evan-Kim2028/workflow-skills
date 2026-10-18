"""Per-workstream progress over time: completion plus runs per hour.

``GET /api/ws/progress?ws=<ID>`` answers what the workstream Execution
tab's "Progress over time" strip paints; ``?ws=all`` answers the
project total for the overall Execution view (all non-archived
workstreams weighted by task count, one series pair):

- Chart A: % of non-deferred tasks done over time (a step line from each
  task's resolved done moment, plus the recorded ``prog``
  = merged-waiting-on-prod count as a lighter band on the same % axis),
  with the first-cross times of 20/40/60/80/100%;
- Chart B: agent runs started per hour on the same time axis (a separate
  chart: runs are a different unit, one y axis per chart).

Done moments come from the one shared rule (``dash.replay``: recorded
``history.jsonl`` transitions win, else the mapped run end, else
inference), so this card and the replay agree task by task. The curve
is non-decreasing: a recorded done transition fixes the moment even
when the task is later reopened. The ``prog``
band is recorded history only: tasks with no history row never read as
``prog``. Undated work stays a labelled gap, never interpolated (see
``docs/ws9-progress-backfill.md`` for today's one-row backfill).

Only ids, counts, money-free percents, and stamps cross this boundary —
never a prompt, a report body, or a path. Results are cached by
history/plan file mtimes plus the store sequence, so idle ticks cost
nothing.
"""

from __future__ import annotations

import json
import math
import time

HOUR_S = 3600

#: Milestone percents labelled on Chart A.
MILESTONES = (20, 40, 60, 80, 100)

#: Run rows read per workstream for the per-hour bars (above every live count).
RUNS_LIMIT = 20000

_CACHE: dict = {}


def _bundle_mtimes(ws: str) -> tuple:
    """(history mtime_ns, plan mtime_ns); -1 when the file is missing."""
    from dash.bundles import bundle_dir

    path = bundle_dir(ws)
    if path is None:
        return (-1, -1)
    try:
        history_mtime = (path / "history.jsonl").stat().st_mtime_ns
    except OSError:
        history_mtime = -1
    try:
        plan_mtime = (path / "plan.json").stat().st_mtime_ns
    except OSError:
        plan_mtime = -1
    return (history_mtime, plan_mtime)


def cache_key(store, ws: str) -> tuple:
    """Current cache key (history/plan mtimes plus the store sequence)."""
    seq = getattr(store, "seq", None)
    if seq is None:
        seq = getattr(store, "_seq", 0)
    data_seq = getattr(store, "data_seq", None)
    if data_seq is None:
        data_seq = getattr(store, "_data_seq", 0)
    return (ws, _bundle_mtimes(ws), seq, data_seq)


def clear_cache() -> None:
    """Drop the cached payload (tests only; production invalidates by key)."""
    _CACHE.clear()


def project_ids() -> list[str]:
    """Bundle ids with a plan, minus archived/dropped ones.

    Never raises: an unreadable bundle root reads as no workstreams.
    """
    try:
        from dash.bundles import list_bundles
    except Exception:
        return []
    try:
        items = list_bundles()
    except Exception:
        return []
    try:
        from dash.workstates import hidden_ids

        hidden = hidden_ids()
    except Exception:
        hidden = set()
    return sorted(
        [item["id"] for item in items if isinstance(item, dict) and item.get("id") not in hidden]
    )


def project_cache_key(store) -> tuple:
    """Cache key for the project total: one entry per non-hidden bundle id."""
    return ("all", tuple((ws, cache_key(store, ws)) for ws in project_ids()))


def _step_at(points: list, moment: float) -> tuple:
    """(done, prog) at one moment from sorted step points; (0, 0) before the first."""
    done, prog = 0, 0
    for pt in points or []:
        if pt["t"] <= moment:
            done, prog = pt["done"], pt["prog"]
        else:
            break
    return done, prog


def build_project_progress(store, now: float | None = None) -> dict:
    """Progress-over-time payload for the project total (``ws=all``).

    One series pair for all non-archived workstreams weighted by task
    count: at every per-workstream event moment the merged done/prog
    counts are the sums of the per-workstream step values there, over
    the summed plan total. Milestones are the first-cross times of
    20/40/60/80/100% on that merged series; runs started per hour are
    summed per hour. Workstreams with no plan read as no data and leave
    the total (never interpolated). Raises ``ReplayNotFound`` when no
    non-archived workstream has plan data, so the strip hides instead
    of drawing an empty frame.
    """
    from dash.replay import ReplayNotFound

    moment = float(now if now is not None else time.time())

    key = project_cache_key(store)
    if _CACHE.get("project-key") == key and "project-payload" in _CACHE:
        return _CACHE["project-payload"]

    parts = []
    for ws in project_ids():
        try:
            part = build_wsprogress(store, ws, moment)
        except ReplayNotFound:
            continue
        if (part.get("total") or 0) > 0:
            parts.append(part)
    if not parts:
        raise ReplayNotFound("all")

    total = sum(int(part["total"]) for part in parts)
    done_now = sum(int(part["done"]) for part in parts)
    counts: dict = {}
    for part in parts:
        for name, value in (part.get("counts") or {}).items():
            try:
                counts[name] = counts.get(name, 0) + int(value)
            except (TypeError, ValueError):
                continue

    ticks = sorted({pt["t"] for part in parts for pt in (part.get("points") or [])})
    points = []
    for tick in ticks:
        done = prog = 0
        for part in parts:
            part_done, part_prog = _step_at(part.get("points"), tick)
            done += part_done
            prog += part_prog
        points.append({"t": tick, "done": done, "prog": prog, "pct": _pct(done, total)})

    milestones = []
    for pct_mark in MILESTONES:
        need = int(math.ceil(total * pct_mark / 100.0)) if total > 0 else 0
        crossed = next((pt["t"] for pt in points if pt["done"] >= need), None)
        if total > 0 and crossed is not None:
            milestones.append(
                {
                    "pct": pct_mark,
                    "t": crossed,
                    "done": need,
                    "elapsed": max(0.0, crossed - points[0]["t"]) if points else 0.0,
                    "reached": True,
                }
            )
        else:
            milestones.append(
                {"pct": pct_mark, "t": None, "done": need, "elapsed": None, "reached": False}
            )

    per_hour: dict[int, int] = {}
    for part in parts:
        for row in part.get("runs_per_hour") or []:
            try:
                hour = int(row["t"])
                per_hour[hour] = per_hour.get(hour, 0) + int(row["n"])
            except (TypeError, ValueError, KeyError):
                continue
    runs_per_hour = [{"t": hour, "n": per_hour[hour]} for hour in sorted(per_hour)]

    def _sum(name: str) -> int:
        subtotal = 0
        for part in parts:
            try:
                subtotal += int(part.get(name) or 0)
            except (TypeError, ValueError):
                continue
        return subtotal

    undated = _sum("undated")
    tasks_word = "task" if undated == 1 else "tasks"
    payload = {
        "ws": "all",
        "workstreams": sorted(part["ws"] for part in parts),
        "total": total,
        "done": done_now,
        "pct": _pct(done_now, total),
        "counts": counts,
        "start": min(part["start"] for part in parts),
        "end": max(part["end"] for part in parts),
        "now": moment,
        "points": points,
        "milestones": milestones,
        "runs_per_hour": runs_per_hour,
        "runs_total": _sum("runs_total"),
        "undated_runs": _sum("undated_runs"),
        "observed": _sum("observed"),
        "inferred": _sum("inferred"),
        "dated": _sum("dated"),
        "undated": undated,
        "gap": ("no recorded task moves: %d done %s not dated" % (undated, tasks_word))
        if undated
        else None,
        "backfilled": [task for part in parts for task in (part.get("backfilled") or [])],
    }

    _CACHE["project-key"] = key
    _CACHE["project-payload"] = payload
    return payload


def _backfilled_tasks(ws: str) -> list[str]:
    """Task ids whose history holds a ``backfill``-marked row (oldest first)."""
    from dash.bundles import bundle_dir

    path = bundle_dir(ws)
    if path is None:
        return []
    try:
        text = (path / "history.jsonl").read_text(encoding="utf-8")
    except OSError:
        return []
    seen: list[str] = []
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
        task = row.get("task")
        if row.get("backfill") and isinstance(task, str) and task not in seen:
            seen.append(task)
    return seen


def _prog_at(chains: dict, moment: float) -> int:
    """Recorded ``prog`` count at one moment from per-task state chains."""
    return sum(1 for states in chains.values() if _state_at(states, moment) == "prog")


def _state_at(states: list, moment: float):
    """Latest ``to`` at or before the moment (None before the first row)."""
    current = None
    for ts, to in states:
        if ts <= moment:
            current = to
        else:
            break
    return current


def _pct(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(done / total * 1000.0) / 10.0


def build_wsprogress(store, ws: str, now: float | None = None) -> dict:
    """Progress-over-time payload for one workstream.

    Pure read path: plan file plus history file plus one store run pass.
    Cached by history/plan mtimes plus the store sequence. Raises
    ``dash.replay.ReplayNotFound`` when the workstream has no plan.
    """
    from dash.bundles import resolve_plan
    from dash.replay import ReplayNotFound, build_replay, read_history

    moment = float(now if now is not None else time.time())

    key = cache_key(store, ws)
    hit = _CACHE.get("key")
    if hit is not None and hit == key and "payload" in _CACHE:
        return _CACHE["payload"]

    _source, body = resolve_plan(ws)
    if not isinstance(body, dict):
        raise ReplayNotFound(ws)
    progress = body.get("progress") if isinstance(body, dict) else None
    if not isinstance(progress, dict):
        raise ReplayNotFound(ws)
    try:
        total = int(progress.get("total") or 0)
        done_now = int(progress.get("done") or 0)
    except (TypeError, ValueError):
        raise ReplayNotFound(ws)
    counts = dict(progress.get("counts") or {})

    replay = build_replay(store, ws, moment)
    tasks = replay.get("tasks") or {}
    done_times: list = sorted(
        (item["done_at"], tid)
        for tid, item in tasks.items()
        if isinstance(item, dict) and item.get("done_at") is not None
    )
    observed = int(replay.get("observed") or 0)
    inferred = int(replay.get("inferred") or 0)

    history = read_history(ws)
    chains: dict[str, list] = {}
    prog_times: set[float] = set()
    for row in history:
        task = row.get("task")
        to = row.get("to")
        if not isinstance(task, str) or not isinstance(to, str):
            continue
        chains.setdefault(task, []).append((float(row["ts"]), to))
        if to in ("prog", "pr", "done"):
            prog_times.add(float(row["ts"]))
    for states in chains.values():
        states.sort(key=lambda item: item[0])

    runs = store.query_runs(moment, ws, None, None, RUNS_LIMIT)
    run_starts: list[float] = []
    undated_runs = 0
    for run in runs:
        if not isinstance(run, dict) or run.get("source") == "dagster":
            continue
        start = run.get("started_at")
        try:
            start = None if start is None else float(start)
        except (TypeError, ValueError):
            start = None
        if start is None:
            undated_runs += 1
        else:
            run_starts.append(start)

    stamps: list[float] = [float(row["ts"]) for row in history]
    stamps += [moment_done for moment_done, _tid in done_times]
    stamps += run_starts
    stamps = [ts for ts in stamps if ts is not None]
    start = min(stamps) if stamps else moment
    data_end = max(stamps) if stamps else moment

    moments: set[float] = {start}
    moments.update(moment_done for moment_done, _tid in done_times)
    moments.update(prog_times)
    ordered = sorted(moment for moment in moments if moment >= start)

    points: list[dict] = []
    for tick in ordered:
        done = sum(1 for moment_done, _tid in done_times if moment_done <= tick)
        prog = _prog_at(chains, tick)
        points.append(
            {
                "t": tick,
                "done": done,
                "prog": prog,
                "pct": _pct(done, total),
            }
        )

    milestones: list[dict] = []
    for pct_mark in MILESTONES:
        need = int(math.ceil(total * pct_mark / 100.0)) if total > 0 else 0
        if total > 0 and need <= len(done_times):
            crossed = done_times[need - 1][0]
            milestones.append(
                {
                    "pct": pct_mark,
                    "t": crossed,
                    "done": need,
                    "elapsed": max(0.0, crossed - start),
                    "reached": True,
                }
            )
        else:
            milestones.append(
                {"pct": pct_mark, "t": None, "done": need, "elapsed": None, "reached": False}
            )

    per_hour: dict[int, int] = {}
    for value in run_starts:
        hour = int(value // HOUR_S) * HOUR_S
        per_hour[hour] = per_hour.get(hour, 0) + 1
    runs_per_hour = [{"t": hour, "n": per_hour[hour]} for hour in sorted(per_hour)]

    # Done tasks with a resolved moment (the replay rule: recorded
    # history first, mapped runs next, inference last). Named `dated`
    # (not burn-up's history-only `recorded`) so the two never mix.
    dated = sum(1 for _moment_done, _tid in done_times)
    undated = max(0, done_now - dated)
    gap = None
    if undated:
        tasks_word = "task" if undated == 1 else "tasks"
        first = min((float(row["ts"]) for row in history), default=None)
        if first is None:
            gap = "no recorded history: %d done %s not dated" % (undated, tasks_word)
        else:
            from dash.projectburn import _day_label, _utc_day

            gap = "no history before %s: %d done %s not dated" % (
                _day_label(_utc_day(first)),
                undated,
                tasks_word,
            )

    payload = {
        "ws": ws,
        "total": total,
        "done": done_now,
        "pct": progress.get("pct"),
        "counts": counts,
        "start": start,
        "end": data_end,
        "now": moment,
        "points": points,
        "milestones": milestones,
        "runs_per_hour": runs_per_hour,
        "runs_total": len(run_starts),
        "undated_runs": undated_runs,
        "observed": observed,
        "inferred": inferred,
        "dated": dated,
        "undated": undated,
        "gap": gap,
        "backfilled": _backfilled_tasks(ws),
    }

    _CACHE.clear()
    _CACHE["key"] = key
    _CACHE["payload"] = payload
    return payload
