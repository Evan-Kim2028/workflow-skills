"""Dead-run reaper: close running rows whose process is gone.

Every 60 s this calls :meth:`Store.reap_dead_runs`: a row still marked
running whose pid exited and whose out/err logs and usage evidence stayed
quiet for ``reaper.idle_minutes`` (default 10) is closed
``died_early``/``DEAD`` (or ``QUOTA`` when the logs match a rate/quota
limit), so dead runs stop reading live and stop failing the deploy gate's
reconcile check. Rows with a live pid are never touched.
"""

from __future__ import annotations

import logging

from dash.liveness import REAP_IDLE_S

NAME = "reaper"
INTERVAL_S = 60.0

log = logging.getLogger("agent_dash.sources.reaper")


def idle_s(cfg) -> float:
    """Reap quiet-window seconds from ``cfg`` (``reaper.idle_minutes``)."""
    section = (cfg or {}).get("reaper")
    minutes = None
    if isinstance(section, dict):
        try:
            minutes = float(section.get("idle_minutes"))
        except (TypeError, ValueError):
            minutes = None
    if minutes is None or minutes <= 0:
        return float(REAP_IDLE_S)
    return minutes * 60.0


def poll(store, cfg, now: float) -> None:
    """Reap once per tick. A failure is logged, never raised."""
    try:
        reaped = store.reap_dead_runs(now, idle_s=idle_s(cfg))
    except Exception as exc:
        log.warning("reap failed: %s: %s", type(exc).__name__, exc)
        return
    for entry in reaped:
        log.info(
            "reaped %s (%s) verdict=%s quiet %ss",
            entry.get("run_id"),
            entry.get("source"),
            entry.get("verdict"),
            entry.get("idle_s"),
        )
