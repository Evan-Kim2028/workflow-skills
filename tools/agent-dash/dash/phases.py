"""Planning-versus-execution classifier: which kind of work a run belongs to.

Kinds of work are ``planning``, ``execution``, ``review``,
``coordination`` and ``unclassified``. Rules are table-driven and live
in ``catalog/phases.json`` (shipped; the file name is historical) with
an optional owner override file ``phases.local.json`` beside
``project.json`` whose entries run first; the first matching rule wins
for a run. Only labels are ever read, never prompts or argv.

Rule types, in file order:

- ``convention``: the explicit mark wins, e.g. ``ws9-plan-xxx``,
  ``ws9-exec-xxx``, ``ws9-review-xxx``, ``ws9-coord-xxx``.
- ``keyword``: regexes over the label (case-insensitive). The shipped
  file orders them review affixes (``r-`` leads, ``-rev``/``-fix``
  tails), then task mapping, then planning, review, execution and
  coordination words.
- ``task``: a run mapped to a plan task id is execution. Resolution is
  :func:`dash.taskmatch.task_of` (exact taskmap keys, ``/regex/``
  taskmap keys, the WS1/WS2/WS7 page matchers, plan-id substrings)
  plus a dash/dot-insensitive fallback so ``ws9-s-tbench`` names plan
  task ``WS9.s-tbench``. A hit outside the workstream's plan is not a
  task run.
- ``lead``: an unmapped lead session (no parent run, a lead source
  such as ``claude-main`` or the top-level Codex TUI surface ``codex``)
  is the planning surface.
- ``window``: still unclassified and started before the workstream's
  first task-mapped run is early exploration: planning.

``unclassified`` is a real bucket (grey on the page), never silently
merged into another kind of work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalog" / "phases.json"

#: Every phase the classifier can return, in legend order.
PHASES = ("planning", "execution", "review", "coordination", "unclassified")

#: Rule id used when nothing matched.
NO_RULE = "none"

_lock = threading.Lock()
_cache: dict = {}
_plan_tasks_cache: dict = {}


def _norm(text) -> str:
    """Lowercase alphanumeric skeleton: ``ws9-s-tbench`` ~ ``WS9.s-tbench``."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _compile(pattern: str):
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def _minimal_rules() -> list[dict]:
    """Convention-only fallback when the catalog file is unreadable."""
    return [
        {
            "id": "convention",
            "type": "convention",
            "pattern": r"(?:ws\d+|hub)[-_. ]?(plan(?:ning)?|exec(?:ution)?|review|rev|coord(?:ination)?)(?![a-z0-9])",
            "map": {
                "plan": "planning",
                "planning": "planning",
                "exec": "execution",
                "execution": "execution",
                "review": "review",
                "rev": "review",
                "coord": "coordination",
                "coordination": "coordination",
            },
        }
    ]


def _read_json(path: Path | None):
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _rule_entries(data: dict | None) -> tuple[list[dict], list[str], object]:
    """``(rules, disabled_ids, version)`` from a parsed rules file."""
    if not isinstance(data, dict):
        return [], [], 1
    rules = data.get("rules")
    if not isinstance(rules, list):
        rules = []
    disabled = data.get("disabled")
    if not isinstance(disabled, list):
        disabled = []
    version = data.get("version", 1)
    return (
        [entry for entry in rules if isinstance(entry, dict)],
        [str(item) for item in disabled if isinstance(item, str)],
        version,
    )


