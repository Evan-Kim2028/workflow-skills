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
"""

from __future__ import annotations

import fcntl
import json
import os
import random
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


class Holder:
    """A held slot (``index`` set), or an unslotted run after the max wait (``index`` None)."""

    def __init__(self, kind: str, index: Optional[int], fd: Optional[int]):
        self.kind = kind
        self.index = index
        self.fd = fd

    @property
    def slotted(self) -> bool:
        """True while this process holds a slot."""
        return self.fd is not None

    def release(self) -> None:
        """Drop the slot lock by closing its descriptor. Safe to call twice."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

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

    After ``max_wait_s`` the run goes ahead unslotted and an overrun is logged.
    """
    directory = slots_dir(home, kind)
    directory.mkdir(parents=True, exist_ok=True)
    count = max(1, int(limit))
    record = {"kind": kind, "pid": os.getpid(), "label": label, "command": command[:COMMAND_CHARS]}
    deadline = clock() + max_wait_s
    delay = BACKOFF_MIN_S
    while True:
        holder = _take(kind, directory, count, admit, record, clock)
        if holder is not None:
            return holder
        if clock() >= deadline:
            message = f"agentq slots: {kind} wait exceeded {max_wait_s:g}s; running {label} unslotted"
            (warn or _warn)(message)
            _log(home, message)
            return Holder(kind, None, None)
        sleep(delay * (0.8 + 0.4 * jitter()))
        delay = min(delay * 1.5, BACKOFF_MAX_S)


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
