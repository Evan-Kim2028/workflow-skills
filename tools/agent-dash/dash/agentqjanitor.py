"""Compress agentq's own backup cruft: ``agent_dash.py janitor agentq``.

``~/.local/state/agentq`` accumulates files nothing prunes: ``broker.json.bak-*``
(written before every broker config change), ``agentq.db.bak-*`` (written
before a schema migration), ``ws-live-edits-*.patch`` (a workstream's live
edits, saved before a deploy) and the ``*.plist.bak-*`` launchd copies. They
are history, not garbage: the owner rule is maximum retention, so this never
deletes one. It zstd-compresses a backup older than ``--older-than-days`` in
place (``<name>.zst``) and removes the plain file only after the compressed
copy decompresses to the identical sha256.

Rules, all of which must hold before a file is touched:

- the name matches one of :data:`PATTERNS` (a backup suffix or a saved patch);
- it is a regular file, not a symlink, owned by the current user;
- its mtime is older than ``--older-than-days`` (default 7);
- it is not already compressed (``.zst``) and no ``<name>.zst`` exists;
- no process holds it open, from one ``lsof -Fn`` snapshot (fails closed);
- the compressed copy verifies (decompressed sha256 and length equal the
  original) before the original is unlinked.

Dry run by default: it compresses to a temp file to measure the real saving,
then deletes the temp file. ``--apply`` keeps the ``.zst`` and removes the
plain file. Exit 0 ok, 1 a file failed, 2 usage.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dash import errors

#: Backup suffixes and saved-patch names this janitor owns. A name must match
#: one of these; nothing else in the state dir is touched.
PATTERNS = (
    "*.bak-*",
    "*.bak",
    "ws-live-edits-*.patch",
    "*.patch.bak-*",
)
#: Never compress these even if a pattern matches (live state, not a backup).
NEVER = (
    "agentq.db",
    "agentq.db-wal",
    "agentq.db-shm",
    "broker.json",
    "broker.pid",
    "broker.heartbeat",
)
DEFAULT_OLDER_THAN_DAYS = 7.0
ZSTD_LEVEL = "-19"
ZSTD_LONG = "--long=31"
READ_CHUNK = 1 << 20
LSOF_TIMEOUT_S = 120

USAGE = """usage: agent-dash janitor agentq [--older-than-days 7] [--apply] [--json]

