"""Stop a pi worker that repeats one tool call, or compacts without end.

A pi worker can wedge into a loop: agentq job ``7703beba8651``
(``pi-deepseek-flash``) ran 4 h 07 m, made 5,796 tool calls and 44
compactions, and its last 30 calls were the identical ``bash`` command. It
committed nothing, and nothing noticed until the lead's 4 h wait timed out.
The model was not failing; it was re-reading the same file forever.

This module watches the run's event stream while the worker is live and
trips on two shapes:

* **A repeated call.** Every ``tool_execution_start`` is keyed by a hash of
  ``(toolName, args)``. The guard trips when one key repeats ``run`` times in
  a row (``AGENTQ_REPEAT_RUN``, default 8) or ``count`` times inside the last
  ``window`` calls (``AGENTQ_REPEAT_COUNT`` of ``AGENTQ_REPEAT_WINDOW``,
  default 12 of 30). The window catches a loop that alternates two calls.
* **Compaction without end.** Each ``compaction_start`` counts; ``C`` of them
  (``AGENTQ_REPEAT_COMPACTIONS``, default 20) trips. A run that compacts that
  often is spending its context on the same dead end.

``AGENTQ_REPEAT_GUARD=0`` turns the guard off. The launcher
(``scripts/launch_pi.py``) starts a ``StreamWatcher`` before the child, stops
the child on a trip, and writes the ``BLOCKED`` report this module renders, so
the broker reads a finish with a verdict instead of a death.

The stream is written by the detached ``dash/logfilter.py`` process, so the
watcher tails the file by byte offset and tolerates a missing file, a partial
last line, and a truncation (a resumed run reopens the same path).
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

#: Identical calls in a row that trip the guard (``AGENTQ_REPEAT_RUN``).
DEFAULT_RUN = 8
#: Identical calls inside the window that trip the guard (``AGENTQ_REPEAT_COUNT``).
DEFAULT_COUNT = 12
#: Tool calls the window holds (``AGENTQ_REPEAT_WINDOW``).
DEFAULT_WINDOW = 30
#: Compactions that trip the guard (``AGENTQ_REPEAT_COMPACTIONS``).
DEFAULT_COMPACTIONS = 20
RUN_ENV = "AGENTQ_REPEAT_RUN"
COUNT_ENV = "AGENTQ_REPEAT_COUNT"
WINDOW_ENV = "AGENTQ_REPEAT_WINDOW"
COMPACTIONS_ENV = "AGENTQ_REPEAT_COMPACTIONS"
OFF_ENV = "AGENTQ_REPEAT_GUARD"
#: Seconds between stream polls (``AGENTQ_REPEAT_POLL_S``). The filter flushes at most every 2 s.
POLL_ENV = "AGENTQ_REPEAT_POLL_S"
POLL_S = 1.0
#: Characters of the repeated call's arguments kept in the report.
ARGS_PREVIEW = 400
#: Seconds between SIGTERM and SIGKILL for a stopped child.
KILL_GRACE_S = 5.0


def _int_env(environ, name: str, default: int) -> int:
    """A positive integer from ``environ``, else ``default``; a bad value never raises."""
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Limits:
    """The guard's thresholds, from the environment with the defaults above."""

    run: int = DEFAULT_RUN
    count: int = DEFAULT_COUNT
    window: int = DEFAULT_WINDOW
    compactions: int = DEFAULT_COMPACTIONS

    @classmethod
    def from_env(cls, environ=None) -> "Limits":
        environ = os.environ if environ is None else environ
        return cls(
            run=_int_env(environ, RUN_ENV, DEFAULT_RUN),
            count=_int_env(environ, COUNT_ENV, DEFAULT_COUNT),
            window=_int_env(environ, WINDOW_ENV, DEFAULT_WINDOW),
            compactions=_int_env(environ, COMPACTIONS_ENV, DEFAULT_COMPACTIONS),
        )

    def describe(self) -> str:
        return (
            f"{self.run} in a row, {self.count} of the last {self.window}, "
            f"{self.compactions} compactions"
        )


def enabled(environ=None) -> bool:
    """False when ``AGENTQ_REPEAT_GUARD=0``."""
    environ = os.environ if environ is None else environ
    return (environ.get(OFF_ENV) or "").strip() != "0"


def poll_interval(environ=None) -> float:
    """Seconds between stream polls: ``AGENTQ_REPEAT_POLL_S``, else ``POLL_S``."""
    environ = os.environ if environ is None else environ
    raw = (environ.get(POLL_ENV) or "").strip()
    if not raw:
        return POLL_S
    try:
        value = float(raw)
    except ValueError:
        return POLL_S
    return value if value > 0 else POLL_S


