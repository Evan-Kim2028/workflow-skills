"""In-process performance telemetry: latencies, polls, lock, caches, CPU.

One global monitor (see :func:`get`) collects fixed-window samples —
per-endpoint latency, per-source poll duration and lag, store lock wait
and hold, single-flight sharing, result-cache hit ratios, thread count,
process CPU — and serves them as the ``perf`` block of ``/api/health``.
Everything here is additive and best-effort: recording never raises,
rings are bounded, and a missing monitor reads as empty rather than
failing. No parameter values are kept except workstream ids.
"""

from __future__ import annotations

import threading
import time
import sys
import traceback

WINDOW_S = 600.0
MAX_SAMPLES = 5000
MAX_POLL_HISTORY = 60


def _pct(sorted_vals: list, pct: float):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (len(sorted_vals) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(sorted_vals) - 1)
    frac = rank - low
    return sorted_vals[low] + (sorted_vals[high] - sorted_vals[low]) * frac


class _Ring:
    """Timestamped floats over the last WINDOW_S, bounded count."""

    def __init__(self) -> None:
        self.items: list = []

    def add(self, value: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.items.append((now, float(value)))
        if len(self.items) > MAX_SAMPLES:
            del self.items[: len(self.items) - MAX_SAMPLES]

    def window(self, now: float | None = None) -> list:
        now = time.time() if now is None else now
        cutoff = now - WINDOW_S
        while self.items and self.items[0][0] < cutoff:
            del self.items[0]
        return [value for _, value in self.items]

    def summary(self, now: float | None = None) -> dict:
        vals = sorted(self.window(now))
        if not vals:
            return {"count": 0, "p50": None, "p95": None, "max": None}
        return {
            "count": len(vals),
            "p50": _pct(vals, 50),
            "p95": _pct(vals, 95),
            "max": vals[-1],
        }


def _cpu_seconds() -> float | None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return float(usage.ru_utime + usage.ru_stime)
    except Exception:
        return None


class PerfMonitor:
    """Collects and summarizes one process's telemetry. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._endpoints: dict[str, _Ring] = {}
        self._lock_wait = _Ring()
        self._lock_hold = _Ring()
        self._polls: dict[str, list] = {}
        self._flight_calls = 0
        self._flight_shared = 0
        self._caches: dict[str, dict[str, int]] = {}
        self._cpu: list = []

    def record_request(self, endpoint: str, elapsed_ms: float, ws=None) -> None:
        """One served request. ``ws`` keeps workstream ids only."""
        try:
            with self._lock:
                ring = self._endpoints.setdefault(str(endpoint), _Ring())
                ring.add(float(elapsed_ms))
        except Exception:
            pass

    def record_lock(self, wait_s: float, hold_s: float) -> None:
        """One outermost store-lock acquisition: wait then hold, seconds."""
        try:
            with self._lock:
                self._lock_wait.add(float(wait_s))
                self._lock_hold.add(float(hold_s))
        except Exception:
            pass

    def record_poll(
        self, source: str, duration_s: float, interval_s: float | None = None
    ) -> None:
        """One finished source poll with its configured interval."""
        try:
            now = time.time()
            with self._lock:
                history = self._polls.setdefault(str(source), [])
                prev = history[-1][0] if history else None
                lag = None if prev is None else max(0.0, now - prev)
                history.append((now, float(duration_s), lag, interval_s))
                if len(history) > MAX_POLL_HISTORY:
                    del history[: len(history) - MAX_POLL_HISTORY]
        except Exception:
            pass

    def record_flight(self, shared: bool) -> None:
        """One single-flight call: shared when it waited on another."""
        try:
            with self._lock:
                self._flight_calls += 1
                if shared:
                    self._flight_shared += 1
        except Exception:
            pass

    def record_cache(self, name: str, outcome: str) -> None:
        """One result-cache lookup: hit, miss, incremental, or full."""
        try:
            with self._lock:
                bucket = self._caches.setdefault(str(name), {})
                bucket[str(outcome)] = bucket.get(str(outcome), 0) + 1
        except Exception:
            pass

    def _poll_summary(self, name: str, history: list, lag_now: float | None) -> dict:
        durations = sorted(entry[1] for entry in history)
        out = {
            "polls": len(history),
            "last_s": history[-1][1] if history else None,
            "p95_s": _pct(durations, 95),
            "max_s": durations[-1] if durations else None,
            "lag_s": lag_now,
            "lag_breaches": 0,
        }
        breaches = 0
        for _, _, lag, interval in reversed(history):
            try:
                bound = 3.0 * float(interval) if interval else None
            except (TypeError, ValueError):
                bound = None
            if bound is None or bound <= 0 or lag is None or lag <= bound:
                break
            breaches += 1
        out["lag_breaches"] = breaches
        return out

    def snapshot(self, live_lag: dict | None = None) -> dict:
        """The ``perf`` block: windowed summaries plus process gauges."""
        now = time.time()
        try:
            with self._lock:
                endpoints = {
                    name: ring.summary(now) for name, ring in self._endpoints.items()
                }
                lock = {
                    "wait_p50_ms": _w(self._lock_wait, now, 50),
                    "wait_p95_ms": _w(self._lock_wait, now, 95),
                    "wait_max_ms": _w(self._lock_wait, now, 100),
                    "hold_p50_ms": _w(self._lock_hold, now, 50),
                    "hold_p95_ms": _w(self._lock_hold, now, 95),
                    "hold_max_ms": _w(self._lock_hold, now, 100),
                    "acquisitions": len(self._lock_wait.window(now)),
                }
                polls = {
                    name: self._poll_summary(name, history, (live_lag or {}).get(name))
                    for name, history in self._polls.items()
                }
                flight = {
                    "calls": self._flight_calls,
                    "shared": self._flight_shared,
                    "hit_ratio": (self._flight_shared / self._flight_calls)
                    if self._flight_calls
                    else 0.0,
                }
                caches = {
                    name: dict(bucket) for name, bucket in self._caches.items()
                }
                cpu_now = _cpu_seconds()
                if cpu_now is not None:
                    self._cpu.append((now, cpu_now))
                    cutoff = now - WINDOW_S
                    while self._cpu and self._cpu[0][0] < cutoff:
                        del self._cpu[0]
                cpu = {
                    "cpu_seconds": cpu_now,
                    "pct_1m": _cpu_pct(self._cpu, now, 60.0),
                    "pct_5m": _cpu_pct(self._cpu, now, 300.0),
                }
        except Exception:
            return {}
        try:
            threads = threading.active_count()
        except Exception:
            threads = None
        return {
            "endpoints": endpoints,
            "lock": lock,
            "sources": polls,
            "singleflight": flight,
            "caches": caches,
            "threads": threads,
            "cpu": cpu,
        }

    def reset(self) -> None:
        """Clear all samples (tests)."""
        with self._lock:
            self._endpoints = {}
            self._lock_wait = _Ring()
            self._lock_hold = _Ring()
            self._polls = {}
            self._flight_calls = 0
            self._flight_shared = 0
            self._caches = {}
            self._cpu = []


def _w(ring: _Ring, now: float, pct: float):
    vals = sorted(v * 1000.0 for v in ring.window(now))
    if not vals:
        return None
    if pct >= 100:
        return vals[-1]
    return _pct(vals, pct)


def _cpu_pct(samples: list, now: float, span: float):
    if len(samples) < 2:
        return None
    floor = now - span
    base = samples[0]
    for stamp, value in samples:
        if stamp >= floor:
            break
        base = (stamp, value)
    head = samples[-1]
    dt = head[0] - base[0]
    if dt <= 0:
        return None
    return (head[1] - base[1]) / dt * 100.0


_MONITOR = PerfMonitor()


def get() -> PerfMonitor:
    """The process-global monitor the server installs by importing."""
    return _MONITOR


def record_request(endpoint: str, elapsed_ms: float, ws=None) -> None:
    """Module shorthand: one served request into the global monitor."""
    _MONITOR.record_request(endpoint, elapsed_ms, ws=ws)


def record_lock(wait_s: float, hold_s: float) -> None:
    """Module shorthand: one store-lock acquisition into the monitor."""
    _MONITOR.record_lock(wait_s, hold_s)


def record_poll(source: str, duration_s: float, interval_s=None) -> None:
    """Module shorthand: one finished source poll into the monitor."""
    _MONITOR.record_poll(source, duration_s, interval_s)


def record_flight(shared: bool) -> None:
    """Module shorthand: one single-flight call into the monitor."""
    _MONITOR.record_flight(shared)


def record_cache(name: str, outcome: str) -> None:
    """Module shorthand: one result-cache lookup into the monitor."""
    _MONITOR.record_cache(name, outcome)


def reset() -> None:
    """Clear the global monitor (tests)."""
    _MONITOR.reset()


class TimedRLock:
    """An RLock proxy recording outermost wait/hold into a monitor.

    Drop-in for the store facade lock: ``with`` blocks, ``acquire``
    and ``release`` behave exactly like :class:`threading.RLock`, and
    only the outermost acquisition per thread records (re-entrant
    takes cost nothing to report). Recording never raises.
    """

    def __init__(self, monitor: PerfMonitor | None = None) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        self._monitor = monitor if monitor is not None else _MONITOR
        self._meta_lock = threading.Lock()
        self._owner_ident = None
        self._owner_name = None
        self._acquired_at = None

    def acquire(self, *args, **kwargs) -> bool:
        depth = getattr(self._local, "depth", 0)
        start = time.monotonic()
        result = self._lock.acquire(*args, **kwargs)
        if not result:
            return result
        wait = time.monotonic() - start
        self._local.depth = depth + 1
        if depth == 0:
            entered = time.monotonic()
            self._local.entered = entered
            self._local.wait = wait
            with self._meta_lock:
                self._owner_ident = threading.get_ident()
                self._owner_name = threading.current_thread().name
                self._acquired_at = entered
        return result

    def release(self, *args, **kwargs) -> None:
        depth = getattr(self._local, "depth", 1)
        if depth <= 1:
            # Keep metadata serialized with the underlying release. A new
            # owner can acquire the RLock immediately, but waits here before
            # publishing its identity, so the old owner cannot clear it.
            with self._meta_lock:
                self._lock.release(*args, **kwargs)
                if self._owner_ident == threading.get_ident():
                    self._owner_ident = None
                    self._owner_name = None
                    self._acquired_at = None
        else:
            self._lock.release(*args, **kwargs)
        depth -= 1
        self._local.depth = max(0, depth)
        if depth <= 0:
            try:
                entered = getattr(self._local, "entered", None)
                wait = getattr(self._local, "wait", 0.0)
                if entered is not None:
                    self._monitor.record_lock(wait, time.monotonic() - entered)
            except Exception:
                pass
            self._local.entered = None

    def diagnostics(self, threshold_s: float = 10.0) -> dict:
        """Describe a long-held lock without attempting to acquire it."""
        try:
            threshold = max(0.0, float(threshold_s))
        except (TypeError, ValueError):
            threshold = 10.0
        with self._meta_lock:
            owner = self._owner_ident
            name = self._owner_name
            acquired = self._acquired_at
        if owner is None or acquired is None:
            return {"held": False, "threshold_s": threshold}
        held_for = max(0.0, time.monotonic() - acquired)
        result = {
            "held": held_for >= threshold,
            "held_for_s": round(held_for, 3),
            "threshold_s": threshold,
            "holder_thread": name,
            "holder_ident": owner,
        }
        if held_for < threshold:
            return result
        try:
            frame = sys._current_frames().get(owner)
            result["holder_stack"] = (
                "".join(traceback.format_stack(frame, limit=40)) if frame is not None else None
            )
        except Exception:
            result["holder_stack"] = None
        return result

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def locked(self) -> bool:
        return self._lock.locked()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TimedRLock({self._lock!r})"
