"""Laptop headroom for the broker's gates: CPU busy, memory free, swap used.

Load average is not used to hold work. Short-lived git, rg and python processes inflate
the run queue while half the CPU sits idle, so load per CPU reports a hot laptop that is not
hot. ``Gauges`` reads the three measures that track real pressure:

- CPU busy: the fraction of CPU time spent non-idle, from cumulative tick counters (macOS
  ``host_processor_info`` through ctypes, Linux ``/proc/stat``). The raw fraction between two
  samples is smoothed by an exponential moving average with a time constant of ``tau_s``.
- Memory free: percent of physical memory free (macOS ``memory_pressure``, Linux
  ``MemAvailable``), cached for ``memory_ttl_s``.
- Swap used: used / total swap (macOS ``sysctl vm.swapusage``, Linux ``/proc/meminfo``).

State lives in ``<home>/slots/gauge.json`` so the broker, the fill controller, the pytest
shims and the dashboard read one set of samples. A reader that finds a measure due takes the
interprocess lock ``slots/gauge.lock``, samples, and writes the state back under that lock. A
reader that finds the lock busy returns the saved state without sampling. Each measure carries
an "until" time: past it, the measure is unknown and never holds work. A failed CPU sample
therefore cannot keep an old reading alive. Load per CPU is kept on ``Headroom`` for reporting
only. Stdlib only.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional

GAUGE_FILE = Path("slots") / "gauge.json"
LOCK_FILE = Path("slots") / "gauge.lock"
SYSCTL_DARWIN = "/usr/sbin/sysctl"
EXPIRY_SAMPLES = 3
DEFAULT_SETTINGS = {"memory_ttl_s": 15.0, "sample_s": 10.0, "tau_s": 60.0}
MEMORY_FREE_LINE = re.compile(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%")
SWAP_USAGE = re.compile(r"\b(total|used)\s*=\s*([\d.]+)([MG])")
PROCESSOR_CPU_LOAD_INFO = 2
CPU_STATE_COUNT = 4
DARWIN_IDLE, DARWIN_USER, DARWIN_SYSTEM, DARWIN_NICE = 2, 0, 1, 3

CpuTicks = tuple[int, int]


class Headroom(NamedTuple):
    """One reading. Every field is None when unreadable; an unreadable measure never holds work.

    ``cpu_busy`` and ``swap_frac`` are fractions from 0 to 1; ``free_pct`` is a percent.
    ``load_per_cpu`` is reported only.
    """

    free_pct: Optional[float]
    cpu_busy: Optional[float]
    swap_frac: Optional[float]
    load_per_cpu: Optional[float]


def settings(cfg: Mapping) -> dict:
    """Sampling cadence from a loaded broker config: memory TTL from ``adaptive``, CPU and smoothing from ``laptop``."""
    laptop = cfg.get("laptop", {})
    return {
        "memory_ttl_s": float(cfg["adaptive"]["gauge_ttl_s"]),
        "sample_s": float(laptop.get("sample_s", DEFAULT_SETTINGS["sample_s"])),
        "tau_s": float(laptop.get("ewma_s", DEFAULT_SETTINGS["tau_s"])),
    }


def pressure(reading: Headroom, *, max_cpu_busy: float, min_free_pct: float, max_swap_frac: float) -> tuple[str, str] | None:
    """The first bar the reading breaches as (key, text), or None when every readable measure is clear.

    The key names the bar, not the live number, so a held job keeps one hold identity while its
    reading moves. The text quotes the live number against the bar.
    """
    if reading.cpu_busy is not None and reading.cpu_busy >= max_cpu_busy:
        bar = f"{max_cpu_busy * 100:.0f}%"
        return f"cpu busy >= {bar}", f"cpu {reading.cpu_busy * 100:.0f}% busy >= {bar}"
    if reading.free_pct is not None and reading.free_pct < min_free_pct:
        bar = f"{min_free_pct:.0f}%"
        return f"memory free < {bar}", f"memory free {reading.free_pct:.0f}% < {bar}"
    if reading.swap_frac is not None and reading.swap_frac >= max_swap_frac:
        bar = f"{max_swap_frac * 100:.0f}%"
        return f"swap used >= {bar}", f"swap {reading.swap_frac * 100:.0f}% used >= {bar}"
    return None


def cpu_fraction(prev: Optional[CpuTicks], ticks: Optional[CpuTicks]) -> Optional[float]:
    """Busy fraction between two cumulative (busy, total) readings, or None when there is no usable interval.

    A counter that went backwards (a reboot, or a corrupt state file) gives None and restarts the baseline.
    """
    if prev is None or ticks is None:
        return None
    busy, total = ticks[0] - prev[0], ticks[1] - prev[1]
    if total <= 0 or busy < 0:
        return None
    return min(1.0, busy / total)


def smooth(prev: Optional[float], prev_at: Optional[float], value: float, now: float, tau_s: float) -> float:
    """Exponential moving average of ``value`` over time. ``prev`` None or unknown age starts at ``value``."""
    if prev is None or prev_at is None:
        return value
    alpha = 1.0 - math.exp(-max(0.0, now - prev_at) / tau_s)
    return prev + alpha * (value - prev)


def parse_proc_stat(text: str) -> Optional[CpuTicks]:
    """(busy, total) ticks from the aggregate ``cpu`` line of ``/proc/stat``. Guest time is inside user time, so it is not added again."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = [int(value) for value in line.split()[1:9]]
            if len(fields) < 4:
                return None
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            total = sum(fields)
            return total - idle, total
    return None


