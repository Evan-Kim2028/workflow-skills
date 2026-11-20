"""Dispatch for the PATH shims in this folder: ``ssh``, ``pytest``, ``python``, ``python3``, ``uv``, ``sleep``, ``pkill``, ``killall``, ``grep``, ``rg`` and ``find``.

Each shell wrapper runs ``dispatch.py <its own name> <args>`` only when the sh fast
path cannot decide. The real binary is the first match on PATH outside every shim
folder (recognised by content: a folder holding ``shim-lib.sh`` and ``dispatch.py``),
so a shim never calls itself or another copy of the shims. Anything that is not
refused is replaced by the real binary with the same argv.

Guards (every refusal prints ``BLOCKED: agentq guard: <reason>`` on stderr, exits 2,
and appends one row to ``guard_blocks.tsv`` under ``AGENTQ_HOME``: epoch, run label,
reason, command cut to 300 characters). ``AGENTQ_GUARD=0`` turns all of them off.

- ``pytest``, ``python[3] -m pytest``, ``uv run pytest``: refused with no test target,
  a directory target, or more than 25 test files.
- ``python``, ``python3``, ``uv``: the full gates ``lor-ci-local.sh``, ``ci-local.sh``
  and ``validate.sh``; ``select_pytests.py --all``; ``pre-commit`` with ``--all-files``
  or ``-a``; ``pyright`` with no file arguments.
- ``sleep``: a single sleep over 120 s; and a per-job budget. With ``AGENTQ_JOB`` set,
  the running total of seconds in ``guard/sleep-<job>`` under ``AGENTQ_HOME`` may not
  pass ``AGENTQ_SLEEP_BUDGET_S`` (default 1800). Fractional sleeps count rounded up to
  whole seconds. Budget refusals are not recorded in the total.
- ``grep`` (sh only): a recursive search (``-r``, ``-R``, ``--recursive``,
  ``--dereference-recursive``, ``-d recurse``, ``--directories=recurse``, or a short
  flag group holding r or R) whose search path is a broad root or a git checkout root
  (a directory holding ``.git``). The search paths are the non-option arguments after
  the pattern, or the current directory when there are none. Recursive grep on a
  subfolder or a file, and plain grep, pass.
- ``rg`` and ``find`` (sh only): a search whose root resolves to a broad folder. The
  broad roots are ``/``, ``/Users``, ``/System``, ``/Library``, ``/private``,
  ``/Volumes``, the home folder, ``Documents``, ``Documents/projects``,
  ``Documents/worktrees`` and each direct child of ``Documents/worktrees``, plus one
  path per line in ``AGENTQ_HOME/guard_broad_roots``. An rg with no path searches the
  current directory. A find's start paths are the words before its first expression.
- ``pkill`` and ``killall`` (sh only): always refused.

Gated runs (slots and the memory cap, not refusals):

- ``ssh``: only for hosts in ``shims.ssh_gated_hosts`` of ``broker.json`` (no default:
  the production host aliases are set locally, so they stay out of the repo). Every
  run rewrites ``slots/ssh_hosts`` for the sh fast path. Others, such as git over ssh
  to github.com, exec the real ssh untouched. Takes an ``ssh`` slot.
- ``pytest``: ``pytest``, ``python[3] -m pytest``, ``uv run pytest`` and
  ``uv run python[3] -m pytest``. Takes a ``pytest`` slot, then runs the real
  command under ``memcap.sh 8192``.

A gate script is caught only when a python or uv shim sees it as an argument
(``uv run scripts/validate.sh``, ``python3 scripts/validate.sh``). ``bash`` and ``sh``
are not shimmed, so ``bash scripts/validate.sh`` is not covered.

The shell fast paths live in ``shim-lib.sh`` and the wrappers. Only argv they cannot
settle (a fractional or suffixed sleep, a non-integer budget) reaches this module.

Environment: ``AGENTQ_SHIMS=0`` turns the shims off for pi (set by
``launch_pi.py``). ``AGENTQ_SLOT_HELD`` lists the slot kinds this process tree
already holds, so a pytest started inside a held pytest does not wait for its
parent's slot. ``AGENTQ_MEMCAPPED`` marks a process already under the memory cap.
``AGENTQ_PYTEST_SLOT_HELD`` is set to 1 for the child of a held pytest slot, and the
pytest plugin (``agentq/pyplugin/agentq_pytest_slot.py``) honours it, so a run that
the shim already gated takes no second slot. A plugin-held run sets it too, so the
shim treats a pytest started under it as already held.
``AGENTQ_MEMCAP`` overrides the memcap script path (tests use it).
"""

