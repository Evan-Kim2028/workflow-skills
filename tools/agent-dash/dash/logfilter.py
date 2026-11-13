"""Streaming base64 filter for agent event streams.

An agent's NDJSON event stream repeats every image the agent viewed (a
base64 run of 2000+ characters) in several events, so one stream can be
99% base64. This module replaces each such run with a short marker::

    <img sha256=<16 hex> bytes=<n>>

The marker holds no quotes, so a JSON string that held the run stays a
valid JSON string. Line count and every other byte are preserved, so
byte-cursor readers and line-based token parsers see the same events.

Three layers, each usable on its own:

* ``StreamFilter`` is the pure state machine. ``feed(chunk)`` returns the
  filtered bytes for that chunk and carries an open run across chunks,
  so memory stays bounded by the chunk size plus at most ``MIN_RUN``
  bytes (a run of 2000+ characters is hashed incrementally, never held).
* ``BufferedSink`` groups the filtered bytes into whole lines and writes
  them in batches, flushing at most every ``FLUSH_INTERVAL`` seconds (and
  at end of stream). Readers poll the file (the dashboard's byte-cursored
  pi reader, ``worker-watch``'s growth check, the launcher's read after
  the child exits), so a couple of seconds of staleness is invisible to
  them, while the number of writes and the bytes dropped per write fall
  by an order of magnitude per stream.
* ``DeltaCoalescer``, inside the sink, merges back-to-back token delta
  events (``thinking_delta``, ``toolcall_delta``, ``text_delta``) into one
  NDJSON line per flush. Those lines are ~99% of a pi stream and no reader
  parses them; the merge concatenates the ``delta`` strings and keeps the
  event shape, so a later reader that joins deltas sees the same text.
* ``start_filtered(argv, stream_path, ...)`` runs a child whose stdout is
  piped through the filter into ``stream_path`` on a pump thread. If the
  filter raises, the pump falls back to raw passthrough for the rest of
  the stream, the child keeps running, and the child's exit code is
  returned unchanged.

CLI: ``python3 dash/logfilter.py < in.ndjson > out.ndjson`` (stdin to
stdout, same batching). Exit 0 on success, including fallback.

Two environment knobs, for a launcher that needs different behaviour:
``AGENTQ_STREAM_FLUSH_S`` (group-flush interval, default 2) and
``AGENTQ_STREAM_COALESCE=0`` (keep every delta line as it arrived).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

#: Base64 runs at least this long are replaced (characters, padding included).
MIN_RUN = 2000
#: Read size per pump step. A line longer than this spans several steps.
CHUNK = 1 << 16
#: Seconds between group flushes of the stream file (``AGENTQ_STREAM_FLUSH_S``).
FLUSH_INTERVAL = 2.0
#: How often the flush ticker wakes while a stream is open, in seconds.
TICK_S = 0.25
#: Bytes held before a write happens regardless of the interval: a burst, or one huge line.
MAX_BUFFER = 1 << 20
#: Complete lines that end a turn: a batch containing one is written at once, so a
#: live view sees a finished turn without waiting for the interval.
FLUSH_MARKS = (b'"type":"turn_end"', b'"type":"message_end"', b'"type":"agent_end"')
#: Token delta events that are merged while they arrive back to back.
DELTA_KINDS = ("text_delta", "thinking_delta", "toolcall_delta")
FLUSH_ENV = "AGENTQ_STREAM_FLUSH_S"
COALESCE_ENV = "AGENTQ_STREAM_COALESCE"
_DELTA_KINDS_B = tuple(kind.encode("ascii") for kind in DELTA_KINDS)

#: The base64 alphabet plus padding. A maximal match is one run.
RUN_RE = re.compile(rb"[A-Za-z0-9+/=]+")


class StreamFilter:
    """Replace base64 runs of ``min_run``+ characters, streaming.

    State between ``feed`` calls: whether a run is open (it may continue
    into the next chunk), its length so far, the bytes held while it is
    still short, and a running sha256 once it has reached ``min_run``.
    """

    def __init__(self, min_run: int = MIN_RUN) -> None:
        self.min_run = min_run
        self.replaced = 0
        self.replaced_bytes = 0
        self._open = False
        self._n = 0
        self._held = bytearray()
        self._hash = None

    def feed(self, data: bytes) -> bytes:
        """Filtered bytes for ``data``. An open run is held until it ends."""
        if not data:
            return b""
        out = []
        pos = 0
        for match in RUN_RE.finditer(data):
            start, end = match.span()
            if start > pos:
                if self._open:
                    out.append(self._close())
                out.append(data[pos:start])
            self._add(data[start:end])
            pos = end
        if pos < len(data):
            if self._open:
                out.append(self._close())
            out.append(data[pos:])
        # A match that reaches the end of ``data`` may continue into the
        # next chunk, so the run stays open.
        return b"".join(out)

    def finish(self) -> bytes:
        """Close a run still open at end of input."""
        return self._close() if self._open else b""

    def _add(self, piece: bytes) -> None:
        self._open = True
        self._n += len(piece)
        if self._hash is not None:
            self._hash.update(piece)
        elif self._n >= self.min_run:
            self._hash = hashlib.sha256()
            self._hash.update(self._held)
            self._hash.update(piece)
            self._held = bytearray()
        else:
            self._held += piece

    def _close(self) -> bytes:
        if self._hash is not None:
            digest = self._hash.hexdigest()[:16]
            marker = ("<img sha256=%s bytes=%d>" % (digest, self._n)).encode("ascii")
            self.replaced += 1
            self.replaced_bytes += self._n
            out = marker
        else:
            out = bytes(self._held)
        self._open = False
        self._n = 0
        self._held = bytearray()
        self._hash = None
        return out


def flush_interval(environ=None) -> float:
    """The group-flush interval for a new sink: ``AGENTQ_STREAM_FLUSH_S``, else 2 s."""
    env = os.environ if environ is None else environ
    try:
        value = float((env.get(FLUSH_ENV) or "").strip())
    except ValueError:
        return FLUSH_INTERVAL
    return value if value >= 0 else FLUSH_INTERVAL


def coalesce_enabled(environ=None) -> bool:
    """False when ``AGENTQ_STREAM_COALESCE=0``: every delta line is kept as it arrived."""
    env = os.environ if environ is None else environ
    return (env.get(COALESCE_ENV) or "").strip() not in ("0", "false", "no")


def _delta_object(line: bytes):
    """The parsed object when ``line`` is a token delta event, else None.

    A cheap substring test keeps the JSON parse off every other line: a
    ``message_end`` is never parsed here.
    """
    if b"message_update" not in line or b'"delta"' not in line:
        return None
    if not any(kind in line for kind in _DELTA_KINDS_B):
        return None
    try:
        obj = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "message_update":
        return None
    event = obj.get("assistantMessageEvent")
    if not isinstance(event, dict) or event.get("type") not in DELTA_KINDS:
        return None
    if not isinstance(event.get("delta"), str):
        return None
    return obj


def _same_event(held, obj) -> bool:
    """True when ``obj`` continues the delta event ``held``, so the two are one event."""
    mine = held["assistantMessageEvent"]
    theirs = obj["assistantMessageEvent"]
    return mine.get("type") == theirs.get("type") and mine.get("contentIndex") == theirs.get("contentIndex")


def _merge_objects(held, obj):
    """``(line, object)`` for the merge: deltas joined, every other field from ``obj``."""
    event = obj["assistantMessageEvent"]
    event["delta"] = held["assistantMessageEvent"]["delta"] + event["delta"]
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8"), obj


class DeltaCoalescer:
    """Join back-to-back token delta events into one line.

    ``add(line)`` returns the bytes that are ready to be buffered (possibly
    nothing): a delta line is held so the next one can join it, and any other
    line is passed straight through after the held delta is released. So the
    merged delta always appears just before the event that ended the run
    (``thinking_end``, ``text_end``, ``message_end``), never after it.

    The merged line keeps the original object shape -- ``type``
    ``message_update`` with an ``assistantMessageEvent`` whose ``delta`` is
    the concatenation -- because ``dash/logarchive`` matches a line's head
    ``{"type": ...`` and a later reader may still join deltas. Only whole
    lines that end in a newline are ever held.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.held = None
        self.held_obj = None
        self.merged = 0

    def add(self, line: bytes) -> bytes:
        """Bytes ready to write for ``line``: the released held line, plus ``line`` itself."""
        if not self.enabled or not line.endswith(b"\n"):
            return self.flush() + line
        obj = _delta_object(line)
        out = b""
        if self.held is not None:
            if obj is not None and _same_event(self.held_obj, obj):
                self.held, self.held_obj = _merge_objects(self.held_obj, obj)
                self.merged += 1
                return b""
            out, self.held, self.held_obj = self.held, None, None
        if obj is None:
            return out + line
        self.held, self.held_obj = line, obj
        return out

    def flush(self) -> bytes:
        """Release a held delta line (end of stream, or of a flush window)."""
        out = self.held or b""
        self.held = self.held_obj = None
        return out


class BufferedSink:
    """Whole-line, batched writes to a stream file.

    ``write(data)`` appends to an internal buffer; the buffer reaches the
    file at most every ``interval`` seconds, when it holds ``max_buffer``
    bytes, at the end of a turn (``turn_end``, ``message_end``, ``agent_end``),
    or at ``flush``/``close``. Complete lines are written as whole lines, so a
    reader polling the file sees every line whole (the byte-cursored readers
    would otherwise skip it). The one exception is a single line larger than
    ``max_buffer``: its bytes are passed through unchanged as they arrive, so
    memory stays bounded and no synthetic newline splits the event. The ticker
    thread makes the flush deadline independent of the child: a stream that
    goes quiet still shows its last lines within one interval.

    A write error is recorded in ``error`` instead of raised, so the pump can
    warn once and keep draining the child (a full pipe would hang the child).
    """

    def __init__(
        self,
        fh,
        *,
        interval: float | None = None,
        max_buffer: int = MAX_BUFFER,
        tick_s: float = TICK_S,
        clock=time.monotonic,
        coalesce: bool | None = None,
        warn=None,
    ) -> None:
        self.fh = fh
        self.interval = flush_interval() if interval is None else float(interval)
        self.max_buffer = max(1, int(max_buffer))
        self.tick_s = float(tick_s)
        self.clock = clock
        self.warn = warn
        self.coalescer = DeltaCoalescer(coalesce_enabled() if coalesce is None else coalesce)
        self.error = None
        self.writes = 0
        self.written = 0
        self._out = bytearray()
        self._tail = b""
        self._last = clock()
        self._lock = threading.Lock()
        self._stop = None
        self._thread = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "BufferedSink":
        """Start the flush ticker. Without it a quiet stream keeps its tail buffered."""
        if self.tick_s <= 0 or self._thread is not None:
            return self
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, name="logfilter-flush", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=max(1.0, self.tick_s * 4))
            self._stop = None
            self._thread = None

    def _tick(self) -> None:
        while not self._stop.wait(self.tick_s):
            with self._lock:
                if self.time_until_flush() <= 0.0:
                    self._flush()

    # -- writing -------------------------------------------------------
    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._tail += data
            cut = self._tail.rfind(b"\n")
            if cut >= 0:
                complete = self._tail[: cut + 1]
                self._tail = self._tail[cut + 1 :]
                for line in complete.splitlines(keepends=True):
                    self._out += self.coalescer.add(line)
            elif len(self._tail) > self.max_buffer:
                # One unterminated line longer than the buffer: write its bytes
                # through unchanged rather than grow without bound. Never add a
                # newline here: that would split a single event (a 1 MiB+
                # message_end) into two invalid JSON lines and lose its text.
                # The line ends when its real newline arrives; that remainder is
                # then a line fragment and passes through as one.
                self._out += self.coalescer.add(self._tail)
                self._tail = b""
            if len(self._out) >= self.max_buffer or self.time_until_flush() <= 0.0:
                self._flush()
            elif cut >= 0 and any(mark in complete for mark in FLUSH_MARKS):
                self._flush()

    def time_until_flush(self) -> float:
        return max(0.0, self.interval - (self.clock() - self._last))

    def tick(self) -> None:
        """Flush now if the interval has passed (the ticker's job, callable by hand)."""
        with self._lock:
            if self.time_until_flush() <= 0.0:
                self._flush()

    def flush(self) -> None:
        """Write everything buffered, including a held delta and an unterminated line."""
        with self._lock:
            self._out += self.coalescer.flush()
            if self._tail:
                self._out += self._tail
                self._tail = b""
            self._flush()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.stop()
            try:
                self.fh.close()
            except Exception:
                pass

    def _flush(self) -> None:
        if self.coalescer.held is not None:
            self._out += self.coalescer.flush()
        if not self._out:
            self._last = self.clock()
            return
        data = bytes(self._out)
        self._out.clear()
        self._last = self.clock()
        if self.error is not None:
            return
        try:
            self.fh.write(data)
            self.writes += 1
            self.written += len(data)
            self.fh.flush()
        except (OSError, ValueError) as exc:
            self.error = exc


