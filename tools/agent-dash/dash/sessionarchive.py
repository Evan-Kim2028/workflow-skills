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
  member costs bounded memory.
* ``verify_archive`` streams the copy back through ``tarfile`` and compares
  every member's sha256 and size before the atomic rename.
* ``ingest_unit`` sends only new lines on append growth, bumps ``rev`` on a
  rewrite, and verifies the stored row count for ``(unit, rev)``.
* ``reclaim_unit`` re-stats and re-hashes the members, refuses on any
  doubt, and deletes exactly the unit.

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
ZSTD_LEVEL = "-12"
ZSTD_LONG = "--long=27"
ZSTD_THREADS = "-T2"
READ_CHUNK = 1 << 20
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
    units = []
    if not _isdir(root):
        return units
    for sub in ("sessions", "archived_sessions"):
        base = os.path.join(root, sub)
        if not _isdir(base):
            continue
        for path, rel in _walk_files(base):
            name = os.path.basename(path)
            if not name.startswith("rollout-"):
                continue
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                continue
            match = _CODEX_RE.match(name)
            session = match.group(1) if match else name
            units.append(
                _finish(
                    Unit(
                        "codex",
                        _rel(sub, rel),
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


def _split_lines(fh):
    buf = b""
    while True:
        chunk = fh.read(READ_CHUNK)
        if not chunk:
            break
        buf += chunk
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            yield buf[start:nl]
            start = nl + 1
        buf = buf[start:]
    if buf:
        yield buf


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
    """Yield ``(text, type, ms, project, model, subtype)`` for one member."""
    fallback = int(unit.newest_mtime * 1000)
    if member.rows == "cursor-store":
        for text, type_, ms in _cursor_rows(member, fallback):
            yield text, type_, ms, "", "", ""
        return
    if member.rows not in ("jsonl", "log"):
        return
    seen = 0
    for line in _iter_lines(member.path):
        if seen < skip:
            seen += 1
            continue
        seen += 1
        info = extract(unit.tool, line, member, fallback)
        yield line.decode("utf-8", "replace"), info["type"], info["ms"], info["project"], info["model"], info["subtype"]


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


def _live_row(tool: str, uid: str, rel: str, line: bytes, rev: str) -> bytes:
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
    extra = {"tool": tool, "session": session, "unit": uid, "member": rel, "rev": rev}
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
    left for the next run. Returns the number of rows sent.
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
    if prior and (size < offset or prefix != prior.get("prefix_sha256")):
        offset = 0
        rev = str(int(rev) + 1)
    if offset > size:
        offset = 0
    new_offset = offset
    sent = 0
    if size > offset:
        def gen():
            nonlocal new_offset
            pos = offset
            with open(path, "rb") as fh:
                fh.seek(offset)
                buf = b""
                while True:
                    chunk = fh.read(READ_CHUNK)
                    if not chunk:
                        break
                    buf += chunk
                    start = 0
                    while True:
                        nl = buf.find(b"\n", start)
                        if nl < 0:
                            break
                        line = buf[start:nl]
                        start = nl + 1
                        pos += len(line) + 1
                        new_offset = pos
                        yield _live_row(tool, uid, rel, line, rev)
                    buf = buf[start:]

        sent = client.insert(gen())
        client.flush()
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
            "lines": int((prior or {}).get("lines", 0)) + sent,
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

    A rerun that finds the store holding exactly the manifest's row count
    sends only the lines after the recorded count (append growth). A member
    whose old bytes are not a prefix of the new file is a rewrite: the unit
    ``rev`` is bumped and every member is resent at the new rev.
    """
    prior_members = {m["rel"]: m for m in (prior or {}).get("members", [])}
    prior_rev = str((prior or {}).get("rev", "0"))
    prior_total = sum(int(m.get("lines") or 0) for m in prior_members.values())
    stored = count_rows(client, unit.uid)
    total_all = sum(int(r.get("lines") or 0) for r in records)
    if prior is None:
        if stored not in (0, total_all):
            raise ArchiveError("store has %d rows for %s with no manifest record" % (stored, unit.uid))
        if stored and stored == total_all:
            # A previous run posted every row and died before the manifest write.
            got = count_rows(client, unit.uid, prior_rev)
            if got != total_all:
                raise ArchiveError(
                    "store has %d rows for %s rev %s, expected %d" % (got, unit.uid, prior_rev, total_all)
                )
            return prior_rev, total_all, 0
    if prior is not None and stored != prior_total:
        raise ArchiveError(
            "store has %d rows for %s, manifest records %d" % (stored, unit.uid, prior_total)
        )

    rev = prior_rev
    plan: list[tuple[Member, dict, int]] = []
    rewrite = False
    by_rel = {r["rel"]: r for r in records}
    for member in unit.members:
        rec = by_rel.get(member.rel)
        if rec is None:
            continue
        old = prior_members.get(member.rel)
        if old is None:
            plan.append((member, rec, 0))
            continue
        if old.get("sha256") == rec["sha256"] and old.get("size") == rec["size"]:
            continue
        if _prefix_matches(member.path, old):
            plan.append((member, rec, int(old.get("lines") or 0)))
        else:
            rewrite = True
            break
    if rewrite:
        rev = str(int(prior_rev) + 1)
        plan = [(m, by_rel[m.rel], 0) for m in unit.members if m.rel in by_rel]

    sent = 0
    if plan:
        def gen():
            for member, _rec, skip in plan:
                for text, type_, ms, project, model, subtype in iter_member_rows(member, unit, skip):
                    extra = {
                        "tool": unit.tool,
                        "session": unit.session,
                        "project": project,
                        "model": model,
                        "subtype": subtype,
                        "unit": unit.uid,
                        "member": member.rel,
                        "rev": rev,
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

    total = sum(int(r.get("lines") or 0) for r in records)
    got = count_rows(client, unit.uid, rev)
    if got != total:
        raise ArchiveError("store has %d rows for %s rev %s, expected %d" % (got, unit.uid, rev, total))
    return rev, total, sent


# --------------------------------------------------------------- reclaim

def _remove_unit(unit: Unit) -> None:
    for member in unit.members:
        try:
            os.remove(member.path)
        except FileNotFoundError:
            pass
    if unit.is_dir and unit.unit_dir:
        for dirpath, _dirnames, _filenames in os.walk(unit.unit_dir, topdown=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def reclaim_unit(unit: Unit, rec: dict, cfg: dict, now: float) -> str | None:
    """Delete the unit's members when every check passes. Returns a reason to keep."""
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
    _remove_unit(unit)
    return None


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


def archive_path(state_dir: Path, unit: Unit) -> Path:
    return Path(state_dir) / "archive" / unit.tool / (unit.key + ".tar.zst")


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
    return bool(prior) and prior.get("status") == "reclaimed" and _fp_equal(prior.get("members"), fingerprint(unit))


def _settled(unit: Unit, manifest: Manifest) -> bool:
    """Archived or reclaimed with a matching fingerprint: nothing to plan."""
    prior = manifest.latest.get(unit.uid)
    return (
        bool(prior)
        and prior.get("status") in ("archived", "reclaimed")
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
    if prior and prior.get("status") == "reclaimed" and _fp_equal(prior.get("members"), fp):
        return "done"
    if prior and prior.get("status") == "archived" and _fp_equal(prior.get("members"), fp):
        if no_reclaim:
            return "archived"
        reason = reclaim_unit(unit, prior, cfg, now)
        if reason:
            print("  keep %s: %s" % (unit.uid, reason), file=out)
            return "archived"
        manifest.write(dict(prior, status="reclaimed"))
        print("  reclaimed %s" % unit.uid, file=out)
        return "reclaimed"

    archive = archive_path(state_dir, unit)
    archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(archive) + ".tmp"
    try:
        records = pack(unit, tmp)
        for rec, member in zip(records, unit.members):
            if rec["lines"] is None:
                rec["lines"] = count_member_rows(member, unit)
        verify_archive(tmp, records)
        os.replace(tmp, archive)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

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
        "raw_bytes": unit.raw_bytes,
        "rev": rev,
        "lines": total,
        "members": records,
        "status": "archived",
    }
    manifest.write(rec)
    print(
        "  archived %s  raw %d -> zst %d  rows %d (sent %d, rev %s)"
        % (unit.uid, unit.raw_bytes, rec["archive_bytes"], total, sent, rev),
        file=out,
    )
    if no_reclaim:
        return "archived"
    reason = reclaim_unit(unit, rec, cfg, now)
    if reason:
        print("  keep %s: %s" % (unit.uid, reason), file=out)
        return "archived"
    manifest.write(dict(rec, status="reclaimed"))
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
            manifest.write(
                {
                    "unit": unit.uid,
                    "tool": unit.tool,
                    "key": unit.key,
                    "status": "error",
                    "error": reason[:500],
                    "members": _fp_members(fingerprint(unit)),
                }
            )
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
    matches = [
        rec
        for rec in manifest.latest.values()
        if rec.get("session") == arg or rec.get("key") == arg or str(rec.get("key", "")).endswith("/" + arg)
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: 0 if r.get("status") in ("archived", "reclaimed") else 1)
    return matches[0]


def _extract(archive: str, target: str, members: list[dict]) -> None:
    expected = {m["rel"]: m for m in members}
    proc = subprocess.Popen([_zstd_bin(), "-dc", ZSTD_LONG, archive], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for info in tar:
                if info.isdir():
                    os.makedirs(os.path.join(target, info.name), exist_ok=True)
                    continue
                if not info.isfile():
                    continue
                dest = os.path.join(target, info.name)
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
                exp = expected.get(info.name)
                if exp and (size != exp["size"] or digest.hexdigest() != exp["sha256"]):
                    raise ArchiveError("restored %s does not match the manifest" % info.name)
    finally:
        proc.stdout.close()
        proc.wait()


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
    os.makedirs(target, exist_ok=True)
    _extract(archive, target, members)
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
            {"archived": 0, "reclaimed": 0, "restored": 0, "error": 0, "archive_bytes": 0, "reclaimed_bytes": 0},
        )
        st = rec.get("status")
        if st in row:
            row[st] += 1
        if st in ("archived", "reclaimed", "restored"):
            row["archive_bytes"] += int(rec.get("archive_bytes") or 0)
        if st == "reclaimed":
            row["reclaimed_bytes"] += int(rec.get("raw_bytes") or 0)
    print("%-12s %9s %10s %9s %7s %12s %14s" % ("tool", "archived", "reclaimed", "restored", "error", "archive", "reclaimed bytes"), file=out)
    for tool in sorted(per):
        row = per[tool]
        print(
            "%-12s %9d %10d %9d %7d %12s %14s"
            % (
                tool,
                row["archived"],
                row["reclaimed"],
                row["restored"],
                row["error"],
                _fmt(row["archive_bytes"]),
                _fmt(row["reclaimed_bytes"]),
            ),
            file=out,
        )
    last = state_dir / "last_success.json"
    if last.exists():
        try:
            rec = json.loads(last.read_text(encoding="utf-8"))
            print("last success: %s" % rec.get("iso", rec.get("ts")), file=out)
        except (OSError, ValueError):
            pass
    errors = [r for r in manifest.latest.values() if r.get("status") == "error"]
    for rec in errors[-5:]:
        print("error %s: %s" % (rec.get("unit"), rec.get("error")), file=out)
    return 0


# --------------------------------------------------------------- scheduled

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
        why = scheduled_skip(dest / "state", now)
        if why:
            print("scheduled: skip (%s)" % why, file=out)
            return 0

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
