"""Always-on doctor: scheduled checks with persisted history.

The server runs :func:`collect_problems` in a background thread every
``doctor.interval_s`` seconds (default 300) and persists the result in
the ``doctor_problems`` store table. ``GET /api/doctor`` serves the
latest persisted run; it never runs checks inline (``?refresh=1`` only
queues a run). ``doctor --fix`` applies only safe, local, reversible
fixes and otherwise just lists them; ``doctor report`` digests history
for hub reports. ``scripts/run_worker.py`` runs a fast local preflight
before launching unless ``--no-preflight`` is passed.

The doctor never signals a process and never writes to any source:
orphan detection only reports. Only the worker label (and the already
stored ``--model`` flag value) ever leaves a process listing.
"""

from __future__ import annotations

import calendar
import glob
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

TOOL_ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("agent_dash.doctor")

SEVERITIES = ("critical", "warn", "info")
SEV_RANK = {"critical": 0, "warn": 1, "info": 2}

GROUPED_CODES = frozenset({
    "dep-jump", "ghost-runs", "orphan-worker", "done-before-deps",
    "stuck-task", "bare-citation", "model-policy", "over-cap",
})
MEMBER_CAP_DEFAULT = 20
GROUP_FIX_GLOBAL = "python3 agent_dash.py doctor --all"
SEV_GLYPH = {"critical": "✖", "warn": "⚠", "info": "ℹ"}
TERMINAL_RUN = ("done", "killed", "died_early")
TERMINAL_TRAIN = ("merged", "dropped")
ORPHAN_MIN_AGE_S = 600.0
FIRST_RUN_DELAY_S = 30.0
FULL_POLL_WAIT_S = 120.0
RESOLVED_KEEP_S = 14 * 86400

KNOWN_TOP_KEYS = {
    "host",
    "port",
    "read_budget_per_file",
    "read_budget_per_tick",
    "read_budget",
    "roots",
    "hub_dir",
    "issues_dir",
    "workstreams_tsv",
    "state_dir",
    "backfill",
    "backfill_session",
    "dagster",
    "caps",
    "doctor",
}
KNOWN_ROOT_KEYS = {
    "claude_projects",
    "claude_sessions",
    "muse_sessions",
    "grok_sessions",
    "grok_log",
    "cursor_home",
    "scratchpads",
    "projects",
    "scratch",
    "scratchpad_root",
    "scratch_root",
    "claude_tmp",
}
MANIFEST_NAMES = (
    "muse-sessions.tsv",
    "glm-sessions.tsv",
    "grok-sessions.tsv",
    "cursor-sessions.tsv",
)
RUN_DIRS_NAMES = ("run-dirs.txt", "run-dirs.json", "run_dirs.json")
SOURCE_STALE_S = 900.0
MIN_PYTHON = (3, 9)


def doctor_cfg(cfg: dict | None) -> dict:
    """Effective doctor settings: interval, thresholds, model policy."""
    raw = (cfg or {}).get("doctor") if isinstance((cfg or {}).get("doctor"), dict) else {}
    policy = raw.get("model_policy") if isinstance(raw.get("model_policy"), dict) else {}
    out = {
        "interval_s": 300.0,
        "stuck_hours": 6.0,
        "approval_days": 3.0,
        "train_hours": 24.0,
        "idle_ready_minutes": 30.0,
        "long_tail_minutes": 60.0,
        "fix_rounds_cap": 2,
        "recent_hours": 24.0,
        "ghost_share": 0.2,
        "archive_nag_days": 7.0,
        "active_idle_days": 3.0,
        "phase_slip_days": 7.0,
        "model_policy": {
            "muse_reasoning_effort": policy.get("muse_reasoning_effort", "xhigh"),
            "grok_require_bracketed_500k": policy.get("grok_require_bracketed_500k", True),
        },
    }
    for key in (
        "interval_s", "stuck_hours", "approval_days", "train_hours",
        "idle_ready_minutes", "long_tail_minutes", "recent_hours",
        "archive_nag_days", "active_idle_days", "phase_slip_days",
    ):
        try:
            value = float(raw.get(key, out[key]))
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[key] = value
    try:
        cap = int(raw.get("fix_rounds_cap", out["fix_rounds_cap"]))
    except (TypeError, ValueError):
        cap = -1
    if cap >= 0:
        out["fix_rounds_cap"] = cap
    try:
        share = float(raw.get("ghost_share", out["ghost_share"]))
    except (TypeError, ValueError):
        share = -1.0
    if 0.0 <= share <= 1.0:
        out["ghost_share"] = share
    return out


def slug(text: str) -> str:
    """Stable 12-hex id key for a free-form message."""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


_LABEL_TITLE_MAX = 60


def _item_label(title, ident) -> str:
    """'Title (id)' for a plan task, train or approval; the bare id when it has no real title."""
    ident = str(ident or "")
    text = " ".join(str(title or "").split())
    if not text or text == ident:
        return ident or "item"
    if len(text) > _LABEL_TITLE_MAX:
        text = text[: _LABEL_TITLE_MAX - 3].rstrip() + "..."
    return f"{text} ({ident})" if ident else text


def make_problem(
    code: str,
    severity: str,
    scope: str,
    ws: str | None,
    title: str,
    detail: str,
    command: str,
    safe_auto: bool = False,
    key: str | None = None,
    items: list | None = None,
) -> dict:
    """One fresh (unpersisted) problem. The store fills history on snapshot.

    Grouped problems (one per code+workstream, see ``GROUPED_CODES``)
    carry ``items``: compact ``{title, id, since}`` member rows. Old
    per-item ids are never emitted; rows for them resolve on snapshot
    because they are absent from the run.
    """
    pid = f"{code}:{scope}" + (f":{key}" if key else "")
    out = {
        "id": pid,
        "code": code,
        "severity": severity if severity in SEV_RANK else "warn",
        "scope": scope,
        "ws": ws,
        "title": title,
        "detail": detail,
        "fix_command": command,
        "safe_auto": bool(safe_auto),
    }
    if items:
        out["items"] = [
            {
                "title": str(item.get("title") or item.get("id") or ""),
                "id": str(item.get("id") or ""),
                "since": item.get("since"),
            }
            for item in items
            if isinstance(item, dict)
        ]
    return out


def collect_problems(
    store,
    cfg: dict | None,
    now: float,
    *,
    url: str = "",
    health: dict | None = None,
    registry=None,
    processes: list | None = None,
    argv_tokens: dict | None = None,
    pgrep_counts: dict | None = None,
    workers_registry: dict | None = None,
    ppids: dict | None = None,
    tool_root=None,
) -> dict:
    """Run every check. Returns ``{"problems", "checks"}`` with fresh problems.

    Nothing is persisted here; the caller snapshots via
    ``store.doctor_snapshot``. ``store`` may be None (store-backed checks
    are skipped). ``health`` is the dashboard health payload, or None when
    the dashboard is down. ``processes`` is the live worker listing; when
    None the fleet checks that need it are skipped. ``argv_tokens``,
    ``pgrep_counts``, ``workers_registry``, and ``ppids`` override the
    live reads (tests inject fakes). ``tool_root`` overrides the checkout
    the git-backed checks read (tests point it at a temp repo).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        from dash.project import read_ignore_list

        _ignored_entries = read_ignore_list(cfg)
    except Exception:
        _ignored_entries = []
    ctx = SimpleNamespace(
        store=store,
        cfg=cfg,
        dcfg=doctor_cfg(cfg),
        now=float(now),
        url=url,
        health=health,
        registry=registry,
        processes=list(processes) if processes is not None else None,
        argv_tokens=argv_tokens,
        pgrep_counts=pgrep_counts,
        workers_registry=workers_registry,
        ppids=ppids,
        tool_root=Path(tool_root) if tool_root is not None else TOOL_ROOT,
        problems=[],
        checks=[],
        emitted=set(),
        groups={},
        _ignored=None,
        _runs=None,
        _runs_loaded=False,
        _bundles=None,
        _taskmap=None,
        _live_ids=None,
    )
    ctx._ignored = list(_ignored_entries or [])
    _collect_dashboard(ctx)
    _collect_roots(ctx)
    _collect_pricing(ctx)
    _collect_reconcile(ctx)
    _collect_bundles(ctx)
    _collect_orchestration(ctx)
    _collect_lifecycle(ctx)
    _collect_fleet(ctx)
    _collect_page(ctx)
    _collect_config(ctx)
    _collect_checkout(ctx)
    _collect_build(ctx)
    _collect_python(ctx)
    _emit_grouped(ctx)
    return {"problems": ctx.problems, "checks": ctx.checks}


def _ignored_ws(ctx, ws) -> bool:
    """True when a workstream id sits on the project's ignore list.

    The list (``project.json`` ``ignore_workstreams``) is read once per
    :func:`collect_problems` run; matching is case-insensitive with
    trailing-``*`` prefixes (see ``dash.project.is_ignored``). A
    missing list never matches.
    """
    if not isinstance(ws, str) or not ws.strip():
        return False
    entries = getattr(ctx, "_ignored", None)
    if not entries:
        return False
    try:
        from dash.project import is_ignored

        return bool(is_ignored(ws, entries))
    except Exception:
        return False


def _add(ctx, code, severity, scope, ws, title, detail, command, safe_auto=False, key=None, items=None):
    if isinstance(ws, str) and ws and _ignored_ws(ctx, ws):
        return None
    item = make_problem(code, severity, scope, ws, title, detail, command, safe_auto, key, items)
    if item["id"] in ctx.emitted:
        return None
    ctx.emitted.add(item["id"])
    ctx.problems.append(item)
    return item


def _group_add(ctx, code, severity, scope, ws, member_title, member_id, since, member_detail=""):
    """Collect one member of a grouped problem (one problem per code+workstream).

    Per-item ids are never emitted; :func:`_emit_grouped` builds the
    single group problem with the worst member severity, a title naming
    the workstream and the count, a detail with the worst 3, and the one
    list-all fix command.
    """
    if isinstance(ws, str) and ws and _ignored_ws(ctx, ws):
        return None
    bucket = ctx.groups.setdefault(
        (str(code), str(scope), ws), {"severity": "info", "members": []}
    )
    if SEV_RANK.get(severity, 9) < SEV_RANK.get(bucket["severity"], 9):
        bucket["severity"] = severity if severity in SEV_RANK else "warn"
    try:
        stamp = float(since)
    except (TypeError, ValueError):
        stamp = 0.0
    bucket["members"].append(
        {
            "title": " ".join(str(member_title or member_id or "").split()) or str(member_id or ""),
            "id": str(member_id or ""),
            "since": stamp,
            "detail": str(member_detail or ""),
            "severity": severity if severity in SEV_RANK else "warn",
        }
    )
    return bucket


def _ws_title(ctx, ws: str | None) -> str:
    """Workstream display title for a group title, or the bare id."""
    if not ws:
        return "global"
    for bundle in ctx._bundles or []:
        if isinstance(bundle, dict) and bundle.get("id") == ws:
            title = " ".join(str(bundle.get("title") or "").split())
            return title or str(ws)
    return str(ws)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _order_members(code: str, members: list) -> list:
    """Worst-first order per code for the detail's top 3."""
    rows = list(members)
    if code in ("stuck-task", "orphan-worker"):
        rows.sort(key=lambda m: (m.get("since") or 0, str(m.get("id"))))
    elif code == "dep-jump":
        rows.sort(key=lambda m: (-(m.get("since") or 0), str(m.get("id"))))
    else:
        rows.sort(key=lambda m: str(m.get("id")))
    return rows


def _group_fix(code: str, ws: str | None) -> str:
    if code == "dep-jump" and ws:
        return f"python3 agent_dash.py ws next {ws}"
    if ws:
        return f"python3 agent_dash.py doctor --ws {ws} --all"
    return GROUP_FIX_GLOBAL


def _group_title(code: str, ws_title: str, ws: str | None, members: list) -> str:
    n = len(members)
    if code == "dep-jump":
        return (
            f"{ws_title} ({ws}): {n} "
            f"{_plural(n, 'task', 'tasks')} started before a dependency finished"
        )
    if code == "stuck-task":
        return (
            f"{ws_title} ({ws}): {n} "
            f"{_plural(n, 'task', 'tasks')} stuck with no live agent"
        )
    if code == "done-before-deps":
        return (
            f"{ws_title} ({ws}): {n} "
            f"{_plural(n, 'task', 'tasks')} marked done before a dependency finished"
        )
    if code == "bare-citation":
        return (
            f"{ws_title} ({ws}): {n} bare number "
            f"{_plural(n, 'citation', 'citations')} with no name"
        )
    if code == "ghost-runs":
        return (
            f"{ws_title} ({ws}): {n} recent "
            f"{_plural(n, 'run', 'runs')} map to no plan task"
        )
    if code == "orphan-worker":
        return f"{n} orphan {_plural(n, 'worker', 'workers')} still alive"
    if code == "model-policy":
        return f"{n} {_plural(n, 'worker', 'workers')} break the model policy"
    if code == "over-cap":
        return f"{n} worker {_plural(n, 'tier', 'tiers')} over cap"
    return f"{ws_title} ({ws}): {n} open" if ws else f"{n} open"


def _group_detail(code: str, members: list) -> str:
    top = members[:3]
    parts = []
    for member in top:
        label = _item_label(member.get("title"), member.get("id"))
        extra = f" — {member['detail']}" if member.get("detail") else ""
        parts.append(f"{label}{extra}")
    text = "; ".join(parts)
    if len(members) > 3:
        text += f" (+{len(members) - 3} more)"
    tails = {
        "ghost-runs": "add exact or /regex/ entries to static/taskmap.local.json for the labels above, then reload",
        "model-policy": "fix the model or flags in workers.local.json, then relaunch the worker",
        "over-cap": "wait for lanes to finish, or raise the cap in workers.local.json",
        "orphan-worker": "it is reported, never signaled",
        "bare-citation": "name the decision, task, PR, issue or ADR before its number",
    }
    if code in tails:
        text += f". {tails[code]}"
    return text


def _emit_grouped(ctx) -> None:
    """Build one problem per (code, workstream) from collected members."""
    for (code, scope, ws), bucket in sorted(ctx.groups.items()):
        members = _order_members(code, bucket.get("members") or [])
        if not members:
            continue
        severity = bucket.get("severity") or "warn"
        ws_title = _ws_title(ctx, ws)
        title = _group_title(code, ws_title, ws, members)
        detail = _group_detail(code, members)
        command = _group_fix(code, ws)
        items = [
            {"title": m.get("title"), "id": m.get("id"), "since": m.get("since")}
            for m in members
        ]
        _add(ctx, code, severity, scope, ws, title, detail, command, False, None, items)


def _check(ctx, name: str, ok: bool, detail: str):
    ctx.checks.append({"name": name, "ok": bool(ok), "detail": detail})
    return ok


