"""pytest plugin: skip a test file whose inputs are unchanged since a recorded pass.

A pi worker re-runs the same one-file pytest command many times per job (measured:
3012 invocations over 244 jobs, 12.3 per job, median one file; 85 % of jobs re-ran an
identical file set, and 85 % of those repeats had no edit at all in between). This
plugin turns such a repeat into a one-line message and exit 0.

It is loaded with ``-p agentq_pytest_cache`` and this folder must be on ``PYTHONPATH``;
``launch_pi.py`` sets both for pi workers, next to ``agentq_pytest_slot``. That is the
only enforcement point that also covers the 31 % of invocations that run a venv
interpreter by absolute path and so bypass the PATH shims.

Hooks:

- ``pytest_cmdline_main``: before collection, so a hit costs no import and no
  collection (the expensive part of the top files is a module-level import). It first
  applies the shim's ``pytest_reason`` refusals (a bare pytest, a directory target,
  more than ``PYTEST_FILE_LIMIT`` files) with the shim's message and exit 2, so the
  absolute-path invocations the shim never sees get the same guard. Then it builds the
  key; when the key is a recorded pass it prints one line and returns 0.
- ``pytest_sessionfinish``: record the key, but only on exit 0 with at least one passed
  test. A failure, an error, a skip-only run and a ``-x`` early exit write nothing.

The key is the whole tree, not an import closure. An import walk cannot see a data file,
a script a test runs through ``subprocess``, a fixture directory, a directory listing or
a plugin named by a string, so it produced false hits on 52 % of one real suite. The
whole tree has no such blind spot and costs one ``git ls-files`` plus one ``git status``:

- **Tree content.** ``git ls-files -s`` (index blob shas) plus the content sha of every
  path ``git status --porcelain=v1 -z --untracked-files=all`` reports as modified,
  deleted or untracked, so any edit anywhere in the repo, committed or not, changes the
  key. A submodule is keyed by its own ``HEAD`` and ``status``. A tracked file is always
  keyed; an untracked path under a tool cache (``.pytest_cache/``, ``__pycache__/``,
  ``.venv/``, ``node_modules/``, ``.mypy_cache/``, ``.ruff_cache/``) is not, because
  pytest writes one on every run and the key would never repeat.
- **Ignored inputs.** Size and mtime of every gitignored file under the repo, because a
  generated input (a dbt ``target/manifest.json``, a ``.env``, a built parquet) is
  gitignored and is still an input. The walk skips ``.venv/``, ``__pycache__/``,
  ``.pytest_cache/``, ``.mypy_cache/``, ``.ruff_cache/``, ``node_modules/``, ``.git/``,
  the cache's own dir, and a nested checkout (a directory with a ``.git`` entry: another
  checkout of this or another repo, not an input of a run here, and walking the ones
  under a ``.worktrees/`` folder cost 26 s on a 14k-file repo). If the walk exceeds
  ``IGNORED_MAX_FILES`` files or ``IGNORED_MAX_S`` seconds the run is not cached.
- **argv**, the whole environment minus ``ENV_SKIP`` (the volatile variables that cannot
  change what a test does: ``AGENTQ_*`` run ids, ``TERM``, ``SHLVL``, ``PWD``,
  ``OLDPWD``, ``_``, the terminal and session ids, ``COLUMNS``/``LINES``,
  ``PYTEST_CURRENT_TEST``, ``PYTEST_XDIST_*``, the per-worker ``AGENT_SCRATCH`` and
  ``PI_SESSION_*``, and ``LOR_PYTEST_CACHE`` itself), the interpreter realpath and its
  ``sys.version``, a digest of the installed ``*.dist-info`` names (two venvs that drifted
  apart never share a key), and the cwd relative to the checkout.
- Every ``conftest.py`` from each target's directory up to the filesystem root:
  pytest loads a conftest above the git toplevel too, and that file is not in the tree
  sha. The content of a ``-c``/``--config-file`` file is keyed the same way.

**Shared across checkouts (v3).** Paths inside the checkout are keyed relative to it, the
checkout path inside an argv or environment value is written ``<root>``, and a
``--basetemp`` value is dropped. Two worktrees with the same content (two jobs fresh from
``origin/main``, or a clean baseline checkout) share one recorded pass. A conftest or config
file outside the checkout keeps its absolute path.

**One run per key.** A keyed miss claims ``pytest-cache/inflight/<key>.pid``. A second run
with the same key while that pid is alive waits (no slot held: the slot plugin takes its slot
later, in ``pytest_configure``) and becomes a hit when the first records its pass; if the first
fails or dies, the waiter runs. The claim goes in ``pytest_unconfigure``.

**Why it missed.** A keyed miss row carries ``changed``: the inputs that moved since the same
label last ran the same argv in the same folder (``tree``, ``ignored``, ``installed``,
``env:<NAME>``, ...), ``first`` for a first run, or ``same`` when the key repeated but the
earlier run did not pass.

A run that cannot be keyed is not cached and runs normally; the reason goes in the
``why`` field of the event row. That covers no target, a missing or directory target, a
state-dependent selector (``--lf``, ``--ff``, ``--nf``, ``--sw``: the target set comes
from ``.pytest_cache`` state, not from the argv, so a recorded pass could cover a subset
of what the same key later means), a random test order (``pytest-randomly`` with no
``--randomly-seed``), no git tree sha, and an ignored-input walk over the bound.

Active only inside a worker: ``AGENTQ_JOB`` or ``AGENTQ_RUN`` must be set, and
``LOR_PYTEST_CACHE=0`` turns it off. A gate or CI run that has to be a real run must set
that variable explicitly rather than rely on ``AGENTQ_JOB`` being unset; this repo's own
gate runs ``python3 -m unittest``, which never loads this plugin.

``slots/events.jsonl`` gets one row per decision with ``cache=hit|miss`` (and ``why``
on a miss that could not be keyed), so the hit rate is visible in
``agentq slots --history``.

The plugin never fails a run. Any error logs one ``agentq pytest cache:`` line to
stderr and pytest runs normally. Stdlib only, Python 3.9+, and it imports nothing from
agentq: the home is ``$AGENTQ_HOME`` or ``~/.local/state/agentq``, and the event row is
appended here with the same shape ``slots.record_event`` writes.
"""

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

