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
- Expired, previous result exists: every request gets that result at
  once (stale-while-revalidate, no wait), and one background refresh
  per key runs (single-flight per key; at most ``BG_MAX`` refreshes
  run at once, extra expiries are served stale and retried on the next
  read). A page that polls is always the reader that starts the refresh,
  so it never pays a recompute inline.
- Idle (no reader for ``IDLE_S``) with an expired generation: the next
  reader computes inline, so a tab opened after a pause never sees data
  older than its own recompute. Idle entries never refresh in the
  background.
- First-ever request (no previous result): single-flight, waiters
  block like ``dash.singleflight`` (there is nothing to serve).
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

#: Freshness contract bound (product rule: a change shows within 5 s).
#: Served age is compute time plus one page tick; a serve older than this
#: is counted in the ``max_age_ms``/``over_max`` stats, so the bound is measured.
MAX_STALE_S = 5.0

#: An entry no reader has touched for this long is idle: its next reader
#: computes inline and no background refresh runs for it.
IDLE_S = 30.0

#: At most this many background refreshes run at once across all keys.
BG_MAX = 4

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
    # Heavy reads (rebuilt from bundles and the store): coalesced like the rest.
    "archive": None,
    "risks": None,
    "project/graph": None,
    "project/feed": None,
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


def _sliding_family(key: tuple):
    """``(family, cell)`` for a sliding-window key (``since`` without ``until``).

    The page's ``since`` rolls onto a new 30 s cell every half minute, so
    each roll is a new key. ``family`` is the key with ``since`` removed;
    ``cell`` is the grid cell. None for every other key.
    """
    try:
        endpoint, items, viewer = key
        names = dict(items)
        if "since" not in names or "until" in names:
            return None
        cell = names["since"]
        if not isinstance(cell, int):
            return None
        family = (endpoint, tuple(item for item in items if item[0] != "since"), viewer)
        return family, cell
    except Exception:
        return None


class _Entry:
    __slots__ = ("payload", "etag", "generated_at", "computing", "event", "last_read")

    def __init__(self):
        self.payload = None
        self.etag = None
        self.generated_at = 0.0
        self.computing = False
        self.event = None
        self.last_read = 0.0


