"""Remove stale temp entries: ``agent_dash.py janitor temp``.

Scans the top level of the user temp dir (``getconf DARWIN_USER_TEMP_DIR``,
else ``$TMPDIR``) and of ``/private/tmp``, and removes an entry only when every
rule below holds. A check that cannot run keeps the entry.

- owned by the current user, and not a symlink (symlinks are never followed);
- its name is not on the allowlist (launchd and system sockets, X11 and ICE
  dirs, ``TemporaryItems``, ``claude-*`` session dirs, ``*.sock``);
- no process has it as cwd or holds an open file under it, from one
  ``lsof -nP +c0 -u <user> -Fpfn`` snapshot (prefix match). lsof failing or
  printing nothing keeps every entry;
- no postmaster in the process table has its ``-D`` data dir under it. A
  relative ``-D`` that cannot be resolved keeps every entry;
- no ``.git`` entry anywhere under it (checkouts and linked worktrees);
- no ``postmaster.pid`` under it names a live pid;
- nothing under it changed within ``--min-age`` (bounded walk, no symlinks).
  A walk that hits its entry bound or the time budget keeps the entry.

Dry run by default. With ``--apply`` the open-file and postgres snapshots are
taken again right before each removal, and an entry that is no longer stale is
kept. Removal uses :func:`dash.wtjanitor.remove_tree`, which never raises.
"""

from __future__ import annotations

import fnmatch
import getpass
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

from dash import errors, pgjanitor, wtjanitor, worktrees

LSOF_TIMEOUT_S = 120
DU_TIMEOUT_S = 30
MAX_WALK_ENTRIES = 200_000
TIME_BUDGET_S = 1200
DEFAULT_MIN_AGE = "5h"

ALLOWLIST = (
    "TemporaryItems",
    ".X11-unix",
    ".ICE-unix",
    ".font-unix",
    ".XIM-unix",
    "claude-*",
    "com.apple.*",
    ".com.apple.*",
    "launchd*",
    ".X11*",
    "*.sock",
)

USAGE = """usage: agent-dash janitor temp [--min-age 5h] [--apply]

Dry run by default: lists the stale top-level entries of the user temp dir
and /private/tmp that would be removed, and the bytes they hold. --apply
removes them after a fresh open-file and postgres check. An entry is kept
while anything under it changed within --min-age, a process uses it, a live
postgres uses it, it is a git checkout, or it is on the allowlist.
"""


@dataclass
class Snapshot:
    """Open paths and postgres data dirs from one process snapshot. None means it did not run."""

    open_paths: list[str] | None
    pg_dirs: list[str] | None
    failure: str = ""


