"""TTL response cache: stale-while-revalidate for heavy read endpoints.
Live dashboards burn a core recomputing the same heavy endpoints on
every poll: with 18 live agents the store changes on every tick, so a
data-version-keyed cache misses on every request and every page
refresh pays a full cold recompute (~400 ms over ~8k runs, 1.4
snapshots per second across tabs). This cache sits in front of the
compute and serves a response computed within the last ``ttl_s``
(default 1.5 s, config ``response_ttl_s``) to every request with the
same canonical key, even when the store moved meanwhile.

Semantics (see CONTRACT.md, docs/perf.md):

- Canonical key: endpoint plus normalised params plus the
  viewer-relevant headers. Window edges quantize to the same 30 s
  grid the store caches use, so the page's sliding ``since`` does not
  miss on the key alone. No current GET varies by viewer headers, so
  the viewer slot is empty by construction (``_VIEWER_HEADERS`` names
  the set; add a header there the day content varies by one).
- Fresh (age <= ttl): serve immediately.
- Expired, previous result exists: the first request recomputes
  (single-flight); concurrent requests during the recompute get the
  previous result instead of waiting (stale-while-revalidate), unless
  it is older than the hard max staleness (5 s), in which case they
  wait for the recompute.
- First-ever request (no previous result): single-flight, waiters
  block like ``dash.singleflight`` (there is nothing stale to serve).
- Window-pinned requests (explicit ``since`` AND ``until``) use a
  longer TTL (10 s): a fixed window's sums move only on writes.
- ``response_ttl_s=0`` disables the cache: every request recomputes
  on the legacy path, byte-identical to before.
- POST handlers for user-visible actions (archive/restore/state/
  bulk) invalidate the whole cache explicitly, so a tap shows
  immediately instead of within the TTL.
- Freshness contract: a change shows within ttl_s plus one page tick,
  well inside the 5 s product rule. Responses carry the true
  ``generated_at`` (a body field, fixed at compute) so the page and
  tests can see the age, plus an ``X-AgentDash-Age-Ms`` header per
  serve for debugging. ETags are content hashes of the cached
  generation, stable until the next recompute, so conditional
  requests still 304 within the window.

Threading: one lock guards the entry table; computation always runs
outside it. Never raises out of ``serve``: a compute failure falls
back to recomputing on the legacy path (``serve`` returns miss).
"""

from __future__ import annotations

import hashlib
import threading
import time

#: Default TTL seconds when the config carries no ``response_ttl_s``.
DEFAULT_TTL_S = 1.5

#: Hard cap: a cached generation older than this is never served stale.
#: Expired readers past this age wait for the recompute instead.
MAX_STALE_S = 5.0

#: TTL for window-pinned requests (explicit since AND until): a fixed
#: window's sums move only on writes, never with the clock.
PINNED_TTL_S = 10.0

#: Desynchronisation spread for effective TTLs. Heavy keys all compute
#: on the same tick (startup, invalidation, aligned tab polls) and
#: would otherwise expire in lockstep, piling every recompute into one
#: store-lock convoy. Each key's effective TTL scales by a deterministic
#: per-key factor in [1-JITTER, 1+JITTER), spreading expiries over
#: ~2*JITTER*ttl while keeping every key's staleness bound at
#: (1+JITTER)*ttl plus one tick, inside the 5 s product rule.
JITTER = 0.15


def effective_ttl(key: tuple, ttl: float) -> float:
    """Per-key TTL with deterministic jitter. Pure function of its inputs."""
    try:
        digest = hashlib.sha256(repr(key).encode()).digest()
        factor = 1.0 - JITTER + 2.0 * JITTER * (int.from_bytes(digest[:4], "big") / 2**32)
        return ttl * factor
    except Exception:
        return ttl

#: Per-endpoint TTL overrides. None means the global ``response_ttl_s``.
#: This table is the one place that tunes an endpoint's freshness; add
#: an entry (not a branch in server.py) to pin an endpoint.
ENDPOINT_TTL: dict = {
    "snapshot": None,
    "runs": None,
    "concurrency": None,
    "workstreams": None,
    "project": None,
    "project/phases": None,
    "state": None,
    "health": None,
    "usage": None,
    "reconcile": None,
}

#: Request headers that change GET content. None today: gzip varies the
#: representation (handled after the cache), If-None-Match is the
#: validator (handled by it). The slot stays so a future
#: viewer-varying endpoint adds its header here, not in server.py.
_VIEWER_HEADERS: tuple = ()