class ResponseCache:
    """One TTL table per server. Methods never raise."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict = {}
        #: family -> (cell, key) of the newest sliding-window key (see _sliding_family).
        self._families: dict = {}
        self._stats: dict = {}
        self._bg_slots = threading.BoundedSemaphore(BG_MAX)

    def _record(self, endpoint: str, outcome: str) -> None:
        try:
            with self._lock:
                bucket = self._stats.setdefault(endpoint, {})
                bucket[outcome] = bucket.get(outcome, 0) + 1
        except Exception:
            pass

    def _note_age(self, endpoint: str, age_s: float) -> None:
        """Track the largest served age and how often it passed MAX_STALE_S."""
        try:
            age_ms = max(0.0, age_s) * 1000.0
            with self._lock:
                bucket = self._stats.setdefault(endpoint, {})
                if age_ms > bucket.get("max_age_ms", 0.0):
                    bucket["max_age_ms"] = round(age_ms, 1)
                if age_s > MAX_STALE_S:
                    bucket["over_max"] = bucket.get("over_max", 0) + 1
        except Exception:
            pass

    def stats(self) -> dict:
        """Per-endpoint counts, hit ratio and the served-age bound."""
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
                "background": bucket.get("background", 0),
                "bg_skipped": bucket.get("bg_skipped", 0),
                "waits": bucket.get("waits", 0),
                "hit_ratio": ((hits + stale) / serves) if serves else 0.0,
                "max_age_ms": bucket.get("max_age_ms", 0.0),
                "over_max": bucket.get("over_max", 0),
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

    def _publish(self, entry: _Entry, payload, generated_at: float) -> None:
        """Install a new generation (caller holds the lock). Resets the ETag:
        the old one names the old bytes."""
        entry.payload = payload
        entry.generated_at = generated_at
        entry.etag = None

    def _run_compute(self, endpoint: str, entry: _Entry, compute):
        """Run ``compute`` for ``entry`` and clear its computing flag.

        Returns ``(payload, generated_at)`` on success. On failure returns
        ``(None, None)`` after clearing the flag, so waiters and the next
        reader retry instead of hanging. Runs outside the lock.
        """
        try:
            fresh = compute()
        except Exception:
            with self._lock:
                entry.computing = False
                if entry.event is not None:
                    entry.event.set()
            raise
        generated_at = time.time()
        if isinstance(fresh, dict) and "generated_at" not in fresh:
            try:
                fresh["generated_at"] = generated_at
            except Exception:
                pass
        with self._lock:
            self._publish(entry, fresh, generated_at)
            entry.computing = False
            if entry.event is not None:
                entry.event.set()
        self._record(endpoint, "recomputes")
        return fresh, generated_at

    def _background(self, endpoint: str, entry: _Entry, compute) -> None:
        """Body of one background refresh thread. Never raises."""
        try:
            self._run_compute(endpoint, entry, compute)
        except Exception:
            self._record(endpoint, "bg_failed")
        finally:
            self._bg_slots.release()

    def get_or_compute(self, key: tuple, endpoint: str, ttl: float, compute,
                       refresh=None, *, wait_timeout_s=None):
        """Serve-or-recompute one key under the SWR protocol.

        ``compute`` builds the payload dict (runs outside the lock; on a
        background thread when this call only starts a refresh);
        ``refresh(payload, now)`` optionally refreshes volatile clock
        fields on the served copy (the stored generation keeps its
        ETag). Returns ``(payload, etag, generated_at, kind)`` where
        kind is ``hit`` (fresh), ``stale`` (served while a refresh runs
        or was started), or ``fresh`` (computed or awaited here). Raises
        only when ``compute`` raises AND no servable generation exists.
        """
        now = time.time()
        # Nominal ttl, desynchronised per key so aligned expiries do
        # not convoy on the store lock.
        ttl = effective_ttl(key, ttl)
        start_bg = False
        skipped = False
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) > 512:
                    self._entries.clear()
                    self._families.clear()
                entry = _Entry()
                self._entries[key] = entry
                self._adopt_neighbour(key, entry, now)
            idle = (now - entry.last_read) > IDLE_S if entry.last_read else True
            entry.last_read = now
            if entry.payload is not None:
                age = now - entry.generated_at
                if age <= ttl:
                    kind = "hit"
                    payload, etag, generated = entry.payload, entry.etag, entry.generated_at
                elif idle and not entry.computing:
                    # Nothing recent to trust: this reader computes inline.
                    entry.computing = True
                    entry.event = threading.Event()
                    kind = "compute"
                    payload = None
                else:
                    # Expired: serve the previous generation now. Start
                    # one background refresh unless one is already running.
                    kind = "stale"
                    payload, etag, generated = entry.payload, entry.etag, entry.generated_at
                    if not entry.computing:
                        if self._bg_slots.acquire(blocking=False):
                            entry.computing = True
                            entry.event = threading.Event()
                            start_bg = True
                        else:
                            skipped = True
            elif entry.computing:
                kind = "wait"
                event = entry.event
                payload = None
            else:
                entry.computing = True
                entry.event = threading.Event()
                kind = "compute"
                payload = None
        if kind == "hit":
            self._record(endpoint, "hits")
            return self._serve_copy(payload, etag, generated, refresh, now, "hit")
        if kind == "stale":
            self._record(endpoint, "stale")
            if skipped:
                self._record(endpoint, "bg_skipped")
            self._note_age(endpoint, now - generated)
            if start_bg:
                self._record(endpoint, "background")
                thread = threading.Thread(
                    target=self._background, args=(endpoint, entry, compute),
                    name=f"respcache-{endpoint}", daemon=True,
                )
                try:
                    thread.start()
                except Exception:
                    # Could not start: release the slot and the flag, serve stale.
                    self._bg_slots.release()
                    with self._lock:
                        entry.computing = False
                        if entry.event is not None:
                            entry.event.set()
            return self._serve_copy(payload, etag, generated, refresh, now, "stale")
        if kind == "wait":
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
        # kind == "compute": this request computes (first-ever or idle).
        try:
            fresh_payload, generated_at = self._run_compute(endpoint, entry, compute)
        except Exception:
            with self._lock:
                fallback = entry.payload
                fetag = entry.etag
                fgen = entry.generated_at
            if fallback is not None:
                self._record(endpoint, "stale")
                self._note_age(endpoint, time.time() - fgen)
                return self._serve_copy(fallback, fetag, fgen, refresh, time.time(), "stale")
            raise
        return self._serve_copy(fresh_payload, None, generated_at, refresh, generated_at, "fresh")

    def _adopt_neighbour(self, key: tuple, entry: _Entry, now: float) -> None:
        """Seed a new sliding-window key from the previous cell's generation.

        Caller holds the lock. The adopted generation keeps its own
        (older) ``generated_at``, so it reads as expired: the reader gets
        it at once and one background refresh runs for the new cell. The
        served window's edge may sit up to one more 30 s cell back than an
        exact ``since``; the grid already tolerates that skew within a cell.
        """
        found = _sliding_family(key)
        if found is None:
            return
        family, cell = found
        prev = self._families.get(family)
        self._families[family] = (cell, key)
        if prev is None or cell - prev[0] != 1:
            return
        neighbour = self._entries.get(prev[1])
        if neighbour is None or neighbour.payload is None:
            return
        entry.payload = neighbour.payload
        entry.etag = neighbour.etag
        entry.generated_at = neighbour.generated_at
        entry.last_read = now  # read now, so not idle: it refreshes in background

    def _serve_copy(self, payload, etag, generated_at, refresh, now, kind):
        if refresh is not None and isinstance(payload, dict):
            try:
                payload = refresh(dict(payload), now)
            except Exception:
                payload = dict(payload)
        elif isinstance(payload, dict):
            payload = dict(payload)
        return (payload, etag, generated_at, kind)

    def store_etag(self, key: tuple, etag, generated=None) -> None:
        """Record the served-bytes ETag for a generation (stable 304s).

        ``generated`` names the generation the bytes came from; a newer
        generation published meanwhile keeps its own (unset) ETag.
        """
        try:
            with self._lock:
                entry = self._entries.get(key)
                if entry is None or entry.payload is None:
                    return
                if generated is not None and entry.generated_at != generated:
                    return
                entry.etag = etag
        except Exception:
            pass