from __future__ import annotations

import fnmatch
import math
import os
import re
import shlex
import subprocess
import sys
import time
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
PYTEST_HELD_ENV = "AGENTQ_PYTEST_SLOT_HELD"
MEMCAP_ENV = "AGENTQ_MEMCAP"
GUARD_ENV = "AGENTQ_GUARD"
GUARD_LOG = "guard_blocks.tsv"
JOB_ENV = "AGENTQ_JOB"
SLEEP_BUDGET_ENV = "AGENTQ_SLEEP_BUDGET_S"
SLEEP_BUDGET_DEFAULT_S = 1800
REFUSED_SLEEP_PAUSE_S = 10
SLEEP_STATE_DIR = "guard"
GUARD_COMMAND_CHARS = 300
PYTEST_FILE_LIMIT = 25
SLEEP_LIMIT_S = 120
FULL_GATE_SCRIPTS = frozenset({"lor-ci-local.sh", "ci-local.sh", "validate.sh"})
PYTEST_VALUE_OPTS = frozenset({
    "-k", "-m", "-p", "-n", "-c", "-o", "-W", "-r", "--maxfail", "--rootdir", "--junitxml", "--durations",
    "--ignore", "--ignore-glob", "--deselect", "--confcutdir", "--basetemp", "--log-level", "--timeout",
    "--tb", "--dist",
})
PYRIGHT_VALUE_OPTS = frozenset({
    "-p", "--project", "--pythonpath", "--pythonversion", "--venvpath", "--typeshedpath", "--level",
})
SLEEP_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
SLEEP_ARG = re.compile(r"(\d+(?:\.\d*)?|\.\d+)([smhd]?)")
PYTEST_HINT = "Run only the test files for the code you changed: pytest <file.py> [...]"


class ShimError(RuntimeError):
    """The real binary a shim wraps cannot be found."""


def is_shim_dir(entry) -> bool:
    """True when ``entry`` is an agentq shim folder: it holds ``shim-lib.sh`` and ``dispatch.py``.

    Recognised by content, not by name, so a copy of the shims under any folder
    name is skipped. A shim that runs a shim is a fork bomb (2026-11-20: two shim
    folders on PATH and an exhausted sleep budget spawned 5795 sleep shims).
    """
    return os.path.isfile(os.path.join(entry, "shim-lib.sh")) and os.path.isfile(
        os.path.join(entry, "dispatch.py")
    )


def real_binary(name: str, environ) -> str:
    """The first executable ``name`` on PATH outside every shim folder.

    Raises ``ShimError`` when there is none, so the shim fails with a message
    instead of recursing into itself or into another copy of the shims.
    """
    shim = os.path.realpath(SHIMS_DIR)
    skipped = []
    for entry in (environ.get("PATH") or "").split(os.pathsep):
        if not entry:
            continue
        if os.path.realpath(entry) == shim or is_shim_dir(entry):
            skipped.append(entry)
            continue
        candidate = os.path.join(entry, name)
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if os.path.realpath(candidate) == os.path.join(shim, name):
            continue
        return candidate
    where = f"the shim folders ({', '.join(skipped)})" if skipped else shim
    raise ShimError(f"agentq shim: no real `{name}` on PATH outside {where}; put it on PATH or set AGENTQ_SHIMS=0")


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


def pytest_argv(name: str, args):
    """The argv after the pytest token of a pytest invocation, or None when the invocation is not pytest.

    Covers ``pytest``, ``python[3] -m pytest``, ``uv run [options] pytest`` and
    ``uv run [options] python[3] -m pytest``.
    """
    if name == "pytest":
        return list(args)
    if name in PYTHON_NAMES:
        return list(args[2:]) if list(args[:2]) == ["-m", "pytest"] else None
    if name == "uv" and args and args[0] == "run":
        rest = list(args[1:])
        index = 0
        while index < len(rest) and rest[index].startswith("-"):
            index += 1
        head = rest[index:]
        if head[:1] == ["pytest"]:
            return head[1:]
        if head[:1] in (["python"], ["python3"]) and head[1:3] == ["-m", "pytest"]:
            return head[3:]
    return None


