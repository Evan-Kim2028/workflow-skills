"""Shared liveness evidence for running rows.

One helper decides whether a stored pid is still alive, so every source
and the store use the same reap-safe check. A zombie still answers
``kill(pid, 0)``; reaping our own exited child first (``waitpid`` with
``WNOHANG``) closes that gap. This mirrors ``scripts/run_worker.py``
``pid_alive``; that script imports this module so there is one copy.
"""

from __future__ import annotations

import os
import re

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

# A headless worker (muse, glm, grok, generic) whose process is still
# alive but whose output evidence (usage heartbeats, out-file growth) is
# older than this is stuck, not live: it reads "stalled", not "running".
# Thirty minutes is far longer than any healthy turn gap (a live agent
# emits usage heartbeats or grows its out file every few minutes) and
# far shorter than the 6 h stale bound, so stalls surface the same day.
# Process sightings alone never count: a stuck process is still sighted
# every poll, which is exactly why it used to read live. Interactive
# lead sessions (claude-main), the scheduler-owned Dagster jobs, and
# shell rows are exempt: a lead thinking for an hour is using the
# dashboard, not stuck in it.
STALLED_AFTER_S = 30 * 60

# Sources whose quiet-but-alive rows read stalled. Everything else keeps
# the old rules (idle leads hide, Dagster follows its active set).
STALLED_SOURCES = ("muse", "glm", "grok", "generic", "cursor")

# A process sample may only vouch for a run it could have produced: the
# process must have started no later than this after the run did, or the
# pid (or label) was reused by a newer worker. Five minutes covers clock
# skew between the manifest clock and the process clock without adopting
# a relaunched twin's process onto the dead row.
PROC_START_SKEW_S = 5 * 60


def proc_started_ok(proc_started, run_started, skew_s: float = PROC_START_SKEW_S) -> bool:
    """True when a matched process could belong to the run.

    A process that started well after the run did is a reused pid (or a
    relaunched same-label twin), not the run's agent. Unknown starts
    never veto: without both clocks there is nothing to compare.
    """
    try:
        proc_started = float(proc_started)
        run_started = float(run_started)
        skew = float(skew_s)
    except (TypeError, ValueError):
        return True
    return proc_started <= run_started + skew


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


# Quota-failure patterns for grok-build (`grok -p`) run logs. Mirrors the
# worker registry (`workers.local.json` quota_patterns): a run whose stderr
# or report tail matches one of these ended on a rate/quota limit, not on a
# finished turn. Sources use this to close the row as done/BLOCKED instead
# of leaving it running (or letting a shared out file read PASS).
QUOTA_PATTERNS = (
    r"rate limit",
    r"quota",
    r"(HTTP|status|error|code)[ :=]*429\b|429 Too Many",
    r"usage limit",
)

# How much of an err/out tail to scan for a quota hit. Small and bounded:
# quota errors print within the last lines, and sources already budget the
# full file reads separately.
QUOTA_TAIL_BYTES = 8 * 1024


def text_shows_quota(text, patterns=QUOTA_PATTERNS) -> bool:
    """True when ``text`` matches any quota pattern (case-insensitive)."""
    if not text or not isinstance(text, str):
        return False
    for pattern in patterns or ():
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def file_shows_quota(path, max_bytes=QUOTA_TAIL_BYTES) -> bool:
    """True when the tail of ``path`` matches a quota pattern.

    Reads only the last ``max_bytes``. Missing or unreadable files read
    as False, never an exception: sources stay exception-safe and a
    failed read simply skips the quota close-out.
    """
    if not path or not isinstance(path, str):
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= 0:
        return False
    try:
        with open(path, "rb") as fh:
            if size <= max_bytes:
                data = fh.read(size)
            else:
                fh.seek(max(0, size - max_bytes))
                data = fh.read(max_bytes)
    except OSError:
        return False
    try:
        text = data.decode("utf-8", "replace")
    except (UnicodeError, ValueError):
        return False
    return text_shows_quota(text)


def read_pid_file(path):
    """Pid from a ``run_worker.py`` ``<label>.pid`` file, or None.

    One integer per file; anything else (missing, blank, non-numeric)
    reads as None so callers fall back to their other evidence.
    """
    if not path or not isinstance(path, str):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(64).strip().split()[0]
    except (OSError, IndexError, ValueError):
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


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
