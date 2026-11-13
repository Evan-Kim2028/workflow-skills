"""Stop orphaned local Postgres servers: ``agent_dash.py janitor postgres``.

Finished agent runs and killed pytest sessions leave postmasters running
(checkpointer, walwriter and autovacuum keep memory and background I/O busy
for days). A server is stopped only when every rule below holds. A check
that cannot run keeps the server.

Rules, all required:

- the postmaster belongs to the current user and carries ``-D <datadir>``;
- the data dir is under the temp roots (``$TMPDIR``, ``DARWIN_USER_TEMP_DIR``,
  ``/private/tmp``, ``/tmp``) or the scratch root, and is not under
  ``/opt/homebrew``, ``/usr/local``, ``/Library`` or ``~/Library``;
- no ancestor of the data dir is a git checkout (a ``.git`` entry);
- the postmaster has run longer than ``--min-age``;
- a relative ``-D`` resolves through the process cwd, which the postmaster
  sets to its data dir after startup;
- a pytest cluster (``pytest-of-<user>/pytest-N``) has no live process that
  names or works in its run dir, and ``psql`` is available to prove it idle;
- a scratch cluster's scratch run is not live (the scratch gc liveness check);
- no client backend is connected. ``psql`` answers this on the port and
  socket named in ``postmaster.pid``, with a 3 s timeout. When ``psql`` is not
  installed, the orphan rule applies instead: the parent is launchd (ppid 1)
  and no process outside the server's own tree works in the data dir.

A stop is ``pg_ctl -D <datadir> stop -m fast``. When pg_ctl is missing or
fails, the postmaster gets SIGTERM, but only after ``postmaster.pid`` still
names that pid. Nothing is ever sent SIGKILL, and the data dir is never
deleted: scratch gc and the temp cleanup own that. Every apply re-judges each
candidate just before it stops, so a server that became busy in between is kept.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from dash import errors, scratchdir, scratchgc, worktrees

PROTECTED_PREFIXES = ("/opt/homebrew", "/usr/local", "/Library")
PSQL_TIMEOUT_S = 3
PG_CTL_TIMEOUT_S = 45
PS_TIMEOUT_S = 30
LSOF_TIMEOUT_S = 60
STOP_POLL_S = 0.5
STOP_WAIT_S = 30
CLIENT_COUNT_SQL = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()"
)
HELPER_TITLES = (
    "checkpointer",
    "background writer",
    "walwriter",
    "wal writer",
    "autovacuum launcher",
    "autovacuum worker",
    "logical replication launcher",
    "archiver",
    "stats collector",
    "startup",
    "walsummarizer",
    "wal summarizer",
    "io worker",
)
PYTEST_RUN_RE = re.compile(r"^(.*/pytest-of-[^/]+/pytest-\d+)(?:/|$)")

USAGE = """usage: agent-dash janitor postgres [--min-age 1h] [--apply]

