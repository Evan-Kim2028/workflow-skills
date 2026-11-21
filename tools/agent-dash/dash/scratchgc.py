"""Scratch garbage collection and status (``agent_dash.py scratch gc|status``).

A scratch folder is reclaimable only when every check below passes. Any
check that cannot run keeps the folder, so the safe answer is always "keep":

1. the folder can be walked and has an owner marker for its own label;
2. no live launcher names the label (``live_worker_processes``);
3. the run folder's ``running/<label>`` file and ``<label>.pid`` name no
   live process;
4. no process has a file open in the folder or has it as its cwd
   (``lsof`` on macOS, ``/proc`` on Linux);
5. the newest file in the folder is older than the cutoff (``gc`` only;
   ``ws archive`` cleanup skips the age check because archiving is the
   decision).

Two rules relax the age cutoff, and only the age cutoff:

- a marked folder whose run is finished is reclaimed once its newest file
  is older than ``--finished-after-hours`` (off by default), even when it
  is younger than ``--older-than-days``;
- a folder with no owner marker file at all is reclaimed once its newest file
  is older than ``--older-than-days``, but only when its name is a worker run
  label (``hub``/``ws<n>`` plus ``exec``/``review``/``plan``/``coord``), its
  path is not a Postgres data directory or a ``.db`` file, and no live
  launcher names it. Every other unmarked folder is kept, and so is a folder
  whose marker file exists but cannot be read.

``gc`` is a dry run unless ``--apply``. Each candidate is assessed a
second time just before removal, so a run that started in between is
kept. Removal is ``shutil.rmtree`` on one folder under the scratch root;
symlinks are never followed.

``gc`` also compacts big logs, in every scratch folder and regardless of the
folder's age: a regular ``*.log`` over ``--log-min-mb`` (100 MB) that has not
been written for ``--log-idle-hours`` (6 h) and that no process holds open
(``lsof``) is compressed with ``zstd`` to ``<name>.log.zst`` next to it. The
``.zst`` is verified with ``zstd -t`` before the plain ``.log`` is removed, so
the log content is kept, never deleted. A log that is still open (a running
Postgres, a live tail) is kept as is. ``--no-compact-logs`` skips this step.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from dash import scratchdir

DEFAULT_OLDER_DAYS = 7.0
LSOF_TIMEOUT_S = 60
LOG_MIN_BYTES = 100 * 1024 * 1024
LOG_IDLE_HOURS = 6.0
ZSTD_LEVEL = "-3"
ZSTD_TIMEOUT_S = 1800
LSOF_FILE_TIMEOUT_S = 30
LIVE_REASONS = ("live launcher", "run still running", "in use")
WORKER_LABEL_RE = re.compile(r"^(hub|ws\d+|WS\d+)-(exec|review|plan|coord)-[A-Za-z0-9._-]+$")
PROTECTED_PATH_RE = re.compile(r"pgdata|postgres|\.db$", re.IGNORECASE)


def _walk(path: Path) -> tuple[int, float, bool]:
    """``(bytes on disk, newest mtime, walked_ok)``. Symlinks are not followed."""
    total = 0
    newest = 0.0
    ok = True
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        ok = False
                        continue
                    newest = max(newest, st.st_mtime)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                    else:
                        total += int(getattr(st, "st_blocks", 0) * 512) or int(st.st_size)
        except OSError:
            ok = False
    try:
        newest = max(newest, path.stat().st_mtime)
    except OSError:
        ok = False
    return total, newest, ok


def live_labels(now: float | None = None) -> set | None:
    """Labels of live worker launches, or None when the listing cannot run."""
    try:
        from dash.sources.process import live_worker_processes

        rows = live_worker_processes(now)
    except Exception:
        return None
    return {str(row.get("label")) for row in rows if row.get("label")}


def _pid_alive(pid: int) -> bool:
    from dash.liveness import pid_alive

    return bool(pid_alive(pid))


def _run_state(run_dir: str, label: str) -> str | None:
    """``"live"``, ``"finished"``, or None when the run state cannot be read."""
    if not run_dir:
        return "finished"
    base = Path(run_dir)
    if not base.is_dir():
        return "finished"
    for name in (base / "running" / label, base / f"{label}.pid"):
        if not name.exists():
            continue
        try:
            text = name.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        head = text.split("\t", 1)[0].strip()
        try:
            pid = int(head)
        except ValueError:
            return None
        if pid > 0 and _pid_alive(pid):
            return "live"
    return "finished"


SBIN_DIRS = ("/usr/sbin", "/usr/bin", "/sbin")


def _lsof() -> str | None:
    """``lsof`` from PATH, else the system folders (a worker shell may lack /usr/sbin)."""
    search = os.pathsep.join([os.environ.get("PATH", ""), *SBIN_DIRS])
    return shutil.which("lsof", path=search)


def _proc_open_in(folder: str) -> bool | None:
    """True when a process has the folder open or as cwd, False when none, None unknown."""
    if os.path.isdir("/proc/self"):
        prefix = folder.rstrip("/") + "/"
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return None
        for pid in pids:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            if cwd and (cwd == folder or cwd.startswith(prefix)):
                return True
            try:
                fds = os.listdir(f"/proc/{pid}/fd")
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target.startswith(prefix):
                    return True
        return False
    lsof = _lsof()
    if lsof is None:
        return None
    try:
        out = subprocess.run(
            [lsof, "-Fn", "+D", folder],
            capture_output=True, text=True, timeout=LSOF_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 both when it finds nothing and when it lists files it
    # could not fully read, so output decides: any listed line means in use.
    if out.stdout.strip():
        return True
    if out.returncode == 1 and not out.stderr.strip():
        return False
    if out.returncode == 0:
        return False
    return None


def _unmarked_worker(folder: Path) -> bool:
    """True when an unmarked folder looks like a launcher-made worker folder."""
    if not WORKER_LABEL_RE.fullmatch(folder.name):
        return False
    return not PROTECTED_PATH_RE.search(str(folder))


def assess(folder, now: float, older_than_days: float | None, live: set | None,
           finished_after_hours: float | None = None) -> dict:
    """Verdict for one scratch folder: ``action`` is ``reclaim`` or ``keep``."""
    folder = Path(folder)
    row = {
        "path": str(folder),
        "label": folder.name,
        "workstream": folder.parent.name,
        "bytes": 0,
        "newest": 0.0,
        "action": "keep",
        "reason": "",
    }

    def keep(reason: str) -> dict:
        row["action"] = "keep"
        row["reason"] = reason
        return row

    size, newest, walked = _walk(folder)
    row["bytes"] = size
    row["newest"] = newest
    if not walked:
        return keep("cannot read the folder")
    owner = scratchdir.read_owner(folder)
    if owner is None:
        if os.path.lexists(folder / scratchdir.OWNER_FILE):
            return keep("owner marker unreadable")
        if older_than_days is None or not _unmarked_worker(folder):
            return keep("no owner marker: not made by a launcher")
        if live is None:
            return keep("live-process check did not run")
        if folder.name in live:
            return keep("live launcher")
        in_use = _proc_open_in(str(folder))
        if in_use is None:
            return keep("open-handle check did not run")
        if in_use:
            return keep("in use")
        if newest > now - float(older_than_days) * 86400:
            return keep(f"newest file is under {older_than_days:g} days old")
        row["action"] = "reclaim"
        row["reason"] = "unmarked worker folder, stale"
        return row
    if str(owner.get("label", "")) != folder.name:
        return keep("owner marker names another run")
    if live is None:
        return keep("live-process check did not run")
    if folder.name in live:
        return keep("live launcher")
    state = _run_state(str(owner.get("run_dir") or ""), folder.name)
    if state is None:
        return keep("run state unreadable")
    if state == "live":
        return keep("run still running")
    in_use = _proc_open_in(str(folder))
    if in_use is None:
        return keep("open-handle check did not run")
    if in_use:
        return keep("in use")
    if older_than_days is not None and newest > now - float(older_than_days) * 86400:
        if finished_after_hours is None or newest > now - float(finished_after_hours) * 3600:
            return keep(f"newest file is under {older_than_days:g} days old")
        row["action"] = "reclaim"
        row["reason"] = f"finished run, idle over {float(finished_after_hours):g} h"
        return row
    row["action"] = "reclaim"
    row["reason"] = "finished run"
    return row


def _folders(root: Path):
    if not root.is_dir() or root.is_symlink():
        return
    for ws_dir in sorted(root.iterdir()):
        if ws_dir.is_symlink() or not ws_dir.is_dir():
            continue
        for folder in sorted(ws_dir.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            yield folder


def scan(root, now: float | None = None, older_than_days: float | None = DEFAULT_OLDER_DAYS,
         finished_after_hours: float | None = None) -> list:
    """Assessed rows for every scratch folder under ``root`` (read-only)."""
    moment = time.time() if now is None else float(now)
    live = live_labels(moment)
    return [assess(f, moment, older_than_days, live, finished_after_hours) for f in _folders(Path(root))]


def _remove(folder: Path) -> bool:
    try:
        shutil.rmtree(folder)
    except OSError:
        return False
    try:
        folder.parent.rmdir()
    except OSError:
        pass
    return True


def gc(root, older_than_days: float = DEFAULT_OLDER_DAYS, apply: bool = False, now: float | None = None,
       finished_after_hours: float | None = None, compact_logs: bool = True,
       log_min_bytes: int = LOG_MIN_BYTES, log_idle_hours: float = LOG_IDLE_HOURS) -> dict:
    """Dry run by default. With ``apply``, removes each reclaimable folder."""
    moment = time.time() if now is None else float(now)
    rows = scan(root, moment, older_than_days, finished_after_hours)
    reclaim = [r for r in rows if r["action"] == "reclaim"]
    removed = []
    failed = []
    if apply:
        live = live_labels(moment)
        for row in reclaim:
            folder = Path(row["path"])
            again = assess(folder, moment, older_than_days, live, finished_after_hours)
            if again["action"] != "reclaim":
                row["action"] = "keep"
                row["reason"] = again["reason"]
                continue
            (removed if _remove(folder) else failed).append(row)
    freed = sum(r["bytes"] for r in removed)
    logs = None
    if compact_logs:
        logs = compress_logs(root, apply=apply, now=moment,
                             min_bytes=log_min_bytes, idle_hours=log_idle_hours)
    return {
        "root": str(root),
        "apply": bool(apply),
        "older_than_days": float(older_than_days),
        "finished_after_hours": None if finished_after_hours is None else float(finished_after_hours),
        "rows": rows,
        "reclaimable": sum(r["bytes"] for r in reclaim),
        "reclaimable_count": len(reclaim),
        "removed_count": len(removed),
        "freed": freed,
        "failed": [r["path"] for r in failed],
        "kept_count": sum(1 for r in rows if r["action"] == "keep"),
        "logs": logs,
    }


def human(num: float) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def status(root, top: int = 10) -> dict:
    """Total size and the largest folders under the scratch root (read-only)."""
    root = Path(root)
    folders = []
    for folder in _folders(root):
        size, newest, _ok = _walk(folder)
        folders.append({
            "path": str(folder),
            "label": folder.name,
            "workstream": folder.parent.name,
            "bytes": size,
            "newest": newest,
        })
    folders.sort(key=lambda r: r["bytes"], reverse=True)
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "folders": len(folders),
        "bytes": sum(f["bytes"] for f in folders),
        "largest": folders[: max(0, int(top))],
    }


# --- workstream helpers used by ``ws archive`` cleanup -------------------


def workstream_folders(ws: str, root=None) -> list[Path]:
    """Scratch folders filed under one workstream key (``WS4``)."""
    base = Path(root) if root is not None else scratchdir.scratch_root()
    key = str(ws or "").strip().upper()
    if not key or "/" in key or key in (".", ".."):
        return []
    ws_dir = base / key
    if ws_dir.is_symlink() or not ws_dir.is_dir():
        return []
    return [f for f in sorted(ws_dir.iterdir()) if f.is_dir() and not f.is_symlink()]


def workstream_live(ws: str, root=None, now: float | None = None) -> tuple[bool, list]:
    """``(verifiable, live_names)`` for one workstream's scratch.

    ``verifiable`` is False when the live listing did not run; the caller
    then refuses cleanup. ``live_names`` lists the labels that block it.
    """
    moment = time.time() if now is None else float(now)
    live = live_labels(moment)
    if live is None:
        return False, []
    blocked = []
    for folder in workstream_folders(ws, root):
        row = assess(folder, moment, None, live)
        if row["reason"] in LIVE_REASONS:
            blocked.append(folder.name)
    for label in sorted(live):
        if scratchdir.workstream_key(label) == str(ws or "").strip().upper() and label not in blocked:
            blocked.append(label)
    return True, blocked


def remove_workstream(ws: str, root=None, now: float | None = None) -> dict:
    """Remove finished scratch folders for one workstream (no age check).

    Folders that cannot be verified are kept and named in ``kept``.
    """
    moment = time.time() if now is None else float(now)
    live = live_labels(moment)
    out = {"removed": [], "kept": [], "freed": 0}
    if live is None:
        out["kept"].append("live-process check did not run")
        return out
    for folder in workstream_folders(ws, root):
        row = assess(folder, moment, None, live)
        if row["action"] != "reclaim":
            out["kept"].append(f"{folder.name} ({row['reason']})")
            continue
        size = row["bytes"]
        if _remove(folder):
            out["removed"].append(folder.name)
            out["freed"] += size
        else:
            out["kept"].append(f"{folder.name} (remove failed)")
    try:
        ws_dir = (Path(root) if root is not None else scratchdir.scratch_root()) / str(ws).strip().upper()
        if ws_dir.is_dir() and not any(ws_dir.iterdir()):
            ws_dir.rmdir()
    except OSError:
        pass
    return out


# --- big-log compaction ---------------------------------------------------


def _log_files(root: Path):
    """Every regular ``*.log`` under the scratch root, with its ``lstat``.

    Symlinked folders and files are skipped, so compaction never writes
    through a link into a checkout or a home directory.
    """
    for folder in _folders(root):
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                if not name.endswith(".log"):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                yield path, st


def _file_open(path: Path) -> bool | None:
    """True when a process holds ``path`` open, False when none, None unknown."""
    target = str(path)
    if os.path.isdir("/proc/self"):
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return None
        for pid in pids:
            try:
                fds = os.listdir(f"/proc/{pid}/fd")
            except OSError:
                continue
            for fd in fds:
                try:
                    held = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if held == target:
                    return True
        return False
    lsof = _lsof()
    if lsof is None:
        return None
    try:
        out = subprocess.run(
            [lsof, "-Fn", "--", target],
            capture_output=True, text=True, timeout=LSOF_FILE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.stdout.strip():
        return True
    if out.returncode == 1 and not out.stderr.strip():
        return False
    if out.returncode == 0:
        return False
    return None


def _zstd_compress(path: Path) -> tuple[bool, str]:
    """Compress ``path`` to ``<path>.zst`` and verify the copy. ``(ok, reason)``."""
    zstd = shutil.which("zstd")
    if zstd is None:
        return False, "zstd not found on PATH"
    out = path.with_name(path.name + ".zst")
    try:
        done = subprocess.run(
            [zstd, "-q", "-f", ZSTD_LEVEL, "-o", str(out), str(path)],
            capture_output=True, text=True, timeout=ZSTD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"zstd could not run: {exc}"
    if done.returncode != 0:
        return False, f"zstd exited {done.returncode}: {(done.stderr or '').strip()[-200:]}"
    try:
        check = subprocess.run(
            [zstd, "-q", "-t", str(out)],
            capture_output=True, text=True, timeout=ZSTD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"zstd -t could not run: {exc}"
    if check.returncode != 0:
        return False, "zstd copy does not verify"
    return True, ""


def _log_verdict(path: Path, st, now: float, min_bytes: int, idle_hours: float) -> dict:
    """One log's row: ``action`` is ``compress`` or ``keep``."""
    row = {
        "path": str(path),
        "bytes": int(st.st_size),
        "mtime": float(st.st_mtime),
        "action": "keep",
        "reason": "",
        "after": int(st.st_size),
    }
    if st.st_size < int(min_bytes):
        row["reason"] = f"under {human(min_bytes)}"
        return row
    if st.st_mtime > now - float(idle_hours) * 3600:
        row["reason"] = f"written in the last {float(idle_hours):g} h"
        return row
    zst = path.with_name(path.name + ".zst")
    try:
        zst_st = zst.lstat()
    except OSError:
        zst_st = None
    if zst_st is not None and zst_st.st_mtime >= st.st_mtime:
        row["reason"] = "already compressed"
        return row
    opened = _file_open(path)
    if opened is None:
        row["reason"] = "open-handle check did not run"
        return row
    if opened:
        row["reason"] = "in use"
        return row
    row["action"] = "compress"
    row["reason"] = f"idle over {float(idle_hours):g} h"
    return row


