"""Log search view backend: a read-only proxy to the local VictoriaLogs store.

``GET /api/logs/search`` and ``GET /api/logs/facets`` are served by
``handle`` below. Both forward LogsQL to the loopback store that
``dash.vlogs`` ingests into (``scripts/vlogs_install.sh``). Nothing is
written here, nothing goes to ``dash.sqlite``, and the endpoints are
never read by ``/api/state``, ``/api/health`` or page load: the page
calls them only after the owner opens the card (CONTRACT.md, "Log archive
exception").

Contract, in short:

* ``q`` is literal text. It becomes one quoted, case-insensitive phrase
  filter (``i("...")``) on ``_msg``; ``\\`` and ``"`` are escaped, control
  characters become spaces, and nothing else from the user reaches LogsQL.
  ``ws``, ``label`` and ``type`` are exact matches with the same quoting.
* Every query carries a time filter (default: the last 30 days) and a
  server-side ``timeout``. ``limit`` defaults to 100 and is capped at 500.
* Results are newest first. A row carries ``ts``, ``ws``, ``label``,
  ``type`` and ``path`` (the archived file's relative path), a
  ``snippet`` of at most 500 characters around the first match, and
  ``text``, the event itself capped at 2000 characters for the detail
  pane.
* When the store is down (refused, unreachable, or no answer within the
  300 ms connect budget) or holds nothing, the answer is HTTP 200 with
  ``status`` ``down`` or an empty list and the install command. It is
  never an error page and never a long wait.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
import urllib.parse
from datetime import datetime, timezone

DEFAULT_URL = "http://127.0.0.1:19428"
URL_ENV = "AGENT_DASH_VLOGS_URL"
INSTALL_CMD = "sh scripts/vlogs_install.sh"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

CONNECT_TIMEOUT_S = 0.3
READ_TIMEOUT_S = 3.0
#: Server-side query budget sent to VictoriaLogs (``timeout`` URL arg).
SERVER_TIMEOUT = "2s"

DEFAULT_WINDOW_S = 30 * 86400
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
Q_MAX_CHARS = 200
FIELD_MAX_CHARS = 200
SNIPPET_CHARS = 500
DETAIL_CHARS = 2000
FACET_ROWS = 5000

PARAMS = ("q", "ws", "label", "type", "since", "until", "limit")
ROUTES = ("/api/logs/search", "/api/logs/facets")

_RELATIVE_RE = re.compile(r"^(\d{1,4})([mhd])$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNIT_S = {"m": 60, "h": 3600, "d": 86400}


class Unreachable(RuntimeError):
    """The store is down, refused, or did not answer in time."""


class StoreError(RuntimeError):
    """The store answered with an error for a query we built."""


# ---------------------------------------------------------------- pure helpers


def base_url(env=None) -> str | None:
    """The loopback store URL, or None when the configured one is not loopback."""
    env = os.environ if env is None else env
    raw = (env.get(URL_ENV) or DEFAULT_URL).strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "http" or (parsed.hostname or "") not in LOOPBACK_HOSTS:
        return None
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def quote(text: str) -> str:
    """A LogsQL double-quoted string holding ``text`` as literal text."""
    clean = _CONTROL_RE.sub(" ", text)
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clip(value, name: str, cap: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > cap:
        raise ValueError("%s too long" % name)
    return text


def parse_time(value, now: float, default: float, name: str) -> float:
    """Epoch seconds from a relative ``7d``/``24h``/``30m`` (before now), an
    ISO date or datetime (UTC when no offset), or a plain epoch number."""
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip()
    match = _RELATIVE_RE.match(text)
    if match:
        return now - int(match.group(1)) * _UNIT_S[match.group(2)]
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return float(text)
    if _DATE_RE.match(text) or _DATETIME_RE.match(text):
        iso = text.replace(" ", "T")
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        if _DATE_RE.match(iso):
            iso += "T00:00:00+00:00"
        elif re.search(r"[+-]\d{4}$", iso):
            iso = iso[:-2] + ":" + iso[-2:]
        elif not re.search(r"[+-]\d{2}:\d{2}$", iso):
            iso += "+00:00"
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError as exc:
            raise ValueError("bad %s" % name) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise ValueError("bad %s" % name)


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(params: dict, now: float) -> tuple[float, float]:
    """``(since, until)`` epoch seconds. Defaults: the last 30 days to now."""
    until = parse_time(params.get("until"), now, now, "until")
    since = parse_time(params.get("since"), now, until - DEFAULT_WINDOW_S, "since")
    if since >= until:
        raise ValueError("since must be before until")
    return since, until


def limit_of(raw) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LIMIT
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("bad limit") from exc
    if value < 1:
        raise ValueError("bad limit")
    return min(value, MAX_LIMIT)


def _time_filter(since: float, until: float) -> str:
    return "_time:[%s, %s]" % (_rfc3339(since), _rfc3339(until))


def search_query(params: dict, since: float, until: float, limit: int) -> str:
    """The LogsQL for one search. Pure: a string in, a string out."""
    parts = [_time_filter(since, until)]
    q = _clip(params.get("q"), "q", Q_MAX_CHARS)
    if q:
        parts.append("i(%s)" % quote(q))
    for name in ("ws", "label", "type"):
        value = _clip(params.get(name), name, FIELD_MAX_CHARS)
        if value:
            parts.append("%s:=%s" % (name, quote(value)))
    parts.append("| sort by (_time desc) | limit %d" % limit)
    parts.append("| fields _time, ws, label, type, path, _msg")
    return " ".join(parts)


def facets_query(since: float, until: float) -> str:
    """One bounded stats query: row counts by workstream, label and type."""
    return "%s | stats by (ws, label, type) count() n | limit %d" % (
        _time_filter(since, until),
        FACET_ROWS,
    )


def snippet(msg: str, q: str, cap: int = SNIPPET_CHARS) -> str:
    """At most ``cap`` characters around the first match of ``q`` (case
    insensitive). Ellipses, when added, count inside the cap."""
    if len(msg) <= cap:
        return msg
    low = msg.lower()
    pos = -1
    needles = [q.lower()] if q else []
    needles += [tok.lower() for tok in re.findall(r"\w+", q or "")][:1]
    for needle in needles:
        if needle:
            pos = low.find(needle)
            if pos >= 0:
                break
    if pos < 0:
        pos = 0
    # Ellipses are the two extra characters, so the whole piece fits the cap.
    room = cap - 2
    start = max(0, pos - room // 3)
    end = min(len(msg), start + room)
    start = max(0, end - room)
    lead = "\u2026" if start > 0 else ""
    trail = "\u2026" if end < len(msg) else ""
    return lead + msg[start:end] + trail


def _row(obj: dict, q: str) -> dict:
    msg = str(obj.get("_msg") or "")
    text = msg[:DETAIL_CHARS]
    return {
        "ts": str(obj.get("_time") or ""),
        "ws": str(obj.get("ws") or ""),
        "label": str(obj.get("label") or ""),
        "type": str(obj.get("type") or ""),
        "path": str(obj.get("path") or ""),
        "snippet": snippet(msg, q),
        "text": text,
        "text_truncated": len(msg) > DETAIL_CHARS,
    }


def _parse_lines(body: bytes) -> list[dict]:
    rows = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


# ------------------------------------------------------------------ transport


class HttpStore:
    """Loopback POST to the store. The connect budget is short so a dead
    store answers at once; the read budget bounds a slow query."""

    def __init__(self, base: str) -> None:
        parsed = urllib.parse.urlsplit(base)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80

    def post(self, path: str, form: dict) -> bytes:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=CONNECT_TIMEOUT_S)
        try:
            try:
                conn.connect()
            except (OSError, socket.timeout) as exc:
                raise Unreachable(str(exc)) from exc
            conn.sock.settimeout(READ_TIMEOUT_S)
            body = urllib.parse.urlencode(form).encode("utf-8")
            try:
                conn.request(
                    "POST",
                    "%s?timeout=%s" % (path, SERVER_TIMEOUT),
                    body=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                raise Unreachable(str(exc)) from exc
            if resp.status >= 400:
                raise StoreError("store answered %d" % resp.status)
            return data
        finally:
            conn.close()


def _store(base: str | None, post):
    if post is not None:
        return post
    if base is None:
        raise Unreachable("VictoriaLogs URL is not loopback")
    return HttpStore(base).post


def _down(extra: dict | None = None) -> dict:
    out = {"status": "down", "install": INSTALL_CMD}
    out.update(extra or {})
    return out


# ------------------------------------------------------------------- payloads


def search(params: dict, now: float | None = None, base: str | None = None, post=None) -> dict:
    """The search payload. ``post`` is an injectable ``(path, form) -> bytes``
    for tests; by default it talks to the loopback store at ``base``."""
    now = time.time() if now is None else now
    since, until = window(params, now)
    limit = limit_of(params.get("limit"))
    q = _clip(params.get("q"), "q", Q_MAX_CHARS)
    query = search_query(params, since, until, limit)
    base = base_url() if base is None and post is None else base
    try:
        body = _store(base, post)("/select/logsql/query", {"query": query})
    except Unreachable:
        return _down({"results": [], "count": 0, "limit": limit})
    except StoreError as exc:
        return {"status": "error", "error": str(exc), "results": [], "count": 0, "limit": limit}
    rows = [_row(obj, q) for obj in _parse_lines(body)]
    return {
        "status": "ok",
        "results": rows,
        "count": len(rows),
        "limit": limit,
        "since": _rfc3339(since),
        "until": _rfc3339(until),
        "install": INSTALL_CMD,
    }


def facets(params: dict, now: float | None = None, base: str | None = None, post=None) -> dict:
    """Workstream, label and type counts over the same time window."""
    now = time.time() if now is None else now
    since, until = window(params, now)
    base = base_url() if base is None and post is None else base
    try:
        body = _store(base, post)("/select/logsql/query", {"query": facets_query(since, until)})
    except Unreachable:
        return _down({"workstreams": [], "labels": [], "types": [], "total": 0})
    except StoreError as exc:
        return {"status": "error", "error": str(exc), "workstreams": [], "labels": [], "types": [], "total": 0}
    lines = _parse_lines(body)
    ws: dict[str, int] = {}
    labels: dict[str, dict] = {}
    types: dict[str, int] = {}
    total = 0
    for obj in lines:
        n = int(obj.get("n") or 0)
        total += n
        name_ws = str(obj.get("ws") or "")
        name_label = str(obj.get("label") or "")
        name_type = str(obj.get("type") or "")
        ws[name_ws] = ws.get(name_ws, 0) + n
        types[name_type] = types.get(name_type, 0) + n
        entry = labels.setdefault(name_label, {"label": name_label, "ws": name_ws, "count": 0})
        entry["count"] += n
    def ordered(counts):
        return sorted(counts, key=lambda item: (-item["count"], str(item.get("ws", "")), str(item.get("label", item.get("type", "")))))
    return {
        "status": "ok",
        "total": total,
        "truncated": len(lines) >= FACET_ROWS,
        "workstreams": ordered([{"ws": k, "count": v} for k, v in ws.items()]),
        "labels": ordered(list(labels.values()))[:500],
        "types": ordered([{"type": k, "count": v} for k, v in types.items()]),
        "install": INSTALL_CMD,
    }


def handle(path: str, params: dict, now: float | None = None) -> tuple[int, dict] | None:
    """Route one GET under ``/api/logs/``. None when the path is not ours.
    Bad parameters raise ``ValueError`` (the server answers 400)."""
    if path == "/api/logs/search":
        return 200, search(params, now)
    if path == "/api/logs/facets":
        return 200, facets(params, now)
    return None
