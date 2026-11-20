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
  key; when every target is a recorded pass it prints one line and returns 0.
- ``pytest_sessionfinish``: record the key, but only on exit 0 with at least one passed
  test. A failure, an error, a skip-only run and a ``-x`` early exit write nothing.

The key covers, all of it (a miss is cheap, a false hit is a lie):

- each target test file's content sha;
- every repo module in its transitive import closure (content sha), walked with a
  stdlib AST import walk; the walk's per-file import lists and the resulting closure are
  cached under the agentq home, keyed by content sha, so a repeat re-hashes the closure
  and walks only what changed;
- every ``conftest.py`` from each target's directory up to the repo root;
- ``uv.lock``, every ``pyproject.toml``, ``pytest.ini``, ``setup.cfg`` and ``tox.ini``
  (dependency and pytest-config changes);
- the whole pytest argv (``-k``, ``-x``, ``--basetemp``, ``-p no:randomly``, ``--dist``);
- the environment the suites read: ``PYTHONPATH``, ``LOR_DBT_MANIFEST``, ``DATA_ROOT``,
  ``LAKE_STORE``, ``PYTEST_ADDOPTS`` and every ``PG*`` and ``LOR_*`` variable
  (``LOR_PYTEST_CACHE`` itself is the control variable and stays out);