def _collect_dashboard(ctx) -> None:
    """Dashboard reachability and per-source health from the health payload."""
    health = ctx.health
    if health is None:
        _check(ctx, "dashboard", False, f"not reachable at {ctx.url}")
        _add(
            ctx, "dashboard-down", "critical", "global", None,
            "Dashboard is down",
            f"dashboard not reachable at {ctx.url}",
            "python3 agent_dash.py run",
        )
        return
    sources = health.get("sources") or []
    bad = [s.get("name") for s in sources if s.get("last_error")]
    stalled = [
        s.get("name")
        for s in sources
        if not s.get("last_error") and s.get("stalled") and s.get("state") != "starting"
    ]
    if health.get("ok") and not bad and not stalled:
        _check(ctx, "dashboard", True, f"up at {ctx.url}, {len(sources)} sources healthy")
    else:
        unpriced = [
            str(item.get("model"))
            for item in (health.get("unpriced_models") or [])
            if isinstance(item, dict) and item.get("model")
        ]
        if bad:
            cause = f"failing sources: {', '.join(str(name) for name in bad)}"
        elif stalled:
            cause = f"stalled sources: {', '.join(str(name) for name in stalled)}"
        elif unpriced:
            cause = f"unpriced models: {', '.join(unpriced)}"
        else:
            cause = "ok=false"
        _check(ctx, "dashboard", False, f"unhealthy: {cause}")
        if not bad and not stalled and not unpriced:
            _add(
                ctx, "dashboard-unhealthy", "warn", "global", None,
                "Dashboard reports unhealthy",
                f"dashboard at {ctx.url} reports {cause}",
                "python3 agent_dash.py status",
            )
    for source in sources:
        name = source.get("name")
        if source.get("last_error"):
            _add(
                ctx, "source-error", "critical", "global", None,
                f"Source {name} is failing",
                f"source {name!r} last poll failed: {source.get('last_error')}",
                "python3 agent_dash.py status",
                key=str(name),
            )
        elif source.get("state") != "starting" and (
            source.get("stalled")
            or (
                not source.get("last_error")
                and (source.get("last_poll") is None or (source.get("lag_s") or 0) > SOURCE_STALE_S)
            )
        ):
            _add(
                ctx, "source-stalled", "critical", "global", None,
                f"Source {name} is stalled",
                f"source {name!r} has no recent poll (last_poll={source.get('last_poll')})",
                "python3 agent_dash.py status",
                key=str(name),
            )
    stale = [
        str(s.get("name"))
        for s in sources
        if s.get("state") != "starting"
        and not s.get("last_error")
        and (s.get("last_poll") is None or (s.get("lag_s") or 0) > SOURCE_STALE_S)
    ]
    if not stale:
        _check(ctx, "sources", True, f"{len(sources)} sources polled recently, no errors")
    else:
        _check(ctx, "sources", False, f"stale: {', '.join(stale)}")
    _check_slow_sources(ctx, sources)
    _check_dagster_closeouts(ctx, health)
    _check_health_roots(ctx)


def _check_slow_sources(ctx, sources: list) -> None:
    """Warn on polls that returned but took longer than SLOW_AFTER_S."""
    from dash.sources import SLOW_AFTER_S

    slow = []
    for source in sources or []:
        if source.get("last_error") or source.get("state") == "starting":
            continue
        try:
            duration = float(source.get("last_duration_s") or 0)
        except (TypeError, ValueError):
            continue
        if source.get("state") == "slow" or duration > SLOW_AFTER_S:
            slow.append((source.get("name"), duration))
    if not slow:
        _check(ctx, "slow-sources", True, "no source poll over the slow threshold")
        return
    _check(ctx, "slow-sources", False, f"slow: {', '.join(str(name) for name, _ in slow)}")
    for name, duration in slow:
        _add(
            ctx, "source-slow", "warn", "global", None,
            f"Source {name} is slow",
            f"source {name!r} last poll took {duration:.1f}s (over {SLOW_AFTER_S:g}s) but returned",
            "python3 agent_dash.py status",
            key=str(name),
        )


def _check_dagster_closeouts(ctx, health: dict) -> None:
    """Warn when full active pages skipped Dagster close-outs."""
    dag = (health or {}).get("dagster") or {}
    try:
        skipped = int(dag.get("skipped_closeouts") or 0)
    except (TypeError, ValueError):
        skipped = 0
    if not skipped:
        _check(ctx, "dagster-closeouts", True, "no skipped close-outs")
        return
    _check(ctx, "dagster-closeouts", False, f"{skipped} skipped close-out(s)")
    _add(
        ctx, "dagster-closeout-skipped", "warn", "global", None,
        "Dagster close-outs were skipped",
        f"{skipped} close-out(s) skipped: the active set hit the page limit, so finished runs may still show running",
        "python3 agent_dash.py status",
        key="closeouts",
    )


def _check_health_roots(ctx) -> None:
    """Surface the dashboard's own run-dir visibility report."""
    health = ctx.health
    if health is None:
        _check(ctx, "run-dirs", True, "skipped, dashboard is down")
        return
    roots = health.get("scratch_roots")
    run_dirs = health.get("run_dirs")
    warnings = health.get("warnings") or []
    if roots is None and run_dirs is None and not warnings:
        _check(ctx, "run-dirs", True, "dashboard reports no run-dir data")
        return
    detail = f"{len(run_dirs or [])} registered run dirs, {len(roots or [])} scratch roots"
    if not warnings:
        _check(ctx, "run-dirs", True, detail)
        return
    _check(ctx, "run-dirs", False, f"{detail}; {len(warnings)} outside roots")
    for warning in warnings:
        _add(
            ctx, "run-dir-outside-roots", "info", "global", None,
            "Run folder outside the configured roots",
            str(warning),
            "add the path to roots.scratchpads in config, then restart the dashboard",
            safe_auto=True,
            key=slug(warning),
        )


def _collect_reconcile(ctx) -> None:
    if ctx.health is None:
        _check(ctx, "reconcile", True, "skipped, dashboard is down")
        return
    if ctx.store is None:
        _check(ctx, "reconcile", True, "skipped, no store")
        return
    if ctx.health is not None and ctx.url:
        payload = _get(ctx.url, "/api/reconcile?hours=24", timeout=15)
        server_mismatches = payload.get("mismatches") if isinstance(payload, dict) else None
    else:
        server_mismatches = None
    if server_mismatches is None and (ctx.health is None or ctx.url):
        _check(ctx, "reconcile", False, "reconcile endpoint unreadable")
        _add(
            ctx, "reconcile-unreadable", "warn", "global", None,
            "Reconcile endpoint is unreadable",
            "GET /api/reconcile failed",
            "python3 agent_dash.py status",
        )
        return
    try:
        body = ctx.store.reconcile_numbers(ctx.now, None, None, ctx.processes or [])
        local = body.get("mismatches") or []
    except Exception:
        local = []
    seen: set[str] = set()
    mismatches: list[str] = []
    for item in list(server_mismatches or []) + list(local):
        text = str(item)
        if text not in seen:
            seen.add(text)
            mismatches.append(text)
    if not mismatches:
        _check(ctx, "reconcile", True, "workstreams reconcile, no mismatches")
        return
    _check(ctx, "reconcile", False, f"{len(mismatches)} mismatches")
    for item in mismatches:
        _reconcile_problem(ctx, item)


def unseen_startup_ready(registry=None, health=None, now: float | None = None) -> bool:
    """Startup grace for the unseen-worker check (shared by doctor and ``check``).

    No unseen-worker problem is emitted for a worker unless the
    dashboard process is older than ``max(3x the slowest source
    interval, 90 s)`` AND every source has completed at least one poll
    since start. This is the one shared startup definition,
    :func:`startup_complete <dash.sources.startup_complete>` (built on
    the registry's :func:`has_completed_poll
    <dash.sources.has_completed_poll>`, :func:`startup_threshold_s
    <dash.sources.startup_threshold_s>`, and ``START_GRACE_S``), also
    used for the registry's ``starting`` state. The
    per-worker half (alive longer than 2 poll intervals) already lives
    in the store's unseen check; this is the dashboard-startup half.
    ``registry`` wins when present (server path); otherwise ``health``
    (the ``/api/health`` sources with ``interval_s``, ``started_at``,
    and ``last_poll``) decides. Unknown state reads as not ready.
    """
    import time as _time

    now = float(now if now is not None else _time.time())
    try:
        from dash.sources import startup_complete
    except Exception:
        return False
    if registry is not None:
        try:
            sources = list(getattr(registry, "sources", None) or [])
            born = float(getattr(registry, "_born", now))
            raw = getattr(registry, "state", None) or {}
            states = [item for item in raw.values() if isinstance(item, dict)]
        except Exception:
            return False
        if sources and not states:
            return False
        if not sources:
            return True
        try:
            return startup_complete(sources, states, born, now)
        except Exception:
            return False
    if isinstance(health, dict):
        sources = health.get("sources")
        if not isinstance(sources, list):
            return False
        if not sources:
            return True
        try:
            intervals = [
                float(item.get("interval_s") or 0) for item in sources
                if isinstance(item, dict)
            ]
        except (TypeError, ValueError):
            intervals = []
        stamps = [
            item.get("started_at") for item in sources
            if isinstance(item, dict) and item.get("started_at") is not None
        ]
        try:
            born = min(float(stamp) for stamp in stamps) if stamps else None
        except (TypeError, ValueError):
            born = None
        if born is None:
            return False
        try:
            return startup_complete(
                intervals,
                [item for item in sources if isinstance(item, dict)],
                born,
                now,
            )
        except Exception:
            return False
    return False


def _reconcile_problem(ctx, item: str) -> None:
    """Map one mismatch string to its problem code and severity."""
    if item.startswith("unseen worker: "):
        if not unseen_startup_ready(ctx.registry, ctx.health, ctx.now):
            return
        rest = item[len("unseen worker: "):]
        label = rest.split(" (", 1)[0].strip()
        _add(
            ctx, "unseen-worker", "critical", "global", None,
            "Worker is invisible on the dashboard",
            item,
            "python3 agent_dash.py check",
            key=label or slug(item),
        )
        return
    if item.startswith("unpriced model: "):
        return
    if item.startswith("source stalled: "):
        # Same startup gate as unseen workers: a stall line from before the
        # first full scan is the restart false positive, not a real stall.
        if not unseen_startup_ready(ctx.registry, ctx.health, ctx.now):
            return
        name = item[len("source stalled: "):].split(" (", 1)[0].strip()
        _add(
            ctx, "source-stalled", "critical", "global", None,
            f"Source {name} is stalled",
            item,
            "python3 agent_dash.py status",
            key=name or slug(item),
        )
        return
    _add(
        ctx, "reconcile-mismatch", "critical", "global", None,
        "Dashboard numbers disagree",
        item,
        "python3 agent_dash.py check",
        key=slug(item),
    )


def _get(url: str, path: str, timeout: float = 10):
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _expand(pattern: str) -> list[str]:
    from dash.locate import expand

    text = expand(str(pattern)).strip()
    if not text:
        return []
    if any(ch in text for ch in "*?["):
        return sorted(item for item in glob.glob(text) if os.path.isdir(item))
    return [text] if os.path.isdir(text) else []


def _scratch_patterns(cfg: dict) -> list[str]:
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    raw = roots.get("scratchpads")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _collect_roots(ctx) -> None:
    from dash.locate import expand

    patterns = _scratch_patterns(ctx.cfg)
    if not patterns:
        _check(ctx, "scratch-roots", True, "no scratchpads configured")
    else:
        missing = []
        unreadable = []
        found = 0
        for pattern in patterns:
            text = expand(str(pattern)).strip()
            matches = _expand(pattern)
            if not matches:
                missing.append(text or pattern)
                continue
            for match in matches:
                found += 1
                if not (os.access(match, os.R_OK) and os.access(match, os.X_OK)):
                    unreadable.append(match)
        if not missing and not unreadable:
            _check(ctx, "scratch-roots", True, f"{found} roots readable")
        else:
            detail = "; ".join(
                ([f"missing: {', '.join(missing)}"] if missing else [])
                + ([f"unreadable: {', '.join(unreadable)}"] if unreadable else [])
            )
            _check(ctx, "scratch-roots", False, detail)
            for path in missing:
                _add(
                    ctx, "scratch-root-missing", "warn", "global", None,
                    "Configured scratch root matches nothing",
                    f"scratch root {path!r} matches nothing",
                    "create the directory, or fix roots.scratchpads in config, then restart the dashboard",
                    key=slug(path),
                )
            for path in unreadable:
                _add(
                    ctx, "scratch-root-unreadable", "warn", "global", None,
                    "Configured scratch root is not readable",
                    f"scratch root {path!r} is not readable",
                    f"fix permissions on {path}, then restart the dashboard",
                    key=slug(path),
                )
    scanned = _scanned_dirs(ctx.cfg)
    home_scratch = _home_scratch()
    places = [home_scratch] + sorted(scanned) + _registry_dirs(_state_dir(ctx.cfg))
    outside = [item for item in _manifest_dirs(places) if not _covered(item, scanned)]
    if not outside:
        _check(ctx, "manifest-coverage", True, f"{len(scanned)} run dirs scanned")
        return
    _check(ctx, "manifest-coverage", False, f"manifests outside scanned roots: {'; '.join(outside)}")
    for item in outside:
        _add(
            ctx, "manifest-outside-roots", "info", "global", None,
            "Worker manifest outside the scanned roots",
            f"live worker manifest at {item} is outside the scanned roots",
            "add the path to roots.scratchpads in config, then restart the dashboard",
            safe_auto=True,
            key=slug(item),
        )


def _home_scratch() -> str:
    return os.path.join(os.path.expanduser("~"), "agent-scratch")


def _state_dir(cfg: dict):
    from dash.locate import expand

    raw = cfg.get("state_dir") if isinstance(cfg, dict) else None
    if isinstance(raw, str) and raw.strip():
        return Path(expand(raw))
    env = os.environ.get("AGENT_DASH_STATE_DIR")
    if env and env.strip():
        return Path(expand(env.strip()))
    return None


def _scanned_dirs(cfg: dict) -> set[str]:
    """Run dirs the collector reads: the shipped scratch glob plus configured roots."""
    scanned: set[str] = set()
    for pattern in _scratch_patterns(cfg):
        scanned.update(_expand(pattern))
    home_scratch = _home_scratch()
    if os.path.isdir(home_scratch):
        scanned.update(
            item for item in glob.glob(os.path.join(home_scratch, "*")) if os.path.isdir(item)
        )
    return scanned


def _registry_dirs(state_dir: Path | None) -> list[str]:
    """Extra known run dirs from the run-dirs registry file in the state dir, if any."""
    if state_dir is None:
        return []
    found = []
    for name in RUN_DIRS_NAMES:
        file = state_dir / name
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        if file.suffix == ".txt":
            found.extend(line.strip() for line in text.splitlines() if line.strip())
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, list):
            found.extend(str(item) for item in data if isinstance(item, str) and item)
    return found


