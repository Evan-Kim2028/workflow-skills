"""Cross-workstream dependency graph: pure builder for GET /api/project/graph.

Reads only local bundles (read-only): workstream rows (id, title, state,
progress, live agents, milestone), ``depends_on`` edges, gate tasks whose
signal or condition text names another workstream, and deferred tasks that
moved to another workstream. Every function here is pure over plain data
so both the server and the unit tests can call it without touching the
store. Freshness follows ``dash.orchestrate.stale_gates``; the moved-task
receiving end follows ``dash.orchestrate.deferred_ledger``.

Nodes are workstreams. Edges point from the prerequisite to the waiter:

- kind ``dependency``: ``bundle.json`` ``depends_on``. Met when the source
  workstream is ``done`` or ``archived``, else waiting.
- kind ``gate``: a gate task in the waiter's plan whose signal, title, or
  ``dep_notes`` text names the source workstream id. Stale per the gate
  freshness rule; met when the source is done/archived, the gate task is
  done-like, or the gate is fresh and confirmed; else waiting.
- kind ``moved``: a deferred task with ``moved_to`` naming the target
  workstream. Met when the receiving end picked it up (``received_as`` or
  a title match, per the deferred ledger), else waiting.

Cycles are reported in the payload (``cycles``), never an error. Depth is
the longest chain from a root; cycle back-edges are skipped
deterministically so layering always terminates.

Privacy: only workstream ids, titles, states, counts, and plan condition
texts cross this boundary — the same prose ``/api/plan`` already serves.
Never paths, report bodies, prompts, or secrets.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

#: Workstream ids look like WS7. Gate texts name them in any case.
_WS_RE = re.compile(r"\bws\s?(\d+)\b", re.IGNORECASE)

#: A source in one of these states no longer blocks its waiters.
MET_SOURCE_STATES = ("done", "archived")

#: A gate task in one of these statuses counts its signal as met.
MET_GATE_STATUSES = ("done", "prog")


def _norm_ws(text: str) -> str:
    """Upper-cased ``WS<n>`` id for a bare number match."""
    return "WS%d" % int(text)


def workstream_ids_in_text(text) -> list[str]:
    """Workstream ids named in free text, upper-cased and de-duplicated.

    Matches ``WS7`` with or without a space, in any case. Pure on the
    string; callers drop unknown or self ids.
    """
    if not isinstance(text, str) or not text:
        return []
    seen: list[str] = []
    for match in _WS_RE.finditer(text):
        ident = _norm_ws(match.group(1))
        if ident not in seen:
            seen.append(ident)
    return seen


def _gate_search_text(task: dict) -> str:
    """The prose a gate task waits on: signal, title, dep_notes values."""
    parts = []
    for key in ("signal", "title", "note"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    notes = task.get("dep_notes")
    if isinstance(notes, dict):
        for value in notes.values():
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return " ".join(parts)


def _gate_condition(task: dict) -> str | None:
    """The condition text for a gate edge: signal first, then dep_notes."""
    signal = task.get("signal")
    if isinstance(signal, str) and signal.strip():
        return signal.strip()
    notes = task.get("dep_notes")
    if isinstance(notes, dict):
        texts = [value.strip() for value in notes.values() if isinstance(value, str) and value.strip()]
        if texts:
            return "; ".join(texts)
    title = task.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _confirmed_at(task: dict) -> float | None:
    """Epoch seconds for a gate's ISO-8601 confirmation, else None."""
    value = task.get("confirmed_at")
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


def _plan_groups(body) -> list:
    """Task groups of either plan shape (API body or raw plan.json)."""
    if not isinstance(body, dict):
        return []
    groups = body.get("groups")
    if isinstance(groups, list):
        return groups
    dag = body.get("dag")
    if isinstance(dag, dict) and isinstance(dag.get("groups"), list):
        return dag["groups"]
    return []


def _plan_tasks(body) -> dict:
    """Task id to task dict for one plan body."""
    by_id: dict[str, dict] = {}
    for group in _plan_groups(body):
        tasks = group.get("tasks") if isinstance(group, dict) else None
        for task in tasks or []:
            if isinstance(task, dict) and task.get("id") not in (None, "") and str(task["id"]) not in by_id:
                by_id[str(task["id"])] = task
    return by_id


