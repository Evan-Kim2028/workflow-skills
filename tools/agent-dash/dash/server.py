"""HTTP API on 127.0.0.1. JSON comes from the store. The page is static/index.html."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dash.buildinfo import build_info
from dash.bundles import approvals_all, approvals_for, list_bundles, load_bundle_api, plans_stamp, read_page, resolve_plan, start_prompt_text, workstream_plan_ids
from dash.errors import api_error
from dash.plans import PlanError
from dash.pricing import health_block

ROOT = Path(__file__).resolve().parents[1]


def state_dir_hash(state_dir) -> str:
    """Short opaque id for a state dir: sha256 of the resolved path.

    The hex digest names the state dir without ever exposing the path,
    so ``stop``/``restart``/``deploy`` can refuse a foreign state dir
    over ``/api/health``. Empty or unreadable input hashes as itself.
    """
    import hashlib
    import os

    try:
        text = os.path.realpath(str(state_dir or ""))
    except (OSError, ValueError):
        text = str(state_dir or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


_MAP_LOG = logging.getLogger("agent_dash.maplayout")
_MAP_WARNED: set = set()
INDEX_PATH = ROOT / "static" / "index.html"
PLACEHOLDER = b"agent-dash\n"
_WS = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PROBLEM_DOC = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")


#: /api/plan orchestration fields keyed by workstream: (plan mtime, payload).
#: Map-backed plans have no single file, so their mtime is None and they
#: recompute every request (still cheap: one DAG pass, no store reads).
_PLAN_EXTRA_CACHE: dict = {}

#: /api/plan map-layout fields keyed by workstream: (plan mtime, payload).
#: Same mtime rule as the orchestration fields above.
_PLAN_MAP_CACHE: dict = {}


def _plan_extra_cached(ws: str, body: dict) -> dict:
    """``{"ready", "critical_path", "max_parallel"}`` cached by plan mtime."""
    from dash import orchestrate
    from dash.bundles import bundle_dir

    mtime = None
    try:
        path = bundle_dir(ws)
        if path is not None:
            mtime = (path / "plan.json").stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime is not None:
        hit = _PLAN_EXTRA_CACHE.get(ws)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    payload = orchestrate.plan_extra(body)
    if mtime is not None:
        _PLAN_EXTRA_CACHE[ws] = (mtime, payload)
    return payload


def _plan_map_cached(ws: str, body: dict) -> dict:
    """``{"layout", "map_warnings"}`` cached by plan mtime."""
    from dash import maplayout
    from dash.bundles import bundle_dir

    mtime = None
    try:
        path = bundle_dir(ws)
        if path is not None:
            mtime = (path / "plan.json").stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime is not None:
        hit = _PLAN_MAP_CACHE.get(ws)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        payload = {"layout": maplayout.layout(body), "map_warnings": maplayout.map_warnings(body)}
    except Exception as exc:
        key = f"{type(exc).__name__}: {exc}"
        if key not in _MAP_WARNED:
            _MAP_WARNED.add(key)
            _MAP_LOG.warning("map layout unavailable for %s (%s)", ws, key, exc_info=True)
        payload = {"layout": None, "map_warnings": []}
    if mtime is not None:
        _PLAN_MAP_CACHE[ws] = (mtime, payload)
    return payload


def _with_run_sections(body: dict) -> dict:
    """Snapshot body with plan sections on its running/recent rows.

    Copies, never mutates: the snapshot cache owns the stored payload, so
    the annotated lists replace (never mutate) its entries. Rows that map
    to no plan task read ``section: null`` ("Unsectioned" on the page).
    Never raises: an annotation failure serves the rows as they were.
    """
    if not isinstance(body, dict):
        return body
    try:
        from dash import sections as _sections

        body = dict(body)
        if isinstance(body.get("running"), list):
            body["running"] = _sections.annotate_runs(body["running"])
        if isinstance(body.get("recent"), list):
            body["recent"] = _sections.annotate_runs(body["recent"])
    except Exception:
        pass
    return body


class DashServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, registry, host="127.0.0.1", port=8765, index_path=None, stream_interval=2.0, cfg=None, build=None):
        self.store = store
        self.registry = registry
        self.cfg = cfg or {}
        self.doctor = None
        # Captured once at start: the safe deploy flow compares this
        # against the port instead of re-reading git per request.
        self.build = dict(build) if isinstance(build, dict) else build_info()
        self.index_path = Path(index_path) if index_path is not None else INDEX_PATH
        self.stream_interval = stream_interval
        self.stop = threading.Event()
        self._disconnects: deque = deque()
        self._disconnects_lock = threading.Lock()
        super().__init__((host, port), Handler)

    def warm(self) -> None:
        """Fill the hot caches off the request path, in a daemon thread.

        The first ``/api/state`` otherwise pays the full usage fold, a
        dozen plan reads plus validations, and a ``ps`` storm (~0.8 s on a
        large database). Warming at startup moves that work to process
        start so the first request serves warm (~ms). Opt-in: tests and
        CLIs construct ``DashServer`` without it; the serve path calls it.
        Never raises.
        """
        def _fill():
            try:
                from dash.state import build_state

                now = time.time()
                processes = _live_workers()
                build_state(
                    self.store, self.registry, self.cfg,
                    now=now, processes=processes, build=self.build,
                )
                store = self.store
                store.snapshot_cached(now, "all")
                # Page-shaped entries: the first load fetches the all-scope
                # snapshot with usage buckets and both workstream scopes, so
                # precompute those keys too (shared caches fill as a side
                # effect even when the seq already moved on).
                since = now - 24 * 3600
                store.snapshot_cached(now, "all", since, None, usage_group="model")
                try:
                    from dash.bundles import workstream_plan_ids
                    from dash.workstates import STATES, all_bundle_meta

                    meta = all_bundle_meta()
                    plan_ids = workstream_plan_ids()
                    store.query_workstreams(plan_ids, since, None, now=now, bundle_meta=meta, allowed_states=None)
                    store.query_workstreams(plan_ids, since, None, now=now, bundle_meta=meta, allowed_states=set(STATES))
                except Exception:
                    pass
            except Exception:
                pass

        try:
            thread = threading.Thread(target=_fill, name="dash-warm", daemon=True)
            thread.start()
        except Exception:
            pass

    def note_disconnect(self) -> None:
        """Record a client that went away mid-response. Never raises."""
        now = time.time()
        with self._disconnects_lock:
            self._disconnects.append(now)
            while self._disconnects and now - self._disconnects[0] > 3600:
                self._disconnects.popleft()

    def disconnects_1h(self) -> int:
        """Client disconnects in the last hour (no traceback was logged)."""
        now = time.time()
        with self._disconnects_lock:
            while self._disconnects and now - self._disconnects[0] > 3600:
                self._disconnects.popleft()
            return len(self._disconnects)

    def handle_error(self, request, client_address):
        """Client disconnects are counted, not printed. Real errors still log."""
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (BrokenPipeError, ConnectionResetError)):
            self.note_disconnect()
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                self._page()
            elif path == "/api/snapshot":
                self._snapshot(query)
            elif path == "/api/stream":
                self._stream()
            elif path == "/api/runs":
                self._runs(query)
            elif path == "/api/usage":
                self._usage(query)
            elif path == "/api/concurrency":
                self._concurrency(query)
            elif path == "/api/sessions":
                self._json(200, {"sessions": self.server.store.query_sessions()})
            elif path == "/api/workstreams":
                self._workstreams(query)
            elif path == "/api/archive":
                self._archive(query)
            elif path == "/api/project/graph":
                self._project_graph(query)
            elif path == "/api/project/timeline":
                self._project_timeline()
            elif path == "/api/project/burnup":
                self._burnup(query)
            elif path == "/api/project/phases":
                self._project_phases(query)
            elif path == "/api/reconcile":
                self._reconcile(query)
            elif path == "/api/plan":
                self._plan(query)
            elif path == "/api/replay":
                self._replay(query)
            elif path == "/plan":
                self._plan_page()
            elif path == "/api/bundles":
                self._bundles()
            elif path == "/api/bundle":
                self._bundle(query)
            elif path == "/api/approvals":
                self._approvals(query)
            elif path == "/api/decisions":
                self._decisions()
            elif path == "/api/risks":
                self._risks()
            elif path == "/api/project/feed":
                self._feed(query)
            elif path.startswith("/bundle/"):
                self._bundle_page(path)
            elif path == "/api/health":
                now = time.time()
                body = _health_with_live(self.server.registry, now, self.server.cfg)
                body["pricing"] = health_block(self.server.store.usage_model_stats())
                body["disconnects_1h"] = self.server.disconnects_1h()
                unpriced = self.server.store.unpriced_models(now, now - 86400, now)
                if unpriced:
                    body["ok"] = False
                    body["unpriced_models"] = unpriced
                body["doctor"] = self._doctor_block(now)
                if int((body["doctor"].get("summary") or {}).get("critical", 0)) > 0:
                    body["ok"] = False
                build = getattr(self.server, "build", None) or {}
                body["build_id"] = build.get("build_id", "unknown")
                body["started_at"] = build.get("started_at")
                body["state_hash"] = state_dir_hash((self.server.cfg or {}).get("state_dir"))
                import os as _os

                body["project_id"] = (_os.environ.get("AGENT_DASH_PROJECT_ID") or "").strip() or None
                self._json(200, body)
            elif path == "/api/doctor":
                self._doctor(query)
            elif path == "/api/state":
                self._state()
            elif path == "/api/project":
                self._project()
            elif path == "/api/projects":
                self._projects()
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path.startswith("/docs/problems/"):
                self._problem_doc(path[len("/docs/problems/"):])
            else:
                self._json(404, api_error("not found", "not_found", "Try GET /api/state for the whole dashboard state."))
        except ValueError as exc:
            self._json(400, api_error(str(exc) or "bad request", "bad_request", _hint_for(str(exc))))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/workstreams/archive":
                self._workstream_action("archive")
            elif path == "/api/workstreams/restore":
                self._workstream_action("restore")
            elif path == "/api/workstreams/state":
                self._workstream_state()
            elif path == "/api/workstreams/bulk":
                self._workstream_bulk()
            else:
                self._json(405, api_error("method not allowed", "method_not_allowed", "The API is read-only except POST /api/workstreams/archive, /api/workstreams/restore, /api/workstreams/state and /api/workstreams/bulk; change other state with the CLI, e.g. python3 agent_dash.py ws task <ID> <task> <status>"))
        except ValueError as exc:
            self._json(400, api_error(str(exc) or "bad request", "bad_request", _hint_for(str(exc))))

    def do_PUT(self):
        self._json(405, api_error("method not allowed", "method_not_allowed", "The API is GET only; change state with the CLI, e.g. python3 agent_dash.py ws task <ID> <task> <status>"))

    def do_DELETE(self):
        self._json(405, api_error("method not allowed", "method_not_allowed", "The API is GET only; change state with the CLI, e.g. python3 agent_dash.py ws task <ID> <task> <status>"))

    def log_message(self, fmt, *args):
        return

    def _state(self):
        from dash.state import build_state, state_identity

        now = time.time()
        processes = _live_workers()
        etag = state_identity(self.server.store, self.server.cfg, processes, now)
        if self._not_modified(etag):
            return
        self._json(
            200,
            build_state(
                self.server.store,
                self.server.registry,
                self.server.cfg,
                now=now,
                processes=processes,
                build=getattr(self.server, "build", None),
            ),
            etag=etag,
        )

    def _project(self):
        from dash import project as project_mod

        self._json(200, project_mod.build_payload(self.server.store, self.server.cfg))

    def _projects(self):
        from dash import projects as projects_mod

        self._json(200, projects_mod.build_projects_payload(self.server.cfg))

    def _doctor_block(self, now: float) -> dict:
        """The ``doctor`` block for ``/api/health``: summary plus generation time."""
        try:
            payload = self.server.store.doctor_latest(now)
        except Exception:
            return {"summary": {"critical": 0, "warn": 0, "info": 0, "regressed": 0}, "generated_at": None}
        return {
            "summary": payload.get("summary") or {"critical": 0, "warn": 0, "info": 0, "regressed": 0},
            "generated_at": payload.get("generated_at"),
        }

    def _doctor(self, query):
        """``GET /api/doctor``: the latest persisted run. Never runs checks inline."""
        ws = _one(query, "ws")
        if ws == "all":
            ws = None
        elif ws is not None and (ws in (".", "..") or not _WS.match(ws)):
            raise ValueError("bad ws")
        severity = _one(query, "severity")
        if severity is not None and severity not in ("critical", "warn", "info"):
            raise ValueError("bad severity")
        include_all = _one(query, "all") == "1"
        refresh = _one(query, "refresh")
        scheduler = getattr(self.server, "doctor", None)
        if refresh == "1" and scheduler is not None:
            try:
                scheduler.queue_refresh()
            except Exception:
                pass
        now = time.time()
        if scheduler is not None:
            try:
                body = scheduler.latest(now, ws, severity, include_all)
            except Exception:
                body = self.server.store.doctor_latest(now, ws, severity, include_all)
        else:
            body = self.server.store.doctor_latest(now, ws, severity, include_all)
        self._json(200, body)

    def _static(self, name):
        """Serve a file from static/ or one subdirectory (json, js, css, svg, html). No dotfiles, no traversal."""
        if name == "taskmap.json":
            self._taskmap()
            return
        types = {".json": "application/json", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html; charset=utf-8"}
        base = self.server.index_path.parent.resolve()
        target = (base / name).resolve()
        parts = name.split("/")
        if any(p == "" or p.startswith(".") for p in parts) or base not in target.parents or target.suffix not in types or not target.is_file():
            self._json(404, api_error("not found", "not_found", "Try GET /api/state for the whole dashboard state."))
            return
        body = target.read_bytes()
        # Stat tag + revalidate (no-cache): reloads cost a 304, never a
        # stale script, and never a full re-download when unchanged.
        self._send_body(200, body, types[target.suffix], etag=_stat_etag(target))

    def _problem_doc(self, name):
        """Serve one runbook from docs/problems/ as text. Only bare ``<slug>.md`` names; anything else is a 404."""
        target = ROOT / "docs" / "problems" / name
        if not _PROBLEM_DOC.fullmatch(name) or not target.is_file():
            self._json(404, api_error("not found", "not_found", "Try GET /api/state for the whole dashboard state."))
            return
        body = target.read_bytes()
        self._send_body(200, body, "text/plain; charset=utf-8", etag=_stat_etag(target))

    def _taskmap(self):
        from dash.locate import layered_path

        found = layered_path("taskmap", "AGENT_DASH_TASKMAP", ROOT / "static")
        if found is None or not found.is_file():
            body = b"{}\n"
        else:
            body = found.read_bytes()
        self._bytes(200, body, "application/json")

    def _page(self):
        path = self.server.index_path
        if path.is_file():
            body = path.read_bytes()
            content_type = "text/html; charset=utf-8"
            self._send_body(200, body, content_type, etag=_stat_etag(path))
        else:
            self._bytes(200, PLACEHOLDER, "text/html; charset=utf-8")

    def _snapshot(self, query):
        ws = _ws(query, default="all")
        now = time.time()
        since = _float(query, "since")
        until = _float(query, "until")
        usage_group = _one(query, "usage_group")
        recent_limit = _recent_limit(query)
        recent_offset = _offset(query, "recent_offset")
        store = self.server.store
        ignored = _ignored_entries()
        try:
            from dash.project import ignore_cache_key

            ignore_key = ignore_cache_key(ignored)
        except Exception:
            ignore_key = ()
        # Identity names the result-cache entry, not the poll clock:
        # store.seq advances on every poll (process samples, counts) while
        # no served byte changes, so tagging it would 200 every tick and
        # the conditional layer could never engage. data_seq is the true
        # data version: it moves only when a write lands. The recent page
        # rides the identity too, so one page's 304 never serves another's
        # bytes and page fetches stay small. The plan stamp rides along as
        # well: run rows carry their plan section, so a plan edit retags
        # even when no store write landed. All three are stable file stats
        # and counters, never volatile clocks.
        try:
            ident = (
                "snapshot",
                store.snapshot_key(ws, since, until, usage_group, recent_limit, recent_offset),
                store.data_seq,
                ignore_key,
                plans_stamp(),
            )
        except Exception:
            ident = None
        etag = _identity_etag(*ident) if ident is not None else None
        if self._not_modified(etag):
            return
        body = store.snapshot_cached(now, ws, since, until, usage_group=usage_group, recent_limit=recent_limit, recent_offset=recent_offset)
        if ignored and ws == "all" and isinstance(body, dict):
            # Copy-on-write: the store cache owns this dict, so the
            # filtered lists replace (never mutate) its entries. Totals
            # stay all-scope; only the per-workstream breakdown shrinks,
            # and usage buckets fold the ignored share into "Other" so a
            # stacked chart still sums to the legend Total.
            body = dict(body)
            body["by_workstream"] = _drop_ignored_rows(body.get("by_workstream"), entries=ignored)
            if usage_group == "workstream" and isinstance(body.get("usage"), list):
                try:
                    from dash.project import fold_other_keys

                    body["usage"] = fold_other_keys(body["usage"], ignored)
                except Exception:
                    pass
        self._json(200, _with_run_sections(body), etag=etag)

    def _workstreams(self, query):
        from dash.workstates import all_bundle_meta, bundle_fallback_rows, filter_rows, parse_state_param

        since = _float(query, "since")
        until = _float(query, "until")
        now = time.time()
        allowed = parse_state_param(_one(query, "state"))
        meta = all_bundle_meta()
        plan_ids = workstream_plan_ids()
        store = self.server.store
        ignored = _ignored_entries()
        try:
            from dash.project import ignore_cache_key

            ignore_key = ignore_cache_key(ignored)
        except Exception:
            ignore_key = ()
        try:
            wkey = store.workstreams_key(plan_ids, since, until, now, meta, allowed)
        except Exception:
            wkey = None
        etag = _identity_etag("workstreams", wkey, ignore_key) if wkey is not None else None
        if self._not_modified(etag):
            return
        rows = store.query_workstreams(
            plan_ids, since, until, now=now,
            bundle_meta=meta, allowed_states=allowed,
        )
        have = {row.get("ws") for row in rows}
        for extra in bundle_fallback_rows(have, plan_ids=plan_ids):
            extra["progress"] = _row_progress(extra.get("ws"))
            rows.append(extra)
        rows = filter_rows(rows, allowed, meta)
        from dash import actions as actions_mod
        from dash.bundles import list_bundles as _list_bundles

        try:
            kinds = {item.get("id"): item.get("kind") for item in (_list_bundles() or []) if isinstance(item, dict)}
        except Exception:
            kinds = {}
        for row in rows:
            if "progress" not in row:
                row["progress"] = _row_progress(row.get("ws"))
            # The page never offers an invalid button: every row names
            # the actions its state allows (none for program bundles,
            # OVERVIEW, and the unassigned row).
            ws = row.get("ws")
            if ws == "unassigned" or not ws:
                row["allowed_actions"] = []
            else:
                row["allowed_actions"] = actions_mod.allowed_actions_for(
                    ws, row.get("state") or meta.get(ws, {}).get("state", "active"), kinds.get(ws)
                )
        if ignored:
            rows = _drop_ignored_rows(rows, entries=ignored)
        self._json(200, {"workstreams": rows}, etag=etag)

    def _archive(self, _query):
        """``GET /api/archive``: frozen summary cards for archived workstreams.

        Cards are the ``summary.json`` contents plus id, title,
        archived_at, and the closing-report basename — never report
        bodies. ``totals`` is the archived workstreams' lifetime share
        from the store (same summary functions as every other total),
        so project lifetime numbers keep including archived work.
        """
        from dash.workstates import all_bundle_meta, archive_cards, archived_aggregate

        now = time.time()
        meta = all_bundle_meta()
        cards = archive_cards(meta)
        card_ids = {card["id"] for card in cards}
        roll = self.server.store.ws_rollup(now)
        agg = archived_aggregate(roll["rows"], card_ids)
        runs = sum(
            len(roll["scopes"].get(ws, {}).get("runs", []))
            for ws in card_ids if ws in roll["scopes"]
        )
        self._json(
            200,
            {
                "archive": cards,
                "totals": {
                    "workstreams": len(cards),
                    "tokens": agg["tokens_all_time"],
                    "cost_usd": agg["cost_usd_all_time"],
                    "runs": runs,
                },
            },
        )

    def _workstream_action(self, action: str):
        """``POST /api/workstreams/archive|restore``: the page write actions.

        Guarded local-only (Host, Origin, ``X-AgentDash-Action: 1``) and
        refused with 403 ``read_only`` when ``AGENT_DASH_READONLY=1`` or
        the config carries ``readonly: true``. Runs the CLI's lifecycle
        functions (see ``dash/actions.py``), which write only bundle
        lifecycle fields under the per-bundle lock.
        """
        from dash import actions as actions_mod

        refused = actions_mod.guard(self, self.server.cfg or {})
        if refused is not None:
            status, body = refused
            self._json(status, body)
            return
        try:
            ws = actions_mod.read_ws_body(self)
        except actions_mod.BadContentType:
            self._json(
                415,
                api_error(
                    "expected Content-Type: application/json",
                    "unsupported_media_type",
                    "Send JSON {\"ws\": \"<ID>\"} with Content-Type: application/json, e.g. POST /api/workstreams/archive.",
                ),
            )
            return
        except ValueError as exc:
            self._json(400, api_error(str(exc) or "bad request", "bad_request", _hint_for(str(exc))))
            return
        try:
            status, body = actions_mod.perform(self.server, action, ws)
        except Exception as exc:
            # A failed write (read-only bundle dir, full disk, or any
            # other surprise from the lifecycle functions) is a 500 in
            # the house shape, never a dropped connection: the body
            # names no path, and the one log line names only the
            # workstream id and the exception class (no traceback).
            code = "write_failed" if isinstance(exc, OSError) else "internal"
            actions_mod.LOG.warning("%s %s failed (%s)", action, ws, type(exc).__name__)
            self._json(
                500,
                {
                    **api_error(
                        f"{action} failed for {ws}",
                        code,
                        f"Retry, or run: python3 agent_dash.py ws validate {ws}",
                    ),
                    "ws": ws,
                },
            )
            return
        self._json(status, body)

    def _workstream_state(self):
        """``POST /api/workstreams/state``: one lifecycle transition.

        Body ``{"ws", "state"}`` (plus an optional ``note`` for
        reopens). Same guards, transition table, writes, and error
        shape as ``ws state`` (see ``dash/actions.py``): unknown 404,
        invalid transition 409 with a hint listing the allowed next
        states, bad body 400. Answers ``{ok, ws, state, prev_state,
        state_changed_at}`` so the page can Undo via the reverse.
        """
        from dash import actions as actions_mod

        refused = actions_mod.guard(self, self.server.cfg or {})
        if refused is not None:
            status, body = refused
            self._json(status, body)
            return
        try:
            ws, target, note = actions_mod.read_state_body(self)
        except ValueError as exc:
            self._json(400, api_error(str(exc) or "bad request", "bad_request", _hint_for(str(exc))))
            return
        status, body = actions_mod.perform_state(self.server, ws, target, note)
        self._json(status, body)

    def _workstream_bulk(self):
        """``POST /api/workstreams/bulk``: one batch, per-id results.

        Body ``{"action", "ws": [...], "state"?}``. Same guards as the
        single actions. Each id runs alone through its single-operation
        path (atomic per-bundle writes); one failure never aborts the
        rest. Always answers 200 with ``{ok, action, results,
        changed}``; successes carry ``prev_state`` so one page Undo
        restores all that changed.
        """
        from dash import actions as actions_mod

        refused = actions_mod.guard(self, self.server.cfg or {})
        if refused is not None:
            status, body = refused
            self._json(status, body)
            return
        try:
            action, target, ids, note = actions_mod.read_bulk_body(self)
        except ValueError as exc:
            self._json(400, api_error(str(exc) or "bad request", "bad_request", _hint_for(str(exc))))
            return
        self._json(200, actions_mod.perform_bulk(self.server, action, ids, target, note))

    def _project_graph(self, _query):
        """``GET /api/project/graph``: the cross-workstream dependency graph.
        Nodes are workstream bundles (id, title, state, progress, live
        agents, milestone, depth); edges come from ``depends_on``, gate
        tasks naming another workstream, and deferred tasks that moved.
        Cycles are reported in the payload, never an error. Read-only over
        local bundles; only ids, titles, states, counts, and plan
        condition texts cross the boundary (never paths or bodies).
        """
        from dash import projectgraph
        from dash.bundles import list_bundles
        from dash.workstates import STATES, all_bundle_meta
        now = time.time()
        meta = all_bundle_meta()
        rows = self.server.store.query_workstreams(
            workstream_plan_ids(), None, None,
            bundle_meta=meta, allowed_states=set(STATES),
        )
        live = {}
        progress = {}
        for row in rows:
            ws = row.get("ws")
            if not ws or ws == "unassigned":
                continue
            live[ws] = row.get("live_agents", 0) or 0
            prog = _row_progress(ws)
            if isinstance(prog, dict):
                progress[ws] = prog
        bundles = [item for item in list_bundles() if item.get("kind") == "workstream"]
        plans: dict = {}
        for item in bundles:
            ws = item["id"]
            try:
                _source, body = resolve_plan(ws)
            except Exception:
                body = None
            if isinstance(body, dict):
                plans[ws] = body
        self._json(200, projectgraph.build_graph(bundles, plans, live, progress, now))
    def _project_timeline(self):
        """``GET /api/project/timeline``: one lane per workstream over time.
        Dates come only from recorded evidence (mapped runs, history
        moves, state moves); lanes with none read ``before_history`` and
        the card draws them in a labelled band without pretending dates.
        Only names and numbers cross this boundary: ids, titles, states,
        dates, progress, milestones, dependency ids, run/token/cost sums,
        and the project started/target/phases block (nulls when no
        project file exists).
        """
        import time
        from dash import projecttimeline
        now = time.time()
        try:
            body = projecttimeline.collect(self.server.store, now)
        except Exception:
            self._json(500, api_error("timeline unavailable", "timeline_unavailable", "Check the workstreams directory and retry."))
            return
        self._json(200, body)
    def _burnup(self, _query):
        """``GET /api/project/burnup``: recorded task burn-up plus per-day
        cost and token burn by workstream (see ``dash/projectburn.py``).
        Days are UTC calendar days; undated work stays a labelled gap.
        """
        from dash.projectburn import build_burnup
        self._json(200, {"burnup": build_burnup(self.server.store)})

    def _project_phases(self, query):
        """``GET /api/project/phases?scope=24h|7d|all``: usage by phase.

        Per workstream (and an all-scope total) the billed tokens, cost,
        agents, runs and hours split into planning, execution, review,
        coordination and unclassified, plus a cumulative-by-day cost and
        token series per phase (see ``dash/projectphases.py``). A bad
        scope is a 400. Identity tag covers scope, data version, rules
        hash, ignore list, plan and taskmap stamps, so an unchanged
        dashboard 304s without recomputing.
        """
        from dash import projectphases
        scope = _choice(query, "scope", "24h", set(projectphases.SCOPES))
        now = time.time()
        store = self.server.store
        try:
            key = projectphases.cache_key(store, scope)
        except Exception:
            key = None
        etag = _identity_etag("project-phases", scope, key) if key is not None else None
        if self._not_modified(etag):
            return
        self._json(200, projectphases.build_phases(store, scope, now), etag=etag)

    def _reconcile(self, query):
        from dash.workstates import hidden_ids

        now = time.time()
        hours = _float(query, "hours")
        since = _float(query, "since")
        until = _float(query, "until")
        if since is None and hours is not None:
            if hours <= 0:
                raise ValueError("bad hours")
            since = now - hours * 3600
        body = self.server.store.reconcile_numbers(
            now, since, until, processes=_live_workers(), archived_ws=hidden_ids()
        )
        try:
            stalled = self.server.registry.stalled_messages(now)
        except Exception:
            stalled = []
        if stalled:
            body["mismatches"] = list(body.get("mismatches", [])) + stalled
        self._json(200, body)

    def _runs(self, query):
        now = time.time()
        since = _float(query, "since")
        until = _float(query, "until")
        limit = _limit(query)
        ws = _ws(query, default=None)
        include = _one(query, "include")
        if include is not None and include != "jobs":
            raise ValueError("bad include")
        # Identity tag names the entry (data version, quantized window,
        # minute): per-request fields like elapsed_s make content hashes
        # useless, but the rows only move on store writes, so a match
        # skips the whole query and the megabytes of transfer. store.seq
        # stays out: it advances on every poll without moving any row.
        # The plan stamp rides along too: rows carry their plan section,
        # so a plan edit retags even when no store write landed.
        store = self.server.store
        try:
            ident = (
                "runs", ws,
                None if since is None else int(float(since) // 30),
                None if until is None else int(float(until) // 30),
                limit, include, int(now // 60), store.data_seq,
                plans_stamp(),
            )
        except Exception:
            ident = None
        etag = _identity_etag(*ident) if ident is not None else None
        if self._not_modified(etag):
            return
        from dash import sections as _sections

        rows = store.query_runs(now, ws, since, until, limit, include_jobs=include == "jobs")
        self._json(200, {"runs": _sections.annotate_runs(rows)}, etag=etag)

    def _usage(self, query):
        group = _choice(query, "group", "model", {"model", "workstream", "tool"})
        bucket = _choice(query, "bucket", "hour", {"hour", "day"})
        ws = _ws(query, default=None)
        since = _float(query, "since")
        until = _float(query, "until")
        buckets = self.server.store.query_usage(group, bucket, ws, since, until)
        if group == "workstream":
            buckets = _fold_other(buckets)
        self._json(200, {"buckets": buckets})

    def _concurrency(self, query):
        now = time.time()
        since = _float(query, "since")
        if since is None:
            since = now - 24 * 3600
        step = _float(query, "step") or 60.0
        ws = _ws(query, default=None)
        # Same identity-tag rationale as /api/runs: the point grid slides
        # with the request clock, but the lanes only move on store writes
        # (store.seq still stays out: it moves on every poll regardless).
        store = self.server.store
        try:
            ident = ("concurrency", ws, int(float(since) // 30), float(step), int(now // 60), store.data_seq)
        except Exception:
            ident = None
        etag = _identity_etag(*ident) if ident is not None else None
        if self._not_modified(etag):
            return
        self._json(200, {"points": store.query_concurrency(now, ws, since, step)}, etag=etag)

    def _plan(self, query):
        ws = _one(query, "ws")
        if not ws:
            raise ValueError("ws required")
        if not _WS.match(ws):
            raise ValueError("bad ws")
        try:
            _source, body = resolve_plan(ws)
        except PlanError:
            self._json(500, {**api_error("plan unreadable", "invalid_data", f"Run: python3 agent_dash.py ws validate {ws}"), "ws": ws})
            return
        if body is None:
            self._json(404, {**api_error("plan not found", "plan_not_found", f"Run: python3 agent_dash.py ws new {ws} \"Title\", or pick an id from GET /api/bundles"), "ws": ws})
            return
        if isinstance(body, dict) and "start_prompt" not in body:
            body["start_prompt"] = start_prompt_text(ws)
        if isinstance(body, dict) and "issues" not in body:
            body["issues"] = []
        if isinstance(body, dict):
            for key, value in _plan_extra_cached(ws, body).items():
                body.setdefault(key, value)
            for key, value in _plan_map_cached(ws, body).items():
                body.setdefault(key, value)
        self._json(200, body)

    def _replay(self, query):
        from dash.replay import ReplayNotFound, build_replay

        ws = _one(query, "ws")
        if not ws:
            raise ValueError("ws required")
        if ws in (".", "..") or not _WS.match(ws):
            raise ValueError("bad ws")
        try:
            body = build_replay(self.server.store, ws)
        except ReplayNotFound:
            self._json(404, {**api_error("plan not found", "plan_not_found", f"Run: python3 agent_dash.py ws new {ws} \"Title\", or pick an id from GET /api/bundles"), "ws": ws})
            return
        self._json(200, body)

    def _plan_page(self):
        """Same shell as `/`. The page reads `/plan` and opens the plan view."""
        path = self.server.index_path
        if not path.is_file():
            self._json(404, api_error("not found", "not_found", "Try GET /api/state for the whole dashboard state."))
            return
        self._send_body(200, path.read_bytes(), "text/html; charset=utf-8", etag=_stat_etag(path))

    def _approvals(self, query):
        ws = _one(query, "ws")
        if not ws:
            raise ValueError("ws required")
        if ws == "all":
            self._json(200, {"approvals": approvals_all()})
            return
        if ws in (".", "..") or not _WS.match(ws):
            raise ValueError("bad ws")
        body = approvals_for(ws)
        if body is None:
            self._json(404, {**api_error("bundle not found", "bundle_not_found", "Run: python3 agent_dash.py ws list"), "ws": ws})
            return
        self._json(200, body)

    def _decisions(self):
        """``GET /api/decisions``: hub decisions, newest first, read-only."""
        from dash.decisions import decisions_payload, resolve_hub_dir
        self._json(200, decisions_payload(resolve_hub_dir(self.server.cfg)))
    def _risks(self):
        """``GET /api/risks``: computed risks first, then hub file rows."""
        from dash.decisions import risks_payload
        self._json(200, risks_payload(self.server.store, self.server.cfg, time.time()))
    def _feed(self, query):
        """``GET /api/project/feed``: newest-first what-changed events.
        Query ``limit`` caps the returned lines (default 50, max 200); a
        bad value is a 400. The body is names and numbers only (workstream
        names and ids, task ids, counts, stamps) built by
        ``dash.projectfeed`` from bundle history files, approvals records,
        and finished agent runs. Lines without a recorded time are
        omitted, never interpolated; ``earliest_ts`` is the oldest
        consumed stamp (null when nothing was recorded).
        """
        from dash import projectfeed
        raw = _one(query, "limit")
        if raw is None:
            limit = projectfeed.DEFAULT_LIMIT
        else:
            try:
                limit = int(raw)
            except ValueError as exc:
                raise ValueError("bad limit") from exc
            if limit < 1:
                raise ValueError("bad limit")
            limit = min(limit, projectfeed.MAX_LIMIT)
        self._json(
            200,
            projectfeed.collect_feed(None, self.server.store, time.time(), limit),
        )

    def _bundles(self):
        self._json(200, {"bundles": list_bundles()})

    def _bundle(self, query):
        ws = _one(query, "ws")
        if not ws:
            raise ValueError("ws required")
        if ws in (".", "..") or not _WS.match(ws):
            raise ValueError("bad ws")
        body = load_bundle_api(ws)
        if body is None:
            self._json(404, {**api_error("bundle not found", "bundle_not_found", "Run: python3 agent_dash.py ws list"), "ws": ws})
            return
        self._json(200, body)

    def _bundle_page(self, path):
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3 or parts[0] != "bundle" or parts[2] != "page.html":
            self._json(404, api_error("not found", "not_found", "Use /bundle/<ID>/page.html with an id from GET /api/bundles."))
            return
        if parts[1] in (".", "..") or not _WS.match(parts[1]):
            self._json(404, api_error("not found", "not_found", "Use /bundle/<ID>/page.html with an id from GET /api/bundles."))
            return
        body = read_page(parts[1])
        if body is None:
            self._json(404, api_error("not found", "not_found", "Run: python3 agent_dash.py ws list"))
            return
        self._bytes(200, body, "text/html; charset=utf-8")

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        interval = self.server.stream_interval
        while not self.server.stop.is_set():
            now = time.time()
            payload = self.server.store.snapshot_cached(now, "all")
            try:
                sources = self.server.registry.source_states(now)
            except Exception:
                sources = []
            data = {
                "seq": payload["seq"],
                "running": payload["running"],
                "now": now,
                "data_seq": payload.get("data_seq"),
                "last_data_ts": payload.get("last_data_ts"),
                "last_poll": _last_poll(self.server.registry),
                "sources": sources,
            }
            blob = ("event: tick\ndata: " + json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n\n").encode()
            try:
                self.wfile.write(blob)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, TimeoutError):
                self.server.note_disconnect()
                return
            if self.server.stop.wait(interval):
                return

    def _json(self, status: int, body: dict, etag: str | None = None) -> None:
        raw = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        self._send_body(status, raw, "application/json; charset=utf-8", etag=etag)

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self._send_body(status, body, content_type)

    def _request_header(self, name: str):
        """One request header, or None outside a real request (unit stubs)."""
        headers = getattr(self, "headers", None)
        if headers is None:
            return None
        try:
            return headers.get(name)
        except Exception:
            return None

    def _not_modified(self, etag: str | None, cache: str = "no-store") -> bool:
        """Send 304 when ``If-None-Match`` names ``etag``. True when sent."""
        if etag is None or not _etag_matches(self._request_header("If-None-Match"), etag):
            return False
        try:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            self.server.note_disconnect()
        return True

    def _send_body(self, status: int, body: bytes, content_type: str, etag: str | None = None) -> None:
        """Send a body with ETag revalidation and gzip for big payloads.

        Every 200 response carries an ETag (content hash, or the caller's
        stat-based tag for files): a matching ``If-None-Match`` answers
        304 with no body, so idle ticks and reloads cost headers only.
        Bodies over 8 KB go out gzipped when the client accepts it
        (``/api/runs`` shrinks from ~2 MB to ~150 KB). Errors never 304:
        only 200s revalidate.
        """
        use_etag = etag if etag is not None else (_content_etag(body) if status == 200 else None)
        if status == 200 and use_etag is not None:
            cache = "no-store" if content_type.startswith("application/json") else "no-cache"
            if self._not_modified(use_etag, cache):
                return
        encoding = None
        if len(body) > 8192 and _accepts_gzip(self._request_header("Accept-Encoding")):
            body = gzip.compress(body, compresslevel=4)
            encoding = "gzip"
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if encoding is not None:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            if use_etag is not None:
                self.send_header("ETag", use_etag)
            self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
            self.end_headers()
            if status != 304:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.note_disconnect()


#: Live-worker ``ps`` listing cache: ``{"at", "rows"}`` under ``_LIVE_LOCK``.
#: Spawning half a dozen ``ps`` children costs ~200 ms on a loaded host,
#: and ``/api/state``, ``/api/health``, and ``/api/reconcile`` each listed
#: live workers per request. A 15 s TTL keeps liveness seconds-stale (well
#: inside the unseen-worker grace of two poll intervals) while idle ticks
#: skip the spawn storm. Copies on the way out; tests still stub this
#: function outright.
_LIVE_WORKERS_TTL_S = 15.0
_LIVE_WORKERS_CACHE: dict = {"at": 0.0, "rows": []}
_LIVE_WORKERS_LOCK = threading.Lock()


def _live_workers() -> list:
    """Live worker processes for the ``unseen worker`` reconcile check.

    Labels only; argv beyond the label is never stored. A failed listing
    yields no unseen-worker mismatches rather than failing the endpoint.
    Tests stub this function to isolate from machine-global ps state.
    """
    now = time.monotonic()
    with _LIVE_WORKERS_LOCK:
        if now - _LIVE_WORKERS_CACHE["at"] < _LIVE_WORKERS_TTL_S:
            return [dict(row) for row in _LIVE_WORKERS_CACHE["rows"]]
    try:
        from dash.sources.process import live_worker_processes

        rows = live_worker_processes() or []
    except Exception:
        return []
    with _LIVE_WORKERS_LOCK:
        _LIVE_WORKERS_CACHE["at"] = time.monotonic()
        _LIVE_WORKERS_CACHE["rows"] = [dict(row) for row in rows]
    return [dict(row) for row in rows]


def _health_with_live(registry, now: float, cfg, processes=None):
    """``registry.health`` fed by the cached live-worker listing.

    The real registry's ``describe_roots`` used to spawn its own ``ps``
    storm per request (``processes="auto"``); passing the 15 s TTL listing
    keeps the same labels without the spawn. Stub registries (tests, CLI)
    keep their two-arg call and never pay for a listing they ignore.
    """
    live = processes
    real = False
    try:
        from dash.sources import Registry as _Registry

        real = isinstance(registry, _Registry)
    except Exception:
        real = False
    if live is None and real:
        try:
            live = _live_workers()
        except Exception:
            live = None
    if live is not None or real:
        try:
            return registry.health(now, cfg, processes=live)
        except TypeError:
            pass
    try:
        return registry.health(now, cfg)
    except TypeError:
        return registry.health(now)


def _content_etag(body: bytes) -> str:
    """Opaque content tag for a response body (never a path or datum)."""
    return '"%s"' % hashlib.sha256(body).hexdigest()[:32]


def _identity_etag(*parts) -> str:
    """Opaque tag for an identity (seqs, params, file stamps), not content.

    Lets endpoints 304 without recomputing: the tag names the exact cache
    entry the bytes would come from, so a match means the client's copy is
    current (modulo volatile timestamps like ``now``, which revalidate
    does not cover and readers already tolerate).
    """
    digest = hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:32]
    return '"id-%s"' % digest


def _stat_etag(path: Path) -> str | None:
    """Cheap file tag from ``(mtime_ns, size)``: an edit always retags."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)