def _manifest_dirs(places: list[str]) -> list[str]:
    """Dirs holding a live (non-empty) worker manifest, checked here and one level down."""
    found = []
    seen = set()

    def consider(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        try:
            names = os.listdir(path)
        except OSError:
            return
        live = [name for name in names if name in MANIFEST_NAMES and _nonempty(os.path.join(path, name))]
        if live:
            found.append(f"{path} ({', '.join(sorted(live))})")

    for place in places:
        if not os.path.isdir(place):
            continue
        consider(place)
        try:
            children = os.listdir(place)
        except OSError:
            continue
        for child in sorted(children):
            full = os.path.join(place, child)
            if os.path.isdir(full):
                consider(full)
    return sorted(found)


def _covered(path: str, scanned: set[str]) -> bool:
    base = path.split(" (")[0]
    return base in scanned or os.path.dirname(base) in scanned


def _nonempty(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _db_path(cfg: dict):
    from dash.locate import expand

    override = os.environ.get("AGENT_DASH_DB")
    if override:
        return Path(expand(override))
    state = cfg.get("state_dir") if isinstance(cfg, dict) else None
    if not state:
        return None
    return Path(expand(str(state))) / "dash.sqlite"


def _db_file(ctx) -> Path | None:
    """SQLite file for read-only doctor queries: the live store wins."""
    store = ctx.store
    path = getattr(store, "path", None)
    if path is not None and str(path) not in ("", ":memory:"):
        return Path(str(path))
    try:
        file = _db_path(ctx.cfg)
    except Exception:
        return None
    if file is not None and file.is_file():
        return file
    return None


def _recent_models(db: Path, since: float | None = None) -> list[dict]:
    """Models with usage rows (and their billed tokens), read-only."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        if since is None:
            rows = conn.execute(
                """SELECT model AS model, COUNT(*) AS rows,
                          SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)
                              + COALESCE(cache_read,0) + COALESCE(cache_write,0)) AS tokens
                   FROM usage_events GROUP BY model"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT model AS model, COUNT(*) AS rows,
                          SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)
                              + COALESCE(cache_read,0) + COALESCE(cache_write,0)) AS tokens
                   FROM usage_events WHERE ts >= ? GROUP BY model""",
                (float(since),),
            ).fetchall()
        return [
            {"model": row["model"], "rows": row["rows"], "tokens": row["tokens"] or 0}
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _collect_pricing(ctx) -> None:
    from dash.pricing import catalog as load_catalog

    try:
        rows = {row["model"]: row for row in load_catalog()}
    except Exception as exc:
        _check(ctx, "pricing", False, f"catalog unreadable: {exc}")
        _add(
            ctx, "pricing-unreadable", "warn", "global", None,
            "Price catalog is unreadable",
            f"catalog/prices.json is unreadable: {exc}",
            "restore tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
        )
        return
    db = _db_file(ctx)
    models: list[dict] = []
    if db is not None and db.is_file():
        models = _recent_models(db, ctx.now - 86400)
    if ctx.health is not None:
        seen = {str(item.get("model")) for item in models if item.get("model")}
        for item in ctx.health.get("unpriced_models") or []:
            if isinstance(item, dict) and item.get("model") and str(item["model"]) not in seen:
                seen.add(str(item["model"]))
                models.append(
                    {"model": item["model"], "rows": item.get("rows") or 0, "tokens": item.get("tokens") or 0}
                )
    missing = [
        item
        for item in models
        if item.get("model")
        and int(item.get("tokens") or 0) > 0
        and (item["model"] not in rows or rows[item["model"]].get("input") is None or rows[item["model"]].get("output") is None)
    ]
    if not missing:
        _check(ctx, "pricing", True, f"{len(rows)} price rows, {len(models)} models on record priced")
        return
    names = [str(item["model"]) for item in missing]
    _check(ctx, "pricing", False, f"unpriced models: {', '.join(names)}")
    for item in missing:
        model = str(item["model"])
        _add(
            ctx, "unpriced-model", "critical", "global", None,
            f"Model {model} has usage but no price",
            f"model {model!r} has {item.get('rows') or 0} usage rows "
            f"({item.get('tokens') or 0} tokens in 24 h) with no price (cost counts unpriced runs, never $0)",
            "add a row to tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
            safe_auto=True,
            key=model,
        )


def _chrome_path() -> str | None:
    explicit = os.environ.get("CHROME_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "darwin" and os.path.exists(mac):
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _collect_page(ctx) -> None:
    node = shutil.which("node")
    chrome = _chrome_path()
    if node and chrome:
        _check(ctx, "page-check", True, f"node {node}, chrome {chrome}")
        return
    missing = [name for name, found in (("node", node), ("chrome", chrome)) if not found]
    _check(ctx, "page-check", False, f"missing: {', '.join(missing)}")
    if not node:
        _add(
            ctx, "page-check-no-node", "info", "global", None,
            "Node is missing, so check --page always skips",
            "node is not on PATH, so check --page always skips",
            "install Node 18+, then run: python3 agent_dash.py check --page",
        )
    if not chrome:
        _add(
            ctx, "page-check-no-chrome", "info", "global", None,
            "No Chrome binary found, so check --page always skips",
            "no Chrome binary found (CHROME_PATH, the default install path, or PATH)",
            "set CHROME_PATH to a Chrome binary, then run: python3 agent_dash.py check --page",
        )


def _state_config_file(state_dir=None) -> Path | None:
    """``<state-dir>/config.local.json`` when local files count.

    Mirrors ``load_cfg``: the state dir copy lets a release checkout run
    with the user's settings. Skipped under ``AGENT_DASH_IGNORE_LOCAL``
    so tests see only the shipped example files.
    """
    from dash.locate import expand, ignore_local

    if ignore_local():
        return None
    raw = os.environ.get("AGENT_DASH_STATE_DIR") or state_dir
    if not raw:
        raw = str(Path.home() / ".local" / "share" / "agent-dash")
    return Path(expand(str(raw))) / "config.local.json"


def _config_files(state_dir=None) -> list[Path]:
    files = [TOOL_ROOT / "config.example.json", TOOL_ROOT / "config.local.json"]
    override = os.environ.get("AGENT_DASH_CONFIG")
    if override:
        from dash.locate import expand

        files.append(Path(expand(override)))
    state_config = _state_config_file(state_dir)
    if state_config is not None:
        files.append(state_config)
    return files


def _collect_config(ctx) -> None:
    bad = []
    unknown = []
    cfg_state = ctx.cfg.get("state_dir") if isinstance(ctx.cfg, dict) else None
    for file in _config_files(cfg_state):
        if file.name != "config.example.json" and not file.is_file():
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            bad.append(f"{file}: {exc}")
            continue
        if not isinstance(data, dict):
            bad.append(f"{file}: top level must be an object")
            continue
        for key in data:
            if key not in KNOWN_TOP_KEYS:
                unknown.append(f"{file.name}: unknown key {key!r}")
        roots = data.get("roots")
        if isinstance(roots, dict):
            for key in roots:
                if key not in KNOWN_ROOT_KEYS:
                    unknown.append(f"{file.name}: unknown roots key {key!r}")
    if bad:
        _check(ctx, "config", False, "; ".join(bad))
        for item in bad:
            _add(
                ctx, "config-unparseable", "warn", "global", None,
                "Config file does not parse",
                item,
                "fix the JSON, then restart the dashboard",
                key=slug(item),
            )
    for item in unknown:
        _add(
            ctx, "config-unknown-key", "warn", "global", None,
            "Config file has an unknown key",
            item,
            "remove the key, then restart the dashboard",
            key=slug(item),
        )
    if unknown and not bad:
        _check(ctx, "config", False, "; ".join(unknown))
    if not bad and not unknown:
        _check(ctx, "config", True, "config files parse, no unknown keys")


def _collect_checkout(ctx) -> None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ctx.tool_root), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        _check(ctx, "checkout", True, "not a git checkout, skipping")
        return
    if proc.returncode != 0:
        _check(ctx, "checkout", True, "not a git checkout, skipping")
        return
    dirty = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not dirty:
        _check(ctx, "checkout", True, "clean")
        return
    _check(ctx, "checkout", True, f"{len(dirty)} dirty files (advisory only)")
    _add(
        ctx, "dashboard-dirty-checkout", "info", "global", None,
        "Dashboard checkout has uncommitted changes",
        f"tool checkout has {len(dirty)} uncommitted changes",
        "review with: git status --short",
    )


def _looks_like_sha(text: str) -> bool:
    """A plausible git sha (full or abbreviated): hex, 4 to 40 chars."""
    return 4 <= len(text) <= 40 and all(ch in "0123456789abcdef" for ch in text)


def _git_line(ctx, *args: str) -> str:
    """One stdout line from a read-only git command, or ``""``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ctx.tool_root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _collect_build(ctx) -> None:
    """The running build: dirty flag plus distance from ``origin/main``.

    Reads the ``build_id`` the dashboard reported in its health payload
    (``build-dirty`` warns when it carries the dirty flag) and compares
    the running commit against ``origin/main`` in the tool checkout
    (``build-behind`` is info when the running build is strictly older).
    Anything git cannot tell skips quietly.
    """
    health = ctx.health
    if health is None:
        _check(ctx, "build", True, "skipped, dashboard is down")
        return
    build = str(health.get("build_id") or "")
    if not build:
        _check(ctx, "build", True, "server reports no build id")
        return
    dirty = build.endswith("-dirty")
    base = build[: -len("-dirty")] if dirty else build
    detail = f"running build {build}"
    behind = False
    tip = ""
    if _looks_like_sha(base):
        # Resolve both ends to full shas first: the running build is
        # abbreviated, and merge-base prints full shas.
        full = _git_line(ctx, "rev-parse", "--verify", f"{base}^{{commit}}")
        tip = _git_line(ctx, "rev-parse", "--verify", "origin/main^{commit}")
        if full and tip and _looks_like_sha(full) and _looks_like_sha(tip) and tip != full:
            # The merge base of the two is the running commit exactly
            # when the running build is strictly older than origin/main.
            behind = _git_line(ctx, "merge-base", full, tip) == full
            if behind:
                detail += f"; origin/main is {tip}"
    if dirty:
        _add(
            ctx, "build-dirty", "warn", "global", None,
            "Running build is dirty",
            f"dashboard at {ctx.url} runs build {build}, built from uncommitted changes",
            "python3 agent_dash.py deploy",
        )
    if behind:
        _add(
            ctx, "build-behind", "info", "global", None,
            "Running build is behind origin/main",
            f"dashboard runs {base}, origin/main is {tip}",
            "python3 agent_dash.py deploy",
        )
    if behind and not dirty:
        detail += " (advisory only)"
    _check(ctx, "build", not dirty, detail)


def _collect_python(ctx) -> None:
    current = sys.version_info[:3]
    runner = TOOL_ROOT / "scripts" / "run_worker.py"
    if tuple(current) >= MIN_PYTHON and runner.is_file():
        _check(
            ctx, "python", True,
            f"python {'.'.join(str(n) for n in current)} runs scripts/run_worker.py",
        )
        return
    if not runner.is_file():
        _check(ctx, "python", False, "scripts/run_worker.py is missing")
        _add(
            ctx, "worker-runner-missing", "warn", "global", None,
            "Worker runner script is missing",
            "scripts/run_worker.py is missing",
            "restore tools/agent-dash/scripts/run_worker.py from version control",
        )
        return
    _check(
        ctx, "python", False,
        f"python {'.'.join(str(n) for n in current)} is older than {'.'.join(str(n) for n in MIN_PYTHON)}",
    )
    _add(
        ctx, "python-too-old", "warn", "global", None,
        "Python is older than the worker runner needs",
        f"python {'.'.join(str(n) for n in current)} is older than scripts/run_worker.py needs",
        "run the dashboard with a newer python3, then run: python3 agent_dash.py status",
    )