CACHE_ENV = "LOR_PYTEST_CACHE"
RUN_ENV = "AGENTQ_RUN"
JOB_ENV = "AGENTQ_JOB"
HOME_ENV = "AGENTQ_HOME"
GUARD_ENV = "AGENTQ_GUARD"
DEFAULT_HOME = "~/.local/state/agentq"
CACHE_DIR = "pytest-cache"
ENTRIES_DIR = "entries"
INFLIGHT_DIR = "inflight"
LAST_DIR = "last"
PRUNE_STAMP = "last-prune"
EVENTS_FILE = "events.jsonl"
EVENTS_MAX_BYTES = 8 * 1024 * 1024
GUARD_LOG = "guard_blocks.tsv"
GUARD_COMMAND_CHARS = 300
MAX_ENTRIES = 20000
MAX_AGE_S = 30 * 86400.0
PRUNE_EVERY_S = 3600.0
PYTEST_FILE_LIMIT = 25
SHARED_WAIT_MAX_S = 900.0
SHARED_POLL_S = 1.0
CHANGED_MAX = 8
BASETEMP_RE = re.compile(r"--basetemp(?:=|\s+)\S+")
PYTEST_HINT = "Run only the test files for the code you changed: pytest <file.py> [...]"
PYTEST_VALUE_OPTS = frozenset({
    "-k", "-m", "-p", "-n", "-c", "-o", "-W", "-r", "--maxfail", "--rootdir", "--junitxml", "--durations",
    "--ignore", "--ignore-glob", "--deselect", "--confcutdir", "--basetemp", "--log-level", "--timeout",
    "--tb", "--dist", "--config-file", "--randomly-seed",
})
# The environment is keyed whole, minus these: they cannot change what a test does. Every
# ``AGENTQ_*`` variable is a run id or a path the harness owns, and ``LOR_PYTEST_CACHE`` is
# this plugin's own control variable.
ENV_SKIP = frozenset({
    CACHE_ENV, "TERM", "SHLVL", "PWD", "OLDPWD", "_", "COLUMNS", "LINES",
    "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID", "ITERM_SESSION_ID",
    "TMUX", "TMUX_PANE", "STY", "WINDOW", "SSH_TTY", "SSH_CLIENT", "SSH_CONNECTION",
    "PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
    "AGENT_SCRATCH", "PI_SESSION_FILE", "PI_SESSION_ID",
})
ENV_SKIP_PREFIXES = ("AGENTQ_",)
# A selector whose target set comes from ``.pytest_cache`` state rather than from the argv.
STATE_SELECTORS = frozenset({
    "--lf", "--last-failed", "--ff", "--failed-first", "--nf", "--new-first",
    "--sw", "--stepwise", "--stepwise-skip",
})
# Directories the ignored-input walk does not descend into: tool caches that every run
# rewrites, and the cache's own dir. A nested checkout is skipped too (see the docstring).
IGNORED_SKIP_DIRS = frozenset({
    ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git",
})
IGNORED_MAX_FILES = 200_000
IGNORED_MAX_S = 3.0