- the interpreter path (``.venv`` vs ``uv run --package <name>``);
- for a test file that reads the whole tree (``SCAN_ROOTS``, ``git ls-files``,
  ``rglob``, ``os.walk``, ``.glob(``), the whole tree sha: index blobs from
  ``git ls-files -s`` plus content shas for the dirty and untracked paths.

A run that cannot be keyed (no target, a missing target, an import that names a repo
module but does not resolve, a whole-tree test with no git tree sha) is not cached and
runs normally; the reason goes in the ``why`` field of the event row.

Active only inside a worker: ``AGENTQ_JOB`` or ``AGENTQ_RUN`` must be set, and
``LOR_PYTEST_CACHE=0`` turns it off. The gate and CI must set that variable explicitly
(an attestation has to be a real run), not rely on ``AGENTQ_JOB`` being unset.

``slots/events.jsonl`` gets one row per decision with ``cache=hit|miss`` (and ``why``
on a miss that could not be keyed), so the hit rate is visible in
``agentq slots --history``.

The plugin never fails a run. Any error logs one ``agentq pytest cache:`` line to
stderr and pytest runs normally. Stdlib only, Python 3.9+, and it imports nothing from
agentq: the home is ``$AGENTQ_HOME`` or ``~/.local/state/agentq``, and the event row is
appended here with the same shape ``slots.record_event`` writes.
"""

import ast
import hashlib
import json
import os
import shlex
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
IMPORTS_DIR = "imports"
CLOSURES_DIR = "closures"
IMPORTS_FILE = "store.json"
PRUNE_STAMP = "last-prune"
EVENTS_FILE = "events.jsonl"
EVENTS_MAX_BYTES = 8 * 1024 * 1024
GUARD_LOG = "guard_blocks.tsv"
GUARD_COMMAND_CHARS = 300
MAX_ENTRIES = 20000
MAX_AGE_S = 30 * 86400.0
PRUNE_EVERY_S = 3600.0
# A closure over this many files is not walked to the end: the key falls back to the whole
# tree sha, which is the same answer for a test that reaches everything and costs one
# `git ls-files` instead of a parse of the repo. The verdict is cached per root blob, so a
# repeat does not walk to the cap again.
MAX_CLOSURE_FILES = 300
MAX_IMPORT_ROWS = 20000
SAVE_EVERY = 500
MAX_CONFIG_FILES = 500
PYTEST_FILE_LIMIT = 25
PYTEST_HINT = "Run only the test files for the code you changed: pytest <file.py> [...]"
PYTEST_VALUE_OPTS = frozenset({
    "-k", "-m", "-p", "-n", "-c", "-o", "-W", "-r", "--maxfail", "--rootdir", "--junitxml", "--durations",
    "--ignore", "--ignore-glob", "--deselect", "--confcutdir", "--basetemp", "--log-level", "--timeout",
    "--tb", "--dist",
})
ENV_KEYS = ("PYTHONPATH", "LOR_DBT_MANIFEST", "DATA_ROOT", "LAKE_STORE", "PYTEST_ADDOPTS")
ENV_PREFIXES = ("PG", "LOR_")
ENV_SKIP = frozenset({CACHE_ENV})
TREE_MARKERS = ("SCAN_ROOTS", "ls-files", "rglob", "os.walk", ".glob(")
DYNAMIC_IMPORTS = frozenset({"import_module", "__import__", "importorskip"})
CONFIG_NAMES = ("uv.lock", "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
CONFIG_GLOBS = ("*pyproject.toml", "*uv.lock", "*pytest.ini", "*setup.cfg", "*tox.ini")
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".nox", "site-packages", "build", "dist",
})


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


def _rel(path, root):
    """``path`` relative to ``root`` as a posix string, or None when it is outside."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
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
    paths = [word for word in argv if not word.startswith("-") and ".py" in word]
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


def _record_event(environ, cache, argv, why=""):
    """Append one ``cache=hit|miss`` row to ``slots/events.jsonl``. Best effort."""
    row = {"ts": round(time.time(), 3), "kind": "pytest", "label": _label(environ),
           "pid": os.getpid(), "wait_ms": 0, "hold_ms": 0,
           "outcome": f"cache-{cache}", "argv": _argv_paths(argv), "cache": cache}
    if why:
        row["why"] = why
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


def _imports_store_path(environ):
    return _home(environ) / CACHE_DIR / IMPORTS_DIR / IMPORTS_FILE


def _closures_dir(environ):
    return _home(environ) / CACHE_DIR / CLOSURES_DIR


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


def _skipped_path(rel):
    """True for a path under a folder the key ignores (``.git``, ``.venv``, ``__pycache__``...).

    pytest writes ``.pytest_cache`` and ``__pycache__`` into the tree on every run, so a
    whole-tree sha that counted them would never repeat. Split on ``/`` rather than
    ``Path.parts``: this runs once per tracked file, and pathlib costs 0.15 ms a call.
    """
    return any(part in SKIP_DIRS for part in rel.split("/"))


def _tree_sha(root):
    """A content sha of the whole tree, or None when git cannot answer.

    ``git ls-files -s`` reads the index, not the files, so this stays cheap on a large
    repo; the dirty and untracked paths from ``git status --porcelain`` are hashed by
    content, so a modified file always changes the sha.
    """
    listing = _git(root, ["ls-files", "-s", "-z"])
    status = _git(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if listing is None or status is None:
        return None
    tracked = []
    for part in listing.split(b"\0"):
        if not part:
            continue
        meta, _, path = part.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) < 2 or not path:
            continue
        rel = path.decode("utf-8", "replace")
        if _skipped_path(rel):
            continue
        tracked.append((rel, fields[1].decode("ascii", "replace")))
    dirty = []
    items = status.split(b"\0")
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        code = item[:2].decode("ascii", "replace")
        path = item[3:].decode("utf-8", "replace")
        if "R" in code or "C" in code:
            index += 1
        if _skipped_path(path):
            continue
        dirty.append((path, _blob(Path(root) / path) or "deleted"))
    tracked.sort()
    dirty.sort()
    return _sha(json.dumps({"tracked": tracked, "dirty": dirty}, sort_keys=True).encode("utf-8"))


def _import_targets(source):
    """``(level, dotted, required)`` for every module a source file may import.

    ``level`` 0 is an absolute import. ``required`` is False for the ``from a.b import c``
    alias candidates, since ``c`` is usually an attribute and not a module. The dynamic
    forms a test file uses with a literal name (``importlib.import_module``, ``__import__``,
    ``pytest.importorskip``) are followed too, since a missed import is a false hit.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((0, alias.name, True))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                found.append((node.level, base, True))
            for alias in node.names:
                if alias.name == "*":
                    continue
                found.append((node.level, f"{base}.{alias.name}" if base else alias.name, False))
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in DYNAMIC_IMPORTS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((0, first.value, True))
    return found


def _call_name(func):
    """The last name of a call target: ``importlib.import_module`` -> ``import_module``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _module_file(base, parts):
    """``base/parts.py`` or ``base/parts/__init__.py``, or None."""
    if not parts:
        return None
    target = Path(base).joinpath(*parts)
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _module_index(root):
    """``dotted name -> [paths]`` for every ``.py`` in the repo, by path suffix.

    A suffix map rather than a package walk, so a ``src`` layout, a
    ``packages/<name>/<name>`` layout and a namespace package all resolve without
    knowing where the import roots are. Two files with the same suffix both match,
    which only ever adds files to the closure. Directory entries only: no file is read.

    The last component is indexed too, so ``import helpers`` next to a test resolves
    without a stat per import root: a test-local helper is the common case, and the
    per-root fallback cost a stat and a ``resolve`` per import of every file walked.
    """
    index = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames
                             if name not in SKIP_DIRS and not name.startswith("."))
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            if not name.endswith(".py"):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            path = Path(dirpath) / name
            index.setdefault(".".join(parts), []).append(path)
            if len(parts) > 1:
                index.setdefault(parts[-1], []).append(path)
    return index


