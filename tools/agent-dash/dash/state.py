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

from dash.buildinfo import build_info
from dash.store import RANGE_S

TOOL_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = 1
WINDOW_S = RANGE_S


def tool_version(root: Path | None = None) -> str:
    """Git short sha of the tool directory, or ``"unknown"`` outside git."""
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
    return sha if proc.returncode == 0 and sha else "unknown"


def build_state(store, registry, cfg=None, now: float | None = None, processes=None, build=None) -> dict:
    """Assemble the /api/state payload. ``cfg`` feeds the health block
    (``scratch_roots``, ``run_dirs``, ``warnings``); pricing and bundles
    resolve from the store and the workstreams root. ``processes`` is the
    live worker listing for the ``unseen worker`` mismatch check.
    ``build`` is the server's captured ``{"build_id", "started_at"}``
    (computed fresh when omitted)."""
    now = float(now if now is not None else time.time())
    try:
        health = registry.health(now, cfg)
    except TypeError:
        health = registry.health(now)
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
        from dash.workstates import all_bundle_meta, archived_aggregate, hidden_ids, state_of
    except Exception:
        all_bundle_meta = None
        archived_aggregate = None
        hidden_ids = None
        state_of = None
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
    unassigned = roll["unassigned"]
    totals = roll["all"]["totals"]
    totals_prev = dict(roll["all"].get("prev_24h") or {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0})

    problems = _doctor_problems(store, now)

    by_source = dict(view.get("running_by_source") or {})
    prod_jobs = int(view.get("prod_jobs") or 0)
    prod_jobs_finished = int(view.get("prod_jobs_finished") or 0)
    info = dict(build) if isinstance(build, dict) else build_info()
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
        "now": now,
        "window": {"since": now - WINDOW_S, "until": now, "hours": WINDOW_S / 3600},
        "health": health,
        # Project identity placeholder: the project worker (GET
        # /api/project) fills this block from project.json. Reads use
        # payload.get("project", {}).get("name") so both shapes work.
        "project": {"name": None},
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