def is_pytest(name: str, args) -> bool:
    """True for the pytest invocations this shim gates."""
    return pytest_argv(name, args) is not None


def _positionals(args, value_opts) -> list[str]:
    """The positional words of an argv: options and the values of the options in ``value_opts`` are skipped.

    Everything after a ``--`` is positional.
    """
    found = []
    rest = list(args)
    index = 0
    while index < len(rest):
        arg = rest[index]
        index += 1
        if arg == "--":
            found.extend(rest[index:])
            break
        if arg.startswith("-") and arg != "-":
            if arg in value_opts:
                index += 1
            continue
        found.append(arg)
    return found


def pytest_reason(argv):
    """Why a pytest argv is refused, or None when every target is a test file or ``file::node`` id."""
    targets = _positionals(argv, PYTEST_VALUE_OPTS)
    if not targets:
        return "pytest has no test target and would collect the whole repo"
    files = {target.split("::", 1)[0] for target in targets}
    for path in sorted(files):
        if path.endswith("/") or os.path.isdir(path):
            return f"pytest target {path} is a directory, not a test file"
    if len(files) > PYTEST_FILE_LIMIT:
        return f"pytest names {len(files)} test files (limit {PYTEST_FILE_LIMIT})"
    return None


def gate_reason(args):
    """Why a python or uv argv is refused as a full gate or a whole-repo check, or None."""
    words = list(args)
    for index, word in enumerate(words):
        base = os.path.basename(word)
        if base in FULL_GATE_SCRIPTS:
            return f"full gate {base} is not a worker check"
        if base == "select_pytests.py" and "--all" in words:
            return "select_pytests.py --all selects the whole suite"
        if word == "pre-commit" and ("--all-files" in words or "-a" in words):
            return "pre-commit over all files is a whole-repo check"
        if base == "pyright" and not _positionals(words[index + 1:], PYRIGHT_VALUE_OPTS):
            return "pyright with no file arguments checks the whole repo"
    return None


def sleep_seconds(args):
    """Total seconds of a plain sleep argv (``5``, ``2m``, ``1.5h``), or None for anything else."""
    if not args:
        return None
    total = 0.0
    for arg in args:
        match = SLEEP_ARG.fullmatch(arg)
        if match is None:
            return None
        total += float(match.group(1)) * SLEEP_UNIT_S[match.group(2)]
    return total


def _seconds_text(seconds: float) -> str:
    return str(int(seconds)) if seconds == int(seconds) else f"{seconds:.1f}"


def job_label(environ) -> str:
    """The job id a sleep budget is kept under (``AGENTQ_JOB``), or an empty string when unset."""
    return (environ.get(JOB_ENV) or "").strip()


def sleep_budget_s(environ) -> float:
    """The per-job sleep budget in seconds: ``AGENTQ_SLEEP_BUDGET_S``, else 1800."""
    raw = (environ.get(SLEEP_BUDGET_ENV) or "").strip()
    try:
        return float(raw) if raw else float(SLEEP_BUDGET_DEFAULT_S)
    except ValueError:
        return float(SLEEP_BUDGET_DEFAULT_S)


def sleep_state_path(home, job: str) -> Path:
    """The file holding a job's running sleep total in whole seconds."""
    return Path(home) / SLEEP_STATE_DIR / f"sleep-{job}"