def _import_roots(root, files):
    """Where an absolute import may start: the repo root, its ``src`` folders, and each
    target's own directory chain.

    pytest puts a test file's rootdir-relative folder on ``sys.path``, so ``import
    helpers`` next to the test resolves there; a ``<package>/src`` layout is where a
    dotted package name starts.
    """
    roots = [Path(root)]
    candidates = [Path(root) / "src"]
    try:
        for entry in sorted(Path(root).iterdir()):
            if entry.is_dir() and entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                candidates.append(entry / "src")
    except OSError:
        pass
    for candidate in candidates:
        if candidate.is_dir():
            roots.append(candidate)
    for path in files:
        parent = Path(path).parent
        while True:
            if parent not in roots:
                roots.append(parent)
            if parent == Path(root) or parent.parent == parent:
                break
            parent = parent.parent
    return roots


def _repo_names(roots):
    """The top-level module names the repo provides, so an import of one must resolve."""
    names = set()
    for root in roots:
        try:
            for entry in Path(root).iterdir():
                if entry.name in SKIP_DIRS or entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    names.add(entry.name)
                elif entry.suffix == ".py":
                    names.add(entry.name[:-3])
        except OSError:
            continue
    return names


def _resolve(path, level, dotted, index, roots, repo_names):
    """The files a ``(level, dotted)`` import names: a list, empty when it is not in the repo.

    The index answers first, so a stdlib or third-party import (``json``, ``typing``) costs
    one dict lookup and no stat: the per-root fallback is only for a name the repo provides
    and the index missed, which is rare and would otherwise be a stat storm per import.
    """
    parts = [part for part in dotted.split(".") if part]
    if not parts:
        return []
    if level:
        base = Path(path).parent
        for _ in range(level - 1):
            base = base.parent
        found = _module_file(base, parts)
        return [found] if found else []
    found = list(index.get(".".join(parts), []))
    if not found and len(parts) > 1:
        found = list(index.get(parts[-1], []))
    if found or parts[0] not in repo_names:
        return found
    for root in roots:
        candidate = _module_file(root, parts)
        if candidate is not None and candidate not in found:
            found.append(candidate)
    return found


