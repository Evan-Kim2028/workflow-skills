"""Single-flight for concurrent identical cold requests.

When N request threads miss the same cache at once (a slow server
stacking one snapshot fetch per SSE tick behind the one already
computing), the first thread computes while the rest wait on its
result instead of each recomputing. Sequential callers never touch
this: the store caches make the second call cheap already.
"""

from __future__ import annotations

import threading


class SingleFlight:
    """Coalesce concurrent ``do(key, fn)`` calls onto one computation.

    The first caller runs ``fn``; concurrent callers with an equal key
    block until it finishes and receive the identical object (no copy:
    callers that mutate must copy first). Exceptions propagate to every
    waiter and are never cached; the next call retries. Only in-flight
    calls share: a finished key is forgotten immediately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict = {}
        self._total = 0
        self._shared = 0

    def do(self, key, fn):
        """Run ``fn`` once per concurrent key, sharing its result."""
        with self._lock:
            call = self._calls.get(key)
            if call is None:
                call = {"event": threading.Event(), "result": None, "error": None}
                self._calls[key] = call
                owner = True
            else:
                owner = False
            self._total += 1
            if not owner:
                self._shared += 1
        if not owner:
            try:
                from dash import perfmon as _perfmon

                _perfmon.get().record_flight(True)
            except Exception:
                pass
            call["event"].wait()
            if call["error"] is not None:
                raise call["error"]
            return call["result"]
        try:
            from dash import perfmon as _perfmon

            _perfmon.get().record_flight(False)
        except Exception:
            pass
        try:
            call["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - shared with waiters
            call["error"] = exc
            raise
        finally:
            with self._lock:
                self._calls.pop(key, None)
            call["event"].set()
        return call["result"]

    def in_flight(self) -> int:
        """Current coalesced computations (tests and diagnostics)."""
        with self._lock:
            return len(self._calls)

    def stats(self) -> dict:
        """Lifetime calls and shares (hit ratio for /api/health perf)."""
        with self._lock:
            total, shared = self._total, self._shared
        return {
            "calls": total,
            "shared": shared,
            "hit_ratio": (shared / total) if total else 0.0,
        }