def build_graph(
    bundles: list[dict],
    plans: dict[str, dict],
    live: dict[str, int] | None = None,
    progress: dict[str, dict] | None = None,
    now: float | None = None,
) -> dict:
    """Build the ``/api/project/graph`` payload from plain data.

    ``bundles`` holds one dict per workstream with ``id`` and optional
    ``title``, ``state``, ``depends_on`` (list of ids), and ``milestone``.
    ``plans`` maps workstream id to its plan body (API or raw shape; a
    missing id means no plan). ``live`` maps id to live-agent count and
    ``progress`` maps id to its ``{done, total, pct}`` object (or None).
    ``now`` is epoch seconds for gate freshness (defaults to time.time).
    """
    from dash.orchestrate import deferred_ledger, stale_gates

    moment = time.time() if now is None else now
    live = live or {}
    progress = progress or {}

    order = sorted({str(item["id"]) for item in bundles if isinstance(item, dict) and item.get("id")})
    by_bundle = {str(item["id"]): item for item in bundles if isinstance(item, dict) and item.get("id")}
    states = {ws: _state_of(by_bundle[ws]) for ws in order}

    stale_by_ws: dict[str, dict[str, bool]] = {}
    for ws in order:
        body = plans.get(ws)
        if body is None:
            continue
        try:
            stale_by_ws[ws] = {row["id"]: bool(row["stale"]) for row in stale_gates(body, moment)}
        except Exception:
            stale_by_ws[ws] = {}

    ledger = []
    try:
        ledger = deferred_ledger({ws: plans[ws] for ws in order if plans.get(ws) is not None})
    except Exception:
        ledger = []
    received = {(row["ws"], row["id"]): not bool(row.get("orphan")) for row in ledger if isinstance(row, dict)}

    edges: list[dict] = []
    seen: set[tuple] = set()

    def add_edge(source: str, target: str, kind: str, ref: str, status: str, condition: str | None, extra: dict | None = None) -> None:
        key = (source, target, kind, ref)
        if key in seen:
            return
        seen.add(key)
        row: dict = {
            "from": source,
            "to": target,
            "kind": kind,
            "status": status,
            "condition": condition,
        }
        if extra:
            row.update(extra)
        edges.append(row)

    for target in order:
        bundle = by_bundle[target]
        depends = bundle.get("depends_on")
        wants = [item for item in depends if isinstance(item, str)] if isinstance(depends, list) else []
        for raw in wants:
            source = raw.upper()
            if source == target or source not in by_bundle:
                continue
            status = "met" if states[source] in MET_SOURCE_STATES else "waiting"
            add_edge(source, target, "dependency", source, status, "%s waits on %s" % (target, source))

    for target in order:
        tasks = _plan_tasks(plans.get(target))
        for tid in sorted(tasks):
            task = tasks[tid]
            if task.get("kind") != "gate":
                continue
            for source in workstream_ids_in_text(_gate_search_text(task)):
                if source == target or source not in by_bundle:
                    continue
                stale = bool(stale_by_ws.get(target, {}).get(tid, False))
                gate_done = task.get("status") in MET_GATE_STATUSES
                confirmed = _confirmed_at(task) is not None
                if stale:
                    status = "stale"
                elif states[source] in MET_SOURCE_STATES or gate_done or (confirmed and not stale):
                    status = "met"
                else:
                    status = "waiting"
                extra = {"gate": tid}
                signal = task.get("signal")
                if isinstance(signal, str) and signal.strip():
                    extra["signal"] = signal.strip()
                if isinstance(task.get("confirmed_at"), str):
                    extra["confirmed_at"] = task["confirmed_at"]
                if isinstance(task.get("fresh_hours"), (int, float)) and not isinstance(task.get("fresh_hours"), bool):
                    extra["fresh_hours"] = task["fresh_hours"]
                add_edge(source, target, "gate", tid, status, _gate_condition(task), extra)

    for source in order:
        tasks = _plan_tasks(plans.get(source))
        for tid in sorted(tasks):
            task = tasks[tid]
            if task.get("status") != "deferred":
                continue
            moved = task.get("moved_to")
            if not isinstance(moved, str):
                continue
            target = moved.upper()
            if target == source or target not in by_bundle:
                continue
            note = task.get("note")
            condition = note.strip() if isinstance(note, str) and note.strip() else "deferred task %s moved to %s" % (tid, target)
            status = "met" if received.get((source, tid), False) else "waiting"
            extra = {"task": tid}
            if isinstance(task.get("received_as"), str) and task["received_as"].strip():
                extra["received_as"] = task["received_as"].strip()
            add_edge(source, target, "moved", tid, status, condition, extra)

    edges.sort(key=lambda row: (row["from"], row["to"], row["kind"], str(row.get("gate") or row.get("task") or "")))
    cycles = find_cycles(order, edges)
    depths = compute_depths(order, edges)

    nodes = []
    for ws in order:
        bundle = by_bundle[ws]
        title = bundle.get("title")
        prog = progress.get(ws)
        nodes.append(
            {
                "id": ws,
                "title": title if isinstance(title, str) and title else ws,
                "state": states[ws],
                "progress": dict(prog) if isinstance(prog, dict) else None,
                "live_agents": int(live.get(ws, 0) or 0),
                "milestone": bundle.get("milestone") if isinstance(bundle.get("milestone"), str) else None,
                "depth": depths.get(ws, 0),
            }
        )
    return {"nodes": nodes, "edges": edges, "cycles": cycles, "generated_at": moment}


