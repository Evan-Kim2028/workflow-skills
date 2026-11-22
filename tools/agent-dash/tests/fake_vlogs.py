"""An in-memory stand-in for ``dash.vlogs.VLogsClient``, for archive tests.

The log archive talks to VictoriaLogs through four calls: ``healthy()``,
``insert(lines)``, ``flush()`` and ``query(logsql)``. This module answers them
from a dict of rows, so a test can run the real measure/verify/ingest/reclaim
pipeline without a store. The count and token queries are answered for the two
shapes ``dash/logarchive.py`` sends (``path:="<rel>"`` plus ``type:=<token>``).

Also here: the two synthetic log bodies the archive tests share, a pi event
stream with a base64 image and two assistant usage lines, and a pi session
transcript with the same messages.
"""

import json

#: A base64 image payload, long enough that stripping it is worth measuring.
B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODk=" * 40


def usage_line(mid, inp, out, cache, typ="message_end", role="assistant"):
    """One pi event line carrying an assistant usage record."""
    message = {"role": role, "model": "m", "usage": {"input": inp, "output": out, "cacheRead": cache}}
    return json.dumps(
        {"type": typ, "id": mid, "timestamp": 1788220800000, "message": message}, separators=(",", ":")
    ).encode() + b"\n"


def stream_bytes():
    """A pi event stream: a session header, two usage lines, one base64 image."""
    image = {"type": "message_start", "message": {"role": "user", "content": [{"type": "image", "data": B64}]}}
    return b"".join(
        [
            b'{"type":"session","id":"sess-1","version":3}\n',
            usage_line("a1", 10, 5, 100),
            (json.dumps(image, separators=(",", ":")) + "\n").encode(),
            usage_line("a2", 7, 3, 50),
        ]
    )


def session_bytes():
    """A pi session transcript: the same messages, one assistant usage line."""
    return b"".join(
        [
            b'{"type":"session","version":3,"id":"sess-1"}\n',
            usage_line("s1", 4, 2, 9, typ="message"),
            usage_line("s2", 1, 1, 1, typ="message", role="user"),
        ]
    )


def query_paths(logsql):
    """The paths a count or token query names: ``path:in("a","b")`` or ``path:="a"``.

    ``dash/logarchive.py`` asks for a whole batch of files in one grouped query
    and falls back to one query per path, so the fake answers both.
    """
    if "path:in(" in logsql:
        inner = logsql.split("path:in(", 1)[1]
        inner = inner[: inner.index(")")]
        return [part.strip().strip('"') for part in inner.split(",") if part.strip()]
    return [logsql.split('path:="', 1)[1].split('"', 1)[0]]


class FakeClient:
    """The slice of ``dash.vlogs.VLogsClient`` the archive uses, in memory."""

    def __init__(self):
        self.rows = {}
        self.posts = 0
        self.flushes = 0
        self.queries = 0

    def healthy(self):
        return True

    def count(self, rel):
        return len(self.rows.get(rel, []))

    def insert(self, lines):
        sent = 0
        for line in lines:
            row = json.loads(line)
            self.rows.setdefault(row["path"], []).append(row)
            sent += 1
        self.posts += 1
        return sent

    def flush(self):
        self.flushes += 1

    def query(self, logsql):
        self.queries += 1
        grouped = "path:in(" in logsql
        # A count query names no type and counts every row; a token query names one.
        tok = None
        if "type:=message_end" in logsql:
            tok = "message_end"
        elif "type:=message" in logsql:
            tok = "message"
        out = []
        for rel in query_paths(logsql):
            rows = self.rows.get(rel, [])
            if tok is None:
                row_out = {"n": str(len(rows))}
                if grouped:
                    row_out["path"] = rel
                out.append(row_out)
                continue
            n = i = o = c = 0
            for row in rows:
                if row["type"] != tok:
                    continue
                message = json.loads(row["_msg"]).get("message") or {}
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                n += 1
                i += usage.get("input", 0)
                o += usage.get("output", 0)
                c += usage.get("cacheRead", 0)
            if not n:
                # The store leaves a path with no counted row out of the answer.
                continue
            row_out = {"n": str(n), "i": str(i), "o": str(o), "c": str(c)}
            if grouped:
                row_out["path"] = rel
            out.append(row_out)
        return out
