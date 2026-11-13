"""Streaming base64 filter: markers, chunk independence, pump, fallback, exit codes.

Covers ``dash/logfilter.py``: the replacement rule (2000+ base64 characters
become ``<img sha256=<16 hex> bytes=<n>>``, shorter runs stay byte for byte),
JSON validity of filtered lines, line count preservation, independence of
the result from chunk boundaries, bounded memory on a 30 MB line, the
child-process pump (exit code kept), the fallback to raw passthrough when the
filter or the sink fails, the whole-line batched sink (flush interval, buffer
cap, no partial lines, write errors recorded), delta coalescing, and reader
compatibility: ``pi.parse_stream``, ``pi.final_text`` and the archive's
line-oriented sniffing see the same events as an unbatched stream.
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
from dash.sources import pi as pi_source  # noqa: E402

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


class DetachedRunTest(unittest.TestCase):
    """detached=True: the filter outlives a killed launcher, so the child finishes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _child(self, n: int, pause: float) -> list:
        code = (
            "import sys, time\n"
            "for i in range(%d):\n"
            "    sys.stdout.write('{\"n\":%%d}\\n' %% i); sys.stdout.flush(); time.sleep(%r)\n"
            "sys.stdout.write('{\"type\":\"end\"}\\n'); sys.stdout.flush()\n"
        ) % (n, pause)
        return [sys.executable, "-c", code]

    def test_filtered_and_exit_code_kept(self):
        stream = self.dir / "d.ndjson"
        raw = _line(B64 + "==") + b'{"type":"message_end"}\n'
        code = "import sys; sys.stdout.buffer.write(%r)" % raw
        rc = logfilter.run_filtered([sys.executable, "-c", code], stream, detached=True)
        self.assertEqual(rc, 0)
        got = stream.read_bytes()
        self.assertIn(b"<img sha256=", got)
        self.assertEqual(got.count(b"\n"), 2)
        self.assertTrue(got.endswith(b'{"type":"message_end"}\n'))

    def test_child_finishes_when_the_launcher_is_killed(self):
        stream = self.dir / "killed.ndjson"
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from dash import logfilter\n"
            "logfilter.run_filtered(%r, %r, detached=True)\n"
        ) % (os.path.dirname(os.path.dirname(os.path.abspath(logfilter.__file__))), self._child(40, 0.05), str(stream))
        launcher = subprocess.Popen([sys.executable, "-c", script])
        deadline = time.time() + 5
        while time.time() < deadline and not (stream.exists() and stream.stat().st_size > 0):
            time.sleep(0.05)
        launcher.kill()
        launcher.wait()
        deadline = time.time() + 15
        data = b""
        while time.time() < deadline:
            data = stream.read_bytes()
            if data.endswith(b'{"type":"end"}\n'):
                break
            time.sleep(0.1)
        self.assertTrue(data.endswith(b'{"type":"end"}\n'), "child stopped after its launcher died: %r" % data[-60:])
        self.assertEqual(data.count(b"\n"), 41)

    def test_a_quiet_detached_stream_is_visible_without_eof(self):
        # The detached filter process is the writer, so its ticker (not EOF) must flush.
        stream = self.dir / "quiet.ndjson"
        code = (
            "import sys, time\n"
            "sys.stdout.write('{\"type\":\"session\",\"id\":\"s1\"}\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        run = logfilter.start_filtered([sys.executable, "-c", code], stream, detached=True)
        try:
            deadline = time.time() + 3.5
            seen = b""
            while time.time() < deadline:
                seen = stream.read_bytes() if stream.exists() else b""
                if b'"session"' in seen:
                    break
                time.sleep(0.05)
            self.assertIn(b'"session"', seen, "a quiet detached stream must flush within one interval")
            self.assertIsNone(run.proc.poll(), "the child is still running, so this was not the EOF flush")
        finally:
            run.proc.kill()
            run.wait()

    def test_in_process_pump_still_stops_the_child_when_the_launcher_dies(self):
        # The default (detached=False) keeps the old in-process behaviour; this documents why the launchers pass detached=True.
        stream = self.dir / "pump.ndjson"
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from dash import logfilter\n"
            "logfilter.run_filtered(%r, %r)\n"
        ) % (os.path.dirname(os.path.dirname(os.path.abspath(logfilter.__file__))), self._child(40, 0.05), str(stream))
        launcher = subprocess.Popen([sys.executable, "-c", script])
        deadline = time.time() + 5
        while time.time() < deadline and not (stream.exists() and stream.stat().st_size > 0):
            time.sleep(0.05)
        launcher.kill()
        launcher.wait()
        time.sleep(2.5)
        self.assertFalse(stream.read_bytes().endswith(b'{"type":"end"}\n'))


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

    def test_stream_grows_while_child_still_runs(self):
        # The sink groups writes; the flush interval is what bounds how stale a live view can be.
        stream = self.dir / "live.ndjson"
        code = (
            "import sys, time\n"
            "sys.stdout.write('{\"type\":\"session\",\"id\":\"s1\"}\\n'); sys.stdout.flush()\n"
            "time.sleep(3)\n"
            "sys.stdout.write('{\"type\":\"done\"}\\n'); sys.stdout.flush()\n"
        )
        with mock.patch.dict(os.environ, {logfilter.FLUSH_ENV: "0.5"}):
            run = logfilter.start_filtered(self._child(code), stream)
        try:
            deadline = time.time() + 2.0
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


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def delta(content_index: int, text: str, kind: str = "text_delta", sent_at: float = 0.0) -> bytes:
    obj = {
        "type": "message_update",
        "sentAt": sent_at,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "assistantMessageEvent": {"type": kind, "contentIndex": content_index, "delta": text},
    }
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def event(kind: str, **fields) -> bytes:
    obj = {"type": kind}
    obj.update(fields)
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