def _note(message):
    """One line to stderr. The plugin's only output on an error path."""
    try:
        print(f"agentq pytest cache: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _home(environ):
    return Path(os.path.expanduser(environ.get(HOME_ENV) or DEFAULT_HOME))


def _label(environ):
    return (environ.get(RUN_ENV) or environ.get(JOB_ENV) or "-").strip()


def _active(environ):
    """True inside a worker with the cache not switched off."""
    if (environ.get(CACHE_ENV) or "").strip() == "0":
        return False
    return bool((environ.get(RUN_ENV) or "").strip() or (environ.get(JOB_ENV) or "").strip())


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _blob(path):
    """The content sha of a file, or None when it cannot be read."""
    try:
        return _sha(Path(path).read_bytes())
    except OSError:
        return None


def _pytest_args(config):
    params = getattr(config, "invocation_params", None)
    args = getattr(params, "args", None)
    return list(args) if args is not None else sys.argv[1:]


def _cwd(config):
    params = getattr(config, "invocation_params", None)
    directory = getattr(params, "dir", None)
    return Path(directory) if directory else Path.cwd()


def _positionals(args, value_opts):
    """The positional words of an argv: options and the values of ``value_opts`` are skipped.

    Everything after a ``--`` is positional. Same rule as ``agentq/shims/dispatch.py``.
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


def _pytest_reason(argv):
    """Why a pytest argv is refused, or None when every target is a test file or ``file::node`` id.

    The same rule and the same wording as ``agentq/shims/dispatch.py:pytest_reason``, so a
    worker that runs a venv interpreter by absolute path sees what the shim would have said.
    """
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


def _argv_paths(argv, limit=3, chars=120):
    """The test file paths of an argv, for the event row. No option values, no environment."""
    paths = [word for word in _positionals(argv, PYTEST_VALUE_OPTS) if ".py" in word]
    return " ".join(paths[:limit])[:chars]


def _log_block(environ, reason, argv):
    """Append one refusal to ``guard_blocks.tsv``, the file the shims write. Never raises."""
    try:
        home = _home(environ)
        command = shlex.join(["pytest", *argv])[:GUARD_COMMAND_CHARS]
        fields = [str(int(time.time())), _label(environ), reason, command]
        line = "\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields)
        path = home / GUARD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, ValueError):
        return


def _refuse(reason, argv, environ):
    """Print the shim's refusal line and record it, exactly as ``dispatch.refusal`` does."""
    try:
        print(f"BLOCKED: agentq guard: {reason}. {PYTEST_HINT}", file=sys.stderr, flush=True)
    except Exception:
        pass
    _log_block(environ, reason, argv)


def _rotate_events(path):
    """Move a full history file to ``events.jsonl.1``, the rule ``slots.record_event`` uses."""
    try:
        if path.stat().st_size < EVENTS_MAX_BYTES:
            return
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def _record_event(environ, cache, argv, why="", **extra):
    """Append one ``cache=hit|miss`` row to ``slots/events.jsonl``. Best effort."""
    row = {"ts": round(time.time(), 3), "kind": "pytest", "label": _label(environ),
           "pid": os.getpid(), "wait_ms": 0, "hold_ms": 0,
           "outcome": f"cache-{cache}", "argv": _argv_paths(argv), "cache": cache}
    if why:
        row["why"] = why
    row.update({name: value for name, value in extra.items() if value not in (None, "", [])})
    path = _home(environ) / "slots" / EVENTS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_events(path)
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _entry_path(environ, key):
    return _home(environ) / CACHE_DIR / ENTRIES_DIR / key[:2] / f"{key}.json"


def _git(root, args, timeout=20):
    """``git -C root args`` stdout, or None when git is missing, slow or unhappy."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _repo_root(start):
    """The git toplevel of ``start``, else ``start`` itself resolved."""
    out = _git(start, ["rev-parse", "--show-toplevel"])
    if out:
        text = out.decode("utf-8", "replace").strip()
        if text:
            try:
                return Path(text).resolve()
            except OSError:
                return None
    try:
        return Path(start).resolve()
    except OSError:
        return None


def _submodule_sha(path):
    """A submodule's own state: its ``HEAD`` and its ``status``, or None when unreadable.

    ``_blob`` on a submodule directory raises, which used to hash every dirty submodule as
    ``"deleted"`` and so hid a change inside it.
    """
    head = _git(path, ["rev-parse", "HEAD"], timeout=10)
    status = _git(path, ["status", "--porcelain", "-z", "--untracked-files=all"], timeout=10)
    if head is None or status is None:
        return None
    return "submodule:" + _sha(head.strip() + b"\0" + status)


def _content_sha(path):
    """The content sha of a worktree path: a file's bytes, a submodule's state, or a marker."""
    try:
        info = Path(path).lstat()
    except OSError:
        return "deleted"
    if stat.S_ISDIR(info.st_mode):
        return _submodule_sha(path) or "unreadable-submodule"
    return _blob(path) or "unreadable"


def _skipped_path(rel):
    """True for a path under a tool cache the key ignores (``.pytest_cache``, ``__pycache__``...).

    pytest writes ``.pytest_cache`` and ``__pycache__`` into the tree on every run, so an
    untracked path under one of them would change the key on every run and no repeat would
    ever hit. A *tracked* file is always keyed, whatever its path. Split on ``/`` rather than
    ``Path.parts``: this runs once per path, and pathlib costs 0.15 ms a call.
    """
    return any(part in IGNORED_SKIP_DIRS for part in rel.split("/"))


def _git_state(root):
    """``(tree_sha, ignored_paths)`` for the whole tree, or None when git cannot answer.

    ``git ls-files -s`` reads the index, not the files, so this stays cheap on a large repo;
    every path ``git status`` reports as modified, deleted or untracked is hashed by content,
    so an edit anywhere in the repo, committed or not, changes the sha. The ``!!`` paths are
    returned for the ignored-input walk rather than hashed here.
    """
    listing = _git(root, ["ls-files", "-s", "-z"])
    status = _git(root, ["status", "--porcelain", "-z", "--untracked-files=all",
                         "--ignored=matching"])
    if listing is None or status is None:
        return None
    digest = hashlib.sha256()
    tracked = []
    for part in listing.split(b"\0"):
        if not part:
            continue
        meta, _, path = part.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) < 2 or not path:
            continue
        tracked.append((path.decode("utf-8", "surrogateescape"),
                        fields[1].decode("ascii", "replace")))
    tracked.sort()
    for rel, blob in tracked:
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(blob.encode("ascii", "replace"))
        digest.update(b"\n")
    dirty = []
    ignored = []
    items = status.split(b"\0")
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        code = item[:2].decode("ascii", "replace")
        path = item[3:].decode("utf-8", "surrogateescape")
        if "R" in code or "C" in code:
            index += 1
        if code == "!!":
            ignored.append(path)
        elif not _skipped_path(path):
            dirty.append((path, _content_sha(Path(root) / path)))
    dirty.sort()
    for rel, blob in dirty:
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(blob.encode("utf-8", "surrogateescape"))
        digest.update(b"\n")
    return digest.hexdigest(), ignored


def _ignored_inputs(root, entries, cache_dir):
    """``[(rel, size, mtime_ns)]`` for every gitignored file, or None when the walk is too big.

    ``entries`` are the ``!!`` paths ``git status`` reported. A directory is walked unless it
    is one of ``IGNORED_SKIP_DIRS``, the cache's own dir, or a nested checkout (a directory
    with a ``.git`` entry). Size and mtime rather than a content sha: a generated input can be
    large, and a rewrite always moves the mtime.
    """
    started = time.monotonic()
    rows = []
    count = 0
    stack = [(rel, Path(root) / rel) for rel in entries]
    while stack:
        if time.monotonic() - started > IGNORED_MAX_S:
            return None
        rel, path = stack.pop()
        if _skipped_path(rel) or path == cache_dir:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                children = list(os.scandir(path))
            except OSError:
                continue
            if any(child.name == ".git" for child in children):
                continue
            for child in children:
                stack.append((f"{rel}/{child.name}", Path(child.path)))
            continue
        count += 1
        if count > IGNORED_MAX_FILES:
            return None
        try:
            info = path.stat()
        except OSError:
            pass
        rows.append((rel, info.st_size, info.st_mtime_ns))
    rows.sort()
    return rows


def _conftest_chain(files):
    """Every ``conftest.py`` from each target's directory up to the filesystem root.

    pytest loads a conftest above the git toplevel too (a ``pytest.ini`` in a parent folder
    sets ``rootdir`` there), and that file is not in the tree sha, so the walk does not stop
    at the repo root and the key is the absolute path.
    """
    found = {}
    for path in files:
        parent = Path(path).parent
        while True:
            candidate = parent / "conftest.py"
            try:
                if candidate.is_file():
                    found[str(candidate)] = _blob(candidate) or "unreadable"
            except OSError:
                pass
            if parent.parent == parent:
                break
            parent = parent.parent
    return found


def _config_arg(argv, cwd):
    """The content sha of every ``-c``/``--config-file`` file, keyed by absolute path.

    A config file inside the repo is already in the tree sha; one outside it is not, and its
    content decides what pytest collects.
    """
    found = {}
    for index, arg in enumerate(argv):
        value = None
        if arg in ("-c", "--config-file"):
            if index + 1 < len(argv):
                value = argv[index + 1]
        elif arg.startswith("--config-file="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-c") and len(arg) > 2:
            value = arg[2:]
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            path = path.resolve()
        except OSError:
            continue
        found[str(path)] = _blob(path) or "unreadable"
    return found


def _state_selector(argv):
    """The first selector in ``argv`` whose target set comes from ``.pytest_cache`` state."""
    for arg in argv:
        name = arg.split("=", 1)[0]
        if name in STATE_SELECTORS:
            return name
    return None


def _random_order(config, argv):
    """True when ``pytest-randomly`` will pick a seed this run, so the order is not reproducible."""
    for arg in argv:
        if arg.split("=", 1)[0] == "--randomly-seed":
            return False
    try:
        return bool(config.pluginmanager.hasplugin("randomly"))
    except Exception:
        return False


def _portable(text, roots=()):
    """``text`` with the checkout path written as ``<root>`` and any ``--basetemp`` value dropped.

    Two worktrees of one repo with the same content must key the same, so a path that only
    names where the checkout lives (``VIRTUAL_ENV``, a ``PATH`` entry, a ``--basetemp`` under a
    per-job folder) is not an input. ``roots`` holds the checkout path as git resolves it and
    as the cwd spells it (they differ under a symlinked folder such as macOS ``/var``).
    """
    text = BASETEMP_RE.sub("--basetemp=<tmp>", str(text))
    for root in roots:
        text = text.replace(root, "<root>")
    return text


def _root_spellings(root, cwd):
    """The checkout path as resolved and as the cwd spells it, longest first.

    macOS resolves ``/var`` and ``/tmp`` to ``/private/...``, while an environment value keeps
    the short form, so both are listed.
    """
    spellings = {str(root)}
    rel = _relative(cwd, root)
    if not os.path.isabs(rel):
        alias = os.path.abspath(str(cwd))
        for _ in ([] if rel == "." else rel.split("/")):
            alias = os.path.dirname(alias)
        spellings.add(alias)
    for spelling in list(spellings):
        if spelling.startswith("/private/"):
            spellings.add(spelling[len("/private"):])
    return sorted(spellings, key=len, reverse=True)


AGENTQ_PLUGIN_OPT_RE = re.compile(r"-p\s*agentq_pytest_\w+")


def _pytest_env_value(key, value):
    """``PYTEST_ADDOPTS`` / ``PYTEST_PLUGINS`` without the parts that cannot change a result.

    The agentq plugins themselves and a ``--basetemp`` value are dropped, so a worker that sets
    ``PYTEST_ADDOPTS="--basetemp=..."`` keys the same as one that passes nothing.
    """
    if key == "PYTEST_ADDOPTS":
        value = BASETEMP_RE.sub(" ", AGENTQ_PLUGIN_OPT_RE.sub(" ", value))
        return " ".join(value.split())
    if key == "PYTEST_PLUGINS":
        return ",".join(part for part in (piece.strip() for piece in value.split(","))
                        if part and not part.startswith("agentq_pytest_"))
    return value


def _env_material(environ, roots=()):
    """The whole environment, minus the volatile variables that cannot change a test."""
    picked = {}
    for key in sorted(environ):
        if key in ENV_SKIP or key.startswith(ENV_SKIP_PREFIXES):
            continue
        value = _pytest_env_value(key, environ[key])
        if key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS") and not value:
            continue
        picked[key] = _portable(value, roots)
    return picked


def _relative(path, root):
    """``path`` relative to ``root`` when it sits inside, else the absolute path."""
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return str(path)


def _installed():
    """A digest of the interpreter's installed distributions (``name-version.dist-info``).

    The interpreter path resolves to the shared base Python, so without this two worktrees
    whose venvs drifted apart (one synced to a newer ``uv.lock``) would share a key.
    """
    names = []
    for folder in sys.path:
        if not folder or not folder.endswith("site-packages"):
            continue
        try:
            names.extend(entry for entry in os.listdir(folder) if entry.endswith(".dist-info"))
        except OSError:
            continue
    return _sha("\n".join(sorted(names)).encode("utf-8"))


def _interpreter():
    try:
        return os.path.realpath(sys.executable)
    except OSError:
        return sys.executable


def _key_material(config, environ):
    """``(key, why)``: the cache key for this run, or ``(None, reason)`` when it cannot be keyed."""
    parts, why = _key_parts(config, environ)
    if parts is None:
        return None, why
    return _digest(parts), ""


def _digest(parts):
    return _sha(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _key_parts(config, environ):
    """``(parts, why)``: the keyed inputs of this run, or ``(None, reason)``.

    Paths inside the checkout are written relative to it, so a second worktree with the same
    content keys the same and reuses the pass. A ``conftest.py`` or ``-c`` file outside the
    checkout keeps its absolute path.
    """
    cwd = _cwd(config)
    argv = _pytest_args(config)
    targets = _positionals(argv, PYTEST_VALUE_OPTS)
    if not targets:
        return None, "no test target"
    names = []
    for target in targets:
        name = target.split("::", 1)[0]
        if name not in names:
            names.append(name)
    if len(names) > PYTEST_FILE_LIMIT:
        return None, f"{len(names)} test files (limit {PYTEST_FILE_LIMIT})"
    files = []
    for name in names:
        if name.endswith("/") or os.path.isdir(os.path.join(str(cwd), name)):
            return None, f"directory target {name}"
        path = Path(name)
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            path = path.resolve()
        except OSError:
            return None, f"unresolvable target {name}"
        if not path.is_file():
            return None, f"missing target {name}"
        files.append(path)
    selector = _state_selector(argv)
    if selector is not None:
        return None, f"state-dependent selector {selector}"
    if _random_order(config, argv):
        return None, "random test order"
    root = _repo_root(cwd)
    if root is None:
        return None, "no repo root"
    state = _git_state(root)
    if state is None:
        return None, "no tree sha"
    tree, ignored_entries = state
    ignored = _ignored_inputs(root, ignored_entries, _home(environ) / CACHE_DIR)
    if ignored is None:
        return None, "ignored inputs too large to key"
    roots = _root_spellings(root, cwd)
    material = {
        "v": 3,
        "cwd": _relative(cwd, root),
        "interpreter": _interpreter(),
        "python": sys.version,
        "installed": _installed(),
        "argv": [_portable(arg, roots) for arg in argv],
        "env": _env_material(environ, roots),
        "tree": tree,
        "ignored": ignored,
        "conftest": {_relative(path, root): blob for path, blob in _conftest_chain(files).items()},
        "config": {_relative(path, root): blob for path, blob in _config_arg(argv, cwd).items()},
    }
    return material, ""


def _hit_message(argv):
    """The one line a worker sees on a hit, naming the escape hatch."""
    files = []
    for target in _positionals(argv, PYTEST_VALUE_OPTS):
        name = target.split("::", 1)[0]
        if name not in files:
            files.append(name)
    return (f"{len(files)} file(s) unchanged since a recorded pass on this content, skipped "
            f"({', '.join(files)}); re-run with {CACHE_ENV}=0, or change a file in the tree")


def _touch(path):
    try:
        os.utime(path, None)
    except OSError:
        pass


def _prune(home):
    """Drop entries older than ``MAX_AGE_S``, and the oldest above the cap."""
    base = Path(home) / CACHE_DIR
    stamp = base / PRUNE_STAMP
    now = time.time()
    try:
        if now - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(now)), encoding="utf-8")
    except OSError:
        return
    rows = []
    for shard in (base / ENTRIES_DIR).glob("*"):
        try:
            for entry in shard.glob("*.json"):
                try:
                    rows.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
        except OSError:
            continue
    for mtime, entry in rows:
        if now - mtime > MAX_AGE_S:
            try:
                entry.unlink()
            except OSError:
                pass
    if len(rows) > MAX_ENTRIES:
        for _, entry in sorted(rows)[:len(rows) - MAX_ENTRIES]:
            try:
                entry.unlink()
            except OSError:
                pass


def _write_entry(environ, key, config):
    """Record a pass for ``key``. Atomic, so a killed run never leaves a half entry."""
    path = _entry_path(environ, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"key": key, "ts": round(time.time(), 3), "label": _label(environ),
           "argv": _argv_paths(_pytest_args(config))}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _prune(_home(environ))


def _passed_count(config):
    """How many tests passed, or None when the run cannot say (then nothing is recorded)."""
    try:
        reporter = config.pluginmanager.getplugin("terminalreporter")
    except Exception:
        return None
    stats = getattr(reporter, "stats", None)
    if not isinstance(stats, dict):
        return None
    return len(stats.get("passed") or [])


def pytest_cmdline_main(config):
    """Refuse a whole-repo pytest, then skip the run when the key is a recorded pass."""
    try:
        environ = os.environ
        if not _active(environ):
            return None
        argv = _pytest_args(config)
        if (environ.get(GUARD_ENV) or "").strip() != "0":
            reason = _pytest_reason(argv)
            if reason is not None:
                _refuse(reason, argv, environ)
                return 2
        parts, why = _key_parts(config, environ)
        if parts is None:
            _record_event(environ, "miss", argv, why)
            return None
        key = _digest(parts)
        entry = _entry_path(environ, key)
        if entry.is_file():
            _touch(entry)
            _record_event(environ, "hit", argv)
            _note(_hit_message(argv))
            return 0
        waited = _wait_for_twin(environ, key, entry)
        if waited is not None:
            _record_event(environ, "hit", argv, shared="twin", wait_ms=waited)
            _note(_hit_message(argv) + " (an identical run finished while this one waited)")
            return 0
        config._agentq_pytest_cache_key = key
        config._agentq_pytest_cache_lock = _claim(environ, key)
        _record_event(environ, "miss", argv, changed=_changed(environ, parts))
        return None
    except Exception as exc:
        _note(f"cache skipped ({exc})")
        return None


def pytest_sessionfinish(session, exitstatus):
    """Record the key, but only for a clean pass with at least one passed test."""
    try:
        config = session.config
        key = getattr(config, "_agentq_pytest_cache_key", None)
        if not key or exitstatus != 0:
            return
        if hasattr(config, "workerinput") or "PYTEST_XDIST_WORKER" in os.environ:
            return
        if not _passed_count(config):
            return
        _write_entry(os.environ, key, config)
    except Exception as exc:
        _note(f"cache not recorded ({exc})")


def pytest_unconfigure(config):
    """Drop this run's in-flight claim, so a waiting twin stops waiting."""
    lock = getattr(config, "_agentq_pytest_cache_lock", None)
    if lock is not None:
        try:
            lock.unlink()
        except OSError:
            pass