def filter_stream(src, dst, flt: StreamFilter | None = None, chunk: int = CHUNK, *, coalesce=None, interval=None) -> StreamFilter:
    """Copy ``src`` to ``dst`` through the filter and the line-buffered sink."""
    flt = flt or StreamFilter()
    own = not isinstance(dst, BufferedSink)
    sink = BufferedSink(dst, coalesce=coalesce, interval=interval) if own else dst
    if own:
        sink.start()
    try:
        read = getattr(src, "read1", None) or src.read
        while True:
            data = read(chunk)
            if not data:
                break
            sink.write(flt.feed(data))
        sink.write(flt.finish())
        sink.flush()
    finally:
        if own:
            sink.stop()
    if sink.error is not None:
        raise sink.error
    return flt


class _Pump:
    """Drains ``src`` into ``sink`` through a filter. Never raises.

    On a filter error the pump writes the current chunk raw and keeps
    raw passthrough for the rest of the stream. A sink error stops writes
    but keeps draining ``src``, so the child never blocks on a full pipe.
    """

    def __init__(self, src, sink, warn) -> None:
        self.src = src
        self.sink = sink
        self.warn = warn
        self.flt = StreamFilter()
        self.raw = False
        self.sink_ok = True
        self.dropped_open_run = False

    def _start(self) -> None:
        start = getattr(self.sink, "start", None)
        if start is not None:
            start()

    def _stop(self) -> None:
        stop = getattr(self.sink, "stop", None)
        if stop is not None:
            stop()

    def run(self) -> None:
        read = getattr(self.src, "read1", None) or self.src.read
        self._start()
        try:
            while True:
                try:
                    data = read(CHUNK)
                except (OSError, ValueError):
                    break
                if not data:
                    break
                out = self._filter(data)
                self._write(out)
            if not self.raw:
                try:
                    self._write(self.flt.finish())
                except Exception as exc:  # pragma: no cover - defensive
                    self._fallback("finish", exc)
        finally:
            self._stop()

    def _filter(self, data: bytes) -> bytes:
        if self.raw:
            return data
        try:
            return self.flt.feed(data)
        except Exception as exc:
            self._fallback("feed", exc)
            return data

    def _fallback(self, where: str, exc: Exception) -> None:
        self.raw = True
        if self.flt._open:
            self.dropped_open_run = True
        try:
            self.warn("logfilter: %s failed (%s); raw passthrough from here" % (where, exc.__class__.__name__))
        except Exception:
            pass

    def _write(self, data: bytes) -> None:
        if not self.sink_ok:
            return
        try:
            if data:
                self.sink.write(data)
            if not isinstance(self.sink, BufferedSink):
                self.sink.flush()
        except (OSError, ValueError) as exc:
            self.sink_ok = False
            self._warn_sink(exc)
            return
        error = getattr(self.sink, "error", None)
        if error is not None:
            self.sink_ok = False
            self._warn_sink(error)

    def _warn_sink(self, exc: BaseException) -> None:
        try:
            self.warn("logfilter: sink write failed (%s); draining child output" % exc.__class__.__name__)
        except Exception:
            pass