def _ttl_seconds(cfg) -> float:
    """Global TTL from config: ``response_ttl_s``, default 1.5, 0 disables."""
    try:
        raw = (cfg or {}).get("response_ttl_s", DEFAULT_TTL_S)
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_S
    if value != value:  # NaN disables nothing; fall back to default
        return DEFAULT_TTL_S
    return value if value > 0 else 0.0


def ttl_for(cfg, endpoint: str, query) -> float:
    """TTL seconds for one request: 0 means recompute on the legacy path."""
    base = _ttl_seconds(cfg)
    if base <= 0:
        return 0.0
    override = ENDPOINT_TTL.get(endpoint)
    ttl = base if override is None else float(override)
    if ttl <= 0:
        return 0.0
    try:
        params = dict(query) if not hasattr(query, "get") else query
        if isinstance(params, dict):
            pinned = params.get("since") is not None and params.get("until") is not None
        else:
            pinned = False
    except Exception:
        pinned = False
    if pinned:
        ttl = max(ttl, PINNED_TTL_S)
    return ttl


def _quantize(value):
    """30 s grid for window edges: a sliding ``since`` must not miss."""
    try:
        return int(float(value) // 30)
    except (TypeError, ValueError):
        return value


def canonical_key(endpoint: str, params: dict, headers=None) -> tuple:
    """Hashable cache identity: endpoint plus normalised params plus viewers."""
    items = []
    try:
        for name in sorted(params):
            value = params[name]
            if name in ("since", "until"):
                value = _quantize(value)
            elif isinstance(value, (list, tuple)):
                value = tuple(value)
            items.append((name, value))
    except Exception:
        items = sorted(((str(key), str(value)) for key, value in dict(params).items()))
    viewer = ()
    try:
        get = getattr(headers, "get", None)
        if get is not None and _VIEWER_HEADERS:
            viewer = tuple(get(name) for name in _VIEWER_HEADERS)
    except Exception:
        viewer = ()
    return (endpoint, tuple(items), viewer)


class _Entry:
    __slots__ = ("payload", "etag", "generated_at", "computing", "event")

    def __init__(self):
        self.payload = None
        self.etag = None
        self.generated_at = 0.0
        self.computing = False
        self.event = None


class ResponseCache:
    """One TTL table per server. Methods never raise."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._stats: dict = {}

    def _record(self, endpoint: str, outcome: str) -> None:
        try:
            with self._lock:
                bucket = self._stats.setdefault(endpoint, {})
                bucket[outcome] = bucket.get(outcome, 0) + 1
        except Exception:
            pass

    def stats(self) -> dict:
        """Per-endpoint ``{hits, stale, recomputes, waits, hit_ratio}``."""
        try:
            with self._lock:
                table = {name: dict(bucket) for name, bucket in self._stats.items()}
        except Exception:
            return {}
        out = {}
        for name, bucket in table.items():
            hits = bucket.get("hits", 0)
            stale = bucket.get("stale", 0)
            recomputes = bucket.get("recomputes", 0)
            serves = hits + stale + recomputes
            out[name] = {
                "hits": hits,
                "stale": stale,
                "recomputes": recomputes,
                "waits": bucket.get("waits", 0),
                "hit_ratio": ((hits + stale) / serves) if serves else 0.0,
            }
        return out

    def invalidate_all(self) -> None:
        """Drop every cached generation (POST user actions)."""
        try:
            with self._lock:
                self._entries.clear()
        except Exception:
            pass

    def invalidate(self, endpoint: str) -> None:
        """Drop one endpoint's generations."""
        try:
            with self._lock:
                for key in [key for key in self._entries if key and key[0] == endpoint]:
                    self._entries.pop(key, None)
        except Exception:
            pass

    def cached_etag(self, key: tuple):
        """Current generation's ETag, or None (304 pre-check without compute)."""
        try:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.payload is not None:
                    return entry.etag
        except Exception:
            pass
        return None

    def get_or_compute(self, key: tuple, endpoint: str, ttl: float, compute,
                       refresh=None, *, wait_timeout_s=None):
        """Serve-or-recompute one key under the SWR protocol.

        ``compute`` builds the payload dict (runs outside the lock);
        ``refresh(payload, now)`` optionally refreshes volatile clock
        fields on the served copy (the stored generation keeps its
        ETag). Returns ``(payload, etag, generated_at, kind)`` where
        kind is ``hit``, ``stale``, or ``fresh``. Raises only when
        ``compute`` raises AND no servable generation exists; when a
        servable generation exists a compute failure serves it stale.
        """
        now = time.time()
        # Nominal ttl, desynchronised per key so aligned expiries do
        # not convoy on the store lock.
        ttl = effective_ttl(key, ttl)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) > 512:
                    self._entries.clear()
                entry = _Entry()
                self._entries[key] = entry
            if entry.payload is not None and not entry.computing:
                age = now - entry.generated_at
                if age <= ttl:
                    payload, etag, generated = entry.payload, entry.etag, entry.generated_at
                    fresh = True
                elif age <= MAX_STALE_S:
                    payload, etag, generated = entry.payload, entry.etag, entry.generated_at
                    fresh = False
                else:
                    payload = None
                    fresh = False
                if payload is None:
                    fresh = False
            else:
                payload = None
                fresh = False
            if payload is not None and fresh:
                kind = "hit"
            elif payload is not None:
                # Expired but servable: this request recomputes while
                # serving the previous generation (SWR). Only one
                # request becomes the computer; concurrent arrivals see
                # ``computing`` below and serve stale without waiting.
                entry.computing = True
                entry.event = threading.Event()
                kind = "stale-then-recompute"
            elif entry.computing:
                event = entry.event
                event_stale = entry
                kind = "wait-or-stale"
            else:
                entry.computing = True
                entry.event = threading.Event()
                kind = "compute"
        if kind == "hit":
            self._record(endpoint, "hits")
            return self._serve_copy(payload, etag, generated, refresh, now, "hit")
        if kind == "wait-or-stale":
            age = now - event_stale.generated_at
            if event_stale.payload is not None and age <= MAX_STALE_S:
                self._record(endpoint, "stale")
                return self._serve_copy(
                    event_stale.payload, event_stale.etag,
                    event_stale.generated_at, refresh, now, "stale",
                )
            self._record(endpoint, "waits")
            wait_s = 30.0 if wait_timeout_s is None else max(0.0, float(wait_timeout_s))
            completed = event.wait(wait_s)
            with self._lock:
                done = self._entries.get(key)
                if done is not None and done.payload is not None and not done.computing:
                    payload, etag, generated = done.payload, done.etag, done.generated_at
                else:
                    payload = None
            if payload is not None:
                return self._serve_copy(payload, etag, generated, refresh, now, "fresh")
            if not completed and wait_timeout_s is not None:
                raise TimeoutError("response cache computation exceeded its wait budget")
            return self.get_or_compute(
                key, endpoint, ttl, compute, refresh,
                wait_timeout_s=wait_timeout_s,
            )
        # This request computes (fresh, or expired past serving).
        try:
            fresh_payload = compute()
        except Exception:
            with self._lock:
                entry.computing = False
                if entry.event is not None:
                    entry.event.set()
                fallback = entry.payload
                fetag = entry.etag
                fgen = entry.generated_at
            if fallback is not None:
                self._record(endpoint, "stale")
                return self._serve_copy(fallback, fetag, fgen, refresh, time.time(), "stale")
            raise
        generated_at = time.time()
        if isinstance(fresh_payload, dict) and "generated_at" not in fresh_payload:
            try:
                fresh_payload["generated_at"] = generated_at
            except Exception:
                pass
        self._record(endpoint, "recomputes")
        with self._lock:
            entry.payload = fresh_payload
            entry.generated_at = generated_at
            # ETag is set by the caller via ``store`` (it hashes the
            # served bytes); when absent, ``serve`` hashes here.
            entry.computing = False
            if entry.event is not None:
                entry.event.set()
        return self._serve_copy(fresh_payload, None, generated_at, refresh, generated_at, "fresh")

    def _serve_copy(self, payload, etag, generated_at, refresh, now, kind):
        if refresh is not None and isinstance(payload, dict):
            try:
                payload = refresh(dict(payload), now)
            except Exception:
                payload = dict(payload)
        elif isinstance(payload, dict):
            payload = dict(payload)
        return (payload, etag, generated_at, kind)

    def store_etag(self, key: tuple, etag) -> None:
        """Record the served-bytes ETag for a generation (stable 304s)."""
        try:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.payload is not None:
                    entry.etag = etag
        except Exception:
            pass


