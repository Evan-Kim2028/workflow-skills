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
