"""One outcome derivation for every run, shared by every surface.

A run's outcome class answers the owner's question "what happened to
this run" in exactly one word, and no two meanings share a bucket:

- ``run``: still open (blue, filled dot, "running").
- ``ok``: a finish line was written and the verdict approves
  (green check, "finished").
- ``part``: a partial verdict (amber half circle, "partial").
- ``blocked``: the agent finished and REPORTED that work must come back
  (orange ban sign, "blocked"). Only agent-reported verdicts land here:
  ``BLOCKED``, ``REQUEST_CHANGES``, ``FIX``.
- ``died``: the session ended WITHOUT a verdict (red cross, "died"):
  killed, crashed, died early, the process vanished, a quota limit
  stopped it, or it reported failure. A ``BLOCKED`` verdict written by
  the quota/idle close-out is not a report, so those rows read died.
- ``cancelled``: cancelled from outside, never crashed (grey stop
  square, "cancelled"). Today that is the Dagster ``CANCELED`` rows.
- ``stalled``: the process is still alive but produced no output for far
  longer than the headless idle window (amber-ish ring, "stalled").
  Set only on open rows by the liveness layer (``run["stalled"]``).
- ``unk``: ended (or never confirmed open) with no verdict and no death
  evidence (grey hollow circle, "no verdict").

``outcome_class`` is the one derivation. The store exposes it as
``run.outcome_class`` (plus ``run.outcome_reason``) on ``/api/runs``
views and snapshot rows; ``static/models.js`` mirrors it as
``ADM.outcomeOf`` for the timeline, the replay, the tile caption, the
tables, and the drawer. No view keeps its own if-chain.

Tokens are pairs of (CSS token, glyph, word): colour never walks
alone, and amber/orange/red stay apart under red-green colour
blindness because the glyph plus the word carry the meaning too.
"""

from __future__ import annotations

# Class -> (CSS token, glyph, word). Mirrored by ADM.OUTCOME in
# static/models.js; keep the two tables identical.
OUTCOME = {
    "run": ("var(--s1)", "\u25cf", "running"),
    "ok": ("var(--good)", "\u2713", "finished"),
    "part": ("var(--warning)", "\u25d0", "partial"),
    "blocked": ("var(--serious)", "\u2298", "blocked"),
    "died": ("var(--critical)", "\u2715", "died"),
    "cancelled": ("var(--muted)", "\u23f9", "cancelled"),
    "stalled": ("var(--warning)", "\u25d4", "stalled"),
    "unk": ("var(--muted)", "\u25cb", "no verdict"),
}

CLASSES = tuple(OUTCOME)

# Verdicts the agent itself reported as "send this work back".
BLOCKED_VERDICTS = ("BLOCKED", "REQUEST_CHANGES", "FIX")

# Verdicts that approve, in whole or in the "finished" sense. APPROVE is
# a prefix so a truncated "APPROVEThe" still reads as approval; the
# partial approvals below take precedence and are matched first.
OK_PREFIXES = ("APPROVE", "PASS", "SUCCESS", "SHIP")

# Partial approvals. APPROVE_WITH_NITS is deliberately absent: like the
# tile caption and the verdict pill it reads as a pass ("ok").
PART_VERDICTS = ("PARTIAL", "APPROVE_WITH_CHANGES")

# Failure verdicts: the session reported failure, so it never finished.
DIED_VERDICTS = ("FAILURE", "FAIL", "UNRESOLVED")

# Verdict written by the quota close-out (dash/sources/*): the quota
# limit ended the run, the agent reported nothing.
QUOTA_VERDICT = "QUOTA"

# Verdict written by the idle close-out: the session went quiet and
# never finished.
IDLE_VERDICT = "IDLE"

# Verdict written by the dead-process reaper (dash/store/running.py,
# dash/sources/reaper.py): the pid exited and the out/err logs stayed
# quiet, so the run ended without ever writing a finish line.
DEAD_VERDICT = "DEAD"

# Terminal statuses: the session is over, whatever the verdict says.
OPEN_STATUSES = ("running", "queued")
FINISHED_STATUSES = ("done", "finished")


def outcome_class(run) -> str:
    """The outcome class for a run row. Never raises on a ragged row."""
    try:
        return _class_of(run)
    except Exception:
        return "unk"


def _get(run, key, default=None):
    if isinstance(run, dict):
        return run.get(key, default)
    return getattr(run, key, default)


def _class_of(run) -> str:
    status = _get(run, "status")
    verdict = _get(run, "verdict")
    ended = _get(run, "ended_at")
    if _get(run, "stalled") and status in OPEN_STATUSES and ended is None:
        return "stalled"
    if ended is None:
        return "run" if status == "running" else "unk"
    if status in ("killed", "died_early"):
        return "died"
    if status == "unknown":
        # The source could not confirm the end: a verdict on an
        # unconfirmed end reads no-verdict, never finished.
        return "unk"
    if isinstance(verdict, str):
        upper = verdict.upper()
        if upper in DIED_VERDICTS or upper in (QUOTA_VERDICT, IDLE_VERDICT, DEAD_VERDICT):
            return "died"
        if upper == "CANCELED":
            return "cancelled"
        if upper in BLOCKED_VERDICTS:
            return "blocked"
        if upper in PART_VERDICTS:
            return "part"
        if upper.startswith(OK_PREFIXES):
            return "ok"
    if status in FINISHED_STATUSES:
        # A finish line was written. A run with no recorded verdict still
        # reads finished: the "never reported" grey is for rows whose end
        # was never confirmed (status unknown / queued without an end).
        return "ok"
    return "unk"


def outcome_reason(run, outcome: str | None = None) -> str | None:
    """Why, in owner words, for tooltips. None when the word says it all.

    Reported verdicts (blocked, partial, approved) need no reason: the
    tooltip names the verdict itself. Deaths, stalls, and unknowns get
    one sentence.
    """
    try:
        return _reason_of(run, outcome or outcome_class(run))
    except Exception:
        return None


def _reason_of(run, outcome: str) -> str | None:
    status = _get(run, "status")
    verdict = _get(run, "verdict")
    upper = verdict.upper() if isinstance(verdict, str) else ""
    if outcome == "died":
        if status == "killed":
            return "terminated: SIGTERM trace in the err log"
        if upper == QUOTA_VERDICT:
            return "quota/rate limit ended the run"
        if upper == IDLE_VERDICT:
            return "closed idle: no output, session never finished"
        if upper == DEAD_VERDICT:
            return "process gone: pid exited and the logs went quiet"
        if upper in ("FAILURE", "FAIL"):
            return "reported FAILURE"
        if upper == "UNRESOLVED":
            return "ended unresolved"
        return "process exited without a verdict"
    if outcome == "stalled":
        age = _get(run, "stalled")
        when = _fmt_age(age) if isinstance(age, (int, float)) else None
        if when:
            return "no output for %s; process still alive" % when
        return "no output; process still alive"
    if outcome == "unk":
        return "no verdict recorded"
    return None


def _fmt_age(seconds) -> str | None:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    if seconds < 90:
        return "%ds" % int(seconds)
    minutes = seconds / 60.0
    if minutes < 90:
        return "%dm" % int(minutes)
    return "%.1fh" % (minutes / 60.0)