def call_key(tool_name, args) -> str:
    """Stable hash of one tool call: the tool name plus its arguments, order-free."""
    try:
        body = json.dumps([tool_name, args], sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = repr([tool_name, args])
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def args_preview(args, limit: int = ARGS_PREVIEW) -> str:
    """The call's arguments as one line, cut to ``limit`` characters."""
    try:
        text = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Trip:
    """Why the guard stopped a run: the repeated call, or the compaction count."""

    kind: str
    tool: str = ""
    args: str = ""
    run: int = 0
    count: int = 0
    calls: int = 0
    compactions: int = 0
    limits: Limits = field(default_factory=Limits)

    def summary(self) -> str:
        if self.kind == "compactions":
            return f"compacted {self.compactions} times"
        if self.run >= self.limits.run:
            return f"repeated one tool call {self.run} times in a row"
        return f"repeated one tool call {self.count} times in the last {self.limits.window}"


class RepetitionGuard:
    """Feed it stream events; it returns a ``Trip`` once a run has wedged.

    ``observe`` is idempotent after a trip: the first ``Trip`` is kept and
    returned again, so a watcher that reads a few more lines cannot change
    the reason the run was stopped.
    """

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits.from_env()
        self.calls = 0
        self.compactions = 0
        self.trip: Trip | None = None
        self._window: deque[str] = deque(maxlen=self.limits.window)
        self._run_key: str | None = None
        self._run_len = 0
        self._last_tool = ""
        self._last_args = ""

    def observe(self, event) -> Trip | None:
        """Count one stream event; return the trip when this event crosses a limit."""
        if self.trip is not None:
            return self.trip
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind == "compaction_start":
            self.compactions += 1
            if self.compactions >= self.limits.compactions:
                self.trip = Trip(
                    kind="compactions",
                    tool=self._last_tool,
                    args=self._last_args,
                    calls=self.calls,
                    compactions=self.compactions,
                    limits=self.limits,
                )
        elif kind == "tool_execution_start":
            self._observe_call(event.get("toolName"), event.get("args"))
        return self.trip

    def _observe_call(self, tool_name, args) -> None:
        key = call_key(tool_name, args)
        self.calls += 1
        self._last_tool = str(tool_name or "")
        self._last_args = args_preview(args)
        self._run_len = self._run_len + 1 if key == self._run_key else 1
        self._run_key = key
        self._window.append(key)
        count = self._window.count(key)
        if self._run_len < self.limits.run and count < self.limits.count:
            return
        self.trip = Trip(
            kind="repeat",
            tool=self._last_tool,
            args=self._last_args,
            run=self._run_len,
            count=count,
            calls=self.calls,
            compactions=self.compactions,
            limits=self.limits,
        )


def report(trip: Trip, label: str, model: str, stream_path=None) -> str:
    """The ``BLOCKED`` report the launcher writes for a stopped run.

    The first line opens with ``BLOCKED`` so the broker's verdict reader
    (``agentq/broker.py``) records a finish with a verdict, not a death.
    """
    if trip.kind == "compactions":
        head = (
            f"BLOCKED: pi compacted its context {trip.compactions} times; "
            "the launcher's repetition guard stopped the worker"
        )
    else:
        head = f"BLOCKED: pi {trip.summary()}; the launcher's repetition guard stopped the worker"
    lines = [
        head,
        "",
        "The guard in `scripts/launch_pi.py` stopped this run, not the model. "
        "Nothing after the loop was attempted, and the run made no commit.",
        "",
        f"- label: `{label}`",
        f"- model: `{model}`",
    ]
    if trip.tool:
        lines.append(f"- last tool call: `{trip.tool}`")
    if trip.args:
        lines.append(f"- its arguments: `{trip.args}`")
    if trip.kind == "repeat":
        lines.append(f"- identical calls in a row: {trip.run}")
        lines.append(f"- identical calls in the last {trip.limits.window}: {trip.count}")
    lines.append(f"- tool calls seen: {trip.calls}")
    lines.append(f"- compactions: {trip.compactions}")
    lines.append(f"- limits: {trip.limits.describe()}")
    lines.append(
        "- overrides: `AGENTQ_REPEAT_RUN`, `AGENTQ_REPEAT_COUNT`, "
        "`AGENTQ_REPEAT_WINDOW`, `AGENTQ_REPEAT_COMPACTIONS`; "
        "`AGENTQ_REPEAT_GUARD=0` turns the guard off"
    )
    if stream_path is not None:
        lines.append(f"- stream: `{stream_path}`")
    lines += [
        "",
        "The lead should resubmit with a narrower task, a different model, or a "
        "prompt that names the file to read.",
        "",
    ]
    return "\n".join(lines)


def stop_child(pid, grace_s: float = KILL_GRACE_S) -> bool:
    """SIGTERM a child by pid, then SIGKILL it after ``grace_s``.

    Returns True when a live process was signalled and False when the pid was
    already gone, so a caller can tell a stop from a run that ended on its own.
    """
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


class StreamWatcher:
    """Tail a pi stream file on a thread and feed each event to a guard.

    ``on_trip`` runs once, on the watcher thread, with the ``Trip``. The
    watcher tolerates a file that does not exist yet, a partial last line,
    and a truncation (a resumed run reopens the same path), and it stops
    itself after a trip.
    """

    def __init__(self, path, guard: RepetitionGuard, on_trip, poll_s: float = POLL_S) -> None:
        self.path = Path(path)
        self.guard = guard
        self.on_trip = on_trip
        self.poll_s = poll_s
        self.trip: Trip | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="repetition-guard", daemon=True)

    def start(self) -> "StreamWatcher":
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the watcher and wait for it; a trip's SIGTERM-to-SIGKILL grace is bounded."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=KILL_GRACE_S + self.poll_s * 2 + 1)

    def _run(self) -> None:
        offset = 0
        pending = b""
        while not self._stop.is_set():
            try:
                size = self.path.stat().st_size
            except OSError:
                size = None
            if size is not None and size < offset:
                offset, pending = 0, b""
            if size is not None and size > offset:
                try:
                    with open(self.path, "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                except OSError:
                    chunk = b""
                offset += len(chunk)
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    if self._feed(line):
                        return
            self._stop.wait(self.poll_s)

    def _feed(self, line: bytes) -> bool:
        """Feed one line; True when the guard tripped and the watcher should stop."""
        line = line.strip()
        if not line:
            return False
        try:
            event = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return False
        trip = self.guard.observe(event)
        if trip is None:
            return False
        self.trip = trip
        try:
            self.on_trip(trip)
        finally:
            self._stop.set()
        return True
