"""Rebuild the VictoriaLogs store from the lossless archives, and cut over.

The store is a rebuildable index, not a system of record. The session
archive (one ``tar.zst`` per unit under ``~/.local/share/agent-sessions/archive``)
and the log archive (one ``zst`` per file under
``~/.local/share/agent-dash/log-archive``) hold the original bytes. This
module re-ingests both into a second VictoriaLogs instance on a scratch
port and data dir, verifies it against the live store per workstream, and
prints a cutover plan. ``cutover`` swaps the directories under the live
launchd service and swaps back on failure.

Neither command deletes history. The old data dir is renamed, never
removed, and the exact ``rm -rf`` is printed for the owner. No flag here
sets a retention or disk-usage cap: the store keeps every row it is given.

Pieces:

* ``Store`` starts and stops one VictoriaLogs process with the live
  plist's flags (minus the listen address and data path).
* ``session_jobs`` and ``log_jobs`` turn the two manifests into jobs.
* ``rebuild`` ingests them with N parallel insert streams, checkpoints
  per unit, and verifies per ``ws``: row counts and assistant token sums
  must equal the live store's.
* ``cutover`` pauses the hourly archive job, re-ingests anything archived
  after the rebuild's snapshot, swaps the data dirs, restarts the service,
  re-verifies against the live port, and swaps back on failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import logarchive, logfilter, sessionarchive, vlogs

DEFAULT_BIN = "~/.local/share/agent-dash/vlogs/bin/victoria-logs-prod"
DEFAULT_PLIST = "~/Library/LaunchAgents/com.agent-dash.vlogs.plist"
DEFAULT_LIVE_DATA = "~/.local/share/agent-sessions/vlogs"
DEFAULT_LIVE_PORT = 19428
DEFAULT_PARALLEL = 4
DEFAULT_MEMCAP_MB = 4096
VLOGS_LABEL = "com.agent-dash.vlogs"
ARCHIVE_LABEL = "com.agent-dash.session-archive"

#: Flags used when the live plist cannot be read. They match the plist the
#: installer writes, including the free-disk floor: a full disk makes the
#: store read-only and loud instead of failing inserts silently.
BASE_FLAGS = (
    "-retentionPeriod=90d",
    "-memory.allowedBytes=1GiB",
    "-insert.maxLineSizeBytes=8MiB",
    "-storage.minFreeDiskSpaceBytes=20GiB",
)

#: The token fields the store's assistant rows carry (``logarchive`` unpacks
#: the same four). The parity check sums them per workstream.
TOKEN_FIELDS = (
    "message.role",
    "message.usage.input",
    "message.usage.output",
    "message.usage.cacheRead",
)


def _expand(path) -> str:
    return os.path.expanduser(str(path))


def _state_dir() -> Path:
    return Path(_expand(os.environ.get("AGENT_DASH_STATE_DIR") or "~/.local/share/agent-dash"))


def _fmt(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


# ------------------------------------------------------------- the process

def live_flags(plist=None) -> list[str]:
    """The live plist's VictoriaLogs flags, minus the listen address and data path.

    A rebuild must run the same binary with the same flags as the service it
    will replace, or the verification compares two different stores. The
    listen address and data path are the two the caller overrides.
    """
    path = Path(_expand(plist or DEFAULT_PLIST))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return list(BASE_FLAGS)
    flags = []
    for arg in re.findall(r"<string>([^<]*)</string>", text):
        if not arg.startswith("-"):
            continue
        if arg.startswith("-httpListenAddr=") or arg.startswith("-storageDataPath="):
            continue
        flags.append(arg)
    return flags or list(BASE_FLAGS)


def live_data_dir(plist=None) -> str:
    """The live store's data dir, from the plist's ``-storageDataPath``."""
    path = Path(_expand(plist or DEFAULT_PLIST))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _expand(DEFAULT_LIVE_DATA)
    m = re.search(r"<string>-storageDataPath=([^<]*)</string>", text)
    return _expand(m.group(1)) if m else _expand(DEFAULT_LIVE_DATA)


def live_port(plist=None) -> int:
    """The live store's port, from the plist's ``-httpListenAddr``."""
    path = Path(_expand(plist or DEFAULT_PLIST))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_LIVE_PORT
    m = re.search(r"<string>-httpListenAddr=[^:<]*:(\d+)</string>", text)
    return int(m.group(1)) if m else DEFAULT_LIVE_PORT


def _healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Store:
    """One VictoriaLogs process on a loopback port and a data dir."""

    def __init__(self, bin_path, data_dir, port, flags=(), log_path=None):
        self.bin_path = _expand(bin_path)
        self.data_dir = _expand(data_dir)
        self.port = int(port)
        self.flags = list(flags)
        self.log_path = _expand(log_path) if log_path else None
        self.proc = None
        self._log = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def start(self, wait_s: float = 180.0) -> None:
        if _healthy(self.url):
            return
        if not os.path.isfile(self.bin_path):
            raise RuntimeError("VictoriaLogs binary not found: %s" % self.bin_path)
        os.makedirs(self.data_dir, exist_ok=True)
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            self._log = open(self.log_path, "ab")
            sink = self._log
        else:
            sink = subprocess.DEVNULL
        argv = [
            self.bin_path,
            "-httpListenAddr=127.0.0.1:%d" % self.port,
            "-storageDataPath=%s" % self.data_dir,
            *self.flags,
        ]
        self.proc = subprocess.Popen(argv, stdout=sink, stderr=sink, stdin=subprocess.DEVNULL)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "VictoriaLogs exited %s on port %d; see %s"
                    % (self.proc.returncode, self.port, self.log_path or "its output")
                )
            if _healthy(self.url):
                return
            time.sleep(0.25)
        self.stop()
        raise RuntimeError("VictoriaLogs did not become healthy on port %d in %ds" % (self.port, wait_s))

    def stop(self, timeout: float = 30.0) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


class LaunchdService:
    """``launchctl`` control for one agent, by label."""

    def __init__(self, label, plist=None, uid=None):
        self.label = label
        self.plist = _expand(plist) if plist else None
        self.uid = os.getuid() if uid is None else int(uid)

    def _run(self, *args) -> int:
        try:
            return subprocess.run(
                ["launchctl", *args], capture_output=True, text=True, timeout=60
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return 127

    def stop(self, wait_s: float = 30.0) -> int:
        """Boot the agent out and wait until launchd has really unloaded it.

        A ``bootstrap`` issued while launchd is still tearing the job down fails
        with "Bootstrap failed: 5: Input/output error" (seen live on 2026-11-21:
        a cutover's restart never happened and the swap-back fired).
        """
        rc = self._run("bootout", "gui/%d/%s" % (self.uid, self.label))
        deadline = time.time() + wait_s
        while self.running() and time.time() < deadline:
            time.sleep(0.5)
        return rc

    def start(self, attempts: int = 5, pause_s: float = 2.0) -> int:
        if not self.plist:
            return 1
        rc = 1
        for attempt in range(attempts):
            rc = self._run("bootstrap", "gui/%d" % self.uid, self.plist)
            if rc == 0 or self.running():
                return 0
            time.sleep(pause_s * (attempt + 1))
        return rc

    def running(self) -> bool:
        return self._run("print", "gui/%d/%s" % (self.uid, self.label)) == 0


# ------------------------------------------------------------------- jobs

@dataclass
class Job:
    """One unit of rebuild work: a session-archive unit or a log-archive file."""

    key: str
    kind: str  # "session" | "log"
    ws: str
    rows: int
    ts: float = 0.0
    unit: dict = field(default_factory=dict)
    archive: str = ""
    entry: object = None
    zst: str = ""


def session_jobs(dest, state_dir=None) -> list[Job]:
    """One job per archived session unit that still has its tar.zst.

    The session manifest lives at ``<dest>/state/manifest.jsonl``, not in the
    agent-dash state dir, so ``state_dir`` defaults to ``<dest>/state``.
    """
    state = Path(state_dir) if state_dir is not None else Path(_expand(dest)) / "state"
    manifest = sessionarchive.Manifest(state)
    jobs: list[Job] = []
    # Every revision, not just the newest: a unit that grew after it was
    # packed is archived again with a higher rev, and the store keeps both
    # (``rev`` is part of its dedupe key), so a rebuild must too.
    for (uid, rev), rec in sorted(manifest.records.items()):
        if rec.get("status") not in ("archived", "reclaimed", "restored"):
            continue
        archive = sessionarchive.resolve_stored_path(
            dest, rec.get("archive_rel"), rec.get("archive")
        )
        if not archive or not os.path.exists(archive):
            continue
        jobs.append(
            Job(
                key="session:%s:%s" % (uid, rev),
                kind="session",
                ws="native/" + str(rec.get("tool") or ""),
                rows=int(rec.get("lines") or 0),
                ts=float(rec.get("ts") or 0.0),
                unit=rec,
                archive=archive,
            )
        )
    return jobs


def skipped_session_records(dest) -> tuple[int, int]:
    """``(revisions, recorded rows)`` the manifest has but no archive backs."""
    state = Path(_expand(dest)) / "state"
    manifest = sessionarchive.Manifest(state)
    count = rows = 0
    for rec in manifest.records.values():
        if rec.get("status") not in ("archived", "reclaimed", "restored"):
            count += 1
            continue
        archive = sessionarchive.resolve_stored_path(
            dest, rec.get("archive_rel"), rec.get("archive")
        )
        if not archive or not os.path.exists(archive):
            count += 1
            rows += int(rec.get("lines") or 0)
    return count, rows


def log_jobs(state_dir=None) -> list[Job]:
    """One job per archived log-archive file that still has its zst."""
    state = Path(state_dir) if state_dir is not None else _state_dir()
    root = logarchive.archive_root(state)
    manifest = logarchive.Manifest(root)
    jobs: list[Job] = []
    for rel, rec in sorted(manifest.latest.items()):
        if rec.get("status") not in ("archived", "reclaimed"):
            continue
        zst = root / (rel + ".zst")
        if not zst.exists():
            continue
        extra = rec.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        mtime_ns = int(rec.get("mtime_ns") or 0)
        entry = logarchive.Entry(
            path=str(rec.get("source") or ""),
            root="",
            rel=rel,
            ws=str(rec.get("ws") or ""),
            label=str(rec.get("label") or ""),
            kind=str(rec.get("kind") or "stream"),
            size=int(rec.get("size") or 0),
            mtime_ns=mtime_ns,
            mtime=mtime_ns / 1e9,
            outdir="",
            tool=str(rec.get("tool") or "other"),
            extra=extra,
        )
        jobs.append(
            Job(
                key="log:" + rel,
                kind="log",
                ws=entry.ws,
                rows=int(rec.get("stripped_lines") or 0),
                ts=float(rec.get("ts") or 0.0),
                entry=entry,
                zst=str(zst),
            )
        )
    return jobs


# ---------------------------------------------------------------- ingest

def _unit_stub(rec: dict) -> sessionarchive.Unit:
    """A ``Unit`` carrying only what the row reader needs from a manifest record."""
    return sessionarchive.Unit(
        tool=str(rec.get("tool") or ""),
        key=str(rec.get("key") or ""),
        root=str(rec.get("root") or ""),
        parent=str(rec.get("parent") or ""),
        session=str(rec.get("session") or ""),
        newest_mtime=float(rec.get("newest_mtime") or rec.get("ts") or 0.0),
    )


def _session_rows(job: Job, member_rel: str, rows):
    """Store rows for one archived member, stripped exactly as the live ingest does."""
    rec = job.unit
    tool = str(rec.get("tool") or "")
    uid = str(rec.get("unit") or "")
    session = str(rec.get("session") or "")
    rev = str(rec.get("rev") or "0")
    for text, type_, ms, project, model, subtype, line_no in rows:
        text, _removed, _replaced = logfilter.strip_text(text)
        extra = {
            "tool": tool,
            "project": project,
            "model": model,
            "subtype": subtype,
            "member": member_rel,
            "rev": rev,
            "line": str(line_no),
        }
        line, _truncated, _redactions = vlogs.make_row(
            time_iso=sessionarchive.iso_from_ms(ms),
            msg=text,
            ws="native/" + tool,
            label=session,
            type_=type_,
            kind="native",
            path=uid,
            extra=extra,
        )
        yield line


def _ingest_cursor_member(job: Job, mrec: dict, src, client) -> int:
    """A cursor sqlite store is read from a temp copy, as the live ingest does."""
    tmp = tempfile.mkdtemp(prefix="storebuild-cursor-")
    try:
        path = os.path.join(tmp, "store.db")
        with open(path, "wb") as fh:
            shutil.copyfileobj(src, fh)
        member = sessionarchive.Member(path=path, rel=mrec["rel"], rows="cursor-store")
        rows = sessionarchive.iter_member_rows(member, _unit_stub(job.unit))
        return client.insert(_session_rows(job, mrec["rel"], rows))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ingest_session_job(job: Job, client) -> int:
    """Re-ingest one archived unit from its tar.zst. Returns rows sent."""
    rec = job.unit
    tool = str(rec.get("tool") or "")
    members = {m["rel"]: m for m in (rec.get("members") or [])}
    fallback = int(float(rec.get("newest_mtime") or rec.get("ts") or 0.0) * 1000)
    sent = 0
    proc = subprocess.Popen(
        [sessionarchive._zstd_bin(), "-dc", sessionarchive.ZSTD_LONG, job.archive],
        stdout=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for info in tar:
                if not info.isfile():
                    continue
                mrec = members.get(info.name)
                if mrec is None:
                    continue
                rows_kind = mrec.get("rows") or "none"
                src = tar.extractfile(info)
                if src is None:
                    continue
                if rows_kind == "cursor-store":
                    sent += _ingest_cursor_member(job, mrec, src, client)
                elif rows_kind in ("jsonl", "log"):
                    sent += client.insert(
                        _session_rows(job, mrec["rel"], _member_rows(tool, mrec, src, fallback))
                    )
    finally:
        proc.stdout.close()
        proc.wait()
    return sent


def _member_rows(tool: str, mrec: dict, src, fallback_ms: int):
    """Rows from one tar member, decompressed when the member file is packed.

    Codex and claude keep some histories as ``.jsonl.zst`` / ``.log.zst``;
    the tar stores the file's bytes as-is (losslessly), so the row reader has
    to decompress it exactly as ``sessionarchive.iter_member_rows`` does for a
    live file. Reading the compressed bytes as text yields garbage rows.
    """
    rel = str(mrec.get("rel") or "")
    rows_kind = mrec.get("rows") or "none"
    if rows_kind not in ("jsonl", "log"):
        return
    packer = None
    if rel.endswith(".zst"):
        packer = [sessionarchive._zstd_bin(), "-dc", sessionarchive.ZSTD_LONG]
    elif rel.endswith(".gz"):
        packer = ["gzip", "-dc"]
    if packer is None:
        for row in sessionarchive.iter_stream_rows(tool, rows_kind, src, fallback_ms):
            yield row
        return
    # A tar member has no fileno(), so the packer is fed by a thread.
    proc = subprocess.Popen(packer, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def feed():
        try:
            shutil.copyfileobj(src, proc.stdin, 1 << 20)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=feed, daemon=True).start()
    try:
        for row in sessionarchive.iter_stream_rows(tool, rows_kind, proc.stdout, fallback_ms):
            yield row
    finally:
        proc.stdout.close()
        proc.wait()


def _log_rows(job: Job):
    for line, _acct in logarchive.row_lines(job.entry, job.zst):
        yield line


def ingest_log_job(job: Job, client) -> int:
    """Re-ingest one archived log-archive file from its zst. Returns rows sent."""
    return client.insert(_log_rows(job))


def ingest_job(job: Job, client) -> int:
    if job.kind == "session":
        return ingest_session_job(job, client)
    return ingest_log_job(job, client)


# ------------------------------------------------------------ checkpoint

class Checkpoint:
    """Append-only ``{key: rows}`` log, so a rebuild resumes where it stopped."""

    def __init__(self, path):
        self.path = Path(path)
        self.done: dict = {}
        self._lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("key"):
                        self.done[rec["key"]] = rec
        except OSError:
            pass

    def write(self, key: str, rows: int, **extra) -> None:
        rec = {"key": key, "rows": int(rows), "ts": round(time.time(), 3)}
        rec.update(extra)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.done[key] = rec


def sidecar_dir(data_dir) -> Path:
    """Where the checkpoint and the snapshot live: beside the data dir, not in it."""
    return Path(_expand(data_dir) + ".rebuild")


def read_meta(data_dir) -> dict:
    try:
        with open(sidecar_dir(data_dir) / "meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def write_meta(data_dir, meta: dict) -> None:
    path = sidecar_dir(data_dir) / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------- verification

def _quote(value: str) -> str:
    if '"' in value or "\\" in value:
        raise ValueError("value has a character LogsQL cannot quote: %r" % value)
    return value


def ws_counts(client) -> dict:
    """``{ws: rows}`` for the whole store."""
    rows = client.query("* | stats by (ws) count() n")
    return {str(r.get("ws") or ""): int(r.get("n") or 0) for r in rows}


def ws_path_counts(client) -> dict:
    """``{(ws, path): rows}`` for the whole store, in one query.

    ``path`` is the unit id, so this is what lets ``verify`` tell "the
    rebuild is short" apart from "the live store has rows the archive never
    had".
    """
    rows = client.query("* | stats by (ws, path) count() n")
    return {
        (str(r.get("ws") or ""), str(r.get("path") or "")): int(r.get("n") or 0) for r in rows
    }


def ws_tokens(client, ws: str) -> dict:
    """Assistant token sums for one workstream, the same fields ``logarchive`` checks."""
    fields = ",".join('"%s"' % f for f in TOKEN_FIELDS)
    query = (
        'ws:="%s" | unpack_json from _msg fields (%s) '
        '| filter "message.role":=assistant '
        '| stats count() n, sum("message.usage.input") i, '
        'sum("message.usage.output") o, sum("message.usage.cacheRead") c'
    ) % (_quote(ws), fields)
    rows = client.query(query)
    row = rows[0] if rows else {}

    def num(key):
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            return 0
        return 0 if value != value else int(value)  # NaN -> 0

    return {"messages": num("n"), "input": num("i"), "output": num("o"), "cache_read": num("c")}


def job_path(job: Job) -> str:
    """The store's ``path`` value for a job: the unit id, or the log file's rel."""
    if job.kind == "session":
        return str(job.unit.get("unit") or "")
    return str(getattr(job.entry, "rel", "") or "")


def verify(
    old_client, new_client, out=None, ws_list=None, expected_paths=None
) -> tuple[bool, list[dict]]:
    """Compare the rebuilt store to the live one per workstream.

    Row counts and assistant token sums must match, with one allowance: a
    ``path`` the **archive does not have** is a live-only row (a session
    ingested live but not yet archived) and may be absent from the rebuilt
    store. A path the archive *does* have and the rebuilt store lacks is a
    failure, and so is a count or token mismatch on a shared path.
    Returns ``(ok, table)``; the table is one dict per workstream.
    """
    out = out or sys.stdout
    old_map = ws_path_counts(old_client)
    new_map = ws_path_counts(new_client)
    old_paths = {path for (_ws, path) in old_map}
    new_paths = {path for (_ws, path) in new_map}
    # Without the archive's own path list the two stores must agree exactly.
    expected = {path for path in (expected_paths if expected_paths is not None else old_paths)}
    missing_all = sorted(expected - new_paths)
    # A path the archive has, the live store never had, and the rebuilt store
    # lacks is archive-only: the store rejected its rows (a future timestamp)
    # or the reader produced none, so neither store can hold it. The archive
    # still has the bytes, so it is not a rebuild failure.
    archive_only = [p for p in missing_all if p not in old_paths]
    missing = [p for p in missing_all if p in old_paths]
    # A live-only row: the live store has it and the archive does not, so the
    # rebuild cannot reproduce it. Reported, never counted as loss.
    live_only: dict = {}
    for (ws, path), n in old_map.items():
        if path in expected:
            continue
        if new_map.get((ws, path)) == n:
            # Copied across from the live store (cutover's copy step): the
            # rebuilt store holds every row, so it is not a gap. Counting it
            # as one made a correct cutover fail its check and swap back.
            continue
        live_only[ws] = live_only.get(ws, 0) + n
    # A path the archive has and the live store never ingested is new-only:
    # it adds rows instead of missing them, and it is not a failure.
    new_only: dict = {}
    for (ws, path), n in new_map.items():
        if path in expected and path not in old_paths:
            new_only[ws] = new_only.get(ws, 0) + n
    old_counts: dict = {}
    for (ws, _path), n in old_map.items():
        old_counts[ws] = old_counts.get(ws, 0) + n
    new_counts = ws_counts(new_client)
    names = sorted(set(old_counts) | set(new_counts))
    if ws_list is not None:
        wanted = set(ws_list)
        names = [n for n in names if n in wanted]
    table = []
    ok = not missing
    for ws in names:
        old_n = old_counts.get(ws, 0)
        gap = live_only.get(ws, 0)
        expected_n = old_n - gap + new_only.get(ws, 0)
        new_n = new_counts.get(ws, 0)
        old_t = ws_tokens(old_client, ws) if old_n else {"messages": 0, "input": 0, "output": 0, "cache_read": 0}
        new_t = ws_tokens(new_client, ws) if new_n else {"messages": 0, "input": 0, "output": 0, "cache_read": 0}
        rows_ok = expected_n == new_n
        # A workstream with live-only rows cannot have an equal token sum:
        # those rows carry usage that the archive does not hold.
        tokens_ok = None if gap else old_t == new_t
        ok = ok and rows_ok and tokens_ok is not False
        table.append(
            {
                "ws": ws,
                "old_rows": old_n,
                "live_only_rows": gap,
                "expected_rows": expected_n,
                "new_rows": new_n,
                "rows_ok": rows_ok,
                "old_tokens": old_t,
                "new_tokens": new_t,
                "tokens_ok": tokens_ok,
            }
        )
    print("verification (per ws): rows and assistant token sums", file=out)
    print(
        "  %-30s %10s %9s %10s %10s %4s %9s %9s %4s"
        % ("ws", "old rows", "live-only", "expected", "new rows", "ok", "old msgs", "new msgs", "ok"),
        file=out,
    )
    for row in table:
        flag = "n/a" if row["tokens_ok"] is None else ("yes" if row["tokens_ok"] else "NO")
        print(
            "  %-30s %10d %9d %10d %10d %4s %9d %9d %4s"
            % (
                row["ws"][:30],
                row["old_rows"],
                row["live_only_rows"],
                row["expected_rows"],
                row["new_rows"],
                "yes" if row["rows_ok"] else "NO",
                row["old_tokens"]["messages"],
                row["new_tokens"]["messages"],
                flag,
            ),
            file=out,
        )
        if row["tokens_ok"] is False:
            print(
                "    tokens old %s new %s" % (row["old_tokens"], row["new_tokens"]),
                file=out,
            )
    total_old = sum(r["old_rows"] for r in table)
    total_gap = sum(r["live_only_rows"] for r in table)
    total_expected = sum(r["expected_rows"] for r in table)
    total_new = sum(r["new_rows"] for r in table)
    print(
        "  %-30s %10d %9d %10d %10d %4s"
        % ("TOTAL", total_old, total_gap, total_expected, total_new, "yes" if ok else "NO"),
        file=out,
    )
    if total_gap:
        print(
            "  live-only: %d rows in %d paths the live store has and the archive does not;"
            % (total_gap, len({p for (_w, p) in old_map if p not in expected})),
            file=out,
        )
        print(
            "  they are sessions ingested live but not archived, so a rebuild cannot"
            " reproduce them; archive them first (agent_dash.py logs sessions) or copy"
            " them across at cutover.",
            file=out,
        )
    if missing:
        print(
            "  MISSING: %d archived paths have no rows in the rebuilt store" % len(missing),
            file=out,
        )
        for path in missing[:10]:
            print("    %s" % path, file=out)
    if archive_only:
        print(
            "  archive-only: %d archived paths have no rows in either store"
            " (the store rejected them, e.g. a future timestamp); the archive"
            " still holds the bytes" % len(archive_only),
            file=out,
        )
        for path in archive_only[:10]:
            print("    %s" % path, file=out)
    return ok, table


# ------------------------------------------------------- copy from live

#: Fields a copied row must not carry: the store's own stream bookkeeping,
#: and the duplicate ``unit``/``session`` copies of ``path``/``label`` that
#: the rebuild does not write. ``session`` is a real field on a job-log row
#: (its session id), so it is dropped only on a native row.
COPY_DROP = ("_stream", "_stream_id", "unit")

#: Paths per ``path:in(...)`` query. One query and one flush per batch.
COPY_BATCH_PATHS = 16


def _copy_drop(row: dict) -> set:
    drop = set(COPY_DROP)
    if str(row.get("kind") or "") == "native":
        drop.add("session")
    return drop


def _copy_row(row: dict) -> bytes | None:
    """One live row as a rebuilt-store row: same _time, _msg, ws, label, fields.

    The live store's rows carry ``unit`` and (on a native row) ``session``,
    duplicates of ``path`` and ``label`` that the rebuild does not write, and
    the store's own ``_stream``/``_stream_id``. ``_msg`` is stripped again
    with the same filter the rebuild uses: a no-op on an already-stripped row,
    and the right thing on an older one.
    """
    msg = row.get("_msg")
    if not isinstance(msg, str):
        return None
    text, _removed, _replaced = logfilter.strip_text(msg)
    drop = _copy_drop(row)
    out = {"_time": str(row.get("_time") or ""), "_msg": text}
    for key, value in row.items():
        if key in drop or key in ("_time", "_msg"):
            continue
        if value is None or value == "":
            continue
        out[key] = value
    return json.dumps(out, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def _row_key(row: dict) -> str:
    """A row's identity for the copy: every field the rebuild writes, sorted.

    Used to skip a row the rebuilt store already holds, so a second copy
    pass (the delta after the archive job is paused) adds only new rows
    instead of duplicating the ones the first pass copied.
    """
    drop = _copy_drop(row)
    items = []
    for key, value in row.items():
        if key in drop or value is None or value == "":
            continue
        if key == "_msg":
            value, _removed, _replaced = logfilter.strip_text(value)
        items.append((key, str(value)))
    return json.dumps(sorted(items), ensure_ascii=True, separators=(",", ":"))


def _in_filter(paths) -> str:
    return "path:in(%s)" % ",".join('"%s"' % _quote(p) for p in paths)


def path_counts(client) -> dict:
    """``{path: rows}`` for the whole store, in one query."""
    rows = client.query("* | stats by (path) count() n")
    return {str(r.get("path") or ""): int(r.get("n") or 0) for r in rows}


def copy_plan(live_client, expected_paths) -> tuple[list[str], list[str]]:
    """``(live_only, missing)``: the paths the copy must read from the live store.

    ``live_only`` is a path the live store has and the archive does not: a
    session ingested live but not yet archived, which a rebuild cannot
    reproduce. ``missing`` is a path the archive has and the live store
    lacks: the copy has nothing to read for it, so it is reported and
    skipped (the archive still holds the bytes).
    """
    live = path_counts(live_client)
    expected = set(expected_paths)
    live_only = sorted(p for p in live if p not in expected)
    missing = sorted(p for p in expected if p not in live)
    return live_only, missing


def copy_from_live(live_client, new_client, paths, out=None, batch=COPY_BATCH_PATHS) -> dict:
    """Copy every row of ``paths`` from the live store into the rebuilt one.

    One ``path:in(...)`` query and one flush per batch. A row keeps its
    ``_time``, ``_msg``, ``ws``, ``label`` and every other field, minus the
    store's own ``_stream``/``_stream_id`` and the duplicate ``unit``/
    ``session`` fields the rebuild does not write; ``_msg`` is stripped with
    the same filter the rebuild uses. A row the rebuilt store already holds
    (by ``_row_key``) is skipped, so a second pass adds only the delta.

    Returns ``{path: {live, copied, rebuilt, ok}}``. A path the live store
    has no rows for is ``ok`` with nothing copied: the archive has it and
    the live store never ingested it, so there is nothing to copy.
    """
    out = out or sys.stdout
    live = path_counts(live_client)
    result = {}
    wanted = []
    for path in paths:
        n = live.get(path, 0)
        result[path] = {"live": n, "copied": 0, "rebuilt": 0, "ok": n == 0}
        if n:
            wanted.append(path)
    for start in range(0, len(wanted), batch):
        chunk = wanted[start:start + batch]
        # All fields, and one row more than the live count so a store that
        # grew since the count is visible as a short copy, not a silent cut.
        limit = sum(live[p] for p in chunk) + 1
        rows = live_client.query("%s | limit %d" % (_in_filter(chunk), limit))
        have = {}
        for row in new_client.query(_in_filter(chunk)):
            key = _row_key(row)
            have[key] = have.get(key, 0) + 1
        lines = []
        per_path = {}
        for row in rows:
            path = str(row.get("path") or "")
            key = _row_key(row)
            if have.get(key):
                have[key] -= 1
                continue
            line = _copy_row(row)
            if line is None:
                continue
            lines.append(line)
            per_path[path] = per_path.get(path, 0) + 1
        new_client.insert(lines)
        new_client.flush()
        for path in chunk:
            rebuilt = new_client.count(path)
            result[path]["copied"] = per_path.get(path, 0)
            result[path]["rebuilt"] = rebuilt
            result[path]["ok"] = rebuilt == live[path]
    return result


def copy_only(
    *,
    data_dir,
    port,
    apply: bool = False,
    url: str | None = None,
    state=None,
    dest=None,
    out=None,
    bin_path=None,
    plist=None,
    store=None,
    client_factory=None,
    old_client=None,
    batch=COPY_BATCH_PATHS,
) -> int:
    """Copy the live-only and MISSING paths from the live store into a rebuilt one.

    Standalone form of the cutover's copy step: it starts the rebuilt store
    on its own port, reads the live store (read-only), copies every path the
    archive cannot reproduce, verifies per path, and records the copied
    paths in the rebuild metadata so the cutover's verification expects them.
    """
    out = out or sys.stdout
    state_dir = Path(state) if state is not None else _state_dir()
    dest = dest or sessionarchive.load_config(state_dir)["dest"]
    live_url = url or "http://127.0.0.1:%d" % live_port(plist)
    client_factory = client_factory or (lambda u: vlogs.VLogsClient(u, timeout=600.0))
    meta = read_meta(data_dir)
    expected = meta.get("expected_paths")
    if expected is None:
        jobs = session_jobs(dest) + log_jobs(state_dir)
        expected = sorted({job_path(job) for job in jobs})
    live_client = old_client or client_factory(live_url)
    live_only, missing = copy_plan(live_client, expected)
    live = path_counts(live_client)
    rows = sum(live.get(p, 0) for p in live_only)
    print(
        "copy plan: %d live-only paths (%d rows), %d archived paths the live store lacks"
        % (len(live_only), rows, len(missing)),
        file=out,
    )
    for path in missing[:10]:
        print("  MISSING (nothing to copy): %s" % path, file=out)
    if not apply:
        print("dry run: pass --apply to start the store and copy", file=out)
        return 0
    if not os.path.isdir(data_dir):
        print("rebuilt data dir not found: %s" % data_dir, file=sys.stderr)
        return 2
    side = sidecar_dir(data_dir)
    store = store or Store(
        bin_path or DEFAULT_BIN, data_dir, port, live_flags(plist),
        log_path=str(side / "vlogs.log"),
    )
    store.start()
    try:
        new_client = client_factory(store.url)
        result = copy_from_live(live_client, new_client, live_only + missing, out=out, batch=batch)
        copied = sorted(p for p, r in result.items() if r["copied"])
        bad = sorted(p for p, r in result.items() if not r["ok"])
        print(
            "copied %d rows in %d paths; %d paths short"
            % (sum(r["copied"] for r in result.values()), len(copied), len(bad)),
            file=out,
        )
        for path in bad[:10]:
            r = result[path]
            print("  SHORT %s: live %d, rebuilt %d" % (path, r["live"], r["rebuilt"]), file=out)
        if copied:
            meta["expected_paths"] = sorted(set(expected) | set(copied))
            write_meta(data_dir, meta)
        ok, _table = verify(
            live_client, new_client, out=out, expected_paths=meta.get("expected_paths") or expected
        )
    finally:
        store.stop()
    return 0 if ok and not bad else 1


# --------------------------------------------------------------- rebuild

def _free_bytes(path) -> int:
    try:
        st = os.statvfs(_expand(path))
        return st.f_bavail * st.f_frsize
    except (OSError, AttributeError):
        try:
            usage = shutil.disk_usage(_expand(path))
            return usage.free
        except OSError:
            return 0


def _dir_nonempty(path) -> bool:
    try:
        return any(True for _ in os.scandir(path))
    except OSError:
        return False


_WORKER: dict = {}


def _init_worker(url: str) -> None:
    """One client per pool process; a process cannot share the parent's."""
    _WORKER["client"] = vlogs.VLogsClient(url, timeout=600.0)


def _ingest_worker(job: Job) -> tuple[str, int, int, str | None]:
    """Ingest one job in a pool process. Never raises: the parent records it."""
    try:
        return job.key, ingest_job(job, _WORKER["client"]), job.rows, None
    except Exception as exc:  # one unit never stops the rebuild
        return job.key, 0, job.rows, "%s: %s" % (exc.__class__.__name__, exc)


def rebuild(
    *,
    data_dir,
    port,
    apply: bool = False,
    parallel: int = DEFAULT_PARALLEL,
    url: str | None = None,
    state=None,
    dest=None,
    out=None,
    bin_path=None,
    plist=None,
    store=None,
    client_factory=None,
    old_client=None,
    limit: int | None = None,
    resume: bool = True,
    keep_running: bool = False,
    processes: bool | None = None,
) -> int:
    """Re-ingest both archives into a second store and verify it. Never touches the live one."""
    out = out or sys.stdout
    state_dir = Path(state) if state is not None else _state_dir()
    dest = dest or sessionarchive.load_config(state_dir)["dest"]
    live_url = url or "http://127.0.0.1:%d" % live_port(plist)
    use_processes = processes if processes is not None else client_factory is None
    client_factory = client_factory or (lambda u: vlogs.VLogsClient(u, timeout=600.0))

    jobs = session_jobs(dest) + log_jobs(state_dir)
    if limit is not None:
        jobs = jobs[: int(limit)]
    total_rows = sum(j.rows for j in jobs)
    session_n = sum(1 for j in jobs if j.kind == "session")
    print(
        "rebuild plan: %d session units, %d log files, %d rows"
        % (session_n, len(jobs) - session_n, total_rows),
        file=out,
    )
    print("  data dir %s  port %d  parallel %d" % (_expand(data_dir), port, parallel), file=out)
    print("  live store %s (read-only: verification only)" % live_url, file=out)
    skipped_n, skipped_rows = skipped_session_records(dest)
    if skipped_n:
        print(
            "  skipped %d manifest revisions with no archive on disk (%d recorded rows):"
            " status error, or the tar is gone. The store cannot rebuild them."
            % (skipped_n, skipped_rows),
            file=out,
        )

    if not apply:
        print("dry run: pass --apply to start the store and ingest", file=out)
        return 0

    bin_path = bin_path or DEFAULT_BIN
    if not os.path.isfile(_expand(bin_path)):
        print("VictoriaLogs binary not found: %s" % _expand(bin_path), file=sys.stderr)
        return 2
    if _dir_nonempty(_expand(data_dir)) and not resume:
        print(
            "%s is not empty; pass --resume to continue a rebuild or pick a new dir"
            % _expand(data_dir),
            file=sys.stderr,
        )
        return 2
    free = _free_bytes(os.path.dirname(_expand(data_dir)) or ".")
    if free and free < 12 * 1024**3:
        print(
            "only %s free beside %s; a rebuild needs about 12 GiB"
            % (_fmt(free), _expand(data_dir)),
            file=sys.stderr,
        )
        return 2

    side = sidecar_dir(data_dir)
    side.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint(side / "checkpoint.jsonl")
    meta = read_meta(data_dir)
    if not meta.get("snapshot_ts"):
        meta = {
            "snapshot_ts": round(time.time(), 3),
            "data_dir": _expand(data_dir),
            "port": int(port),
            "jobs": len(jobs),
            "rows": total_rows,
        }
        write_meta(data_dir, meta)
    print("  snapshot ts %s (units archived after it are re-ingested at cutover)" % meta["snapshot_ts"], file=out)

    pending = [j for j in jobs if j.key not in checkpoint.done]
    print(
        "  %d jobs already checkpointed, %d to ingest"
        % (len(jobs) - len(pending), len(pending)),
        file=out,
    )
    if checkpoint.done and not meta.get("complete"):
        # A unit is checkpointed only after its rows are accepted, so a run
        # that stopped cleanly resumes exactly. A killed run leaves the units
        # it had in flight half written, and re-ingesting them would add
        # duplicates, because the store cannot drop one unit's rows.
        print(
            "  WARN the previous run did not finish. Units that were in flight were"
            " partly written and will be ingested again, so this store may hold"
            " duplicate rows for them. For an exact store, re-run with a fresh"
            " --data-dir and --no-resume.",
            file=out,
        )

    store = store or Store(bin_path, data_dir, port, live_flags(plist), log_path=str(side / "vlogs.log"))
    store.start()
    print("  store up at %s" % store.url, file=out)

    local = threading.local()
    print_lock = threading.Lock()
    counters = {"done": 0, "rows": 0, "errors": 0}

    def client_for():
        client = getattr(local, "client", None)
        if client is None:
            client = client_factory(store.url)
            local.client = client
        return client

    def record(key: str, sent: int, expected: int, error: str | None) -> None:
        """Checkpoint one finished job and print progress. Parent only."""
        if error is not None:
            counters["errors"] += 1
            print("  ERROR %s: %s" % (key, error), file=out)
            return
        # No per-unit flush or count: a force_flush per unit is the whole
        # cost of a rebuild (6 s a unit). The checkpoint records rows sent,
        # and the per-ws verification after the last flush is the real
        # check that the store holds what the live one does.
        checkpoint.write(key, sent, expected=expected)
        counters["done"] += 1
        counters["rows"] += sent
        if sent != expected:
            print("  WARN %s: %d rows sent, %d expected" % (key, sent, expected), file=out)
        if counters["done"] % 50 == 0 or counters["done"] == len(pending):
            print(
                "  %d/%d jobs, %d rows" % (counters["done"], len(pending), counters["rows"]),
                file=out,
            )

    def run(job: Job) -> None:
        try:
            sent = ingest_job(job, client_for())
        except Exception as exc:  # one unit never stops the rebuild
            with print_lock:
                record(job.key, 0, job.rows, "%s: %s" % (exc.__class__.__name__, exc))
            return
        with print_lock:
            record(job.key, sent, job.rows, None)

    # Row building is CPU-bound Python, so threads share one core. Processes
    # are the default; a test that injects a client factory keeps threads,
    # because its fake store lives in this process.
    try:
        # ``as_completed``, not ``map``: a checkpoint written in submission
        # order lags behind the workers by every job queued behind one slow
        # unit, and a kill would then re-ingest (and duplicate) all of them.
        if pending and use_processes:
            with ProcessPoolExecutor(
                max_workers=max(1, int(parallel)),
                initializer=_init_worker,
                initargs=(store.url,),
            ) as pool:
                futures = {pool.submit(_ingest_worker, job): job for job in pending}
                for future in as_completed(futures):
                    record(*future.result())
        elif pending:
            with ThreadPoolExecutor(max_workers=max(1, int(parallel))) as pool:
                futures = [pool.submit(run, job) for job in pending]
                for future in as_completed(futures):
                    future.result()
        new_client = client_factory(store.url)
        new_client.flush()
        old_client = old_client or client_factory(live_url)
        expected_paths = sorted({job_path(job) for job in jobs})
        ok, _table = verify(
            old_client, new_client, out=out, expected_paths=expected_paths
        )
        meta["expected_paths"] = expected_paths
        meta["complete"] = bool(ok and not counters["errors"])
        write_meta(data_dir, meta)
    finally:
        if not keep_running:
            store.stop()

    print("", file=out)
    print("cutover plan (nothing has been changed):", file=out)
    print("  agent_dash.py logs cutover-store --from %s --apply" % _expand(data_dir), file=out)
    print("  it stops %s, pauses %s, re-ingests units archived after %s," % (VLOGS_LABEL, ARCHIVE_LABEL, meta["snapshot_ts"]), file=out)
    print("  renames %s to <dir>.old-<date>, moves the rebuilt dir in, restarts," % live_data_dir(plist), file=out)
    print("  re-verifies against the live port, and swaps back on failure.", file=out)
    if counters["errors"]:
        print("  %d jobs failed; fix them and re-run to resume" % counters["errors"], file=out)
    return 0 if ok and not counters["errors"] else 1


# --------------------------------------------------------------- cutover

def _delta_jobs(dest, state_dir, snapshot_ts: float) -> list[Job]:
    """Jobs for units archived after the rebuild's snapshot."""
    jobs = session_jobs(dest) + log_jobs(state_dir)
    return [job for job in jobs if job.ts > snapshot_ts]


def cutover(
    *,
    from_dir,
    apply: bool = False,
    url: str | None = None,
    state=None,
    dest=None,
    out=None,
    plist=None,
    live_data=None,
    port=None,
    service=None,
    archive_service=None,
    store=None,
    client_factory=None,
    old_client=None,
    bin_path=None,
    healthy=None,
) -> int:
    """Swap the rebuilt store in under the live service, and back on failure."""
    out = out or sys.stdout
    state_dir = Path(state) if state is not None else _state_dir()
    dest = dest or sessionarchive.load_config(state_dir)["dest"]
    from_dir = _expand(from_dir)
    live_data = _expand(live_data or live_data_dir(plist))
    port = int(port or live_port(plist))
    live_url = url or "http://127.0.0.1:%d" % port
    client_factory = client_factory or (lambda u: vlogs.VLogsClient(u, timeout=600.0))
    healthy = healthy or _healthy
    service = service or LaunchdService(VLOGS_LABEL, plist or DEFAULT_PLIST)
    archive_service = archive_service or LaunchdService(
        ARCHIVE_LABEL, "~/Library/LaunchAgents/%s.plist" % ARCHIVE_LABEL
    )

    meta = read_meta(from_dir)
    snapshot_ts = float(meta.get("snapshot_ts") or 0.0)
    if not snapshot_ts:
        print(
            "%s has no rebuild metadata; run logs rebuild-store --apply first"
            % sidecar_dir(from_dir),
            file=sys.stderr,
        )
        return 2
    if not os.path.isdir(from_dir):
        print("rebuilt data dir not found: %s" % from_dir, file=sys.stderr)
        return 2
    if not os.path.isdir(live_data):
        print("live data dir not found: %s" % live_data, file=sys.stderr)
        return 2
    try:
        same_device = os.stat(from_dir).st_dev == os.stat(live_data).st_dev
    except OSError:
        same_device = False
    if not same_device:
        print(
            "%s and %s are on different filesystems; the swap is a rename and needs one"
            % (from_dir, live_data),
            file=sys.stderr,
        )
        return 2

    delta = _delta_jobs(dest, state_dir, snapshot_ts)
    delta_rows = sum(j.rows for j in delta)
    expected_paths = set(meta.get("expected_paths") or []) | {job_path(j) for j in delta}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    old_dir = "%s.old-%s" % (live_data, stamp)

    print("cutover plan:", file=out)
    print("  pause %s (so nothing is archived mid-swap)" % ARCHIVE_LABEL, file=out)
    print("  copy live-only paths and archived paths the live store lacks from %s" % live_url, file=out)
    print(
        "  re-ingest %d units archived after %s (%d rows) into %s"
        % (len(delta), meta["snapshot_ts"], delta_rows, from_dir),
        file=out,
    )
    print("  copy again for the delta, then stop %s" % VLOGS_LABEL, file=out)
    print("  rename %s -> %s" % (live_data, old_dir), file=out)
    print("  move %s -> %s" % (from_dir, live_data), file=out)
    print("  start %s, verify against %s, swap back on failure" % (VLOGS_LABEL, live_url), file=out)
    print("  resume %s" % ARCHIVE_LABEL, file=out)
    print("  the old dir is kept; remove it with: rm -rf %s" % old_dir, file=out)
    if not apply:
        print("dry run: pass --apply to perform the cutover", file=out)
        return 0

    store = store or Store(
        bin_path or DEFAULT_BIN, from_dir, int(meta.get("port") or port), live_flags(plist),
        log_path=str(sidecar_dir(from_dir) / "vlogs.log"),
    )

    # 1. Pause the hourly archive job, so the delta set is complete: nothing
    #    can be archived between the scan and the swap. The live store stays
    #    up for the copy, which reads it.
    archive_service.stop()

    # 2. Copy the rows the archive cannot reproduce (live-only paths) and any
    #    archived path the rebuild could not ingest, from the live store.
    store.start()
    live_client = old_client or client_factory(live_url)
    live_only, missing = copy_plan(live_client, expected_paths)
    print(
        "  copy %d live-only paths and %d archived paths the live store lacks"
        % (len(live_only), len(missing)),
        file=out,
    )
    copy_result = copy_from_live(
        live_client, client_factory(store.url), live_only + missing, out=out
    )
    copied_paths = {p for p, r in copy_result.items() if r["copied"]}
    expected_paths |= copied_paths

    # 3. Re-ingest the delta into the rebuilt store.
    errors = 0
    if delta:
        local = threading.local()

        def client_for():
            client = getattr(local, "client", None)
            if client is None:
                client = client_factory(store.url)
                local.client = client
            return client

        for job in delta:
            try:
                ingest_job(job, client_for())
            except Exception as exc:
                errors += 1
                print("  ERROR delta %s: %s" % (job.key, exc), file=out)
        client_for().flush()

    # 4. Copy again for the delta: rows that arrived while step 3 ran. The
    #    copy skips rows the rebuilt store already holds, so this adds only
    #    the new ones.
    copy_result = copy_from_live(
        live_client, client_factory(store.url), live_only + missing, out=out
    )
    copied_paths |= {p for p, r in copy_result.items() if r["copied"]}
    expected_paths |= copied_paths
    short = sorted(p for p, r in copy_result.items() if not r["ok"])
    store.stop()
    if errors or short:
        if errors:
            print("  %d delta units failed; not swapping" % errors, file=out)
        if short:
            print("  %d paths are still short after the copy; not swapping" % len(short), file=out)
            for path in short[:10]:
                r = copy_result[path]
                print("    %s: live %d, rebuilt %d" % (path, r["live"], r["rebuilt"]), file=out)
        archive_service.start()
        return 1

    # 5. Stop the live service and swap the directories.
    service.stop()
    try:
        os.rename(live_data, old_dir)
    except OSError as exc:
        print("could not rename %s: %s" % (live_data, exc), file=sys.stderr)
        service.start()
        archive_service.start()
        return 1
    try:
        os.rename(from_dir, live_data)
    except OSError as exc:
        print("could not move %s into place: %s" % (from_dir, exc), file=sys.stderr)
        os.rename(old_dir, live_data)
        service.start()
        archive_service.start()
        return 1

    # 4. Start the service and verify against the live port.
    service.start()
    deadline = time.time() + 180
    while time.time() < deadline and not healthy(live_url):
        time.sleep(0.5)
    ok = False
    if healthy(live_url):
        new_client = client_factory(live_url)
        new_client.flush()
        old_client = old_client or client_factory(live_url)
        ok, _table = verify(
            old_client, new_client, out=out, expected_paths=expected_paths
        )
    else:
        print("the service did not come up on %s" % live_url, file=out)

    if not ok:
        print("verification failed; swapping back", file=out)
        service.stop()
        try:
            os.rename(live_data, from_dir)
            os.rename(old_dir, live_data)
        except OSError as exc:
            print("SWAP BACK FAILED: %s" % exc, file=sys.stderr)
            print("  live data is at %s, old data at %s" % (live_data, old_dir), file=sys.stderr)
            return 1
        service.start()
        archive_service.start()
        return 1

    archive_service.start()
    print("", file=out)
    print("cutover done. The old store is kept at %s" % old_dir, file=out)
    print("remove it when you are satisfied: rm -rf %s" % old_dir, file=out)
    return 0


# ------------------------------------------------------------------- CLI

USAGE = """\
usage:
  agent_dash.py logs rebuild-store --data-dir DIR --port P [--apply] [--parallel N]
      [--limit N] [--no-resume] [--keep-running] [--url URL] [--dest DIR]
      [--bin PATH] [--plist PATH] [--memcap MB] [--no-memcap] [--copy-from-live]
  agent_dash.py logs cutover-store --from DIR [--apply] [--url URL] [--dest DIR]
      [--live-data DIR] [--port P] [--plist PATH]

rebuild-store re-ingests the session archive and the log archive into a
second VictoriaLogs on a scratch port and data dir, verifies it per ws
against the live store, and prints a cutover plan. It never touches the
live store or the plist. Resumable: a checkpoint per unit lives beside
the data dir (DIR.rebuild/).

rebuild-store --copy-from-live copies the paths the archive cannot
reproduce (a session ingested live but not yet archived) and any archived
path the rebuilt store lacks from the live store into the rebuilt one,
then verifies per path. It is the cutover's copy step, usable on its own
before the swap and again for the delta after the archive job is paused.

cutover-store stops the live service, pauses the hourly archive job,
re-ingests units archived after the rebuild's snapshot, renames the live
data dir to DIR.old-<date>, moves the rebuilt dir in, restarts, verifies
against the live port, and swaps back on failure. It never deletes the
old dir; it prints the exact rm -rf.
"""


def _memcap_reexec(argv: list[str], mb: int) -> None:
    """Re-exec under scripts/memcap.sh, so the rebuild cannot grow past the cap."""
    if os.environ.get("AGENT_DASH_MEMCAP"):
        return
    memcap = Path(__file__).resolve().parent.parent / "scripts" / "memcap.sh"
    if not memcap.is_file():
        return
    env = dict(os.environ, AGENT_DASH_MEMCAP="1", PYTHONUNBUFFERED="1")
    os.execve(
        str(memcap),
        [str(memcap), str(int(mb)), sys.executable, sys.argv[0], "logs", *argv],
        env,
    )


def _low_priority() -> None:
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def _parse(argv: list[str], flags: dict, values: dict) -> tuple[dict, list[str]]:
    """Tiny parser: ``flags`` are booleans, ``values`` take the next argument."""
    got = {name: False for name in flags}
    got.update({name: None for name in values})
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in flags:
            got[arg] = True
        elif arg in values:
            if i + 1 >= len(argv):
                raise SystemExit("missing value for %s" % arg)
            got[arg] = argv[i + 1]
            i += 1
        else:
            rest.append(arg)
        i += 1
    return got, rest


def _main_rebuild(argv: list[str]) -> int:
    try:
        opts, rest = _parse(
            argv,
            {"--apply", "--no-resume", "--keep-running", "--no-memcap", "--copy-from-live"},
            {
                "--data-dir": None,
                "--port": None,
                "--parallel": None,
                "--limit": None,
                "--url": None,
                "--dest": None,
                "--bin": None,
                "--plist": None,
                "--memcap": None,
            },
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if rest:
        print("unknown argument: %s" % rest[0], file=sys.stderr)
        return 2
    if not opts["--data-dir"] or not opts["--port"]:
        print(USAGE, end="", file=sys.stderr)
        return 2
    try:
        port = int(opts["--port"])
        parallel = int(opts["--parallel"] or DEFAULT_PARALLEL)
        limit = int(opts["--limit"]) if opts["--limit"] else None
        memcap_mb = int(opts["--memcap"] or DEFAULT_MEMCAP_MB)
    except ValueError as exc:
        print("bad number: %s" % exc, file=sys.stderr)
        return 2
    if not opts["--no-memcap"]:
        _memcap_reexec(["rebuild-store", *argv], memcap_mb)
    _low_priority()
    if opts["--copy-from-live"]:
        return copy_only(
            data_dir=opts["--data-dir"],
            port=port,
            apply=opts["--apply"],
            url=opts["--url"],
            dest=opts["--dest"],
            bin_path=opts["--bin"],
            plist=opts["--plist"],
        )
    return rebuild(
        data_dir=opts["--data-dir"],
        port=port,
        apply=opts["--apply"],
        parallel=parallel,
        url=opts["--url"],
        dest=opts["--dest"],
        bin_path=opts["--bin"],
        plist=opts["--plist"],
        limit=limit,
        resume=not opts["--no-resume"],
        keep_running=opts["--keep-running"],
    )


def _main_cutover(argv: list[str]) -> int:
    try:
        opts, rest = _parse(
            argv,
            {"--apply"},
            {
                "--from": None,
                "--url": None,
                "--dest": None,
                "--live-data": None,
                "--port": None,
                "--plist": None,
                "--bin": None,
            },
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if rest:
        print("unknown argument: %s" % rest[0], file=sys.stderr)
        return 2
    if not opts["--from"]:
        print(USAGE, end="", file=sys.stderr)
        return 2
    try:
        port = int(opts["--port"]) if opts["--port"] else None
    except ValueError as exc:
        print("bad port: %s" % exc, file=sys.stderr)
        return 2
    _low_priority()
    return cutover(
        from_dir=opts["--from"],
        apply=opts["--apply"],
        url=opts["--url"],
        dest=opts["--dest"],
        live_data=opts["--live-data"],
        port=port,
        plist=opts["--plist"],
        bin_path=opts["--bin"],
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, end="", file=sys.stderr)
        return 2
    if argv[0] == "rebuild-store":
        return _main_rebuild(argv[1:])
    if argv[0] == "cutover-store":
        return _main_cutover(argv[1:])
    print(USAGE, end="", file=sys.stderr)
    return 2
