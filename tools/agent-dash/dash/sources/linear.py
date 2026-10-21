"""Linear sync poller: pushes linked workstream bundles to Linear.

Off unless a Linear credential is available (OAuth sign-in or ``LINEAR_API_KEY``) and at least one bundle carries a
``linear.json``. The read-only switch (``AGENT_DASH_READONLY=1`` or config
``readonly: true``) skips every poll. Each linked bundle is synced by
``dash.linear_sync``. A failed bundle backs off on its own clock, so a
healthy bundle keeps its cadence. A ``RATELIMITED`` result stops the poll
for every bundle and pauses all of them. The key is never logged or
returned.
"""

from __future__ import annotations

import logging
import time

from dash import linear, linear_auth
from dash.actions import actions_enabled
from dash.sources import POLL_BUDGET_S

log = logging.getLogger("agent_dash.linear")

NAME = "linear"
INTERVAL_S = 60

DEFAULT_MAX_BACKOFF_S = 300.0
RATE_LIMIT_PAUSE_S = 300.0

last_error = None
_clock = time.monotonic
_fail_streak: dict[str, int] = {}
_backoff: dict[str, float] = {}
_next_attempt: dict[str, float] = {}
_rate_limit_until: float | None = None


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
    ``synced_at``, ``last_error``, the outbox row count and that bundle's
    ``backoff_s`` (0 when healthy). ``rate_limit_pause_s`` is the shared pause
    left after a ``RATELIMITED`` result (0 when none).
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
            "backoff_s": _backoff.get(ws, 0.0),
        }
    return {
        "linked": len(linked),
        "rate_limit_pause_s": _rate_limit_remaining(),
        "workstreams": workstreams,
    }


def _rate_limit_remaining() -> float:
    if _rate_limit_until is None:
        return 0.0
    return max(0.0, float(_rate_limit_until - _clock()))


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


def _record_success(ws: str) -> None:
    for table in (_fail_streak, _backoff, _next_attempt):
        table.pop(ws, None)


def _record_failure(ws: str, cfg: dict) -> None:
    """Count one failed poll of ``ws`` and schedule its next attempt after the back-off delay."""
    streak = _fail_streak.get(ws, 0) + 1
    _fail_streak[ws] = streak
    delay = float(min(INTERVAL_S * (2 ** min(streak - 1, 30)), _max_backoff(cfg)))
    _backoff[ws] = delay
    _next_attempt[ws] = _clock() + delay
    log.warning("linear: %s backing off %gs after %d failed poll(s)", ws, delay, streak)


def _rate_limit_pause(result: dict) -> float:
    """Seconds to pause every bundle after a RATELIMITED result.

    Uses the result's ``reset_at`` (epoch seconds, when present and later
    than now) and falls back to RATE_LIMIT_PAUSE_S.
    """
    reset_at = result.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        remaining = float(reset_at) - time.time()
        if remaining > 0:
            return remaining
    return RATE_LIMIT_PAUSE_S


def poll(store, cfg, now: float) -> None:
    global last_error
    try:
        _poll(cfg or {})
    except Exception as exc:
        last_error = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("linear: %s", last_error)


def _poll(cfg: dict) -> None:
    global _rate_limit_until
    if not actions_enabled(cfg):
        return
    if linear_auth.credential() is None:
        return
    bundles = _linked()
    if not bundles:
        return
    if _rate_limit_until is not None and _clock() < _rate_limit_until:
        return
    _rate_limit_until = None
    now = _clock()
    due = [ws for ws in bundles if now >= _next_attempt.get(ws, 0.0)]
    if not due:
        return
    budget = _budget(cfg)
    started = _clock()
    attempted = 0
    longest = 0.0
    for ws in due:
        elapsed = _clock() - started
        if attempted and elapsed + longest > budget:
            log.info("linear: poll budget reached after %d bundle(s)", attempted)
            break
        began = _clock()
        try:
            result = _sync(ws)
        except Exception as exc:
            result = {"error": _clip(f"{type(exc).__name__}: {exc}")}
        attempted += 1
        error = result.get("error") if isinstance(result, dict) else None
        if error and str(error).startswith("RATELIMITED"):
            _rate_limit_until = _clock() + _rate_limit_pause(result)
            log.warning("linear: rate limited; pausing all bundles for %gs", _rate_limit_until - _clock())
            return
        if error:
            log.warning("linear: %s sync failed: %s", ws, _clip(error))
            _record_failure(ws, cfg)
        else:
            _record_success(ws)
            if _clock() - started < budget:
                _daily_update(ws)
            else:
                log.info("linear: %s daily update deferred, poll budget spent; retries next poll", ws)
        longest = max(longest, _clock() - began)


def _daily_update(ws: str) -> None:
    """Post the bundle's daily standup update when it is due (``dash.linear_update`` owns the rule). Never raises."""
    try:
        from dash import linear_update

        now = time.time()
        if linear_update.due(ws, now):
            error = linear_update.send_daily(ws, now)
            if error:
                log.warning("linear: %s daily update failed: %s", ws, _clip(error))
    except Exception as exc:
        log.warning("linear: %s daily update failed: %s", ws, _clip(f"{type(exc).__name__}: {exc}"))


def _clip(text) -> str:
    """Clip a message to 200 characters with the exact API key replaced by ``[redacted]``."""
    out = str(text).replace("\n", " ").strip()
    key = linear.api_key()
    if key:
        out = out.replace(key, "[redacted]")
    return out[:200]
