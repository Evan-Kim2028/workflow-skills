"""Orchestration helpers for workstream plans: ready queue, waves, overlaps, gates.

Pure functions (``ready_queue``, ``critical_path``, ``waves``, ``overlaps``,
``stale_gates``, ``deferred_ledger``, ``dep_jumps``) take plain plan bodies
and return plain data, so both the ``ws`` CLI and ``dash.doctor`` can call
them without touching the store. ``lead_profile`` and the run-mapping
helpers read the dashboard database through the public ``Store`` query
methods (plus one read-only sqlite pass for ``out_path``, which the run
views do not carry).

A plan body is either the ``/api/plan`` shape (``groups`` at top level) or
the raw ``plan.json`` shape (``dag.groups``); every helper accepts both.

Study rules these enforce (generic form, no private data):

- execution serializes silently behind mega-gates, so ready tasks are
  ranked by critical-path impact, not listed flat;
- ready tasks starve while one agent runs, so waves name the max-parallel
  set and the CLI warns on stalled and dep-jump tasks;
- file collisions surface at merge time, so waves split on declared file
  footprints and overlap warnings fire while tasks are still open;
- gates decay after one check, so gates carry freshness and stale ones
  block their dependents loudly;
- deferred tasks leave the plan, so the ledger tracks the receiving end;
- workflow-driven leads read as zero delegation, so the delegation
  profile counts manifest and wrapper children too.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

#: A dep with one of these statuses no longer blocks its dependents.
#: ``prog`` is merged to main with only a production step left, so like
#: ``done`` it unblocks work waiting on the code.
SATISFIED_STATUSES = ("done", "prog")

#: Task statuses that mean "started": used for stalled and dep-jump checks.
ACTIVE_STATUSES = ("wip", "pr")

#: Sources whose rows are worker runs a lead may have launched (as opposed
#: to interactive lead sessions, shell rows, or Dagster jobs).
WORKER_SOURCES = ("muse", "glm", "grok", "generic", "claude-subagent", "claude-workflow", "shell")

#: Title-similarity floor for matching a deferred task to a receiving task.
TITLE_SIMILARITY = 0.6

#: Idle-ready share above which a lead is flagged IDLE-READY.
IDLE_READY_SHARE = 0.30

#: Lead-owned tool calls above which few workers means LOW-DELEGATION.
LEAD_TOOL_CALLS_MANY = 60

#: Context tokens above which a lead session is flagged CONTEXT>250k.
CONTEXT_TOKENS_FLAG = 250_000


def _groups_of(body: dict) -> list:
    """The task groups of either plan shape (API body or raw plan.json)."""
    if not isinstance(body, dict):
        return []
    groups = body.get("groups")
    if isinstance(groups, list):
        return groups
    dag = body.get("dag")
    if isinstance(dag, dict) and isinstance(dag.get("groups"), list):
        return dag["groups"]
    return []


def plan_tasks(body: dict) -> tuple[dict, dict, list]:
    """(by_id, deps, order) for a plan body.

    ``by_id`` maps task id to its task dict, ``deps`` maps task id to its
    dep-id list (strings only), ``order`` is first-seen task order.
    """
    by_id: dict[str, dict] = {}
    deps: dict[str, list[str]] = {}
    order: list[str] = []
    for group in _groups_of(body):
        tasks = group.get("tasks") if isinstance(group, dict) else None
        for task in tasks or []:
            if not isinstance(task, dict) or task.get("id") in (None, ""):
                continue
            tid = str(task["id"])
            if tid in by_id:
                continue
            by_id[tid] = task
            raw = task.get("deps")
            deps[tid] = [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
            order.append(tid)
    return by_id, deps, order


def plan_trains(body: dict) -> list:
    """The sanitized-or-raw trains list of a plan body (empty when absent)."""
    trains = body.get("trains")
    return [item for item in trains if isinstance(item, dict)] if isinstance(trains, list) else []


def train_of(body: dict, task_id: str) -> str | None:
    """The train id whose members include ``task_id``, or None."""
    for train in plan_trains(body):
        members = train.get("members")
        if isinstance(members, list) and task_id in members:
            ident = train.get("id")
            return str(ident) if ident is not None else None
    return None


def _dependents_map(by_id: dict, deps: dict) -> dict[str, list[str]]:
    """Direct dependents per task id (reverse of deps, plan members only)."""
    downstream: dict[str, list[str]] = {tid: [] for tid in by_id}
    for tid, wants in deps.items():
        for dep in wants:
            if dep in downstream and tid in by_id:
                downstream[dep].append(tid)
    return downstream


def downstream_stats(by_id: dict, deps: dict) -> dict[str, dict]:
    """Per task ``{"chain": n, "dependents": m, "unblocks": [...]}``.

    ``chain`` is the longest downstream path starting at the task counting
    itself (1 when nothing waits on it); ``dependents`` is the transitive
    dependent count; ``unblocks`` is the sorted direct-dependent list.
    """
    downstream = _dependents_map(by_id, deps)
    chain: dict[str, int] = {}
    trans: dict[str, set] = {}

    def visit(tid: str, seen: set) -> int:
        if tid in chain:
            return chain[tid]
        if tid in seen:
            return 1
        seen.add(tid)
        kids = [kid for kid in downstream.get(tid, []) if kid in by_id]
        best = 0
        merged: set[str] = set()
        for kid in kids:
            best = max(best, visit(kid, seen))
            merged.add(kid)
            merged |= trans.get(kid, set())
        seen.discard(tid)
        chain[tid] = best + 1
        trans[tid] = merged
        return chain[tid]

    out = {}
    for tid in by_id:
        out[tid] = {
            "chain": visit(tid, set()),
            "dependents": len(trans[tid]),
            "unblocks": sorted(trans[tid] and [kid for kid in downstream.get(tid, []) if kid in by_id]),
        }
    return out


def _dep_satisfied(by_id: dict, dep: str) -> bool:
    """A dep is satisfied when it is a known task whose status is done-like.

    Unknown dep ids (an invalid plan) and gates that are still todo block,
    so a typo never reads as ready.
    """
    task = by_id.get(dep)
    return isinstance(task, dict) and task.get("status") in SATISFIED_STATUSES


def ready_queue(body: dict, live_by_task: dict | None = None) -> list[dict]:
    """Ready tasks (status todo, every dep satisfied) ranked by impact.

    Rank is longest downstream chain first, then transitive dependent
    count, then task id. Gates count as deps: a todo gate blocks every
    task that names it. Each row carries what the task unblocks, its
    train, and the live run label when ``live_by_task`` maps it.
    """
    by_id, deps, _order = plan_tasks(body)
    stats = downstream_stats(by_id, deps)
    live = live_by_task or {}
    rows = []
    for tid, task in by_id.items():
        if task.get("status") != "todo":
            continue
        if not all(_dep_satisfied(by_id, dep) for dep in deps.get(tid, [])):
            continue
        stat = stats[tid]
        rows.append(
            {
                "id": tid,
                "title": task.get("title"),
                "chain": stat["chain"],
                "dependents": stat["dependents"],
                "unblocks": stat["unblocks"],
                "train": train_of(body, tid),
                "live_run": live.get(tid),
            }
        )
    rows.sort(key=lambda row: (-row["chain"], -row["dependents"], row["id"]))
    return rows


def stalled_tasks(body: dict, live_by_task: dict | None = None) -> list[dict]:
    """Tasks in wip/pr with no live run mapped to them.

    A started task with no worker behind it is either stalled or tracked
    elsewhere; either way the lead should look. ``live_by_task`` maps task
    id to a live run label (empty or None means no live worker).
    """
    live = live_by_task or {}
    rows = []
    by_id, _deps, _order = plan_tasks(body)
    for tid, task in by_id.items():
        if task.get("status") in ACTIVE_STATUSES and not live.get(tid):
            rows.append({"id": tid, "title": task.get("title"), "status": task.get("status"), "train": train_of(body, tid)})
    rows.sort(key=lambda row: row["id"])
    return rows


def dep_jumps(body: dict, starts: dict, dones: dict) -> list[dict]:
    """Tasks that started before a dep finished.

    ``starts`` maps task id to first-active epoch (or None when unknown),
    ``dones`` maps task id to done epoch (or None when not done). A task
    with a known start jumped a dep when the dep is still unfinished, or
    when the dep's done moment lands at or after the start. A dep that is
    already done but has no recorded moment is skipped (order unknown,
    benefit of the doubt), so backfilled statuses never read as jumps.
    Pure: callers build the maps from history.jsonl, run starts, or
    replay moments.
    """
    by_id, deps, _order = plan_tasks(body)
    rows = []
    for tid in by_id:
        start = starts.get(tid)
        if start is None:
            continue
        try:
            start_f = float(start)
        except (TypeError, ValueError):
            continue
        for dep in deps.get(tid, []):
            if dep not in by_id:
                continue
            done = dones.get(dep)
            if done is None:
                if by_id[dep].get("status") not in SATISFIED_STATUSES:
                    rows.append({"task": tid, "dep": dep, "started_at": start_f, "dep_done_at": None})
                continue
            try:
                done_f = float(done)
            except (TypeError, ValueError):
                continue
            if start_f <= done_f:
                rows.append({"task": tid, "dep": dep, "started_at": start_f, "dep_done_at": done_f})
    rows.sort(key=lambda row: (row["task"], row["dep"]))
    return rows


#: Numbered fix/review markers in run labels (``-fixN``, ``-revN``).
_FIXREV_RE = re.compile(r"-(fix|rev)(\d+)(?![0-9])", re.IGNORECASE)


def label_rounds(label: str | None) -> list[tuple[str, int]]:
    """Fix/review round markers in one run label: ``[(kind, number)]``.

    Reviews append ``-revN`` and fixes append ``-fixN`` (see
    docs/integrate.md), so ``ws4-WS4.1-slug-fix2`` carries
    ``[("fix", 2)]`` and a plain lane label carries none. Matching is
    case-insensitive and purely on the label string: a slug that merely
    contains those letters without the hyphen-plus-number form does not
    count. Callers union the markers across a task's labels and compare
    the distinct count against the fix-round cap.
    """
    return [
        (match.group(1).lower(), int(match.group(2)))
        for match in _FIXREV_RE.finditer(str(label or ""))
    ]


def _topo_order(by_id: dict, deps: dict) -> list[str]:
    """Task ids in dependency-first order (ties broken by plan order)."""
    order = [tid for tid in by_id]
    pos = {tid: index for index, tid in enumerate(order)}
    pending = {tid: [dep for dep in deps.get(tid, []) if dep in by_id] for tid in by_id}
    done: set[str] = set()
    out: list[str] = []
    while len(out) < len(by_id):
        ready = sorted(
            [tid for tid in by_id if tid not in done and all(dep in done for dep in pending[tid])],
            key=lambda tid: pos[tid],
        )
        if not ready:
            rest = sorted([tid for tid in by_id if tid not in done], key=lambda tid: pos[tid])
            out.extend(rest)
            break
        out.extend(ready)
        done.update(ready)
    return out


def critical_path(body: dict, durations: dict | None = None) -> list[str]:
    """The longest dependency chain: by task count, or by observed duration.

    Without ``durations`` every task costs 1 and the path is the longest
    chain in tasks. With ``durations`` (task id to seconds) each task
    costs its duration (missing or bad values cost 1... see below: they
    cost 0 so unobserved tasks never outrank observed ones? No: they cost
    1 task-unit). Missing values fall back to 1 so partially observed
    plans still rank unobserved tails honestly. Ties break toward the
    plan order. Downstream-first: the path reads from the binding root to
    the last task it unblocks.
    """
    by_id, deps, _order = plan_tasks(body)
    if not by_id:
        return []

    def cost(tid: str) -> float:
        if not durations:
            return 1.0
        try:
            value = float(durations.get(tid, 1.0))
        except (TypeError, ValueError):
            return 1.0
        return max(0.0, value)

    best_end: dict[str, float] = {}
    best_prev: dict[str, str | None] = {}
    for tid in _topo_order(by_id, deps):
        known = [(dep, best_end[dep]) for dep in deps.get(tid, []) if dep in best_end]
        if known:
            prev, value = max(known, key=lambda item: item[1])
            best_end[tid] = value + cost(tid)
            best_prev[tid] = prev
        else:
            best_end[tid] = cost(tid)
            best_prev[tid] = None
    end = max(by_id, key=lambda tid: (best_end[tid], -list(by_id).index(tid)))
    path = []
    node: str | None = end
    while node is not None:
        path.append(node)
        node = best_prev[node]
    path.reverse()
    return path


def task_files(task: dict) -> list[str]:
    """Declared file footprints of one task (the optional ``files`` field)."""
    files = task.get("files")
    return [item for item in files if isinstance(item, str) and item] if isinstance(files, list) else []


def globs_overlap(first: str, second: str) -> bool:
    """True when two file globs can name the same file.

    Equal strings overlap, and either glob matching the other as a path
    counts (so ``api/x.py`` collides with ``api/*.py``). Glob-vs-glob
    pairs that only share a wildcard expansion without matching
    literally (``a/*.py`` vs ``b/*.py``) do not count: the rule favors
    the explicit footprints leads actually declare.
    """
    if first == second:
        return True
    return fnmatch.fnmatchcase(first, second) or fnmatch.fnmatchcase(second, first)


def files_overlap(first: list[str], second: list[str]) -> list[str]:
    """Sorted ``"a <> b"`` pairs witnessing the overlap of two file lists."""
    hits = set()
    for pattern_a in first:
        for pattern_b in second:
            if globs_overlap(pattern_a, pattern_b):
                hits.add(f"{pattern_a} <> {pattern_b}")
    return sorted(hits)


def overlaps(body: dict, only_ids: list[str] | set[str] | None = None) -> list[dict]:
    """Declared-file overlap pairs: tasks whose ``files`` can collide.

    ``only_ids`` restricts the pairs (the wip/pr set, or one wave); None
    compares every pair. Each row names both tasks and the witnessing
    glob pairs. Pure on declared footprints; inferred diffs join later.
    """
    by_id, _deps, order = plan_tasks(body)
    members = [tid for tid in order if only_ids is None or tid in only_ids]
    rows = []
    for pos, left in enumerate(members):
        mine = task_files(by_id[left])
        if not mine:
            continue
        for right in members[pos + 1:]:
            theirs = task_files(by_id[right])
            if not theirs:
                continue
            shared = files_overlap(mine, theirs)
            if shared:
                rows.append({"a": left, "b": right, "files": shared, "source": "declared"})
    return rows


def waves(body: dict) -> list[list[str]]:
    """Max-parallel waves: topological levels, split on file conflicts.

    Each level holds tasks whose deps all sit in earlier levels; a level
    is then split greedily (plan order) so no two tasks in one wave share
    a declared file. Without ``files`` footprints a level is one wave.
    Done tasks keep their level so the shape stays stable as work lands.
    """
    by_id, deps, _order = plan_tasks(body)
    level: dict[str, int] = {}
    for tid in _topo_order(by_id, deps):
        known = [level[dep] for dep in deps.get(tid, []) if dep in level]
        level[tid] = (max(known) + 1) if known else 0
    buckets: dict[int, list[str]] = {}
    for tid in _topo_order(by_id, deps):
        buckets.setdefault(level[tid], []).append(tid)
    out: list[list[str]] = []
    for depth in sorted(buckets):
        rest = list(buckets[depth])
        while rest:
            wave: list[str] = []
            kept: list[str] = []
            placed_files: list[str] = []
            for tid in rest:
                mine = task_files(by_id[tid])
                if mine and any(files_overlap([item], mine) for item in placed_files):
                    kept.append(tid)
                    continue
                wave.append(tid)
                placed_files.extend(mine)
            if not wave:
                wave.append(kept.pop(0))
            out.append(wave)
            rest = kept
    return out


def max_parallel(body: dict) -> int:
    """The plan's max parallelism: the widest wave."""
    levels = waves(body)
    return max((len(wave) for wave in levels), default=0)


def plan_extra(body: dict, durations: dict | None = None) -> dict:
    """The ``/api/plan`` orchestration fields: ready, critical path, width."""
    ready = [row["id"] for row in ready_queue(body)]
    path = critical_path(body, durations)
    return {"ready": ready, "critical_path": path, "max_parallel": max_parallel(body)}


def _parse_when(value) -> float | None:
    """Epoch seconds for an ISO-8601 stamp (trailing Z allowed), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def stale_gates(body: dict, now: float | None = None) -> list[dict]:
    """Every gate task with its signal, freshness, and blocked dependents.

    A gate is STALE when ``fresh_hours`` is set and ``confirmed_at`` is
    missing or older than that many hours. Gates without ``fresh_hours``
    never go stale (their signal is standing, not sampled). ``blocked``
    lists the sorted tasks that name the gate in ``deps``.
    """
    moment = time.time() if now is None else now
    by_id, deps, order = plan_tasks(body)
    downstream = _dependents_map(by_id, deps)
    rows = []
    for tid in order:
        task = by_id[tid]
        if task.get("kind") != "gate":
            continue
        signal = task.get("signal") if isinstance(task.get("signal"), str) else None
        confirmed_raw = task.get("confirmed_at") if isinstance(task.get("confirmed_at"), str) else None
        fresh = task.get("fresh_hours")
        fresh_f = None
        if isinstance(fresh, bool):
            fresh_f = None
        elif isinstance(fresh, (int, float)):
            fresh_f = float(fresh) if float(fresh) > 0 else None
        confirmed_at = _parse_when(confirmed_raw)
        stale = bool(fresh_f is not None and (confirmed_at is None or moment - confirmed_at > fresh_f * 3600))
        rows.append(
            {
                "id": tid,
                "title": task.get("title"),
                "status": task.get("status"),
                "signal": signal,
                "confirmed_at": confirmed_raw,
                "fresh_hours": fresh,
                "stale": stale,
                "blocked": sorted(downstream.get(tid, [])),
            }
        )
    return rows


def _title_similarity(first: str, second: str) -> float:
    return SequenceMatcher(None, first.lower(), second.lower()).ratio()


def deferred_ledger(bodies: dict[str, dict]) -> list[dict]:
    """Deferred tasks across bundles with their receiving-end state.

    ``bodies`` maps workstream id to plan body. Each row carries the
    deferred task, its ``moved_to`` workstream, its ``received_as`` hint,
    and whether a matching task exists there: first by the explicit
    ``received_as`` id, then by title similarity (>= 0.6). A deferral
    with a ``moved_to`` but no receiving match, or with no ``moved_to``
    at all, is an ``orphan`` nobody picked up.
    """
    rows = []
    for ws in sorted(bodies):
        by_id, _deps, order = plan_tasks(bodies[ws])
        for tid in order:
            task = by_id[tid]
            if task.get("status") != "deferred":
                continue
            moved = task.get("moved_to") if isinstance(task.get("moved_to"), str) else None
            received_as = task.get("received_as") if isinstance(task.get("received_as"), str) else None
            received = None
            if moved and moved in bodies:
                target_ids, _target_deps, _target_order = plan_tasks(bodies[moved])
                if received_as and received_as in target_ids:
                    received = received_as
                else:
                    mine = str(task.get("title") or "")
                    best: str | None = None
                    best_score = 0.0
                    for cand, cand_task in target_ids.items():
                        score = _title_similarity(mine, str(cand_task.get("title") or ""))
                        if score > best_score:
                            best_score = score
                            best = cand
                    if best is not None and best_score >= TITLE_SIMILARITY:
                        received = best
            rows.append(
                {
                    "ws": ws,
                    "id": tid,
                    "title": task.get("title"),
                    "moved_to": moved,
                    "note": task.get("note") if isinstance(task.get("note"), str) else None,
                    "received_as": received_as,
                    "received": received,
                    "orphan": received is None,
                }
            )
    rows.sort(key=lambda row: (row["ws"], row["id"]))
    return rows


def live_by_task(
    runs: list[dict],
    ws: str,
    plan_ids: list[str] | tuple = (),
    taskmap: dict | None = None,
    live_only: bool = True,
) -> dict[str, str]:
    """Map task id to the label of a run working it (newest wins).

    Labels map through the shared rule (``dash.taskmatch``): exact map
    entries, regex entries, built-ins, then plan-id substring. With
    ``live_only`` only ``running``/``queued`` rows count; else every run
    counts. Labels only, never prompts.
    """
    from dash import taskmatch

    table = taskmap if isinstance(taskmap, dict) else taskmatch.load_taskmap()
    ids = list(plan_ids) if plan_ids else taskmatch.plan_task_ids([])
    best: dict[str, tuple[float, str]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        if live_only and run.get("status") not in ("running", "queued"):
            continue
        owner = run.get("workstream")
        if owner and owner != ws:
            continue
        label = run.get("label") or ""
        tid = taskmatch.task_of(label, ws, table, ids)
        if not isinstance(tid, str):
            continue
        start = run.get("started_at") or 0
        try:
            start_f = float(start)
        except (TypeError, ValueError):
            start_f = 0.0
        if tid not in best or start_f >= best[tid][0]:
            best[tid] = (start_f, label)
    return {tid: label for tid, (_start, label) in best.items()}


def dashboard_runs(base_url: str, ws: str | None = None, timeout: float = 3.0) -> list[dict] | None:
    """Live run rows from the dashboard API, or None when unreachable.

    Read-only GET ``/api/runs``; any transport or decode failure returns
    None so callers fall back to "live status unknown" instead of failing.
    """
    import json
    import urllib.parse
    import urllib.request

    query = "/api/runs?limit=5000"
    if ws:
        query += "&ws=" + urllib.parse.quote(ws)
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + query, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    runs = payload.get("runs") if isinstance(payload, dict) else None
    return runs if isinstance(runs, list) else None


def dashboard_replay(base_url: str, ws: str, timeout: float = 5.0) -> dict | None:
    """The ``/api/replay`` timeline for a workstream, or None when missing.

    404 (no plan) and transport failures both return None; callers treat
    durations and spans as unknown rather than failing.
    """
    import json
    import urllib.parse
    import urllib.request

    try:
        with urllib.request.urlopen(
            base_url.rstrip("/") + "/api/replay?ws=" + urllib.parse.quote(ws), timeout=timeout
        ) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def replay_durations(replay: dict | None) -> dict[str, float]:
    """Observed task durations (done_at minus first_active) from a replay.

    Only tasks with both moments observed (never inferred) count, so a
    guessed moment never becomes a measured span.
    """
    out: dict[str, float] = {}
    tasks = replay.get("tasks") if isinstance(replay, dict) else None
    if not isinstance(tasks, dict):
        return out
    for tid, item in tasks.items():
        if not isinstance(item, dict) or item.get("inferred"):
            continue
        start = item.get("first_active")
        end = item.get("done_at")
        try:
            span = float(end) - float(start)
        except (TypeError, ValueError):
            continue
        if span >= 0:
            out[str(tid)] = span
    return out


def replay_starts_dones(replay: dict | None) -> tuple[dict, dict]:
    """(starts, dones) maps for ``dep_jumps`` from a replay timeline."""
    starts: dict[str, float] = {}
    dones: dict[str, float] = {}
    tasks = replay.get("tasks") if isinstance(replay, dict) else None
    if not isinstance(tasks, dict):
        return starts, dones
    for tid, item in tasks.items():
        if not isinstance(item, dict):
            continue
        for key, dest in (("first_active", starts), ("done_at", dones)):
            try:
                dest[str(tid)] = float(item[key])
            except (KeyError, TypeError, ValueError):
                continue
    return starts, dones


def default_db_path() -> Path:
    """The dashboard sqlite path (``AGENT_DASH_STATE_DIR`` wins)."""
    import os

    raw = os.environ.get("AGENT_DASH_STATE_DIR")
    if raw:
        return Path(raw).expanduser() / "dash.sqlite"
    return Path.home() / ".local" / "share" / "agent-dash" / "dash.sqlite"


def run_out_paths(db_path: Path | None = None) -> dict[str, str]:
    """Run id to ``out_path`` from a read-only pass over the runs table.

    The served run views do not carry ``out_path``, so overlap inference
    reads it here instead of widening the API. Read-only URI, no writes,
    and any failure (missing DB, locked, odd schema) returns {}.
    """
    import contextlib

    path = Path(db_path) if db_path is not None else default_db_path()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except Exception:
        return {}
    try:
        try:
            rows = conn.execute("SELECT run_id, out_path FROM runs WHERE out_path IS NOT NULL AND out_path != ''").fetchall()
        except Exception:
            return {}
        return {str(run_id): str(out) for run_id, out in rows if run_id and out}
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def enclosing_worktree(start: Path) -> Path | None:
    """Nearest ancestor (or self) holding ``.git``, or None.

    Run folders sometimes live inside the worktree the worker ran in; the
    out path then reveals the worktree by walking up. Scratchpads outside
    any checkout return None and the caller skips silently.
    """
    node = start if start.is_dir() else start.parent
    for _ in range(12):
        if (node / ".git").exists():
            return node
        parent = node.parent
        if parent == node:
            return None
        node = parent
    return None


def branch_files(worktree: Path, branch: str, timeout: float = 15.0) -> list[str] | None:
    """Files ``branch`` touches vs main, read-only, or None when unknown.

    Runs ``git diff --name-only origin/main...<branch>`` in ``worktree``
    (falling back to ``main...<branch>`` with no origin), never fetching
    or writing. Any failure (no git, unknown branch, timeout) is None.
    """
    import subprocess

    for base in ("origin/main", "main"):
        try:
            proc = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--name-only", f"{base}...{branch}"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            return None
        if proc.returncode == 0:
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if "unknown revision" in (proc.stderr or "") and base == "origin/main":
            continue
        return None
    return None


def infer_branch(task_id: str, body: dict) -> str | None:
    """The train branch most likely carrying ``task_id``, or None."""
    for train in plan_trains(body):
        members = train.get("members")
        if isinstance(members, list) and task_id in members:
            branch = train.get("branch")
            if isinstance(branch, str) and branch:
                return branch
    return None


def inferred_overlaps(
    body: dict,
    runs: list[dict],
    task_ids: list[str] | set[str] | None = None,
    out_paths: dict[str, str] | None = None,
    label_worktrees: dict[str, str] | None = None,
) -> list[dict]:
    """Overlap pairs inferred from live runs' branch diffs.

    Each live run mapped to a plan task resolves a branch (its train's
    branch) and a worktree (walked up from its ``out_path``, or the
    label-worktree map). Tasks whose diffs share files are one row with
    ``source`` ``inferred``. Unknown branches and worktrees skip
    silently: no evidence means no row, never an error.
    """
    from dash import taskmatch

    by_id, _deps, _order = plan_tasks(body)
    ws = body.get("ws") if isinstance(body.get("ws"), str) else None
    if ws is None and isinstance(body.get("workstream"), str):
        ws = body.get("workstream")
    ids = list(by_id)
    table = taskmatch.load_taskmap()
    exact: dict[str, str] = {}
    extracted_wt: dict[str, str] = {}
    if isinstance(table, dict):
        for sub in table.values():
            if not isinstance(sub, dict):
                continue
            for lab, val in sub.items():
                if isinstance(val, dict):
                    if isinstance(val.get("task"), str):
                        exact[str(lab)] = val["task"]
                    if isinstance(val.get("worktree"), str):
                        extracted_wt[str(lab)] = val["worktree"]
    paths = out_paths if isinstance(out_paths, dict) else {}
    wt_map = dict(extracted_wt)
    if isinstance(label_worktrees, dict):
        wt_map.update(label_worktrees)
    diffs: dict[str, set[str]] = {}
    wanted = set(task_ids) if task_ids is not None else None
    for run in runs:
        if not isinstance(run, dict) or run.get("status") not in ("running", "queued"):
            continue
        label = run.get("label") or ""
        tid = exact.get(label) or taskmatch.task_of(label, ws, table, ids)
        if not isinstance(tid, str) or tid not in by_id:
            continue
        if wanted is not None and tid not in wanted:
            continue
        if tid in diffs:
            continue
        branch = infer_branch(tid, body)
        if not branch:
            continue
        worktree: Path | None = None
        if label in wt_map:
            candidate = Path(wt_map[label])
            worktree = candidate if candidate.is_dir() else None
        else:
            out = paths.get(run.get("run_id") or "") or run.get("out_path")
            if isinstance(out, str) and out:
                worktree = enclosing_worktree(Path(out))
        if worktree is None:
            continue
        files = branch_files(worktree, branch)
        if files is None:
            continue
        diffs[tid] = set(files)
    rows = []
    members = sorted(diffs)
    for pos, left in enumerate(members):
        for right in members[pos + 1:]:
            shared = sorted(diffs[left] & diffs[right])
            if shared:
                rows.append({"a": left, "b": right, "files": shared, "source": "inferred"})
    return rows


def _sweep_stats(intervals: list[tuple[float, float]]) -> tuple[float, int]:
    """(mean concurrency, peak concurrency) for [start, end) intervals."""
    if not intervals:
        return 0.0, 0
    events: list[tuple[float, int]] = []
    total = 0.0
    span_end = max(end for _start, end in intervals)
    span_start = min(start for start, _end in intervals)
    for start, end in intervals:
        if end > start:
            events.append((start, 1))
            events.append((end, -1))
            total += end - start
    events.sort(key=lambda item: (item[0], item[1]))
    peak = 0
    live = 0
    for _at, delta in events:
        live += delta
        peak = max(peak, live)
    width = span_end - span_start
    return (total / width if width > 0 else float(len(intervals))), peak


def _muse_tool_calls(session_uuid: str, roots: list[str] | None = None) -> tuple[int | None, str | None]:
    """(tool_use count, transcript path) for a muse session, else (None, None).

    Best effort and read-only: counts ``"tool_use"`` lines in the session
    log when it is on disk. Unknown layouts and missing logs are None,
    never an error; the delegation verdict does not depend on this.
    """
    import glob as _glob
    import os

    candidates = list(roots or [])
    if not candidates:
        default = os.environ.get("AGENT_DASH_MUSE_SESSIONS") or str(Path.home() / ".local" / "share" / "muse" / "sessions")
        candidates = [default]
    for root in candidates:
        matches = _glob.glob(os.path.join(root, "*", "*", "*", session_uuid, "session.jsonl"))
        for match in matches:
            try:
                if os.path.getsize(match) > 32 * 1024 * 1024:
                    continue
                with open(match, encoding="utf-8", errors="replace") as handle:
                    count = sum(1 for line in handle if '"tool_use"' in line)
                return count, match
            except OSError:
                continue
    return None, None


def lead_profile(store, session_id: str | None = None, ws: str | None = None, now: float | None = None) -> dict | None:
    """A lead session's delegation profile: every worker it launched.

    ``session_id`` profiles one lead session: every run whose
    ``parent_session_id`` names it (workflow wrappers and direct
    subagents included). ``ws`` finds the workstream's leads by who
    actually launched its runs — parent sessions with children in that
    workstream — and reports the busiest as primary (ties break toward
    the earliest launch), plus in-window worker-source runs in the same
    workstream with no parent recorded. Those parentless rows are the
    Bash-launched manifest workers (muse/cursor/GLM) and generic
    reports the old parent-only count read as zero delegation; the
    workstream attribution itself comes from run labels, so this is the
    label-prefix leg of the match. A collapsed workflow lane (one
    wrapper row hiding N workers) counts all N through its ``children``
    tally. Returns None when the session (or workstream) is unknown.

    Also reports the lead's own tool-call count when its transcript is
    on disk, mean/peak worker concurrency, the idle-ready share for its
    workstream over the session window, wave sizes with tail lanes, and
    flags: LOW-DELEGATION, IDLE-READY, LONG-TAIL, CONTEXT>250k.
    """
    moment = time.time() if now is None else now
    sessions = store.query_sessions()
    by_sid = {str(item.get("session_id")): item for item in sessions if item.get("session_id")}
    if session_id is not None and session_id not in by_sid:
        return None
    if session_id is None and ws is None:
        return None
    runs = store.query_runs(moment, None, None, None, 20000)

    kids: dict[str, list[dict]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        parent = run.get("parent_session_id")
        if not parent or run.get("session_id") == parent:
            continue
        kids.setdefault(str(parent), []).append(run)

    def in_window(run: dict, start: float, end: float) -> bool:
        at = run.get("started_at")
        try:
            return start <= float(at) <= end
        except (TypeError, ValueError):
            return False

    def earliest_start(items: list[dict]) -> float:
        starts = []
        for run in items:
            try:
                starts.append(float(run.get("started_at")))
            except (TypeError, ValueError):
                continue
        return min(starts) if starts else moment

    considered = 0
    if session_id is not None:
        primary = by_sid.get(session_id, {})
        primary_sid = session_id
        children = [run for run in kids.get(session_id, []) if run.get("session_id") != session_id]
        lead_ws = ws or primary.get("workstream")
        if not lead_ws:
            owned = Counter(run.get("workstream") for run in children if run.get("workstream"))
            lead_ws = owned.most_common(1)[0][0] if owned else None
        considered = 1
    else:
        assert ws is not None
        in_ws = {sid: [run for run in items if run.get("workstream") == ws] for sid, items in kids.items()}
        in_ws = {sid: items for sid, items in in_ws.items() if items}
        considered = len(in_ws)
        if in_ws:
            primary_sid = max(sorted(in_ws), key=lambda sid: (len(in_ws[sid]), -earliest_start(in_ws[sid])))
            primary = by_sid.get(primary_sid, {})
            children = [run for run in kids.get(primary_sid, []) if run.get("session_id") != primary_sid]
        else:
            owned = [item for item in sessions if item.get("workstream") == ws]
            if not owned:
                return None
            owned.sort(key=lambda item: item.get("started_at") or 0)
            primary = owned[0]
            primary_sid = str(primary.get("session_id"))
            children = []
        lead_ws = ws

    lead_start = primary.get("started_at") if isinstance(primary, dict) else None
    try:
        lead_start = float(lead_start)
    except (TypeError, ValueError):
        lead_start = earliest_start(children)
    ends = []
    for run in children:
        end = run.get("ended_at")
        try:
            ends.append(float(end))
        except (TypeError, ValueError):
            continue
    window_end = max(ends) if ends else moment
    if window_end < lead_start:
        window_end = moment

    if session_id is None and lead_ws:
        lead_ids = set(by_sid)
        for run in runs:
            if not isinstance(run, dict) or run.get("source") not in WORKER_SOURCES:
                continue
            if run.get("session_id") in lead_ids or any(run.get("run_id") == kid.get("run_id") for kid in children):
                continue
            if run.get("workstream") != lead_ws or run.get("parent_session_id"):
                continue
            if in_window(run, lead_start, window_end):
                children.append(run)

    children.sort(key=lambda run: run.get("started_at") or 0)
    intervals = []
    for run in children:
        try:
            start = float(run.get("started_at"))
        except (TypeError, ValueError):
            continue
        end = run.get("ended_at")
        try:
            stop = float(end)
        except (TypeError, ValueError):
            stop = window_end if run.get("status") in ("running", "queued") else start
        intervals.append((start, max(stop, start)))
    mean_cc, peak_cc = _sweep_stats(intervals)

    tool_calls, transcript = _muse_tool_calls(primary_sid)
    tokens = primary.get("tokens") or 0
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        tokens = 0

    idle_share = _idle_ready_share(store, lead_ws, lead_start, window_end, moment)

    from dash.bundles import resolve_plan

    wave_sizes: list[int] = []
    if lead_ws:
        try:
            _source, body = resolve_plan(lead_ws)
        except Exception:
            body = None
        if isinstance(body, dict):
            wave_sizes = [len(wave) for wave in waves(body)]

    by_label: dict[str, list[float]] = {}
    for run in children:
        start = run.get("started_at")
        end = run.get("ended_at") or (window_end if run.get("status") in ("running", "queued") else None)
        try:
            span = float(end) - float(start)
        except (TypeError, ValueError):
            continue
        if span < 0:
            continue
        by_label.setdefault(str(run.get("label") or run.get("run_id")), []).append(span)
    lanes = sorted(
        ({"label": label, "seconds": max(spans)} for label, spans in by_label.items()),
        key=lambda item: item["seconds"],
        reverse=True,
    )
    tails = lanes[:3]

    hidden = 0
    for run in children:
        extra = run.get("children")
        if isinstance(extra, int) and extra > 0:
            hidden += extra
    launched = len(children) + hidden

    flags = []
    if launched < 2 or (tool_calls is not None and tool_calls > LEAD_TOOL_CALLS_MANY and launched < 3):
        flags.append("LOW-DELEGATION")
    if idle_share is not None and idle_share > IDLE_READY_SHARE:
        flags.append("IDLE-READY")
    spans = sorted(item["seconds"] for item in lanes)
    if len(spans) >= 3 and spans[-1] > 3 * spans[len(spans) // 2]:
        flags.append("LONG-TAIL")
    if tokens > CONTEXT_TOKENS_FLAG:
        flags.append("CONTEXT>250k")

    by_source: dict[str, int] = {}
    for run in children:
        by_source[str(run.get("source"))] = by_source.get(str(run.get("source")), 0) + 1
    return {
        "session_id": primary_sid,
        "ws": lead_ws,
        "tool": primary.get("tool"),
        "label": primary.get("label"),
        "started_at": primary.get("started_at"),
        "window": {"start": lead_start, "end": window_end},
        "workers_launched": launched,
        "by_source": by_source,
        "own_tool_calls": tool_calls,
        "transcript": transcript,
        "context_tokens": tokens,
        "mean_concurrency": round(mean_cc, 2),
        "peak_concurrency": peak_cc,
        "idle_ready_share": None if idle_share is None else round(idle_share, 3),
        "wave_sizes": wave_sizes,
        "tails": [{**item, "seconds": round(item["seconds"], 1)} for item in tails],
        "flags": flags,
        "sessions_considered": considered,
    }


def _idle_ready_share(store, ws: str | None, start: float, end: float, now: float) -> float | None:
    """Share of the session window with <=1 agent running while tasks were ready.

    Readiness replays from the replay rule: a task is pending while it has
    no done moment at or before ``t`` and ready while every dep is done by
    ``t``. Concurrency comes from the store buckets. Any missing input
    (no workstream, no plan, no samples) is None, never zero: unknown is
    not idle.
    """
    if not ws or end <= start:
        return None
    try:
        from dash.replay import build_replay

        replay = build_replay(store, ws, now)
    except Exception:
        return None
    tasks = replay.get("tasks") if isinstance(replay, dict) else None
    if not isinstance(tasks, dict) or not tasks:
        return None
    try:
        from dash.bundles import resolve_plan

        _source, body = resolve_plan(ws)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    by_id, deps, _order = plan_tasks(body)
    done_at: dict[str, float | None] = {}
    for tid in by_id:
        item = tasks.get(tid)
        moment = item.get("done_at") if isinstance(item, dict) else None
        try:
            done_at[tid] = float(moment)
        except (TypeError, ValueError):
            done_at[tid] = None
    cands = [tid for tid in by_id if by_id[tid].get("status") != "deferred"]
    try:
        points = store.query_concurrency(now, ws, start, 60.0)
    except Exception:
        return None
    points = [point for point in points if isinstance(point, dict)]
    kept = []
    for point in points:
        try:
            moment = float(point.get("t", 0))
        except (TypeError, ValueError):
            continue
        if start <= moment <= end:
            kept.append(point)
    if not kept:
        return None

    def ready_at(moment: float) -> bool:
        for tid in cands:
            moment_done = done_at.get(tid)
            if moment_done is not None and moment_done <= moment:
                continue
            blocked = False
            for dep in deps.get(tid, []):
                dep_done = done_at.get(dep)
                if dep not in by_id or dep_done is None or dep_done > moment:
                    blocked = True
                    break
            if not blocked:
                return True
        return False

    idle = 0
    for point in kept:
        total = sum(int(point.get(key) or 0) for key in ("grok", "muse", "glm", "claude"))
        try:
            moment = float(point.get("t", 0))
        except (TypeError, ValueError):
            continue
        if total <= 1 and ready_at(moment):
            idle += 1
    return idle / len(kept)