class BufferedSinkTest(unittest.TestCase):
    """Writes reach the file at most once per interval, whole lines only."""

    def sink(self, *, interval=2.0, max_buffer=1 << 20, coalesce=False):
        fh = _Sink()
        clock = FakeClock()
        return logfilter.BufferedSink(
            fh, interval=interval, max_buffer=max_buffer, tick_s=0, clock=clock, coalesce=coalesce
        ), fh, clock

    def test_nothing_reaches_the_file_before_the_interval(self):
        sink, fh, clock = self.sink(interval=2.0)
        sink.write(event("turn_start"))
        self.assertEqual(fh.getvalue(), b"")
        clock.advance(1.9)
        sink.tick()
        self.assertEqual(fh.getvalue(), b"")
        clock.advance(0.2)
        sink.tick()
        self.assertEqual(fh.getvalue(), event("turn_start"))
        self.assertEqual(sink.writes, 1)

    def test_many_writes_within_one_interval_become_one_file_write(self):
        sink, fh, clock = self.sink(interval=2.0)
        for index in range(500):
            sink.write(event("tool_execution_update", n=index))
        self.assertEqual(fh.getvalue(), b"")
        clock.advance(2.1)
        sink.tick()
        self.assertEqual(sink.writes, 1)
        self.assertEqual(fh.getvalue().count(b"\n"), 500)

    def test_a_full_buffer_flushes_without_waiting(self):
        sink, fh, clock = self.sink(interval=60.0, max_buffer=120)
        sink.write(event("message_start", padding="x" * 200))
        self.assertEqual(sink.writes, 1)
        self.assertGreater(len(fh.getvalue()), 0)

    def test_a_partial_line_never_reaches_the_file(self):
        sink, fh, clock = self.sink(interval=1.0, coalesce=False)
        sink.write(b'{"type":"message_start"}\n{"type":"mess')
        clock.advance(1.1)
        sink.tick()
        self.assertEqual(fh.getvalue(), b'{"type":"message_start"}\n')
        sink.write(b'age_end"}\n')
        clock.advance(1.1)
        sink.tick()
        self.assertEqual(fh.getvalue(), b'{"type":"message_start"}\n{"type":"message_end"}\n')
        json.loads(fh.getvalue().splitlines()[1])

    def test_a_completed_turn_is_written_without_waiting_for_the_interval(self):
        sink, fh, clock = self.sink(interval=60.0, coalesce=True)
        sink.write(delta(0, "half a sen"))
        self.assertEqual(fh.getvalue(), b"")
        sink.write(event("turn_end"))
        self.assertEqual(fh.getvalue().count(b"\n"), 2)
        self.assertTrue(fh.getvalue().endswith(event("turn_end")))
        held = json.loads(fh.getvalue().splitlines()[0])
        self.assertEqual(held["assistantMessageEvent"]["delta"], "half a sen")

    def test_flush_releases_everything_including_the_tail(self):
        sink, fh, clock = self.sink(interval=60.0)
        sink.write(b'{"type":"a"}\n{"ty')
        sink.flush()
        self.assertEqual(fh.getvalue(), b'{"type":"a"}\n{"ty')

    def test_close_flushes_and_closes(self):
        class Keeping(io.BytesIO):
            saved = b""

            def close(self):
                self.saved = self.getvalue()
                super().close()

        fh = Keeping()
        sink = logfilter.BufferedSink(fh, interval=60.0, tick_s=0, clock=FakeClock(), coalesce=False)
        sink.write(event("turn_end"))
        sink.close()
        self.assertEqual(fh.saved, event("turn_end"))
        self.assertTrue(fh.closed)

    def test_a_write_error_is_recorded_not_raised(self):
        class Broken(io.BytesIO):
            def write(self, data):
                raise OSError("disk full")

        sink = logfilter.BufferedSink(Broken(), interval=0.0, tick_s=0, coalesce=False)
        sink.write(event("turn_start"))  # interval 0 means write now; must not raise
        self.assertIsInstance(sink.error, OSError)
        sink.write(event("turn_end"))
        self.assertIsInstance(sink.error, OSError)
        sink.close()

    def test_the_ticker_flushes_a_quiet_stream(self):
        fh = _Sink()
        sink = logfilter.BufferedSink(fh, interval=0.2, tick_s=0.02, coalesce=False).start()
        try:
            sink.write(event("turn_start"))
            deadline = time.time() + 2.0
            while time.time() < deadline and not fh.getvalue():
                time.sleep(0.01)
            self.assertEqual(fh.getvalue(), event("turn_start"))
        finally:
            sink.close()

    def test_the_interval_can_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {logfilter.FLUSH_ENV: "0.25"}):
            sink = logfilter.BufferedSink(_Sink(), tick_s=0, coalesce=False)
        self.assertEqual(sink.interval, 0.25)
        with mock.patch.dict(os.environ, {logfilter.FLUSH_ENV: "nonsense"}):
            self.assertEqual(logfilter.flush_interval(), logfilter.FLUSH_INTERVAL)


