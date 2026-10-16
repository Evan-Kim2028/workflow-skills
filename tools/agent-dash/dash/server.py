"""HTTP API on 127.0.0.1. JSON comes from the store. The page is static/index.html."""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dash.bundles import approvals_all, approvals_for, bundle_plan_view, list_bundles, load_bundle_api, read_page, workstream_plan_ids
from dash.plans import PlanError, PlanNotFound, load as load_plan
from dash.pricing import health_block
from dash.store import DEFAULT_LONG_RUNNING_S

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
PLACEHOLDER = b"agent-dash\n"
_WS = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class DashServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, registry, host="127.0.0.1", port=8765, index_path=None, stream_interval=2.0, cfg=None):
        self.store = store
        self.registry = registry
        self.cfg = cfg or {}
        self.index_path = Path(index_path) if index_path is not None else INDEX_PATH
        self.stream_interval = stream_interval
        self.stop = threading.Event()
        super().__init__((host, port), Handler)


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
                self._json(200, {"workstreams": self.server.store.query_workstreams(workstream_plan_ids())})
            elif path == "/api/plan":
                self._plan(query)
            elif path == "/plan":
                self._plan_page()
            elif path == "/api/bundles":
                self._bundles()
            elif path == "/api/bundle":
                self._bundle(query)
            elif path == "/api/approvals":
                self._approvals(query)
            elif path.startswith("/bundle/"):
                self._bundle_page(path)
            elif path == "/api/health":
                body = self.server.registry.health(time.time())
                body["pricing"] = health_block(self.server.store.usage_model_stats())
                self._json(200, body)
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            else:
                self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self):
        self._json(405, {"error": "method not allowed"})

    def do_PUT(self):
        self._json(405, {"error": "method not allowed"})

    def do_DELETE(self):
        self._json(405, {"error": "method not allowed"})

    def log_message(self, fmt, *args):
        return

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
            self._json(404, {"error": "not found"})
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types[target.suffix])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        self._json(200, self.server.store.snapshot(now, ws, _long(self.server), since, until))

    def _runs(self, query):
        now = time.time()
        since = _float(query, "since")
        until = _float(query, "until")
        limit = _limit(query)
        ws = _ws(query, default=None)
        self._json(200, {"runs": self.server.store.query_runs(now, ws, since, until, limit)})

    def _usage(self, query):
        group = _choice(query, "group", "model", {"model", "workstream", "tool"})
        bucket = _choice(query, "bucket", "hour", {"hour", "day"})
        ws = _ws(query, default=None)
        since = _float(query, "since")
        self._json(200, {"buckets": self.server.store.query_usage(group, bucket, ws, since)})

    def _concurrency(self, query):
        now = time.time()
        since = _float(query, "since")
        if since is None:
            since = now - 6 * 3600
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
            body = load_plan(ws)
        except PlanNotFound:
            body = bundle_plan_view(ws)
            if body is None:
                self._json(404, {"error": "plan not found", "ws": ws})
                return
        except PlanError:
            self._json(500, {"error": "plan unreadable", "ws": ws})
            return
        self._json(200, body)

    def _plan_page(self):
        """Same shell as `/`. The page reads `/plan` and opens the plan view."""
        path = self.server.index_path
        if not path.is_file():
            self._json(404, {"error": "not found"})
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
            self._json(404, {"error": "bundle not found", "ws": ws})
            return
        self._json(200, body)

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
            self._json(404, {"error": "bundle not found", "ws": ws})
            return
        self._json(200, body)

    def _bundle_page(self, path):
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3 or parts[0] != "bundle" or parts[2] != "page.html":
            self._json(404, {"error": "not found"})
            return
        if parts[1] in (".", "..") or not _WS.match(parts[1]):
            self._json(404, {"error": "not found"})
            return
        body = read_page(parts[1])
        if body is None:
            self._json(404, {"error": "not found"})
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
            payload = self.server.store.snapshot(time.time(), "all", _long(self.server))
            data = {"seq": payload["seq"], "running": payload["running"], "alerts_count": len(payload["alerts"])}
            blob = ("event: tick\ndata: " + json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n\n").encode()
            try:
                self.wfile.write(blob)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, TimeoutError):
                return
            if self.server.stop.wait(interval):
                return

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        self._bytes(status, raw, "application/json; charset=utf-8")

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _static_rel(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return False
    if len(parts) == 1:
        return True
    return len(parts) == 2 and parts[0] == "vendor"


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


def _long(server) -> dict:
    table = dict(DEFAULT_LONG_RUNNING_S)
    custom = (server.cfg or {}).get("long_running_s")
    if isinstance(custom, dict):
        table.update(custom)
    return table