def merge_rules(catalog: dict | None, local: dict | None) -> tuple[list[dict], object]:
    """Merged rule list: local entries first, shadowing by rule id.

    A local file holds ``{"rules": [...], "disabled": [ids]}``. Its
    rules run before the catalog's; a catalog rule whose ``id`` a
    local rule reuses is replaced, and ``disabled`` ids drop from both.
    Returns ``(rules, version)`` where version is the local version
    when present, else the catalog's.
    """
    cat_rules, cat_disabled, cat_version = _rule_entries(catalog)
    loc_rules, loc_disabled, loc_version = _rule_entries(local)
    dead = set(cat_disabled) | set(loc_disabled)
    local_ids = {str(entry.get("id")) for entry in loc_rules if entry.get("id") is not None}
    merged = [entry for entry in loc_rules if str(entry.get("id")) not in dead]
    merged.extend(
        entry
        for entry in cat_rules
        if str(entry.get("id")) not in dead and str(entry.get("id")) not in local_ids
    )
    return merged, (loc_version if local is not None else cat_version)


def rules_hash(rules: list[dict]) -> str:
    """Content hash of the merged rules: part of every phases cache key."""
    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _prepare(rules: list[dict]) -> list[dict]:
    """Compiled rule list: regexes compiled once, bad patterns dropped."""
    out = []
    for entry in rules:
        kind = entry.get("type")
        item = dict(entry)
        if kind == "convention":
            item["_rx"] = _compile(str(entry.get("pattern") or ""))
            mapping = entry.get("map") or {}
            item["_map"] = {
                str(key).lower(): value
                for key, value in mapping.items()
                if isinstance(key, str) and value in PHASES
            }
            if item["_rx"] is None or not item["_map"]:
                continue
        elif kind == "keyword":
            compiled = []
            for pattern in entry.get("patterns") or []:
                if not isinstance(pattern, str):
                    continue
                rx = _compile(pattern)
                if rx is not None:
                    compiled.append(rx)
            if not compiled or entry.get("phase") not in PHASES:
                continue
            item["_rx_list"] = compiled
        elif kind == "task":
            if entry.get("phase") not in PHASES:
                continue
        elif kind == "lead":
            sources = entry.get("sources") or []
            item["_sources"] = {str(name) for name in sources if isinstance(name, str)}
            if not item["_sources"] or entry.get("phase") not in PHASES:
                continue
        elif kind == "window":
            if entry.get("phase") not in PHASES:
                continue
        else:
            continue
        out.append(item)
    return out


def catalog_path() -> Path:
    """Shipped rules file, overridable via ``AGENT_DASH_PHASES``."""
    override = os.environ.get("AGENT_DASH_PHASES")
    if isinstance(override, str) and override.strip():
        from dash.locate import expand

        return Path(expand(override.strip()))
    return CATALOG_PATH


def local_path(cfg: dict | None = None, root=None) -> Path | None:
    """Owner override file beside ``project.json`` (``phases.local.json``)."""
    try:
        from dash import dataroot

        project_file = dataroot.project_file(cfg, root)
    except Exception:
        return None
    try:
        return Path(project_file).parent / "phases.local.json"
    except Exception:
        return None


def load_rules(
    catalog: Path | None = None, local: Path | None = None
) -> tuple[list[dict], str, object]:
    """``(compiled_rules, hash, version)``, cached by file mtimes.

    Missing or unreadable files fall back gracefully: a bad local
    file is ignored, a bad catalog file leaves the convention-only
    minimal rules so classification never fails.
    """
    cat_path = Path(catalog) if catalog is not None else catalog_path()
    try:
        cat_stamp = cat_path.stat().st_mtime_ns
    except OSError:
        cat_stamp = None
    loc_stamp = None
    if local is not None:
        try:
            loc_stamp = Path(local).stat().st_mtime_ns
        except OSError:
            loc_stamp = None
    key = (str(cat_path), cat_stamp, None if local is None else str(local), loc_stamp)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit
        cat_data = _read_json(cat_path)
        loc_data = _read_json(Path(local)) if local is not None else None
        if cat_data is None:
            merged, version = _minimal_rules(), 0
        else:
            merged, version = merge_rules(cat_data, loc_data)
        compiled = _prepare(merged)
        if not compiled:
            compiled = _prepare(_minimal_rules())
            version = 0
        result = (compiled, rules_hash(merged), version)
        _cache[key] = result
        if len(_cache) > 16:
            _cache.clear()
            _cache[key] = result
        return result