class FilteredRun:
    """A child process whose stdout is pumped through the filter.

    ``proc`` is the ``subprocess.Popen`` (so callers can write its stdin
    before ``wait``). ``wait`` joins the pump and returns the child's exit
    code unchanged. ``pump`` exposes the filter state for tests and logs.
    """

    def __init__(self, proc, sink, warn) -> None:
        self.proc = proc
        self.pump = _Pump(proc.stdout, sink, warn)
        self._sink = sink
        self._thread = threading.Thread(target=self._drain, name="logfilter-pump", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            self.pump.run()
        finally:
            try:
                self.proc.stdout.close()
            except Exception:
                pass

    def wait(self) -> int:
        rc = self.proc.wait()
        self._thread.join()
        try:
            self._sink.close()
        except Exception:
            pass
        return rc


class DetachedRun:
    """A child whose stdout is read by a filter process that outlives the launcher.

    The pipe's read end moves from the launcher to ``python3 dash/logfilter.py``
    (its own session, so a signal to the launcher's group does not reach it).
    If the launcher is killed, the filter keeps reading until the child closes
    stdout, so the child's next write succeeds. Without this, the child's write
    raised SIGPIPE and the child died, where the old file redirect let it run on.
    Warnings go to the filter's stderr, which the launchers point at the err log.
    """

    def __init__(self, proc, filt, sink) -> None:
        self.proc = proc
        self.filter = filt
        self._sink = sink
        self.pump = None

    def wait(self) -> int:
        rc = self.proc.wait()
        self.filter.wait()
        try:
            self._sink.close()
        except Exception:
            pass
        return rc


def start_filtered(argv, stream_path, *, cwd=None, stdin=None, stderr=None, env=None, warn=None, append: bool = False, detached: bool = False):
    """Start ``argv`` with stdout going through the filter into ``stream_path``.

    ``detached=True`` hands the pipe to a filter process that survives this
    process (see DetachedRun). If that process cannot start, the in-process pump
    is used instead, so the run still goes through the filter. The detached
    filter is the writer, so it is the one that batches: its own sink is a
    ``BufferedSink`` over stdout.
    """
    raw = open(stream_path, "ab" if append else "wb")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
        )
    except BaseException:
        raw.close()
        raise
    if detached:
        try:
            filt = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__)],
                stdin=proc.stdout,
                stdout=raw,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError:
            filt = None
        if filt is not None:
            proc.stdout.close()  # the filter now holds the only read end
            return DetachedRun(proc, filt, raw)
    sink = BufferedSink(raw, warn=warn or _stderr_warn)
    return FilteredRun(proc, sink, warn or _stderr_warn)


