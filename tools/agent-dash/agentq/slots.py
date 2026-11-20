"""Laptop-wide counting semaphores for ssh and pytest runs made by pi workers.

A slot is the file ``$AGENTQ_HOME/slots/<kind>/slot-<n>``. A holder takes
``flock(LOCK_EX | LOCK_NB)`` on the first free slot and keeps that descriptor
open for as long as its child runs. The kernel drops the lock when the holder
exits, crash included, so a dead holder never leaks a slot. The holder writes
one JSON record (pid, label, command, start epoch) into its slot for
``agentq slots``. Descriptors are non-inheritable, so the child never keeps a
slot alive after its holder is gone.

``ssh`` takes any free slot up to a fixed count. ``pytest`` also needs laptop
headroom (``pytest_admission``). A holder that waits longer than its max wait
runs anyway, unslotted, and logs an overrun, so a stuck slot can slow a worker
but never deadlock it. Limits come from ``broker.json`` with the broker's keys
and defaults. Stdlib only.

A run that has to wait registers a waiter file, ``slots/<kind>/waiters/<pid>``,
so the broker can see the queue as well as the holders (``live_state``). The file
is dropped on acquire, on timeout and on any exit, and a waiter whose pid is gone
is reaped by the next reader and recorded as ``reaped``.

Every acquire, release, timeout and reaped waiter appends one row to
``slots/events.jsonl``: epoch, kind, label, pid, wait and hold milliseconds, the
outcome and the test file paths of the command (never an environment value).
``history`` aggregates those rows per label and ``status_hint`` turns the last
hour into one line for ``agentq status``. The file is jsonl rather than a table
in ``agentq.db`` because this module is loaded by file path in arbitrary
interpreters and must never fail a run: an append cannot block on the broker's
write lock.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import random
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from agentq import broker
from agentq import headroom as headroom_lib

KINDS = ("ssh", "pytest")
DEFAULT_GATED_HOSTS: tuple[str, ...] = ()
SSH_HOSTS_FILE = "ssh_hosts"
WAITERS_DIR = "waiters"
EVENTS_FILE = "events.jsonl"
EVENTS_MAX_BYTES = 8 * 1024 * 1024
EVENT_ARGV_PATHS = 3
EVENT_ARGV_CHARS = 120
HISTORY_SINCE_S = 6 * 3600.0
STATUS_WINDOW_S = 3600.0
RECOMMEND_WAIT_MS = 60_000.0
RECOMMEND_STEP = 4
PYTEST_CEILING = 16
MAX_WAIT_S = 1800.0
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 5.0
COMMAND_CHARS = 300


@dataclass(frozen=True)
class Limits:
    """Slot counts and pytest admission thresholds for one agentq home."""

    ssh: int
    pytest: int
    adaptive: bool
    floor: int
    min_free_pct: float
    max_cpu_busy: float
    max_swap_frac: float
    gauge_settings: dict


class GaugeReader(Protocol):
    """Anything with ``read() -> Headroom``."""

    def read(self) -> headroom_lib.Headroom:
        """Current headroom."""


@dataclass(frozen=True)
class SlotState:
    """What one slot kind looks like right now: its cap, its holders and its waiters."""

    kind: str
    limit: int
    held: int
    waiting: int

    @property
    def busy(self) -> int:
        """Holders plus waiters: the pressure a new run would add to."""
        return self.held + self.waiting


class Holder:
    """A held slot (``index`` set), or an unslotted run after the max wait (``index`` None)."""

    def __init__(self, kind: str, index: Optional[int], fd: Optional[int], *, home=None, label: str = "-",
                 argv: str = "", started: Optional[float] = None, wait_ms: Optional[int] = None,
                 clock=time.time):
        self.kind = kind
        self.index = index
        self.fd = fd
        self.home = home
        self.label = label
        self.argv = argv
        self.started = started
        self.wait_ms = wait_ms
        self.clock = clock

    @property
    def slotted(self) -> bool:
        """True while this process holds a slot."""
        return self.fd is not None

    def release(self) -> None:
        """Drop the slot lock by closing its descriptor and record the hold. Safe to call twice."""
        if self.fd is None:
            return
        os.close(self.fd)
        self.fd = None
        if self.home is None:
            return
        hold_ms = None
        if self.started is not None:
            hold_ms = int(max(0.0, self.clock() - self.started) * 1000)
        record_event(self.home, kind=self.kind, label=self.label, pid=os.getpid(), wait_ms=self.wait_ms,
                     hold_ms=hold_ms, outcome="released", argv=self.argv, clock=self.clock)

    def __enter__(self) -> "Holder":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def read_broker_json(home) -> dict:
    """The raw ``broker.json`` object, or ``{}`` when it is missing or not an object."""
    data = _read_json(Path(home) / "broker.json")
    return data if isinstance(data, dict) else {}


def load_limits(home) -> Limits:
    """Limits for ``home``.

    ``slots.ssh``, ``adaptive`` and ``laptop`` use the broker's keys and defaults. The pytest
    ceiling is ``slots.tests`` when the file sets it, else ``PYTEST_CEILING``
    (16), because the broker's ``tests`` cap of 6 governs broker jobs, not shims.
    """
    config = broker.load_config(home)
    raw_slots = read_broker_json(home).get("slots")
    raw_slots = raw_slots if isinstance(raw_slots, dict) else {}
    ceiling = PYTEST_CEILING
    if "tests" in raw_slots:
        try:
            ceiling = int(raw_slots["tests"])
        except (TypeError, ValueError):
            ceiling = PYTEST_CEILING
    adaptive = config["adaptive"]
    return Limits(
        ssh=int(config["slots"]["ssh"]),
        pytest=ceiling,
        adaptive=bool(adaptive["enabled"]),
        floor=int(adaptive["tests_floor"]),
        min_free_pct=float(config["laptop"]["tests_min_free_pct"]),
        max_cpu_busy=float(config["laptop"]["tests_max_cpu_busy"]),
        max_swap_frac=float(config["laptop"]["max_swap_frac"]),
        gauge_settings=headroom_lib.settings(config),
    )


def gated_hosts(home) -> tuple[str, ...]:
    """``shims.ssh_gated_hosts`` from ``broker.json``, or ``DEFAULT_GATED_HOSTS`` (empty) when unset."""
    shims = read_broker_json(home).get("shims")
    hosts = shims.get("ssh_gated_hosts") if isinstance(shims, dict) else None
    if isinstance(hosts, list) and all(isinstance(host, str) for host in hosts):
        return tuple(hosts)
    return DEFAULT_GATED_HOSTS


def ssh_hosts_path(home) -> Path:
    """The file the sh ssh shim greps: one gated host pattern per line."""
    return Path(home) / "slots" / SSH_HOSTS_FILE


def write_ssh_hosts(home) -> Path:
    """Write the gated host patterns to ``ssh_hosts_path`` (empty file when none). Atomic; best effort."""
    path = ssh_hosts_path(home)
    text = "".join(f"{host.lower()}\n" for host in gated_hosts(home))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return path


def kind_limit(limits: Limits, kind: str) -> int:
    """The slot count for ``kind``."""
    return limits.ssh if kind == "ssh" else limits.pytest


class Gauges:
    """Laptop headroom shared by every shim through ``slots/gauge.json`` (see ``headroom.Gauges``)."""

    def __init__(self, home, *, settings: dict, memory=None, ticks=None, swap=None,
                 loadavg=os.getloadavg, cpus=os.cpu_count, clock=time.time):
        self._gauges = headroom_lib.Gauges(home, settings=settings, memory=memory, ticks=ticks, swap=swap,
                                           loadavg=loadavg, cpus=cpus, clock=clock)

    def read(self) -> headroom_lib.Headroom:
        """Current headroom, sampling only what is stale."""
        return self._gauges.read()


def pytest_admission(limits: Limits, gauges: GaugeReader) -> Callable[[int], bool]:
    """Admission rule for a new pytest run, given how many pytest slots are busy.

    Below the ceiling, a run starts when fewer than ``floor`` are busy, or when no tests bar is
    breached: memory free at least ``min_free_pct``, CPU busy under ``max_cpu_busy``, swap used
    under ``max_swap_frac``. An unreadable measure adds no gate, as in the broker.
    With adaptive admission off, only the ceiling applies.
    """

    def admit(running: int) -> bool:
        if running >= limits.pytest:
            return False
        if not limits.adaptive or running < limits.floor:
            return True
        breach = headroom_lib.pressure(
            gauges.read(), max_cpu_busy=limits.max_cpu_busy, min_free_pct=limits.min_free_pct,
            max_swap_frac=limits.max_swap_frac,
        )
        return breach is None

    return admit


def slots_dir(home, kind: str) -> Path:
    """Folder holding the slot files of ``kind``."""
    return Path(home) / "slots" / kind


def slot_path(home, kind: str, index: int) -> Path:
    """Path of slot ``index`` of ``kind``."""
    return slots_dir(home, kind) / f"slot-{index}"


def acquire(kind: str, *, limit: int, home, label: str = "-", command: str = "",
            admit: Optional[Callable[[int], bool]] = None, max_wait_s: float = MAX_WAIT_S,
            clock=time.time, sleep=time.sleep, jitter=random.random,
            warn: Optional[Callable[[str], None]] = None) -> Holder:
    """Take a ``kind`` slot, waiting with jittered backoff (0.5 s growing to 5 s).

    A run that has to wait registers a waiter file, so ``live_state`` counts it. The file is
    dropped on acquire, on timeout and on any exit. After ``max_wait_s`` the run goes ahead
    unslotted and an overrun is logged. Each acquire, release and timeout writes one history row.
    """
    directory = slots_dir(home, kind)
    directory.mkdir(parents=True, exist_ok=True)
    count = max(1, int(limit))
    record = {"kind": kind, "pid": os.getpid(), "label": label, "command": command[:COMMAND_CHARS]}
    argv = test_paths(command)
    started = clock()
    deadline = started + max_wait_s
    delay = BACKOFF_MIN_S
    waiter: Optional[Path] = None
    try:
        while True:
            holder = _take(kind, directory, count, admit, record, clock)
            if holder is not None:
                if waiter is not None:
                    _drop_waiter(waiter)
                    waiter = None
                wait_ms = int(max(0.0, clock() - started) * 1000)
                holder.home, holder.label, holder.argv = home, label, argv
                holder.started, holder.wait_ms, holder.clock = clock(), wait_ms, clock
                record_event(home, kind=kind, label=label, pid=os.getpid(), wait_ms=wait_ms,
                             hold_ms=None, outcome="acquired", argv=argv, clock=clock)
                return holder
            if waiter is None:
                waiter = _register_waiter(home, kind, record, started)
            if clock() >= deadline:
                message = f"agentq slots: {kind} wait exceeded {max_wait_s:g}s; running {label} unslotted"
                (warn or _warn)(message)
                _log(home, message)
                record_event(home, kind=kind, label=label, pid=os.getpid(),
                             wait_ms=int(max(0.0, clock() - started) * 1000), hold_ms=None,
                             outcome="timeout", argv=argv, clock=clock)
                return Holder(kind, None, None)
            sleep(delay * (0.8 + 0.4 * jitter()))
            delay = min(delay * 1.5, BACKOFF_MAX_S)
    finally:
        if waiter is not None:
            _drop_waiter(waiter)


def holders(home, kind: str, limit: int, *, clock=time.time) -> list[dict]:
    """Busy slots of ``kind``: slot, pid, label, command and seconds held."""
    now = clock()
    found = []
    for index in range(max(1, int(limit))):
        path = slot_path(home, kind, index)
        if not path.exists() or not _is_busy(path):
            continue
        record = _read_json(path)
        record = record if isinstance(record, dict) else {}
        started = record.get("started")
        found.append({
            "kind": kind,
            "slot": index,
            "pid": record.get("pid"),
            "label": record.get("label", ""),
            "command": record.get("command", "(starting)"),
            "held_s": round(now - started) if isinstance(started, (int, float)) else None,
        })
    return found


def waiters_dir(home, kind: str) -> Path:
    """Folder holding one file per process waiting for a ``kind`` slot."""
    return slots_dir(home, kind) / WAITERS_DIR


def live_state(home, kind: str, limit: Optional[int] = None, *, clock=time.time) -> SlotState:
    """Held and waiting counts for ``kind`` right now. Dead waiters are reaped first."""
    if limit is None:
        limit = kind_limit(load_limits(home), kind)
    count = max(1, int(limit))
    return SlotState(kind=kind, limit=count, held=_busy_count(slots_dir(home, kind), count),
                     waiting=reap_waiters(home, kind, clock=clock))


def reap_waiters(home, kind: str, *, clock=time.time) -> int:
    """Live waiter files of ``kind``. A waiter whose pid is gone is removed and recorded as reaped."""
    directory = waiters_dir(home, kind)
    try:
        paths = list(directory.iterdir())
    except OSError:
        return 0
    alive = 0
    for path in paths:
        record = _read_json(path)
        record = record if isinstance(record, dict) else {}
        pid = record.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            alive += 1
            continue
        try:
            path.unlink()
        except OSError:
            continue
        since = record.get("since")
        wait_ms = int(max(0.0, clock() - since) * 1000) if isinstance(since, (int, float)) else 0
        record_event(home, kind=kind, label=record.get("label") or "-", pid=pid, wait_ms=wait_ms,
                     hold_ms=None, outcome="reaped", argv=record.get("argv") or "", clock=clock)
    return alive


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` exists. A permission error means it exists and belongs to someone else."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _register_waiter(home, kind: str, record: dict, since: float) -> Optional[Path]:
    """Write this process's waiter file. Best effort: a failure only hides the wait from the broker."""
    path = waiters_dir(home, kind) / str(os.getpid())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(dict(record, since=since), sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def _drop_waiter(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def events_path(home) -> Path:
    """The slot history file: one JSON row per line, next to the slot folders."""
    return Path(home) / "slots" / EVENTS_FILE


def record_event(home, *, kind: str, label: str, pid, wait_ms, hold_ms, outcome: str, argv: str = "",
                 clock=time.time) -> None:
    """Append one slot event to ``slots/events.jsonl``. Best effort: a failure never fails a run."""
    row = {"ts": round(clock(), 3), "kind": kind, "label": label or "-", "pid": pid,
           "wait_ms": wait_ms, "hold_ms": hold_ms, "outcome": outcome, "argv": argv}
    path = events_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_events(path)
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _rotate_events(path: Path) -> None:
    """Move a full history file to ``events.jsonl.1``, replacing the previous one. Best effort."""
    try:
        if path.stat().st_size < EVENTS_MAX_BYTES:
            return
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def read_events(home, *, since: Optional[float] = None) -> list[dict]:
    """History rows at or after ``since``, oldest first. A bad line is skipped, never raised."""
    path = events_path(home)
    rows = []
    for candidate in (path.with_name(f"{path.name}.1"), path):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("ts"), (int, float)):
                continue
            if since is not None and row["ts"] < since:
                continue
            rows.append(row)
    rows.sort(key=lambda row: row["ts"])
    return rows


def percentile(values, pct: float) -> Optional[float]:
    """The nearest-rank percentile of ``values``, or None when there are none."""
    ordered = sorted(value for value in values if isinstance(value, (int, float)))
    if not ordered:
        return None
    rank = max(1, min(len(ordered), math.ceil(pct / 100.0 * len(ordered))))
    return float(ordered[rank - 1])


def history(home, *, since_s: float = HISTORY_SINCE_S, now=None) -> dict:
    """Per-label slot totals over the last ``since_s`` seconds.

    Each label gets ``runs`` (acquires), ``timeouts``, ``reaped``, total ``hold_ms`` and
    ``wait_ms``, their p50 and p95, and ``reruns``: an acquire whose test paths are identical to
    a run of the same label that is still holding, so the same command ran twice with no release
    in between. ``totals`` sums the labels.
    """
    since = (time.time() if now is None else now) - max(0.0, float(since_s))
    labels: dict[str, dict] = {}
    holding: dict[str, list[str]] = {}
    all_holds: list[float] = []
    all_waits: list[float] = []
    for row in read_events(home, since=since):
        label = row.get("label") or "-"
        entry = labels.setdefault(label, {"label": label, "runs": 0, "timeouts": 0, "reaped": 0,
                                          "hold_ms": 0.0, "wait_ms": 0.0,
                                          "holds": [], "waits": []})
        outcome = row.get("outcome")
        argv = row.get("argv") or ""
        if outcome == "acquired":
            entry["runs"] += 1
            open_argvs = holding.setdefault(label, [])
            if argv and argv in open_argvs:
                entry["reruns"] = entry.get("reruns", 0) + 1
            else:
                open_argvs.append(argv)
        elif outcome == "released":
            open_argvs = holding.get(label) or []
            if argv in open_argvs:
                open_argvs.remove(argv)
        elif outcome == "timeout":
            entry["timeouts"] += 1
        elif outcome == "reaped":
            entry["reaped"] += 1
        else:
            continue
        wait = row.get("wait_ms")
        if isinstance(wait, (int, float)):
            entry["wait_ms"] += wait
            entry["waits"].append(wait)
            all_waits.append(wait)
        hold = row.get("hold_ms")
        if isinstance(hold, (int, float)):
            entry["hold_ms"] += hold
            entry["holds"].append(hold)
            all_holds.append(hold)
    entries = []
    for entry in labels.values():
        entry["reruns"] = entry.get("reruns", 0)
        entry["hold_p50_ms"] = percentile(entry["holds"], 50)
        entry["hold_p95_ms"] = percentile(entry["holds"], 95)
        entry["wait_p50_ms"] = percentile(entry["waits"], 50)
        entry["wait_p95_ms"] = percentile(entry["waits"], 95)
        del entry["holds"], entry["waits"]
        entries.append(entry)
    entries.sort(key=lambda entry: (-entry["hold_ms"], entry["label"]))
    totals = {"runs": sum(entry["runs"] for entry in entries),
              "timeouts": sum(entry["timeouts"] for entry in entries),
              "reaped": sum(entry["reaped"] for entry in entries),
              "reruns": sum(entry["reruns"] for entry in entries),
              "hold_ms": sum(entry["hold_ms"] for entry in entries),
              "wait_ms": sum(entry["wait_ms"] for entry in entries),
              "hold_p50_ms": percentile(all_holds, 50),
              "hold_p95_ms": percentile(all_holds, 95),
              "wait_p50_ms": percentile(all_waits, 50),
              "wait_p95_ms": percentile(all_waits, 95)}
    return {"since": since, "now": time.time() if now is None else now, "labels": entries, "totals": totals}


def format_history(report: dict) -> str:
    """The ``agentq slots --history`` table: one row per label, then the totals."""
    lines = [f"slot history {_stamp(report['since'])} to {_stamp(report['now'])}"]
    if not report["labels"]:
        lines.append("no slot events in the window")
        return "\n".join(lines)
    lines.append(f"{'label':<28} {'runs':>4} {'hold':>8} {'wait':>8} {'p50':>7} {'p95':>7} {'reruns':>6}")
    for entry in [*report["labels"], dict(report["totals"], label="total")]:
        lines.append(f"{entry['label'][:28]:<28} {entry['runs']:>4} {format_ms(entry['hold_ms']):>8} "
                     f"{format_ms(entry['wait_ms']):>8} {format_ms(entry['wait_p50_ms']):>7} "
                     f"{format_ms(entry['wait_p95_ms']):>7} {entry['reruns']:>6}")
    return "\n".join(lines)


def status_hint(home, *, now=None, gauges: Optional[GaugeReader] = None,
                since_s: float = STATUS_WINDOW_S) -> str:
    """One line for ``agentq status``: pytest slot utilization, median wait and a cap hint.

    Utilization is held time over capacity (``slots.tests`` slots times the window). The cap is
    only recommended up when the median wait is at or above ``RECOMMEND_WAIT_MS`` and the laptop
    has headroom at the tests bars. Nothing is changed here: the line is a hint for the owner.
    """
    limits = load_limits(home)
    cap = max(1, limits.pytest)
    now = time.time() if now is None else now
    rows = read_events(home, since=now - since_s)
    held_ms = sum(row["hold_ms"] for row in rows
                  if row.get("outcome") == "released" and isinstance(row.get("hold_ms"), (int, float)))
    waits = [row["wait_ms"] for row in rows
             if row.get("outcome") in ("acquired", "timeout") and isinstance(row.get("wait_ms"), (int, float))]
    utilization = held_ms / (since_s * 1000.0 * cap)
    median = percentile(waits, 50)
    text = (f"slots: pytest {utilization:.0%} utilized over {_window(since_s)} (cap {cap}), "
            f"median wait {format_ms(median)}")
    if not waits:
        return text + "; no waits recorded"
    if median < RECOMMEND_WAIT_MS:
        return text + f"; keep slots.tests at {cap}"
    reader = gauges or Gauges(home, settings=limits.gauge_settings)
    breach = headroom_lib.pressure(reader.read(), max_cpu_busy=limits.max_cpu_busy,
                                   min_free_pct=limits.min_free_pct, max_swap_frac=limits.max_swap_frac)
    if breach is not None:
        return text + f"; keep slots.tests at {cap} ({breach[1]})"
    ceiling = broker.load_config(home)["tests_ceiling"]
    target = min(ceiling, cap + RECOMMEND_STEP)
    if target <= cap:
        return text + f"; keep slots.tests at {cap} (ceiling {ceiling})"
    return text + f"; waits high with headroom: raise slots.tests to {target} (ceiling {ceiling})"


def test_paths(command: str, *, limit: int = EVENT_ARGV_PATHS, chars: int = EVENT_ARGV_CHARS) -> str:
    """The test file paths in a pytest command line, for the history row.

    Only arguments that name a test file (``.py``, or a ``file.py::test`` node id) are kept, at
    most ``limit`` of them, joined and cut to ``chars``. Options, their values and every
    environment value stay out of the record.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    paths = [word for word in words[1:] if not word.startswith("-") and ".py" in word]
    return " ".join(paths[:limit])[:chars]


def format_ms(ms) -> str:
    """A millisecond count as a short duration, or ``-`` when there is none."""
    if not isinstance(ms, (int, float)):
        return "-"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _window(seconds: float) -> str:
    return format_ms(seconds * 1000.0)


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _take(kind, directory: Path, count: int, admit, record: dict, clock) -> Optional[Holder]:
    """One pass: admission first, then the first free slot. None when none is free."""
    if admit is not None and not admit(_busy_count(directory, count)):
        return None
    for index in range(count):
        fd = _lock(directory / f"slot-{index}")
        if fd is not None:
            _write_json_fd(fd, dict(record, started=clock()))
            return Holder(kind, index, fd)
    return None


def _busy_count(directory: Path, count: int) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for index in range(count) if _is_busy(directory / f"slot-{index}"))


def _lock(path: Path) -> Optional[int]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _is_busy(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _write_json_fd(fd: int, record: dict) -> None:
    os.ftruncate(fd, 0)
    os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, record: dict) -> None:
    """Best effort atomic write: a temp file renamed over ``path``."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _log(home, message: str) -> None:
    """Append one timestamped line to ``slots/overruns.log``. Best effort."""
    try:
        path = Path(home) / "slots" / "overruns.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{int(time.time())} {message}\n")
    except OSError:
        pass


def _warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