class _ImportStore:
    """Per-file import lists, keyed by content sha, in one JSON file under the agentq home.

    One file rather than one per source file: a closure of a thousand files would otherwise
    cost a thousand small reads and writes on every cold walk. Content-keyed, so the same
    file in another worktree reuses the entry, and a lost update is only a re-parse.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.rows = {}
        self.dirty = False
        self.added = 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.rows = data
        except (OSError, ValueError):
            pass

    def imports(self, rel, blob, data):
        """The import list of one file, parsed once per content sha."""
        row = self.rows.get(rel)
        if isinstance(row, dict) and row.get("blob") == blob and isinstance(row.get("imports"), list):
            try:
                return [(int(item[0]), str(item[1]), bool(item[2])) for item in row["imports"]]
            except (TypeError, ValueError, IndexError):
                pass
        targets = _import_targets(data)
        if targets is None:
            return None
        self.rows[rel] = {"blob": blob, "imports": [list(item) for item in targets]}
        self.dirty = True
        self.added += 1
        if self.added >= SAVE_EVERY:
            self.save()
        return targets

    def save(self):
        if not self.dirty:
            return
        self.added = 0
        if len(self.rows) > MAX_IMPORT_ROWS:
            for key in list(self.rows)[:len(self.rows) - MAX_IMPORT_ROWS]:
                del self.rows[key]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.rows, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def _walk(seeds, root, context, store, known, budget):
    """Files reachable from ``seeds``, not descending into ``known`` (already in the closure).

    Returns ``(files, blobs, unresolved)``: the blobs are the shas of the bytes already read
    here, so the caller does not read and hash every file a second time.
    """
    index, roots, repo_names = context()
    files = set()
    blobs = {}
    unresolved = set()
    queue = list(seeds)
    while queue:
        path = queue.pop()
        if path in files:
            continue
        files.add(path)
        if len(files) + len(known) > budget[0]:
            unresolved.add("<closure too large>")
            break
        rel = _rel(path, root)
        try:
            data = Path(path).read_bytes()
        except OSError:
            unresolved.add(f"<unreadable {rel or path}>")
            continue
        blob = _sha(data)
        blobs[rel or str(path)] = blob
        targets = store.imports(rel or str(path), blob, data)
        if targets is None:
            unresolved.add(f"<unparsable {rel or path}>")
            continue
        for level, dotted, required in targets:
            found = _resolve(path, level, dotted, index, roots, repo_names)
            if not found:
                if required and (level or dotted.split(".")[0] in repo_names):
                    unresolved.add(dotted)
                continue
            for item in found:
                item_rel = _rel(item, root)
                if item_rel is not None and item_rel in known:
                    continue
                queue.append(item)
    return files, blobs, unresolved


def _read_closure(path, root_blob):
    """The cached ``{rel: blob}`` closure for this root content, or None.

    A closure that was too large to walk is cached as ``{"too_large": True}``, so the next
    run goes straight to the whole-tree key instead of walking to the cap again.
    """
    try:
        row = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or row.get("root_blob") != root_blob:
        return None
    if row.get("too_large"):
        return {}
    files = row.get("files")
    if not isinstance(files, dict):
        return None
    return {str(key): str(value) for key, value in files.items()}


def _write_closure(path, root_blob, blobs, too_large=False):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        row = {"root_blob": root_blob, "too_large": True} if too_large else {
            "root_blob": root_blob, "files": blobs}
        tmp = Path(path).with_name(f"{Path(path).name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(row, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _closure(test_path, root, context, store, closures_dir, budget):
    """``(blobs, unresolved, too_large)``: every repo file the test file imports, transitively.

    A closure is cached per (root file, its content sha) with every member's blob, and
    validated by re-hashing the members: a member whose blob still matches has the same
    imports, so the cached closure is still the one a walk would produce. A changed member
    is walked again, and the walk stops at members that are still unchanged.

    A closure over ``MAX_CLOSURE_FILES`` is not walked to the end: the caller keys on the
    whole tree instead, which is the same answer for a test that reaches everything, and
    costs one ``git ls-files`` rather than a parse of the repo.
    """
    rel = _rel(test_path, root) or str(test_path)
    root_blob = _blob(test_path)
    if root_blob is None:
        return None, {f"<unreadable {rel}>"}, False
    cache_file = Path(closures_dir) / f"{_sha(f'{rel}\0{root_blob}'.encode('utf-8'))[:32]}.json"
    blobs = {}
    seeds = [Path(test_path)]
    stored = _read_closure(cache_file, root_blob)
    if stored is not None:
        if not stored:
            return None, set(), True
        changed = []
        for item_rel, item_blob in stored.items():
            blob = _blob(Path(root) / item_rel)
            if blob is None or blob != item_blob:
                changed.append(Path(root) / item_rel)
            else:
                blobs[item_rel] = blob
        if not changed:
            return blobs, set(), False
        seeds = changed
    found, walked, unresolved = _walk(seeds, root, context, store, blobs, budget)
    if "<closure too large>" in unresolved:
        _write_closure(cache_file, root_blob, {}, too_large=True)
        return None, set(), True
    for path in found:
        item_rel = _rel(path, root)
        if item_rel is None:
            unresolved.add(str(path))
            continue
        if item_rel in blobs:
            continue
        blob = walked.get(item_rel) or _blob(path)
        if blob is None:
            unresolved.add(f"<unreadable {item_rel}>")
            continue
        blobs[item_rel] = blob
    if not unresolved:
        _write_closure(cache_file, root_blob, blobs)
    return blobs, unresolved, False


def _conftest_chain(files, root):
    """Every ``conftest.py`` from each target's directory up to the repo root."""
    found = {}
    for path in files:
        parent = Path(path).parent
        while True:
            candidate = parent / "conftest.py"
            if candidate.is_file():
                rel = _rel(candidate, root)
                if rel:
                    found[rel] = _blob(candidate)
            if parent == Path(root) or parent.parent == parent:
                break
            parent = parent.parent
    return found


