"""Dispatch for the PATH shims ``ssh``, ``pytest``, ``python``, ``python3`` and ``uv``.

Each shell wrapper in this folder runs ``dispatch.py <its own name> <args>``. The
real binary is the first match on PATH with this folder removed, so a shim never
calls itself. Anything that is not a gated run is replaced by the real binary
with the same argv.

Gated runs:

- ``ssh``: only when the target host is in ``shims.ssh_gated_hosts`` of
  ``broker.json`` (no default: the production host aliases are set locally, so
  they stay out of the repo). Every run rewrites ``slots/ssh_hosts`` for the sh fast path. Others, such as git over ssh to github.com, exec
  the real ssh untouched. Takes an ``ssh`` slot.
- ``pytest``: ``pytest``, ``python[3] -m pytest``, ``uv run pytest`` and
  ``uv run python[3] -m pytest``. Takes a ``pytest`` slot, then runs the real
  command under ``memcap.sh 8192``.

Environment: ``AGENTQ_SHIMS=0`` turns the shims off for pi (set by
``launch_pi.py``). ``AGENTQ_SLOT_HELD`` lists the slot kinds this process tree
already holds, so a pytest started inside a held pytest does not wait for its
parent's slot. ``AGENTQ_MEMCAPPED`` marks a process already under the memory cap.
``AGENTQ_MEMCAP`` overrides the memcap script path (tests use it).
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import sys
from pathlib import Path

SHIMS_DIR = Path(__file__).resolve().parent
ROOT = SHIMS_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentq import db, slots  # noqa: E402

PYTEST_MEMCAP_MB = 8192
PYTHON_NAMES = ("python", "python3")
SSH_VALUE_FLAGS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
HELD_ENV = "AGENTQ_SLOT_HELD"
MEMCAPPED_ENV = "AGENTQ_MEMCAPPED"
MEMCAP_ENV = "AGENTQ_MEMCAP"


class ShimError(RuntimeError):
    """The real binary a shim wraps cannot be found."""


def real_binary(name: str, environ) -> str:
    """The first executable ``name`` on PATH outside the shim folder.

    Raises ``ShimError`` when there is none, so the shim fails with a message
    instead of recursing into itself.
    """
    shim = os.path.realpath(SHIMS_DIR)
    for entry in (environ.get("PATH") or "").split(os.pathsep):
        if not entry or os.path.realpath(entry) == shim:
            continue
        candidate = os.path.join(entry, name)
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if os.path.realpath(candidate) == os.path.join(shim, name):
            continue
        return candidate
    raise ShimError(f"agentq shim: no real `{name}` on PATH outside {shim}; put it on PATH or set AGENTQ_SHIMS=0")


def ssh_host(args) -> str | None:
    """The destination host of an ssh argv, or None when there is none (for example ``ssh -V``).

    Options are skipped; an option whose value is its own argument (``-p 22``) skips
    that value too, while an attached value (``-p22``, ``-oBatchMode=yes``) does not.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return _host_name(args[index + 1]) if index + 1 < len(args) else None
        if arg.startswith("-") and len(arg) > 1:
            index += 1 + _takes_separate_value(arg)
            continue
        return _host_name(arg)
    return None


def _takes_separate_value(arg: str) -> int:
    """1 when the option cluster ``arg`` ends in a value flag, so its value is the next argv word."""
    for position, char in enumerate(arg[1:], start=1):
        if char in SSH_VALUE_FLAGS:
            return 1 if position == len(arg) - 1 else 0
    return 0


def _host_name(target: str) -> str:
    """Host from ``host``, ``user@host``, ``[v6]``, or ``ssh://user@host:port``, lowercased."""
    scheme = target.startswith("ssh://")
    host = target[len("ssh://"):] if scheme else target
    host = host.rsplit("@", 1)[-1]
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif scheme and ":" in host:
        host = host.split(":", 1)[0]
    return host.lower()


def ssh_gated(args, patterns) -> bool:
    """True when the ssh argv targets a host matching one of ``patterns`` (shell-style globs)."""
    host = ssh_host(args)
    return host is not None and any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in patterns)


