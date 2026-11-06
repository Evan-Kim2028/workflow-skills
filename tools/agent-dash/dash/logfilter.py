"""Streaming base64 filter for agent event streams.

An agent's NDJSON event stream repeats every image the agent viewed (a
base64 run of 2000+ characters) in several events, so one stream can be
99% base64. This module replaces each such run with a short marker::

    <img sha256=<16 hex> bytes=<n>>

The marker holds no quotes, so a JSON string that held the run stays a
valid JSON string. Line count and every other byte are preserved, so
byte-cursor readers and line-based token parsers see the same events.

Two entry points:

* ``StreamFilter`` is the pure state machine. ``feed(chunk)`` returns the
  filtered bytes for that chunk and carries an open run across chunks,
  so memory stays bounded by the chunk size plus at most ``MIN_RUN``
  bytes (a run of 2000+ characters is hashed incrementally, never held).
* ``start_filtered(argv, stream_path, ...)`` runs a child whose stdout is
  piped through the filter into ``stream_path`` on a pump thread, flushing
  per chunk so the file grows live. If the filter raises, the pump falls
  back to raw passthrough for the rest of the stream, the child keeps
  running, and the child's exit code is returned unchanged.

CLI: ``python3 dash/logfilter.py < in.ndjson > out.ndjson`` (stdin to
stdout, flushed per chunk). Exit 0 on success, including fallback.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading

#: Base64 runs at least this long are replaced (characters, padding included).
MIN_RUN = 2000
#: Read size per pump step. A line longer than this spans several steps.
CHUNK = 1 << 16

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


def filter_stream(src, dst, flt: StreamFilter | None = None, chunk: int = CHUNK) -> StreamFilter:
    """Copy ``src`` to ``dst`` through the filter, flushing each chunk."""
    flt = flt or StreamFilter()
    read = getattr(src, "read1", None) or src.read
    while True:
        data = read(chunk)
        if not data:
            break
        dst.write(flt.feed(data))
        dst.flush()
    dst.write(flt.finish())
    dst.flush()
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

    def run(self) -> None:
        read = getattr(self.src, "read1", None) or self.src.read
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
        if not data or not self.sink_ok:
            return
        try:
            self.sink.write(data)
            self.sink.flush()
        except (OSError, ValueError) as exc:
            self.sink_ok = False
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
    is used instead, so the run still goes through the filter.
    """
    sink = open(stream_path, "ab" if append else "wb")
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
        sink.close()
        raise
    if detached:
        try:
            filt = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__)],
                stdin=proc.stdout,
                stdout=sink,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError:
            filt = None
        if filt is not None:
            proc.stdout.close()  # the filter now holds the only read end
            return DetachedRun(proc, filt, sink)
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
    """stdin to stdout filter. Exit 0 even when the filter fell back."""
    src = sys.stdin.buffer
    dst = sys.stdout.buffer
    try:
        filter_stream(src, dst)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except Exception as exc:
        # The filter never blocks a worker: pass the remaining bytes raw.
        _stderr_warn("logfilter: %s; raw passthrough" % exc.__class__.__name__)
        try:
            for chunk in iter(lambda: src.read1(CHUNK), b""):
                dst.write(chunk)
                dst.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
