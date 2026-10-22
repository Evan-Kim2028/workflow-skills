"""Workstream inheritance and lead inference over stored rows.

Pure rules live in :mod:`dash.attribution`; this module binds them to the
store. Every function assumes the caller holds ``store._lock`` and talks
to ``store._conn`` directly, so the per-poll upsert path pays one indexed
lookup and quiet rows converge on the throttled refresh pass instead.
"""

from __future__ import annotations

import json
import os
import time

from dash import attribution

#: Minimum seconds between refresh passes (mirrors the grok backfill gate).
REFRESH_MIN_S = 60.0
REFRESH_META_KEY = "ws_inherit_ts"
#: Cap on bundle directories scanned for train branches per refresh.
_TRAIN_DIR_CAP = 512
#: Cap on bytes read from one plan.json for train branches.
_TRAIN_READ_CAP = 1 << 20

_train_cache: dict = {}


def train_branches(root=None) -> dict:
    """Map bundle train branch names to their workstream.

    Read-only scan of ``<root>/<WS>/plan.json`` ``trains`` entries. The
    result is cached per root for two minutes; an unreadable root maps
    to no branches instead of failing the poll.
    """
    from dash.attribution import bundles_root

    base = str(root) if root is not None else str(bundles_root())
    now = time.time()
    hit = _train_cache.get(base)
    if hit and now - hit[0] < 120.0:
        return hit[1]
    mapping: dict[str, str] = {}
    try:
        entries = sorted(os.scandir(base), key=lambda ent: ent.name)
    except OSError:
        entries = []
    for ent in entries[:_TRAIN_DIR_CAP]:
        try:
            if not ent.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        ws = ent.name
        if not attribution._WS_ID.match(ws):
            continue
        plan = os.path.join(ent.path, "plan.json")
        try:
            with open(plan, "rb") as fh:
                raw = fh.read(_TRAIN_READ_CAP)
            data = json.loads(raw)
        except (OSError, ValueError):
            continue
        trains = data.get("trains") if isinstance(data, dict) else None
        if not isinstance(trains, list):
            continue
        for item in trains:
            if not isinstance(item, dict):
                continue
            branch = item.get("branch")
            if isinstance(branch, str) and branch:
                mapping[branch] = ws
                mapping[branch.replace("/", "-")] = ws
    _train_cache[base] = (now, mapping)
    return mapping


def parent_workstream(store, parent_session_id: str) -> str | None:
    """The resolved workstream of a parent session, if any."""
    if not isinstance(parent_session_id, str) or not parent_session_id.strip():
        return None
    try:
        row = store._one(
            "SELECT workstream FROM sessions WHERE session_id=?",
            (parent_session_id.strip(),),
        )
    except Exception:
        return None
    ws = (row or {}).get("workstream")
    return ws if isinstance(ws, str) and ws.strip() else None


def lead_workers(store, session_id: str, now: float) -> list:
    """Workstreams of attributed workers a lead launched in the window."""
    if not isinstance(session_id, str) or not session_id.strip():
        return []
    try:
        since = float(now) - attribution.WORKER_WINDOW_S
    except (TypeError, ValueError):
        return []
    try:
        rows = store._conn.execute(
            """SELECT workstream FROM runs
               WHERE parent_session_id=? AND workstream IS NOT NULL AND workstream != ''
                 AND started_at IS NOT NULL AND started_at >= ?""",
            (session_id.strip(), since),
        ).fetchall()
    except Exception:
        return []
    return [row[0] for row in rows]


def lead_cwd_hint(store, session_id: str) -> tuple[str | None, str | None]:
    """(cwd, ws_hint) for a lead session, else (None, None)."""
    if not isinstance(session_id, str) or not session_id.strip():
        return None, None
    try:
        row = store._one(
            "SELECT cwd, ws_hint FROM sessions WHERE session_id=?",
            (session_id.strip(),),
        )
    except Exception:
        return None, None
    if not row:
        return None, None
    cwd = row.get("cwd")
    hint = row.get("ws_hint")
    return (
        cwd if isinstance(cwd, str) and cwd.strip() else None,
        hint if isinstance(hint, str) and hint.strip() else None,
    )


def resolve_child(store, session_id, parent_session_id, *, now=None, run_id=None) -> tuple:
    """Rule (b): a helper with no workstream takes its parent's.

    A Claude Agent subagent first tries its spawning turn: the
    workstream of the main-session turn whose Agent tool call spawned
    it, else the majority of its own signals, else the cursor value at
    the spawn (all confidence ``inherited``). Anything still unresolved
    falls through to the parent-session rule below, so existing
    behaviour never regresses.
    """
    if isinstance(run_id, str) and run_id:
        resolver = getattr(store, "resolve_subagent", None)
        if callable(resolver):
            try:
                ws, conf = resolver(run_id)
            except Exception:
                ws, conf = None, None
            if ws:
                return ws, conf
    ws = parent_workstream(store, parent_session_id)
    if ws:
        return ws, attribution.INHERITED
    return None, None