def _inflight_path(environ, key):
    return _home(environ) / CACHE_DIR / INFLIGHT_DIR / f"{key}.pid"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _claim_owner(path):
    """The live pid holding an in-flight claim, or None (no claim, unreadable, or dead owner)."""
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and pid != os.getpid() and _pid_alive(pid) else None


def _claim(environ, key):
    """Mark this run as the one computing ``key``. Returns the claim path, or None. Best effort."""
    path = _inflight_path(environ, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and _claim_owner(path) is None:
            path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return path


def _wait_for_twin(environ, key, entry, clock=time.monotonic, sleep=time.sleep):
    """Wait while a live run with the same key computes it. Returns ms waited on a hit, else None.

    A twin that fails, is killed or records nothing ends the wait without a hit, and this run
    goes ahead. The wait happens before ``pytest_configure``, so it holds no pytest slot.
    """
    path = _inflight_path(environ, key)
    if _claim_owner(path) is None:
        return None
    started = clock()
    _note("an identical run is in flight; waiting for its result instead of running twice")
    while clock() - started < SHARED_WAIT_MAX_S:
        if entry.is_file():
            _touch(entry)
            return int((clock() - started) * 1000)
        if _claim_owner(path) is None:
            return int((clock() - started) * 1000) if entry.is_file() else None
        sleep(SHARED_POLL_S)
    return None


def _changed(environ, parts):
    """Which keyed inputs moved since this label last ran this argv in this folder.

    Written to the miss event as ``changed`` (``tree``, ``ignored``, ``env:<NAME>``, ...), or
    ``["first"]`` for a first run, so the hit rate can be explained from the event log alone.
    Keeps one small digest file per (label, argv, cwd). Best effort: an error returns None.
    """
    try:
        slot = _sha(json.dumps([_label(environ), parts["argv"], parts["cwd"]]).encode("utf-8"))
        path = _home(environ) / CACHE_DIR / LAST_DIR / slot[:2] / f"{slot}.json"
        now = {name: _digest(value) for name, value in parts.items() if name != "env"}
        now["env"] = {name: _sha(value.encode("utf-8")) for name, value in parts["env"].items()}
        try:
            before = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(now, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        if not isinstance(before, dict):
            return ["first"]
        moved = [name for name in sorted(now) if name != "env" and before.get(name) != now[name]]
        old_env = before.get("env") if isinstance(before.get("env"), dict) else {}
        moved += [f"env:{name}" for name in sorted(set(old_env) | set(now["env"]))
                  if old_env.get(name) != now["env"].get(name)]
        return (moved or ["same"])[:CHANGED_MAX]
    except Exception:
        return None