Dry run by default: lists every local postgres server owned by this user,
with the action and reason for each. --apply stops the orphaned ones with
pg_ctl (SIGTERM fallback). Data dirs are never deleted.
"""


@dataclass(frozen=True)
class Proc:
    """One row of the process table."""

    pid: int
    ppid: int
    user: str
    age_s: float
    argv: tuple[str, ...]


@dataclass
class Index:
    """Process table plus the cwd map (None when lsof could not run)."""

    procs: list[Proc]
    cwds: dict[int, str] | None
    children: dict[int, list[int]]

    def tree(self, pid: int) -> set[int]:
        """The pid and all its descendants."""
        out = {pid}
        stack = [pid]
        while stack:
            for child in self.children.get(stack.pop(), []):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out


class Host:
    """Real machine access. Tests subclass it and override each method."""

    def __init__(self) -> None:
        self.user = getpass.getuser()
        self.home = os.path.realpath(os.path.expanduser("~"))

    def ps_text(self) -> str | None:
        return _run_text(["ps", "-axo", "pid=,ppid=,user=,etime=,command="], PS_TIMEOUT_S)

    def cwd_text(self) -> str | None:
        return _run_text(["lsof", "-a", "-d", "cwd", "-Fpn", "-u", self.user], LSOF_TIMEOUT_S)

    def has(self, name: str) -> bool:
        return shutil.which(name) is not None

    def roots(self) -> tuple[str, ...]:
        candidates = [
            os.environ.get("TMPDIR", ""),
            os.environ.get("DARWIN_USER_TEMP_DIR", ""),
            "/private/tmp",
            "/tmp",
            str(scratchdir.scratch_root()),
        ]
        out: list[str] = []
        for raw in candidates:
            if not raw.strip():
                continue
            root = os.path.realpath(os.path.expanduser(raw.strip()))
            if root in ("/", self.home) or root in out:
                continue
            out.append(root)
        return tuple(out)

    def pid_file_lines(self, datadir: str) -> list[str] | None:
        try:
            with open(os.path.join(datadir, "postmaster.pid"), encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        except OSError:
            return None

    def psql(self, args: list[str]) -> tuple[int, str, str]:
        env = dict(os.environ, PGCONNECT_TIMEOUT=str(PSQL_TIMEOUT_S))
        try:
            proc = subprocess.run(
                args, check=False, capture_output=True, text=True,
                timeout=PSQL_TIMEOUT_S + 2, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1, "", ""
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def pg_ctl_stop(self, datadir: str) -> int:
        try:
            proc = subprocess.run(
                ["pg_ctl", "-D", datadir, "stop", "-m", "fast", "-w", "-t", "30"],
                check=False, capture_output=True, text=True, timeout=PG_CTL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1
        return proc.returncode

    def scratch_verdict(self, datadir: str) -> str | None:
        """Why the scratch run owning ``datadir`` is live (keep), or None."""
        root = os.path.realpath(str(scratchdir.scratch_root()))
        if not _under(datadir, root):
            return None
        parts = os.path.relpath(datadir, root).split(os.sep)
        if len(parts) < 2:
            return None
        label = parts[1]
        owner = scratchdir.read_owner(os.path.join(root, parts[0], label))
        if owner is None:
            return None
        live = scratchgc.live_labels()
        if live is None:
            return "scratch liveness check did not run"
        if label in live:
            return "scratch run has a live launcher"
        state = scratchgc._run_state(str(owner.get("run_dir") or ""), label)
        if state is None:
            return "scratch run state unreadable"
        if state == "live":
            return "scratch run still running"
        return None

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def signal(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _run_text(args: list[str], timeout: int) -> str | None:
    try:
        proc = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    return proc.stdout or ""


def parse_etime(raw: str) -> float | None:
    """``[[dd-]hh:]mm:ss`` (ps etime) to seconds, or None."""
    days = 0
    rest = raw.strip()
    if "-" in rest:
        day_text, rest = rest.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    try:
        fields = [int(part) for part in rest.split(":")]
    except ValueError:
        return None
    if len(fields) == 2:
        hours, minutes, seconds = 0, fields[0], fields[1]
    elif len(fields) == 3:
        hours, minutes, seconds = fields
    else:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def parse_ps(text: str) -> list[Proc]:
    """Rows of ``ps -axo pid=,ppid=,user=,etime=,command=``."""
    procs: list[Proc] = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        age = parse_etime(parts[3])
        if age is None:
            continue
        try:
            argv = tuple(shlex.split(parts[4]))
        except ValueError:
            argv = tuple(parts[4].split())
        procs.append(Proc(pid, ppid, parts[2], age, argv))
    return procs


def parse_lsof_cwds(text: str) -> dict[int, str]:
    """``lsof -Fpn`` output (cwd fd only) to a pid-to-cwd map."""
    cwds: dict[int, str] = {}
    pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def data_dir_arg(argv: tuple[str, ...]) -> str | None:
    """The value after ``-D`` in a postmaster command line."""
    for index, token in enumerate(argv[:-1]):
        if token == "-D":
            return argv[index + 1]
    return None


def resolve_datadir(raw: str, cwd: str | None) -> str | None:
    """Absolute, real data dir for a ``-D`` value, or None when unresolvable.

    A relative value is matched against the postmaster's cwd. The postmaster
    chdirs into its data dir at startup, so the cwd is normally that dir.
    """
    if os.path.isabs(raw):
        return os.path.realpath(raw)
    if not cwd:
        return None
    want = os.path.normpath(raw)
    have = os.path.normpath(cwd)
    if have == want or have.endswith(os.sep + want):
        return os.path.realpath(cwd)
    return None


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def is_protected(datadir: str, home: str) -> bool:
    prefixes = PROTECTED_PREFIXES + (os.path.join(home, "Library"),)
    return any(_under(datadir, prefix) for prefix in prefixes)


def in_repo_checkout(path: str) -> bool:
    """True when the path or an ancestor holds a ``.git`` entry."""
    current = path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def is_helper_title(title: str) -> bool:
    """True for a background helper's ps title (``io worker 3`` counts), not a client."""
    for helper in HELPER_TITLES:
        if title == helper:
            return True
        if title.startswith(helper + " ") and title[len(helper) + 1:].isdigit():
            return True
    return False


def client_backends(index: Index, pid: int) -> list[str]:
    """Titles of the postmaster's children that are client backends, read from the process table."""
    titles: list[str] = []
    for child in index.procs:
        if child.ppid != pid or not child.argv or child.argv[0] != "postgres:":
            continue
        title = " ".join(child.argv[1:])
        if not is_helper_title(title):
            titles.append(title)
    return titles