def resolve_lead(store, session_id: str, *, now=None) -> tuple:
    """Rule (c): a lead's own workstream from workers, cwd, or writes."""
    moment = time.time() if now is None else float(now)
    workers = lead_workers(store, session_id, moment)
    cwd, hint = lead_cwd_hint(store, session_id)
    branches = train_branches()
    return attribution.resolve_lead(
        workers, cwd, hint, train_branches=branches,
    )


def resolve(store, label, session_id, parent_session_id, *, now=None, run_id=None) -> tuple:
    """Fill the gap left by the explicit rules: (b) then (c)."""
    if isinstance(parent_session_id, str) and parent_session_id.strip():
        return resolve_child(store, session_id, parent_session_id, now=now, run_id=run_id)
    return resolve_lead(store, session_id, now=now)


def _throttled(store, now: float) -> bool:
    try:
        store._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = store._conn.execute(
            "SELECT value FROM meta WHERE key=?", (REFRESH_META_KEY,)
        ).fetchone()
        if row and row[0]:
            try:
                if float(now) - float(row[0]) < REFRESH_MIN_S:
                    return True
            except (TypeError, ValueError):
                pass
        store._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (REFRESH_META_KEY, str(float(now))),
        )
        store._conn.commit()
    except Exception:
        return False
    return False


def refresh(store, now: float) -> int:
    """Recompute inherited/inferred rows; never touch explicit ones.

    Sessions first so child runs inherit the fresh lead value in the
    same pass. Returns how many rows changed.
    """
    try:
        moment = float(now)
    except (TypeError, ValueError):
        return 0
    if _throttled(store, moment):
        return 0
    changed = 0
    try:
        sessions = [
            dict(row)
            for row in store._conn.execute(
                """SELECT * FROM sessions
                   WHERE workstream IS NULL OR ws_confidence IN ('inherited', 'inferred')"""
            ).fetchall()
        ]
    except Exception:
        return 0
    branches = train_branches()
    for session in sessions:
        sid = session.get("session_id")
        if not sid:
            continue
        explicit, conf = attribution.attribute(session.get("label"), sid)
        if explicit is not None:
            target = (explicit, conf)
        else:
            workers = lead_workers(store, sid, moment)
            target = attribution.resolve_lead(
                workers,
                session.get("cwd"),
                session.get("ws_hint"),
                train_branches=branches,
            )
        if target[0] == session.get("workstream") and target[1] == session.get(
            "ws_confidence"
        ):
            continue
        try:
            store._conn.execute(
                "UPDATE sessions SET workstream=?, ws_confidence=? WHERE session_id=?",
                (target[0], target[1], sid),
            )
        except Exception:
            continue
        changed += 1
    try:
        runs = [
            dict(row)
            for row in store._conn.execute(
                """SELECT * FROM runs
                   WHERE workstream IS NULL OR ws_confidence IN ('inherited', 'inferred')"""
            ).fetchall()
        ]
    except Exception:
        runs = []
    touched_hours: set = set()
    for run in runs:
        rid = run.get("run_id")
        if not rid:
            continue
        explicit, conf = attribution.attribute(
            run.get("label"),
            run.get("session_id"),
            parent_session_id=run.get("parent_session_id"),
        )
        if explicit is not None:
            target = (explicit, conf)
        elif isinstance(run.get("parent_session_id"), str) and run.get(
            "parent_session_id"
        ).strip():
            target = resolve_child(
                store,
                run.get("session_id"),
                run["parent_session_id"],
                now=moment,
                run_id=rid,
            )
        else:
            workers = lead_workers(store, run.get("session_id"), moment)
            cwd, hint = lead_cwd_hint(store, run.get("session_id"))
            target = attribution.resolve_lead(
                workers, cwd, hint, train_branches=branches,
            )
        if target[0] == run.get("workstream") and target[1] == run.get(
            "ws_confidence"
        ):
            continue
        try:
            store._conn.execute(
                "UPDATE runs SET workstream=?, ws_confidence=? WHERE run_id=?",
                (target[0], target[1], rid),
            )
        except Exception:
            continue
        changed += 1
        try:
            touched_hours |= store._hours_for_run(rid)
        except Exception:
            pass
    try:
        for hour in touched_hours:
            store._rebuild_hour(hour)
        if changed:
            store._conn.commit()
    except Exception:
        try:
            store._conn.execute("ROLLBACK")
        except Exception:
            pass
    return changed
