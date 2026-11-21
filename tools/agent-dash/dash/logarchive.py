"""Archive finished agent logs: stripped zstd copy, VictoriaLogs rows, reclaim.

``agent_dash.py logs archive`` walks the hub roots for agent event streams
(``*.ndjson``, ``out.jsonl``, ``stream-*.jsonl``, pi ``sessions/*.jsonl``)
and, for each finished one, does this, in order:

1. **measure** (one streaming pass, bounded memory): base64 runs are
   replaced with ``dash.logfilter`` markers, the stripped bytes go through
   ``zstd -9 --long=27`` into the mirror path under ``<state>/log-archive/``, and both
   the raw and the stripped bytes are scanned for line count and assistant
   token totals (``message_end``, role assistant: input, output, cacheRead).
   A sha256 of the stripped bytes is kept.
2. **verify the copy**: the zstd file decompresses to exactly the stripped
   sha256; raw and stripped line counts and token totals are equal.
3. **ingest**: the rows are read back from the zstd copy (never the raw
   file), redacted and truncated by ``dash.vlogs``, and posted to the local
   VictoriaLogs store. Before posting, the store is asked how many rows
   this path already has: zero means ingest, the expected count means
   already done (no duplicate), anything else stops with an error.
4. **verify the store**: row count equals the line count; the stored token
   sums equal the sums over the rows sent.
5. **reclaim** (only with ``--apply``): the raw file is removed, or
   replaced according to ``--keep`` (see ``KEEP_CHOICES``), after a fresh
   open-handle and size/mtime check.

Each step writes a line to ``<state>/log-archive/manifest.jsonl``. A rerun
reads the manifest, skips finished files (same size and mtime) and
resumes the rest, so it never rereads a done file or re-ingests its rows.
Dry run (the default) runs step 1 and the skip rules only: it writes no
zstd copy, no manifest and no lock, and never touches the store.

Two sources feed the same pipeline. The hub roots (``log_archive.roots``) hold
workstream run folders. ``--agentq`` adds the agentq job folders under
``$AGENTQ_HOME/jobs`` (``~/.local/state/agentq/jobs``): a job whose state is
terminal and that finished at least ``--min-idle-hours`` ago contributes its
``runs/<run>/pi/stream-*.ndjson`` and ``runs/<run>/pi/sessions/*.jsonl`` files,
including the ``.gz`` copies ``agentq.janitor`` leaves behind. Rows from a job
carry ``ws="agentq/<workspace basename>"``, ``label=<job label>`` and the extra
fields ``job_id``, ``job_kind``, ``tier``, ``verdict``, ``session``, ``attempt``.
A gzip file cannot be stripped in place, so its keep is ``remove``: the zstd
copy is the compressed copy.

Entry points: ``main(argv)`` (the CLI), ``run(opts)`` (for a later
``ws archive`` hook: ``run({"ws": "WS7", "apply": True})`` limits the pass to
one workstream and returns the same exit codes), and ``archive_entries`` (the
shared body ``agentq gc`` calls so it archives before it reclaims).
"""

from __future__ import annotations

import fcntl
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dash import logfilter
from dash import vlogs
from dash.dataroot import state_dir as _state_dir

def default_roots() -> list[str]:
    """Hub roots scanned when no ``--root`` is given: ``log_archive.roots`` from the
    agent-dash local config (``<state>/config.local.json``, git-ignored). The repo
    names no project, and there is no fallback: a hub that is not listed is never
    scanned (``hub_dir`` may name an unrelated project). Empty means refuse.
    """
    try:
        with open(os.path.join(_state_dir(), "config.local.json"), encoding="utf-8") as fh:
            local = json.load(fh)
    except (OSError, ValueError):
        return []
    section = local.get("log_archive") if isinstance(local, dict) else None
    if not isinstance(section, dict) or not isinstance(section.get("roots"), list):
        return []
    return [os.path.expanduser(r) for r in section["roots"] if isinstance(r, str) and r]

MIN_IDLE_HOURS = 3.0
ZSTD_LEVEL = "-9"
#: -9 with a 128 MiB window: -19 --long=31 saved 2-5% for 20-180x the CPU
#: (2026-11-21 measurement), too costly on the shared laptop. Readers pass the
#: same window flag.
ZSTD_LONG = "--long=27"
#: Largest message_end line whose body is kept for token parsing. Larger
#: lines are counted but not parsed; the verify step then refuses them.
PARSE_CAP = 64 * 2**20
READ_CHUNK = 1 << 20
KEEP_CHOICES = ("remove", "stub", "stripped")
#: What stays at the original path, per tool. Set from the option test in
#: docs/log-archive.md ("Per-kind decision"). ``stripped`` where a poller or
#: an outside reader needs the file: a fresh-store rebuild or a crashed launch
#: loses run rows under ``remove`` and ``stub`` (measured). ``remove`` only
#: where nothing reads the file after its launcher has finished.
KEEP_BY_TOOL = {
    "pi": "stripped",        # pi.poll: session id from the stream on the crash path (cold rebuild loses a run)
    "cmd": "stripped",       # commandcode.poll: run_start in the stream on the crash path (run row lost)
    "codex": "remove",       # no reader of stream-<label>.jsonl after the launcher's own finish
    "pi-session": "remove",  # pi sessions/*.jsonl duplicate the stream; no reader of the contents
    "out": "stripped",       # out.jsonl: readers outside this repo read tool calls by path
    "other": "stripped",     # unknown ndjson: keep the readable copy at the path
}
#: None means per tool (``KEEP_BY_TOOL``); a ``--keep`` value overrides every tool.
DEFAULT_KEEP = None
SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "__pycache__"}
#: The event type that carries assistant usage, per kind. Stream files
#: (pi stdout) use ``message_end``; pi's own session files use ``message``.
TOKEN_TYPE = {"session": "message"}
DEFAULT_TOKEN_TYPE = "message_end"


def token_type(kind: str) -> str:
    return TOKEN_TYPE.get(kind, DEFAULT_TOKEN_TYPE)
HEAD_RE = re.compile(rb'^\{"type":"([^"]+)"')
T_EPOCH_RE = re.compile(rb'"timestamp":(\d{13})')
T_ISO_RE = re.compile(rb'"timestamp":"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"')
SNIFF_RE = re.compile(rb'\{\s*"type"\s*:')
#: First line of a stub left by ``--keep stub``. Never archived again.
STUB_RE = re.compile(rb'\{"type":"archived",')


class ArchiveError(RuntimeError):
    """A step failed in a way that must stop this run (not the whole pass)."""


# ---------------------------------------------------------------- discovery

@dataclass
class Entry:
    path: str
    root: str
    rel: str          # mirror path: relative to ~/Documents/projects when possible
    ws: str
    label: str
    kind: str         # stream | out | session | ndjson
    size: int
    mtime_ns: int
    mtime: float
    outdir: str
    tool: str = "other"   # key into KEEP_BY_TOOL
    #: The file is gzip-compressed on disk (``agentq.janitor`` gzips old streams).
    compressed: bool = False
    #: ``hub`` (a workstream run folder) or ``agentq`` (a job folder).
    source: str = "hub"
    #: Extra store fields for every row of this file (agentq job identity).
    extra: dict = field(default_factory=dict)