def is_pytest(name: str, args) -> bool:
    """True for the pytest invocations this shim gates."""
    if name == "pytest":
        return True
    if name in PYTHON_NAMES:
        return list(args[:2]) == ["-m", "pytest"]
    if name == "uv":
        return _uv_runs_pytest(args)
    return False


def _uv_runs_pytest(args) -> bool:
    """``uv run [options] pytest`` or ``uv run [options] python[3] -m pytest``."""
    if not args or args[0] != "run":
        return False
    rest = list(args[1:])
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        index += 1
    head = rest[index:]
    if head[:1] == ["pytest"]:
        return True
    return head[:1] in (["python"], ["python3"]) and head[1:3] == ["-m", "pytest"]


def run_label(environ) -> str:
    """The run label a slot records: ``AGENTQ_RUN``, else ``AGENTQ_JOB``, else ``-``."""
    return (environ.get("AGENTQ_RUN") or environ.get("AGENTQ_JOB") or "-").strip()


def memcap_command(command, environ) -> tuple[list[str], bool]:
    """``memcap.sh 8192 <command>`` and True, or the command unchanged and False when the script is absent."""
    script = Path(environ.get(MEMCAP_ENV) or (Path.home() / ".claude" / "scripts" / "memcap.sh"))
    if not script.is_file():
        print(f"agentq shim: memcap.sh not found at {script}; pytest runs without the memory cap", file=sys.stderr)
        return list(command), False
    return ["/bin/bash", str(script), str(PYTEST_MEMCAP_MB), *command], True


def _held_kinds(environ) -> set[str]:
    return {kind for kind in (environ.get(HELD_ENV) or "").split(",") if kind}


def _child_env(environ, held: set[str], kind: str | None, capped: bool) -> dict:
    env = dict(environ)
    env[HELD_ENV] = ",".join(sorted(held | ({kind} if kind else set())))
    if capped:
        env[MEMCAPPED_ENV] = "1"
    return env


def _run(command, env) -> int:
    """Run the child in the foreground and return its exit code (128+signal when killed).

    A Ctrl-C reaches the child too, so the shim keeps waiting until the child exits.
    """
    process = subprocess.Popen(command, env=env)
    while True:
        try:
            code = process.wait()
            break
        except KeyboardInterrupt:
            continue
    return code if code >= 0 else 128 - code


def run_gated(kind: str, command, environ, home) -> int:
    """Run ``command`` while holding a ``kind`` slot; pytest also runs under the memory cap."""
    held = _held_kinds(environ)
    if kind in held:
        return _run(command, _child_env(environ, held, None, capped=False))
    limits = slots.load_limits(home)
    admit = None
    capped = False
    shown = shlex.join(command)
    if kind == "pytest":
        admit = slots.pytest_admission(limits, slots.Gauges(home, ttl_s=limits.gauge_ttl_s))
        if not environ.get(MEMCAPPED_ENV):
            command, capped = memcap_command(command, environ)
    with slots.acquire(kind, limit=slots.kind_limit(limits, kind), home=home, label=run_label(environ),
                       command=shown, admit=admit):
        return _run(command, _child_env(environ, held, kind, capped))


def main(argv, environ=None) -> int:
    """Run one shim invocation. ``argv`` is ``[shim name, *args]``; returns the exit code.

    Pass-through calls replace this process with the real binary and never return.
    """
    environ = os.environ if environ is None else environ
    name, args = argv[0], list(argv[1:])
    try:
        real = real_binary(name, environ)
    except ShimError as exc:
        print(exc, file=sys.stderr)
        return 127
    home = db.home_dir(environ)
    command = [real, *args]
    slots.write_ssh_hosts(home)
    if name == "ssh" and ssh_gated(args, slots.gated_hosts(home)):
        return run_gated("ssh", command, environ, home)
    if is_pytest(name, args):
        return run_gated("pytest", command, environ, home)
    os.execve(real, [name, *args], dict(environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
