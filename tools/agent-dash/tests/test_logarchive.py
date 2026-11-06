"""``logs archive``: scan, measure, ingest, verify, reclaim, manifest, exit codes.

Runs the real pipeline on small synthetic streams under a temp hub root and
a temp state dir. The store is a loopback fake (rows kept in memory, LogsQL
count and token queries answered for the shapes the archive sends), so the
tests need no VictoriaLogs. Covered: the token rule per kind (stream
``message_end``, session ``message``), line counting with and without a
final newline, discovery filters (non-stream files, archive stubs, ws),
liveness checks (open handle, live launcher, stale marker, pid file), the
verified pipeline with ``--keep remove|stub|stripped``, idempotent reruns
(no second POST), refusal of a partial store, the dry-run exit code on
real input, the lock, and the unreachable-store exit code.
"""

import fcntl
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import logarchive as la  # noqa: E402
from dash import logfilter  # noqa: E402
from dash import vlogs  # noqa: E402

B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODk=" * 40


def _usage_line(mid, inp, out, cache, typ="message_end", role="assistant", extra=None):
    message = {"role": role, "model": "m", "usage": {"input": inp, "output": out, "cacheRead": cache, "cacheWrite": 0}}
    if extra:
        message.update(extra)
    return json.dumps({"type": typ, "id": mid, "timestamp": 1788220800000, "message": message}, separators=(",", ":")).encode() + b"\n"


def _stream_bytes(with_image=True):
    lines = [
        b'{"type":"session","id":"sess-1","version":3}\n',
        _usage_line("a1", 10, 5, 100),
        b'{"type":"tool_execution_end","result":"ok"}\n',
    ]
    if with_image:
        img = {"type": "message_start", "message": {"role": "user", "content": [{"type": "image", "data": B64}]}}
        lines.append((json.dumps(img, separators=(",", ":")) + "\n").encode())
    lines.append(_usage_line("a2", 7, 3, 50))
    lines.append(_usage_line("u1", 1, 1, 1, role="user"))
    return b"".join(lines)


class _Store(http.server.BaseHTTPRequestHandler):
    server_version = "fake-vl"

    def log_message(self, *args):
        pass

    def _send(self, data=b"", code=200):
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(b"OK")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        srv = self.server
        if self.path.startswith("/insert/jsonline"):
            srv.posts += 1
            if self.headers.get("Content-Type") != "application/stream+json":
                return self._send(b"", 200)
            for line in body.splitlines():
                if line.strip():
                    row = json.loads(line)
                    srv.rows.setdefault(row["path"], []).append(row)
            return self._send(b"")
        if self.path.startswith("/internal/force_flush"):
            return self._send(b"")
        if self.path.startswith("/select/logsql/query"):
            query = parse_qs(body.decode())["query"][0]
            rel = query.split('path:="', 1)[1].split('"', 1)[0]
            rows = srv.rows.get(rel, [])
            if "unpack_json" in query:
                tok = "message_end" if "type:=message_end" in query else "message"
                n = i = o = c = 0
                for row in rows:
                    if row["type"] != tok:
                        continue
                    msg = json.loads(row["_msg"])
                    m = msg.get("message") or {}
                    if m.get("role") != "assistant":
                        continue
                    u = m.get("usage") or {}
                    n += 1
                    i += u.get("input", 0)
                    o += u.get("output", 0)
                    c += u.get("cacheRead", 0)
                out = {"n": str(n), "i": str(i) if n else "NaN", "o": str(o) if n else "NaN", "c": str(c) if n else "NaN"}
                return self._send(json.dumps(out).encode() + b"\n")
            return self._send(json.dumps({"n": str(len(rows))}).encode() + b"\n")
        return self._send(b"", 404)