def compress_logs(root, apply: bool = False, now: float | None = None,
                  min_bytes: int = LOG_MIN_BYTES, idle_hours: float = LOG_IDLE_HOURS) -> dict:
    """Compress idle big logs in place (dry run unless ``apply``).

    Every scratch folder is scanned, whatever its age: a folder that is kept
    by ``gc`` can still hold a multi-GB log. The plain ``.log`` is removed
    only after the ``.zst`` verifies, so the log content is never lost.
    """
    moment = time.time() if now is None else float(now)
    rows = [_log_verdict(path, st, moment, min_bytes, idle_hours) for path, st in _log_files(Path(root))]
    todo = [r for r in rows if r["action"] == "compress"]
    compressed = []
    failed = []
    if apply:
        for row in todo:
            path = Path(row["path"])
            try:
                st = path.lstat()
            except OSError:
                row["action"] = "keep"
                row["reason"] = "gone"
                continue
            again = _log_verdict(path, st, moment, min_bytes, idle_hours)
            if again["action"] != "compress":
                row["action"] = "keep"
                row["reason"] = again["reason"]
                continue
            ok, why = _zstd_compress(path)
            if not ok:
                row["action"] = "keep"
                row["reason"] = why
                failed.append(row["path"])
                continue
            zst = path.with_name(path.name + ".zst")
            try:
                row["after"] = int(zst.stat().st_size)
            except OSError:
                row["after"] = 0
            try:
                path.unlink()
            except OSError:
                row["action"] = "keep"
                row["reason"] = "compressed, but the plain log could not be removed"
                failed.append(row["path"])
                continue
            compressed.append(row)
    compressible = sum(r["bytes"] for r in todo)
    if apply:
        before = sum(r["bytes"] for r in compressed)
        after = sum(r["after"] for r in compressed)
    else:
        before = compressible
        after = before
    return {
        "root": str(root),
        "apply": bool(apply),
        "min_bytes": int(min_bytes),
        "idle_hours": float(idle_hours),
        "rows": rows,
        "compressible_count": len(todo),
        "compressible": compressible,
        "compressed_count": len(compressed),
        "before": before,
        "after": after,
        "saved": before - after,
        "failed": failed,
    }


