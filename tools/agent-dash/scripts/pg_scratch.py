#!/usr/bin/env python3
"""Start a throwaway Postgres 17 in ``$AGENT_SCRATCH`` with quiet, capped logging.

Workers used to hand-roll ``initdb`` + ``pg_ctl`` for a scratch cluster, and a
failed migration then wrote every statement it rejected into a plain log next
to the data dir: one hub run left a 3.8 GB ``pg17.log`` beside a 206 MB data
dir, and ``scratch gc`` kept the folder because it was younger than 7 days.

This helper is the one way to start such a cluster. It always passes the
settings below, so the log holds warnings and errors only, and the collector
truncates it hourly and rotates it at 50 MB:

- ``log_min_messages=warning``, ``log_statement=none``,
  ``log_min_duration_statement=-1``: no statement or duration logging;
- ``log_min_error_statement=panic``: a rejected statement is not echoed with
  its full text (the default, ``error``, is what filled the 3.8 GB log);
- ``log_checkpoints=off``, ``log_connections=off``, ``log_disconnections=off``;
- ``logging_collector=on`` with ``log_rotation_size=50MB``,
  ``log_rotation_age=1h`` and ``log_truncate_on_rotation=on``, writing
  ``<datadir>/log/pg17.log``: the file is truncated at each hour boundary, so
  it cannot grow without bound.

usage:
  pg_scratch.py start  [--dir DIR] [--port N] [--bin DIR] [--no-init]
  pg_scratch.py stop   [--dir DIR]
  pg_scratch.py status [--dir DIR]

``start`` prints ``PGPORT``, ``PGHOST`` and a ``psql`` URL on success. The
data dir defaults to ``$AGENT_SCRATCH/pg17`` (or
``~/.cache/agent-dash/scratch/pg17`` when ``AGENT_SCRATCH`` is unset).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

QUIET_SETTINGS = (
    "log_min_messages=warning",
    "log_statement=none",
    "log_min_duration_statement=-1",
    "log_min_error_statement=panic",
    "log_checkpoints=off",
    "log_connections=off",
    "log_disconnections=off",
    "logging_collector=on",
    "log_rotation_size=50MB",
    "log_rotation_age=1h",
    "log_truncate_on_rotation=on",
    "log_filename=pg17.log",
)
# Throwaway-cluster settings that make a test load fast; they are not about logs.
SPEED_SETTINGS = (
    "fsync=off",
    "full_page_writes=off",
    "max_connections=40",
)
BIN_CANDIDATES = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/usr/local/opt/postgresql@17/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)
START_TIMEOUT_S = 120
STOP_TIMEOUT_S = 30
USAGE = """usage:
  pg_scratch.py start  [--dir DIR] [--port N] [--bin DIR] [--no-init]
  pg_scratch.py stop   [--dir DIR]
  pg_scratch.py status [--dir DIR]
"""


def default_dir() -> Path:
    """``$AGENT_SCRATCH/pg17``, else the shared scratch root's ``pg17``."""
    scratch = os.environ.get("AGENT_SCRATCH") or ""
    if scratch:
        return Path(scratch) / "pg17"
    return Path.home() / ".cache" / "agent-dash" / "scratch" / "pg17"