def pytest_run_dir(datadir: str) -> str | None:
    match = PYTEST_RUN_RE.match(datadir)
    return match.group(1) if match else None


def _run_dir_named_live(run_dir: str, index: Index, tree: set[int]) -> bool:
    for proc in index.procs:
        if proc.pid in tree:
            continue
        if any(run_dir in token for token in proc.argv):
            return True
        cwd = (index.cwds or {}).get(proc.pid)
        if cwd and _under(cwd, run_dir):
            return True
    return False


def _pid_file_endpoint(lines: list[str]) -> tuple[str | None, str | None]:
    """(port, socket dir) from postmaster.pid lines 4 and 5."""
    port = lines[3].strip() if len(lines) > 3 else ""
    socket_dir = lines[4].strip() if len(lines) > 4 else ""
    return (port if port.isdigit() else None), (socket_dir or None)


def _client_args(port: str, socket_dir: str | None) -> list[str]:
    host = socket_dir if socket_dir and socket_dir.startswith("/") else "127.0.0.1"
    return ["psql", "-X", "-A", "-t", "-w", "-h", host, "-p", port, "-d", "postgres", "-c", CLIENT_COUNT_SQL]


def _row(proc: Proc, datadir: str, action: str, reason: str) -> dict:
    return {"pid": proc.pid, "age_s": proc.age_s, "datadir": datadir, "action": action, "reason": reason}


def judge(host: Host, proc: Proc, raw: str, index: Index, roots: tuple[str, ...], min_age_s: float) -> dict:
    """The keep or stop verdict for one postmaster, with the reason."""
    if index.cwds is None:
        return _row(proc, raw, "keep", "process cwd check did not run")
    datadir = resolve_datadir(raw, index.cwds.get(proc.pid))
    if datadir is None:
        return _row(proc, raw, "keep", "relative data dir, cwd unknown")
    if is_protected(datadir, host.home):
        return _row(proc, datadir, "keep", "protected path")
    if not any(_under(datadir, root) for root in roots):
        return _row(proc, datadir, "keep", "outside temp and scratch roots")
    if in_repo_checkout(datadir):
        return _row(proc, datadir, "keep", "inside a repo checkout")
    if proc.age_s < min_age_s:
        return _row(proc, datadir, "keep", "younger than --min-age")
    tree = index.tree(proc.pid)
    run_dir = pytest_run_dir(datadir)
    has_psql = host.has("psql")
    if run_dir and _run_dir_named_live(run_dir, index, tree):
        return _row(proc, datadir, "keep", "pytest run still live")
    if run_dir and not has_psql:
        return _row(proc, datadir, "keep", "pytest cluster needs psql to prove the run finished")
    scratch_reason = host.scratch_verdict(datadir)
    if scratch_reason:
        return _row(proc, datadir, "keep", scratch_reason)
    keep_reason, idle_text = _idle_check(host, proc, datadir, index, tree, has_psql)
    if keep_reason is not None:
        return _row(proc, datadir, "keep", keep_reason)
    return _row(proc, datadir, "stop", "orphaned: " + idle_text)


def _idle_check(
    host: Host, proc: Proc, datadir: str, index: Index, tree: set[int], has_psql: bool
) -> tuple[str | None, str]:
    """(keep reason or None, text saying why the server is idle).

    A server that answers but refuses this OS user (a FATAL from psql, as with
    clusters whose superuser has another name) cannot be queried, so it falls
    through to the orphan rule. Any other psql failure keeps the server.
    """
    port, socket_dir = _pid_file_endpoint(host.pid_file_lines(datadir) or [])
    backends = client_backends(index, proc.pid)
    if backends:
        return f"{len(backends)} client backend(s) in process table", ""
    refused = False
    if has_psql and port is not None:
        rc, out, err = host.psql(_client_args(port, socket_dir))
        if rc == 0:
            try:
                clients = int(out.strip())
            except ValueError:
                return "client check did not answer", ""
            if clients:
                return f"{clients} client connection(s)", ""
            return None, "no client connections"
        if "FATAL" not in err:
            return "client check did not answer", ""
        refused = True
    if proc.ppid != 1:
        return "no client check; launcher still running", ""
    for other in index.procs:
        if other.pid in tree:
            continue
        cwd = (index.cwds or {}).get(other.pid)
        if cwd and _under(cwd, datadir):
            return "no client check; a process works in the data dir", ""
    why = "psql role refused" if refused else "no psql"
    return None, f"no client check ({why}); launcher gone, no process in data dir"