def current_hash(cfg: dict | None = None, root=None) -> str:
    """Rules hash for the live file set: part of state/row cache keys."""
    try:
        _compiled, digest, _version = load_rules(local=local_path(cfg, root))
    except Exception:
        return "unavailable"
    return digest


def resolve_task(
    label: str | None,
    ws: str | None,
    taskmap: dict | None = None,
    plan_ids: list | tuple = (),
) -> str | None:
    """A label's plan task id, or None.

    :func:`dash.taskmatch.task_of` first (exact taskmap keys,
    ``/regex/`` keys, the WS1/WS2/WS7 page matchers, plan-id
    substrings), then a dash/dot-insensitive fallback so
    ``ws9-s-tbench`` names ``WS9.s-tbench``. The hit must name a task
    in the workstream's plan; without plan ids there is no task run.
    """
    ids = [str(item) for item in (plan_ids or []) if str(item)]
    if not ids:
        return None
    members, skeletons = _plan_id_index(tuple(ids))
    try:
        from dash.taskmatch import task_of
    except Exception:
        task_of = None
    if task_of is not None:
        try:
            hit = task_of(label, ws, taskmap, ids)
        except Exception:
            hit = None
        if hit is not None and str(hit) in members:
            return str(hit)
    lab = _norm(label)
    if not lab:
        return None
    best = None
    best_len = 0
    for ident, skeleton in skeletons:
        if not skeleton or skeleton not in lab:
            continue
        if best is None or len(skeleton) > best_len:
            best = ident
            best_len = len(skeleton)
    return best


_plan_id_cache: dict = {}


def _plan_id_index(ids: tuple) -> tuple:
    """``(frozenset of ids, ((id, skeleton), ...))`` cached per id tuple.

    The skeleton is :func:`_norm` of the id, so the fallback match in
    :func:`resolve_task` normalises each plan id once rather than on
    every run.
    """
    hit = _plan_id_cache.get(ids)
    if hit is not None:
        return hit
    index = (frozenset(ids), tuple((ident, _norm(ident)) for ident in ids))
    with _lock:
        if len(_plan_id_cache) >= 256:
            _plan_id_cache.clear()
        _plan_id_cache[ids] = index
    return index


def classify_run(run: dict, ctx: dict | None = None, *, rules=None) -> tuple[str, str]:
    """``(phase, rule_id)`` for one run. First matching rule wins.

    ``run`` carries ``label``, ``source``, ``parent_session_id``,
    ``started_at``. ``ctx`` carries the precomputed ``task_id`` (or
    None) and ``in_window`` (bool). ``rules`` is a compiled list from
    :func:`load_rules`; omitted, the live files are used.
    """
    ctx = ctx or {}
    if rules is None:
        try:
            rules, _digest, _version = load_rules(local=local_path())
        except Exception:
            rules = _prepare(_minimal_rules())
    label = run.get("label") if isinstance(run, dict) else None
    text = label if isinstance(label, str) else ""
    parent = run.get("parent_session_id") if isinstance(run, dict) else None
    no_parent = not (isinstance(parent, str) and parent.strip())
    source = run.get("source") if isinstance(run, dict) else None
    for entry in rules:
        kind = entry.get("type")
        rid = str(entry.get("id") or kind or "rule")
        if kind == "convention":
            match = entry["_rx"].search(text)
            if not match:
                continue
            token = _norm(match.group(1)) if match.lastindex else ""
            phase = None
            for key, value in entry["_map"].items():
                if _norm(key) == token:
                    phase = value
                    break
            if phase is None:
                continue
            return phase, rid
        if kind == "keyword":
            if any(rx.search(text) for rx in entry["_rx_list"]):
                return entry["phase"], rid
        elif kind == "task":
            if ctx.get("task_id") is not None:
                return entry["phase"], rid
        elif kind == "lead":
            if no_parent and isinstance(source, str) and source in entry["_sources"]:
                return entry["phase"], rid
        elif kind == "window":
            if ctx.get("in_window"):
                return entry["phase"], rid
    return "unclassified", NO_RULE


