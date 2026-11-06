"""Streaming base64 filter: markers, chunk independence, pump, fallback, exit codes.

Covers ``dash/logfilter.py``: the replacement rule (2000+ base64 characters
become ``<img sha256=<16 hex> bytes=<n>>``, shorter runs stay byte for byte),
JSON validity of filtered lines, line count preservation, independence of
the result from chunk boundaries, bounded memory on a 30 MB line, the
child-process pump (per-line flush, exit code kept), and the fallback to raw
passthrough when the filter or the sink fails.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import logfilter  # noqa: E402

MARK = re.compile(rb"<img sha256=([0-9a-f]{16}) bytes=(\d+)>")
B64 = "".join(chr(c) for c in list(range(65, 91)) + list(range(97, 123)) + list(range(48, 58))) * 40  # 2000 chars


def _line(payload: str, extra: str = "") -> bytes:
    obj = {"type": "message_end", "message": {"role": "user", "content": [{"type": "image", "data": payload}]}, "x": extra}
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def _filter_all(data: bytes, chunk: int) -> bytes:
    flt = logfilter.StreamFilter()
    out = b""
    for i in range(0, len(data), chunk):
        out += flt.feed(data[i : i + chunk])
    return out + flt.finish()


class ReplacementRuleTest(unittest.TestCase):
    def test_long_run_inside_json_string_is_replaced_and_stays_valid(self):
        payload = B64 + "=="
        raw = _line(payload)
        out = _filter_all(raw, 1 << 16)
        self.assertEqual(out.count(b"\n"), 1)
        obj = json.loads(out)
        marker = obj["message"]["content"][0]["data"]
        m = re.fullmatch(r"<img sha256=([0-9a-f]{16}) bytes=(\d+)>", marker)
        self.assertIsNotNone(m, marker)
        self.assertEqual(int(m.group(2)), len(payload))
        self.assertEqual(m.group(1), hashlib.sha256(payload.encode()).hexdigest()[:16])
        self.assertNotIn(b'"', out[out.index(b"<img") : out.index(b">") + 1])

    def test_short_run_is_kept_byte_for_byte(self):
        payload = B64[:1999]
        raw = _line(payload)
        self.assertEqual(_filter_all(raw, 1 << 16), raw)

    def test_exactly_min_run_is_replaced(self):
        payload = B64[:2000]
        out = _filter_all(_line(payload), 1 << 16)
        self.assertIsNotNone(MARK.search(out))

    def test_non_base64_bytes_and_line_count_are_preserved(self):
        raw = b'{"a":1}\n\nplain text\n' + _line(B64) + b'{"tail":true}'
        out = _filter_all(raw, 1 << 16)
        self.assertEqual(out.count(b"\n"), raw.count(b"\n"))
        self.assertTrue(out.startswith(b'{"a":1}\n\nplain text\n'))
        self.assertTrue(out.endswith(b'{"tail":true}'))

    def test_marker_is_sha256_prefix_of_the_run(self):
        out = _filter_all(B64.encode() + b"\n", 1 << 16)
        m = MARK.search(out)
        self.assertEqual(m.group(1).decode(), hashlib.sha256(B64.encode()).hexdigest()[:16])
        self.assertEqual(int(m.group(2)), len(B64))


class ChunkIndependenceTest(unittest.TestCase):
    def test_result_does_not_depend_on_chunk_size(self):
        raw = b'{"k":1}\n' + _line(B64 + "=", "abc") + b'{"s":"' + B64[:50].encode() + b'"}\n' + _line(B64[:700]) + _line(B64 * 3)
        ref = _filter_all(raw, len(raw))
        for chunk in (1, 2, 3, 7, 64, 1000, 1999, 2000, 2001, 65536):
            self.assertEqual(_filter_all(raw, chunk), ref, "chunk %d" % chunk)

    def test_run_split_at_every_offset_is_still_one_marker(self):
        raw = b"x" + B64.encode() + b"y\n"
        ref = _filter_all(raw, len(raw))
        for cut in range(1, len(raw)):
            flt = logfilter.StreamFilter()
            out = flt.feed(raw[:cut]) + flt.feed(raw[cut:]) + flt.finish()
            self.assertEqual(out, ref, "cut %d" % cut)
            self.assertEqual(flt.replaced, 1)


class BoundedMemoryTest(unittest.TestCase):
    def test_thirty_megabyte_line_is_never_held_whole(self):
        flt = logfilter.StreamFilter()
        piece = (B64[:1000] * 64).encode()  # 64000 bytes of base64
        total = 0
        peak_held = 0
        out = bytearray()
        chunks = 0
        # 30 MB of base64 on one line, fed as the pump would (64 KB reads).
        while total < 30 * 2**20:
            out += flt.feed(piece)
            total += len(piece)
            peak_held = max(peak_held, len(flt._held))
            chunks += 1
        out += flt.feed(b"\n")
        out += flt.finish()
        self.assertLess(peak_held, logfilter.MIN_RUN)
        self.assertEqual(flt.replaced, 1)
        self.assertEqual(out.count(b"\n"), 1)
        self.assertLess(len(out), 200)
        self.assertGreater(chunks, 400)
        self.assertIn(b"bytes=%d" % total, out)


class _Sink(io.BytesIO):
    def flush(self):
        super().flush()


class PumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _child(self, code: str) -> list:
        return [sys.executable, "-c", code]

    def test_child_stdout_is_filtered_and_exit_code_kept(self):
        payload = B64
        code = "import sys; sys.stdout.write(%r); sys.stdout.write('\\n'); sys.exit(3)" % (
            '{"type":"message_end","d":"' + payload + '"}'
        )
        stream = self.dir / "stream.ndjson"
        rc = logfilter.run_filtered(self._child(code), stream)
        self.assertEqual(rc, 3)
        text = stream.read_bytes()
        self.assertEqual(text.count(b"\n"), 1)
        self.assertIsNotNone(MARK.search(text))
        json.loads(text)

    def test_stream_grows_per_line_while_child_still_runs(self):
        stream = self.dir / "live.ndjson"
        code = (
            "import sys, time\n"
            "sys.stdout.write('{\"type\":\"session\",\"id\":\"s1\"}\\n'); sys.stdout.flush()\n"
            "time.sleep(3)\n"
            "sys.stdout.write('{\"type\":\"done\"}\\n'); sys.stdout.flush()\n"
        )
        run = logfilter.start_filtered(self._child(code), stream)
        try:
            deadline = time.time() + 2.5
            seen = b""
            while time.time() < deadline:
                if stream.exists():
                    seen = stream.read_bytes()
                    if seen:
                        break
                time.sleep(0.05)
            self.assertIn(b'"session"', seen, "first line must be visible before the child exits")
            self.assertIsNone(run.proc.poll(), "child should still be running")
        finally:
            self.assertEqual(run.wait(), 0)
        self.assertEqual(stream.read_bytes().count(b"\n"), 2)

    def test_filter_failure_falls_back_to_raw_and_child_finishes(self):
        lines = "".join('{"type":"line","n":%d}\n' % i for i in range(20000))
        code = "import sys; sys.stdout.write(%r)" % lines
        stream = self.dir / "fallback.ndjson"
        calls = {"n": 0}
        real_feed = logfilter.StreamFilter.feed

        def flaky(self, data):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return real_feed(self, data)

        warnings = []
        with mock.patch.object(logfilter.StreamFilter, "feed", flaky):
            rc = logfilter.run_filtered(self._child(code), stream, warn=warnings.append)
        self.assertEqual(rc, 0)
        self.assertTrue(any("raw passthrough" in w for w in warnings), warnings)
        got = stream.read_bytes()
        self.assertEqual(got.count(b"\n"), 20000)
        self.assertTrue(got.endswith(b'{"type":"line","n":19999}\n'))

    def test_sink_failure_keeps_draining_so_child_never_blocks(self):
        lines = "x" * 300000
        code = "import sys; sys.stdout.write(%r)" % lines
        warnings = []

        class BrokenSink(io.BytesIO):
            def write(self, data):
                raise OSError("disk full")

        proc = subprocess.Popen(self._child(code), stdout=subprocess.PIPE)
        fr = logfilter.FilteredRun(proc, BrokenSink(), warnings.append)
        rc = fr.wait()
        self.assertEqual(rc, 0)
        self.assertTrue(any("sink write failed" in w for w in warnings), warnings)

    def test_warn_to_writes_into_the_launcher_err_log(self):
        err = self.dir / "err.log"
        with open(err, "a") as fh:
            logfilter.warn_to(fh)("logfilter: test message")
        self.assertIn("logfilter: test message", err.read_text())


class CliTest(unittest.TestCase):
    def test_stdin_to_stdout(self):
        raw = b'{"a":"' + B64.encode() + b'"}\n{"b":2}\n'
        proc = subprocess.run(
            [sys.executable, str(ROOT / "dash" / "logfilter.py")],
            input=raw,
            capture_output=True,
            check=True,
        )
        self.assertEqual(proc.stdout.count(b"\n"), 2)
        self.assertIsNotNone(MARK.search(proc.stdout))
        self.assertEqual(json.loads(proc.stdout.splitlines()[1]), {"b": 2})


if __name__ == "__main__":
    unittest.main()