def darwin_ticks(values: list[int]) -> CpuTicks:
    """(busy, total) from the flat ``host_processor_info`` array: four states per CPU, idle is state 2."""
    busy = total = 0
    for base in range(0, len(values) - CPU_STATE_COUNT + 1, CPU_STATE_COUNT):
        user, system = values[base + DARWIN_USER], values[base + DARWIN_SYSTEM]
        nice, idle = values[base + DARWIN_NICE], values[base + DARWIN_IDLE]
        busy += user + system + nice
        total += user + system + nice + idle
    return busy, total


@lru_cache(maxsize=1)
def _darwin_libc():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.mach_host_self.restype = ctypes.c_uint
    lib.mach_host_self.argtypes = []
    lib.host_processor_info.restype = ctypes.c_int
    lib.host_processor_info.argtypes = [
        ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint),
    ]
    lib.vm_deallocate.restype = ctypes.c_int
    lib.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    return lib


def read_darwin_ticks() -> Optional[CpuTicks]:
    """Cumulative CPU ticks from ``host_processor_info``. The kernel's array is freed before returning."""
    try:
        lib = _darwin_libc()
        count, info_count = ctypes.c_uint(0), ctypes.c_uint(0)
        info = ctypes.POINTER(ctypes.c_uint)()
        status = lib.host_processor_info(
            lib.mach_host_self(), PROCESSOR_CPU_LOAD_INFO,
            ctypes.byref(count), ctypes.byref(info), ctypes.byref(info_count),
        )
        if status != 0 or not info:
            return None
        values = [info[index] for index in range(info_count.value)]
        task = ctypes.c_uint.in_dll(lib, "mach_task_self_").value
        lib.vm_deallocate(task, ctypes.cast(info, ctypes.c_void_p).value, info_count.value * ctypes.sizeof(ctypes.c_uint))
        return darwin_ticks(values)
    except (OSError, AttributeError, ValueError):
        return None