def _config_files(root):
    """``uv.lock``, every ``pyproject.toml`` and the other pytest config files, or None."""
    found = {}
    for name in CONFIG_NAMES:
        candidate = Path(root) / name
        if candidate.is_file():
            found[name] = _blob(candidate)
    listing = _git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                          *CONFIG_GLOBS])
    if listing is not None:
        rels = [part.decode("utf-8", "replace") for part in listing.split(b"\0") if part]
    else:
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames
                                 if name not in SKIP_DIRS and not name.startswith("."))
            for name in filenames:
                if name in CONFIG_NAMES:
                    rel = _rel(Path(dirpath) / name, root)
                    if rel:
                        rels.append(rel)
    for rel in sorted(set(rels)):
        if rel in found or _skipped_path(rel):
            continue
        if len(found) > MAX_CONFIG_FILES:
            return None
        found[rel] = _blob(Path(root) / rel)
    return found


def _has_tree_marker(path):
    """True when a test file reads the whole tree, so every tracked file is an input."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(marker in text for marker in TREE_MARKERS)


def _env_material(environ):
    """The environment values the suites read, sorted, with the control variable left out."""
    picked = {}
    for key in sorted(environ):
        if key in ENV_SKIP:
            continue
        if key in ENV_KEYS or key.startswith(ENV_PREFIXES):
            picked[key] = environ[key]
    return picked


def _interpreter():
    try:
        return os.path.realpath(sys.executable)
    except OSError:
        return sys.executable


def _key_material(config, environ):
    """``(key, why)``: the cache key for this run, or ``(None, reason)`` when it cannot be keyed."""
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
    root = _repo_root(cwd)
    if root is None:
        return None, "no repo root"
    rels = {}
    for path in files:
        rel = _rel(path, root)
        if rel is None:
            return None, f"target outside the repo ({path})"
        rels[rel] = path
    context_cache = {}

    def context():
        """The import index, built only when a closure has to be walked."""
        if not context_cache:
            roots = _import_roots(root, files)
            context_cache["value"] = (_module_index(root), roots, _repo_names(roots))
        return context_cache["value"]

    store = _ImportStore(_imports_store_path(environ))
    closures_dir = _closures_dir(environ)
    budget = [MAX_CLOSURE_FILES]
    closure = {}
    unresolved = set()
    too_large = False
    for path in files:
        found, missing, large = _closure(path, root, context, store, closures_dir, budget)
        unresolved |= missing
        too_large = too_large or large
        if found:
            closure.update(found)
    store.save()
    if unresolved:
        return None, "unresolved import " + ", ".join(sorted(unresolved)[:3])
    conftest = _conftest_chain(files, root)
    configs = _config_files(root)
    if configs is None:
        return None, "too many config files"
    tree = None
    if too_large or any(_has_tree_marker(path) for path in files):
        tree = _tree_sha(root)
        if tree is None:
            return None, "whole-tree test but no tree sha"
    material = {
        "v": 1,
        "root": str(root),
        "interpreter": _interpreter(),
        "argv": argv,
        "env": _env_material(environ),
        "targets": {rel: _blob(path) for rel, path in rels.items()},
        "closure": {} if too_large else closure,
        "conftest": conftest,
        "config": configs,
        "tree": tree,
    }
    for rel, blob in material["targets"].items():
        if blob is None:
            return None, f"unreadable target {rel}"
    for rel, blob in closure.items():
        if blob is None:
            return None, f"unreadable import {rel}"
    return _sha(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")), ""


def _hit_message(argv):
    """The one line a worker sees on a hit, naming the escape hatch."""
    files = []
    for target in _positionals(argv, PYTEST_VALUE_OPTS):
        name = target.split("::", 1)[0]
        if name not in files:
            files.append(name)
    return (f"{len(files)} file(s) unchanged since a recorded pass on this tree, skipped "
            f"({', '.join(files)}); re-run with {CACHE_ENV}=0, or change a file they import")


def _touch(path):
    try:
        os.utime(path, None)
    except OSError:
        pass


def _prune(home):
    """Drop entries and closures older than ``MAX_AGE_S``, and the oldest above the cap."""
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
    for folder, cap in ((ENTRIES_DIR, MAX_ENTRIES), (CLOSURES_DIR, MAX_ENTRIES)):
        rows = []
        for shard in (base / folder).glob("*"):
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
        if len(rows) > cap:
            for _, entry in sorted(rows)[:len(rows) - cap]:
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
    """Refuse a whole-repo pytest, then skip the run when every target is a recorded pass."""
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
        key, why = _key_material(config, environ)
        if key is None:
            _record_event(environ, "miss", argv, why)
            return None
        entry = _entry_path(environ, key)
        if entry.is_file():
            _touch(entry)
            _record_event(environ, "hit", argv)
            _note(_hit_message(argv))
            return 0
        config._agentq_pytest_cache_key = key
        _record_event(environ, "miss", argv)
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