def sleep_used_s(home, job: str) -> int:
    """The job's sleep total so far in whole seconds; 0 when there is no counter yet."""
    try:
        return int(sleep_state_path(home, job).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def sleep_budget_refusal(home, environ, seconds: float):
    """``(reason, message)`` when this sleep would push the job past its budget, else None."""
    job = job_label(environ)
    if not job:
        return None
    budget = sleep_budget_s(environ)
    total = sleep_used_s(home, job) + math.ceil(seconds)
    if total <= budget:
        return None
    reason = f"sleep budget used ({_seconds_text(total)} s of {_seconds_text(budget)} s)"
    return reason, (f"BLOCKED: agentq guard: {reason}; workers never babysit. Launch long work detached, "
                    "write a status file, and report its PID and paths.")


def record_sleep(home, environ, args) -> None:
    """Add a sleep's seconds (rounded up) to the job's budget counter. Never raises."""
    job = job_label(environ)
    seconds = sleep_seconds(args)
    if not job or seconds is None or (environ.get(GUARD_ENV) or "").strip() == "0":
        return
    try:
        path = sleep_state_path(home, job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(sleep_used_s(home, job) + math.ceil(seconds)) + "\n", encoding="utf-8")
    except OSError:
        return


def pause_refused_sleep(args, environ) -> None:
    """Sleep up to REFUSED_SLEEP_PAUSE_S (``AGENTQ_REFUSED_SLEEP_PAUSE_S`` overrides) before a refused
    sleep returns, so a polling loop that ignores the refusal keeps its pace instead of spinning."""
    seconds = sleep_seconds(args)
    try:
        cap = float(environ.get("AGENTQ_REFUSED_SLEEP_PAUSE_S") or REFUSED_SLEEP_PAUSE_S)
    except ValueError:
        cap = float(REFUSED_SLEEP_PAUSE_S)
    if seconds and cap > 0:
        time.sleep(min(seconds, cap))


def refusal(name: str, args, environ):
    """``(reason, message)`` when the guard refuses this shim run, else None.

    The message is the stderr line a worker sees; the reason is what ``guard_blocks.tsv`` records.
    ``AGENTQ_GUARD=0`` turns every refusal off.
    """
    if (environ.get(GUARD_ENV) or "").strip() == "0":
        return None
    if name == "sleep":
        seconds = sleep_seconds(args)
        if seconds is None:
            return None
        if seconds > SLEEP_LIMIT_S:
            reason = f"sleep {_seconds_text(seconds)} s refused"
            return reason, (f"BLOCKED: agentq guard: {reason}; workers never babysit. Launch long work detached, "
                            "write a status file, and return its PID and paths in your report.")
        return sleep_budget_refusal(db.home_dir(environ), environ, seconds)
    argv = pytest_argv(name, args)
    if argv is not None:
        reason = pytest_reason(argv)
    elif name in PYTHON_NAMES or name == "uv":
        reason = gate_reason(args)
    else:
        reason = None
    if reason is None:
        return None
    return reason, f"BLOCKED: agentq guard: {reason}. {PYTEST_HINT}"


def log_block(home, environ, reason: str, name: str, args) -> None:
    """Append one refusal to ``guard_blocks.tsv``: epoch, run label, reason, command. Never raises."""
    try:
        command = shlex.join([name, *args])[:GUARD_COMMAND_CHARS]
        fields = [str(int(time.time())), run_label(environ), reason, command]
        line = "\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields)
        path = Path(home) / GUARD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, ValueError):
        return


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
    held = {kind for kind in (environ.get(HELD_ENV) or "").split(",") if kind}
    if (environ.get(PYTEST_HELD_ENV) or "").strip():
        held.add("pytest")
    return held


def _child_env(environ, held: set[str], kind: str | None, capped: bool) -> dict:
    env = dict(environ)
    held = held | ({kind} if kind else set())
    env[HELD_ENV] = ",".join(sorted(held))
    if "pytest" in held:
        env[PYTEST_HELD_ENV] = "1"
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
        admit = slots.pytest_admission(limits, slots.Gauges(home, settings=limits.gauge_settings))
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
    blocked = refusal(name, args, environ)
    if blocked is not None:
        reason, message = blocked
        print(message, file=sys.stderr)
        log_block(home, environ, reason, name, args)
        if name == "sleep":
            pause_refused_sleep(args, environ)
        return 2
    if name == "sleep":
        record_sleep(home, environ, args)
    command = [real, *args]
    slots.write_ssh_hosts(home)
    if name == "ssh" and ssh_gated(args, slots.gated_hosts(home)):
        return run_gated("ssh", command, environ, home)
    if is_pytest(name, args):
        return run_gated("pytest", command, environ, home)
    os.execve(real, [name, *args], dict(environ))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