def run_filtered(argv, stream_path, **kwargs) -> int:
    """Run ``argv`` through the filter to ``stream_path``; return its exit code."""
    return start_filtered(argv, stream_path, **kwargs).wait()


def _stderr_warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def warn_to(fh):
    """A warn callback that appends to an open log file (the launcher's err log)."""

    def _warn(message: str) -> None:
        fh.write(message + "\n")
        fh.flush()

    return _warn


def main(argv=None) -> int:
    """stdin to stdout filter. Exit 0 even when the filter fell back.

    ``filter_stream`` is given a sink, so it does not start or stop it: the
    ticker that makes a quiet stream's tail visible starts here, because this
    process is the detached writer the launchers use.
    """
    src = sys.stdin.buffer
    sink = BufferedSink(sys.stdout.buffer, warn=_stderr_warn).start()
    try:
        try:
            filter_stream(src, sink)
        except (BrokenPipeError, KeyboardInterrupt):
            return 0
        except Exception as exc:
            # The filter never blocks a worker: pass the remaining bytes raw.
            _stderr_warn("logfilter: %s; raw passthrough" % exc.__class__.__name__)
            try:
                for chunk in iter(lambda: src.read1(CHUNK), b""):
                    sink.write(chunk)
                sink.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        return 0
    finally:
        sink.stop()


if __name__ == "__main__":
    sys.exit(main())
