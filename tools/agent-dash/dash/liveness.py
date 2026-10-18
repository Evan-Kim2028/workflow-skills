"""Shared liveness evidence for running rows.

One helper decides whether a stored pid is still alive, so every source
and the store use the same reap-safe check. A zombie still answers
``kill(pid, 0)``; reaping our own exited child first (``waitpid`` with
``WNOHANG``) closes that gap. This mirrors ``scripts/run_worker.py``
``pid_alive``; that script imports this module so there is one copy.
"""

from __future__ import annotations

import os

# A running row with no fresher evidence than this is stale in views and
# in reconcile, even when its pid is alive or it carries no pid. Six hours
# catches the 13 h Dagster jobs, the 44 h dead-pid lead row, and the 143 h
# idle lead session from the incident, while a live agent emits usage or a
# process sample every few minutes so it never trips this bound.
STALE_IDLE_S = 6 * 3600

# Dagster active-set freshness: the poll runs every 60 s, so an active set
# older than five minutes is not trusted for close-out or stale reports.
# A failed poll leaves stored rows alone.
DAGSTER_ACTIVE_LIMIT = 200
DAGSTER_ACTIVE_FRESH_S = 300

# Sources whose running rows carry a pid. Shell rows carry no pid; their
# liveness is out-file growth or a live label (see manifests.classify_status)
# plus the idle bound above.
PID_SOURCES = ("claude-main", "muse", "grok", "glm", "shell")

# A run whose process vanished is closed only after the process has been
# gone this long: a quiet session file never closes a muse/glm/grok run
# with a live pid or a process sample fresher than this. Thirty seconds
# covers a missed process listing or two (5 s cadence) without hiding a
# real death: the next polls then emit done and the close lands.
PROCESS_GONE_GRACE_S = 30.0

# Poll cadence per source (seconds). The stale check ignores a dead-pid row
# whose last evidence is within two poll intervals of its source: the source
# closes the row on its next poll, so flagging it in between is noise. For
# the 2 s tool sources that grace is 4 s.
SOURCE_INTERVAL_S = {
    "grok": 2.0,
    "muse": 2.0,
    "glm": 2.0,
    "generic": 2.0,
    "claude-main": 2.0,
    "claude-subagent": 2.0,
    "claude-workflow": 2.0,
    "shell": 2.0,
    "dagster": 60.0,
}


def stale_grace_s(source) -> float:
    """Dead-pid grace for ``source``: two of its poll intervals."""
    try:
        interval = float(SOURCE_INTERVAL_S.get(source or "", 2.0))
    except (TypeError, ValueError):
        interval = 2.0
    return 2.0 * interval


def pid_alive(pid) -> bool:
    """True while the process runs. Reaps our own exited child first."""
    try:
        if os.waitpid(int(pid), os.WNOHANG)[0] != 0:
            return False
    except ChildProcessError:
        pass
    except (OSError, TypeError, ValueError):
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True