def projects_root() -> Path:
    return Path(os.path.expanduser("~/Documents/projects"))


def mirror_rel(path: str) -> str:
    """The path a file is mirrored under: relative to the projects root, else sans leading slash."""
    p = Path(path)
    try:
        return str(p.relative_to(projects_root()))
    except ValueError:
        return str(p).lstrip("/")


def _kind(name: str, parent: str) -> str | None:
    if parent == "sessions" and name.endswith(".jsonl"):
        return "session"
    if name == "out.jsonl":
        return "out"
    if name.startswith("stream-") and name.endswith((".jsonl", ".ndjson")):
        return "stream"
    if name.endswith(".ndjson"):
        return "ndjson"
    return None


def _label(name: str, kind: str, parent: str) -> str:
    if kind == "out":
        return "%s/out.jsonl" % parent
    base = name
    if base.startswith("stream-"):
        base = base[len("stream-"):]
    for ext in (".ndjson", ".jsonl"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base


_CMD_STREAM_RE = re.compile(r"^stream-(.+)-(\d+)\.ndjson$")


def tool_of(name: str, kind: str, outdir: str) -> str:
    """Which tool wrote the file: the key into ``KEEP_BY_TOOL``.

    A manifest beside the stream decides first (``cmd-sessions.tsv`` is Command
    Code, ``pi-sessions.tsv`` is pi). Without one, the name decides: ``.jsonl``
    is codex, ``stream-<label>-<n>.ndjson`` is Command Code, else pi.
    """
    if kind == "session":
        return "pi-session"
    if kind == "out":
        return "out"
    if kind == "stream":
        if name.endswith(".jsonl"):
            return "codex"
        if os.path.isfile(os.path.join(outdir, "cmd-sessions.tsv")):
            return "cmd"
        if os.path.isfile(os.path.join(outdir, "pi-sessions.tsv")):
            return "pi"
        return "cmd" if _CMD_STREAM_RE.match(name) else "pi"
    return "other"


def keep_for(entry: "Entry", override: str | None = None) -> str:
    """What reclaim leaves at the path: the ``--keep`` override, else the tool's default.

    A gzip file is never ``stripped``: the rewrite would decompress it at the
    path and grow it. Its zstd copy is the compressed copy, so it is removed.
    """
    keep = override or KEEP_BY_TOOL.get(entry.tool, "stripped")
    if entry.compressed and keep == "stripped":
        return "remove"
    return keep


def reclaim_bytes(entry: Entry, m, keep: str) -> int:
    """Bytes that leave the original path under ``keep``.

    ``remove`` and ``stub`` give back the whole file; ``stripped`` gives back only
    the base64 that the rewrite removed.
    """
    if keep == "stripped":
        return max(0, m.raw_bytes - m.stripped_bytes)
    return entry.size


def _stream_kind_of(path: str) -> str | None:
    """``"stream"`` for a JSON-object-with-type file, ``"stub"`` for an archive stub, else None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    if STUB_RE.match(head):
        return "stub"
    return "stream" if SNIFF_RE.match(head) else None


def discover(roots, *, ws: str | None = None, exclude=()) -> tuple[list[Entry], dict]:
    """All candidate stream files under ``roots``, plus a count of what was skipped.

    ``exclude`` is a list of glob patterns matched (``fnmatch``, ``*`` crosses
    ``/``) against each path relative to its root, e.g. ``catalog-confidence/*``.
    Excluded files are counted as ``excluded`` and never stat'ed further.
    """
    entries: list[Entry] = []
    skipped: dict = {}
    for root in roots:
        root = os.path.realpath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        hub = os.path.basename(root)
        hub = hub[: -len("-hub-state")] if hub.endswith("-hub-state") else hub
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            parent = os.path.basename(dirpath)
            for name in filenames:
                kind = _kind(name, parent)
                if kind is None:
                    continue
                path = os.path.join(dirpath, name)
                rel_root = os.path.relpath(path, root)
                if exclude and any(fnmatch.fnmatchcase(rel_root, g) for g in exclude):
                    skipped["excluded"] = skipped.get("excluded", 0) + 1
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or st.st_size == 0:
                    continue
                rel_hub = os.path.relpath(path, root).split(os.sep)
                first = rel_hub[0] if len(rel_hub) > 1 else "_root"
                ws_id = "%s/%s" % (hub, first)
                if ws and ws not in (ws_id, first):
                    continue
                shape = _stream_kind_of(path)
                if shape != "stream":
                    key = "archive-stub" if shape == "stub" else "not-stream"
                    skipped[key] = skipped.get(key, 0) + 1
                    continue
                outdir = os.path.dirname(dirpath) if kind == "session" else dirpath
                entries.append(
                    Entry(
                        path=path,
                        root=root,
                        rel=mirror_rel(path),
                        ws=ws_id,
                        label=_label(name, kind, parent),
                        kind=kind,
                        size=st.st_size,
                        mtime_ns=st.st_mtime_ns,
                        mtime=st.st_mtime,
                        outdir=outdir,
                        tool=tool_of(name, kind, outdir),
                    )
                )
    entries.sort(key=lambda e: e.path)
    return entries, skipped


# ------------------------------------------------------- agentq job folders

#: ``$AGENTQ_HOME``, else ``~/.local/state/agentq`` (the same default as ``agentq.db``).
AGENTQ_HOME_DEFAULT = "~/.local/state/agentq"
#: Mirror prefix for a job file: ``agentq/jobs/<id>/runs/<run>/pi/stream-<label>.ndjson``.
AGENTQ_REL_PREFIX = "agentq/jobs"
#: Job states that mean the job will not write again (``agentq.db.TERMINAL_STATES``).
AGENTQ_TERMINAL = ("done", "failed", "cancelled")
#: The job row fields every row of that job's log carries. ``kind`` is already the
#: file kind in the store, so the job's own kind is ``job_kind``.
AGENTQ_EXTRA_FIELDS = ("job_id", "job_kind", "tier", "verdict", "session", "attempt")


def agentq_home(env=None) -> Path:
    """The agentq home: ``$AGENTQ_HOME``, else ``~/.local/state/agentq``."""
    env = os.environ if env is None else env
    return Path(os.path.expanduser(env.get("AGENTQ_HOME") or AGENTQ_HOME_DEFAULT))


def agentq_jobs(home) -> list[dict]:
    """The job rows the agentq pass needs, read from ``<home>/agentq.db`` without writing.

    Read-only: the broker owns the database and may be writing it. A missing
    database is an empty list (nothing to archive), not an error.
    """
    db_path = Path(str(home)) / "agentq.db"
    if not db_path.is_file():
        return []
    columns = "id, state, label, workspace, kind, tier, verdict, session, attempt, finished_at"
    query = "SELECT %s FROM jobs WHERE state IN (?, ?, ?)" % columns
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA query_only=ON")
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, AGENTQ_TERMINAL).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _agentq_kind(name: str, parent: str) -> str | None:
    """The file kind for a job-folder file, or None. A ``.gz`` suffix is the janitor's copy."""
    base = name[: -len(".gz")] if name.endswith(".gz") else name
    if parent == "sessions" and base.endswith(".jsonl"):
        return "session"
    if base.startswith("stream-") and base.endswith((".jsonl", ".ndjson")):
        return "stream"
    return None


def agentq_extra(job: dict) -> dict:
    """The job fields every row of that job's log carries (``make_row`` drops empty ones)."""
    values = {
        "job_id": job.get("id"),
        "job_kind": job.get("kind"),
        "tier": job.get("tier"),
        "verdict": job.get("verdict"),
        "session": job.get("session"),
        "attempt": job.get("attempt"),
    }
    return {key: (None if value is None else str(value)) for key, value in values.items()}


def agentq_entry(path, job: dict, home, st=None) -> "Entry | None":
    """The ``Entry`` for one job-folder file, or None when it is not a stream or session file.

    ``st`` is the ``lstat`` the caller already took (``_agentq_files`` does), so a
    full scan costs one stat per file instead of three.
    """
    p = Path(str(path))
    kind = _agentq_kind(p.name, p.parent.name)
    if kind is None:
        return None
    if st is None:
        try:
            st = os.lstat(str(p))
        except OSError:
            return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size == 0:
        return None
    jobs_dir = Path(str(home)) / "jobs"
    try:
        rel = os.path.relpath(str(p), str(jobs_dir))
    except ValueError:
        return None
    workspace = str(job.get("workspace") or "").rstrip("/")
    outdir = str(p.parent.parent) if kind == "session" else str(p.parent)
    return Entry(
        path=str(p),
        root=str(jobs_dir),
        rel=AGENTQ_REL_PREFIX + "/" + rel,
        ws="agentq/" + (os.path.basename(workspace) or "unknown"),
        label=str(job.get("label") or ""),
        kind=kind,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        mtime=st.st_mtime,
        outdir=outdir,
        tool=tool_of(p.name, kind, outdir),
        compressed=p.name.endswith(".gz"),
        source="agentq",
        extra=agentq_extra(job),
    )


def _agentq_files(run_dir: Path) -> list[tuple[Path, os.stat_result]]:
    """Every stream and session file under one run folder, with its ``lstat``, symlinks skipped.

    ``os.scandir`` rather than ``os.walk``: the dirent type answers "is a
    directory" without a syscall, so a full scan of 15,000 job folders costs one
    ``lstat`` per candidate file instead of three stats and an ``islink`` each.
    """
    found: list[tuple[Path, os.stat_result]] = []
    stack = [str(run_dir)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                items = list(it)
        except OSError:
            continue
        parent = os.path.basename(current)
        for item in items:
            try:
                if item.is_dir(follow_symlinks=False):
                    stack.append(item.path)
                    continue
                if not item.is_file(follow_symlinks=False):
                    continue
                if _agentq_kind(item.name, parent) is None:
                    continue
                st = item.stat(follow_symlinks=False)
            except OSError:
                continue
            found.append((Path(item.path), st))
    return found


def discover_agentq(home, jobs, *, min_idle_hours: float = MIN_IDLE_HOURS, now: float | None = None,
                    exclude=()) -> tuple[list[Entry], dict]:
    """Stream and session files of finished agentq jobs, plus a count of what was skipped.

    A job is considered only when its state is terminal and it finished at least
    ``min_idle_hours`` ago. The job id must be plain lowercase hex and its
    ``runs`` folder must resolve inside ``<home>/jobs`` (``agentq.gc.job_runs_root``),
    so a bad row or a planted symlink cannot point the pass outside the tree.
    A live ``running/`` pid is left to ``plan_entries``, which refuses it.
    """
    from agentq import gc as gc_lib  # lazy: agentq.gc imports this module for its archive-first pass

    now = time.time() if now is None else float(now)
    cutoff = now - float(min_idle_hours) * 3600.0
    home = Path(str(home))
    jobs_dir = home / "jobs"
    try:
        jobs_resolved = jobs_dir.resolve(strict=True)
    except OSError:
        jobs_resolved = None
    entries: list[Entry] = []
    skipped: dict = {}

    def bump(key: str) -> None:
        skipped[key] = skipped.get(key, 0) + 1

    for job in jobs:
        if job.get("state") not in AGENTQ_TERMINAL:
            bump("not terminal")
            continue
        finished = job.get("finished_at")
        if finished is None or float(finished) > cutoff:
            bump("finished recently")
            continue
        job_root, reason = gc_lib.job_runs_root(home, job.get("id"), jobs_resolved=jobs_resolved)
        if job_root is None:
            if reason != "no runs folder":
                bump(reason)
            continue
        for run_dir in sorted(job_root.iterdir()):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            for path, st in _agentq_files(run_dir):
                rel = os.path.relpath(str(path), str(jobs_dir))
                if exclude and any(fnmatch.fnmatchcase(rel, g) for g in exclude):
                    bump("excluded")
                    continue
                entry = agentq_entry(path, job, home, st=st)
                if entry is not None:
                    entries.append(entry)
    entries.sort(key=lambda e: e.path)
    return entries, skipped


# ------------------------------------------------------------- liveness

def open_paths(paths) -> set[str]:
    """Paths some process holds open. Fails closed: lsof missing is an error.

    ``lsof`` scans every process whatever the argument count, so one call costs
    the same for 100 paths as for 2,000 (about 7 s on a busy laptop). Chunks are
    therefore as large as the argument limit allows: a 26,800-file agentq scan is
    14 calls, not 268.
    """
    paths = [os.path.realpath(p) for p in paths]
    found: set[str] = set()
    chunk: list[str] = []
    chunk_bytes = 0
    for path in paths + [None]:
        if path is not None:
            chunk.append(path)
            chunk_bytes += len(path) + 1
            if len(chunk) < 2000 and chunk_bytes < 200_000:
                continue
        if not chunk:
            continue
        try:
            proc = subprocess.run(
                ["lsof", "-Fn", "--"] + chunk,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise ArchiveError("lsof not found; cannot prove no open handle") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArchiveError("lsof timed out") from exc
        if proc.returncode not in (0, 1):
            raise ArchiveError("lsof failed (exit %d)" % proc.returncode)
        for line in proc.stdout.splitlines():
            if line.startswith("n"):
                found.add(line[1:])
        chunk = []
        chunk_bytes = 0
    return found


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def launcher_block(outdir: str) -> str | None:
    """Why the run folder is still live, or None.

    A ``running/<label>`` file whose pid is alive blocks the folder. One
    whose pid is gone is a launch that crashed before it cleaned up; it no
    longer blocks (the stream is finished), and the plan reports it.
    A ``pid`` file (claude ``out.jsonl`` runs) blocks while its pid lives.
    """
    rdir = os.path.join(outdir, "running")
    try:
        names = os.listdir(rdir)
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(rdir, name), "r", encoding="utf-8") as fh:
                head = fh.read(64).split("\t", 1)[0].strip()
            pid = int(head)
        except (OSError, ValueError):
            return "running/%s (unreadable)" % name
        if _pid_alive(pid):
            return "running/%s (live launcher)" % name
    pid_file = os.path.join(outdir, "pid")
    if os.path.isfile(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as fh:
                pid = int(fh.read(64).split()[0])
            if _pid_alive(pid):
                return "pid file (live)"
        except (OSError, ValueError, IndexError):
            return "pid file (unreadable)"
    return None


@dataclass
class Plan:
    entry: Entry
    action: str            # candidate | skip
    reason: str = ""
    stale_marker: bool = False


def _has_marker(outdir: str) -> bool:
    try:
        return bool(os.listdir(os.path.join(outdir, "running")))
    except OSError:
        return False


def plan_entries(entries, *, min_idle_hours: float, now: float, ws_scoped: bool = True) -> list[Plan]:
    """Idle, open-handle and launcher checks. Pure except for lsof and pid probes.

    With ``ws_scoped`` False (a hub-wide pass, no ``--ws``), a folder that has
    any ``running/`` marker is skipped, live or stale: a whole-hub pass must not
    reclaim a launch whose state it cannot judge. ``--ws`` names the folder and
    lifts that refusal. A live launcher is refused in both modes (launcher_block).
    """
    open_set = open_paths([e.path for e in entries]) if entries else set()
    plans = []
    cutoff = now - min_idle_hours * 3600.0
    for e in entries:
        if e.mtime > cutoff:
            plans.append(Plan(e, "skip", "not idle (%.1f h)" % ((now - e.mtime) / 3600.0)))
            continue
        if os.path.realpath(e.path) in open_set:
            plans.append(Plan(e, "skip", "open handle"))
            continue
        block = launcher_block(e.outdir)
        if block:
            plans.append(Plan(e, "skip", block))
            continue
        stale = os.path.isdir(os.path.join(e.outdir, "running"))
        if stale and not ws_scoped and _has_marker(e.outdir):
            plans.append(Plan(e, "skip", "running/ marker (pass --ws to reclaim)"))
            continue
        plans.append(Plan(e, "candidate", "", stale_marker=stale))
    return plans


# ------------------------------------------------------------- scanning

def _num(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


class Scan:
    """Line count and assistant token totals over a byte stream, streamed.

    Lines are counted by newline, plus one for a non-empty final line, so
    raw and stripped streams count the same way. Only ``message_end``
    lines are buffered (up to ``PARSE_CAP``); the rest are dropped as read.
    """

    def __init__(self, tok: str = DEFAULT_TOKEN_TYPE) -> None:
        self.tok = tok
        self.prefix = b'{"type":"%s"' % tok.encode("ascii")
        self.lines = 0
        self.bytes = 0
        self.messages = 0
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.unparsed = 0
        self._head = b""
        self._is_me = None
        self._body = None
        self._overflow = False
        self._cur = 0

    def feed(self, data: bytes) -> None:
        self.bytes += len(data)
        start = 0
        while True:
            nl = data.find(b"\n", start)
            if nl < 0:
                self._seg(data[start:])
                return
            self._seg(data[start:nl])
            self._end_line()
            start = nl + 1

    def finish(self) -> None:
        if self._cur > 0:
            self._end_line()

    def _seg(self, seg: bytes) -> None:
        self._cur += len(seg)
        if self._is_me is None:
            if len(self._head) < len(self.prefix):
                self._head += seg[: len(self.prefix) - len(self._head)]
            if len(self._head) >= len(self.prefix):
                self._is_me = self._head.startswith(self.prefix)
                if not self._is_me:
                    self._body = None
        if self._is_me is False or self._overflow:
            return
        if self._body is None:
            self._body = bytearray()
        if len(self._body) + len(seg) > PARSE_CAP:
            self._overflow = True
            self._body = None
            return
        self._body += seg

    def _end_line(self) -> None:
        self.lines += 1
        if self._is_me is None:
            self._is_me = self._head.startswith(self.prefix)
        if self._is_me:
            if self._overflow:
                self.unparsed += 1
            elif self._body is not None:
                self._tokens(bytes(self._body))
        self._head = b""
        self._is_me = None
        self._body = None
        self._overflow = False
        self._cur = 0

    def _tokens(self, body: bytes) -> None:
        try:
            obj = json.loads(body)
        except ValueError:
            self.unparsed += 1
            return
        if not isinstance(obj, dict) or obj.get("type") != self.tok:
            return
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        self.messages += 1
        self.input += _num(usage.get("input"))
        self.output += _num(usage.get("output"))
        self.cache_read += _num(usage.get("cacheRead"))

    def tokens(self) -> dict:
        return {
            "messages": self.messages,
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
        }


class _Drain(threading.Thread):
    """Reads compressor output into a file, or only counts it."""

    def __init__(self, src, dst_path: str | None) -> None:
        super().__init__(daemon=True)
        self.src = src
        self.count = 0
        self.dst = open(dst_path, "wb") if dst_path else None

    def run(self) -> None:
        try:
            while True:
                chunk = self.src.read(READ_CHUNK)
                if not chunk:
                    break
                self.count += len(chunk)
                if self.dst is not None:
                    self.dst.write(chunk)
        finally:
            self.src.close()
            if self.dst is not None:
                self.dst.close()


def _zstd_path() -> str:
    found = shutil.which("zstd")
    if not found:
        raise ArchiveError("zstd not found on PATH")
    return found


@dataclass
class Measure:
    raw_bytes: int = 0
    stripped_bytes: int = 0
    zst_bytes: int = 0
    replaced: int = 0
    replaced_bytes: int = 0
    raw_lines: int = 0
    stripped_lines: int = 0
    raw_tokens: dict = field(default_factory=dict)
    stripped_tokens: dict = field(default_factory=dict)
    raw_unparsed: int = 0
    stripped_unparsed: int = 0
    stripped_sha256: str = ""


def measure(path: str, zst_out: str | None, tok: str = DEFAULT_TOKEN_TYPE, compressed: bool = False) -> Measure:
    """One streaming pass: stripped zstd copy to ``zst_out`` (or counted only).

    ``compressed`` reads the file through gzip (``agentq.janitor`` gzips old
    streams); the copy and the counts are of the decompressed bytes.
    """
    flt = logfilter.StreamFilter()
    raw_scan, out_scan = Scan(tok), Scan(tok)
    digest = hashlib.sha256()
    zproc = subprocess.Popen(
        [_zstd_path(), ZSTD_LEVEL, ZSTD_LONG, "-q", "-c"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    drain = _Drain(zproc.stdout, zst_out)
    drain.start()
    try:
        with (gzip.open if compressed else open)(path, "rb") as fh:
            while True:
                chunk = fh.read(READ_CHUNK)
                if not chunk:
                    break
                raw_scan.feed(chunk)
                stripped = flt.feed(chunk)
                out_scan.feed(stripped)
                digest.update(stripped)
                zproc.stdin.write(stripped)
        tail = flt.finish()
        raw_scan.finish()
        out_scan.feed(tail)
        out_scan.finish()
        digest.update(tail)
        zproc.stdin.write(tail)
    finally:
        try:
            zproc.stdin.close()
        except OSError:
            pass
        rc = zproc.wait()
        drain.join()
    if rc != 0:
        raise ArchiveError("zstd exited %d" % rc)
    return Measure(
        raw_bytes=raw_scan.bytes,
        stripped_bytes=out_scan.bytes,
        zst_bytes=drain.count,
        replaced=flt.replaced,
        replaced_bytes=flt.replaced_bytes,
        raw_lines=raw_scan.lines,
        stripped_lines=out_scan.lines,
        raw_tokens=raw_scan.tokens(),
        stripped_tokens=out_scan.tokens(),
        raw_unparsed=raw_scan.unparsed,
        stripped_unparsed=out_scan.unparsed,
        stripped_sha256=digest.hexdigest(),
    )


def decompressed_sha256(zst_path: str) -> str:
    """sha256 of the zstd file's decompressed content, streamed."""
    proc = subprocess.Popen([_zstd_path(), "-d", ZSTD_LONG, "-q", "-c", zst_path], stdout=subprocess.PIPE)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = proc.stdout.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise ArchiveError("zstd -d exited %d for %s" % (rc, zst_path))
    return digest.hexdigest()


# ------------------------------------------------------------- VictoriaLogs

def _row_time(head: bytes, fallback_ms: int) -> str:
    m = T_EPOCH_RE.search(head)
    if m:
        ms = int(m.group(1))
    else:
        m2 = T_ISO_RE.search(head)
        if m2:
            text = m2.group(1).decode("ascii")
            try:
                import calendar

                base = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
                frac = text[20:-1]
                ms = base * 1000 + (int(frac[:3].ljust(3, "0")) if frac else 0)
            except ValueError:
                ms = fallback_ms
        else:
            ms = fallback_ms
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)


def _iter_stored_lines(zst_path: str):
    """Yield ``(prefix, total_len)`` for each line of the decompressed copy.

    ``prefix`` is the line up to ``MAX_MSG_BYTES`` (the stored row text);
    ``total_len`` is the whole line length. The final line without a
    newline is yielded too, as in ``Scan``.
    """
    proc = subprocess.Popen([_zstd_path(), "-d", ZSTD_LONG, "-q", "-c", zst_path], stdout=subprocess.PIPE)
    cap = vlogs.MAX_MSG_BYTES
    prefix = bytearray()
    total = 0
    try:
        while True:
            chunk = proc.stdout.read(READ_CHUNK)
            if not chunk:
                break
            start = 0
            while True:
                nl = chunk.find(b"\n", start)
                seg = chunk[start:] if nl < 0 else chunk[start:nl]
                if len(prefix) < cap + 1:
                    prefix += seg[: cap + 1 - len(prefix)]
                total += len(seg)
                if nl < 0:
                    break
                yield bytes(prefix), total
                prefix = bytearray()
                total = 0
                start = nl + 1
        if total > 0:
            yield bytes(prefix), total
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise ArchiveError("zstd -d exited %d for %s" % (rc, zst_path))


def row_lines(entry: Entry, zst_path: str):
    """Rows for one archived file, plus the token sums of the rows sent.

    Yields ``(line_bytes, accounting)``; ``accounting`` is a dict updated
    as each row is built. Token sums only count rows kept whole, because a
    truncated row is not valid JSON for the store to unpack.
    """
    fallback_ms = int(entry.mtime * 1000)
    tok = token_type(entry.kind).encode("ascii")
    acct = {"rows": 0, "truncated": 0, "redactions": 0, "messages": 0, "input": 0, "output": 0, "cache_read": 0}
    for prefix, total in _iter_stored_lines(zst_path):
        head = prefix[:400]
        m = HEAD_RE.match(prefix)
        type_ = m.group(1).decode("ascii", "replace") if m else "?"
        text = prefix.decode("utf-8", "replace")
        whole = total <= vlogs.MAX_MSG_BYTES
        if whole and type_ == tok.decode("ascii") and b'"role":"assistant"' in prefix:
            try:
                obj = json.loads(prefix)
                message = obj.get("message") if isinstance(obj, dict) else None
                if isinstance(message, dict) and message.get("role") == "assistant":
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    acct["messages"] += 1
                    acct["input"] += _num(usage.get("input"))
                    acct["output"] += _num(usage.get("output"))
                    acct["cache_read"] += _num(usage.get("cacheRead"))
            except ValueError:
                pass
        line, truncated, redactions = vlogs.make_row(
            time_iso=_row_time(head, fallback_ms),
            msg=text,
            ws=entry.ws,
            label=entry.label,
            type_=type_,
            kind=entry.kind,
            path=entry.rel,
            extra=entry.extra or None,
        )
        acct["rows"] += 1
        acct["truncated"] += 1 if truncated else 0
        acct["redactions"] += redactions
        yield line, acct


def _store_tokens(client: vlogs.VLogsClient, rel: str, tok: str) -> dict:
    q = (
        'path:="%s" type:=%s '
        '| unpack_json from _msg fields ("message.role","message.usage.input","message.usage.output","message.usage.cacheRead") '
        '| filter "message.role":=assistant '
        '| stats count() n, sum("message.usage.input") i, sum("message.usage.output") o, sum("message.usage.cacheRead") c'
    ) % (rel, tok)
    rows = client.query(q)
    r = rows[0] if rows else {}
    return {
        "messages": _as_int(r.get("n")),
        "input": _as_int(r.get("i")),
        "output": _as_int(r.get("o")),
        "cache_read": _as_int(r.get("c")),
    }


def _as_int(value) -> int:
    """A LogsQL sum: an empty set returns NaN, which is zero here."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number != number:  # NaN
        return 0
    return int(number)


def ingest(entry: Entry, zst_path: str, lines: int, client: vlogs.VLogsClient) -> dict:
    """Post the rows of one archived file once, and verify the store. Returns the result."""
    before = client.count(entry.rel)
    if before == lines:
        sent_acct = None
        status = "already-ingested"
    elif before == 0:
        gen_acct = {}

        def gen():
            nonlocal gen_acct
            for line, acct in row_lines(entry, zst_path):
                gen_acct = acct
                yield line

        sent = client.insert(gen())
        sent_acct = dict(gen_acct)
        status = "ingested"
        if sent != lines:
            raise ArchiveError("sent %d rows for %d lines" % (sent, lines))
        client.flush()
    else:
        raise ArchiveError(
            "store has %d rows for %s, expected 0 or %d; remove them before retrying" % (before, entry.rel, lines)
        )
    stored = client.count(entry.rel)
    if stored != lines:
        raise ArchiveError("store has %d rows for %s, expected %d" % (stored, entry.rel, lines))
    if sent_acct is None:
        sent_acct = _sent_tokens(entry, zst_path)
    got = _store_tokens(client, entry.rel, token_type(entry.kind))
    want = {"messages": sent_acct["messages"], "input": sent_acct["input"], "output": sent_acct["output"], "cache_read": sent_acct["cache_read"]}
    if got != want:
        raise ArchiveError("store token sums %s differ from sent %s" % (got, want))
    return {
        "status": status,
        "vl_rows": stored,
        "vl_tokens": got,
        "truncated": sent_acct["truncated"],
        "redactions": sent_acct["redactions"],
    }


def _sent_tokens(entry: Entry, zst_path: str) -> dict:
    acct = {}
    for _line, acct in row_lines(entry, zst_path):
        pass
    return dict(acct) if acct else {"messages": 0, "input": 0, "output": 0, "cache_read": 0, "truncated": 0, "redactions": 0}


# ------------------------------------------------------------- manifest

def archive_root(state: Path | None = None) -> Path:
    base = Path(state) if state is not None else Path(_state_dir())
    return base / "log-archive"


def zst_path_for(root: Path, entry: Entry) -> Path:
    return root / (entry.rel + ".zst")


class Manifest:
    """Append-only JSON lines; the last record for a path wins."""

    def __init__(self, root: Path) -> None:
        self.path = root / "manifest.jsonl"
        self.latest: dict[str, dict] = {}
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("path"):
                        self.latest[rec["path"]] = rec

    def write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(rec, ts=round(time.time(), 3))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.latest[rec["path"]] = rec


def _same_file(rec: dict | None, entry: Entry) -> bool:
    return bool(rec) and rec.get("size") == entry.size and rec.get("mtime_ns") == entry.mtime_ns


# ------------------------------------------------------------- reclaim

def reclaim(entry: Entry, zst: Path, keep: str, manifest_rec: dict) -> None:
    """Replace or remove the raw file. Re-checks the file is unchanged and closed."""
    st = os.stat(entry.path)
    if st.st_size != entry.size or st.st_mtime_ns != entry.mtime_ns:
        raise ArchiveError("file changed since measured")
    if os.path.realpath(entry.path) in open_paths([entry.path]):
        raise ArchiveError("file has an open handle now")
    if keep == "remove":
        os.remove(entry.path)
        return
    tmp = entry.path + ".archive-tmp"
    if keep == "stub":
        body = json.dumps(
            {
                "type": "archived",
                "archive": "log-archive/" + entry.rel + ".zst",
                "sha256": manifest_rec.get("stripped_sha256"),
                "lines": manifest_rec.get("stripped_lines"),
            },
            separators=(",", ":"),
        ) + "\n"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
    elif keep == "stripped":
        proc = subprocess.Popen([_zstd_path(), "-d", ZSTD_LONG, "-q", "-c", str(zst)], stdout=subprocess.PIPE)
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(proc.stdout, fh)
        proc.stdout.close()
        if proc.wait() != 0:
            raise ArchiveError("zstd -d failed for stripped rewrite")
    else:
        raise ArchiveError("unknown keep mode %r" % keep)
    os.replace(tmp, entry.path)


# ------------------------------------------------------------- driver

@dataclass
class Totals:
    files: int = 0
    raw: int = 0
    stripped: int = 0
    zst: int = 0
    reclaimable: int = 0
    #: Per tool: files, raw bytes, bytes reclaimed, and the keep applied.
    by_tool: dict = field(default_factory=dict)
    rows: int = 0
    replaced: int = 0
    truncated: int = 0
    redactions: int = 0
    #: Assistant token sums over the raw files, and whether every file's
    #: stripped sums equalled them (the regression check).
    tokens: dict = field(default_factory=lambda: {"messages": 0, "input": 0, "output": 0, "cache_read": 0})
    #: The same sums split by kind. A pi session file repeats the stream's
    #: assistant messages, so only the stream kinds add up to a run's usage.
    tokens_by_kind: dict = field(default_factory=dict)
    tokens_match: bool = True


def _fmt(n: int) -> str:
    return "%.2f GB" % (n / 1e9)


def process(
    plans: list[Plan],
    *,
    apply: bool,
    keep: str,
    client: vlogs.VLogsClient | None,
    root: Path,
    limit: int | None,
    out,
) -> tuple[Totals, dict]:
    """Run the pipeline over candidate plans. Dry run measures only."""
    totals = Totals()
    results = {"archived": 0, "reclaimed": 0, "done": 0, "errors": 0, "planned": 0, "skipped": {}}
    manifest = Manifest(root) if apply else None
    done_paths = manifest.latest if manifest else {}
    candidates = [p for p in plans if p.action == "candidate"]
    for p in plans:
        if p.action == "skip":
            key = p.reason.split(" (")[0]
            results["skipped"][key] = results["skipped"].get(key, 0) + 1
    if limit is not None:
        candidates = candidates[:limit]
    for plan in candidates:
        e = plan.entry
        prior = done_paths.get(e.rel)
        zst = zst_path_for(root, e)
        if apply and prior and _same_file(prior, e) and prior.get("status") == "reclaimed":
            results["done"] += 1
            continue
        if not apply:
            m = measure(e.path, None, token_type(e.kind), compressed=e.compressed)
            _count(totals, e, m, keep=keep_for(e, keep))
            results["planned"] += 1
            print("  would archive %s  raw %s  stripped %s  lines %d  replaced %d" % (e.rel, _fmt(m.raw_bytes), _fmt(m.stripped_bytes), m.stripped_lines, m.replaced), file=out)
            continue
        try:
            zst.parent.mkdir(parents=True, exist_ok=True)
            if prior and _same_file(prior, e) and prior.get("status") == "archived" and zst.exists():
                rec = prior
            else:
                tmp_zst = str(zst) + ".tmp"
                m = measure(e.path, tmp_zst, token_type(e.kind), compressed=e.compressed)
                _verify_measure(m)
                if decompressed_sha256(tmp_zst) != m.stripped_sha256:
                    raise ArchiveError("zstd copy does not decompress to the stripped bytes")
                os.replace(tmp_zst, zst)
                rec = {
                    "path": e.rel,
                    "source": e.path,
                    "ws": e.ws,
                    "label": e.label,
                    "kind": e.kind,
                    "size": e.size,
                    "mtime_ns": e.mtime_ns,
                    "status": "measured",
                    "raw_bytes": m.raw_bytes,
                    "stripped_bytes": m.stripped_bytes,
                    "zst_bytes": m.zst_bytes,
                    "replaced": m.replaced,
                    "raw_lines": m.raw_lines,
                    "stripped_lines": m.stripped_lines,
                    "stripped_sha256": m.stripped_sha256,
                    "raw_tokens": m.raw_tokens,
                    "stripped_tokens": m.stripped_tokens,
                }
                manifest.write(rec)
            ing = ingest(e, str(zst), rec["stripped_lines"], client)
            rec = dict(rec, status="archived", vl_rows=ing["vl_rows"], vl_tokens=ing["vl_tokens"], truncated=ing["truncated"], redactions=ing["redactions"], ingest=ing["status"])
            manifest.write(rec)
            results["archived"] += 1
            k = keep_for(e, keep)
            _count(totals, e, _Measured(rec), keep=k)
            reclaim(e, zst, k, rec)
            done_rec = dict(rec, status="reclaimed", keep=k)
            if k != "remove" and os.path.exists(e.path):
                # The file was rewritten in place: record its new identity so the next
                # pass skips it instead of archiving the stripped copy again.
                st = os.stat(e.path)
                done_rec.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
            manifest.write(done_rec)
            results["reclaimed"] += 1
            print("  archived %s  raw %s -> zst %s  rows %d (%s)  reclaimed" % (e.rel, _fmt(e.size), _fmt(rec["zst_bytes"]), rec["vl_rows"], ing["status"]), file=out)
        except Exception as exc:  # one file never stops the run; the manifest keeps the reason
            results["errors"] += 1
            reason = "%s: %s" % (exc.__class__.__name__, exc)
            if manifest:
                manifest.write({"path": e.rel, "source": e.path, "size": e.size, "mtime_ns": e.mtime_ns, "status": "error", "error": reason[:500]})
            print("  ERROR %s: %s" % (e.rel, reason), file=out)
    return totals, results


class _Measured:
    """Adapter so manifest records and fresh measures count the same way."""

    def __init__(self, rec: dict) -> None:
        self.raw_bytes = rec["raw_bytes"]
        self.stripped_bytes = rec["stripped_bytes"]
        self.zst_bytes = rec["zst_bytes"]
        self.replaced = rec.get("replaced", 0)
        self.stripped_lines = rec["stripped_lines"]
        self.raw_tokens = rec.get("raw_tokens")
        self.stripped_tokens = rec.get("stripped_tokens")


def _verify_measure(m: Measure) -> None:
    if m.raw_unparsed or m.stripped_unparsed:
        raise ArchiveError("unparsed message lines (raw %d, stripped %d)" % (m.raw_unparsed, m.stripped_unparsed))
    if m.raw_lines != m.stripped_lines:
        raise ArchiveError("line count differs: raw %d stripped %d" % (m.raw_lines, m.stripped_lines))
    if m.raw_tokens != m.stripped_tokens:
        raise ArchiveError("token totals differ: raw %s stripped %s" % (m.raw_tokens, m.stripped_tokens))


def _count(totals: Totals, e: Entry, m, *, keep: str | None = None) -> None:
    """Add one file to the totals. ``keep`` is the resolved keep (None: not reclaimed)."""
    totals.files += 1
    totals.raw += m.raw_bytes
    totals.stripped += m.stripped_bytes
    totals.zst += m.zst_bytes
    totals.replaced += m.replaced
    raw_tok = getattr(m, "raw_tokens", None)
    if raw_tok:
        bucket = totals.tokens_by_kind.setdefault(e.kind, {"messages": 0, "input": 0, "output": 0, "cache_read": 0})
        for key in ("messages", "input", "output", "cache_read"):
            bucket[key] += raw_tok.get(key, 0)
            # A session file repeats the stream's assistant messages: never add it to the total.
            if e.kind != "session":
                totals.tokens[key] += raw_tok.get(key, 0)
    if getattr(m, "raw_tokens", None) is not None and m.raw_tokens != m.stripped_tokens:
        totals.tokens_match = False
    if keep:
        gone = reclaim_bytes(e, m, keep)
        totals.reclaimable += gone
        row = totals.by_tool.setdefault(e.tool, {"files": 0, "raw": 0, "reclaim": 0, "keep": keep})
        row["files"] += 1
        row["raw"] += m.raw_bytes
        row["reclaim"] += gone
        row["keep"] = keep if row["keep"] == keep else "mixed"


def print_totals(totals: Totals, plans: list[Plan], out) -> None:
    skipped = {}
    for p in plans:
        if p.action == "skip":
            key = p.reason.split(" (")[0]
            skipped[key] = skipped.get(key, 0) + 1
    print("", file=out)
    print("files discovered %d, candidates %d, skipped %d %s" % (
        len(plans),
        sum(1 for p in plans if p.action == "candidate"),
        sum(1 for p in plans if p.action == "skip"),
        skipped if skipped else "",
    ), file=out)
    print("totals (%d files):" % totals.files, file=out)
    print("  raw        %s  (%d bytes)" % (_fmt(totals.raw), totals.raw), file=out)
    print("  stripped   %s  (%d bytes)" % (_fmt(totals.stripped), totals.stripped), file=out)
    print("  zstd %-5s %s  (%d bytes)" % (ZSTD_LEVEL, _fmt(totals.zst), totals.zst), file=out)
    print("  reclaimable %s  (%d bytes)" % (_fmt(totals.reclaimable), totals.reclaimable), file=out)
    for tool in sorted(totals.by_tool):
        row = totals.by_tool[tool]
        print("  tool %-11s keep %-8s files %5d  raw %s  reclaim %s" % (tool, row["keep"], row["files"], _fmt(row["raw"]), _fmt(row["reclaim"])), file=out)
    t = totals.tokens
    print(
        "assistant tokens (raw, stripped equal: %s): messages %d  input %d  output %d  cacheRead %d"
        % ("yes" if totals.tokens_match else "NO", t["messages"], t["input"], t["output"], t["cache_read"]),
        file=out,
    )
    for kind in sorted(totals.tokens_by_kind):
        k = totals.tokens_by_kind[kind]
        print(
            "  kind %-8s messages %d  input %d  output %d  cacheRead %d"
            % (kind, k["messages"], k["input"], k["output"], k["cache_read"]),
            file=out,
        )


def print_census(entries, out) -> None:
    """Files and bytes discovered, by kind, before any measurement (cheap: stat only)."""
    by_kind: dict = {}
    for e in entries:
        row = by_kind.setdefault(e.kind, {"files": 0, "bytes": 0, "gzip": 0})
        row["files"] += 1
        row["bytes"] += e.size
        row["gzip"] += 1 if e.compressed else 0
    print("discovered %d files, %s on disk" % (len(entries), _fmt(sum(e.size for e in entries))), file=out)
    for kind in sorted(by_kind):
        row = by_kind[kind]
        print(
            "  kind %-8s files %6d  %s  (%d gzip)" % (kind, row["files"], _fmt(row["bytes"]), row["gzip"]),
            file=out,
        )


def archive_status(entries, *, state: Path | None = None) -> dict:
    """``{rel: status}`` from the manifest: ``reclaimed``, ``archived``, ``error`` or ``none``.

    ``archived`` means a verified zstd copy exists and its rows are in the store;
    ``reclaimed`` means the path already holds the archive's own copy. Anything
    else (no record, a record for a different size or mtime, an error) is
    ``none``: the file has no verified archive and must not be deleted.
    """
    manifest = Manifest(archive_root(state))
    found: dict = {}
    for e in entries:
        rec = manifest.latest.get(e.rel)
        if not rec:
            found[e.rel] = "none"
        elif rec.get("status") in ("archived", "reclaimed") and _same_file(rec, e):
            found[e.rel] = rec["status"]
        elif rec.get("status") == "error":
            found[e.rel] = "error"
        else:
            found[e.rel] = "none"
    return found


def archive_entries(
    entries,
    *,
    apply: bool,
    keep: str | None = None,
    url: str | None = None,
    out=None,
    state: Path | None = None,
    min_idle_hours: float = 0.0,
    now: float | None = None,
    ws_scoped: bool = True,
    limit: int | None = None,
    client=None,
) -> tuple[int, Totals, dict, list[Plan]]:
    """Plan, then measure/verify/ingest/reclaim a known set of entries.

    The shared body of ``logs archive`` and ``agentq gc``: takes the archive
    lock, checks the store, and returns ``(exit_code, totals, results, plans)``. A file
    is only ever reclaimed by ``process`` after its zstd copy verified and its
    rows are in the store, so a caller cannot delete an un-archived log through
    this path. ``client`` is injectable for tests.
    """
    out = out or sys.stdout
    root = archive_root(state)
    now = time.time() if now is None else float(now)
    lock_fh = None
    if apply:
        # Only a real run takes the lock; a dry run writes nothing at all.
        root.mkdir(parents=True, exist_ok=True)
        lock_fh = open(root / "archive.lock", "a+")
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fh.close()
            print("another logs archive run holds %s" % (root / "archive.lock"), file=sys.stderr)
            return 3, Totals(), {"errors": 0, "lock": "held"}, []
    try:
        if apply and client is None:
            client = vlogs.VLogsClient(url or vlogs.DEFAULT_URL)
            if not client.healthy():
                print(
                    "VictoriaLogs is not reachable at %s; start it: sh scripts/vlogs_install.sh"
                    % (url or vlogs.DEFAULT_URL),
                    file=sys.stderr,
                )
                return 5, Totals(), {"errors": 0, "store": "unreachable"}, []
        # Drop what the manifest already reclaimed, before the liveness probe: a
        # caught-up pass then costs one lsof per new file, not one per file ever
        # archived (26,779 agentq files is 268 lsof calls, about two minutes).
        done = 0
        if entries:
            latest = Manifest(root).latest
            kept = []
            for e in entries:
                rec = latest.get(e.rel)
                if rec and rec.get("status") == "reclaimed" and _same_file(rec, e):
                    done += 1
                    continue
                kept.append(e)
            entries = kept
        plans = plan_entries(entries, min_idle_hours=min_idle_hours, now=now, ws_scoped=ws_scoped)
        totals, results = process(
            plans, apply=apply, keep=keep, client=client, root=root, limit=limit, out=out
        )
        results["done"] = results.get("done", 0) + done
        return (1 if results["errors"] else 0), totals, results, plans
    finally:
        if lock_fh is not None:
            lock_fh.close()


def main(argv=None, out=None) -> int:
    """``agent_dash.py logs archive`` entry. Returns the exit code."""
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    opts = parse_args(args)
    if isinstance(opts, str):
        print(opts, file=sys.stderr)
        return 2
    return run(opts, out=out)


def run(opts: dict, out=None, *, state: Path | None = None) -> int:
    """Run one archive pass. ``opts`` has the keys ``parse_args`` returns.

    The entry point for a ``ws archive`` hook: pass ``ws=<id>`` to archive
    one workstream's logs, ``apply=True`` to write. Dry run is the default.
    ``agentq=True`` adds the agentq job folders as a second source (and needs no
    ``log_archive.roots``). Returns the exit code (0 ok, 1 failures or zero
    planned on real input, 2 bad options, 3 another run holds the lock, 5 store
    unreachable).
    """
    out = out or sys.stdout
    opts = dict(_DEFAULTS, **opts)
    roots = opts["roots"] or ([] if opts["agentq"] else default_roots())
    if not roots and not opts["agentq"]:
        print("no hub roots: pass --root <hub> or set log_archive.roots in <state>/config.local.json", file=sys.stderr)
        return 2
    now = time.time()
    entries: list[Entry] = []
    skipped_meta: dict = {}
    if roots:
        hub_entries, hub_skipped = discover(roots, ws=opts["ws"], exclude=opts["exclude"])
        entries.extend(hub_entries)
        for key, count in hub_skipped.items():
            skipped_meta[key] = skipped_meta.get(key, 0) + count
    if opts["agentq"]:
        home = agentq_home()
        aq_entries, aq_skipped = discover_agentq(
            home, agentq_jobs(home), min_idle_hours=opts["min_idle"], now=now, exclude=opts["exclude"]
        )
        entries.extend(aq_entries)
        for key, count in aq_skipped.items():
            skipped_meta[key] = skipped_meta.get(key, 0) + count
    print(
        "mode: %s   keep: %s   min-idle: %g h   roots: %d   agentq: %s"
        % (
            "apply" if opts["apply"] else "dry-run",
            opts["keep"] or "per tool",
            opts["min_idle"],
            len(roots),
            "yes" if opts["agentq"] else "no",
        ),
        file=out,
    )
    for key, count in sorted(skipped_meta.items()):
        print("ignored %s: %d files" % (key, count), file=out)
    print_census(entries, out)
    rc, totals, results, plans = archive_entries(
        entries,
        apply=opts["apply"],
        keep=opts["keep"],
        url=opts["url"],
        out=out,
        state=state,
        min_idle_hours=opts["min_idle"],
        now=now,
        ws_scoped=opts["ws"] is not None,
        limit=opts["limit"],
    )
    if rc in (3, 5):
        return rc
    print_totals(totals, plans, out)
    print("results: %s" % json.dumps(results, sort_keys=True), file=out)
    if not opts["apply"]:
        print("dry run: nothing written. Re-run with --apply to archive.", file=out)
        if entries and results["planned"] == 0 and not opts["scheduled"]:
            print("dry run planned zero actions on real input", file=sys.stderr)
            return 1
        return 0
    return rc


_DEFAULTS = {
    "roots": [],
    "ws": None,
    "min_idle": MIN_IDLE_HOURS,
    "apply": False,
    "keep": DEFAULT_KEEP,
    "url": vlogs.DEFAULT_URL,
    "limit": None,
    "exclude": (),
    "agentq": False,
    "scheduled": False,
}


def parse_args(args: list[str]):
    opts = dict(_DEFAULTS, roots=[], exclude=[])
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--apply",):
            opts["apply"] = True
        elif a in ("--agentq",):
            opts["agentq"] = True
        elif a in ("--scheduled",):
            opts["scheduled"] = True
        elif a in ("--root", "--ws", "--min-idle-hours", "--keep", "--vlogs-url", "--limit", "--exclude"):
            if i + 1 >= len(args):
                return "%s needs a value" % a
            val = args[i + 1]
            i += 1
            if a == "--root":
                opts["roots"].append(val)
            elif a == "--exclude":
                opts["exclude"].append(val)
            elif a == "--ws":
                opts["ws"] = val
            elif a == "--min-idle-hours":
                try:
                    opts["min_idle"] = float(val)
                except ValueError:
                    return "--min-idle-hours needs a number"
            elif a == "--keep":
                if val not in KEEP_CHOICES:
                    return "--keep must be one of %s" % ", ".join(KEEP_CHOICES)
                opts["keep"] = val
            elif a == "--vlogs-url":
                opts["url"] = val
            elif a == "--limit":
                try:
                    opts["limit"] = int(val)
                except ValueError:
                    return "--limit needs an integer"
        else:
            return "unknown logs archive argument: %s" % a
        i += 1
    return opts
