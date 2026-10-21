"""Linear sync poller: pushes linked workstream bundles to Linear.

Off unless ``LINEAR_API_KEY`` is set and at least one bundle carries a
``linear.json``. The read-only switch (``AGENT_DASH_READONLY=1`` or config
``readonly: true``) skips every poll. Each linked bundle is synced by
``dash.linear_sync``; a result carrying ``error`` counts as a failed poll
and the next attempt backs off like the dagster source. The key is never
logged or returned.
"""

from __future__ import annotations

import logging
import time

from dash import linear
from dash.actions import actions_enabled
from dash.sources import POLL_BUDGET_S

log = logging.getLogger("agent_dash.linear")

NAME = "linear"
INTERVAL_S = 60

DEFAULT_MAX_BACKOFF_S = 300.0

last_error = None
_clock = time.monotonic
_fail_streak = 0
_backoff_s = 0.0
_next_attempt = None


def _linked() -> list[str]:
    """Workstream ids of the bundles that carry a link (``dash.linear_sync`` owns this)."""
    from dash.linear_sync import linked_bundles

    return linked_bundles()


def _sync(ws: str) -> dict:
    """Run one bundle's sync cycle (``dash.linear_sync`` owns this)."""
    from dash.linear_sync import sync

    return sync(ws)


def _linked_on_disk() -> list[str]:
    """Linked bundle ids read straight from the bundle directories (no sync module needed)."""
    from dash.bundles import list_bundles

    return [item["id"] for item in list_bundles() if linear.is_linked(item["id"])]


def health_extra() -> dict:
    """Extra health payload merged by the registry under ``linear``.

    ``workstreams`` carries only the non-secret sync fields per linked bundle:
    ``synced_at``, ``last_error`` and the outbox row count.
    """
    workstreams = {}
    try:
        linked = _linked_on_disk()
    except Exception as exc:
        log.warning("linear: health scan failed: %s", type(exc).__name__)
        linked = []
    for ws in linked:
        state = linear.sync_state(ws) or {}
        workstreams[ws] = {
            "synced_at": state.get("synced_at"),
            "last_error": _clip(state["last_error"]) if state.get("last_error") else None,
            "outbox": int(state.get("outbox") or 0),
        }
    return {
        "linked": len(linked),
        "backoff_s": _backoff_s,
        "workstreams": workstreams,
    }


def _max_backoff(cfg: dict) -> float:
    block = cfg.get("linear") if isinstance(cfg.get("linear"), dict) else {}
    try:
        value = float(block.get("max_backoff_s", DEFAULT_MAX_BACKOFF_S))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BACKOFF_S
    return value if value > 0 else DEFAULT_MAX_BACKOFF_S


def _budget(cfg: dict) -> float:
    try:
        value = float(cfg.get("poll_budget_s", POLL_BUDGET_S))
    except (TypeError, ValueError):
        return POLL_BUDGET_S
    return value if value > 0 else POLL_BUDGET_S


def _record_success() -> None:
    global _fail_streak, _backoff_s, _next_attempt
    _fail_streak = 0
    _backoff_s = 0.0
    _next_attempt = None


def _record_failure(cfg: dict) -> None:
    """Count one failed poll and schedule the next attempt after the back-off delay."""
    global _fail_streak, _backoff_s, _next_attempt
    _fail_streak += 1
    delay = INTERVAL_S * (2 ** min(_fail_streak - 1, 30))
    _backoff_s = float(min(delay, _max_backoff(cfg)))
    _next_attempt = _clock() + _backoff_s
    log.warning("linear: backing off %gs after %d failed poll(s)", _backoff_s, _fail_streak)


def poll(store, cfg, now: float) -> None:
    global last_error
    try:
        _poll(cfg or {})
    except Exception as exc:
        last_error = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("linear: %s", last_error)


def _poll(cfg: dict) -> None:
    if not actions_enabled(cfg):
        return
    if linear.api_key() is None:
        return
    bundles = _linked()
    if not bundles:
        return
    if _next_attempt is not None and _clock() < _next_attempt:
        return
    budget = _budget(cfg)
    started = _clock()
    attempted = 0
    failed = 0
    longest = 0.0
    for ws in bundles:
        elapsed = _clock() - started
        if attempted and elapsed + longest > budget:
            log.info("linear: poll budget reached after %d bundle(s)", attempted)
            break
        began = _clock()
        try:
            result = _sync(ws)
        except Exception as exc:
            result = {"error": _clip(f"{type(exc).__name__}: {exc}")}
        longest = max(longest, _clock() - began)
        attempted += 1
        if isinstance(result, dict) and result.get("error"):
            failed += 1
            log.warning("linear: %s sync failed: %s", ws, _clip(result["error"]))
    if not attempted:
        return
    if failed:
        _record_failure(cfg)
    else:
        _record_success()


def _clip(text) -> str:
    return str(text).replace("\n", " ").strip()[:200]