class LargeEventTest(unittest.TestCase):
    """An event bigger than the sink's buffer must reach the file byte for byte."""

    def big_message_end(self) -> tuple[bytes, str]:
        text = "".join("report line %06d. " % index for index in range(70_000))
        raw = event(
            "message_end",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "usage": {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4},
            },
        )
        self.assertGreater(len(raw), logfilter.MAX_BUFFER + 100_000)
        return raw, text

    def test_a_message_end_over_the_buffer_survives_byte_identical(self):
        raw, text = self.big_message_end()
        self.assertEqual(raw.count(b"\n"), 1)
        out = io.BytesIO()
        logfilter.filter_stream(io.BytesIO(raw), out, chunk=1 << 16, coalesce=True, interval=0.0)
        got = out.getvalue()
        self.assertEqual(got.count(b"\n"), 1, "one event must stay one line")
        self.assertEqual(got, raw, "the event's bytes must not change")
        self.assertEqual(pi_source.final_text(got), text)
        json.loads(got)

    def test_a_partial_line_over_the_buffer_is_not_terminated_early(self):
        text = "word " * 230_000  # no long base64 run, so the filter passes it straight through
        raw = event("message_end", message={"role": "assistant", "content": [{"type": "text", "text": text}]})
        fh = _Sink()
        clock = FakeClock()
        sink = logfilter.BufferedSink(fh, interval=1.0, tick_s=0, clock=clock, coalesce=False)
        flt = logfilter.StreamFilter()
        sink.write(flt.feed(raw[: logfilter.MAX_BUFFER + 1000]))
        clock.advance(1.1)
        sink.tick()
        held = fh.getvalue()
        self.assertGreater(len(held), 0, "the bytes must be passed through, not held")
        self.assertNotEqual(held[-1:], b"\n", "no newline may be invented for an unfinished event")
        sink.write(flt.feed(raw[logfilter.MAX_BUFFER + 1000 :]))
        sink.write(flt.finish())
        sink.flush()
        self.assertEqual(fh.getvalue(), raw)


class DeltaCoalescerTest(unittest.TestCase):
    def test_consecutive_text_deltas_become_one_line(self):
        coal = logfilter.DeltaCoalescer(True)
        out = coal.add(delta(0, "Hel")) + coal.add(delta(0, "lo, ")) + coal.add(delta(0, "world"))
        self.assertEqual(out, b"")
        out += coal.flush()
        self.assertEqual(out.count(b"\n"), 1)
        obj = json.loads(out)
        self.assertEqual(obj["assistantMessageEvent"]["delta"], "Hello, world")
        self.assertEqual(coal.merged, 2)

    def test_the_merged_line_comes_before_the_event_that_ends_the_run(self):
        coal = logfilter.DeltaCoalescer(True)
        out = coal.add(delta(0, "a")) + coal.add(delta(0, "b"))
        out += coal.add(event("message_update", assistantMessageEvent={"type": "text_end", "contentIndex": 0}))
        self.assertEqual(out.count(b"\n"), 2)
        first, second = out.splitlines()
        self.assertEqual(json.loads(first)["assistantMessageEvent"]["delta"], "ab")
        self.assertEqual(json.loads(second)["assistantMessageEvent"]["type"], "text_end")

    def test_a_different_content_index_is_not_merged(self):
        coal = logfilter.DeltaCoalescer(True)
        out = coal.add(delta(0, "text")) + coal.add(delta(1, "tool", kind="toolcall_delta")) + coal.flush()
        self.assertEqual(out.count(b"\n"), 2)
        self.assertEqual([json.loads(line)["assistantMessageEvent"]["contentIndex"] for line in out.splitlines()], [0, 1])

    def test_disabled_coalescing_keeps_every_delta_line(self):
        coal = logfilter.DeltaCoalescer(False)
        out = b"".join(coal.add(delta(0, str(index))) for index in range(5))
        self.assertEqual(out.count(b"\n"), 5)
        self.assertEqual(coal.merged, 0)
        self.assertEqual(coal.flush(), b"")

    def test_message_end_is_never_held(self):
        coal = logfilter.DeltaCoalescer(True)
        line = event("message_end", message={"role": "assistant", "usage": {"input": 1}})
        self.assertEqual(coal.add(line), line)
        self.assertEqual(coal.flush(), b"")


