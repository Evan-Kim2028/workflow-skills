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
STALE_SOURCE_S = 900.0


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


def build_state(store, registry, cfg=None, now: float | None = None, processes=None) -> dict:
    """Assemble the /api/state payload. ``cfg`` feeds the health block
    (``scratch_roots``, ``run_dirs``, ``warnings``); pricing and bundles
    resolve from the store and the workstreams root. ``processes`` is the
    live worker listing for the ``unseen worker`` mismatch check."""
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

    workstreams = []
    for row in roll["rows"]:
        ws = row["ws"]
        progress = _row_progress(resolve_plan, ws) if resolve_plan is not None else None
        workstreams.append(
            {
                "id": ws,
                "title": titles.get(ws) or bundles.get(ws, {}).get("title", "") or "",
                "plan": ws in plan_ids,
                "progress": progress,
                "live_agents": row["live_agents"],
                "tokens": row["tokens"],
                "cost_usd": row["cost_usd"],
                "tokens_all_time": row["tokens_all_time"],
                "cost_usd_all_time": row["cost_usd_all_time"],
                "last_activity": row["last_activity"],
            }
        )
    unassigned = roll["unassigned"]
    totals = roll["all"]["totals"]

    problems = _problems(health, mismatches, view.get("stale") or [], store, bundles)

    by_source = dict(view.get("running_by_source") or {})
    return {
        "schema": SCHEMA_VERSION,
        "version": {"tool": tool_version(), "schema": SCHEMA_VERSION},
        "now": now,
        "window": {"since": now - WINDOW_S, "until": now, "hours": WINDOW_S / 3600},
        "health": health,
        "workstreams": workstreams,
        "unassigned": {
            "live_agents": unassigned["live_agents"],
            "tokens": unassigned["tokens"],
            "cost_usd": unassigned["cost_usd"],
            "tokens_all_time": unassigned["tokens_all_time"],
            "cost_usd_all_time": unassigned["cost_usd_all_time"],
            "last_activity": unassigned["last_activity"],
        },
        "running": {"count": sum(by_source.values()), "by_source": by_source},
        "totals": totals,
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


def _problems(health: dict, mismatches: list, stale: list, store, bundles: dict) -> list:
    problems = []
    for source in (health or {}).get("sources") or []:
        name = source.get("name")
        error = source.get("last_error")
        if error:
            problems.append(
                {
                    "code": "source-error",
                    "message": f"source {name!r} last poll failed: {error}",
                    "command": "python3 agent_dash.py status",
                }
            )
            continue
        last = source.get("last_poll")
        lag = source.get("lag_s")
        if last is None or (isinstance(lag, (int, float)) and lag > STALE_SOURCE_S):
            problems.append(
                {
                    "code": "source-stale",
                    "message": f"source {name!r} has no recent poll (last_poll={last})",
                    "command": "python3 agent_dash.py status",
                }
            )
    for item in mismatches:
        if item.startswith("stale running: "):
            continue
        problems.append(
            {
                "code": "reconcile-mismatch",
                "message": item,
                "command": "python3 agent_dash.py check",
            }
        )
    for row in stale:
        label = row.get("label")
        source = row.get("source")
        if source == "generic" and label:
            command = f"python3 agent_dash.py report finish --label {label} --verdict PASS --status done"
        else:
            command = "python3 agent_dash.py check"
        problems.append(
            {
                "code": "stale-running",
                "message": f"stale running: {row.get('run_id')} ({row.get('reason')})",
                "command": command,
            }
        )
    problems.extend(_price_problems(store))
    problems.extend(_bundle_problems(bundles))
    return problems


def _price_problems(store) -> list:
    """Models with usage rows but no input/output rate stay unpriced."""
    try:
        stats = store.usage_model_stats()
    except Exception:
        return []
    try:
        from dash.pricing import catalog as load_catalog
    except Exception:
        return []
    try:
        rows = {row["model"]: row for row in load_catalog()}
    except Exception:
        return []
    problems = []
    for stat in stats:
        model = stat.get("model")
        if not model or not stat.get("rows"):
            continue
        row = rows.get(model)
        if row is None or row.get("input") is None or row.get("output") is None:
            problems.append(
                {
                    "code": "unpriced-model",
                    "message": (
                        f"model {model!r} has {stat.get('rows')} usage rows with no price "
                        "(cost counts unpriced runs, never $0)"
                    ),
                    "command": (
                        "add a row to tools/agent-dash/catalog/prices.json, "
                        "then run: python3 agent_dash.py reprice"
                    ),
                }
            )
    return problems


def _bundle_problems(bundles: dict) -> list:
    try:
        from dash.bundles import validate_bundle
    except Exception:
        return []
    problems = []
    for ws in bundles:
        try:
            issues = validate_bundle(ws)
        except Exception:
            continue
        if issues:
            first = issues[0]
            extra = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            problems.append(
                {
                    "code": "invalid-bundle",
                    "message": f"bundle {ws} fails validation: {first}{extra}",
                    "command": f"python3 agent_dash.py ws validate {ws}",
                }
            )
    return problems
