"""Native agent-session archive: lossless ``tar.zst``, VictoriaLogs rows, reclaim.

The laptop holds the native session history of several coding agents
(Claude Code, Codex, Grok build, Cursor, Command Code, pi, muse). This
module packs each finished *unit* of that history into a lossless
``tar.zst`` under ``<dest>/archive/<tool>/<unit>.tar.zst``, ingests its
transcript lines into the loopback VictoriaLogs store for analytics, and,
after a longer idle window, deletes the original once the archive has been
re-verified. Any unit can be restored byte-for-byte.

Unlike ``dash.logarchive`` (which strips base64 images), this archive is
lossless: the tar holds the original bytes, and the store row text is the
original line. The two share the manifest/lock conventions, the
``open_paths`` liveness probe and the zstd helpers.

Pieces:

* ``TOOLS`` is a data registry: one entry per tool with its root and a
  discovery function. A root that does not exist is reported ``absent``.
* ``pack`` streams a PAX tar of the unit's members into ``zstd -12
  --long=27 -T2`` and hashes every member while reading, so a 500 MB
  member costs bounded memory. Archives are versioned
  (``<key>.<seq>.tar.zst``) and a pack never replaces an existing file.
* ``verify_archive`` streams the copy back through ``tarfile`` and compares
  every member's sha256 and size before the atomic rename.
* ``ingest_unit`` is at-least-once: every row carries ``line``, verification
  is ``count_uniq(member, line)``, and a retry resends from the last
  confirmed line. A rewrite bumps ``rev``.
* ``reclaim_unit`` is a journaled two-phase move: preconditions, a
  ``reclaiming`` manifest record, rename into ``state/trash``, re-hash, then
  delete the trash. ``recover_trash`` resolves a crash at any step.
* ``restore`` extracts to a temp dir, accepts only manifest members as
  regular files, verifies every hash, and refuses to overwrite.

Entry points: ``main(argv)`` (the CLI, ``agent_dash.py logs sessions``) and
``run(opts)`` (for tests and a later hook).
"""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from dash import logarchive, vlogs
from dash.dataroot import state_dir as _state_dir

DEFAULT_DEST = "~/Documents/data/agent-sessions"
DEFAULT_MIN_IDLE_HOURS = 6.0
DEFAULT_RECLAIM_DAYS = 14.0
DEFAULT_MAX_MINUTES = 90.0
#: Warn (but still run) when the dest volume has less free space than this.
DISK_WARN_GB = 30.0
ZSTD_LEVEL = "-12"
ZSTD_LONG = "--long=27"
ZSTD_THREADS = "-T2"
READ_CHUNK = 1 << 20
#: A line longer than this is truncated to ``vlogs.MAX_MSG_BYTES`` and the
#: rest is discarded while streaming, so RSS stays bounded.
MAX_LINE_BYTES = 64 << 20
CONFIG_KEYS = ("dest", "min_idle_hours", "reclaim_days", "reclaim_days_by_tool", "max_minutes", "tools")
#: Directories that are never part of a unit, per tool.
CLAUDE_SKIP_DIRS = ("memory",)
#: Out-of-scope trees, measured in the census but never packed or reclaimed.
OUT_OF_SCOPE = (
    ("muse views", "~/.local/share/muse/sessions/.msp-view-*"),
    ("grok worktrees", "~/.grok/worktrees"),
)
#: Live append-only histories: ingested from a stored byte offset, never packed.
LIVE_HISTORIES = (
    ("claude", "~/.claude/history.jsonl"),
    ("codex", "~/.codex/history.jsonl"),
    ("commandcode", "~/.commandcode/history.jsonl"),
    ("grok", "~/.grok/sessions/*/prompt_history.jsonl"),
)


class ArchiveError(RuntimeError):
    """A step failed in a way that must stop this unit (not the whole pass)."""


# --------------------------------------------------------------- config

def load_config(state: Path | None = None) -> dict:
    """``log_archive.sessions`` from the agent-dash local config, with defaults.

    ``SESSION_ARCHIVE_DEST`` overrides ``dest`` (used by tests and by a
    read-only census that must not create anything under the real dest).
    """
    base = Path(state) if state is not None else Path(_state_dir())
    local: dict = {}
    try:
        with open(base / "config.local.json", encoding="utf-8") as fh:
            local = json.load(fh)
    except (OSError, ValueError):
        local = {}
    section: dict = {}
    if isinstance(local, dict):
        la = local.get("log_archive")
        if isinstance(la, dict) and isinstance(la.get("sessions"), dict):
            section = la["sessions"]
    cfg = {
        "dest": DEFAULT_DEST,
        "min_idle_hours": DEFAULT_MIN_IDLE_HOURS,
        "reclaim_days": DEFAULT_RECLAIM_DAYS,
        "reclaim_days_by_tool": {},
        "max_minutes": DEFAULT_MAX_MINUTES,
        "tools": None,
    }
    for key in CONFIG_KEYS:
        if key in section and section[key] is not None:
            cfg[key] = section[key]
    env = os.environ.get("SESSION_ARCHIVE_DEST")
    if env:
        cfg["dest"] = env
    cfg["dest"] = os.path.expanduser(str(cfg["dest"]))
    if not isinstance(cfg["reclaim_days_by_tool"], dict):
        cfg["reclaim_days_by_tool"] = {}
    return cfg


# --------------------------------------------------------------- time

def parse_iso_ms(text) -> int | None:
    """Milliseconds from an ISO-8601 timestamp, or None. Offsets are ignored."""
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        base = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    frac = 0
    if len(text) > 20 and text[19] == ".":
        j = 20
        while j < len(text) and text[j].isdigit():
            j += 1
        frac = int((text[20:j] + "000")[:3])
    return base * 1000 + frac


def epoch_ms(value) -> int | None:
    """Milliseconds from a numeric epoch in seconds, ms, us or ns."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if v <= 0:
        return None
    if v > 1e17:
        return int(v / 1e6)
    if v > 1e14:
        return int(v / 1e3)
    if v > 1e11:
        return int(v)
    return int(v * 1000)


def iso_from_ms(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)


# --------------------------------------------------------------- units

@dataclass
class Member:
    path: str
    rel: str          # relative to the unit's parent dir (the extract target)
    rows: str = "none"  # jsonl | log | cursor-store | none
    size: int = 0
    mtime_ns: int = 0


@dataclass
class Unit:
    tool: str
    key: str          # relative to the tool root
    root: str
    parent: str       # dir the archive extracts into
    session: str
    project: str = ""
    members: list = field(default_factory=list)
    dirs: list = field(default_factory=list)
    is_dir: bool = False
    unit_dir: str = ""
    newest_mtime: float = 0.0
    raw_bytes: int = 0

    @property
    def uid(self) -> str:
        return "%s/%s" % (self.tool, self.key)


def _isdir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _walk_files(base: str, skip_dirs=()):
    """Regular files under ``base`` (never following symlinks), as (abs, rel)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames if d not in skip_dirs and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            out.append((path, os.path.relpath(path, base)))
    return out


def _walk_dirs(base: str, skip_dirs=()):
    out = []
    for dirpath, dirnames, _filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames if d not in skip_dirs and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in dirnames:
            out.append(os.path.relpath(os.path.join(dirpath, name), base))
    return out


def _finish(unit: Unit) -> Unit:
    newest = 0.0
    raw = 0
    for member in unit.members:
        try:
            st = os.stat(member.path)
        except OSError:
            continue
        member.size = st.st_size
        member.mtime_ns = st.st_mtime_ns
        raw += st.st_size
        newest = max(newest, st.st_mtime)
    unit.raw_bytes = raw
    unit.newest_mtime = newest
    return unit


def _rel(*parts: str) -> str:
    return "/".join(p.replace(os.sep, "/") for p in parts if p)


# --------------------------------------------------------------- discovery