def _runs_ro(ctx) -> list[dict]:
    """All run rows for task mapping, read-only. Cached per collect."""
    if ctx._runs_loaded:
        return ctx._runs or []
    ctx._runs_loaded = True
    ctx._runs = []
    db = _db_file(ctx)
    if db is None or not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        ctx._runs = [
            {
                "run_id": row["run_id"],
                "source": row["source"],
                "label": row["label"],
                "status": row["status"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "workstream": row["workstream"],
            }
            for row in conn.execute(
                "SELECT run_id, source, label, status, started_at, ended_at, workstream FROM runs"
            ).fetchall()
        ]
    except sqlite3.Error:
        ctx._runs = []
    finally:
        conn.close()
    return ctx._runs


def _live_run_ids(ctx) -> set[str]:
    """Open run ids with liveness evidence: running rows minus stale ones."""
    rows = _runs_ro(ctx)
    open_ids = {
        str(row["run_id"]) for row in rows if row.get("status") in ("running", "queued")
    }
    if not open_ids or ctx.store is None:
        return open_ids
    try:
        stale = {str(run.get("run_id")) for run, _reason in ctx.store.stale_running(ctx.now)}
    except Exception:
        return open_ids
    return open_ids - stale


def _bundle_task_map(ws: str):
    """(tasks, plan_ids, plan_mtime, history_ts, error). Never raises."""
    from dash.bundles import bundle_dir, resolve_plan

    tasks: dict[str, dict] = {}
    try:
        _source, body = resolve_plan(ws)
    except Exception:
        return None, [], None, {}, "unreadable"
    groups = body.get("groups") if isinstance(body, dict) else None
    if not isinstance(groups, list):
        return None, [], None, {}, "unreadable"
    for group in groups:
        if not isinstance(group, dict):
            continue
        for task in group.get("tasks") or []:
            if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                continue
            deps = task.get("deps")
            tasks[task["id"]] = {
                "status": task.get("status") or "todo",
                "deps": [item for item in deps] if isinstance(deps, list) else [],
                "kind": task.get("kind") or "task",
                "title": task.get("title") or task["id"],
            }
    try:
        from dash.taskmatch import plan_task_ids

        plan_ids = plan_task_ids(groups)
    except Exception:
        plan_ids = list(tasks)
    path = bundle_dir(ws)
    mtime = None
    history: dict[str, float] = {}
    if path is not None:
        try:
            mtime = (path / "plan.json").stat().st_mtime
        except OSError:
            mtime = None
        try:
            for line in (path / "history.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("task"), str):
                    try:
                        history[str(row["task"])] = float(row.get("ts") or 0)
                    except (TypeError, ValueError):
                        continue
        except OSError:
            pass
    return tasks, plan_ids, mtime, history, None


def _dep_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle, or None. Unknown dep ids are validation's issue."""
    order = sorted(deps)
    seen: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> list[str] | None:
        seen.add(node)
        stack.append(node)
        on_stack.add(node)
        for dep in deps.get(node) or []:
            if dep not in deps:
                continue
            if dep not in seen:
                hit = visit(dep)
                if hit is not None:
                    return hit
            elif dep in on_stack:
                return stack[stack.index(dep):] + [dep]
        stack.pop()
        on_stack.discard(node)
        return None

    for node in order:
        if node not in seen:
            hit = visit(node)
            if hit is not None:
                return hit
    return None


def _collect_bundles(ctx) -> None:
    from dash.bundles import approvals_for, list_bundles, validate_bundle
    from dash.taskmatch import load_taskmap, task_of

    try:
        bundles = list_bundles()
    except Exception as exc:
        _check(ctx, "bundles", False, f"bundle list failed: {exc}")
        _add(
            ctx, "bundles-unreadable", "warn", "global", None,
            "Bundle list failed",
            f"bundle list failed: {exc}",
            "python3 agent_dash.py ws list",
        )
        for name in ("workstream", "approvals", "trains"):
            _check(ctx, name, True, "skipped, bundle list failed")
        return
    failing: dict[str, list[str]] = {}
    for item in bundles:
        try:
            issues = validate_bundle(item["id"])
        except Exception:
            continue
        if issues:
            failing[item["id"]] = issues
    if not failing:
        _check(ctx, "bundles", True, f"{len(bundles)} bundles validate")
    else:
        _check(ctx, "bundles", False, f"failing: {', '.join(sorted(failing))}")
    for ws in sorted(failing):
        issues = failing[ws]
        bare = [issue for issue in issues if "bare citation" in issue]
        rest = [issue for issue in issues if "bare citation" not in issue]
        for issue in bare:
            _group_add(
                ctx, "bare-citation", "warn", f"ws:{ws}", ws,
                str(issue)[:60], slug(issue), ctx.now, str(issue),
            )
        if rest:
            first = rest[0]
            extra = f" (+{len(rest) - 1} more)" if len(rest) > 1 else ""
            _add(
                ctx, "invalid-bundle", "warn", f"ws:{ws}", ws,
                f"Bundle {ws} fails validation",
                f"bundle {ws} fails validation: {first}{extra}",
                f"python3 agent_dash.py ws validate {ws}",
            )
    taskmap = load_taskmap()
    live_ids = _live_run_ids(ctx)
    runs = _runs_ro(ctx)
    ctx._bundles = bundles
    ctx._taskmap = taskmap
    ctx._live_ids = live_ids
    stuck_hours = ctx.dcfg["stuck_hours"]
    hygiene = 0
    for item in bundles:
        hygiene += _collect_workstream(ctx, item["id"], taskmap, runs, live_ids, stuck_hours)
    _check(
        ctx, "workstream", hygiene == 0,
        f"{len(bundles)} workstreams checked, {hygiene} stuck or inconsistent tasks" if hygiene
        else f"{len(bundles)} workstreams, no stuck or inconsistent tasks",
    )
    stale = _collect_approvals(ctx, approvals_for, bundles)
    _check(
        ctx, "approvals", not stale,
        f"{stale} pending approvals past their window" if stale else "no approval past its window",
    )
    stuck = _collect_trains(ctx, bundles)
    _check(
        ctx, "trains", not stuck,
        f"{stuck} trains stuck in one status" if stuck else "no train stuck in one status",
    )


def _collect_workstream(ctx, ws: str, taskmap: dict, runs: list, live_ids: set, stuck_hours: float) -> int:
    """Task hygiene for one bundle. Returns the problem count."""
    from dash.taskmatch import task_of

    tasks, plan_ids, mtime, history, error = _bundle_task_map(ws)
    if error or tasks is None:
        return 0
    status_of = {tid: info["status"] for tid, info in tasks.items()}
    by_task: dict[str, list[dict]] = {}
    for run in runs:
        label = run.get("label")
        if not label:
            continue
        try:
            task = task_of(label, ws, taskmap, plan_ids)
        except Exception:
            task = None
        if task and task in tasks:
            by_task.setdefault(task, []).append(run)
    live_labels = set()
    for proc in ctx.processes or []:
        if isinstance(proc, dict) and proc.get("label"):
            try:
                task = task_of(proc["label"], ws, taskmap, plan_ids)
            except Exception:
                task = None
            if task and task in tasks:
                live_labels.add(task)
    live_runs = set()
    for task, rows in by_task.items():
        for row in rows:
            if str(row.get("run_id")) in live_ids:
                live_runs.add(task)
                break
    count = 0
    for tid, info in sorted(tasks.items()):
        status = info["status"]
        if status in ("wip", "pr") and tid not in live_labels and tid not in live_runs:
            stamps = [history.get(tid) or 0]
            for row in by_task.get(tid, []):
                for key in ("started_at", "ended_at"):
                    value = row.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        stamps.append(float(value))
            if mtime is not None and not by_task.get(tid) and not history.get(tid):
                stamps.append(float(mtime))
            last = max(stamps) if stamps else 0
            if last and ctx.now - last > stuck_hours * 3600:
                hours = (ctx.now - last) / 3600
                _group_add(
                    ctx, "stuck-task", "warn", f"ws:{ws}", ws,
                    info["title"], tid, last,
                    f"{status} with no live agent for {hours:.1f} h (limit {stuck_hours:g} h)",
                )
                count += 1
        if status == "done":
            for dep in info["deps"]:
                if dep in status_of and status_of[dep] != "done":
                    dep_label = _item_label(tasks[dep]["title"], dep)
                    _group_add(
                        ctx, "done-before-deps", "warn", f"ws:{ws}", ws,
                        info["title"], tid, history.get(tid) or mtime or ctx.now,
                        f"done while {dep_label} is {status_of[dep]} (not done)",
                    )
                    count += 1
                    break
        if info["kind"] == "gate" and status != "done":
            building = sorted(
                other for other, other_info in tasks.items()
                if tid in other_info["deps"] and other_info["status"] in ("wip", "pr")
            )
            if building:
                names = ", ".join(
                    _item_label(tasks[other]["title"], other) for other in building[:3]
                ) + (f" (+{len(building) - 3} more)" if len(building) > 3 else "")
                label = _item_label(info["title"], tid)
                _add(
                    ctx, "gate-blocking", "warn", f"ws:{ws}", ws,
                    f"{label} is unmet while dependents are already building",
                    f"{label} in {ws} is {status} (unmet) with dependents already in "
                    f"wip/pr: {names}",
                    f"python3 agent_dash.py ws plan {ws}",
                    key=tid,
                )
                count += 1
    cycle = _dep_cycle({tid: [dep for dep in info["deps"]] for tid, info in tasks.items()})
    if cycle:
        _add(
            ctx, "dep-cycle", "warn", f"ws:{ws}", ws,
            f"Dependency cycle in the {ws} plan",
            f"plan for {ws} has a dependency cycle: "
            f"{' -> '.join(_item_label(tasks[tid]['title'], tid) for tid in cycle)}",
            f"python3 agent_dash.py ws validate {ws}",
        )
        count += 1
    return count


def _collect_approvals(ctx, approvals_for, bundles) -> int:
    """Pending approvals older than the window. Returns the problem count."""
    from dash.bundles import bundle_dir

    limit_s = ctx.dcfg["approval_days"] * 86400
    count = 0
    for item in bundles:
        ws = item["id"]
        try:
            record = approvals_for(ws)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        pending = [
            entry for entry in (record.get("items") or [])
            if isinstance(entry, dict) and entry.get("status") == "pending"
        ]
        if not pending:
            continue
        path = bundle_dir(ws)
        try:
            mtime = (path / "approvals.json").stat().st_mtime if path is not None else None
        except OSError:
            mtime = None
        if mtime is None or ctx.now - mtime <= limit_s:
            continue
        days = (ctx.now - mtime) / 86400
        for entry in pending:
            item_id = str(entry.get("id") or "")
            label = _item_label(entry.get("step"), item_id)
            _add(
                ctx, "approval-stale", "warn", f"ws:{ws}", ws,
                f"{label} approval has waited too long",
                f"{label} approval in {ws} has been "
                f"pending for {days:.1f} days (limit {ctx.dcfg['approval_days']:g} days)",
                f"python3 agent_dash.py ws approvals {ws}",
                key=item_id or slug(str(entry.get("step"))),
            )
            count += 1
    return count


def _collect_trains(ctx, bundles) -> int:
    """Trains stuck in one non-terminal status. Returns the problem count."""
    from dash.bundles import bundle_dir

    limit_s = ctx.dcfg["train_hours"] * 3600
    count = 0
    for item in bundles:
        ws = item["id"]
        path = bundle_dir(ws)
        if path is None:
            continue
        try:
            data = json.loads((path / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        trains = data.get("trains") if isinstance(data, dict) else None
        if not isinstance(trains, list) or not trains:
            continue
        try:
            mtime = (path / "plan.json").stat().st_mtime
        except OSError:
            continue
        if ctx.now - mtime <= limit_s:
            continue
        hours = (ctx.now - mtime) / 3600
        for train in trains:
            if not isinstance(train, dict):
                continue
            status = train.get("status")
            if status in TERMINAL_TRAIN or status is None:
                continue
            tid = str(train.get("id") or "")
            label = _item_label(train.get("title"), tid)
            members = train.get("members") if isinstance(train.get("members"), list) else []
            _add(
                ctx, "train-stuck", "warn", f"ws:{ws}", ws,
                f"{label} is stuck waiting",
                f"{label} in {ws} has status {status} for {hours:.1f} h "
                f"(limit {ctx.dcfg['train_hours']:g} h, {len(members)} members)",
                f"python3 agent_dash.py ws trains {ws}",
                key=tid or slug(str(train.get("title"))),
            )
            count += 1
    return count


def _collect_orchestration(ctx) -> None:
    """Flag the failure modes the orchestration study measured.

    Wires the pure ``dash/orchestrate.py`` helpers into the doctor: one
    plan read per workstream plus the already-cached run rows, and one
    ``lead_profile`` call only for workstreams with a live run right
    now. No extra store scans per workstream. A workstream with no plan
    (no resolvable tasks), or with no activity inside the recent window
    (no mapped or attributed run stamps, no live run, no plan write),
    never fires: a quiet board is not a failure.
    """
    from dash import orchestrate
    from dash.bundles import list_bundles, resolve_plan
    from dash.taskmatch import load_taskmap

    bundles = ctx._bundles
    if bundles is None:
        try:
            bundles = list_bundles()
        except Exception:
            bundles = []
    taskmap = ctx._taskmap if isinstance(ctx._taskmap, dict) else load_taskmap()
    live_ids = ctx._live_ids if isinstance(ctx._live_ids, set) else _live_run_ids(ctx)
    runs = _runs_ro(ctx)
    recent_s = float(ctx.dcfg["recent_hours"]) * 3600
    bodies: dict[str, dict] = {}
    titles: dict[str, str] = {}
    for item in bundles or []:
        ws = item.get("id") if isinstance(item, dict) else None
        if not isinstance(ws, str) or not ws:
            continue
        try:
            _source, body = resolve_plan(ws)
        except Exception:
            body = None
        if isinstance(body, dict):
            bodies[ws] = body
            title = item.get("title") if isinstance(item, dict) else None
            titles[ws] = str(title or ws)
    counts = {
        "idle-ready": 0, "dep-jump": 0, "stale-gate": 0,
        "deferred-orphan": 0, "long-tail": 0, "review-spiral": 0,
        "ghost-runs": 0, "lead-undelegated": 0,
    }
    states: dict[str, dict] = {}
    for ws in sorted(bodies):
        try:
            state = _orch_ws_state(ctx, ws, bodies[ws], titles[ws], taskmap, runs, live_ids, recent_s)
        except Exception:
            state = None
        if state is None:
            continue
        states[ws] = state
        counts["idle-ready"] += _orch_idle_ready(ctx, ws, state)
        counts["dep-jump"] += _orch_dep_jumps(ctx, ws, state)
        counts["stale-gate"] += _orch_stale_gates(ctx, ws, state)
        counts["long-tail"] += _orch_long_tail(ctx, ws, state)
        counts["review-spiral"] += _orch_review_spiral(ctx, ws, state)
        counts["ghost-runs"] += _orch_ghost_runs(ctx, ws, state)
        counts["lead-undelegated"] += _orch_lead(ctx, ws, state)
    try:
        ledger = orchestrate.deferred_ledger(bodies)
    except Exception:
        ledger = []
    for row in ledger if isinstance(ledger, list) else []:
        counts["deferred-orphan"] += _orch_deferred_orphan(ctx, row, states, recent_s)
    for code in counts:
        total = counts[code]
        _check(
            ctx, code, total == 0,
            f"{total} open" if total else "clean",
        )


def _orch_plan_change(ws: str) -> tuple[float | None, list]:
    """(last_change, history_rows) for one bundle: plan mtime over history."""
    from dash.bundles import bundle_dir
    from dash.replay import read_history

    change: float | None = None
    rows: list = []
    try:
        path = bundle_dir(ws)
    except Exception:
        path = None
    if path is None:
        return None, []
    try:
        change = (path / "plan.json").stat().st_mtime
    except OSError:
        change = None
    try:
        rows = read_history(ws) or []
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            stamp = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if change is None or stamp > change:
            change = stamp
    return change, rows


def _orch_ws_state(ctx, ws: str, body: dict, title: str, taskmap: dict,
                   runs: list, live_ids: set, recent_s: float) -> dict | None:
    """Mapped runs, live lanes, and history for one workstream, or None.

    None means the workstream must not fire: no plan tasks, or no
    activity inside the recent window. One pass over the cached runs;
    the plan body is already loaded by the caller.
    """
    from dash import orchestrate
    from dash.taskmatch import task_of

    by_id, _deps, order = orchestrate.plan_tasks(body)
    if not by_id:
        return None
    by_task: dict[str, list] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        owner = run.get("workstream")
        if owner and owner != ws:
            continue
        try:
            tid = task_of(run.get("label") or "", ws, taskmap, order)
        except Exception:
            tid = None
        if isinstance(tid, str) and tid in by_id:
            by_task.setdefault(tid, []).append(run)
    ws_runs = [run for run in runs if isinstance(run, dict) and run.get("workstream") == ws]
    live_map: dict[str, str] = {}
    live_labels: set[str] = set()
    for tid, rows in by_task.items():
        alive = [run for run in rows if str(run.get("run_id")) in live_ids]
        if not alive:
            continue

        def _started(run) -> float:
            try:
                return float(run.get("started_at") or 0)
            except (TypeError, ValueError):
                return 0.0

        alive.sort(key=_started)
        label = str(alive[-1].get("label") or alive[-1].get("run_id"))
        live_map[tid] = label
        for run in alive:
            live_labels.add(str(run.get("label") or run.get("run_id")))
    proc_tasks: dict[str, list] = {}
    for proc in ctx.processes or []:
        if not isinstance(proc, dict) or not proc.get("label"):
            continue
        try:
            tid = task_of(str(proc["label"]), ws, taskmap, order)
        except Exception:
            tid = None
        if isinstance(tid, str) and tid in by_id:
            proc_tasks.setdefault(tid, []).append(str(proc["label"]))
    plan_change, history = _orch_plan_change(ws)
    cutoff = ctx.now - recent_s
    mapped = [row for rows in by_task.values() for row in rows]
    live_run_here = any(str(run.get("run_id")) in live_ids for run in mapped + ws_runs)
    recent_run = False
    for run in mapped + ws_runs:
        for key in ("started_at", "ended_at"):
            try:
                stamp = float(run.get(key))
            except (TypeError, ValueError):
                continue
            if stamp >= cutoff:
                recent_run = True
                break
        if recent_run:
            break
    fresh_plan = plan_change is not None and ctx.now - plan_change <= recent_s
    if not (live_run_here or proc_tasks or recent_run or fresh_plan):
        return None
    return {
        "body": body, "title": title, "by_id": by_id, "order": order,
        "by_task": by_task, "ws_runs": ws_runs, "live_map": live_map,
        "live_labels": live_labels, "proc_tasks": proc_tasks,
        "history": history, "plan_change": plan_change,
        "taskmap": taskmap, "live_ids": live_ids,
    }


def _orch_idle_ready(ctx, ws: str, state: dict) -> int:
    """Ready tasks with nobody to run them (warn, one per workstream)."""
    from dash import orchestrate

    by_id = state["by_id"]
    try:
        ready = orchestrate.ready_queue(state["body"], state["live_map"])
    except Exception:
        return 0
    tasks = [row for row in ready if by_id.get(row["id"], {}).get("kind") != "gate"]
    if not tasks:
        return 0
    agents: set[str] = set(state["live_labels"])
    for labels in state["proc_tasks"].values():
        agents.update(labels)
    if len(agents) >= 2:
        return 0
    idle_s = float(ctx.dcfg["idle_ready_minutes"]) * 60
    change = state["plan_change"]
    if change is None or ctx.now - change <= idle_s:
        return 0
    top = ", ".join(
        f"{_item_label(by_id[row['id']].get('title'), row['id'])} (chain {row['chain']})"
        for row in tasks[:3]
    )
    who = "no live agent" if not agents else "only 1 live agent"
    title = state["title"]
    _add(
        ctx, "idle-ready", "warn", f"ws:{ws}", ws,
        f"{title} ({ws}) has {len(tasks)} ready tasks with {who}",
        f"{title} ({ws}) has {len(tasks)} ready tasks and {who} mapped to it, "
        f"unchanged for {_orch_age_text(ctx.now - change)} "
        f"(limit {ctx.dcfg['idle_ready_minutes']:g} min). "
        f"Top by critical-path rank: {top}",
        f"python3 agent_dash.py ws next {ws}",
        key="queue",
    )
    return 1


def _orch_done_moment(state: dict, tid: str) -> float | None:
    """Latest done evidence for one task: history, else a satisfied status's runs."""
    from dash import orchestrate

    best: float | None = None
    for row in state["history"]:
        if not isinstance(row, dict) or row.get("task") != tid or row.get("to") != "done":
            continue
        try:
            stamp = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if best is None or stamp > best:
            best = stamp
    if best is None and state["by_id"].get(tid, {}).get("status") in orchestrate.SATISFIED_STATUSES:
        for run in state["by_task"].get(tid, []):
            try:
                stamp = float(run.get("ended_at"))
            except (TypeError, ValueError):
                continue
            if best is None or stamp > best:
                best = stamp
    return best


def _orch_dep_jumps(ctx, ws: str, state: dict) -> int:
    """Tasks that started before a dependency finished (warn recent, info old)."""
    from dash import orchestrate

    by_id = state["by_id"]
    starts: dict[str, float] = {}
    for row in state["history"]:
        if not isinstance(row, dict):
            continue
        task = row.get("task")
        if not isinstance(task, str) or task not in by_id:
            continue
        try:
            stamp = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if task not in starts or stamp < starts[task]:
            starts[task] = stamp
    for tid, rows in state["by_task"].items():
        for run in rows:
            try:
                stamp = float(run.get("started_at"))
            except (TypeError, ValueError):
                continue
            if tid not in starts or stamp < starts[tid]:
                starts[tid] = stamp
    dones: dict[str, float] = {}
    for tid in by_id:
        moment = _orch_done_moment(state, tid)
        if moment is not None:
            dones[tid] = moment
    try:
        jumps = orchestrate.dep_jumps(state["body"], starts, dones)
    except Exception:
        return 0
    grouped: dict[str, list] = {}
    for jump in jumps:
        grouped.setdefault(jump["task"], []).append(jump)
    recent_cut = ctx.now - float(ctx.dcfg["recent_hours"]) * 3600
    count = 0
    for tid in sorted(grouped):
        parts = []
        for jump in grouped[tid]:
            dep = jump["dep"]
            dep_task = by_id.get(dep, {})
            dep_label = _item_label(dep_task.get("title"), dep)
            done_at = jump.get("dep_done_at")
            if done_at is None:
                when = f"still {dep_task.get('status')}"
            else:
                when = "finished " + time.strftime("%Y-%m-%d %H:%M", time.gmtime(done_at)) + " UTC"
            parts.append(f"{dep_label} ({when})")
        start = starts.get(tid, 0.0)
        started = time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(start))) + " UTC"
        _group_add(
            ctx, "dep-jump",
            "warn" if start >= recent_cut else "info",
            f"ws:{ws}", ws,
            by_id[tid].get("title"), tid, start,
            f"started at {started} before {'; '.join(parts)}",
        )
        count += 1
    return 1 if count else 0


def _orch_stale_gates(ctx, ws: str, state: dict) -> int:
    """Gates whose freshness bound expired while still unmet (warn)."""
    from dash import orchestrate

    try:
        gates = orchestrate.stale_gates(state["body"], ctx.now)
    except Exception:
        return 0
    by_id = state["by_id"]
    count = 0
    for gate in gates:
        if not gate.get("stale"):
            continue
        task = by_id.get(gate["id"], {})
        if task.get("status") == "done":
            continue
        label = _item_label(task.get("title"), gate["id"])
        blocked = [
            _item_label(by_id[dep].get("title"), dep)
            for dep in (gate.get("blocked") or [])[:3]
            if dep in by_id
        ]
        extra = len(gate.get("blocked") or []) - len(blocked)
        if extra > 0:
            blocked.append(f"+{extra} more")
        confirmed = gate.get("confirmed_at") or "never confirmed"
        _add(
            ctx, "stale-gate", "warn", f"ws:{ws}", ws,
            f"{label} confirmation is stale",
            f"{label} in {ws} waits on {gate.get('signal') or 'an unnamed signal'}; "
            f"last confirmation {confirmed} is past its {gate.get('fresh_hours')} h bound. "
            f"Blocks: {', '.join(blocked) if blocked else 'nothing yet'}",
            f"python3 agent_dash.py ws gates {ws}",
            key=gate["id"],
        )
        count += 1
    return count


def _orch_long_tail(ctx, ws: str, state: dict) -> int:
    """A wave finished except one still-running lane (info)."""
    from dash import orchestrate

    by_id = state["by_id"]
    try:
        wave_list = orchestrate.waves(state["body"])
    except Exception:
        return 0
    tail_s = float(ctx.dcfg["long_tail_minutes"]) * 60
    live_tasks = set(state["live_map"]) | set(state["proc_tasks"])
    count = 0
    for wave in wave_list:
        lanes = [tid for tid in wave if tid in by_id and by_id[tid].get("status") != "deferred"]
        if len(lanes) < 2:
            continue
        done_at = {}
        for tid in lanes:
            if by_id[tid].get("status") not in orchestrate.SATISFIED_STATUSES:
                continue
            moment = _orch_done_moment(state, tid)
            if moment is not None:
                done_at[tid] = moment
        open_lanes = [tid for tid in lanes if tid not in done_at]
        if len(open_lanes) != 1:
            continue
        lane = open_lanes[0]
        if by_id[lane].get("status") not in orchestrate.ACTIVE_STATUSES:
            continue
        if lane not in live_tasks:
            continue
        if not done_at or any(ctx.now - moment <= tail_s for moment in done_at.values()):
            continue
        label = _item_label(by_id[lane].get("title"), lane)
        finished = ", ".join(
            f"{_item_label(by_id[tid].get('title'), tid)} "
            f"({_orch_age_text(ctx.now - moment)} ago)"
            for tid, moment in sorted(done_at.items())
        )
        live_label = state["live_map"].get(lane) or (state["proc_tasks"].get(lane) or [""])[0]
        _add(
            ctx, "long-tail", "info", f"ws:{ws}", ws,
            f"{label} still runs while its wave finished long ago",
            f"{label} in {ws} still runs ({live_label}) while the rest of its wave "
            f"finished more than {_orch_age_text(tail_s)} ago: {finished}. "
            "Split the lane or reassign it",
            f"python3 agent_dash.py ws waves {ws}",
            key=lane,
        )
        count += 1
    return count


def _orch_age_text(seconds: float) -> str:
    """Short age for problem detail: minutes under two hours, else hours."""
    if seconds >= 7200:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.0f} min"


def _orch_review_spiral(ctx, ws: str, state: dict) -> int:
    """A task past the fix/review round cap (warn, open tasks only)."""
    from dash import orchestrate

    cap = int(ctx.dcfg["fix_rounds_cap"])
    by_id = state["by_id"]
    count = 0
    for tid in sorted(state["by_task"]):
        if by_id.get(tid, {}).get("status") not in ("todo", "wip", "pr"):
            continue
        rounds: set[tuple[str, int]] = set()
        for run in state["by_task"][tid]:
            for marker in orchestrate.label_rounds(run.get("label")):
                rounds.add(marker)
        for label in state["proc_tasks"].get(tid, []):
            for marker in orchestrate.label_rounds(label):
                rounds.add(marker)
        if len(rounds) <= cap:
            continue
        label = _item_label(by_id[tid].get("title"), tid)
        shown = ", ".join(f"{kind}{num}" for kind, num in sorted(rounds)[:6])
        top = max(num for _kind, num in rounds)
        _add(
            ctx, "review-spiral", "warn", f"ws:{ws}", ws,
            f"{label} is past the fix-round cap ({len(rounds)} rounds, cap {cap})",
            f"{label} in {ws} has {len(rounds)} fix/review rounds ({shown}), "
            f"latest round {top}. The third round goes to the lead: "
            "take the lane over or re-brief it",
            f"python3 agent_dash.py ws plan {ws}",
            key=tid,
        )
        count += 1
    return count


def _orch_ghost_runs(ctx, ws: str, state: dict) -> int:
    """Recent runs mapping to no plan task (info, one per workstream)."""
    from dash.taskmatch import task_of

    recent_s = float(ctx.dcfg["recent_hours"]) * 3600
    cutoff = ctx.now - recent_s
    window = []
    for run in state["ws_runs"]:
        recent = False
        for key in ("started_at", "ended_at"):
            try:
                stamp = float(run.get(key))
            except (TypeError, ValueError):
                continue
            if stamp >= cutoff:
                recent = True
                break
        if recent:
            window.append(run)
    if not window:
        return 0
    by_id = state["by_id"]
    ghosts = []
    for run in window:
        try:
            tid = task_of(run.get("label") or "", ws, state["taskmap"], state["order"])
        except Exception:
            tid = None
        if not isinstance(tid, str) or tid not in by_id:
            ghosts.append(run)
    if not ghosts:
        return 0
    share = len(ghosts) / len(window)
    if share < float(ctx.dcfg["ghost_share"]):
        return 0
    for run in ghosts:
        label = str(run.get("label") or run.get("run_id"))
        try:
            stamp = float(run.get("started_at") or run.get("ended_at") or 0)
        except (TypeError, ValueError):
            stamp = 0.0
        _group_add(
            ctx, "ghost-runs", "info", f"ws:{ws}", ws,
            label, str(run.get("run_id") or label), stamp,
            f"{len(ghosts)} of {len(window)} recent runs map to no task "
            f"({share * 100:.0f}% >= {float(ctx.dcfg['ghost_share']) * 100:.0f}%)",
        )
    return 1


def _orch_lead(ctx, ws: str, state: dict) -> int:
    """A live lead flagged LOW-DELEGATION or CONTEXT>250k (warn)."""
    if ctx.store is None:
        return 0
    if not any(str(run.get("run_id")) in state["live_ids"] for run in state["ws_runs"]):
        return 0
    from dash import orchestrate

    try:
        profile = orchestrate.lead_profile(ctx.store, ws=ws, now=ctx.now)
    except Exception:
        return 0
    if not isinstance(profile, dict):
        return 0
    flags = [flag for flag in profile.get("flags") or [] if flag in ("LOW-DELEGATION", "CONTEXT>250k")]
    if not flags:
        return 0
    sid = str(profile.get("session_id") or "")
    title = state["title"]
    launched = profile.get("workers_launched")
    calls = profile.get("own_tool_calls")
    tokens = profile.get("context_tokens")
    _add(
        ctx, "lead-undelegated", "warn", f"ws:{ws}", ws,
        f"{title} ({ws}) lead shows {', '.join(flags)}",
        f"lead {sid[:8] or 'unknown'} in {ws} launched {launched} workers "
        f"with {calls} own tool calls and {tokens} context tokens: "
        f"{', '.join(flags)}. Hand multi-step jobs to workers; "
        "re-brief or start a successor past the context limit",
        f"python3 agent_dash.py lead-profile --ws {ws}",
        key=sid or "lead",
    )
    return 1


def _orch_deferred_orphan(ctx, row: dict, states: dict, recent_s: float) -> int:
    """A deferred task nobody picked up (info, older than the window)."""
    if not isinstance(row, dict) or not row.get("orphan"):
        return 0
    ws, tid = row.get("ws"), row.get("id")
    state = states.get(ws) if isinstance(ws, str) else None
    if state is None or not isinstance(tid, str) or tid not in state["by_id"]:
        return 0
    defer_at: float | None = None
    for hist in state["history"]:
        if not isinstance(hist, dict) or hist.get("task") != tid or hist.get("to") != "deferred":
            continue
        try:
            stamp = float(hist.get("ts"))
        except (TypeError, ValueError):
            continue
        if defer_at is None or stamp > defer_at:
            defer_at = stamp
    if defer_at is None:
        defer_at = state["plan_change"]
    if defer_at is None or ctx.now - defer_at <= recent_s:
        return 0
    task = state["by_id"][tid]
    label = _item_label(task.get("title"), tid)
    moved = row.get("moved_to") or "no destination"
    received = row.get("received_as") or "none recorded"
    note = row.get("note") or "no note"
    _add(
        ctx, "deferred-orphan", "info", f"ws:{ws}", ws,
        f"{label} deferred to {moved} has no receiving task",
        f"{label} in {ws} was deferred {_orch_age_text(ctx.now - defer_at)} ago "
        f"(moved_to={moved}, received_as={received}, note={note}) "
        "and no receiving workstream picked it up",
        f"python3 agent_dash.py ws deferred {ws}",
        key=tid,
    )
    return 1


LIFECYCLE_STATES = ("planned", "active", "paused", "done", "archived", "dropped")
LIFECYCLE_DONE_STATES = ("done", "archived")
_LIFECYCLE_META_KEYS = (
    "state", "state_changed_at", "archived_at", "summary", "depends_on", "milestone",
)
_LIFECYCLE_CODES = (
    "done-not-archived", "active-idle", "planned-blocked", "planned-ready", "planned-live",
    "phase-slipping", "unplanned-activity",
)


def _suggest_title(ws: str) -> str:
    """Human title suggestion for ``ws new`` when a run id has no bundle."""
    text = str(ws or "").strip()
    upper = text.upper()
    if upper == "TOOLS":
        return "Shared tooling"
    if upper == "PLANNING":
        return "Planning lane"
    return f"{text} work" if text else "Untitled work"


def _looks_shared_lane(ws: str) -> bool:
    """True when an id reads like a shared tool or planning lane, not a WS number."""
    import re as _re

    return not bool(_re.match(r"(?i)^ws\d+$", str(ws or "").strip()))


def _lifecycle_meta(ws: str) -> dict:
    """Lifecycle fields from ``bundle.json``. Absent state reads as active."""
    from dash.bundles import bundle_dir

    meta = {
        "state": "active",
        "state_changed_at": None,
        "archived_at": None,
        "depends_on": [],
        "milestone": None,
        "has_lifecycle": False,
    }
    try:
        path = bundle_dir(ws)
    except Exception:
        return meta
    if path is None:
        return meta
    try:
        raw = json.loads((path / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return meta
    if not isinstance(raw, dict):
        return meta
    meta["has_lifecycle"] = any(key in raw for key in _LIFECYCLE_META_KEYS)
    state = raw.get("state")
    if isinstance(state, str) and state in LIFECYCLE_STATES:
        meta["state"] = state
    for key in ("state_changed_at", "archived_at"):
        try:
            stamp = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        if stamp > 0:
            meta[key] = stamp
    deps = raw.get("depends_on")
    if isinstance(deps, list):
        meta["depends_on"] = [str(item) for item in deps if isinstance(item, str) and item]
    milestone = raw.get("milestone")
    if isinstance(milestone, str) and milestone:
        meta["milestone"] = milestone
    return meta


def _lifecycle_history(ws: str) -> tuple[float | None, float | None]:
    """(last move, last done move) over raw ``history.jsonl`` rows. Never raises.

    Reads the file directly instead of ``replay.read_history`` because the
    lifecycle ``{kind: state}`` lines carry no task id and that reader drops
    them; the nag only needs the newest stamp of each kind.
    """
    from dash.bundles import bundle_dir

    try:
        path = bundle_dir(ws)
    except Exception:
        return None, None
    if path is None:
        return None, None
    try:
        text = (path / "history.jsonl").read_text(encoding="utf-8")
    except OSError:
        return None, None
    last_any: float | None = None
    last_done: float | None = None
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
            stamp = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if stamp <= 0:
            continue
        if last_any is None or stamp > last_any:
            last_any = stamp
        if row.get("to") == "done" and (last_done is None or stamp > last_done):
            last_done = stamp
    return last_any, last_done


def _lifecycle_plan(ws: str) -> tuple[dict | None, float | None]:
    """(tasks-or-None, plan mtime-or-None) for one bundle. Never raises."""
    try:
        tasks, _plan_ids, mtime, _history, error = _bundle_task_map(ws)
    except Exception:
        return None, None
    if error or tasks is None:
        return None, mtime
    return tasks, mtime


def _lifecycle_project_exists() -> bool:
    """Whether ``project.json`` sits beside the bundles. Never raises."""
    from dash.bundles import workstreams_root

    try:
        root = workstreams_root()
    except Exception:
        return False
    try:
        raw = json.loads((root / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict)


def _lifecycle_phases() -> list | None:
    """Phase entries from ``project.json``, or None. Never raises."""
    from dash.bundles import workstreams_root

    try:
        root = workstreams_root()
    except Exception:
        return None
    try:
        raw = json.loads((root / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    phases = raw.get("phases") if isinstance(raw, dict) else None
    if not isinstance(phases, list):
        return None
    return [
        entry for entry in phases
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]
    ]


def _lifecycle_target(entry: dict) -> tuple[float | None, str]:
    """(epoch-or-None, raw text) for a phase target date. Never raises."""
    for key in ("target", "target_date", "due", "deadline"):
        value = entry.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return (float(value), str(value)) if float(value) > 0 else (None, "")
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        try:
            stamp = float(text)
            return (stamp, text) if stamp > 0 else (None, "")
        except ValueError:
            pass
        try:
            parsed = time.strptime(text[:10], "%Y-%m-%d")
            return calendar.timegm(parsed), text
        except (ValueError, OverflowError):
            continue
    return None, ""


def _lifecycle_done_nag(ctx, ws: str, meta: dict) -> int:
    """A done (or 100% active) workstream waiting past the archive nag (warn)."""
    tasks, mtime = _lifecycle_plan(ws)
    hist_any, hist_done = _lifecycle_history(ws)
    if meta["state"] == "active":
        if not tasks:
            return 0
        total = sum(1 for info in tasks.values() if info.get("status") != "deferred")
        done = sum(1 for info in tasks.values() if info.get("status") == "done")
        if total <= 0 or done != total:
            return 0
        # An active bundle's state_changed_at is when it started, not when it
        # reached 100%, so the nag clock is the completion moment instead.
        base = hist_done or hist_any or mtime
    else:
        base = meta.get("state_changed_at") or hist_any or mtime
    if base is None:
        return 0
    nag_s = float(ctx.dcfg["archive_nag_days"]) * 86400
    if ctx.now - base <= nag_s:
        return 0
    days = (ctx.now - base) / 86400
    title = _ws_title(ctx, ws)
    if meta["state"] == "done":
        heading = f"{title} ({ws}) is done but not archived"
    else:
        heading = f"{title} ({ws}) is 100% complete but not archived"
    _add(
        ctx, "done-not-archived", "warn", f"ws:{ws}", ws,
        heading,
        f"{title} ({ws}) has been done for {days:.1f} days "
        f"(nag limit {ctx.dcfg['archive_nag_days']:g} days). Write docs/closing-report.md "
        "in the bundle, then archive it",
        f"python3 agent_dash.py ws archive {ws}",
    )
    return 1


def _lifecycle_idle(ctx, ws: str, meta: dict, runs: list) -> int:
    """An active workstream with open tasks but no recent activity (warn)."""
    tasks, mtime = _lifecycle_plan(ws)
    if not tasks:
        return 0
    total = sum(1 for info in tasks.values() if info.get("status") != "deferred")
    done = sum(1 for info in tasks.values() if info.get("status") == "done")
    if total <= 0 or total - done <= 0:
        return 0
    last: float | None = None
    for run in runs:
        if not isinstance(run, dict) or run.get("workstream") != ws:
            continue
        for key in ("started_at", "ended_at"):
            try:
                stamp = float(run.get(key))
            except (TypeError, ValueError):
                continue
            if stamp > 0 and (last is None or stamp > last):
                last = stamp
    hist_any, _hist_done = _lifecycle_history(ws)
    if hist_any is not None and (last is None or hist_any > last):
        last = hist_any
    if last is None:
        last = mtime
    if last is None:
        return 0
    idle_s = float(ctx.dcfg["active_idle_days"]) * 86400
    if ctx.now - last <= idle_s:
        return 0
    days = (ctx.now - last) / 86400
    title = _ws_title(ctx, ws)
    _add(
        ctx, "active-idle", "warn", f"ws:{ws}", ws,
        f"{title} ({ws}) is active but idle",
        f"{title} ({ws}) has no runs or history moves for {days:.1f} days "
        f"(limit {ctx.dcfg['active_idle_days']:g} days) with {total - done} of {total} "
        "tasks still open. Start work, or pause it with a note",
        f"python3 agent_dash.py ws next {ws}",
    )
    return 1


def _lifecycle_planned(ctx, ws: str, meta: dict, metas: dict) -> tuple[int, int]:
    """A planned workstream: blocked (info) or ready to start (info)."""
    unmet = []
    for dep in meta.get("depends_on") or []:
        dep_meta = metas.get(dep)
        dep_state = dep_meta["state"] if dep_meta is not None else None
        if dep_state not in LIFECYCLE_DONE_STATES:
            unmet.append(dep)
    stale: list = []
    body: dict | None = None
    try:
        from dash import orchestrate
        from dash.bundles import resolve_plan

        _source, body = resolve_plan(ws)
        gates = orchestrate.stale_gates(body, ctx.now) if isinstance(body, dict) else []
        stale = [gate for gate in gates if isinstance(gate, dict) and gate.get("stale")]
    except Exception:
        stale = []
    title = _ws_title(ctx, ws)
    if unmet or stale:
        waits = []
        for dep in unmet:
            if dep in metas:
                waits.append(f"{_ws_title(ctx, dep)} ({dep})")
            else:
                waits.append(f"unknown workstream ({dep})")
        try:
            from dash import orchestrate as _orch

            _by_id, _deps, _order = _orch.plan_tasks(body if isinstance(body, dict) else {})
        except Exception:
            _by_id = {}
        for gate in stale:
            gid = str(gate.get("id") or "")
            signal = gate.get("signal") or "an unnamed signal"
            waits.append(f"{_item_label(_by_id.get(gid, {}).get('title'), gid)} waits on {signal}")
        _add(
            ctx, "planned-blocked", "info", f"ws:{ws}", ws,
            f"{title} ({ws}) is planned and waiting",
            f"{title} ({ws}) is planned and waits on: "
            + "; ".join(waits)
            + ". Check gate freshness, then activate it when ready",
            f"python3 agent_dash.py ws gates {ws}",
        )
        return 1, 0
    deps = meta.get("depends_on") or []
    if deps:
        dep_text = f"all {len(deps)} {_plural(len(deps), 'dependency', 'dependencies')} done or archived"
    else:
        dep_text = "no dependencies"
    _add(
        ctx, "planned-ready", "info", f"ws:{ws}", ws,
        f"{title} ({ws}) is planned and ready to start",
        f"{title} ({ws}) is ready to start: {dep_text}, no stale gates",
        f"python3 agent_dash.py ws state {ws} active",
    )
    return 0, 1


def _lifecycle_planned_live(ctx, ws: str, meta: dict, runs: list) -> int:
    """A planned workstream with runs behind it: the first agent started.

    The dashboard never writes bundles, so the state stays ``planned``
    until a human runs ``ws state <ID> active``. This nag names that
    command and the earliest run start, so the Current view hint and the
    CLI agree. One problem per workstream, no member rows.
    """
    first: float | None = None
    count = 0
    for run in runs or []:
        if not isinstance(run, dict) or run.get("workstream") != ws:
            continue
        count += 1
        try:
            stamp = float(run.get("started_at") or 0)
        except (TypeError, ValueError):
            continue
        if stamp > 0 and (first is None or stamp < first):
            first = stamp
    if not count or first is None:
        return 0
    title = _ws_title(ctx, ws)
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(first)) + " UTC"
    _add(
        ctx, "planned-live", "warn", f"ws:{ws}", ws,
        f"{title} ({ws}) is planned but has runs",
        f"{title} ({ws}) is still planned but its first agent started {when} "
        f"({count} {_plural(count, 'run', 'runs')} recorded). The page shows it "
        f"under Current until the state moves. Activate it with",
        f"python3 agent_dash.py ws state {ws} active",
    )
    return 1


def _collect_unplanned(ctx, bundles, runs) -> int:
    """Runs attributed to an id with no plan bundle (one finding per id).

    Ids on the project's ``ignore_workstreams`` list never fire here
    (or anywhere else workstream-scoped): existing rows for them
    resolve on the next snapshot because they are absent from the run.
    Shared lanes (an id without a workstream number, such as a tool or
    planning lane) get the same ``ws new`` command with a lane-style
    suggested title; the detail says to keep it as a workstream bundle
    or, when it is truly shared, to record it as a program bundle by
    setting ``kind`` to ``program`` in ``bundle.json`` after creation,
    or to ignore the id instead.
    """
    have = set()
    for item in bundles or []:
        ws = item.get("id") if isinstance(item, dict) else None
        if isinstance(ws, str) and ws:
            have.add(ws)
    by_ws: dict[str, list] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        ws = run.get("workstream")
        if not isinstance(ws, str) or not ws or ws == "unassigned":
            continue
        if ws in have:
            continue
        by_ws.setdefault(ws, []).append(run)
    count = 0
    for ws in sorted(by_ws):
        if _ignored_ws(ctx, ws):
            continue
        rows = by_ws[ws]
        first: float | None = None
        for run in rows:
            try:
                stamp = float(run.get("started_at") or run.get("ended_at") or 0)
            except (TypeError, ValueError):
                continue
            if stamp > 0 and (first is None or stamp < first):
                first = stamp
        title = _suggest_title(ws)
        when = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(first)) + " UTC") if first else "recently"
        detail = (
            f"Active without a plan: {ws} has {len(rows)} "
            f"{_plural(len(rows), 'run', 'runs')} recorded (first {when}) "
            f"but no plan bundle. Create it with: "
            f"python3 agent_dash.py ws new {ws} \"{title}\" "
            f"or ignore it: python3 agent_dash.py project ignore add {ws}"
        )
        if _looks_shared_lane(ws):
            detail += (
                ". This id looks like a shared tool or planning lane: if it is "
                "shared rather than one workstream, set \"kind\" to \"program\" "
                "in bundle.json after creation"
            )
        _add(
            ctx, "unplanned-activity", "warn", f"ws:{ws}", ws,
            f"Active without a plan: {ws}",
            detail,
            f"python3 agent_dash.py ws new {ws} \"{title}\"",
        )
        count += 1
    return count


def _lifecycle_phase_members(entry: dict, metas: dict) -> list[str]:
    """Member workstream ids for a phase: explicit list, else milestone match."""
    raw = entry.get("workstreams")
    if isinstance(raw, list) and raw:
        return sorted({str(item) for item in raw if isinstance(item, str) and item in metas})
    name = entry.get("name")
    return sorted(ws for ws, meta in metas.items() if meta.get("milestone") == name)


def _lifecycle_done_moment(ctx, ws: str, metas: dict) -> float | None:
    """When a workstream finished: state move, else history, else plan mtime."""
    hist_any, _hist_done = _lifecycle_history(ws)
    _tasks, mtime = _lifecycle_plan(ws)
    return metas[ws].get("state_changed_at") or hist_any or mtime


def _lifecycle_phases_check(ctx, metas: dict, phases: list | None) -> int:
    """Finished phases whose successor never started, and passed targets (info)."""
    if phases is None:
        return 0
    count = 0
    slip_s = float(ctx.dcfg["phase_slip_days"]) * 86400
    for index, entry in enumerate(phases):
        name = entry.get("name")
        members = _lifecycle_phase_members(entry, metas)
        target, raw_target = _lifecycle_target(entry)
        if target is not None and members and ctx.now > target:
            unfinished = [ws for ws in members if metas[ws]["state"] not in LIFECYCLE_DONE_STATES]
            if unfinished:
                shown = ", ".join(f"{_ws_title(ctx, ws)} ({ws})" for ws in unfinished[:3])
                if len(unfinished) > 3:
                    shown += f" (+{len(unfinished) - 3} more)"
                _add(
                    ctx, "phase-slipping", "info", "global", None,
                    f"Phase {name} passed its target with work still open",
                    f"phase {name} passed its target ({raw_target}) with unfinished "
                    f"workstreams: {shown}. Review the plan",
                    "python3 agent_dash.py project show",
                    key=f"target:{name}",
                )
                count += 1
        if index + 1 < len(phases) and members and all(
            metas[ws]["state"] in LIFECYCLE_DONE_STATES for ws in members
        ):
            nxt = phases[index + 1]
            nxt_name = nxt.get("name")
            nxt_members = _lifecycle_phase_members(nxt, metas)
            if not nxt_members:
                continue
            if any(metas[ws]["state"] != "planned" for ws in nxt_members):
                continue
            moments = [_lifecycle_done_moment(ctx, ws, metas) for ws in members]
            if any(moment is None for moment in moments):
                continue
            finished = max(moment for moment in moments if moment is not None)
            if ctx.now - finished <= slip_s:
                continue
            days = (ctx.now - finished) / 86400
            _add(
                ctx, "phase-slipping", "info", "global", None,
                f"Phase {name} is done but {nxt_name} has not started",
                f"phase {name} finished {days:.1f} days ago "
                f"(limit {ctx.dcfg['phase_slip_days']:g} days) and phase {nxt_name} "
                f"has not started ({len(nxt_members)} planned "
                f"{_plural(len(nxt_members), 'workstream', 'workstreams')}). Review the plan",
                "python3 agent_dash.py project show",
                key=f"after:{name}",
            )
            count += 1
    return count


def _collect_lifecycle(ctx) -> None:
    """Nags for workstream lifecycle states and slipping project phases.

    Silent for projects without lifecycle data (no lifecycle keys in any
    ``bundle.json`` and no ``project.json``). Reuses the already-loaded
    bundles and the cached run rows; each bundle costs one small metadata
    read, plus plan and history reads only for bundles in a state the
    checks inspect. Absent ``state`` reads as ``active``.
    """
    metas: dict[str, dict] = {}
    for item in ctx._bundles or []:
        ws = item.get("id") if isinstance(item, dict) else None
        if isinstance(ws, str) and ws:
            metas[ws] = _lifecycle_meta(ws)
    project = _lifecycle_project_exists()
    phases = _lifecycle_phases() if project else None
    if not any(meta.get("has_lifecycle") for meta in metas.values()) and not project:
        for name in [code for code in _LIFECYCLE_CODES if code != "unplanned-activity"]:
            _check(ctx, name, True, "no lifecycle data")
        runs = _runs_ro(ctx)
        found = _collect_unplanned(ctx, ctx._bundles or [], runs)
        _check(ctx, "unplanned-activity", found == 0, f"{found} open" if found else "clean")
        return
    runs = _runs_ro(ctx)
    counts = {"done-not-archived": 0, "active-idle": 0, "planned-blocked": 0, "planned-ready": 0, "planned-live": 0, "unplanned-activity": 0}
    for ws in sorted(metas):
        state = metas[ws]["state"]
        if state in ("done", "active"):
            counts["done-not-archived"] += _lifecycle_done_nag(ctx, ws, metas[ws])
        if state == "active":
            counts["active-idle"] += _lifecycle_idle(ctx, ws, metas[ws], runs)
        if state == "planned":
            counts["planned-live"] += _lifecycle_planned_live(ctx, ws, metas[ws], runs)
            blocked, ready = _lifecycle_planned(ctx, ws, metas[ws], metas)
            counts["planned-blocked"] += blocked
            counts["planned-ready"] += ready
    counts["unplanned-activity"] += _collect_unplanned(ctx, ctx._bundles or [], runs)
    for code in ("done-not-archived", "active-idle", "planned-blocked", "planned-ready", "planned-live", "unplanned-activity"):
        total = counts[code]
        _check(
            ctx, code, total == 0,
            f"{total} open" if total else "clean",
        )
    slipping = _lifecycle_phases_check(ctx, metas, phases)
    _check(
        ctx, "phase-slipping", slipping == 0,
        f"{slipping} open" if slipping else ("clean" if phases else "no project phases"),
    )


def model_policy_reason(kind: str | None, model: str | None, reasoning_effort: str | None, policy: dict | None) -> str | None:
    """Why a worker violates the model policy, or None when it complies.

    Read-only and argv-blind: it judges already-extracted flag values and
    reports the worker label, never the rest of argv. ``muse`` workers
    require ``--reasoning-effort xhigh`` (or the configured level);
    ``cursor`` workers running a ``grok-4.7`` model must use the bracketed
    500k form, never a bare ``grok-4.7-high`` or a ``-fast`` id.
    """
    policy = policy if isinstance(policy, dict) else {}
    if kind == "muse":
        want = str(policy.get("muse_reasoning_effort", "xhigh") or "xhigh")
        if (reasoning_effort or "") != want:
            return (
                f"muse worker needs --reasoning-effort {want} "
                f"(got {reasoning_effort or 'none'})"
            )
        return None
    if kind == "cursor" and model and "grok-4.7" in model:
        if not policy.get("grok_require_bracketed_500k", True):
            return None
        import re as _re

        if model == "grok-4.7-high" or "[" not in model or "500k" not in model:
            return (
                "cursor-agent model must be the bracketed 500k form "
                f"(grok-4.7[context=500k,...]), never a bare grok-4.7-high (got {model})"
            )
        if _re.search(r"-fast([^\w]|$)", model):
            return (
                "cursor-agent model must be the bracketed 500k form with "
                f"fast=false, never a -fast id (got {model})"
            )
    return None


def _comm_kind(comm: str | None) -> str | None:
    """Worker kind for a process comm: muse, cursor, grok-build, or None."""
    if comm in ("muse", "muse-bin") or (isinstance(comm, str) and comm.startswith("muse-bin-")):
        return "muse"
    if comm == "cursor-agent":
        return "cursor"
    if comm == "grok":
        return "grok-build"
    return None


def _live_argv_tokens(ctx) -> dict:
    """Pid -> {model, reasoning_effort} from argv. Empty when ps fails."""
    if ctx.argv_tokens is not None:
        return ctx.argv_tokens
    try:
        from dash.sources.process import _tokens_by_pid

        return _tokens_by_pid()
    except Exception:
        return {}


def _live_ppids(ctx) -> dict:
    if ctx.ppids is not None:
        return ctx.ppids
    try:
        from dash.sources.process import ppid_map

        return ppid_map()
    except Exception:
        return {}


def _pgrep_count(pattern: str) -> int:
    """Live processes matching a worker pgrep pattern. Read-only."""
    if not pattern:
        return 0
    cmd = ["pgrep", "-f"]
    if any(ch in pattern for ch in "|()"):
        cmd.append("-E")
    cmd.append(pattern)
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode not in (0, 1):
        return 0
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def _workers_registry():
    """The worker registry: local override wins, like the launcher."""
    from dash.locate import layered_path

    path = layered_path("workers", "AGENT_DASH_WORKERS")
    if path is None or not path.is_file():
        path = TOOL_ROOT / "workers.example.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("workers"), dict):
        return None, None
    return data, path


def effort_from_extra_args(worker: dict) -> str | None:
    """The ``--reasoning-effort`` value in a registry worker's extra_args."""
    extra = worker.get("extra_args") if isinstance(worker, dict) else None
    if not isinstance(extra, list):
        return None
    for index, arg in enumerate(extra):
        if not isinstance(arg, str):
            continue
        if arg == "--reasoning-effort" and index + 1 < len(extra):
            value = extra[index + 1]
            return str(value) if isinstance(value, str) else None
        if arg.startswith("--reasoning-effort="):
            return arg.split("=", 1)[1] or None
    return None


def _collect_fleet(ctx) -> None:
    if ctx.processes is None:
        _check(ctx, "fleet", True, "skipped, no process listing")
        return
    policy = ctx.dcfg["model_policy"]
    tokens = _live_argv_tokens(ctx)
    ppids = _live_ppids(ctx)
    runs = _runs_ro(ctx)
    by_label: dict[str, list[dict]] = {}
    for row in runs:
        if row.get("label"):
            by_label.setdefault(str(row["label"]), []).append(row)
    count = 0
    for proc in ctx.processes:
        if not isinstance(proc, dict) or not proc.get("label"):
            continue
        label = str(proc["label"])
        try:
            age = ctx.now - float(proc.get("started_at"))
        except (TypeError, ValueError):
            age = None
        kind = _comm_kind(proc.get("comm"))
        token = tokens.get(proc.get("pid")) or {}
        model = token.get("model") or proc.get("model")
        effort = token.get("reasoning_effort")
        if effort is None:
            effort = proc.get("reasoning_effort")
        reason = model_policy_reason(kind, model, effort, policy)
        if reason is not None:
            try:
                started = float(proc.get("started_at") or 0)
            except (TypeError, ValueError):
                started = 0.0
            _group_add(
                ctx, "model-policy", "warn", "global", None,
                label, label, started or ctx.now, reason,
            )
            count += 1
        if age is not None and age >= ORPHAN_MIN_AGE_S:
            ended = any(
                row.get("status") in TERMINAL_RUN for row in by_label.get(label, [])
            )
            reparented = ppids.get(proc.get("pid")) == 1
            if ended or reparented:
                minutes = age / 60
                why = "its run row ended" if ended else "its parent is pid 1"
                try:
                    started = float(proc.get("started_at") or 0)
                except (TypeError, ValueError):
                    started = 0.0
                _group_add(
                    ctx, "orphan-worker", "warn", "global", None,
                    label, label, started or ctx.now,
                    f"{proc.get('comm')} pid {proc.get('pid')} alive "
                    f"{minutes:.0f} min though {why}",
                )
                count += 1
    registry = ctx.workers_registry
    reg_path = None
    if registry is None:
        registry, reg_path = _workers_registry()
    counts = ctx.pgrep_counts if ctx.pgrep_counts is not None else {}
    if isinstance(registry, dict):
        for name, worker in (registry.get("workers") or {}).items():
            if not isinstance(worker, dict):
                continue
            try:
                cap = int(worker.get("cap"))
            except (TypeError, ValueError):
                continue
            pattern = worker.get("pgrep") or ""
            if pattern in counts:
                live = counts[pattern]
            else:
                live = _pgrep_count(pattern)
                counts[pattern] = live
            if live > cap:
                where = f" in {reg_path}" if reg_path is not None else ""
                _group_add(
                    ctx, "over-cap", "warn", "global", None,
                    f"tier {name}", str(name), ctx.now,
                    f"tier {name}{where} has {live} live workers above cap {cap}",
                )
                count += 1
    _check(
        ctx, "fleet", count == 0,
        f"{len(ctx.processes)} live workers checked, {count} fleet problems" if count
        else f"{len(ctx.processes)} live workers checked, fleet clean",
    )


class DoctorScheduler:
    """Background doctor: collect, snapshot, sleep. Started by ``run``."""

    def __init__(self, store, registry, cfg: dict | None = None, build: dict | None = None):
        self.store = store
        self.registry = registry
        self.cfg = cfg if isinstance(cfg, dict) else {}
        # The server's captured build, so the build checks see the
        # running build without re-reading git (the registry health the
        # scheduler collects from carries no build id of its own).
        self.build = dict(build) if isinstance(build, dict) else None
        self.interval_s = doctor_cfg(self.cfg)["interval_s"]
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._next_run_at: float | None = None

    def start(self) -> "DoctorScheduler":
        self._started_at = time.time()
        self._next_run_at = self._started_at + FIRST_RUN_DELAY_S
        self._thread = threading.Thread(target=self._loop, name="doctor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._refresh.set()

    def queue_refresh(self) -> None:
        """``GET /api/doctor?refresh=1`` asks for a run; serving never blocks on it."""
        self._refresh.set()

    @property
    def next_run_at(self) -> float | None:
        return self._next_run_at

    def run_once(self, now: float | None = None) -> dict:
        now = float(now if now is not None else time.time())
        try:
            health = self.registry.health(now, self.cfg)
        except Exception:
            health = None
        if isinstance(health, dict) and isinstance(self.build, dict) and self.build.get("build_id"):
            health = dict(health)
            health.setdefault("build_id", self.build["build_id"])
            health.setdefault("started_at", self.build.get("started_at"))
        try:
            from dash.sources.process import live_worker_processes

            processes = live_worker_processes() or []
        except Exception:
            processes = []
        result = collect_problems(
            self.store, self.cfg, now, health=health, registry=self.registry,
            processes=processes,
        )
        try:
            payload = self.store.doctor_snapshot(result["problems"], now, self.interval_s)
        except Exception:
            log.exception("doctor snapshot failed")
            payload = self.store.doctor_latest(now)
        self._next_run_at = now + self.interval_s
        return payload

    def latest(
        self,
        now: float,
        ws: str | None = None,
        min_severity: str | None = None,
        include_all: bool = False,
    ) -> dict:
        try:
            payload = self.store.doctor_latest(now, ws, min_severity, include_all)
        except TypeError:
            payload = self.store.doctor_latest(now, ws, min_severity)
        if self._next_run_at is not None:
            payload["next_run_at"] = self._next_run_at
        return payload

    def _full_poll_done(self) -> bool:
        # Same definition as the registry "starting" state: no source may
        # still be waiting for its first completed poll.
        from dash.sources import has_completed_poll

        try:
            state = getattr(self.registry, "state", None) or {}
        except Exception:
            return True
        return all(
            isinstance(info, dict) and has_completed_poll(info)
            for info in state.values()
        )

    def _loop(self) -> None:
        assert self._started_at is not None
        poll_deadline = self._started_at + FULL_POLL_WAIT_S
        while not self._stop.is_set():
            now = time.time()
            due = self._next_run_at is not None and now >= self._next_run_at
            polled = self._full_poll_done() or now >= poll_deadline
            if (due and polled) or self._refresh.is_set():
                self._refresh.clear()
                try:
                    self.run_once(now)
                except Exception:
                    log.exception("doctor run failed")
                continue
            self._stop.wait(1.0)


def summarize(problems: list) -> dict:
    """Severity counts over fresh or persisted problems (open only).

    Counts groups; ``items_total`` is the member count across them
    (present when nonzero).
    """
    out = {"critical": 0, "warn": 0, "info": 0, "regressed": 0}
    total = 0
    for item in problems or []:
        if not isinstance(item, dict):
            continue
        if item.get("resolved_at") is not None:
            continue
        sev = item.get("severity")
        if sev in out:
            out[sev] += 1
        if item.get("regressed"):
            out["regressed"] += 1
        members = item.get("items")
        if isinstance(members, list):
            total += len(members)
        elif isinstance(item.get("items_total"), int):
            total += item["items_total"]
    if total:
        out["items_total"] = total
    return out


def filter_problems(problems: list, ws: str | None = None, min_severity: str | None = None) -> list:
    """Workstream plus minimum-severity filter for CLI output."""
    out = []
    for item in problems or []:
        if not isinstance(item, dict):
            continue
        if ws not in (None, "", "all"):
            scope = str(item.get("scope") or "global")
            if scope != "global" and scope != f"ws:{ws}" and item.get("ws") != ws:
                continue
        if min_severity in SEV_RANK:
            rank = SEV_RANK.get(str(item.get("severity")), 9)
            if rank > SEV_RANK[min_severity]:
                continue
        out.append(item)
    return out


def _group_size(item: dict) -> int:
    """Member count for digest sorting: full total, else shown rows."""
    try:
        total = int(item.get("items_total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total:
        return total
    members = item.get("items")
    return len(members) if isinstance(members, list) else 0


def format_doctor_human(payload: dict, show_all: bool = False) -> str:
    """Compact digest of a ``GET /api/doctor`` payload: one line per group.

    Groups sort by severity then size. Each line carries the severity
    glyph and word, code, workstream, member count, and one-line title.
    Critical and warn groups show their top 3 members; ``show_all``
    (``doctor --all``) prints every member under every group.
    """
    summary = payload.get("summary") or {}
    lines = [
        "doctor: {c} critical, {w} warn, {i} info "
        "(generated {g}, next run {n}, every {iv:g}s)".format(
            c=summary.get("critical", 0),
            w=summary.get("warn", 0),
            i=summary.get("info", 0),
            g=int(payload.get("generated_at") or 0),
            n=int(payload.get("next_run_at") or 0),
            iv=float(payload.get("interval_s") or 300),
        )
    ]
    problems = payload.get("problems") or []
    if not problems:
        lines.append("doctor: clean, no problems found")
    ordered = sorted(
        [item for item in problems if isinstance(item, dict)],
        key=lambda entry: (
            SEV_RANK.get(str(entry.get("severity")), 9),
            -_group_size(entry),
            str(entry.get("code")),
            str(entry.get("id")),
        ),
    )
    for item in ordered:
        sev = str(item.get("severity") or "warn")
        glyph = SEV_GLYPH.get(sev, "?")
        code = item.get("code") or ""
        ws = item.get("ws") or "global"
        count = _group_size(item)
        title = " ".join(str(item.get("title") or "").split())
        size = f" x{count}" if count else ""
        lines.append(f"{glyph} {sev} {code} {ws}{size} {title} ({item.get('id')})")
        members = item.get("items")
        rows = list(members) if isinstance(members, list) else []
        if show_all:
            for member in rows:
                if isinstance(member, dict):
                    lines.append(f"  - {_item_label(member.get('title'), member.get('id'))}")
        elif sev in ("critical", "warn"):
            for member in rows[:3]:
                if isinstance(member, dict):
                    lines.append(f"  - {_item_label(member.get('title'), member.get('id'))}")
            if len(rows) > 3:
                lines.append(f"  (+{len(rows) - 3} more)")
        fix = item.get("fix") or {}
        if fix.get("command"):
            auto = " [safe-auto]" if fix.get("safe_auto") else ""
            lines.append(f"  fix: {fix.get('command')}{auto}")
        if item.get("regressed"):
            lines.append("  regressed since its last resolution")
    try:
        items_total = int(summary.get("items_total") or 0)
    except (TypeError, ValueError):
        items_total = 0
    if not items_total:
        items_total = sum(_group_size(item) for item in ordered)
    lines.append(
        f"{len(ordered)} groups, {items_total} items; "
        "doctor --all to expand; doctor --ws <ID>"
    )
    resolved = payload.get("resolved_24h") or []
    if resolved:
        lines.append(f"resolved in the last 24 h ({len(resolved)}):")
        for entry in resolved[:10]:
            lines.append(f"  {entry.get('code')}: {entry.get('title')}")
        if len(resolved) > 10:
            lines.append(f"  (+{len(resolved) - 10} more)")
    return "\n".join(lines)


def _open_store(cfg: dict):
    """The state database, or None when it cannot be opened."""
    try:
        from dash.store import Store

        path = _db_path(cfg)
        if path is None:
            state = _state_dir(cfg)
            if state is None:
                return None
            path = state / "dash.sqlite"
        return Store(path)
    except Exception:
        return None


def run_doctor(cfg: dict, url: str, now: float | None = None) -> dict:
    """Run every check. Returns ``{"ok", "problems", "checks"}`` (legacy shape).

    Read-only: collection never persists. The server persists on its own
    schedule; the CLI snapshots the fallback run itself.
    """
    now = float(now if now is not None else time.time())
    cfg = cfg if isinstance(cfg, dict) else {}
    health = _get(url, "/api/health", timeout=10)
    store = _open_store(cfg)
    try:
        result = collect_problems(store, cfg, now, url=url, health=health)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    problems = []
    for item in result["problems"]:
        problems.append(
            {
                "code": item["code"],
                "message": f"{item['title']}: {item['detail']}",
                "command": item["fix_command"],
                **({"warning": True} if item["severity"] == "info" else {}),
            }
        )
    hard = [item for item in problems if not item.get("warning")]
    return {"ok": not hard, "problems": problems, "checks": result["checks"]}


def safe_fixes(problems: list) -> list:
    """Persisted or fresh problems whose fix may run unattended."""
    out = []
    for item in problems or []:
        if not isinstance(item, dict):
            continue
        fix = item.get("fix") or {}
        auto = fix.get("safe_auto")
        if auto is None:
            auto = item.get("safe_auto")
        if auto:
            out.append(item)
    return out


def _fix_command(item: dict) -> str:
    fix = item.get("fix") or {}
    return str(fix.get("command") or item.get("fix_command") or "")


def _outside_root_path(detail: str) -> str | None:
    """Run-dir path from a run-dir-outside-roots style detail, or None."""
    text = str(detail or "")
    if "live worker manifest at " in text:
        path = text.split("live worker manifest at ", 1)[1].split(" is outside")[0]
        return path.split(" (")[0].strip() or None
    if "live worker manifest outside roots.scratchpads: " in text:
        rest = text.split("live worker manifest outside roots.scratchpads: ", 1)[1]
        label, _, path = rest.partition(" in ")
        return (path.strip().split(" (")[0] if path else None) or None
    if "worker manifests outside roots.scratchpads: " in text:
        path = text.split("worker manifests outside roots.scratchpads: ", 1)[1]
        return path.split(" (liveness")[0].strip() or None
    return None


def _local_config_path() -> Path:
    return TOOL_ROOT / "config.local.json"


def _add_scratch_root(path: str) -> str:
    """Add ``path`` to config.local.json roots.scratchpads with a backup.

    Only ever touches ``config.local.json`` (never the shipped example).
    Keeps the newest 3 ``*.bak-*`` files. Returns a human message.
    """
    target = _local_config_path()
    data: dict = {}
    made_backup = False
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"config fix skipped: {target} does not parse; fix it by hand"
        if not isinstance(data, dict):
            return f"config fix skipped: {target} is not an object; fix it by hand"
        stamp = int(time.time())
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        try:
            backup.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            return f"config fix skipped: cannot back up {target}: {exc}"
        made_backup = True
        for old in sorted(target.parent.glob(f"{target.name}.bak-*"))[:-3]:
            try:
                old.unlink()
            except OSError:
                pass
    roots = data.setdefault("roots", {})
    if not isinstance(roots, dict):
        roots = {}
        data["roots"] = roots
    current = roots.get("scratchpads")
    if isinstance(current, str):
        current = [current]
    if not isinstance(current, list):
        current = []
    if path in current:
        return f"scratch root already in config.local.json: {path}"
    current.append(path)
    roots["scratchpads"] = current
    try:
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return f"config fix failed: cannot write {target}: {exc}"
    if made_backup:
        return f"added scratch root to config.local.json (backup kept): {path}"
    return f"added scratch root to a new config.local.json: {path}"


def apply_safe_fixes(store, cfg: dict, problems: list, now: float | None = None) -> list[str]:
    """Apply safe, local, reversible fixes. Returns one message per action.

    Only ever: register a run dir, add a missing scratch root to
    ``config.local.json`` (with a backup), run one offset prune batch,
    restart a stalled dashboard, reprice and backfill run models. Never
    anything touching sources, other sessions, or processes.
    """
    from dash.rundirs import register_run_dir, registered_run_dirs

    now = float(now if now is not None else time.time())
    cfg = cfg if isinstance(cfg, dict) else {}
    state = _state_dir(cfg)
    messages: list[str] = []
    try:
        from dash.locate import scratch_roots

        roots = list(scratch_roots(cfg) or [])
    except Exception:
        roots = []
    try:
        registered = set(registered_run_dirs(state, cfg, now) if state is not None else [])
    except Exception:
        registered = set()
    for item in safe_fixes(problems):
        code = item.get("code")
        detail = item.get("detail") or ""
        if code in ("run-dir-outside-roots", "manifest-outside-roots"):
            path = _outside_root_path(detail)
            if not path:
                messages.append(f"no path in {code} detail; fixed nothing")
                continue
            if not os.path.isdir(path):
                messages.append(f"run dir is gone, fixed nothing: {path}")
                continue
            if path not in registered:
                ok = register_run_dir(state, path) if state is not None else False
                messages.append(
                    f"registered run dir: {path}" if ok
                    else f"could not register run dir: {path}"
                )
                continue
            covered = any(
                path == root or path.startswith(root.rstrip("/") + "/") for root in roots
            )
            if not covered:
                messages.append(_add_scratch_root(path))
            else:
                messages.append(f"run dir already covered, fixed nothing: {path}")
        elif code == "source-stalled":
            try:
                from agent_dash import cmd_restart

                code_out = cmd_restart()
            except Exception as exc:
                messages.append(f"dashboard restart failed: {exc}")
                continue
            messages.append(
                "restarted the stalled dashboard" if code_out == 0
                else f"dashboard restart exited {code_out}"
            )
        elif code == "unpriced-model":
            try:
                from dash.pricing import reprice as reprice_usage

                from agent_dash import _claude_projects  # noqa: private but same tool

                summary = reprice_usage(store, _claude_projects(cfg))
                filled = store.backfill_grok_models(now) if hasattr(store, "backfill_grok_models") else 0
                models = len(summary.get("models") or [])
                messages.append(f"repriced {models} models, backfilled {filled} run models")
            except Exception as exc:
                messages.append(f"reprice failed: {exc}")
        else:
            messages.append(f"no safe applier for {code}; fixed nothing")
    try:
        pruned = store.prune_offsets()
    except Exception as exc:
        messages.append(f"offset prune failed: {exc}")
    else:
        messages.append(f"pruned {pruned} dead offset rows" if pruned else "offset table already clean")
    return messages


def parse_since(raw: str | None, now: float) -> float:
    """``--since 24h`` (s/m/h/d suffix, bare number means hours) to epoch."""
    if raw is None or not str(raw).strip():
        return float(now) - 86400
    text = str(raw).strip().lower()
    unit = 3600.0
    if text[-1] in ("s", "m", "h", "d"):
        unit = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[text[-1]]
        text = text[:-1]
    try:
        amount = float(text)
    except ValueError as exc:
        raise ValueError(f"bad --since {raw!r}: use like 24h, 7d") from exc
    if amount <= 0:
        raise ValueError(f"bad --since {raw!r}: use like 24h, 7d")
    return float(now) - amount * unit


def format_report(report: dict, md: bool = False) -> str:
    """Digest for hub reports: new, regressed, resolved, open by severity and workstream."""
    open_by_sev = report.get("open_by_severity") or {}
    open_by_ws = report.get("open_by_ws") or {}
    new = report.get("new") or []
    regressed = report.get("regressed") or []
    resolved = report.get("resolved") or []
    window_h = (report.get("now", 0) - report.get("since", 0)) / 3600
    if md:
        lines = [
            f"## Doctor report (last {window_h:g} h)",
            "",
            f"Open: {open_by_sev.get('critical', 0)} critical, "
            f"{open_by_sev.get('warn', 0)} warn, {open_by_sev.get('info', 0)} info.",
            "",
        ]
        if open_by_ws:
            lines.append("By workstream: " + ", ".join(f"{ws} {n}" for ws, n in open_by_ws.items()))
            lines.append("")
        for head, rows in (("New", new), ("Regressed", regressed)):
            lines.append(f"### {head} ({len(rows)})")
            for item in rows:
                sev = item.get("severity", "?")
                lines.append(f"- [{sev}] {item.get('title')} (`{item.get('id')}`)")
            lines.append("")
        lines.append(f"### Resolved ({len(resolved)})")
        for entry in resolved:
            lines.append(f"- {entry.get('code')}: {entry.get('title')}")
        return "\n".join(lines).rstrip() + "\n"
    lines = [
        f"doctor report (last {window_h:g} h): "
        f"{open_by_sev.get('critical', 0)} critical, {open_by_sev.get('warn', 0)} warn, "
        f"{open_by_sev.get('info', 0)} info open",
    ]
    if open_by_ws:
        lines.append("open by workstream: " + ", ".join(f"{ws}={n}" for ws, n in open_by_ws.items()))
    lines.append(f"new: {len(new)}, regressed: {len(regressed)}, resolved: {len(resolved)}")
    for item in list(new) + list(regressed):
        lines.append(f"  [{item.get('severity')}] {item.get('title')} ({item.get('id')})")
    for entry in resolved:
        lines.append(f"  resolved {entry.get('code')}: {entry.get('title')}")
    return "\n".join(lines)


def preflight_for_launch(
    worker_name: str,
    worker: dict,
    run_dir: str,
    state_dir: str,
    url: str = "http://127.0.0.1:8765",
    timeout_s: float = 3.0,
    policy: dict | None = None,
) -> dict | None:
    """Fast local preflight before a worker launch. Returns a problem or None.

    Checks the dashboard answers, the run dir will be visible to it, and
    the tier's model is priced and policy-compliant. Read-only (a state-dir
    writability probe aside), label-free, and bounded to a few seconds.
    """
    health = _get(url, "/api/health", timeout=timeout_s)
    if health is None:
        return {
            "code": "dashboard-down",
            "title": "Dashboard is down",
            "detail": f"dashboard not reachable at {url}",
            "command": "python3 agent_dash.py run",
        }
    roots = health.get("scratch_roots") or []
    run_dirs = health.get("run_dirs") or []
    rundir = os.path.abspath(str(run_dir))
    visible = rundir in run_dirs or any(
        rundir == root or rundir.startswith(root.rstrip("/") + "/") for root in roots
    )
    if not visible:
        try:
            from dash.rundirs import registered_run_dirs

            visible = rundir in (registered_run_dirs(state_dir) or [])
        except Exception:
            visible = False
    if not visible:
        probe = state_dir if os.path.isdir(state_dir) else os.path.dirname(state_dir)
        if not os.access(probe or ".", os.W_OK | os.X_OK):
            return {
                "code": "run-dir-outside-roots",
                "title": "Run folder would be invisible to the dashboard",
                "detail": f"run dir {rundir} is outside the dashboard roots and cannot be registered",
                "command": f"add {rundir} to roots.scratchpads in config, then restart the dashboard",
            }
    model = (worker or {}).get("model") or ""
    kind = (worker or {}).get("kind")
    if not model:
        return {
            "code": "unpriced-model",
            "title": f"Tier {worker_name} has no model",
            "detail": f"tier {worker_name} names no model, so its runs would be unpriced",
            "command": "set a priced model for the tier in workers.local.json",
        }
    try:
        from dash.pricing import status as price_status

        priced = price_status(model)["priced"]
    except Exception:
        priced = False
    if not priced:
        return {
            "code": "unpriced-model",
            "title": f"Tier {worker_name} model has no price",
            "detail": f"tier {worker_name} model {model!r} has no catalog rate",
            "command": "add a row to tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
        }
    effort = effort_from_extra_args(worker or {})
    reason = model_policy_reason(kind, model, effort, policy)
    if reason is not None:
        return {
            "code": "model-policy",
            "title": f"Tier {worker_name} breaks the model policy",
            "detail": f"tier {worker_name}: {reason}",
            "command": "fix the model or flags in workers.local.json",
        }
    return None


def format_human(result: dict) -> str:
    lines = []
    for item in result.get("checks") or []:
        mark = "ok" if item.get("ok") else "FAIL"
        lines.append(f"[{mark}] {item.get('name')}: {item.get('detail')}")
    problems = result.get("problems") or []
    if problems:
        lines.append("")
        for item in problems:
            tag = "warning" if item.get("warning") else "problem"
            lines.append(f"{tag} ({item.get('code')}): {item.get('message')}")
            lines.append(f"  fix: {item.get('command')}")
    else:
        lines.append("doctor: clean, no problems found")
    return "\n".join(lines)