class _FakeStore:
    def __enter__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Store)
        self.server.rows = {}
        self.server.posts = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class ScanTest(unittest.TestCase):
    def test_stream_counts_message_end_and_skips_user_and_session(self):
        sc = la.Scan("message_end")
        sc.feed(_stream_bytes())
        sc.finish()
        self.assertEqual(sc.lines, 6)
        self.assertEqual((sc.messages, sc.input, sc.output, sc.cache_read), (2, 17, 8, 150))

    def test_session_files_count_type_message(self):
        line = _usage_line("s1", 4, 2, 9, typ="message")
        sc = la.Scan("message")
        sc.feed(line + _usage_line("s2", 4, 2, 9, typ="message_end"))
        sc.finish()
        self.assertEqual((sc.messages, sc.input, sc.output, sc.cache_read), (1, 4, 2, 9))

    def test_final_line_without_newline_is_counted(self):
        sc = la.Scan("message_end")
        sc.feed(b'{"a":1}\n{"b":2}')
        sc.finish()
        self.assertEqual(sc.lines, 2)

    def test_chunk_boundaries_do_not_change_counts(self):
        data = _stream_bytes()
        ref = la.Scan("message_end")
        ref.feed(data)
        ref.finish()
        for size in (1, 5, 13, 64, 4096):
            sc = la.Scan("message_end")
            for i in range(0, len(data), size):
                sc.feed(data[i : i + size])
            sc.finish()
            self.assertEqual((sc.lines, sc.tokens()), (ref.lines, ref.tokens()), size)

    def test_oversize_message_line_is_flagged_not_silently_zero(self):
        with mock.patch.object(la, "PARSE_CAP", 64):
            sc = la.Scan("message_end")
            sc.feed(_usage_line("big", 1, 1, 1) + b"x" * 10)
            sc.finish()
        self.assertEqual(sc.unparsed, 1)


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_measure_strips_verifies_and_round_trips(self):
        src = self.dir / "stream-x.ndjson"
        src.write_bytes(_stream_bytes())
        zst = self.dir / "x.zst"
        m = la.measure(str(src), str(zst), "message_end")
        self.assertEqual(m.replaced, 1)
        self.assertLess(m.stripped_bytes, m.raw_bytes)
        self.assertEqual(m.raw_lines, m.stripped_lines)
        self.assertEqual(m.raw_tokens, m.stripped_tokens)
        self.assertEqual(la.decompressed_sha256(str(zst)), m.stripped_sha256)
        la._verify_measure(m)

    def test_measure_counts_without_writing_when_no_output(self):
        src = self.dir / "s.ndjson"
        src.write_bytes(_stream_bytes())
        m = la.measure(str(src), None, "message_end")
        self.assertGreater(m.zst_bytes, 0)
        self.assertFalse(list(self.dir.glob("*.zst")))


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub = Path(self.tmp.name) / "lor-hub-state"
        self._w("pi/stream-a.ndjson", _stream_bytes())
        self._w("pi/out.jsonl", b'{"type":"system"}\n')
        self._w("ws9/runs/sessions/s.jsonl", b'{"type":"session"}\n')
        self._w("ws9/runs/notes.ndjson", b"plain text, not a stream\n")
        self._w("pi/stream-stub.ndjson", b'{"type":"archived","archive":"x","sha256":"y","lines":1}\n')
        self._w("pi/empty.ndjson", b"")

    def _w(self, rel, data):
        p = self.hub / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_finds_streams_out_and_sessions_and_ignores_the_rest(self):
        entries, skipped = la.discover([str(self.hub)])
        real = os.path.realpath(self.hub)
        kinds = sorted((os.path.relpath(e.path, real), e.kind) for e in entries)
        self.assertEqual(
            kinds,
            [
                ("pi/out.jsonl", "out"),
                ("pi/stream-a.ndjson", "stream"),
                ("ws9/runs/sessions/s.jsonl", "session"),
            ],
        )
        self.assertEqual(skipped, {"not-stream": 1, "archive-stub": 1})
        out = [e for e in entries if e.kind == "out"][0]
        self.assertEqual(out.label, "pi/out.jsonl")
        self.assertEqual(out.ws, "lor/pi")

    def test_ws_filter(self):
        entries, _ = la.discover([str(self.hub)], ws="ws9")
        self.assertEqual([e.kind for e in entries], ["session"])


class LivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.stream = self.dir / "stream-x.ndjson"
        self.stream.write_bytes(_stream_bytes())
        old = time.time() - 10 * 3600
        os.utime(self.stream, (old, old))

    def _entries(self):
        entries, _ = la.discover([str(self.dir)])
        return entries

    def test_idle_finished_file_is_a_candidate(self):
        plans = la.plan_entries(self._entries(), min_idle_hours=3, now=time.time())
        self.assertEqual([p.action for p in plans], ["candidate"])

    def test_recent_file_is_not_idle(self):
        plans = la.plan_entries(self._entries(), min_idle_hours=3, now=time.time() - 9 * 3600)
        self.assertEqual(plans[0].action, "skip")
        self.assertIn("not idle", plans[0].reason)

    def test_open_handle_blocks(self):
        with open(self.stream, "rb"):
            plans = la.plan_entries(self._entries(), min_idle_hours=3, now=time.time())
        self.assertEqual(plans[0].reason, "open handle")

    def test_live_launcher_blocks_and_dead_marker_does_not(self):
        running = self.dir / "running"
        running.mkdir()
        (running / "x").write_text("%d\tmodel" % os.getpid())
        plans = la.plan_entries(self._entries(), min_idle_hours=3, now=time.time())
        self.assertIn("live launcher", plans[0].reason)
        (running / "x").write_text("999999\tmodel")
        plans = la.plan_entries(self._entries(), min_idle_hours=3, now=time.time())
        self.assertEqual(plans[0].action, "candidate")
        self.assertTrue(plans[0].stale_marker)

    def test_pid_file_of_a_live_run_blocks(self):
        (self.dir / "pid").write_text(str(os.getpid()))
        plans = la.plan_entries(self._entries(), min_idle_hours=3, now=time.time())
        self.assertEqual(plans[0].reason, "pid file (live)")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.hub = self.base / "hub" / "lor-hub-state"
        self.state = self.base / "state"
        self.env = mock.patch.dict(os.environ, {"AGENT_DASH_STATE_DIR": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stream = self.hub / "pi" / "stream-a.ndjson"
        self.stream.parent.mkdir(parents=True)
        self.stream.write_bytes(_stream_bytes())
        old = time.time() - 10 * 3600
        os.utime(self.stream, (old, old))

    def _plans(self):
        entries, _ = la.discover([str(self.hub)])
        return la.plan_entries(entries, min_idle_hours=3, now=time.time())

    def _run(self, store_url, keep="remove", plans=None):
        root = la.archive_root(self.state)
        root.mkdir(parents=True, exist_ok=True)
        client = vlogs.VLogsClient(store_url)
        out = io.StringIO()
        totals, results = la.process(
            plans if plans is not None else self._plans(),
            apply=True,
            keep=keep,
            client=client,
            root=root,
            limit=None,
            out=out,
        )
        return totals, results, out.getvalue()

    def test_apply_remove_verifies_store_and_reclaims(self):
        with _FakeStore() as fs:
            _totals, results, text = self._run(fs.url)
            self.assertEqual(results["reclaimed"], 1, text)
            self.assertFalse(self.stream.exists())
            rel = la.mirror_rel(os.path.realpath(self.stream))
            self.assertEqual(len(fs.server.rows[rel]), 6)
            zst = la.archive_root(self.state) / (rel + ".zst")
            self.assertTrue(zst.exists())
            recs = la.Manifest(la.archive_root(self.state)).latest
            self.assertEqual(recs[rel]["status"], "reclaimed")
            self.assertEqual(recs[rel]["vl_tokens"], {"messages": 2, "input": 17, "output": 8, "cache_read": 150})
            self.assertEqual(recs[rel]["raw_tokens"], recs[rel]["stripped_tokens"])

    def test_rerun_neither_reposts_nor_rereads_done_files(self):
        with _FakeStore() as fs:
            self._run(fs.url)
            posts = fs.server.posts
            totals, results, _ = self._run(fs.url, plans=[])
            self.assertEqual(fs.server.posts, posts)
            self.assertEqual(results["archived"], 0)

    def test_keep_stub_leaves_a_pointer_that_is_never_archived_again(self):
        with _FakeStore() as fs:
            self._run(fs.url, keep="stub")
            body = self.stream.read_text()
            self.assertIn('"type":"archived"', body)
            entries, skipped = la.discover([str(self.hub)])
            self.assertEqual(entries, [])
            self.assertEqual(skipped.get("archive-stub"), 1)

    def test_keep_stripped_rewrites_to_the_stripped_content(self):
        with _FakeStore() as fs:
            raw_len = self.stream.stat().st_size
            self._run(fs.url, keep="stripped")
            self.assertLess(self.stream.stat().st_size, raw_len)
            self.assertIn(b"<img sha256=", self.stream.read_bytes())
            for line in self.stream.read_bytes().splitlines():
                json.loads(line)

    def test_partial_store_is_refused_and_the_raw_file_is_kept(self):
        with _FakeStore() as fs:
            rel = la.mirror_rel(os.path.realpath(self.stream))
            fs.server.rows[rel] = [{"_msg": "{}", "path": rel, "type": "x", "_time": "2026-01-01T00:00:00.000Z"}]
            _t, results, text = self._run(fs.url)
            self.assertEqual(results["errors"], 1)
            self.assertIn("store has 1 rows", text)
            self.assertTrue(self.stream.exists())

    def test_dry_run_plans_and_exits_nonzero_when_nothing_is_planned(self):
        recent = self.hub / "pi" / "fresh.ndjson"
        recent.write_bytes(_stream_bytes())
        os.utime(self.stream, None)
        os.utime(recent, None)
        rc = la.main(["--root", str(self.hub)], out=io.StringIO())
        self.assertEqual(rc, 1)

    def test_dry_run_plans_idle_files_and_writes_nothing(self):
        out = io.StringIO()
        rc = la.main(["--root", str(self.hub)], out=out)
        self.assertEqual(rc, 0)
        self.assertIn("would archive", out.getvalue())
        self.assertIn("assistant tokens (raw, stripped equal: yes): messages 2  input 17  output 8  cacheRead 150", out.getvalue())
        self.assertFalse(la.archive_root(self.state).exists())
        self.assertTrue(self.stream.exists())

    def test_apply_with_unreachable_store_exits_5(self):
        rc = la.main(["--root", str(self.hub), "--apply", "--vlogs-url", "http://127.0.0.1:9"], out=io.StringIO())
        self.assertEqual(rc, 5)
        self.assertTrue(self.stream.exists())

    def test_second_concurrent_run_is_refused_by_the_lock(self):
        root = la.archive_root(self.state)
        root.mkdir(parents=True, exist_ok=True)
        with _FakeStore() as fs, open(root / "archive.lock", "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc = la.main(["--root", str(self.hub), "--apply", "--vlogs-url", fs.url], out=io.StringIO())
        self.assertEqual(rc, 3)

    def test_argument_errors(self):
        self.assertIsInstance(la.parse_args(["--keep", "shred"]), str)
        self.assertIsInstance(la.parse_args(["--bogus"]), str)
        opts = la.parse_args(["--root", "/a", "--root", "/b", "--ws", "WS1", "--min-idle-hours", "2", "--keep", "stub"])
        self.assertEqual(opts["roots"], ["/a", "/b"])
        self.assertEqual(opts["min_idle"], 2.0)
        self.assertEqual(opts["keep"], "stub")
        self.assertFalse(opts["apply"])


class KeepByToolTest(unittest.TestCase):
    """Per-tool keep: classification, defaults, the override, and a stripped rewrite that stays done."""

    def test_tool_of_names_each_kind(self):
        self.assertEqual(la.tool_of("stream-a.ndjson", "stream", "/x/pi"), "pi")
        self.assertEqual(la.tool_of("stream-a-1.ndjson", "stream", "/x/cmd"), "cmd")
        self.assertEqual(la.tool_of("stream-a-1.ndjson", "stream", "/x/pi"), "cmd")
        self.assertEqual(la.tool_of("stream-a.ndjson", "stream", "/x/pi-n"), "pi")
        self.assertEqual(la.tool_of("stream-a-7.ndjson", "stream", "/x/y"), "cmd")
        self.assertEqual(la.tool_of("stream-a.jsonl", "stream", "/x/codex"), "codex")
        self.assertEqual(la.tool_of("s.jsonl", "session", "/x/pi"), "pi-session")
        self.assertEqual(la.tool_of("out.jsonl", "out", "/x/claude"), "out")
        self.assertEqual(la.tool_of("notes.ndjson", "ndjson", "/x"), "other")

    def test_defaults_are_per_tool_and_override_wins(self):
        def entry(tool):
            return la.Entry(path="p", root="r", rel="p", ws="w", label="l", kind="stream", size=1,
                            mtime_ns=0, mtime=0.0, outdir="o", tool=tool)
        self.assertEqual(la.DEFAULT_KEEP, None)
        self.assertEqual(la.keep_for(entry("pi")), "stripped")
        self.assertEqual(la.keep_for(entry("cmd")), "stripped")
        self.assertEqual(la.keep_for(entry("codex")), "remove")
        self.assertEqual(la.keep_for(entry("pi-session")), "remove")
        self.assertEqual(la.keep_for(entry("out")), "stripped")
        self.assertEqual(la.keep_for(entry("pi"), "remove"), "remove")

    def test_stripped_reclaims_only_the_base64_bytes(self):
        e = la.Entry(path="p", root="r", rel="p", ws="w", label="l", kind="stream", size=1000,
                     mtime_ns=0, mtime=0.0, outdir="o", tool="pi")
        m = mock.Mock(raw_bytes=1000, stripped_bytes=300)
        self.assertEqual(la.reclaim_bytes(e, m, "stripped"), 700)
        self.assertEqual(la.reclaim_bytes(e, m, "remove"), 1000)

    def test_cmd_stream_stays_stripped_and_the_next_pass_skips_it(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        hub = base / "hub" / "lor-hub-state"
        stream = hub / "cmd" / "stream-hub-exec-x-1.ndjson"
        stream.parent.mkdir(parents=True)
        stream.write_bytes(_stream_bytes())
        (stream.parent / "cmd-sessions.tsv").write_text("label\n")
        old = time.time() - 10 * 3600
        os.utime(stream, (old, old))
        state = base / "state"
        with mock.patch.dict(os.environ, {"AGENT_DASH_STATE_DIR": str(state)}), _FakeStore() as fs:
            root = la.archive_root(state)
            root.mkdir(parents=True, exist_ok=True)
            entries, _ = la.discover([str(hub)])
            self.assertEqual(entries[0].tool, "cmd")
            plans = la.plan_entries(entries, min_idle_hours=3, now=time.time())
            totals, results = la.process(plans, apply=True, keep=None, client=vlogs.VLogsClient(fs.url),
                                         root=root, limit=None, out=io.StringIO())
            self.assertEqual(results["reclaimed"], 1)
            self.assertTrue(stream.exists())
            self.assertIn(b"<img sha256=", stream.read_bytes())
            self.assertEqual(totals.by_tool["cmd"]["keep"], "stripped")
            posts = fs.server.posts
            entries, _ = la.discover([str(hub)])
            plans = la.plan_entries(entries, min_idle_hours=0, now=time.time() + 4 * 3600)
            totals, results = la.process(plans, apply=True, keep=None, client=vlogs.VLogsClient(fs.url),
                                         root=root, limit=None, out=io.StringIO())
            self.assertEqual(fs.server.posts, posts)
            self.assertEqual(results["done"], 1)
            self.assertEqual(results["reclaimed"], 0)


class CliWiringTest(unittest.TestCase):
    def test_agent_dash_logs_archive_help_path(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "agent_dash.py"), "logs", "archive", "--bogus"],
            capture_output=True,
            text=True,
            env=dict(os.environ, AGENT_DASH_STATE_DIR=tempfile.mkdtemp()),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown logs archive argument", proc.stderr)


if __name__ == "__main__":
    unittest.main()