def discover_claude(root: str) -> list[Unit]:
    units = []
    if not _isdir(root):
        return units
    for proj in sorted(os.listdir(root)):
        pdir = os.path.join(root, proj)
        if not _isdir(pdir):
            continue
        for name in sorted(os.listdir(pdir)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            uuid = name[: -len(".jsonl")]
            members = [Member(path, name, "jsonl")]
            dirs: list[str] = []
            sib = os.path.join(pdir, uuid)
            if _isdir(sib):
                for p, rel in _walk_files(sib, skip_dirs=CLAUDE_SKIP_DIRS):
                    rows = "jsonl" if rel.startswith("subagents" + os.sep) and rel.endswith(".jsonl") else "none"
                    members.append(Member(p, _rel(uuid, rel), rows))
                for d in _walk_dirs(sib, skip_dirs=CLAUDE_SKIP_DIRS):
                    dirs.append(_rel(uuid, d))
            units.append(
                _finish(
                    Unit(
                        "claude",
                        _rel(proj, uuid),
                        root,
                        pdir,
                        uuid,
                        members=members,
                        dirs=dirs,
                        is_dir=_isdir(sib),
                        unit_dir=sib,
                    )
                )
            )
    return units


_CODEX_RE = re.compile(
    r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?$"
)


def discover_codex(root: str) -> list[Unit]:
    """Exactly the specified layout, no recursion under ``archived_sessions``.

    ``sessions/YYYY/MM/DD/rollout-*.jsonl``, the same with ``.zst``, and
    ``archived_sessions/rollout-*.jsonl.zst``. Anything else under the root
    (nested or uncompressed archived files, sqlite stores) is not a unit.
    """
    import glob

    units = []
    if not _isdir(root):
        return units
    patterns = []
    sessions = os.path.join(root, "sessions")
    if _isdir(sessions):
        day = os.path.join(sessions, "[0-9][0-9][0-9][0-9]", "[0-9][0-9]", "[0-9][0-9]")
        patterns.append(("sessions", os.path.join(day, "rollout-*.jsonl")))
        patterns.append(("sessions", os.path.join(day, "rollout-*.jsonl.zst")))
    archived = os.path.join(root, "archived_sessions")
    if _isdir(archived):
        patterns.append(("archived_sessions", os.path.join(archived, "rollout-*.jsonl.zst")))
    for sub, pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            name = os.path.basename(path)
            match = _CODEX_RE.match(name)
            session = match.group(1) if match else name
            units.append(
                _finish(
                    Unit(
                        "codex",
                        _rel(os.path.relpath(path, root)),
                        root,
                        os.path.dirname(path),
                        session,
                        members=[Member(path, name, "jsonl")],
                    )
                )
            )
    return units


def discover_grok(root: str) -> list[Unit]:
    units = []
    if not _isdir(root):
        return units
    for enc in sorted(os.listdir(root)):
        edir = os.path.join(root, enc)
        if not _isdir(edir):
            continue
        for sid in sorted(os.listdir(edir)):
            sdir = os.path.join(edir, sid)
            if not _isdir(sdir):
                continue
            members = []
            for path, rel in _walk_files(sdir):
                rows = "jsonl" if rel.endswith(".jsonl") else ("log" if rel.endswith(".log") else "none")
                members.append(Member(path, _rel(sid, rel), rows))
            if not members:
                continue
            dirs = [_rel(sid, d) for d in _walk_dirs(sdir)]
            units.append(
                _finish(
                    Unit(
                        "grok",
                        _rel(enc, sid),
                        root,
                        edir,
                        sid,
                        members=members,
                        dirs=dirs,
                        is_dir=True,
                        unit_dir=sdir,
                    )
                )
            )
    return units


def discover_cursor(root: str) -> list[Unit]:
    units = []
    if not _isdir(root):
        return units
    chats = os.path.join(root, "chats")
    if _isdir(chats):
        for ws in sorted(os.listdir(chats)):
            wdir = os.path.join(chats, ws)
            if not _isdir(wdir):
                continue
            for cid in sorted(os.listdir(wdir)):
                cdir = os.path.join(wdir, cid)
                if not _isdir(cdir):
                    continue
                members = []
                for path, rel in _walk_files(cdir):
                    rows = "cursor-store" if rel == "store.db" else "none"
                    members.append(Member(path, _rel(cid, rel), rows))
                if not members:
                    continue
                dirs = [_rel(cid, d) for d in _walk_dirs(cdir)]
                units.append(
                    _finish(
                        Unit(
                            "cursor",
                            _rel("chats", ws, cid),
                            root,
                            wdir,
                            cid,
                            members=members,
                            dirs=dirs,
                            is_dir=True,
                            unit_dir=cdir,
                        )
                    )
                )
    projects = os.path.join(root, "projects")
    if _isdir(projects):
        for proj in sorted(os.listdir(projects)):
            pdir = os.path.join(projects, proj)
            if not _isdir(pdir):
                continue
            members = []
            for path, rel in _walk_files(pdir):
                rows = "jsonl" if rel.endswith(".jsonl") else ("log" if rel.endswith(".log") else "none")
                members.append(Member(path, _rel(proj, rel), rows))
            if not members:
                continue
            dirs = [_rel(proj, d) for d in _walk_dirs(pdir)]
            units.append(
                _finish(
                    Unit(
                        "cursor",
                        _rel("projects", proj),
                        root,
                        projects,
                        proj,
                        members=members,
                        dirs=dirs,
                        is_dir=True,
                        unit_dir=pdir,
                    )
                )
            )
    return units


def discover_commandcode(root: str) -> list[Unit]:
    units = []
    if not _isdir(root):
        return units
    for proj in sorted(os.listdir(root)):
        pdir = os.path.join(root, proj)
        if not _isdir(pdir):
            continue
        groups: dict[str, list] = {}
        for path, rel in _walk_files(pdir):
            if os.sep in rel:
                continue
            stem = rel.split(".")[0]
            groups.setdefault(stem, []).append((path, rel))
        for stem, files in sorted(groups.items()):
            members = []
            for path, rel in files:
                rows = "jsonl" if rel.endswith(".jsonl") else "none"
                members.append(Member(path, rel, rows))
            units.append(_finish(Unit("commandcode", _rel(proj, stem), root, pdir, stem, members=members)))
    return units


def discover_pi(root: str) -> list[Unit]:
    units = []
    if not _isdir(root):
        return units
    for proj in sorted(os.listdir(root)):
        pdir = os.path.join(root, proj)
        if not _isdir(pdir):
            continue
        for name in sorted(os.listdir(pdir)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            stem = name[: -len(".jsonl")]
            session = stem.rsplit("_", 1)[-1] if "_" in stem else stem
            units.append(
                _finish(Unit("pi", _rel(proj, name), root, pdir, session, members=[Member(path, name, "jsonl")]))
            )
    return units


def discover_muse(root: str) -> list[Unit]:
    units = []
    if not _isdir(root):
        return units
    for year in sorted(os.listdir(root)):
        ydir = os.path.join(root, year)
        if not _isdir(ydir) or not year.isdigit():
            continue
        for month in sorted(os.listdir(ydir)):
            mdir = os.path.join(ydir, month)
            if not _isdir(mdir) or not month.isdigit():
                continue
            for day in sorted(os.listdir(mdir)):
                ddir = os.path.join(mdir, day)
                if not _isdir(ddir) or not day.isdigit():
                    continue
                for sid in sorted(os.listdir(ddir)):
                    sdir = os.path.join(ddir, sid)
                    if not _isdir(sdir):
                        continue
                    members = []
                    for path, rel in _walk_files(sdir):
                        rows = "jsonl" if rel == "session.jsonl" else "none"
                        members.append(Member(path, _rel(sid, rel), rows))
                    if not members:
                        continue
                    dirs = [_rel(sid, d) for d in _walk_dirs(sdir)]
                    units.append(
                        _finish(
                            Unit(
                                "muse",
                                _rel(year, month, day, sid),
                                root,
                                ddir,
                                sid,
                                members=members,
                                dirs=dirs,
                                is_dir=True,
                                unit_dir=sdir,
                            )
                        )
                    )
    return units


#: name -> (root, discovery function). Data, not branches.
TOOLS = {
    "claude": ("~/.claude/projects", discover_claude),
    "codex": ("~/.codex", discover_codex),
    "grok": ("~/.grok/sessions", discover_grok),
    "cursor": ("~/.cursor", discover_cursor),
    "commandcode": ("~/.commandcode/projects", discover_commandcode),
    "pi": ("~/.pi/agent/sessions", discover_pi),
    "muse": ("~/.local/share/muse/sessions", discover_muse),
}


def discover(tool_names) -> tuple[list[Unit], dict]:
    units: list[Unit] = []
    meta = {"absent": [], "out_of_scope": {}}
    for name in tool_names:
        root_t, fn = TOOLS[name]
        root = os.path.expanduser(root_t)
        if not os.path.isdir(root):
            meta["absent"].append(name)
            continue
        units.extend(fn(root))
    units.sort(key=lambda u: (u.tool, u.key))
    return units, meta


def fingerprint(unit: Unit) -> list[tuple]:
    return sorted((m.rel, m.size, m.mtime_ns) for m in unit.members)


def _fp_members(fp) -> list[dict]:
    return [{"rel": rel, "size": size, "mtime_ns": mtime_ns} for rel, size, mtime_ns in fp]


def _fp_equal(prior_members, fp) -> bool:
    if not prior_members:
        return False
    try:
        have = sorted((m["rel"], m["size"], m["mtime_ns"]) for m in prior_members)
    except (KeyError, TypeError):
        return False
    return have == sorted(fp)


# --------------------------------------------------------------- zstd / tar

def _zstd_bin() -> str:
    found = shutil.which("zstd")
    if not found:
        raise ArchiveError("zstd not found on PATH")
    return found


def open_paths(paths) -> set[str]:
    """Which of ``paths`` some process holds open, from one ``lsof -Fn`` call.

    The native roots hold hundreds of thousands of member files, so a
    path-scoped ``lsof`` per chunk is far too slow. One system-wide listing
    (a few seconds) is intersected with the wanted set instead. Fails
    closed: a missing or failing ``lsof`` is an error.
    """
    wanted = {os.path.realpath(p) for p in paths}
    if not wanted:
        return set()
    try:
        proc = subprocess.run(["lsof", "-Fn"], capture_output=True, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise ArchiveError("lsof not found; cannot prove no open handle") from exc
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError("lsof timed out") from exc
    if proc.returncode not in (0, 1):
        raise ArchiveError("lsof failed (exit %d)" % proc.returncode)
    found = set()
    for line in proc.stdout.splitlines():
        if line.startswith("n") and line[1:] in wanted:
            found.add(line[1:])
    return found


class _HashReader:
    """File wrapper that hashes and counts newlines as tarfile reads it."""

    def __init__(self, fh) -> None:
        self.fh = fh
        self.h = hashlib.sha256()
        self.n = 0
        self.nl = 0
        self.last = b""

    def read(self, size: int = -1) -> bytes:
        data = self.fh.read(size)
        if data:
            self.h.update(data)
            self.n += len(data)
            self.nl += data.count(b"\n")
            self.last = data[-1:]
        return data


def pack(unit: Unit, out_path: str) -> list[dict]:
    """Stream a PAX tar of the unit into ``zstd -12 --long=27 -T2``.

    Returns one record per member with ``rel``, ``size``, ``mtime_ns``,
    ``sha256``, ``lines`` (None for a cursor store, counted later) and
    ``rows``. Never holds a whole member in memory.
    """
    zstd = _zstd_bin()
    proc = subprocess.Popen(
        [zstd, ZSTD_LEVEL, ZSTD_LONG, ZSTD_THREADS, "-q", "-f", "-o", out_path],
        stdin=subprocess.PIPE,
    )
    records: list[dict] = []
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for rel in unit.dirs:
                info = tarfile.TarInfo(rel.rstrip("/") + "/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                info.mtime = int(unit.newest_mtime)
                tar.addfile(info)
            for member in unit.members:
                st = os.stat(member.path)
                info = tarfile.TarInfo(member.rel)
                info.size = st.st_size
                info.mode = st.st_mode & 0o777
                info.mtime = int(st.st_mtime)
                info.uid = st.st_uid
                info.gid = st.st_gid
                with open(member.path, "rb") as fh:
                    reader = _HashReader(fh)
                    tar.addfile(info, reader)
                if member.rows in ("jsonl", "log"):
                    if member.path.endswith(".zst"):
                        # The tar holds compressed bytes; ingest reads the
                        # decompressed stream, so count that same stream.
                        lines = _count_lines(member.path)
                    else:
                        lines = reader.nl + (1 if reader.n and reader.last != b"\n" else 0)
                elif member.rows == "none":
                    lines = 0
                else:
                    lines = None
                records.append(
                    {
                        "rel": member.rel,
                        "size": reader.n,
                        "mtime_ns": st.st_mtime_ns,
                        "sha256": reader.h.hexdigest(),
                        "lines": lines,
                        "rows": member.rows,
                    }
                )
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        rc = proc.wait()
    if rc != 0:
        raise ArchiveError("zstd exited %d" % rc)
    return records


def verify_archive(path: str, records: list[dict]) -> None:
    """Stream the copy back and compare every member's sha256 and size."""
    expected = {r["rel"]: r for r in records}
    seen: dict[str, tuple] = {}
    proc = subprocess.Popen([_zstd_bin(), "-dc", ZSTD_LONG, path], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for info in tar:
                if not info.isfile():
                    continue
                digest = hashlib.sha256()
                size = 0
                src = tar.extractfile(info)
                while True:
                    chunk = src.read(READ_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                seen[info.name] = (size, digest.hexdigest())
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise ArchiveError("zstd -dc exited %d" % rc)
    if set(seen) != set(expected):
        missing = sorted(set(expected) - set(seen))[:3]
        extra = sorted(set(seen) - set(expected))[:3]
        raise ArchiveError("archive members differ (missing %s, extra %s)" % (missing, extra))
    for rel, (size, sha) in seen.items():
        rec = expected[rel]
        if size != rec["size"] or sha != rec["sha256"]:
            raise ArchiveError("archive member %s does not match (size %d/%d)" % (rel, size, rec["size"]))


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------- rows

def _iter_lines(path: str):
    """Lines of a text or ``.zst`` member, streamed, without the newline."""
    if path.endswith(".zst"):
        proc = subprocess.Popen([_zstd_bin(), "-dc", ZSTD_LONG, path], stdout=subprocess.PIPE)
        try:
            yield from _split_lines(proc.stdout)
        finally:
            proc.stdout.close()
            proc.wait()
    else:
        with open(path, "rb") as fh:
            yield from _split_lines(fh)


def _iter_lines_offsets(fh):
    """Yield ``(line, end_offset, complete)`` with a bounded buffer.

    A line longer than ``MAX_LINE_BYTES`` without a newline is emitted once
    as ``MAX_MSG_BYTES + 1`` bytes (so ``make_row`` truncates it and sets
    ``truncated=1``); the rest of that line is discarded while streaming.
    ``end_offset`` is the byte position just past the line's newline (or
    past the last byte at EOF), so a live-history offset never lands in the
    middle of a discarded line. ``complete`` is False only for a trailing
    partial line at EOF, which a live history leaves for the next run.
    """
    buf = bytearray()
    pos = 0
    long_head = None
    while True:
        chunk = fh.read(READ_CHUNK)
        if not chunk:
            break
        if long_head is not None:
            nl = chunk.find(b"\n")
            if nl < 0:
                pos += len(chunk)
                continue
            pos += nl + 1
            yield long_head, pos, True
            long_head = None
            chunk = chunk[nl + 1:]
            if not chunk:
                continue
        buf += chunk
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            yield bytes(buf[start:nl]), pos + (nl + 1 - start), True
            pos += nl + 1 - start
            start = nl + 1
        del buf[:start]
        if len(buf) > MAX_LINE_BYTES:
            long_head = bytes(buf[: vlogs.MAX_MSG_BYTES + 1])
            pos += len(buf)
            buf.clear()
    if long_head is not None:
        yield long_head, pos, False
    elif buf:
        pos += len(buf)
        yield bytes(buf), pos, False


def _split_lines(fh):
    for line, _end, _complete in _iter_lines_offsets(fh):
        yield line


def _count_lines(path: str) -> int:
    """Line count of the stream ingest reads (decompressed for ``.zst``)."""
    return sum(1 for _ in _iter_lines(path))


def _json(line: bytes):
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _line_ms(obj: dict, fallback: int) -> int:
    for key in ("timestamp", "ts", "time", "created_at", "recorded_at", "date"):
        if key not in obj:
            continue
        value = obj[key]
        if isinstance(value, str):
            ms = parse_iso_ms(value)
        else:
            ms = epoch_ms(value)
        if ms is not None:
            return ms
    return fallback


def _muse_record(obj: dict) -> dict:
    if "record_type" in obj:
        return obj
    children = obj.get("children")
    if isinstance(children, list) and children and isinstance(children[0], dict):
        inner = children[0].get("record_json")
        if isinstance(inner, str):
            parsed = _json(inner.encode("utf-8", "replace"))
            if parsed:
                return parsed
    return obj


def extract(tool: str, line: bytes, member: Member, fallback_ms: int) -> dict:
    """Row fields for one line: type, subtype, project, model, ms."""
    out = {"type": "raw", "subtype": "", "project": "", "model": "", "ms": fallback_ms}
    if member.rows == "log":
        out["type"] = "log"
        return out
    obj = _json(line)
    if obj is None:
        return out
    out["ms"] = _line_ms(obj, fallback_ms)
    if tool == "claude":
        out["type"] = str(obj.get("type") or "raw")
        message = obj.get("message")
        if isinstance(message, dict):
            out["model"] = str(message.get("model") or "")
        out["project"] = str(obj.get("cwd") or "")
    elif tool == "codex":
        out["type"] = str(obj.get("type") or "raw")
        payload = obj.get("payload")
        if isinstance(payload, dict):
            out["subtype"] = str(payload.get("type") or "")
    elif tool == "grok":
        out["type"] = str(obj.get("type") or obj.get("method") or "raw")
    elif tool == "cursor":
        out["type"] = str(obj.get("role") or obj.get("type") or "raw")
    elif tool == "commandcode":
        out["type"] = str(obj.get("type") or "raw")
        out["model"] = str(obj.get("model") or "")
    elif tool == "pi":
        out["type"] = str(obj.get("type") or "raw")
        out["model"] = str(obj.get("modelId") or "")
    elif tool == "muse":
        rec = _muse_record(obj)
        out["type"] = str(rec.get("record_type") or "raw")
        out["subtype"] = str(rec.get("payload_type") or "")
        out["ms"] = _line_ms(rec, fallback_ms)
    return out


def _hex_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _cursor_type(obj) -> str:
    if isinstance(obj, dict):
        return str(obj.get("role") or obj.get("type") or "raw")
    return "raw"


def _cursor_rows(member: Member, fallback_ms: int):
    """Rows from a Cursor ``store.db``: hex-JSON meta values and JSON blobs.

    The db (plus ``-wal``/``-shm``) is copied to a temp dir first, then
    opened read-only, so a live Cursor process is never touched.
    """
    tmp = tempfile.mkdtemp(prefix="cursor-store-")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = member.path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "store.db" + suffix))
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(tmp, "store.db"), uri=True)
        try:
            for (value,) in con.execute("select value from meta"):
                text = _hex_text(value)
                if text is None:
                    continue
                obj = _json(text.encode("utf-8", "replace"))
                yield text, _cursor_type(obj), _line_ms(obj, fallback_ms) if obj else fallback_ms
            for (data,) in con.execute("select data from blobs"):
                if not isinstance(data, (bytes, bytearray)):
                    continue
                try:
                    text = bytes(data).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                obj = _json(text.encode("utf-8", "replace"))
                if obj is None:
                    continue
                yield text, _cursor_type(obj), _line_ms(obj, fallback_ms)
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def iter_member_rows(member: Member, unit: Unit, skip: int = 0):
    """Yield ``(text, type, ms, project, model, subtype, line)`` per row.

    ``line`` is the 1-based line number within the member (for a cursor
    store, the 1-based row index). It is the dedupe key for at-least-once
    ingest: ``count_uniq(member, line)`` is the verification count.
    """
    fallback = int(unit.newest_mtime * 1000)
    if member.rows == "cursor-store":
        for line_no, (text, type_, ms) in enumerate(_cursor_rows(member, fallback), 1):
            if line_no <= skip:
                continue
            yield text, type_, ms, "", "", "", line_no
        return
    if member.rows not in ("jsonl", "log"):
        return
    for line_no, line in enumerate(_iter_lines(member.path), 1):
        if line_no <= skip:
            continue
        info = extract(unit.tool, line, member, fallback)
        yield (
            line.decode("utf-8", "replace"),
            info["type"],
            info["ms"],
            info["project"],
            info["model"],
            info["subtype"],
            line_no,
        )


def count_member_rows(member: Member, unit: Unit) -> int:
    if member.rows == "cursor-store":
        return sum(1 for _ in _cursor_rows(member, int(unit.newest_mtime * 1000)))
    return 0


# --------------------------------------------------------------- live histories

def live_files(tool_names) -> list[tuple[str, str]]:
    """Existing live append-only history files for the selected tools."""
    import glob

    out = []
    for tool, pattern in LIVE_HISTORIES:
        if tool not in tool_names:
            continue
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            if os.path.isfile(path) and not os.path.islink(path):
                out.append((tool, path))
    return out


def _prefix_sha(path: str, n: int) -> str:
    digest = hashlib.sha256()
    remaining = n
    with open(path, "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(READ_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _live_row(tool: str, uid: str, rel: str, line: bytes, rev: str, line_no: int) -> bytes:
    obj = _json(line)
    type_ = "raw"
    session = ""
    ms = int(time.time() * 1000)
    if obj:
        type_ = str(obj.get("type") or obj.get("role") or "raw")
        session = str(
            obj.get("sessionId") or obj.get("session_id") or obj.get("session") or ""
        )
        ms = _line_ms(obj, ms)
    extra = {
        "tool": tool,
        "session": session,
        "unit": uid,
        "member": rel,
        "rev": rev,
        "line": str(line_no),
    }
    row, _truncated, _redactions = vlogs.make_row(
        time_iso=iso_from_ms(ms),
        msg=line.decode("utf-8", "replace"),
        ws="native/" + tool,
        label=session or os.path.basename(rel),
        type_=type_,
        kind="native",
        path=uid,
        extra=extra,
    )
    return row


def ingest_live(tool: str, path: str, manifest: Manifest, client) -> int:
    """Ingest complete new lines of a live history from its stored offset.

    A shrink or a change in the sha256 of the first ``min(size, 4096)``
    bytes restarts from offset 0 with ``rev+1``. A trailing partial line is
    left for the next run. Every row carries its 1-based ``line`` number in
    the file for that rev, so a resend after a crash is a harmless duplicate
    and verification uses ``count_uniq(member, line)``.
    """
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(path, home)
    except ValueError:
        rel = os.path.basename(path)
    rel = rel.replace(os.sep, "/")
    uid = "live/%s/%s" % (tool, rel)
    prior = manifest.latest.get(uid)
    st = os.stat(path)
    size = st.st_size
    prefix_len = int(prior.get("prefix_len", min(size, 4096))) if prior else min(size, 4096)
    prefix = _prefix_sha(path, min(prefix_len, size))
    offset = int(prior.get("offset", 0)) if prior else 0
    rev = str(prior.get("rev", "0")) if prior else "0"
    line_no = int(prior.get("line", 0)) if prior else 0
    if prior and (size < offset or prefix != prior.get("prefix_sha256")):
        offset = 0
        rev = str(int(rev) + 1)
        line_no = 0
    if offset > size:
        offset = 0
        line_no = 0
    new_offset = offset
    sent = 0
    if size > offset:
        def gen():
            nonlocal new_offset, line_no
            with open(path, "rb") as fh:
                fh.seek(offset)
                for line, end, complete in _iter_lines_offsets(fh):
                    if not complete:
                        # A trailing partial line is left for the next run.
                        break
                    new_offset = offset + end
                    line_no += 1
                    yield _live_row(tool, uid, rel, line, rev, line_no)

        sent = client.insert(gen())
        client.flush()
    got = count_uniq_rows(client, uid, rev)
    if got != line_no:
        raise ArchiveError(
            "store has %d distinct (member,line) rows for %s rev %s, expected %d"
            % (got, uid, rev, line_no)
        )
    manifest.write(
        {
            "unit": uid,
            "tool": tool,
            "key": rel,
            "status": "live",
            "offset": new_offset,
            "prefix_sha256": prefix,
            "prefix_len": prefix_len,
            "size": size,
            "rev": rev,
            "line": line_no,
            "lines": line_no,
        }
    )
    return sent


# --------------------------------------------------------------- store

def _quote(value: str) -> str:
    if '"' in value or "\\" in value:
        raise ArchiveError("value has a character LogsQL cannot quote: %r" % value)
    return value


def count_rows(client, unit_uid: str, rev: str | None = None) -> int:
    query = 'unit:="%s"' % _quote(unit_uid)
    if rev is not None:
        query += ' rev:="%s"' % _quote(rev)
    query += " | stats count() n"
    rows = client.query(query)
    return int(rows[0]["n"]) if rows else 0


def count_uniq_rows(client, unit_uid: str, rev: str) -> int:
    """Distinct ``(member, line)`` pairs stored for ``(unit, rev)``.

    This is the ingest verification count. A resend after a crash stores
    duplicate rows, which are harmless and invisible here; the docs give
    the analytics dedupe pattern ``| uniq by (unit, member, rev, line)``.
    """
    query = 'unit:="%s" rev:="%s" | stats count_uniq(member, line) n' % (
        _quote(unit_uid),
        _quote(rev),
    )
    rows = client.query(query)
    return int(rows[0]["n"]) if rows else 0


def _prefix_matches(path: str, old: dict) -> bool:
    """True when the current file's first ``old.size`` bytes hash to ``old.sha256``."""
    if old.get("rows") == "cursor-store":
        return False
    size = int(old.get("size") or 0)
    digest = hashlib.sha256()
    seen = 0
    with open(path, "rb") as fh:
        while seen < size:
            chunk = fh.read(min(READ_CHUNK, size - seen))
            if not chunk:
                break
            digest.update(chunk)
            seen += len(chunk)
    return seen == size and digest.hexdigest() == old.get("sha256")


def ingest_unit(unit: Unit, records: list[dict], prior: dict | None, client) -> tuple[str, int, int]:
    """Send the unit's rows, verify the store, and return ``(rev, total, sent)``.

    At-least-once: a rerun resends from the last line the manifest confirmed
    (or from line 1 of the current rev) and verifies with
    ``count_uniq(member, line)``, so a crash after a POST but before the
    manifest write is harmless. A member whose old bytes are not a prefix of
    the new file is a rewrite: the unit ``rev`` is bumped and every member is
    resent at the new rev. Verification and append planning use only the
    current rev, so rows kept from an older rev never block growth.
    """
    prior_members = {m["rel"]: m for m in (prior or {}).get("members", [])}
    prior_rev = str((prior or {}).get("rev", "0"))
    total = sum(int(r.get("lines") or 0) for r in records)
    by_rel = {r["rel"]: r for r in records}

    rewrite = False
    for member in unit.members:
        rec = by_rel.get(member.rel)
        old = prior_members.get(member.rel)
        if rec is None or old is None:
            continue
        if old.get("sha256") == rec["sha256"] and old.get("size") == rec["size"]:
            continue
        if not _prefix_matches(member.path, old):
            rewrite = True
            break
    rev = str(int(prior_rev) + 1) if rewrite else prior_rev

    if not rewrite and count_uniq_rows(client, unit.uid, rev) == total:
        # A previous run posted every row and died before the manifest write.
        return rev, total, 0

    plan: list[tuple[Member, int]] = []
    for member in unit.members:
        rec = by_rel.get(member.rel)
        if rec is None:
            continue
        old = prior_members.get(member.rel)
        if rewrite or old is None:
            plan.append((member, 0))
        elif old.get("sha256") == rec["sha256"] and old.get("size") == rec["size"]:
            continue
        elif _prefix_matches(member.path, old):
            plan.append((member, int(old.get("lines") or 0)))
        else:
            plan.append((member, 0))

    sent = 0
    if plan:
        def gen():
            for member, skip in plan:
                for text, type_, ms, project, model, subtype, line_no in iter_member_rows(member, unit, skip):
                    extra = {
                        "tool": unit.tool,
                        "session": unit.session,
                        "project": project,
                        "model": model,
                        "subtype": subtype,
                        "unit": unit.uid,
                        "member": member.rel,
                        "rev": rev,
                        "line": str(line_no),
                    }
                    line, _truncated, _redactions = vlogs.make_row(
                        time_iso=iso_from_ms(ms),
                        msg=text,
                        ws="native/" + unit.tool,
                        label=unit.session,
                        type_=type_,
                        kind="native",
                        path=unit.uid,
                        extra=extra,
                    )
                    yield line

        sent = client.insert(gen())
        client.flush()

    got = count_uniq_rows(client, unit.uid, rev)
    if got != total:
        raise ArchiveError(
            "store has %d distinct (member,line) rows for %s rev %s, expected %d"
            % (got, unit.uid, rev, total)
        )
    return rev, total, sent


# --------------------------------------------------------------- reclaim

def _uid_safe(uid: str) -> str:
    """Reversible directory name for a unit id (``/`` -> ``%2F``)."""
    return uid.replace("%", "%25").replace("/", "%2F")


def _trash_root(dest) -> Path:
    return Path(dest) / "state" / "trash"


def _trash_dir(dest, uid: str) -> Path:
    return _trash_root(dest) / _uid_safe(uid)


def _prune_dirs(parent: str, rels) -> None:
    """Remove now-empty ancestor dirs of ``rels``, bottom-up, never siblings.

    Stops at ``parent`` (the unit's parent dir), so a Claude ``memory/``
    subtree or any other excluded tree is never walked or removed.
    """
    base = os.path.abspath(parent)
    dirs = set()
    for rel in rels:
        d = os.path.dirname(os.path.abspath(os.path.join(base, rel)))
        while d and d != base and d.startswith(base + os.sep):
            dirs.add(d)
            d = os.path.dirname(d)
    for d in sorted(dirs, key=len, reverse=True):
        try:
            os.rmdir(d)
        except OSError:
            pass


def _trash_mismatch(trash: Path, members) -> list[str]:
    """Rels whose file in trash is missing or does not match the manifest."""
    bad = []
    for m in members:
        path = trash / m["rel"]
        try:
            st = os.stat(path)
        except OSError:
            bad.append(m["rel"])
            continue
        if st.st_size != int(m["size"]):
            bad.append(m["rel"])
            continue
        if _sha256_file(str(path)) != m["sha256"]:
            bad.append(m["rel"])
    return bad


def _rollback(trash: Path, members) -> str:
    """Move trashed files back to their original paths. Returns leftovers.

    A file whose original path now exists is left in trash (never
    overwritten) and named in the returned string; the trash dir is kept so
    the next recovery can retry it.
    """
    problems = []
    left = False
    for m in members:
        src = trash / m["rel"]
        if not src.exists():
            continue
        dst = Path(m["path"])
        if dst.exists():
            problems.append("%s (left in trash: %s)" % (dst, src))
            left = True
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
        except OSError as exc:
            problems.append("%s: %s" % (dst, exc))
            left = True
    if not left:
        shutil.rmtree(trash, ignore_errors=True)
    return "; ".join(problems)


def _archive_ok(rec: dict) -> bool:
    archive = rec.get("archive")
    if not archive or not os.path.exists(archive):
        return False
    if os.path.getsize(archive) != int(rec.get("archive_bytes") or -1):
        return False
    try:
        verify_archive(archive, rec.get("members") or [])
    except ArchiveError:
        return False
    return True


def _finish_reclaim(rec: dict, dest, manifest: Manifest) -> None:
    """Step 5: delete the unit's trash dir, prune empty parents, record it."""
    trash = _trash_dir(dest, rec["unit"])
    root = _trash_root(dest)
    if str(trash).startswith(str(root) + os.sep):
        shutil.rmtree(trash, ignore_errors=True)
    members = rec.get("trash_members") or rec.get("members") or []
    _prune_dirs(rec.get("parent") or "", [m["rel"] for m in members])
    manifest.write(dict(rec, status="reclaimed", reclaimed_ts=round(time.time(), 3)))


def _delete_guard(unit: Unit, cfg: dict) -> None:
    """Refuse any delete under the archive dest or outside the tool root.

    The guard resolves symlinks, so a unit that points into the archive
    folder (or anywhere outside its tool root) can never be deleted. It
    raises ``ArchiveError``; ``apply_pass`` records that as the unit's
    error and keeps going, so one bad unit never stops the pass.
    """
    dest = os.path.realpath(os.path.expanduser(str(cfg.get("dest") or DEFAULT_DEST)))
    root = os.path.realpath(unit.root)
    targets = [m.path for m in unit.members]
    if unit.is_dir and unit.unit_dir:
        targets.append(unit.unit_dir)
    for path in targets:
        real = os.path.realpath(path)
        if real == dest or real.startswith(dest + os.sep):
            raise ArchiveError("refusing to delete %s: under the archive dest %s" % (path, dest))
        if not (real == root or real.startswith(root + os.sep)):
            raise ArchiveError("refusing to delete %s: not under the tool root %s" % (path, root))


def reclaim_unit(
    unit: Unit,
    rec: dict,
    cfg: dict,
    now: float,
    manifest: Manifest,
    dest,
    client,
) -> str | None:
    """Journaled two-phase reclaim. Returns a reason to keep, or None.

    Preconditions (all, else keep and record why): status ``archived``,
    fingerprint unchanged, idle >= ``reclaim_days``, no open handle, store
    reachable and the unit's ingest verified, the current archive exists at
    the recorded size and verifies against the recorded members, and the
    delete guard passes. Then the manifest records ``reclaiming`` before any
    file moves, each member is renamed into ``state/trash/<uid>/``, every
    trashed file is re-hashed, and only then is the trash deleted. A crash
    at any point is resolved by ``recover_trash`` on the next ``--apply``.
    """
    _delete_guard(unit, cfg)
    days = float(cfg["reclaim_days_by_tool"].get(unit.tool, cfg["reclaim_days"]))
    idle_hours = (now - unit.newest_mtime) / 3600.0
    if idle_hours < days * 24.0:
        return "too young (%.1f h < %g d)" % (idle_hours, days)
    if logarchive.open_paths([m.path for m in unit.members]):
        return "open handle"
    root = os.path.realpath(unit.root)
    for member in unit.members:
        real = os.path.realpath(member.path)
        if not (real == root or real.startswith(root + os.sep)):
            return "path escapes root: %s" % member.path
    recorded = {m["rel"]: m for m in rec.get("members", [])}
    for member in unit.members:
        old = recorded.get(member.rel)
        if old is None:
            return "member not in the manifest: %s" % member.rel
        try:
            st = os.stat(member.path)
        except OSError as exc:
            return "stat failed: %s" % exc
        if st.st_size != old["size"] or st.st_mtime_ns != old["mtime_ns"]:
            return "changed since archive: %s" % member.rel
        if _sha256_file(member.path) != old["sha256"]:
            return "content changed since archive: %s" % member.rel

    # The archive must exist, be the recorded size, and verify.
    archive = rec.get("archive")
    if not archive or not os.path.exists(archive):
        return "archive missing: %s" % archive
    if os.path.getsize(archive) != int(rec.get("archive_bytes") or -1):
        return "archive size changed: %s" % archive
    try:
        verify_archive(archive, rec.get("members") or [])
    except ArchiveError as exc:
        return "archive does not verify: %s" % exc

    # The store must be reachable and this unit's ingest verified.
    if client is None:
        return "store not checked"
    try:
        got = count_uniq_rows(client, unit.uid, str(rec.get("rev", "0")))
    except Exception as exc:  # store down or query failed
        return "store unreachable: %s" % exc
    if got != int(rec.get("lines") or 0):
        return "ingest not verified (%d/%d rows)" % (got, int(rec.get("lines") or 0))

    # Same volume: rename must not cross a device boundary.
    trash = _trash_dir(dest, unit.uid)
    try:
        trash.parent.mkdir(parents=True, exist_ok=True)
        trash_dev = os.stat(trash.parent).st_dev
    except OSError as exc:
        return "trash dir unavailable: %s" % exc
    for member in unit.members:
        try:
            if os.stat(member.path).st_dev != trash_dev:
                return "different device for %s" % member.rel
        except OSError as exc:
            return "stat failed: %s" % exc

    trash_members = [
        {
            "rel": m.rel,
            "path": m.path,
            "sha256": recorded[m.rel]["sha256"],
            "size": recorded[m.rel]["size"],
        }
        for m in unit.members
    ]
    journal = dict(
        rec,
        status="reclaiming",
        trash=str(trash),
        trash_members=trash_members,
        reclaim_started_ts=round(time.time(), 3),
    )
    manifest.write(journal)

    try:
        for m in unit.members:
            dst = trash / m.rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(m.path, dst)
    except OSError as exc:
        leftover = _rollback(trash, trash_members)
        manifest.write(
            dict(
                journal,
                status="error" if leftover else "archived",
                reclaim_reason="rename failed: %s%s" % (exc, "; " + leftover if leftover else ""),
            )
        )
        return "rename failed: %s" % exc

    bad = _trash_mismatch(trash, trash_members)
    if bad:
        leftover = _rollback(trash, trash_members)
        manifest.write(
            dict(
                journal,
                status="error" if leftover else "archived",
                error="trash hash mismatch: %s%s" % (", ".join(bad[:5]), "; " + leftover if leftover else ""),
            )
        )
        return "trash hash mismatch: %s" % ", ".join(bad[:5])

    _finish_reclaim(journal, dest, manifest)
    return None


def _recover_one(uid: str, rec: dict, dest, manifest: Manifest, out) -> None:
    """Resolve one ``reclaiming`` record from whatever the trash holds."""
    entry = _trash_dir(dest, uid)
    members = rec.get("trash_members") or []
    files = [p for p in entry.rglob("*") if p.is_file()] if entry.is_dir() else []
    if not files:
        # Crash after the trash delete (or before any rename).
        if _archive_ok(rec) and all(not os.path.exists(m["path"]) for m in members):
            _finish_reclaim(rec, dest, manifest)
            print("  recovered reclaim %s: reclaimed" % uid, file=out)
        else:
            manifest.write(dict(rec, status="archived", reclaim_reason="recovered: nothing in trash"))
            print("  recovered reclaim %s: archived" % uid, file=out)
        return
    bad = _trash_mismatch(entry, members)
    if bad or not _archive_ok(rec):
        leftover = _rollback(entry, members)
        manifest.write(
            dict(
                rec,
                status="error" if leftover else "archived",
                error="recovered: trash mismatch %s%s"
                % (", ".join(bad[:5]), "; " + leftover if leftover else ""),
            )
        )
        print("  recovered reclaim %s: rolled back" % uid, file=out)
    else:
        _finish_reclaim(rec, dest, manifest)
        print("  recovered reclaim %s: reclaimed" % uid, file=out)


def recover_trash(dest, manifest: Manifest, out) -> int:
    """Resolve every ``reclaiming`` record and leftover trash dir.

    Runs at the start of every ``--apply``. If the trash hashes match the
    manifest and the archive verifies, step 5 is finished (``reclaimed``);
    otherwise the files are moved back and the unit returns to ``archived``.
    A ``reclaiming`` record is resolved even when its trash dir is already
    gone (a crash after the trash delete). Returns the number resolved.
    """
    root = _trash_root(dest)
    resolved = 0
    seen = set()

    def needs_recovery(rec) -> bool:
        return rec.get("status") == "reclaiming" or (
            rec.get("status") == "error" and bool(rec.get("trash_members"))
        )

    for uid, rec in list(manifest.latest.items()):
        if not needs_recovery(rec):
            continue
        seen.add(_uid_safe(uid))
        _recover_one(uid, rec, dest, manifest, out)
        resolved += 1
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in seen:
                continue
            uid = entry.name.replace("%2F", "/").replace("%25", "%")
            rec = manifest.latest.get(uid)
            if rec is None or not needs_recovery(rec):
                # No journal: never delete unknown data. Leave it and say so.
                if any(entry.iterdir()):
                    print("  trash without a reclaiming record: %s" % entry, file=out)
                else:
                    shutil.rmtree(entry, ignore_errors=True)
                continue
            _recover_one(uid, rec, dest, manifest, out)
            resolved += 1
    return resolved


# --------------------------------------------------------------- manifest

class Manifest:
    """Append-only JSON lines; the last record for a unit wins."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "manifest.jsonl"
        self.latest: dict[str, dict] = {}
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("unit"):
                        self.latest[rec["unit"]] = rec

    def write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(rec, ts=round(time.time(), 3))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.latest[rec["unit"]] = rec


def archive_path(state_dir: Path, unit: Unit, seq: int = 0) -> Path:
    """``<dest>/archive/<tool>/<key>.<seq>.tar.zst``. A pack never replaces."""
    return Path(state_dir) / "archive" / unit.tool / ("%s.%d.tar.zst" % (unit.key, seq))


def next_archive_seq(state_dir: Path, unit: Unit) -> int:
    """The next free archive sequence for a unit (0 when none exist)."""
    base = archive_path(state_dir, unit, 0).parent
    prefix = os.path.basename(unit.key) + "."
    seq = 0
    if base.is_dir():
        for name in os.listdir(base):
            if not (name.startswith(prefix) and name.endswith(".tar.zst")):
                continue
            mid = name[len(prefix): -len(".tar.zst")]
            if mid.isdigit():
                seq = max(seq, int(mid) + 1)
    return seq


# --------------------------------------------------------------- eligibility

def _idle(unit: Unit, cfg: dict, now: float) -> bool:
    return (now - unit.newest_mtime) >= float(cfg["min_idle_hours"]) * 3600.0


def _is_open(unit: Unit, open_set: set) -> bool:
    return any(os.path.realpath(m.path) in open_set for m in unit.members)


def _reclaim_ready(unit: Unit, cfg: dict, now: float) -> bool:
    days = float(cfg["reclaim_days_by_tool"].get(unit.tool, cfg["reclaim_days"]))
    return (now - unit.newest_mtime) >= days * 24.0 * 3600.0


def _done(unit: Unit, manifest: Manifest) -> bool:
    prior = manifest.latest.get(unit.uid)
    return (
        bool(prior)
        and prior.get("status") in ("reclaimed", "restored")
        and _fp_equal(prior.get("members"), fingerprint(unit))
    )


def _settled(unit: Unit, manifest: Manifest) -> bool:
    """Archived, reclaimed or restored with a matching fingerprint."""
    prior = manifest.latest.get(unit.uid)
    return (
        bool(prior)
        and prior.get("status") in ("archived", "reclaimed", "restored")
        and _fp_equal(prior.get("members"), fingerprint(unit))
    )


# --------------------------------------------------------------- pass

def process_unit(
    unit: Unit,
    prior: dict | None,
    cfg: dict,
    client,
    manifest: Manifest,
    state_dir: Path,
    out,
    *,
    no_reclaim: bool,
    now: float,
) -> str:
    """Archive, ingest and maybe reclaim one unit. Returns the outcome name."""
    fp = fingerprint(unit)
    if prior and prior.get("status") in ("reclaimed", "restored") and _fp_equal(prior.get("members"), fp):
        return "done"
    if prior and prior.get("status") == "reclaiming":
        # recover_trash runs first; an unresolved journal is never re-packed.
        return "skipped"
    if prior and prior.get("status") == "archived" and _fp_equal(prior.get("members"), fp):
        if no_reclaim:
            return "archived"
        reason = reclaim_unit(unit, prior, cfg, now, manifest, state_dir, client)
        if reason:
            print("  keep %s: %s" % (unit.uid, reason), file=out)
            return "archived"
        print("  reclaimed %s" % unit.uid, file=out)
        return "reclaimed"

    # Reuse the current archive when it already matches this fingerprint
    # (an ingest retry after an error record must not pack a new version).
    reuse = None
    if prior and prior.get("archive") and _fp_equal(prior.get("members"), fp):
        path = prior["archive"]
        if os.path.exists(path) and os.path.getsize(path) == int(prior.get("archive_bytes") or -1):
            reuse = prior

    if reuse is not None:
        records = reuse["members"]
        archive = Path(reuse["archive"])
        seq = int(reuse.get("seq", 0))
        archives = list(reuse.get("archives") or [])
        if not archives:
            archives = [{"seq": seq, "path": str(archive), "bytes": int(reuse.get("archive_bytes") or 0), "members": records}]
    else:
        seq = next_archive_seq(state_dir, unit)
        archive = archive_path(state_dir, unit, seq)
        archive.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(archive) + ".tmp"
        try:
            records = pack(unit, tmp)
            for rec, member in zip(records, unit.members):
                if rec["lines"] is None:
                    rec["lines"] = count_member_rows(member, unit)
            verify_archive(tmp, records)
            if os.path.exists(archive):
                raise ArchiveError("refusing to replace existing archive %s" % archive)
            os.rename(tmp, archive)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        archives = list((prior or {}).get("archives") or [])
        archives.append(
            {"seq": seq, "path": str(archive), "bytes": os.path.getsize(archive), "members": records}
        )

    rev, total, sent = ingest_unit(unit, records, prior, client)
    rec = {
        "unit": unit.uid,
        "tool": unit.tool,
        "key": unit.key,
        "session": unit.session,
        "root": unit.root,
        "parent": unit.parent,
        "archive": str(archive),
        "archive_bytes": os.path.getsize(archive),
        "seq": seq,
        "archives": archives,
        "raw_bytes": unit.raw_bytes,
        "rev": rev,
        "lines": total,
        "members": records,
        "status": "archived",
    }
    manifest.write(rec)
    print(
        "  archived %s  raw %d -> zst %d  rows %d (sent %d, rev %s, seq %d)"
        % (unit.uid, unit.raw_bytes, rec["archive_bytes"], total, sent, rev, seq),
        file=out,
    )
    if no_reclaim:
        return "archived"
    reason = reclaim_unit(unit, rec, cfg, now, manifest, state_dir, client)
    if reason:
        print("  keep %s: %s" % (unit.uid, reason), file=out)
        return "archived"
    print("  reclaimed %s" % unit.uid, file=out)
    return "reclaimed"


def apply_pass(
    units: list[Unit],
    cfg: dict,
    client,
    manifest: Manifest,
    state_dir: Path,
    open_set: set,
    out,
    *,
    no_reclaim: bool,
    limit: int | None,
    max_minutes: float,
    now: float,
) -> dict:
    results = {"archived": 0, "reclaimed": 0, "done": 0, "skipped": 0, "errors": 0, "budget": False}
    started = time.monotonic()
    budget = max_minutes * 60.0
    picked = 0
    # Resolve any journal left by a crash before planning new work.
    recover_trash(state_dir, manifest, out)
    for unit in units:
        if limit is not None and picked >= limit:
            break
        if max_minutes > 0 and time.monotonic() - started >= budget:
            results["budget"] = True
            print("budget reached, resume next run", file=out)
            break
        prior = manifest.latest.get(unit.uid)
        if _done(unit, manifest):
            results["done"] += 1
            continue
        if not _idle(unit, cfg, now):
            results["skipped"] += 1
            continue
        if _is_open(unit, open_set):
            results["skipped"] += 1
            continue
        picked += 1
        try:
            outcome = process_unit(
                unit, prior, cfg, client, manifest, state_dir, out, no_reclaim=no_reclaim, now=now
            )
            results[outcome] = results.get(outcome, 0) + 1
        except Exception as exc:  # one unit never stops the run
            results["errors"] += 1
            reason = "%s: %s" % (exc.__class__.__name__, exc)
            # Keep the previous confirmed members, rev and totals so the
            # next run resumes instead of rejecting already-posted rows.
            base = dict(prior) if prior else {}
            base.update(
                {
                    "unit": unit.uid,
                    "tool": unit.tool,
                    "key": unit.key,
                    "status": "error",
                    "error": reason[:500],
                }
            )
            if "members" not in base:
                base["members"] = _fp_members(fingerprint(unit))
            manifest.write(base)
            print("  ERROR %s: %s" % (unit.uid, reason), file=out)
    return results


# --------------------------------------------------------------- census

def _tree_bytes(pattern: str) -> int:
    import glob

    total = 0
    for base in glob.glob(os.path.expanduser(pattern)):
        if os.path.isfile(base) and not os.path.islink(base):
            try:
                total += os.path.getsize(base)
            except OSError:
                pass
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    continue
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
    return total


def _fmt(n: int) -> str:
    return "%.2f GB" % (n / 1e9)


def census(
    units: list[Unit],
    cfg: dict,
    manifest: Manifest,
    open_set: set,
    now: float,
    out,
    meta: dict,
    *,
    sample: int = 3,
) -> tuple[dict, int]:
    """Print the per-tool census. Returns ``(per_tool, planned)``."""
    per: dict[str, dict] = {}
    planned = 0
    for unit in units:
        row = per.setdefault(
            unit.tool,
            {"units": 0, "raw": 0, "eligible": 0, "archived": 0, "reclaim": 0, "reclaim_bytes": 0},
        )
        row["units"] += 1
        row["raw"] += unit.raw_bytes
        prior = manifest.latest.get(unit.uid)
        fp = fingerprint(unit)
        if prior and prior.get("status") in ("archived", "reclaimed") and _fp_equal(prior.get("members"), fp):
            row["archived"] += 1
            if prior.get("status") == "archived" and _reclaim_ready(unit, cfg, now) and not _is_open(unit, open_set):
                row["reclaim"] += 1
                row["reclaim_bytes"] += unit.raw_bytes
                planned += 1
        elif _idle(unit, cfg, now) and not _is_open(unit, open_set):
            row["eligible"] += 1
            planned += 1

    print("mode: dry-run   dest: %s   min-idle: %g h   reclaim: %g d" % (cfg["dest"], cfg["min_idle_hours"], cfg["reclaim_days"]), file=out)
    print("", file=out)
    print(
        "%-12s %7s %12s %10s %10s %10s %12s"
        % ("tool", "units", "raw", "eligible", "archived", "reclaim", "reclaim bytes"),
        file=out,
    )
    for tool in sorted(per):
        row = per[tool]
        print(
            "%-12s %7d %12s %10d %10d %10d %12s"
            % (
                tool,
                row["units"],
                _fmt(row["raw"]),
                row["eligible"],
                row["archived"],
                row["reclaim"],
                _fmt(row["reclaim_bytes"]),
            ),
            file=out,
        )
    if meta.get("absent"):
        print("absent roots: %s" % ", ".join(meta["absent"]), file=out)
    for label, pattern in OUT_OF_SCOPE:
        print("out of scope (%s): %s" % (label, _fmt(_tree_bytes(pattern))), file=out)

    projected = _projected(units, cfg, manifest, open_set, now, sample)
    if projected:
        print("projected archive (first %d eligible units per tool): %s" % (sample, _fmt(projected)), file=out)
    live = live_files([u.tool for u in units])
    if live:
        counts: dict[str, int] = {}
        for tool, _path in live:
            counts[tool] = counts.get(tool, 0) + 1
        print(
            "live history files: %s"
            % ", ".join("%s %d" % (tool, counts[tool]) for tool in sorted(counts)),
            file=out,
        )
    print("", file=out)
    print("planned actions: %d" % planned, file=out)
    return per, planned


def _projected(units, cfg, manifest, open_set, now, sample) -> int:
    """Compress a sample of eligible units into a temp dir and sum the copies."""
    picked: dict[str, int] = {}
    total = 0
    tmpdir = tempfile.mkdtemp(prefix="session-archive-sample-")
    try:
        for unit in units:
            if picked.get(unit.tool, 0) >= sample:
                continue
            prior = manifest.latest.get(unit.uid)
            if prior and prior.get("status") in ("archived", "reclaimed") and _fp_equal(prior.get("members"), fingerprint(unit)):
                continue
            if not _idle(unit, cfg, now) or _is_open(unit, open_set):
                continue
            out_path = os.path.join(tmpdir, "%s-%d.tar.zst" % (unit.tool, picked.get(unit.tool, 0)))
            try:
                pack(unit, out_path)
                total += os.path.getsize(out_path)
            except Exception:
                continue
            picked[unit.tool] = picked.get(unit.tool, 0) + 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return total


# --------------------------------------------------------------- restore

def _find_record(manifest: Manifest, arg: str) -> dict | None:
    if arg in manifest.latest:
        return manifest.latest[arg]
    for rec in manifest.latest.values():
        if rec.get("archive") == arg:
            return rec
        for entry in rec.get("archives") or []:
            if entry.get("path") == arg:
                return rec
    matches = [
        rec
        for rec in manifest.latest.values()
        if rec.get("session") == arg or rec.get("key") == arg or str(rec.get("key", "")).endswith("/" + arg)
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: 0 if r.get("status") in ("archived", "reclaimed") else 1)
    return matches[0]


def _safe_rel(name: str) -> bool:
    """A relative member name with no ``..`` and no absolute prefix."""
    if not name or name.startswith("/") or os.path.isabs(name):
        return False
    parts = name.replace("\\", "/").split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _extract(archive: str, target: str, members: list[dict]) -> None:
    """Extract only manifest members, as regular files, beneath ``target``.

    Rejects absolute names, ``..`` components, names not in the manifest,
    and any non-regular entry (symlink, hardlink, device, fifo). Verifies
    every member's size and sha256 and that every manifest member is
    present. Raises ``ArchiveError`` on any doubt.
    """
    expected = {m["rel"]: m for m in members}
    target_abs = os.path.abspath(target)
    os.makedirs(target_abs, exist_ok=True)
    seen: set[str] = set()
    proc = subprocess.Popen([_zstd_bin(), "-dc", ZSTD_LONG, archive], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for info in tar:
                if info.isdir():
                    if not _safe_rel(info.name):
                        raise ArchiveError("refusing unsafe archive entry %r" % info.name)
                    continue
                if not info.isfile():
                    raise ArchiveError("refusing non-regular archive entry %r" % info.name)
                name = info.name
                if not _safe_rel(name):
                    raise ArchiveError("refusing unsafe archive entry %r" % name)
                exp = expected.get(name)
                if exp is None:
                    raise ArchiveError("archive entry %r is not in the manifest" % name)
                dest = os.path.abspath(os.path.join(target_abs, name))
                if not dest.startswith(target_abs + os.sep):
                    raise ArchiveError("archive entry %r escapes the target" % name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                src = tar.extractfile(info)
                with open(dest, "wb") as fh:
                    while True:
                        chunk = src.read(READ_CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        fh.write(chunk)
                if size != exp["size"] or digest.hexdigest() != exp["sha256"]:
                    raise ArchiveError("restored %s does not match the manifest" % name)
                seen.add(name)
    finally:
        proc.stdout.close()
        proc.wait()
    missing = sorted(set(expected) - seen)
    if missing:
        raise ArchiveError("archive is missing manifest member(s): %s" % ", ".join(missing[:5]))


def restore(arg: str, to: str | None, state_dir: Path, out) -> int:
    manifest = Manifest(state_dir)
    rec = _find_record(manifest, arg)
    if rec is None:
        print("no archived session matches %r" % arg, file=sys.stderr)
        return 2
    archive = rec.get("archive")
    if not archive or not os.path.exists(archive):
        print("archive missing: %s" % archive, file=sys.stderr)
        return 2
    target = os.path.expanduser(to) if to else rec.get("parent")
    if not target:
        print("no target dir; pass --to DIR", file=sys.stderr)
        return 2
    members = rec.get("members") or []
    existing = [os.path.join(target, m["rel"]) for m in members if os.path.exists(os.path.join(target, m["rel"]))]
    if existing:
        print("refusing to overwrite %d existing path(s):" % len(existing), file=sys.stderr)
        for path in existing[:20]:
            print("  %s" % path, file=sys.stderr)
        return 2
    target_abs = os.path.abspath(target)
    parent = os.path.dirname(target_abs) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".restore-", dir=parent)
    moved: list[tuple[str, str]] = []
    try:
        _extract(archive, tmp, members)
        # Re-check after extraction: a concurrent writer must not be clobbered.
        existing = [os.path.join(target_abs, m["rel"]) for m in members if os.path.exists(os.path.join(target_abs, m["rel"]))]
        if existing:
            print("refusing to overwrite %d existing path(s):" % len(existing), file=sys.stderr)
            for path in existing[:20]:
                print("  %s" % path, file=sys.stderr)
            return 2
        for m in members:
            src = os.path.join(tmp, m["rel"])
            dst = os.path.join(target_abs, m["rel"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            moved.append((src, dst))
    except (ArchiveError, OSError) as exc:
        # Put back anything already renamed so the target is untouched.
        for src, dst in reversed(moved):
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(dst, src)
            except OSError:
                pass
        print("restore failed, target untouched: %s" % exc, file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    manifest.write(dict(rec, status="restored"))
    print("restored %s to %s" % (rec["unit"], target), file=out)
    return 0


# --------------------------------------------------------------- status

def status(state_dir: Path, out) -> int:
    manifest = Manifest(state_dir)
    per: dict[str, dict] = {}
    for rec in manifest.latest.values():
        tool = rec.get("tool") or str(rec.get("unit", "")).split("/")[0]
        row = per.setdefault(
            tool,
            {
                "archived": 0,
                "reclaimed": 0,
                "restored": 0,
                "reclaiming": 0,
                "error": 0,
                "archive_bytes": 0,
                "reclaimed_bytes": 0,
            },
        )
        st = rec.get("status")
        if st in row:
            row[st] += 1
        if st in ("archived", "reclaimed", "restored"):
            row["archive_bytes"] += int(rec.get("archive_bytes") or 0)
        if st == "reclaimed":
            row["reclaimed_bytes"] += int(rec.get("raw_bytes") or 0)
    print(
        "%-12s %9s %10s %9s %10s %7s %12s %14s"
        % ("tool", "archived", "reclaimed", "restored", "reclaiming", "error", "archive", "reclaimed bytes"),
        file=out,
    )
    for tool in sorted(per):
        row = per[tool]
        print(
            "%-12s %9d %10d %9d %10d %7d %12s %14s"
            % (
                tool,
                row["archived"],
                row["reclaimed"],
                row["restored"],
                row["reclaiming"],
                row["error"],
                _fmt(row["archive_bytes"]),
                _fmt(row["reclaimed_bytes"]),
            ),
            file=out,
        )
    trash = state_dir / "trash"
    if trash.is_dir():
        size = 0
        count = 0
        for dirpath, _dirnames, filenames in os.walk(trash):
            for name in filenames:
                try:
                    size += os.path.getsize(os.path.join(dirpath, name))
                    count += 1
                except OSError:
                    pass
        print("trash: %d file(s), %s (resolved by the next --apply)" % (count, _fmt(size)), file=out)
    last = state_dir / "last_success.json"
    if last.exists():
        try:
            rec = json.loads(last.read_text(encoding="utf-8"))
            print("last success: %s" % rec.get("iso", rec.get("ts")), file=out)
        except (OSError, ValueError):
            pass
    warn = state_dir / "disk_warning.json"
    if warn.exists():
        try:
            rec = json.loads(warn.read_text(encoding="utf-8"))
            print(
                "warning: dest free disk %.1f GB (< %g GB) at %s"
                % (float(rec.get("free_gb", 0)), DISK_WARN_GB, rec.get("iso", rec.get("ts"))),
                file=out,
            )
        except (OSError, ValueError, TypeError):
            pass
    errors = [r for r in manifest.latest.values() if r.get("status") == "error"]
    for rec in errors[-5:]:
        print("error %s: %s" % (rec.get("unit"), rec.get("error")), file=out)
    return 0


# --------------------------------------------------------------- scheduled

def _free_disk_gb(path) -> float | None:
    """Free space in GB on the volume holding ``path`` (nearest existing parent)."""
    probe = Path(path)
    while True:
        try:
            return shutil.disk_usage(str(probe)).free / 1e9
        except OSError:
            if probe.parent == probe:
                return None
            probe = probe.parent


def _write_disk_warning(state_dir: Path, now: float, free_gb: float) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "disk_warning.json"
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {"ts": round(now, 3), "iso": iso_from_ms(int(now * 1000)), "free_gb": round(free_gb, 2)},
            fh,
        )
        fh.write("\n")
    os.replace(tmp, path)


def _clear_disk_warning(state_dir: Path) -> None:
    try:
        (state_dir / "disk_warning.json").unlink()
    except FileNotFoundError:
        pass


def _memory_free_pct() -> int | None:
    try:
        proc = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"free percentage:\s*(\d+)%", proc.stdout)
    return int(match.group(1)) if match else None


def _on_battery() -> bool:
    try:
        proc = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in proc.stdout


def scheduled_skip(state_dir: Path, now: float) -> str | None:
    last = state_dir / "last_success.json"
    if last.exists():
        try:
            rec = json.loads(last.read_text(encoding="utf-8"))
            ts = float(rec.get("ts", 0))
            if now - ts < 20 * 3600:
                return "last success %.1f h ago (< 20 h)" % ((now - ts) / 3600.0)
        except (OSError, ValueError, TypeError):
            pass
    pct = _memory_free_pct()
    if pct is not None and pct < 30:
        return "memory free %d%% (< 30%%)" % pct
    if _on_battery():
        return "on battery power"
    return None


# --------------------------------------------------------------- driver

def _write_last_success(state_dir: Path, now: float) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "last_success.json"
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"ts": round(now, 3), "iso": iso_from_ms(int(now * 1000))}, fh)
        fh.write("\n")
    os.replace(tmp, path)


def run(opts: dict, out=None, *, state: Path | None = None, now: float | None = None) -> int:
    """One pass. Returns 0 ok, 1 failures or empty dry run, 2 bad options,
    3 lock held, 5 store unreachable."""
    out = out or sys.stdout
    state_dir = Path(state) if state is not None else Path(_state_dir())
    cfg = load_config(state_dir)
    if opts.get("dest"):
        cfg["dest"] = os.path.expanduser(opts["dest"])
    now = time.time() if now is None else now
    dest = Path(cfg["dest"])

    if opts.get("scheduled"):
        state_dest = dest / "state"
        why = scheduled_skip(state_dest, now)
        if why:
            print("scheduled: skip (%s)" % why, file=out)
            return 0
        free_gb = _free_disk_gb(dest)
        if free_gb is not None and free_gb < DISK_WARN_GB:
            print(
                "WARNING: dest volume has %.1f GB free (< %g GB); running anyway, archiving frees space"
                % (free_gb, DISK_WARN_GB),
                file=out,
            )
            _write_disk_warning(state_dest, now, free_gb)
        else:
            _clear_disk_warning(state_dest)

    tools = opts.get("tools") or cfg.get("tools") or list(TOOLS)
    bad = [t for t in tools if t not in TOOLS]
    if bad:
        print("unknown tool(s): %s" % ", ".join(bad), file=sys.stderr)
        return 2

    units, meta = discover(tools)
    manifest = Manifest(dest / "state")
    open_set = open_paths([m.path for u in units for m in u.members]) if units else set()

    if not opts.get("apply"):
        _per, planned = census(units, cfg, manifest, open_set, now, out, meta)
        if units and planned == 0 and any(not _settled(u, manifest) for u in units):
            print("dry run planned zero actions on real input", file=sys.stderr)
            return 1
        print("dry run: nothing written. Re-run with --apply to archive.", file=out)
        return 0

    client = vlogs.VLogsClient(opts.get("url") or vlogs.DEFAULT_URL)
    if not client.healthy():
        print(
            "VictoriaLogs is not reachable at %s; start it: sh scripts/vlogs_install.sh"
            % (opts.get("url") or vlogs.DEFAULT_URL),
            file=sys.stderr,
        )
        return 5

    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass
    marker = dest / ".metadata_never_index"
    if not marker.exists():
        marker.write_text("", encoding="utf-8")

    state_dir_dest = dest / "state"
    state_dir_dest.mkdir(parents=True, exist_ok=True)
    lock_fh = open(state_dir_dest / "archive.lock", "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fh.close()
        print("another logs sessions run holds %s" % (state_dir_dest / "archive.lock"), file=sys.stderr)
        return 3

    try:
        results = apply_pass(
            units,
            cfg,
            client,
            manifest,
            dest,
            open_set,
            out,
            no_reclaim=bool(opts.get("no_reclaim")),
            limit=opts.get("limit"),
            max_minutes=float(opts.get("max_minutes") or cfg["max_minutes"]),
            now=now,
        )
        live_rows = 0
        for tool, path in live_files(tools):
            try:
                live_rows += ingest_live(tool, path, manifest, client)
            except Exception as exc:
                results["errors"] += 1
                print("  ERROR live %s: %s" % (path, exc), file=out)
        if live_rows:
            print("live history rows sent: %d" % live_rows, file=out)
        print("results: %s" % json.dumps(results, sort_keys=True), file=out)
        if results["errors"] == 0 and not results["budget"]:
            _write_last_success(state_dir_dest, now)
        return 1 if results["errors"] else 0
    finally:
        lock_fh.close()


_DEFAULTS = {
    "apply": False,
    "no_reclaim": False,
    "scheduled": False,
    "tools": [],
    "limit": None,
    "max_minutes": None,
    "url": None,
    "dest": None,
}


def parse_args(args: list[str]):
    opts = dict(_DEFAULTS, tools=[])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--apply":
            opts["apply"] = True
        elif arg == "--no-reclaim":
            opts["no_reclaim"] = True
        elif arg == "--scheduled":
            opts["scheduled"] = True
        elif arg in ("--tool", "--limit", "--max-minutes", "--url", "--dest"):
            if i + 1 >= len(args):
                return "%s needs a value" % arg
            value = args[i + 1]
            i += 1
            if arg == "--tool":
                if value not in TOOLS:
                    return "--tool must be one of %s" % ", ".join(TOOLS)
                opts["tools"].append(value)
            elif arg == "--limit":
                try:
                    opts["limit"] = int(value)
                except ValueError:
                    return "--limit needs an integer"
            elif arg == "--max-minutes":
                try:
                    opts["max_minutes"] = float(value)
                except ValueError:
                    return "--max-minutes needs a number"
            elif arg == "--url":
                opts["url"] = value
            else:
                opts["dest"] = value
        else:
            return "unknown logs sessions argument: %s" % arg
        i += 1
    return opts


def _cmd_restore(args: list[str], out) -> int:
    target = None
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--to":
            if i + 1 >= len(args):
                print("--to needs a value", file=sys.stderr)
                return 2
            target = args[i + 1]
            i += 2
            continue
        positional.append(args[i])
        i += 1
    if len(positional) != 1:
        print("usage: logs sessions restore <session-id|unit|archive> [--to DIR]", file=sys.stderr)
        return 2
    dest = Path(load_config()["dest"])
    return restore(positional[0], target, dest / "state", out)


def _cmd_status(args: list[str], out) -> int:
    if args:
        print("usage: logs sessions status", file=sys.stderr)
        return 2
    dest = Path(load_config()["dest"])
    return status(dest / "state", out)


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "restore":
        return _cmd_restore(args[1:], out)
    if args and args[0] == "status":
        return _cmd_status(args[1:], out)
    opts = parse_args(args)
    if isinstance(opts, str):
        print(opts, file=sys.stderr)
        return 2
    return run(opts, out=out)