def build_index(host: Host) -> Index:
    """Process table and cwd map. Raises RuntimeError when ps cannot run."""
    text = host.ps_text()
    if text is None:
        raise RuntimeError("ps did not run; nothing judged")
    procs = parse_ps(text)
    cwd_raw = host.cwd_text()
    cwds = None if cwd_raw is None else parse_lsof_cwds(cwd_raw)
    children: dict[int, list[int]] = {}
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc.pid)
    return Index(procs=procs, cwds=cwds, children=children)


def plan(host: Host, min_age_s: float) -> list[dict]:
    """One verdict row per postmaster owned by the current user."""
    index = build_index(host)
    roots = host.roots()
    rows: list[dict] = []
    for proc in index.procs:
        if proc.user != host.user or not proc.argv or os.path.basename(proc.argv[0]) != "postgres":
            continue
        raw = data_dir_arg(proc.argv)
        if raw is None:
            continue
        rows.append(judge(host, proc, raw, index, roots, min_age_s))
    return rows


def stop(host: Host, datadir: str, pid: int) -> tuple[bool, str]:
    """Stop one postmaster. Returns (stopped, how)."""
    if host.has("pg_ctl") and host.pg_ctl_stop(datadir) == 0:
        return True, "pg_ctl stop -m fast"
    if not host.alive(pid):
        return True, "already gone"
    lines = host.pid_file_lines(datadir)
    if not lines or lines[0].strip() != str(pid):
        return False, "postmaster.pid does not name this pid; left running"
    host.signal(pid, signal.SIGTERM)
    for _ in range(int(STOP_WAIT_S / STOP_POLL_S)):
        if not host.alive(pid):
            return True, "SIGTERM"
        host.sleep(STOP_POLL_S)
    return (not host.alive(pid)), "SIGTERM sent; still running after wait"


def _age_text(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _line(verb: str, row: dict) -> str:
    return f"  {verb:<12} pid {row['pid']:<7} age {_age_text(row['age_s']):<8} {row['datadir']}  ({row['reason']})"


def cmd_postgres(argv: list[str]) -> int:
    """``janitor postgres``. Dry run unless ``--apply``. Exit 0 ok, 1 a stop failed, 2 usage."""
    apply = False
    min_age_raw = "1h"
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
            return 0
        else:
            print(USAGE, end="", file=sys.stderr)
            return errors.USAGE
    min_age_s = worktrees.parse_duration(min_age_raw)
    if min_age_s is None:
        print(f"--min-age needs a duration like 1h, got {min_age_raw!r}", file=sys.stderr)
        return errors.USAGE

    host = Host()
    try:
        rows = plan(host, min_age_s)
    except RuntimeError as exc:
        print(f"postgres janitor: {exc}", file=sys.stderr)
        return errors.CHECK_FAILED
    if not rows:
        print(f"no local postgres servers owned by {host.user}")
        print(f"ANSWER: no local postgres servers owned by {host.user}; nothing to stop.")
        return errors.OK

    if apply:
        rows = _apply(host, rows, min_age_s)
    failed = 0
    for row in rows:
        if row["action"] == "stop":
            print(_line("would stop", row))
        elif row["action"] == "stopped":
            print(_line("stopped", row) + f" [{row['detail']}]")
        elif row["action"] == "stop-failed":
            failed += 1
            print(_line("stop failed", row) + f" [{row['detail']}]")
        else:
            print(_line("keep", row))
    print(_answer(rows, apply))
    return errors.CHECK_FAILED if failed else errors.OK


def _apply(host: Host, rows: list[dict], min_age_s: float) -> list[dict]:
    """Re-judge each candidate just before its stop; stop only the ones still orphaned."""
    fresh = {row["pid"]: row for row in plan(host, min_age_s)}
    out: list[dict] = []
    for row in rows:
        if row["action"] != "stop":
            out.append(row)
            continue
        now = fresh.get(row["pid"])
        if now is None or now["action"] != "stop" or now["datadir"] != row["datadir"]:
            reason = now["reason"] if now else "server gone before the stop"
            out.append(dict(row, action="keep", reason=f"kept on re-check: {reason}"))
            continue
        ok, detail = stop(host, row["datadir"], row["pid"])
        out.append(dict(row, action="stopped" if ok else "stop-failed", detail=detail))
    return out


def _answer(rows: list[dict], apply: bool) -> str:
    total = len(rows)
    if apply:
        stopped = sum(1 for row in rows if row["action"] == "stopped")
        failed = sum(1 for row in rows if row["action"] == "stop-failed")
        text = f"ANSWER: stopped {stopped} orphaned local postgres server(s) of {total}"
        return text + (f"; {failed} failed to stop" if failed else "") + "."
    candidates = sum(1 for row in rows if row["action"] == "stop")
    if not candidates:
        return f"ANSWER: no orphaned local postgres servers; all {total} kept."
    return (
        f"ANSWER: {candidates} of {total} local postgres servers would be stopped; "
        "re-run with --apply to stop them."
    )
