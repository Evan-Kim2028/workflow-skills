"""Source registry.

Each module in this package that defines NAME, INTERVAL_S, and poll(store, cfg, now)
is polled on its interval. A failing poll is recorded for /api/health and does not
stop the loop.

cfg keys other sources can read: read_budget_per_file (4 MiB), read_budget_per_tick
(8 MiB), backfill (bool), backfill_session (str or None). Tail files with
store.get_offset and store.set_offset.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import time

log = logging.getLogger("agent_dash.sources")


class Registry:
    def __init__(self, sources=None):
        self.state = {}
        if sources is None:
            sources = discover(self.state)
        else:
            for source in sources:
                self.state[source.NAME] = {"last_poll": None, "last_error": None, "skip": False}
        self.sources = sources

    def tick(self, store, cfg, now: float, force: bool = False, stop=None) -> None:
        for source in self.sources:
            if stop is not None and stop.is_set():
                return
            state = self.state.setdefault(source.NAME, {"last_poll": None, "last_error": None, "skip": False})
            if state.get("skip"):
                continue
            if not force and state["last_poll"] is not None and now - state["last_poll"] < source.INTERVAL_S:
                continue
            try:
                source.poll(store, cfg, now)
            except Exception as exc:
                state["last_error"] = _short(exc)
                log.warning("source %s: %s", source.NAME, state["last_error"])
            else:
                state["last_error"] = _surfaced(source)
            state["last_poll"] = now

    def health(self, now: float, cfg=None, processes="auto") -> dict:
        sources = []
        ok = True
        for source in self.sources:
            state = self.state.get(source.NAME) or {}
            error = state.get("last_error")
            if error:
                ok = False
            last = state.get("last_poll")
            sources.append(
                {
                    "name": source.NAME,
                    "last_poll": last,
                    "last_error": error,
                    "lag_s": None if last is None else max(0.0, now - last),
                }
            )
        body: dict = {"ok": ok, "sources": sources}
        if cfg is not None:
            try:
                from dash.rundirs import describe_roots

                body.update(describe_roots(cfg, now=now, processes=processes))
            except Exception as exc:
                log.warning("health roots: %s", type(exc).__name__)
        return body


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
                state[info.name] = {"last_poll": None, "last_error": _short(exc), "skip": True}
            continue
        if not all(hasattr(module, attr) for attr in ("NAME", "INTERVAL_S", "poll")):
            continue
        found.append(module)
        if state is not None:
            state[module.NAME] = {"last_poll": None, "last_error": None, "skip": False}
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