class TempHost(pgjanitor.Host):
    """Real machine access. Tests subclass it and override each method."""

    def __init__(self) -> None:
        super().__init__()
        self.uid = os.getuid()

    def roots(self) -> tuple[str, ...]:
        candidates = [_getconf_temp(), os.environ.get("TMPDIR", ""), "/private/tmp"]
        out: list[str] = []
        for raw in candidates:
            if not raw or not raw.strip():
                continue
            root = os.path.realpath(os.path.expanduser(raw.strip()))
            if root in ("/", self.home) or root in out:
                continue
            out.append(root)
        return tuple(out)

    def lsof_text(self) -> str | None:
        text = pgjanitor._run_text(["lsof", "-nP", "+c0", "-u", self.user, "-Fpfn"], LSOF_TIMEOUT_S)
        return text if text and text.strip() else None

    def du_bytes(self, path: str) -> int | None:
        try:
            proc = subprocess.run(
                ["du", "-sk", path], check=False, capture_output=True, text=True, timeout=DU_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        first = (proc.stdout or "").split()
        if not first or not first[0].isdigit():
            return None
        return int(first[0]) * 1024

    def remove(self, path: str) -> bool:
        return wtjanitor.remove_tree(path)


def _getconf_temp() -> str:
    try:
        proc = subprocess.run(
            ["getconf", "DARWIN_USER_TEMP_DIR"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def parse_lsof(text: str) -> tuple[list[str], dict[int, str]]:
    """Open paths and the cwd per pid from ``lsof -Fpfn`` output."""
    paths: list[str] = []
    cwds: dict[int, str] = {}
    pid: int | None = None
    fd = ""
    for line in text.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "f":
            fd = value
        elif tag == "n" and value.startswith("/"):
            paths.append(value)
            if fd == "cwd" and pid is not None:
                cwds[pid] = value
    return paths, cwds


def take_snapshot(host: TempHost) -> Snapshot:
    """One lsof and one ps pass. Any failure leaves the matching field None."""
    lsof = host.lsof_text()
    if lsof is None:
        return Snapshot(None, None, "lsof did not run")
    paths, cwds = parse_lsof(lsof)
    ps = host.ps_text()
    if ps is None:
        return Snapshot(paths, None, "ps did not run")
    pg_dirs: list[str] = []
    for proc in pgjanitor.parse_ps(ps):
        if not proc.argv or os.path.basename(proc.argv[0]) != "postgres":
            continue
        raw = pgjanitor.data_dir_arg(proc.argv)
        if raw is None:
            continue
        resolved = pgjanitor.resolve_datadir(raw, cwds.get(proc.pid))
        if resolved is None:
            return Snapshot(paths, None, "postgres data dir unresolved")
        pg_dirs.append(resolved)
    return Snapshot(paths, pg_dirs)


def allowlisted(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in ALLOWLIST)


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def held_reason(path: str, snap: Snapshot) -> str | None:
    """Keep reason when a process or postgres holds the path, else None."""
    if snap.open_paths is None:
        return snap.failure or "open-file check did not run"
    if snap.pg_dirs is None:
        return snap.failure or "postgres check did not run"
    if any(_under(open_path, path) for open_path in snap.open_paths):
        return "open file or cwd inside"
    if any(_under(pg_dir, path) or _under(path, pg_dir) for pg_dir in snap.pg_dirs):
        return "running postgres data dir inside"
    return None


def quiet_reason(path: str, cutoff: float, deadline: float, pid_alive) -> tuple[str | None, float]:
    """(keep reason or None, newest mtime seen). None means the entry is stale."""
    newest = 0.0
    stack = [path]
    seen = 0
    while stack:
        if time.time() > deadline:
            return "time budget reached", newest
        current = stack.pop()
        seen += 1
        if seen > MAX_WALK_ENTRIES:
            return "walk bound reached", newest
        try:
            st = os.lstat(current)
        except OSError:
            return "unreadable", newest
        newest = max(newest, st.st_mtime)
        if st.st_mtime > cutoff:
            return "changed within --min-age", newest
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.name == ".git":
                            return "git checkout or worktree", newest
                        if entry.name == "postmaster.pid" and _pid_file_live(entry.path, pid_alive):
                            return "postmaster.pid names a live pid", newest
                        if not entry.is_symlink():
                            stack.append(entry.path)
            except OSError:
                return "unreadable", newest
    return None, newest


def _pid_file_live(path: str, pid_alive) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return True
    return first.isdigit() and pid_alive(int(first))


def judge(entry: os.DirEntry, host: TempHost, snap: Snapshot, cutoff: float, deadline: float) -> dict:
    """Verdict for one top-level entry: action is ``remove`` or ``keep``."""
    path = os.path.realpath(entry.path)
    row = {"path": path, "action": "keep", "reason": "", "age_s": 0.0}
    if allowlisted(entry.name):
        row["reason"] = "allowlisted name"
        return row
    held = held_reason(path, snap)
    if held:
        row["reason"] = held
        return row
    if entry.is_file(follow_symlinks=False):
        st = os.lstat(path)
        if st.st_mtime > cutoff:
            row["reason"] = "changed within --min-age"
            return row
        row.update(action="remove", reason="stale", age_s=time.time() - st.st_mtime)
        return row
    reason, newest = quiet_reason(path, cutoff, deadline, host.alive)
    if reason:
        row["reason"] = reason
        return row
    row.update(action="remove", reason="stale", age_s=time.time() - newest)
    return row


def candidates(host: TempHost):
    """Top-level entries owned by the current user, from each root. Symlinks are skipped."""
    for root in host.roots():
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                if entry.stat(follow_symlinks=False).st_uid != host.uid:
                    continue
            except OSError:
                continue
            yield entry


def plan(host: TempHost, min_age_s: float) -> tuple[list[dict], Snapshot]:
    snap = take_snapshot(host)
    cutoff = time.time() - min_age_s
    deadline = time.time() + TIME_BUDGET_S
    rows = [judge(entry, host, snap, cutoff, deadline) for entry in candidates(host)]
    for row in rows:
        if row["action"] == "remove":
            row["bytes"] = host.du_bytes(row["path"])
    return rows, snap


def _apply(host: TempHost, rows: list[dict], min_age_s: float) -> list[dict]:
    """Remove each stale row after a fresh open-file and postgres snapshot."""
    fresh = take_snapshot(host)
    cutoff = time.time() - min_age_s
    deadline = time.time() + TIME_BUDGET_S
    out: list[dict] = []
    for row in rows:
        if row["action"] != "remove":
            out.append(row)
            continue
        kept = held_reason(row["path"], fresh)
        if kept is None:
            entry = _entry_for(row["path"])
            again = judge(entry, host, fresh, cutoff, deadline) if entry is not None else None
            if again is None or again["action"] != "remove":
                kept = again["reason"] if again else "gone before the re-check"
        if kept:
            out.append(dict(row, action="keep", reason="kept on re-check: " + kept))
            continue
        ok = host.remove(row["path"])
        out.append(dict(row, action="removed" if ok else "remove-failed"))
    return out


def _entry_for(path: str) -> os.DirEntry | None:
    parent, name = os.path.split(path)
    try:
        for entry in os.scandir(parent):
            if entry.name == name:
                return entry
    except OSError:
        return None
    return None


def _human(num: float | None) -> str:
    if num is None:
        return "size unknown"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _age(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours >= 48:
        return f"{hours // 24}d"
    return f"{hours}h"


def cmd_temp(argv: list[str]) -> int:
    """``janitor temp``. Dry run unless ``--apply``. Exit 0 ok, 1 a removal failed or a check did not run, 2 usage."""
    apply = False
    min_age_raw = DEFAULT_MIN_AGE
    args = list(argv or [])
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--apply":
            apply = True
            index += 1
        elif token == "--min-age" and index + 1 < len(args):
            min_age_raw = args[index + 1]
            index += 2
        elif token in ("-h", "--help"):
            print(USAGE, end="")
            return errors.OK
        else:
            print(USAGE, end="", file=sys.stderr)
            return errors.USAGE
    min_age_s = worktrees.parse_duration(min_age_raw)
    if min_age_s is None:
        print(f"--min-age needs a duration like 5h, got {min_age_raw!r}", file=sys.stderr)
        return errors.USAGE

    host = TempHost()
    rows, snap = plan(host, min_age_s)
    if snap.open_paths is None or snap.pg_dirs is None:
        print(f"temp janitor: {snap.failure}; keeping everything", file=sys.stderr)
        print(f"ANSWER: nothing removed; {snap.failure}, so every entry is kept.")
        return errors.CHECK_FAILED
    if apply:
        rows = _apply(host, rows, min_age_s)

    failed = 0
    kept_reasons: dict[str, int] = {}
    total_bytes = 0
    unknown_bytes = 0
    removed = 0
    for row in rows:
        if row["action"] == "keep":
            kept_reasons[row["reason"]] = kept_reasons.get(row["reason"], 0) + 1
            continue
        size = row.get("bytes")
        if size is None:
            unknown_bytes += 1
        else:
            total_bytes += size
        if row["action"] == "remove-failed":
            failed += 1
            print(f"  remove failed  {row['path']}")
        elif row["action"] == "removed":
            removed += 1
            print(f"  removed        {row['path']}  age {_age(row['age_s'])}  {_human(size)}")
        else:
            print(f"  would remove   {row['path']}  age {_age(row['age_s'])}  {_human(size)}")
    if kept_reasons:
        summary = ", ".join(f"{reason} {count}" for reason, count in sorted(kept_reasons.items(), key=lambda kv: -kv[1]))
        print(f"kept {sum(kept_reasons.values())}: {summary}")
    stale = len(rows) - sum(kept_reasons.values())
    print(_answer(apply, rows, stale, removed, total_bytes, unknown_bytes, failed))
    return errors.CHECK_FAILED if failed else errors.OK


def _answer(apply: bool, rows: list[dict], stale: int, removed: int, total: int, unknown: int, failed: int) -> str:
    size_text = _human(total) + (f" plus {unknown} entries of unknown size" if unknown else "")
    if apply:
        text = f"ANSWER: removed {removed} of {len(rows)} temp entries, freeing {size_text}"
        return text + (f"; {failed} failed to remove." if failed else ".")
    if not stale:
        return f"ANSWER: nothing stale; all {len(rows)} temp entries kept."
    return (
        f"ANSWER: {stale} of {len(rows)} temp entries would be removed, {size_text}; "
        "re-run with --apply to remove them."
    )