# --- CLI ------------------------------------------------------------------

SCRATCH_USAGE = """usage:
  python3 agent_dash.py scratch gc [--older-than-days N] [--finished-after-hours H]
                                   [--log-min-mb N] [--log-idle-hours H] [--no-compact-logs]
                                   [--apply] [--json]
  python3 agent_dash.py scratch status [--top N] [--json]
"""


def _parse_flags(argv: list[str], allowed: dict) -> dict | None:
    """``allowed`` maps flag -> ``True`` (switch) or ``False`` (takes a value)."""
    out = {}
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag not in allowed:
            return None
        if allowed[flag]:
            out[flag] = True
            i += 1
        else:
            if i + 1 >= len(argv):
                return None
            out[flag] = argv[i + 1]
            i += 2
    return out


def cmd_scratch(argv: list[str]) -> int:
    """``scratch gc|status``. Exit 0 ok, 2 usage."""
    if not argv or argv[0] not in ("gc", "status"):
        print(SCRATCH_USAGE, end="", file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    root = scratchdir.scratch_root()
    if verb == "gc":
        flags = _parse_flags(rest, {"--apply": True, "--json": True, "--no-compact-logs": True,
                                    "--older-than-days": False, "--finished-after-hours": False,
                                    "--log-min-mb": False, "--log-idle-hours": False})
        if flags is None:
            print(SCRATCH_USAGE, end="", file=sys.stderr)
            return 2
        try:
            days = float(flags.get("--older-than-days", DEFAULT_OLDER_DAYS))
        except ValueError:
            print(f"--older-than-days needs a number, got {flags['--older-than-days']!r}", file=sys.stderr)
            return 2
        if not math.isfinite(days):
            print(f"--older-than-days must be a finite number, got {flags.get('--older-than-days', DEFAULT_OLDER_DAYS)!r}",
                  file=sys.stderr)
            return 2
        if days < 0:
            print("--older-than-days must be 0 or more", file=sys.stderr)
            return 2
        finished = flags.get("--finished-after-hours")
        if finished is not None:
            try:
                finished = float(finished)
            except ValueError:
                print(f"--finished-after-hours needs a number, got {finished!r}", file=sys.stderr)
                return 2
            if not math.isfinite(finished):
                print(f"--finished-after-hours must be a finite number, got {flags['--finished-after-hours']!r}",
                      file=sys.stderr)
                return 2
            if finished < 0:
                print("--finished-after-hours must be 0 or more", file=sys.stderr)
                return 2
        log_min_mb = flags.get("--log-min-mb")
        if log_min_mb is not None:
            try:
                log_min_mb = float(log_min_mb)
            except ValueError:
                print(f"--log-min-mb needs a number, got {log_min_mb!r}", file=sys.stderr)
                return 2
            if not math.isfinite(log_min_mb) or log_min_mb < 0:
                print(f"--log-min-mb must be a finite number of 0 or more, got {log_min_mb!r}",
                      file=sys.stderr)
                return 2
        log_idle = flags.get("--log-idle-hours")
        if log_idle is not None:
            try:
                log_idle = float(log_idle)
            except ValueError:
                print(f"--log-idle-hours needs a number, got {log_idle!r}", file=sys.stderr)
                return 2
            if not math.isfinite(log_idle) or log_idle < 0:
                print(f"--log-idle-hours must be a finite number of 0 or more, got {log_idle!r}",
                      file=sys.stderr)
                return 2
        report = gc(root, older_than_days=days, apply=bool(flags.get("--apply")),
                    finished_after_hours=finished,
                    compact_logs=not flags.get("--no-compact-logs"),
                    log_min_bytes=int(LOG_MIN_BYTES if log_min_mb is None else log_min_mb * 1024 * 1024),
                    log_idle_hours=LOG_IDLE_HOURS if log_idle is None else log_idle)
        if flags.get("--json"):
            out = {k: v for k, v in report.items() if k != "rows"}
            if out.get("logs"):
                out["logs"] = {k: v for k, v in out["logs"].items() if k != "rows"}
            print(json.dumps(out, sort_keys=True))
            return 0
        _print_gc(report)
        return 0
    flags = _parse_flags(rest, {"--json": True, "--top": False})
    if flags is None:
        print(SCRATCH_USAGE, end="", file=sys.stderr)
        return 2
    try:
        top = int(flags.get("--top", 10))
    except ValueError:
        print("--top needs a whole number", file=sys.stderr)
        return 2
    report = status(root, top=top)
    if flags.get("--json"):
        print(json.dumps(report, sort_keys=True))
        return 0
    _print_status(report)
    return 0


def _print_gc(report: dict) -> None:
    mode = "apply" if report["apply"] else "dry run"
    print(f"scratch gc ({mode}) under {report['root']}, folders finished more than {report['older_than_days']:g} days ago:")
    if report.get("finished_after_hours") is not None:
        print(f"  finished runs idle over {report['finished_after_hours']:g} h are reclaimed too")
    for row in report["rows"]:
        if row["action"] == "reclaim":
            print(f"  reclaim  {row['workstream']}/{row['label']}  {human(row['bytes'])}")
    for row in report["rows"]:
        if row["action"] == "keep":
            print(f"  keep     {row['workstream']}/{row['label']}  {human(row['bytes'])}  ({row['reason']})")
    if report["apply"]:
        print(f"removed {report['removed_count']} folder(s), freed {human(report['freed'])}")
        for path in report["failed"]:
            print(f"  could not remove {path}")
    else:
        print(f"reclaimable: {report['reclaimable_count']} folder(s), {human(report['reclaimable'])}. "
              "Re-run with --apply to remove them.")
    print(f"kept: {report['kept_count']} folder(s)")
    logs = report.get("logs")
    if logs:
        floor = f"over {human(logs['min_bytes'])} idle {logs['idle_hours']:g} h"
        if logs["apply"]:
            print(f"logs: compressed {logs['compressed_count']} file(s) {floor}, "
                  f"{human(logs['before'])} -> {human(logs['after'])} (saved {human(logs['saved'])})")
        else:
            print(f"logs: {logs['compressible_count']} file(s) {floor}, {human(logs['compressible'])} "
                  "would be compressed (dry run)")
        for row in logs["rows"]:
            if row["action"] == "compress":
                print(f"  compress {row['path']}  {human(row['bytes'])}")
        for path in logs["failed"]:
            print(f"  could not compress {path}")


def _print_status(report: dict) -> None:
    if not report["exists"]:
        print(f"scratch root {report['root']} does not exist yet: 0 folders, 0 B")
        return
    print(f"scratch root {report['root']}: {report['folders']} folder(s), {human(report['bytes'])}")
    for row in report["largest"]:
        print(f"  {human(row['bytes']):>10}  {row['workstream']}/{row['label']}")