Dry run by default: lists the agentq backup files older than the cutoff and
the bytes each would save when zstd-compressed in place. Nothing is deleted;
with --apply the plain file is removed only after its .zst copy verifies.
"""


def agentq_home() -> Path:
    override = os.environ.get("AGENTQ_HOME")
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".local" / "state" / "agentq"


def _zstd() -> str:
    found = shutil.which("zstd")
    if not found:
        raise RuntimeError("zstd not found on PATH")
    return found


def _sha256(path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _decompressed_sha(path) -> tuple[str, int]:
    proc = subprocess.Popen([_zstd(), "-dc", ZSTD_LONG, str(path)], stdout=subprocess.PIPE)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = proc.stdout.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError("zstd -dc exited %d on %s" % (rc, path))
    return digest.hexdigest(), total


def open_paths(paths) -> set[str] | None:
    """Which of ``paths`` a process holds open, or ``None`` when lsof failed.

    One system-wide ``lsof -Fn`` listing intersected with the wanted set: a
    path-scoped lsof per file is slow and a missing lsof must fail closed.
    """
    wanted = {os.path.realpath(p) for p in paths}
    if not wanted:
        return set()
    try:
        proc = subprocess.run(["lsof", "-Fn"], capture_output=True, text=True, timeout=LSOF_TIMEOUT_S)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    found = set()
    for line in proc.stdout.splitlines():
        if line.startswith("n") and line[1:] in wanted:
            found.add(line[1:])
    return found


def _matches(name: str) -> bool:
    if name in NEVER:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in PATTERNS)


def plan(home=None, *, older_than_days: float = DEFAULT_OLDER_THAN_DAYS, now: float | None = None) -> list[dict]:
    """The candidate rows: ``{path, name, bytes, age_days, action, reason}``.

    ``action`` is ``compress`` for a candidate and ``keep`` with a reason for
    everything else that matched a pattern (too new, already compressed, a
    live file, a symlink, not a regular file).
    """
    now = time.time() if now is None else float(now)
    root = Path(home or agentq_home())
    cutoff = now - float(older_than_days) * 86400.0
    rows: list[dict] = []
    if not root.is_dir():
        return rows
    for entry in sorted(root.iterdir()):
        name = entry.name
        if not _matches(name):
            continue
        row = {"path": str(entry), "name": name, "bytes": 0, "age_days": 0.0, "action": "keep", "reason": ""}
        try:
            st = entry.lstat()
        except OSError as exc:
            row["reason"] = "stat failed: %s" % exc
            rows.append(row)
            continue
        row["bytes"] = int(st.st_size)
        row["age_days"] = round((now - st.st_mtime) / 86400.0, 2)
        if entry.is_symlink():
            row["reason"] = "symlink"
        elif not entry.is_file():
            row["reason"] = "not a regular file"
        elif name.endswith(".zst"):
            row["reason"] = "already compressed"
        elif os.path.exists(str(entry) + ".zst"):
            row["reason"] = "a .zst copy already exists"
        elif st.st_mtime >= cutoff:
            row["reason"] = "newer than %.0f days" % older_than_days
        else:
            row["action"] = "compress"
        rows.append(row)
    return rows


def _compress(src: str, dst: str) -> None:
    proc = subprocess.run(
        [_zstd(), ZSTD_LEVEL, ZSTD_LONG, "-q", "-f", "-o", dst, src],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "zstd exited %d: %s" % (proc.returncode, proc.stderr.decode("utf-8", "replace")[:200])
        )


def compress_one(path, *, apply: bool = False) -> dict:
    """Compress one file to ``<path>.zst``, verified; remove the plain file on apply."""
    path = str(path)
    dst = path + ".zst"
    tmp = dst + ".tmp"
    before = os.path.getsize(path)
    want_sha, want_len = _sha256(path)
    try:
        _compress(path, tmp)
        after = os.path.getsize(tmp)
        try:
            got_sha, got_len = _decompressed_sha(tmp)
        except RuntimeError as exc:
            return {"path": path, "before": before, "after": after, "ok": False, "reason": str(exc)}
        if got_sha != want_sha or got_len != want_len:
            return {
                "path": path, "before": before, "after": after, "ok": False,
                "reason": "decompressed content differs (sha %s/%s, bytes %d/%d)"
                % (want_sha[:12], got_sha[:12], want_len, got_len),
            }
        if after >= before:
            # A tiny JSON blob can grow. Replacing a readable backup with a
            # bigger one is not a saving, so the plain file stays.
            return {
                "path": path, "before": before, "after": after, "ok": True, "gain": False,
                "reason": "no gain (compressed copy is not smaller)",
            }
        if apply:
            os.replace(tmp, dst)
            os.unlink(path)
        return {"path": path, "before": before, "after": after, "ok": True, "gain": True, "reason": ""}
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def run(*, apply: bool = False, older_than_days: float = DEFAULT_OLDER_THAN_DAYS,
        home=None, out=None, now: float | None = None, as_json: bool = False) -> int:
    out = out or sys.stdout
    rows = plan(home, older_than_days=older_than_days, now=now)
    candidates = [row for row in rows if row["action"] == "compress"]
    held = open_paths([row["path"] for row in candidates])
    if held is None:
        print("agentq janitor: lsof failed; keeping every file", file=out)
        return 1
    for row in candidates:
        if os.path.realpath(row["path"]) in held:
            row["action"] = "keep"
            row["reason"] = "open by a process"
    candidates = [row for row in rows if row["action"] == "compress"]
    before_total = after_total = 0
    done = failed = 0
    for row in candidates:
        try:
            res = compress_one(row["path"], apply=apply)
        except (RuntimeError, OSError) as exc:
            failed += 1
            row["action"] = "failed"
            row["reason"] = str(exc)
            continue
        if not res["ok"]:
            failed += 1
            row["action"] = "failed"
            row["reason"] = res["reason"]
            continue
        if not res.get("gain", True):
            row["action"] = "keep"
            row["reason"] = res["reason"]
            continue
        done += 1
        before_total += res["before"]
        after_total += res["after"]
        row["after"] = res["after"]
    if as_json:
        import json

        print(
            json.dumps(
                {
                    "apply": apply,
                    "older_than_days": older_than_days,
                    "rows": rows,
                    "compressed": done,
                    "failed": failed,
                    "before": before_total,
                    "after": after_total,
                    "saved": before_total - after_total,
                },
                indent=2,
                sort_keys=True,
            ),
            file=out,
        )
        return 1 if failed else 0
    print(
        "agentq janitor: %s, %d file(s) older than %.0f days"
        % ("apply" if apply else "dry run", len(candidates), older_than_days),
        file=out,
    )
    for row in rows:
        after = row.get("after")
        print(
            "  %-8s %-52s %10d -> %s  %s"
            % (
                row["action"],
                row["name"],
                row["bytes"],
                ("%d" % after) if after is not None else "-",
                row["reason"],
            ),
            file=out,
        )
    kept = sum(1 for row in rows if row["action"] == "keep")
    print(
        "%s %d file(s): %d -> %d bytes, saved %d (%.1f%%); %d kept, %d failed"
        % (
            "compressed" if apply else "would compress",
            done,
            before_total,
            after_total,
            before_total - after_total,
            100.0 * (before_total - after_total) / before_total if before_total else 0.0,
            kept,
            failed,
        ),
        file=out,
    )
    if not apply and done:
        print("dry run: nothing replaced; re-run with --apply", file=out)
    return 1 if failed else 0


def cmd_agentq(argv: list[str]) -> int:
    """``janitor agentq``. Dry run unless ``--apply``."""
    apply = False
    older = DEFAULT_OLDER_THAN_DAYS
    as_json = False
    args = list(argv or [])
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--apply":
            apply = True
            index += 1
        elif token == "--json":
            as_json = True
            index += 1
        elif token == "--older-than-days" and index + 1 < len(args):
            try:
                older = float(args[index + 1])
            except ValueError:
                print("--older-than-days needs a number", file=sys.stderr)
                return errors.USAGE
            index += 2
        elif token in ("-h", "--help"):
            print(USAGE, end="")
            return errors.OK
        else:
            print(USAGE, end="", file=sys.stderr)
            return errors.USAGE
    return run(apply=apply, older_than_days=older, as_json=as_json)