def _etag_matches(header_value, etag: str) -> bool:
    """True when ``If-None-Match`` names our tag (or ``*``)."""
    if not header_value or not etag:
        return False
    tags = [tag.strip() for tag in str(header_value).split(",")]
    return etag in tags or "*" in tags


def _accepts_gzip(header_value) -> bool:
    """True when the client offers gzip in ``Accept-Encoding``."""
    if not header_value:
        return False
    return any(
        part.strip().split(";")[0].strip().lower() == "gzip"
        for part in str(header_value).split(",")
    )


def _last_poll(registry) -> float | None:
    """Newest successful source poll time, or None before the first tick.

    The tick carries it so the page can tell a stalled collector (old
    poll, open SSE) from a healthy idle one (fresh poll, unchanged seq).
    Never raises: a missing registry yields None rather than breaking SSE.
    """
    try:
        state = getattr(registry, "state", None) or {}
        stamps = [
            info.get("last_poll")
            for info in state.values()
            if isinstance(info, dict) and info.get("last_poll") is not None
        ]
        return max(stamps) if stamps else None
    except Exception:
        return None


def _static_rel(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return False
    if len(parts) == 1:
        return True
    return len(parts) == 2 and parts[0] == "vendor"


def _row_progress(ws):
    """Plan progress for one workstream row, via the one plan resolver.

    Returns the same ``progress`` object ``/api/plan`` carries, or None when
    the workstream has no plan. Never raises: an unreadable plan leaves the
    row without progress instead of breaking the endpoint.
    """
    try:
        _source, body = resolve_plan(ws)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    progress = body.get("progress")
    return dict(progress) if isinstance(progress, dict) else None


def _one(query: dict, name: str):
    values = query.get(name)
    if not values or values[0] == "":
        return None
    return values[0]


def _ws(query: dict, default):
    value = _one(query, "ws")
    if value is None:
        return default
    if not _WS.match(value):
        raise ValueError("bad ws")
    return value


def _float(query: dict, name: str):
    value = _one(query, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"bad {name}") from exc


def _limit(query: dict) -> int:
    value = _one(query, "limit")
    if value is None:
        return 1000
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("bad limit") from exc
    if number < 1:
        raise ValueError("bad limit")
    return min(number, 5000)


def _recent_limit(query: dict) -> int:
    """Finished-page size for ``GET /api/snapshot`` (default 50, the historic window)."""
    value = _one(query, "recent_limit")
    if value is None:
        return 50
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("bad limit") from exc
    if number < 1:
        raise ValueError("bad limit")
    return min(number, 5000)


def _offset(query: dict, name: str) -> int:
    """Zero-based page start for a paged list (default 0)."""
    value = _one(query, name)
    if value is None:
        return 0
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"bad {name}") from exc
    if number < 0:
        raise ValueError(f"bad {name}")
    return number


