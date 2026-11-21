"""Client for the local VictoriaLogs store that holds archived agent logs.

This store is deliberately separate from ``dash.sqlite``: it keeps the
prompt and tool text of finished agent runs, which the dashboard never
stores (CONTRACT.md, "Log archive exception"). It is loopback only, and
every row passes ``redact`` before it leaves this process.

Pieces:

* ``redact(text)`` replaces secret-shaped strings (API keys, bearer
  tokens, ``user:pass@`` URLs, ``*_KEY=`` and ``*_TOKEN=`` values).
* ``make_row(...)`` builds one JSON-line row. Rows over ``MAX_MSG_BYTES``
  are truncated and counted; the zstd copy keeps them whole.
* ``VLogsClient`` posts batches to ``/insert/jsonline`` with
  ``Content-Type: application/stream+json`` (any other type is accepted
  with 200 and ingests zero rows) and counts rows with LogsQL.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:19428"
INSERT_PATH = "/insert/jsonline"
CONTENT_TYPE = "application/stream+json"
STREAM_FIELDS = "ws,label"
#: Row text cap. Under the service's 8 MiB line limit with room to spare.
MAX_MSG_BYTES = int(1.8 * 2**20)
#: Target size of one POST body.
BATCH_BYTES = 8 * 2**20
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Secret-shaped strings. Order matters: specific token shapes first, the
# generic ``*_KEY=`` / ``*_TOKEN=`` assignment last. Each replacement
# keeps the shape of the surrounding text so the row stays readable.
#
# Each entry carries ``groups``: tuples of alternative literals, all of which
# must be present for the pattern to have any chance of matching. A row is
# mostly prose, so the cheap ``in`` tests skip almost every regex scan.
# Without them, redacting one 2 KB row costs about 6 ms, which is the whole
# cost of a store rebuild.
_REDACTORS = [
    (
        (("sk-",),),
        re.compile(r"\bsk-(?:ant-|proj-|live-|or-)?[A-Za-z0-9_\-]{16,}"),
        "sk-REDACTED",
    ),
    (
        (("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
        "gh_REDACTED",
    ),
    (
        (("github_pat_",),),
        re.compile(r"\bgithub[_]pat_[A-Za-z0-9_]{20,}"),
        "github-pat-REDACTED",
    ),
    (
        (("tk_live_",),),
        re.compile(r"\btk_live_[A-Za-z0-9]{8,}"),
        "tk_live_REDACTED",
    ),
    (
        (("bearer",),),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{12,}"),
        "Bearer REDACTED",
    ),
    (
        (("://",), ("@",)),
        re.compile(r"(?i)\b(?:[a-z][a-z0-9+.\-]*://)[^\s/:@\"'\\]+:[^\s/@\"'\\]+@"),
        None,
    ),
    # value: "quoted", 'quoted', JSON-escaped \"quoted\", or a bare run
    (
        (("_key", "_token"), ("=",)),
        re.compile(r"(?i)\b([A-Za-z0-9_]*_(?:KEY|TOKEN))=(?:\\\"[^\\\"\n]*\\\"|\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'\\&,;]+)"),
        r"\1=REDACTED",
    ),
]


def _url_userinfo(match: re.Match) -> str:
    text = match.group(0)
    scheme_end = text.index("://") + 3
    return text[:scheme_end] + "REDACTED@"


def redact(text: str) -> tuple[str, int]:
    """Return ``(redacted_text, count)``. Pure; never raises on text.

    A pattern runs only when one of its ``needles`` is present, so a row of
    ordinary prose costs one lowercase pass and a few substring scans
    instead of seven regex passes. The needles are lowercase and the test
    runs on the folded text, which is a superset for a case-sensitive
    pattern too: a false positive only costs one regex scan.
    """
    count = 0
    folded = text.lower()
    for groups, pattern, repl in _REDACTORS:
        if not all(any(needle in folded for needle in group) for group in groups):
            continue
        if repl is None:
            text, n = pattern.subn(_url_userinfo, text)
        else:
            text, n = pattern.subn(repl, text)
        count += n
        folded = text.lower()
    return text, count


def make_row(
    *,
    time_iso: str,
    msg: str,
    ws: str,
    label: str,
    type_: str,
    kind: str,
    path: str,
    extra: dict | None = None,
) -> tuple[bytes, bool, int]:
    """One JSON-line row. Returns ``(line, truncated, redactions)``.

    ``msg`` is redacted, then truncated to ``MAX_MSG_BYTES`` (UTF-8). A
    truncated row is still valid JSON because truncation happens on a
    character boundary. ``extra`` adds string fields to the row object;
    empty values are omitted so a row never carries a blank field.
    """
    clean, redactions = redact(msg)
    truncated = False
    raw = clean.encode("utf-8", "replace")
    if len(raw) > MAX_MSG_BYTES:
        raw = raw[:MAX_MSG_BYTES]
        clean = raw.decode("utf-8", "ignore")
        truncated = True
    row = {
        "_time": time_iso,
        "_msg": clean,
        "ws": ws,
        "label": label,
        "type": type_,
        "kind": kind,
        "path": path,
    }
    if truncated:
        row["truncated"] = "1"
    if extra:
        for key, value in extra.items():
            if value is None or value == "":
                continue
            row[key] = value
    return json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n", truncated, redactions


class VLogsError(RuntimeError):
    """The store refused a request or is unreachable."""


class VLogsClient:
    """Loopback-only VictoriaLogs client. Stdlib only."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 120.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or (parsed.hostname or "") not in LOOPBACK_HOSTS:
            raise VLogsError("refusing non-loopback VictoriaLogs url: %s" % url)
        self.base = "%s://%s" % (parsed.scheme, parsed.netloc)
        self.timeout = timeout

    def _request(self, method: str, path: str, data: bytes | None = None, headers=None) -> bytes:
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise VLogsError("%s %s failed: %s" % (method, path, exc)) from exc

    def healthy(self) -> bool:
        try:
            self._request("GET", "/health")
            return True
        except VLogsError:
            return False

    def insert(self, lines) -> int:
        """POST JSON-line rows in batches. Returns the number of rows sent."""
        sent = 0
        batch: list[bytes] = []
        size = 0
        for line in lines:
            batch.append(line)
            size += len(line)
            if size >= BATCH_BYTES:
                sent += self._post(batch)
                batch, size = [], 0
        if batch:
            sent += self._post(batch)
        return sent

    def _post(self, batch) -> int:
        qs = urllib.parse.urlencode(
            {"_stream_fields": STREAM_FIELDS, "_msg_field": "_msg", "_time_field": "_time"}
        )
        self._request(
            "POST",
            "%s?%s" % (INSERT_PATH, qs),
            data=b"".join(batch),
            headers={"Content-Type": CONTENT_TYPE},
        )
        return len(batch)

    def flush(self) -> None:
        """Make recent inserts searchable now (loopback debug endpoint)."""
        self._request("POST", "/internal/force_flush")

    def query(self, logsql: str) -> list[dict]:
        body = self._request(
            "POST",
            "/select/logsql/query",
            data=urllib.parse.urlencode({"query": logsql}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        rows = []
        for line in body.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def count(self, path: str) -> int:
        """Rows stored for one archived file (by its relative path)."""
        if '"' in path or "\\" in path:
            raise VLogsError("path has a character LogsQL cannot quote: %r" % path)
        rows = self.query('path:="%s" | stats count() n' % path)
        return int(rows[0]["n"]) if rows else 0