def find_bin(explicit: str | None = None) -> Path | None:
    """Directory holding ``initdb``, ``pg_ctl`` and ``psql``."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    pg_config = shutil.which("pg_config")
    if pg_config:
        try:
            out = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                candidates.append(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    candidates.extend(BIN_CANDIDATES)
    for name in ("initdb", "pg_ctl", "psql"):
        found = shutil.which(name)
        if found:
            candidates.append(str(Path(found).parent))
    for candidate in candidates:
        base = Path(candidate)
        if all((base / name).is_file() for name in ("initdb", "pg_ctl", "psql")):
            return base
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_options(port: int, sockdir: Path) -> list[str]:
    """The ``-c`` options for a quiet scratch server, in ``pg_ctl -o`` order."""
    options = [
        "-p", str(int(port)),
        "-k", str(sockdir),
        "-c", "listen_addresses=127.0.0.1",
    ]
    for setting in SPEED_SETTINGS + QUIET_SETTINGS:
        options.extend(["-c", setting])
    return options


def _run(argv: list[str], timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)


def _cluster_env() -> dict:
    """Environment for initdb/pg_ctl: a valid locale, or the postmaster aborts on macOS."""
    env = dict(os.environ)
    env.setdefault("LC_ALL", "C")
    env.setdefault("LANG", "C")
    return env


def init_cluster(bindir: Path, datadir: Path) -> tuple[bool, str]:
    """``initdb`` when the data dir is empty. ``(ok, message)``."""
    if (datadir / "PG_VERSION").is_file():
        return True, "data dir already initialised"
    datadir.parent.mkdir(parents=True, exist_ok=True)
    # Without a valid locale the postmaster aborts with "became multithreaded
    # during startup" on macOS, which is what the old hand-rolled starts hit.
    env = _cluster_env()
    try:
        done = _run([str(bindir / "initdb"), "-D", str(datadir), "-U", "postgres",
                     "--auth=trust", "-E", "UTF8"], timeout=300, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"initdb could not run: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:]
        return False, f"initdb exited {done.returncode}: {' '.join(tail)}"
    return True, "initialised"


def start(datadir: Path, bindir: Path, port: int | None = None, init: bool = True) -> int:
    if init:
        ok, message = init_cluster(bindir, datadir)
        if not ok:
            print(message, file=sys.stderr)
            return 1
    chosen = int(port) if port else free_port()
    logfile = datadir / "startup.log"
    argv = [
        str(bindir / "pg_ctl"), "-D", str(datadir), "-l", str(logfile),
        "-o", " ".join(server_options(chosen, datadir)),
        "-w", "-t", str(START_TIMEOUT_S), "start",
    ]
    try:
        done = _run(argv, timeout=START_TIMEOUT_S + 30, env=_cluster_env())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"pg_ctl could not run: {exc}", file=sys.stderr)
        return 1
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-400:]
        print(f"pg_ctl start exited {done.returncode}: {tail}", file=sys.stderr)
        return 1
    print(f"PGPORT={chosen}")
    print(f"PGHOST={datadir}")
    print(f"psql postgresql://postgres@127.0.0.1:{chosen}/postgres")
    print(f"log {datadir / 'log' / 'pg17.log'} (warnings only, truncated hourly, 50 MB rotation)")
    return 0


def stop(datadir: Path, bindir: Path) -> int:
    argv = [str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "fast",
            "-w", "-t", str(STOP_TIMEOUT_S), "stop"]
    try:
        done = _run(argv, timeout=STOP_TIMEOUT_S + 30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"pg_ctl could not run: {exc}", file=sys.stderr)
        return 1
    if done.returncode != 0:
        print(f"pg_ctl stop exited {done.returncode}: {(done.stderr or done.stdout or '').strip()[-300:]}",
              file=sys.stderr)
        return 1
    print(f"stopped {datadir}")
    return 0


def status(datadir: Path, bindir: Path) -> int:
    try:
        done = _run([str(bindir / "pg_ctl"), "-D", str(datadir), "status"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"pg_ctl could not run: {exc}", file=sys.stderr)
        return 1
    print((done.stdout or done.stderr or "").strip())
    return 0 if done.returncode == 0 else 3


def _parse(argv: list[str]) -> dict | None:
    allowed = {"--dir": False, "--port": False, "--bin": False, "--no-init": True}
    out: dict = {}
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


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("start", "stop", "status"):
        print(USAGE, end="", file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    flags = _parse(rest)
    if flags is None:
        print(USAGE, end="", file=sys.stderr)
        return 2
    datadir = Path(flags.get("--dir") or default_dir()).expanduser()
    bindir = find_bin(flags.get("--bin"))
    if bindir is None:
        print("no Postgres 17 binaries found (initdb, pg_ctl, psql); pass --bin DIR", file=sys.stderr)
        return 1
    if verb == "start":
        port = flags.get("--port")
        try:
            port = int(port) if port is not None else None
        except ValueError:
            print(f"--port needs a whole number, got {port!r}", file=sys.stderr)
            return 2
        return start(datadir, bindir, port=port, init=not flags.get("--no-init"))
    if verb == "stop":
        return stop(datadir, bindir)
    return status(datadir, bindir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
