"""GET /api/state: the whole dashboard in one call, for agents.

One store pass (``Store.state_view``) yields the rollup rows, the
reconcile mismatches, the running count by source, and the stale rows.
Titles, plan progress, and price coverage ride along from files outside
the lock. The schema is documented in CONTRACT.md: fields are only
added, never renamed.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from dash.store import RANGE_S

TOOL_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = 1
WINDOW_S = RANGE_S

#: Seconds a state payload may serve from cache with zero store writes.
#: Bounds time-derived staleness (stale-running boundaries, lag text) on a
#: quiet dashboard; any store write invalidates immediately via the seq.
STATE_CACHE_TTL_S = 60

_tool_version_cached: str | None = None


def tool_version(root: Path | None = None) -> str:
    """Git short sha of the tool directory, or ``"unknown"`` outside git.

    Resolved once per process: ``/api/state`` used to spawn a git child
    on every request (~13 ms) for a value that cannot change under a
    running server.
    """
    global _tool_version_cached
    if root is None and _tool_version_cached is not None:
        return _tool_version_cached
    root = Path(root) if root is not None else TOOL_ROOT
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    sha = (proc.stdout or "").strip()
    value = sha if proc.returncode == 0 and sha else "unknown"
    if root == TOOL_ROOT:
        _tool_version_cached = value
    return value


#: Cached ``/api/state`` bodies: ``(key, body)`` under ``_STATE_LOCK``.
#: Key is ``(seq, data_seq, workers, files, quantum)``; ``body`` holds the
#: whole payload except the per-request ``now``/``window``/``health``.
_STATE_CACHE: dict = {}
_STATE_LOCK = None


def _state_lock():
    global _STATE_LOCK
    if _STATE_LOCK is None:
        import threading

        _STATE_LOCK = threading.Lock()
    return _STATE_LOCK


def _stamp(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _actions_flag(cfg) -> bool:
    """Whether page actions are enabled: part of the state cache key so
    flipping the read-only switch never serves a stale payload."""
    try:
        from dash.actions import actions_enabled
        return bool(actions_enabled(cfg))
    except Exception:
        return False


def _files_fingerprint(cfg=None) -> tuple:
    """``(mtime_ns, size)`` over every file ``build_state`` reads.

    Bundle ``plan.json``/``bundle.json``/``approvals.json``/
    ``START-PROMPT.md`` per bundle, the plans-map file and its targets
    (the ``resolve_plan`` fallback), and the hub decisions/risks files.
    Store rows are covered by the seq, not here. Dozens of stats, ~1 ms:
    far cheaper than re-reading and re-validating a dozen plans.
    """
    stamps = []
    try:
        from dash.bundles import workstreams_root

        base = workstreams_root()
        try:
            children = sorted(path for path in base.iterdir() if path.is_dir() and not path.name.startswith("."))
        except OSError:
            children = []
        for child in children:
            if not (child / "bundle.json").is_file():
                continue
            for name in ("plan.json", "bundle.json", "approvals.json", "START-PROMPT.md"):
                stamps.append(_stamp(child / name))
    except Exception:
        pass
    try:
        from dash.bundles import plans_map_stamps

        stamps.append(plans_map_stamps())
    except Exception:
        pass
    try:
        from dash.decisions import resolve_hub_dir

        hub = resolve_hub_dir(cfg)
        if hub is not None:
            for name in ("decisions.tsv", "risks.tsv", "risks.json"):
                stamps.append(_stamp(Path(hub) / name))
    except Exception:
        pass
    return tuple(stamps)


def state_identity(store, cfg=None, processes=None, now: float | None = None) -> str | None:
    """Identity ETag for the state body: the cache key hashed, or None.

    ``/api/state`` 304s on this without recomputing when nothing changed
    (same store seqs, worker listing, files, and doctor run). Volatile
    fields (``now``, ``window``, ``health`` lag) stay out of the tag:
    they refresh per request and readers tolerate seconds of staleness.
    """
    import hashlib

    now = float(now if now is not None else time.time())
    try:
        doctor = store.doctor_latest(now)
    except Exception:
        doctor = None
    generated = doctor.get("generated_at") if isinstance(doctor, dict) else None
    key = _state_key(store, processes, cfg, now, generated)
    if key is None:
        return None
    return '"id-%s"' % hashlib.sha256(repr(("state", key)).encode("utf-8")).hexdigest()[:32]


def _workers_key(processes) -> tuple:
    """Hashable identity of the live-worker listing for the cache key.

    ``None`` (no listing) and ``[]`` (no workers) must key differently:
    the unseen-worker check treats them the same today, but the key
    should not merge inputs that differ.
    """
    if processes is None:
        return ("none",)
    try:
        return tuple(sorted(
            (str(item.get("label")), int(item.get("pid") or 0), float(item.get("started_at") or 0.0))
            for item in processes
            if isinstance(item, dict)
        ))
    except (TypeError, ValueError):
        return ("unhashable",)


def _state_key(store, processes, cfg, now: float, doctor_generated) -> tuple | None:
    """Cache key for the state body, or None when caching is unsafe.

    ``:memory:`` stores share one path across distinct databases, so they
    always recompute. File-backed stores key on their path plus the store
    sequences (every store write bumps them), the worker listing identity
    (the unseen-worker check reads it), every file the body reads, and a
    60 s quantum bounding time-derived drift on a quiet dashboard.
    """
    try:
        path = str(store.path)
    except Exception:
        return None
    if not path or path == ":memory:":
        return None
    try:
        seq = store.seq
        data_seq = store.data_seq
    except Exception:
        return None
    return (
        path,
        seq,
        data_seq,
        _workers_key(processes),
        _files_fingerprint(cfg),
        _actions_flag(cfg),
        doctor_generated,
        int(now // STATE_CACHE_TTL_S),
    )


def build_state(store, registry, cfg=None, now: float | None = None, processes=None, build=None) -> dict:
    """Assemble the /api/state payload. ``cfg`` feeds the health block
    (``scratch_roots``, ``run_dirs``, ``warnings``); pricing and bundles
    resolve from the store and the workstreams root. ``processes`` is the
    live worker listing for the ``unseen worker`` mismatch check.
    ``build`` is the server's captured ``{"build_id", "started_at"}``
    (computed fresh when omitted).

    The body (everything but ``now``/``window``/``health``) serves from a
    one-entry cache keyed by :func:`_state_key`: an unchanged dashboard
    rebuilds nothing, so idle ticks cost a fingerprint (~1 ms) plus a
    fresh health block. ``now``, ``window``, and ``health`` are always
    fresh; windowed sums stay frozen at compute time between writes, the
    same staleness the snapshot cache already promises.
    """
    now = float(now if now is not None else time.time())
    try:
        from dash.server import _health_with_live as _health_with_live_fn

        health = _health_with_live_fn(registry, now, cfg, processes)
    except Exception:
        try:
            health = registry.health(now, cfg)
        except TypeError:
            health = registry.health(now)
    try:
        doctor = store.doctor_latest(now)
    except Exception:
        doctor = None
    generated = doctor.get("generated_at") if isinstance(doctor, dict) else None
    key = _state_key(store, processes, cfg, now, generated)
    body = None
    if key is not None:
        with _state_lock():
            hit = _STATE_CACHE.get("entry")
            if hit is not None and hit[0] == key:
                body = hit[1]
    if body is None:
        body = _build_state_body(store, cfg, now, processes, doctor, build)
        if key is not None:
            with _state_lock():
                _STATE_CACHE["entry"] = (key, body)
    out = dict(body)
    out["now"] = now
    out["window"] = {"since": now - WINDOW_S, "until": now, "hours": WINDOW_S / 3600}
    out["health"] = health
    return out


def _build_state_body(store, cfg, now: float, processes, doctor, build) -> dict:
    """The cacheable state body: everything but ``now``/``window``/``health``."""
    view = store.state_view(now, processes=processes)
    roll = view["roll"]
    mismatches = view["mismatches"]

    titles = dict(view.get("boards") or {})
    try:
        from dash.bundles import list_bundles, resolve_plan
        from dash.plans import plan_progress  # noqa: F401  (progress rides on resolve_plan)
    except Exception:
        list_bundles = None
        resolve_plan = None
    bundles: dict = {}
    if list_bundles is not None:
        try:
            for item in list_bundles() or []:
                bundles[item["id"]] = item
                titles.setdefault(item["id"], item.get("title") or "")
        except Exception:
            pass
    plan_ids: set[str] = set()
    if resolve_plan is not None:
        try:
            from dash.bundles import workstream_plan_ids

            plan_ids = set(workstream_plan_ids())
        except Exception:
            plan_ids = set()

    try:
        from dash.workstates import all_bundle_meta, archived_aggregate, bundle_fallback_rows, hidden_ids, state_of
    except Exception:
        all_bundle_meta = None
        archived_aggregate = None
        bundle_fallback_rows = None
        hidden_ids = None
        state_of = None
    try:
        from dash.actions import actions_enabled
    except Exception:
        actions_enabled = None
    meta = {}
    if all_bundle_meta is not None:
        try:
            meta = all_bundle_meta() or {}
        except Exception:
            meta = {}

    workstreams = []
    for row in roll["rows"]:
        ws = row["ws"]
        progress = _row_progress(resolve_plan, ws) if resolve_plan is not None else None
        workstreams.append(
            {
                "id": ws,
                "title": titles.get(ws) or bundles.get(ws, {}).get("title", "") or "",
                "state": state_of(ws, meta) if state_of is not None else "active",
                "plan": ws in plan_ids,
                "progress": progress,
                "live_agents": row["live_agents"],
                "tokens": row["tokens"],
                "cost_usd": row["cost_usd"],
                "prev_24h": dict(row.get("prev_24h") or {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0}),
                "tokens_all_time": row["tokens_all_time"],
                "cost_usd_all_time": row["cost_usd_all_time"],
                "last_activity": row["last_activity"],
            }
        )
    if bundle_fallback_rows is not None:
        try:
            have = {row.get("id") for row in workstreams}
            have |= {row.get("ws") for row in roll["rows"]}
            for extra in bundle_fallback_rows(have, plan_ids=plan_ids) or []:
                ws = extra.get("ws")
                progress = _row_progress(resolve_plan, ws) if resolve_plan is not None else None
                workstreams.append(
                    {
                        "id": ws,
                        "title": titles.get(ws) or bundles.get(ws, {}).get("title", "") or extra.get("name", "") or "",
                        "state": extra.get("state", "active"),
                        "plan": ws in plan_ids,
                        "progress": progress,
                        "live_agents": 0,
                        "tokens": 0,
                        "cost_usd": 0.0,
                        "prev_24h": {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0},
                        "tokens_all_time": 0,
                        "cost_usd_all_time": 0.0,
                        "last_activity": None,
                    }
                )
        except Exception:
            pass
    unassigned = roll["unassigned"]
    totals = roll["all"]["totals"]
    totals_prev = dict(roll["all"].get("prev_24h") or {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0})

    problems = _problems_from_payload(doctor)

    decisions = {"total": 0, "open": 0}
    risks = {"total": 0, "by_severity": {}}
    try:
        from dash.decisions import decisions_summary, resolve_hub_dir, risks_summary

        decisions = decisions_summary(resolve_hub_dir(cfg))
        risks = risks_summary(store, cfg, now)
    except Exception:
        pass

    by_source = dict(view.get("running_by_source") or {})
    prod_jobs = int(view.get("prod_jobs") or 0)
    prod_jobs_finished = int(view.get("prod_jobs_finished") or 0)
    info = dict(build) if isinstance(build, dict) else _default_build_info()
    archived = (
        archived_aggregate(roll["rows"], hidden_ids(meta))
        if archived_aggregate is not None and hidden_ids is not None
        else {"tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "workstreams": []}
    )
    return {
        "schema": SCHEMA_VERSION,
        "version": {
            "tool": tool_version(),
            "build_id": info.get("build_id", "unknown"),
            "started_at": info.get("started_at"),
            "schema": SCHEMA_VERSION,
        },
        # Project identity placeholder: the project worker (GET
        # /api/project) fills this block from project.json. Reads use
        # payload.get("project", {}).get("name") so both shapes work.
        "project": {"name": None},
        # The one write action the page may offer. False in read-only
        # mode, when the Archive buttons fall back to copy-command.
        "actions": {"archive": bool(actions_enabled is not None and actions_enabled(cfg))},
        "decisions": decisions,
        "risks": risks,
        "workstreams": workstreams,
        "unassigned": {
            "live_agents": unassigned["live_agents"],
            "tokens": unassigned["tokens"],
            "cost_usd": unassigned["cost_usd"],
            "prev_24h": dict(unassigned.get("prev_24h") or {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0}),
            "tokens_all_time": unassigned["tokens_all_time"],
            "cost_usd_all_time": unassigned["cost_usd_all_time"],
            "last_activity": unassigned["last_activity"],
        },
        "running": {"count": sum(by_source.values()), "by_source": by_source, "prod_jobs": prod_jobs, "prod_jobs_finished": prod_jobs_finished},
        "totals": {**totals, "prev_24h": totals_prev, "prod_jobs": prod_jobs, "prod_jobs_finished": prod_jobs_finished, "archived": archived},
        "totals_all_time": roll["all_time"]["all"],
        "reconcile": {"ok": not mismatches, "mismatches": mismatches},
        "problems": problems,
    }


def _row_progress(resolve_plan, ws: str):
    """The same ``progress`` object ``/api/plan`` carries, or None."""
    try:
        _source, body = resolve_plan(ws)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    progress = body.get("progress")
    return dict(progress) if isinstance(progress, dict) else None


def _doctor_problems(store, now: float) -> list:
    """The persisted doctor run: the same list (same ids) as ``GET /api/doctor``.

    Never runs checks inline; the background doctor owns the data. Each
    entry keeps the legacy ``message`` and ``command`` keys so older
    readers keep working alongside the new ``id`` shape.
    """
    try:
        payload = store.doctor_latest(now)
    except Exception:
        return []
    return _problems_from_payload(payload)


def _problems_from_payload(payload) -> list:
    """Problem entries from an already-fetched doctor payload."""
    if not isinstance(payload, dict):
        return []
    out = []
    for item in payload.get("problems") or []:
        if not isinstance(item, dict):
            continue
        fix = item.get("fix") or {}
        entry = dict(item)
        entry["message"] = f"{item.get('title')}: {item.get('detail')}"
        entry["command"] = fix.get("command") or ""
        out.append(entry)
    return out


_cached_build_id: str | None = None


def _default_build_info() -> dict:
    """Fallback ``{"build_id", "started_at"}`` without spawning git twice.

    ``build_id`` runs ``rev-parse`` plus ``status --porcelain`` (~25 ms);
    the id cannot change under a running server, so it resolves once per
    process. ``started_at`` stays fresh: it names this response, not the
    process.
    """
    global _cached_build_id
    if _cached_build_id is None:
        try:
            from dash.buildinfo import build_id as _build_id

            _cached_build_id = _build_id()
        except Exception:
            _cached_build_id = "unknown"
    return {"build_id": _cached_build_id, "started_at": time.time()}