def _choice(query: dict, name: str, default: str, allowed: set[str]) -> str:
    value = _one(query, name) or default
    if value not in allowed:
        raise ValueError(f"bad {name}")
    return value


def _ignored_entries():
    """The project's ignore list, or [] when unreadable. Never raises."""
    try:
        from dash.project import read_ignore_list

        return read_ignore_list()
    except Exception:
        return []


def _drop_ignored_rows(rows: list, field: str = "ws", entries=None):
    """Workstream rows minus ignored ids. Returns a new list. Never raises."""
    if entries is None:
        entries = _ignored_entries()
    if not entries or not isinstance(rows, list):
        return list(rows) if isinstance(rows, list) else rows
    try:
        from dash.project import is_ignored

        return [
            row for row in rows
            if not (isinstance(row, dict) and isinstance(row.get(field), str) and is_ignored(row[field], entries))
        ]
    except Exception:
        return list(rows)


def _fold_other(cells: list):
    """Usage buckets with ignored workstream keys folded to "Other".

    A new list (the page stacks these to the all-scope total, so the
    fold keeps the legend honest). Returns the input untouched when no
    ignore list is configured. Never raises.
    """
    entries = _ignored_entries()
    if not entries:
        return cells
    try:
        from dash.project import fold_other_keys

        return fold_other_keys(cells, entries)
    except Exception:
        return cells


def _hint_for(error: str) -> str:
    """Next step for a 400: name the valid values or the example call."""
    if error == "ws required":
        return "Add ?ws=<ID>, e.g. /api/plan?ws=WS7; use ws=all where the endpoint allows it."
    if error == "bad ws":
        return "Use a workstream id like WS7 (see GET /api/bundles for the ids)."
    if error == "bad body":
        return "Send JSON {\"ws\": \"<ID>\"} with header X-AgentDash-Action: 1, e.g. POST /api/workstreams/archive."
    if error in ("bad hours", "bad since", "bad until"):
        return "Pass epoch seconds, e.g. /api/reconcile?hours=24."
    if error == "bad limit":
        return "Pass an integer 1-5000, e.g. /api/runs?limit=50."
    if error == "bad recent_offset":
        return "Pass an integer 0 or greater, e.g. /api/snapshot?recent_limit=10&recent_offset=20."
    if error in ("bad usage_group", "bad group", "bad bucket"):
        return "Use group=model|workstream|tool and bucket=hour|day."
    if error == "bad state":
        return "Use ?state=all or a comma list of planned|active|paused|done|archived|dropped."
    return "Check the query parameters against CONTRACT.md, or run the matching CLI command."


