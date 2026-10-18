"""Source registry.

Each module in this package that defines NAME, INTERVAL_S, and poll(store, cfg, now)
is polled on its interval. A failing poll is recorded for /api/health and does not
stop the loop.

Every source runs in its own watchdog thread with a join timeout
(``POLL_BUDGET_S``, 25 s by default, ``poll_budget_s`` in cfg): a hung
transcript scan or ssh call can no longer stall the other sources behind
it. When a poll overruns, the tick moves on, the source is flagged
(``timed_out``, ``last_error``), and its ``last_poll`` is left alone so
``lag_s`` keeps growing until it finishes. A source whose previous poll
is still running is skipped, never stacked.

cfg keys other sources can read: read_budget_per_file (4 MiB), read_budget_per_tick
(8 MiB), backfill (bool), backfill_session (str or None). Tail files with
store.get_offset and store.set_offset.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import threading
import time

log = logging.getLogger("agent_dash.sources")

POLL_BUDGET_S = 25.0
STALL_FACTOR = 3.0
# A source that has never completed a poll is "starting", not stalled,
# until this long after process start (per-source grace is the max of
# this floor and 3x its interval). The first scan can take tens of
# seconds while the interval is only a few.
START_GRACE_S = 90.0
# A poll that returned but took longer than this is "slow": surfaced in
# health and doctor, but still ok (unlike an over-budget timeout).
SLOW_AFTER_S = 15.0


def _budget(cfg) -> float:
    try:
        value = float((cfg or {}).get("poll_budget_s", POLL_BUDGET_S))
    except (TypeError, ValueError):
        return POLL_BUDGET_S
    return value if value > 0 else POLL_BUDGET_S


def _new_state() -> dict:
    return {
        "last_poll": None,
        "last_ok": None,
        "last_error": None,
        "last_duration_s": None,
        "timed_out": False,
        "skip": False,
        "thread": None,
        "poll_start": None,
    }


class Registry:
    def __init__(self, sources=None):
        self.state = {}
        self._born = time.time()
        self._lock = threading.Lock()
        if sources is None:
            sources = discover(self.state)
        else:
            for source in sources:
                self.state[source.NAME] = _new_state()
        self.sources = sources

    def tick(self, store, cfg, now: float, force: bool = False, stop=None) -> None:
        budget = _budget(cfg)
        for source in self.sources:
            if stop is not None and stop.is_set():
                return
            with self._lock:
                state = self.state.setdefault(source.NAME, _new_state())
                thread = state.get("thread")
                if thread is not None and thread.is_alive():
                    continue
                state["thread"] = None
                if state.get("skip"):
                    continue
                last = state["last_poll"]
                if not force and last is not None and now - last < source.INTERVAL_S:
                    continue
                worker = threading.Thread(
                    target=_run_poll,
                    args=(self, source, store, cfg, now, budget),
                    name=f"poll-{source.NAME}",
                    daemon=True,
                )
                state["thread"] = worker
                state["poll_start"] = now
            worker.start()
            if force:
                worker.join()
                overran = False
            else:
                deadline = time.monotonic() + budget
                while worker.is_alive() and time.monotonic() < deadline:
                    if stop is not None and stop.is_set():
                        break
                    worker.join(min(0.25, max(0.0, deadline - time.monotonic())))
                overran = worker.is_alive() and not (
                    stop is not None and stop.is_set()
                )
            if not overran:
                continue
            with self._lock:
                state["timed_out"] = True
                state["last_error"] = (
                    f"TimeoutError: poll exceeded {budget:g}s budget"
                )
                log.warning("source %s exceeded %gs budget", source.NAME, budget)

    def source_states(self, now: float) -> list:
        """Per-source freshness for the SSE tick and /api/health."""
        rows = []
        with self._lock:
            items = [(source, dict(self.state.get(source.NAME) or {})) for source in self.sources]
        for source, state in items:
            last = state.get("last_poll")
            rows.append(
                {
                    "name": source.NAME,
                    "last_poll": last,
                    "lag_s": None if last is None else max(0.0, now - last),
                }
            )
        return rows

    def stalled_messages(self, now: float) -> list:
        """One ``source stalled: <name> (last ok poll N s ago)`` per lagging source.

        Uses the same state as /api/health (:func:`source_state` with the
        shared :func:`startup_complete` gate): no line while the source
        reads ``starting``, so neither /api/reconcile nor the doctor sees
        phantom stall lines after a restart.
        """
        found = []
        with self._lock:
            items = [(source, dict(self.state.get(source.NAME) or {})) for source in self.sources]
            born = self._born
        startup = not startup_complete(
            [source for source, _ in items],
            [state for _, state in items],
            born,
            now,
        )
        for source, state in items:
            if source_state(source, state, now, born, startup=startup) != "stalled":
                continue
            ref = state.get("last_ok")
            if ref is None:
                ref = state.get("last_poll")
            if ref is None:
                ref = born
            ago = int(max(0.0, now - ref))
            found.append(f"source stalled: {source.NAME} (last ok poll {ago} s ago)")
        return found

    def health(self, now: float, cfg=None, processes="auto") -> dict:
        sources = []
        extra_body: dict = {}
        ok = True
        slow = 0
        with self._lock:
            items = [(source, dict(self.state.get(source.NAME) or {})) for source in self.sources]
            born = self._born
        startup = not startup_complete(
            [source for source, _ in items],
            [state for _, state in items],
            born,
            now,
        )
        for source, state in items:
            error = state.get("last_error")
            last = state.get("last_poll")
            name = source_state(source, state, now, born, startup=startup)
            stalled = name == "stalled"
            if name in ("error", "stalled"):
                ok = False
            if name == "slow":
                slow += 1
            sources.append(
                {
                    "name": source.NAME,
                    "last_poll": last,
                    "last_error": error,
                    "lag_s": None if last is None else max(0.0, now - last),
                    "last_duration_s": state.get("last_duration_s"),
                    "interval_s": source.INTERVAL_S,
                    "stalled": stalled,
                    "state": name,
                    "started_at": born,
                }
            )
            extra = getattr(source, "health_extra", None)
            if callable(extra):
                try:
                    info = extra()
                except Exception:
                    info = None
                if isinstance(info, dict) and info:
                    extra_body.setdefault(source.NAME, {}).update(info)
        body: dict = {"ok": ok, "sources": sources, "slow_sources": slow}
        body.update(extra_body)
        if cfg is not None:
            try:
                from dash.rundirs import describe_roots

                body.update(describe_roots(cfg, now=now, processes=processes))
            except Exception as exc:
                log.warning("health roots: %s", type(exc).__name__)
        return body


def _run_poll(registry: Registry, source, store, cfg, now: float, budget: float) -> None:
    """Poll one source and record the outcome. Always runs in a worker thread."""
    start = time.monotonic()
    try:
        source.poll(store, cfg, now)
    except Exception as exc:
        duration = time.monotonic() - start
        with registry._lock:
            state = registry.state.setdefault(source.NAME, _new_state())
            state["last_poll"] = now
            state["last_error"] = _short(exc)
            state["last_duration_s"] = duration
            if duration <= budget:
                state["timed_out"] = False
        log.warning("source %s: %s", source.NAME, state["last_error"])
    else:
        duration = time.monotonic() - start
        with registry._lock:
            state = registry.state.setdefault(source.NAME, _new_state())
            state["last_poll"] = now
            state["last_ok"] = now
            state["last_error"] = _surfaced(source)
            state["last_duration_s"] = duration
            state["timed_out"] = duration > budget
            if state["timed_out"]:
                state["last_error"] = (
                    f"TimeoutError: poll took {duration:.1f}s, over {budget:g}s budget"
                )
                log.warning("source %s: %s", source.NAME, state["last_error"])


def has_completed_poll(state: dict) -> bool:
    """One shared definition of "this source finished a poll at least once".

    The registry health path (``starting`` vs ``stalled``) and the
    doctor scheduler (first-run wait) both use this, so they agree.
    """
    return state.get("last_poll") is not None


def start_grace_s(source) -> float:
    """Grace after process start before a never-polled source may stall."""
    return max(STALL_FACTOR * float(getattr(source, "INTERVAL_S", 0) or 0), START_GRACE_S)


def _interval_s(source) -> float:
    """A source's poll interval in seconds; plain numbers pass through so
    health-dict callers (interval_s floats) can share this module's math."""
    if isinstance(source, (int, float)):
        try:
            return float(source or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(getattr(source, "INTERVAL_S", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def startup_threshold_s(sources) -> float:
    """Seconds after process start before the first scan may be judged.

    One shared definition (registry and doctor): ``max(3x the slowest
    source interval, START_GRACE_S)``. Takes source modules or plain
    interval seconds.
    """
    slowest = max(([_interval_s(source) for source in sources] or [0.0]))
    return max(STALL_FACTOR * slowest, START_GRACE_S)


def startup_complete(sources, states, born: float, now: float) -> bool:
    """True once the process is past its first-scan window.

    The one shared startup definition (registry ``health`` /
    ``stalled_messages`` and the doctor's :func:`unseen_startup_ready
    <dash.doctor.unseen_startup_ready>`): complete only when the process
    has been up for :func:`startup_threshold_s` (max(3x the slowest
    source interval, ``START_GRACE_S``)) AND every source completed one
    poll (:func:`has_completed_poll`). Until then a source that finished
    its first scan early reads ``starting``, not ``stalled``, while
    slower sources are still on theirs — the registry polls sequentially
    and lag counts from the first completed poll, so judging it stalled
    would be a restart false positive. A source that never polled keeps
    its own per-source grace (:func:`start_grace_s`) past the window, so
    a hung first scan still ends up ``stalled``, never pinning the
    dashboard in ``starting`` forever.
    """
    try:
        polled = all(
            has_completed_poll(state) if isinstance(state, dict) else False
            for state in states
        )
    except Exception:
        return False
    if not polled:
        return False
    return now - born > startup_threshold_s(sources)


def source_state(source, state: dict, now: float, born: float, *, startup: bool | None = None) -> str:
    """One of ``starting``, ``error``, ``stalled``, ``slow``, ``ok``.

    A source that never completed a poll — and has no failure recorded —
    is ``starting`` (not stalled) inside its own grace window. When the
    registry passes its shared first-scan gate (``startup=True``, i.e.
    :func:`startup_complete` is not yet true), a source that already
    polled reads ``starting`` too instead of ``stalled``-by-lag. Positive
    failure evidence (an error or a budget overrun) still reports
    ``error`` immediately, even on the first poll. A poll that returned
    but took longer than ``SLOW_AFTER_S`` is ``slow``: surfaced, but
    still ok.
    """
    if state.get("last_error") or state.get("timed_out"):
        return "error"
    if not has_completed_poll(state):
        if now - born <= start_grace_s(source):
            return "starting"
    elif startup:
        return "starting"
    if _is_stalled(source, state, now, born):
        return "stalled"
    try:
        duration = float(state.get("last_duration_s") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration > SLOW_AFTER_S:
        return "slow"
    return "ok"


def _is_stalled(source, state: dict, now: float, born: float) -> bool:
    limit = STALL_FACTOR * float(getattr(source, "INTERVAL_S", 0) or 0)
    last = state.get("last_poll")
    if last is None:
        return now - born > max(limit, START_GRACE_S)
    return now - last > limit


def list_dir_cached(path: str, cache: dict, cap=None):
    """Scandir entries cached by (inode, mtime_ns).

    ``cache`` is owned by the caller: ``path -> (ino, mtime_ns, entries)``
    where each entry is ``(name, full_path, is_file, is_dir)`` resolved with
    ``follow_symlinks=False``. An unchanged directory costs one stat and no
    listing, so repeated polls over a quiet tree stay cheap while new files
    (which always bump the parent mtime) are still found.

    ``cap`` is an optional mutable ``[remaining]`` counter bounding fresh
    listings per pass. When it is exhausted and there is no cache hit, this
    returns None: the caller must skip the directory this pass (its mtime
    still differs, so a later pass retries it).

    Returns ``(entries, listed)`` where ``listed`` is True when a fresh
    listing happened. ``entries`` is None only when deferred by ``cap``.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return [], False
    key = (st.st_ino, st.st_mtime_ns)
    hit = cache.get(path)
    if hit is not None and hit[0] == key:
        return hit[1], False
    if cap is not None and cap[0] <= 0:
        return None, False
    try:
        with os.scandir(path) as scanner:
            entries = []
            for ent in scanner:
                try:
                    is_file = ent.is_file(follow_symlinks=False)
                    is_dir = ent.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((ent.name, ent.path, is_file, is_dir))
    except OSError:
        return [], False
    entries.sort(key=lambda item: item[0])
    cache[path] = (key, entries)
    if cap is not None:
        cap[0] -= 1
    return entries, True


def discover(state: dict | None = None) -> list:
    import dash.sources as package

    found = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"dash.sources.{info.name}")
        except Exception as exc:
            found.append(_Broken(info.name, exc))
            if state is not None:
                bad = _new_state()
                bad["last_error"] = _short(exc)
                bad["skip"] = True
                state[info.name] = bad
            continue
        if not all(hasattr(module, attr) for attr in ("NAME", "INTERVAL_S", "poll")):
            continue
        found.append(module)
        if state is not None:
            state[module.NAME] = _new_state()
    found.sort(key=lambda source: source.NAME)
    return found


class _Broken:
    def __init__(self, name: str, exc: Exception):
        self.NAME = name
        self.INTERVAL_S = 10**9
        self._error = _short(exc)

    def poll(self, store, cfg, now: float) -> None:
        raise RuntimeError(self._error)


def _short(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:200]


def _surfaced(source) -> str | None:
    """A source may record a whole-poll failure without raising.

    Partial failures in other sources stay on their own ``last_error`` and
    do not mark the source unhealthy. ``SURFACE_ERROR`` opts in.
    """
    if not getattr(source, "SURFACE_ERROR", False):
        return None
    value = getattr(source, "last_error", None)
    if isinstance(value, str) and value.strip():
        return value.strip()[:200]
    return None