def _state_of(bundle: dict) -> str:
    """Lifecycle state with the contract default (absent reads active)."""
    state = bundle.get("state")
    if state in ("planned", "active", "paused", "done", "archived", "dropped"):
        return state
    return "active"


def adjacency(ids: list[str], edges: list[dict]) -> dict[str, list[str]]:
    """Sorted successor list per id over pair-deduped edges."""
    out: dict[str, list[str]] = {ws: [] for ws in ids}
    for edge in edges:
        source, target = edge.get("from"), edge.get("to")
        if source in out and target in out and target not in out[source]:
            out[source].append(target)
    for ws in out:
        out[ws].sort()
    return out


def find_cycles(ids: list[str], edges: list[dict]) -> list[list[str]]:
    """Every directed cycle, each starting at its smallest id.

    Deterministic depth-first search over sorted ids and successors; a
    back-edge closes one cycle. Reported in the payload, never an error.
    """
    graph = adjacency(ids, edges)
    cycles: list[list[str]] = []
    seen: set[tuple] = set()
    visiting: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        visiting.append(node)
        on_path.add(node)
        for succ in graph.get(node, []):
            if succ in on_path:
                start = visiting.index(succ)
                cycle = visiting[start:]
                key = tuple(sorted(cycle))
                if key not in seen:
                    seen.add(key)
                    leader = min(cycle)
                    pivot = cycle.index(leader)
                    cycles.append(cycle[pivot:] + cycle[:pivot])
            elif succ not in done:
                visit(succ)
        visiting.pop()
        on_path.discard(node)
        done.add(node)

    for ws in sorted(ids):
        if ws not in done:
            visit(ws)
    cycles.sort()
    return cycles


def compute_depths(ids: list[str], edges: list[dict]) -> dict[str, int]:
    """Longest-chain depth from a root (no incoming edge) per id.

    A root sits at 0; every other node sits one past its deepest
    prerequisite. Cycle back-edges are skipped deterministically
    (sorted order), so layering always terminates and stays stable
    across requests.
    """
    preds: dict[str, list[str]] = {ws: [] for ws in ids}
    for edge in edges:
        source, target = edge.get("from"), edge.get("to")
        if source in preds and target in preds and source not in preds[target]:
            preds[target].append(source)
    for ws in preds:
        preds[ws].sort()
    depth: dict[str, int] = {}
    on_path: set[str] = set()

    def visit(node: str) -> int:
        if node in depth:
            return depth[node]
        if node in on_path:
            return 0
        on_path.add(node)
        best = -1
        for pred in preds.get(node, []):
            if pred in on_path:
                continue
            best = max(best, visit(pred))
        on_path.discard(node)
        depth[node] = best + 1
        return best + 1

    for ws in sorted(ids):
        visit(ws)
    return {ws: depth.get(ws, 0) for ws in ids}


def structure_key(mtimes: dict[str, tuple]) -> tuple:
    """Hashable cache key for the graph structure: sorted id/mtime pairs."""
    return tuple(sorted((ws, times) for ws, times in mtimes.items()))