def fixture_stream(turns: int = 4) -> bytes:
    """A small stream in pi's shape: deltas inside message_update, usage in message_end."""
    parts = [event("session", id="s1", model="morph-dsv41flash-1017"), event("agent_start")]
    for turn in range(turns):
        parts += [event("turn_start")]
        parts += [event("message_start", message={"role": "assistant"})]
        parts += [
            event("message_update", assistantMessageEvent={"type": "text_start", "contentIndex": 0}),
            *[delta(0, f"word{index} ") for index in range(20)],
            *[delta(0, "think ", kind="thinking_delta") for _ in range(20)],
            *[delta(1, "arg ", kind="toolcall_delta") for _ in range(20)],
            event("message_update", assistantMessageEvent={"type": "text_end", "contentIndex": 0}),
        ]
        parts += [
            event(
                "message_end",
                message={
                    "role": "assistant",
                    "model": "morph-dsv41flash-1017",
                    "content": [{"type": "text", "text": f"turn {turn} report"}],
                    "usage": {
                        "input": 1000 + turn,
                        "cacheRead": 2000,
                        "cacheWrite": 100,
                        "output": 50,
                        "cacheWrite5m": 60,
                        "cacheWrite1h": 40,
                    },
                },
            ),
            event("turn_end"),
        ]
    parts += [event("agent_end")]
    return b"".join(parts)


class ReaderCompatibilityTest(unittest.TestCase):
    """The batched, coalesced stream must look the same to every reader."""

    def filtered(self, raw: bytes, *, coalesce: bool = True, chunk: int = 1 << 16) -> bytes:
        out = io.BytesIO()
        logfilter.filter_stream(io.BytesIO(raw), out, chunk=chunk, coalesce=coalesce, interval=0.0)
        return out.getvalue()

    def test_usage_events_are_identical(self):
        raw = fixture_stream()
        got = self.filtered(raw)
        self.assertEqual(
            pi_source.parse_stream(got, "sid"),
            pi_source.parse_stream(raw, "sid"),
        )

    def test_the_final_report_text_is_identical(self):
        raw = fixture_stream()
        self.assertEqual(pi_source.final_text(self.filtered(raw)), pi_source.final_text(raw))
        self.assertIn("turn 3 report", pi_source.final_text(raw))

    def test_the_stream_is_smaller_and_every_line_stays_valid_and_typed(self):
        raw = fixture_stream(8)
        got = self.filtered(raw)
        self.assertLess(len(got), len(raw))
        self.assertLess(got.count(b"\n"), raw.count(b"\n"))
        for line in got.splitlines():
            self.assertTrue(line.startswith(b'{"type":"'), line[:40])
            self.assertIn("type", json.loads(line))

    def test_a_byte_cursor_reader_always_sees_whole_lines(self):
        raw = fixture_stream()
        fh = _Sink()
        sink = logfilter.BufferedSink(fh, interval=60.0, tick_s=0, coalesce=True)
        flt = logfilter.StreamFilter()
        position = 0
        for index in range(0, len(raw), 97):
            sink.write(flt.feed(raw[index : index + 97]))
            seen = fh.getvalue()[position:]
            if seen:
                self.assertTrue(seen.endswith(b"\n"), seen[-60:])
                position += len(seen)
        sink.flush()
        tail = fh.getvalue()[position:]
        self.assertTrue(tail == b"" or tail.endswith(b"\n"))
        self.assertEqual(pi_source.final_text(fh.getvalue()), pi_source.final_text(raw))
        self.assertLess(fh.getvalue().count(b"\n"), raw.count(b"\n"))
