"""HTTP API on 127.0.0.1. JSON comes from the store. The page is static/index.html."""

from __future__ import annotations

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
from dash.bundles import approvals_all, approvals_for, list_bundles, load_bundle_api, read_page, resolve_plan, start_prompt_text, workstream_plan_ids
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
                body = self.server.registry.health(now, self.server.cfg)
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
                self._json(200, body)
            elif path == "/api/doctor":
                self._doctor(query)
            elif path == "/api/state":
                self._state()
            elif path == "/api/project":
                self._project()
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path.startswith("/docs/problems/"):
                self._problem_doc(path[len("/docs/problems/"):])
            else:
                self._json(404, api_error("not found", "not_found", "Try GET /api/state for the whole dashboard state."))
        except ValueError as exc:
            self._json(400, api_error(str(exc) or "bad request", "bad_request", _hint_for(str(exc))))

    def do_POST(self):
        self._json(405, api_error("method not allowed", "method_not_allowed", "The API is GET only; change state with the CLI, e.g. python3 agent_dash.py ws task <ID> <task> <status>"))

    def do_PUT(self):
        self._json(405, api_error("method not allowed", "method_not_allowed", "The API is GET only; change state with the CLI, e.g. python3 agent_dash.py ws task <ID> <task> <status>"))

    def do_DELETE(self):
        self._json(405, api_error("method not allowed", "method_not_allowed", "The API is GET only; change state with the CLI, e.g. python3 agent_dash.py ws task <ID> <task> <status>"))

    def log_message(self, fmt, *args):
        return

    def _state(self):
        from dash.state import build_state

        self._json(
            200,
            build_state(
                self.server.store,
                self.server.registry,
                self.server.cfg,
                processes=_live_workers(),
                build=getattr(self.server, "build", None),
            ),
        )

    def _project(self):
        from dash import project as project_mod

        self._json(200, project_mod.build_payload(self.server.store, self.server.cfg))

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
        try:
            self.send_response(200)
            self.send_header("Content-Type", types[target.suffix])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.note_disconnect()

    def _problem_doc(self, name):
        """Serve one runbook from docs/problems/ as text. Only bare ``<slug>.md`` names; anything else is a 404."""
        target = ROOT / "docs" / "problems" / name
        if not _PROBLEM_DOC.fullmatch(name) or not target.is_file():
            self._json(404, api_error("not found", "not_found", "Try GET /api/state for the whole dashboard state."))
            return
        body = target.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.note_disconnect()

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
        else:
            body = PLACEHOLDER
            content_type = "text/html; charset=utf-8"
        self._bytes(200, body, content_type)

    def _snapshot(self, query):
        ws = _ws(query, default="all")
        now = time.time()
        since = _float(query, "since")
        until = _float(query, "until")
        usage_group = _one(query, "usage_group")
        self._json(
            200,
            self.server.store.snapshot_cached(now, ws, since, until, usage_group=usage_group),
        )

    def _workstreams(self, query):
        from dash.workstates import all_bundle_meta, parse_state_param

        since = _float(query, "since")
        until = _float(query, "until")
        allowed = parse_state_param(_one(query, "state"))
        meta = all_bundle_meta()
        rows = self.server.store.query_workstreams(
            workstream_plan_ids(), since, until,
            bundle_meta=meta, allowed_states=allowed,
        )
        for row in rows:
            row["progress"] = _row_progress(row.get("ws"))
        self._json(200, {"workstreams": rows})

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
        self._json(200, {"runs": self.server.store.query_runs(now, ws, since, until, limit, include_jobs=include == "jobs")})

    def _usage(self, query):
        group = _choice(query, "group", "model", {"model", "workstream", "tool"})
        bucket = _choice(query, "bucket", "hour", {"hour", "day"})
        ws = _ws(query, default=None)
        since = _float(query, "since")
        until = _float(query, "until")
        self._json(200, {"buckets": self.server.store.query_usage(group, bucket, ws, since, until)})

    def _concurrency(self, query):
        now = time.time()
        since = _float(query, "since")
        if since is None:
            since = now - 24 * 3600
        step = _float(query, "step") or 60.0
        ws = _ws(query, default=None)
        self._json(200, {"points": self.server.store.query_concurrency(now, ws, since, step)})

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
        self._bytes(200, path.read_bytes(), "text/html; charset=utf-8")

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

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        self._bytes(status, raw, "application/json; charset=utf-8")

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.note_disconnect()


def _live_workers() -> list:
    """Live worker processes for the ``unseen worker`` reconcile check.

    Labels only; argv beyond the label is never stored. A failed listing
    yields no unseen-worker mismatches rather than failing the endpoint.
    Tests stub this function to isolate from machine-global ps state.
    """
    try:
        from dash.sources.process import live_worker_processes

        return live_worker_processes() or []
    except Exception:
        return []


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


def _choice(query: dict, name: str, default: str, allowed: set[str]) -> str:
    value = _one(query, name) or default
    if value not in allowed:
        raise ValueError(f"bad {name}")
    return value


def _hint_for(error: str) -> str:
    """Next step for a 400: name the valid values or the example call."""
    if error == "ws required":
        return "Add ?ws=<ID>, e.g. /api/plan?ws=WS7; use ws=all where the endpoint allows it."
    if error == "bad ws":
        return "Use a workstream id like WS7 (see GET /api/bundles for the ids)."
    if error in ("bad hours", "bad since", "bad until"):
        return "Pass epoch seconds, e.g. /api/reconcile?hours=24."
    if error == "bad limit":
        return "Pass an integer 1-5000, e.g. /api/runs?limit=50."
    if error in ("bad usage_group", "bad group", "bad bucket"):
        return "Use group=model|workstream|tool and bucket=hour|day."
    if error == "bad state":
        return "Use ?state=all or a comma list of planned|active|paused|done|archived|dropped."
    return "Check the query parameters against CONTRACT.md, or run the matching CLI command."