def read_linux_ticks(path: str = "/proc/stat") -> Optional[CpuTicks]:
    """Cumulative CPU ticks from ``/proc/stat``, or None when unreadable."""
    try:
        return parse_proc_stat(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_cpu_ticks(platform: str | None = None) -> Optional[CpuTicks]:
    """Cumulative (busy, total) CPU ticks for this platform, or None."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return read_darwin_ticks()
    if platform.startswith("linux"):
        return read_linux_ticks()
    return None


def parse_swapusage(text: str) -> Optional[float]:
    """Used fraction from ``sysctl -n vm.swapusage`` output. No configured swap is 0.0, not unknown."""
    sizes = {name: float(value) * (1024.0 if unit == "G" else 1.0) for name, value, unit in SWAP_USAGE.findall(text)}
    if "total" not in sizes or "used" not in sizes:
        return None
    if sizes["total"] <= 0:
        return 0.0
    return min(1.0, sizes["used"] / sizes["total"])


def parse_meminfo(text: str) -> dict[str, float]:
    """``/proc/meminfo`` fields in kB, keyed by name without the colon."""
    fields: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                fields[parts[0][:-1]] = float(parts[1])
            except ValueError:
                continue
    return fields


def swap_frac_from_meminfo(fields: dict[str, float]) -> Optional[float]:
    """Used swap fraction from parsed ``/proc/meminfo`` fields."""
    total, free = fields.get("SwapTotal"), fields.get("SwapFree")
    if total is None or free is None:
        return None
    if total <= 0:
        return 0.0
    return min(1.0, (total - free) / total)


def sysctl_path() -> str:
    """``/usr/sbin/sysctl`` by absolute path when it exists, else a PATH lookup. Workers run with a PATH that omits /usr/sbin."""
    if os.path.exists(SYSCTL_DARWIN):
        return SYSCTL_DARWIN
    return shutil.which("sysctl") or "sysctl"


def read_swap_frac(run=subprocess.run, platform: str | None = None) -> Optional[float]:
    """Used fraction of swap, or None when unreadable."""
    platform = sys.platform if platform is None else platform
    try:
        if platform == "darwin":
            return parse_swapusage(run([sysctl_path(), "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout or "")
        if platform.startswith("linux"):
            return swap_frac_from_meminfo(parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8")))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def memory_free_pct(run=subprocess.run, platform: str | None = None) -> Optional[float]:
    """Free physical memory percent: macOS ``memory_pressure``, Linux ``MemAvailable``. None when unavailable."""
    platform = sys.platform if platform is None else platform
    try:
        if platform == "darwin":
            out = run(["memory_pressure"], capture_output=True, text=True, timeout=5).stdout
            match = MEMORY_FREE_LINE.search(out or "")
            return float(match.group(1)) if match else None
        if platform.startswith("linux"):
            return memory_free_from_meminfo(parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8")))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def memory_free_from_meminfo(fields: dict[str, float]) -> Optional[float]:
    """Free memory percent from parsed ``/proc/meminfo`` fields: MemAvailable over MemTotal."""
    total, available = fields.get("MemTotal"), fields.get("MemAvailable")
    if not total or available is None:
        return None
    return 100.0 * available / total


def reading_from_state(state: Mapping, now: float) -> Headroom:
    """The reading a saved state holds at ``now``: each measure counts only before its "until" time. Reads nothing new."""
    def live(value_key: str, until_key: str) -> Optional[float]:
        value, until = _number(state.get(value_key)), _number(state.get(until_key))
        return value if value is not None and until is not None and now < until else None

    return Headroom(
        free_pct=live("free", "free_until"),
        cpu_busy=live("cpu_busy", "cpu_until"),
        swap_frac=live("swap", "swap_until"),
        load_per_cpu=None,
    )


def load_state(home) -> dict:
    """The saved gauge state under ``home``, or ``{}`` when missing or unreadable."""
    try:
        data = json.loads((Path(home) / GAUGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _try_lock(home) -> Optional[int]:
    """Descriptor holding the exclusive gauge lock, or None when another reader holds it (or it cannot be taken).

    Close the descriptor to release the lock.
    """
    path = Path(home) / LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _due(state: Mapping, until_key: str, now: float) -> bool:
    until = _number(state.get(until_key))
    return until is None or now >= until


def _save_state(home, state: dict) -> None:
    """Write the gauge state atomically. Best effort: a failed write only means the next reader samples again."""
    path = Path(home) / GAUGE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class Gauges:
    """Samples the laptop's headroom: memory at most once per ``memory_ttl_s``, CPU and swap once per ``sample_s``.

    With ``home`` the state persists in ``slots/gauge.json`` and sampling is serialized by the
    lock in ``slots/gauge.lock``. A reader that cannot take the lock returns the saved state
    as it stands, with no sample and no write. Without ``home`` the state stays in memory,
    which is how the tests run. Cadence settings are read when the object is built; thresholds
    are the caller's to apply with ``pressure``. ``ticks``, ``swap``, ``memory``, ``loadavg``,
    ``cpus`` and ``clock`` are injectable for tests.
    """

    def __init__(self, home=None, *, settings: Optional[Mapping] = None,
                 memory: Callable[[], Optional[float]] | None = None,
                 ticks: Callable[[], Optional[CpuTicks]] | None = None,
                 swap: Callable[[], Optional[float]] | None = None,
                 loadavg=os.getloadavg, cpus=os.cpu_count, clock=time.time):
        self.home = home
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.memory = memory or memory_free_pct
        self.ticks = ticks or read_cpu_ticks
        self.swap = swap or read_swap_frac
        self.loadavg = loadavg
        self.cpus = cpus
        self.clock = clock
        self._state: dict = {}

    def read(self) -> Headroom:
        """Current headroom, sampling whatever is due. Never raises on an unreadable measure."""
        if self.home is None:
            self._state, now = self._refresh(self._state)
            return self._reading(self._state, now)
        fd = _try_lock(self.home)
        if fd is None:
            return self._reading(load_state(self.home), self.clock())
        try:
            state, now = self._refresh(load_state(self.home))
            _save_state(self.home, state)
            return self._reading(state, now)
        finally:
            os.close(fd)

    def _refresh(self, saved: Mapping) -> tuple[dict, float]:
        """The saved state with every due measure sampled. Reads the clock here, after any lock is held,
        so a reader never reasons from a time older than the state it found."""
        state = dict(saved)
        now = self.clock()
        expiry = EXPIRY_SAMPLES * self.settings["sample_s"]
        if _due(state, "free_until", now):
            state["free"] = self.memory()
            state["free_until"] = now + self.settings["memory_ttl_s"]
        if _due(state, "cpu_next", now):
            ticks = self.ticks()
            sample = cpu_fraction(_pair(state.get("ticks")), ticks)
            state["cpu_next"] = now + self.settings["sample_s"]
            if ticks is not None:
                state["ticks"] = list(ticks)
            if sample is not None:
                state["cpu_busy"] = smooth(_number(state.get("cpu_busy")), _number(state.get("cpu_at")),
                                           sample, now, self.settings["tau_s"])
                state["cpu_at"] = now
                state["cpu_until"] = now + expiry
            state["swap"] = self.swap()
            state["swap_until"] = now + expiry
        return state, now

    def _reading(self, state: Mapping, now: float) -> Headroom:
        return reading_from_state(state, now)._replace(load_per_cpu=self._load_per_cpu())

    def _load_per_cpu(self) -> Optional[float]:
        try:
            return self.loadavg()[0] / (self.cpus() or 1)
        except (OSError, IndexError):
            return None


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _pair(value) -> Optional[CpuTicks]:
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, int) for item in value):
        return value[0], value[1]
    return None