def classify_runs(
    runs: list[dict],
    *,
    taskmap: dict | None = None,
    plan_tasks: dict | None = None,
    rules=None,
) -> dict:
    """``{run_id: (phase, rule_id)}`` for every run with an id.

    Two passes: first resolve each run's plan task and note each
    workstream's first task-mapped start (the planning window edge),
    then classify with that context. ``plan_tasks`` maps workstream
    id to its plan task ids; omitted, every task rule misses.
    """
    if rules is None:
        try:
            rules, _digest, _version = load_rules(local=local_path())
        except Exception:
            rules = _prepare(_minimal_rules())
    if taskmap is None:
        try:
            from dash.taskmatch import load_taskmap

            taskmap = load_taskmap()
        except Exception:
            taskmap = {}
    tasks = plan_tasks or {}
    resolved: dict = {}
    first_task_start: dict = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        rid = run.get("run_id")
        if not rid:
            continue
        ws = run.get("workstream")
        ids = tasks.get(ws) if isinstance(tasks, dict) else None
        tid = resolve_task(run.get("label"), ws, taskmap, ids or ())
        resolved[rid] = tid
        if tid is not None:
            try:
                start = float(run.get("started_at")) if run.get("started_at") is not None else None
            except (TypeError, ValueError):
                start = None
            if start is not None and isinstance(ws, str) and ws:
                edge = first_task_start.get(ws)
                if edge is None or start < edge:
                    first_task_start[ws] = start
    out = {}
    for run in runs or []:
        if not isinstance(run, dict) or not run.get("run_id"):
            continue
        rid = run["run_id"]
        ws = run.get("workstream")
        edge = first_task_start.get(ws) if isinstance(ws, str) else None
        try:
            start = float(run.get("started_at")) if run.get("started_at") is not None else None
        except (TypeError, ValueError):
            start = None
        in_window = edge is not None and start is not None and start < edge
        out[rid] = classify_run(
            run, {"task_id": resolved.get(rid), "in_window": in_window}, rules=rules
        )
    return out


def plan_tasks_for(workstreams, *, root=None) -> dict:
    """``{ws: [plan task ids]}`` via the one plan resolver, cached by stamp.

    Workstreams without a resolvable plan map to ``[]`` (their task
    rule then misses, honestly). Cached on the bundle plan stamp so
    repeated state builds cost no file reads.
    """
    try:
        from dash.bundles import plans_stamp, resolve_plan
        from dash.taskmatch import plan_task_ids
    except Exception:
        return {ws: [] for ws in (workstreams or [])}
    ids = sorted({str(ws) for ws in (workstreams or []) if ws})
    try:
        stamp = plans_stamp(root)
    except Exception:
        stamp = None
    key = (str(root), tuple(ids), stamp)
    with _lock:
        hit = _plan_tasks_cache.get(key)
        if hit is not None:
            return {ws: list(tasks) for ws, tasks in hit.items()}
    out: dict = {}
    for ws in ids:
        try:
            _source, body = resolve_plan(ws)
        except Exception:
            body = None
        groups = body.get("groups") if isinstance(body, dict) else None
        try:
            out[ws] = [str(item) for item in plan_task_ids(groups)] if groups else []
        except Exception:
            out[ws] = []
    with _lock:
        _plan_tasks_cache.clear()
        _plan_tasks_cache[key] = {ws: list(tasks) for ws, tasks in out.items()}
    return out


def clear_caches() -> None:
    """Drop the rules and plan-task caches (tests only)."""
    with _lock:
        _cache.clear()
        _plan_tasks_cache.clear()
