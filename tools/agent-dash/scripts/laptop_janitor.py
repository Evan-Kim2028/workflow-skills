#!/usr/bin/env python3
"""laptop_janitor.py - scheduled cleanup of agent leftovers on macOS.

Removes only what is provably unused: throwaway Postgres servers, old temp
entries, finished git worktrees and finished worker scratch folders. Every
guard that cannot be evaluated keeps the thing. Runs safely while other agents
are working; a held lock makes a second run exit at once.

Commands::

    laptop_janitor.py run [--apply] [--scheduled] [--only a,b] [--skip a,b] [--json]
    laptop_janitor.py status [--json]
    laptop_janitor.py install [--apply]
    laptop_janitor.py uninstall [--apply]
    laptop_janitor.py print-plist

Standard library only, Python 3.9 (/usr/bin/python3). See
docs/laptop-janitor.md for the sweepers, the guards and the undo.
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import math
import os
import plistlib
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

LABEL = "com.agent-dash.laptop-janitor"
SWEEPERS = ["postgres", "tmp", "worktrees", "scratch"]

DEFAULTS = {
    "interval_days": 3,
    "sweeper_budget_seconds": 1200,
    "pg_min_age_hours": 24,
    "tmp_min_age_days": 2,
    "session_min_age_days": 7,
    "tmp_age_scan_max_entries": 200000,
    "snapshot_max_age_seconds": 120,
    "worktree_min_idle_days": 3,
    "scratch_min_age_days": 3,
    "tmp_protect_globs": [],
    "postgres_extra_roots": [],
    "repos": [],
    "repo_roots": ["~/Documents/projects"],
    "worktree_protect_globs": ["~/.local/share/agent-dash/releases/*"],
    "worktree_manager_cmd": "workspace-manager",
    "agent_dash_cmd": "~/.claude/tools/agent-dash/agent_dash.py",
}

DEFAULT_TMP_PROTECT_GLOBS = [
    "com.*",
    ".*",
    "TemporaryItems",
    "tmux-*",
    "ssh-*",
    "*.sock",
    "launchd-*",
]

# Ignored entries that are cheap to rebuild; anything else ignored keeps the
# worktree.
ALLOWED_IGNORED = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
}

MERGE_STATE_FILES = [
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
]

POSTGRES_HELPERS = [
    "checkpointer",
    "background writer",
    "walwriter",
    "autovacuum launcher",
    "logical replication launcher",
    "archiver",
    "stats collector",
    "startup",
    "walsummarizer",
    "io worker",
]

LOG_KEEP = 30


class ConfigError(Exception):
    """The config file exists but cannot be used."""


# --- paths and config ------------------------------------------------------


def state_dir():
    return os.path.abspath(
        os.path.expanduser(
            os.environ.get("LAPTOP_JANITOR_STATE") or "~/.local/state/laptop-janitor"
        )
    )


def log_dir():
    return os.path.abspath(
        os.path.expanduser(
            os.environ.get("LAPTOP_JANITOR_LOGS") or "~/Library/Logs/laptop-janitor"
        )
    )


def config_path():
    return os.path.abspath(
        os.path.expanduser(
            os.environ.get("LAPTOP_JANITOR_CONFIG")
            or "~/.config/laptop-janitor/config.json"
        )
    )


def installed_script():
    return os.path.join(
        os.path.expanduser("~/.local/share/laptop-janitor"), "laptop_janitor.py"
    )


def launch_agents_dir():
    return os.path.expanduser("~/Library/LaunchAgents")


def plist_path():
    return os.path.join(launch_agents_dir(), LABEL + ".plist")


def _kind_ok(value, expected):
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if expected is str:
        return isinstance(value, str)
    if expected == "strlist":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return True


CONFIG_TYPES = {
    "interval_days": float,
    "sweeper_budget_seconds": float,
    "pg_min_age_hours": float,
    "tmp_min_age_days": float,
    "session_min_age_days": float,
    "tmp_age_scan_max_entries": int,
    "snapshot_max_age_seconds": float,
    "worktree_min_idle_days": float,
    "scratch_min_age_days": float,
    "tmp_protect_globs": "strlist",
    "postgres_extra_roots": "strlist",
    "repos": "strlist",
    "repo_roots": "strlist",
    "worktree_protect_globs": "strlist",
    "worktree_manager_cmd": str,
    "agent_dash_cmd": str,
}


def load_config(path=None):
    """Return DEFAULTS merged with the JSON config.

    A missing file means "all defaults". An unreadable file, bad JSON, a
    non-object, an unknown key or a wrong type raises ConfigError: the janitor
    never silently falls back to defaults for a config it could not read.
    """
    path = path or config_path()
    cfg = dict(DEFAULTS)
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("invalid JSON in %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s must hold a JSON object" % path)
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise ConfigError("%s has unknown key(s): %s" % (path, ", ".join(unknown)))
    for key, value in data.items():
        if not _kind_ok(value, CONFIG_TYPES[key]):
            raise ConfigError("%s: bad type for %r" % (path, key))
    cfg.update(data)
    return cfg


# --- small helpers ---------------------------------------------------------


def utc_stamp(ts=None):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts if ts is not None else time.time()))


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_etime(text):
    """Seconds from ps etime: [[dd-]hh:]mm:ss."""
    text = text.strip()
    days = 0
    if not text:
        return 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    while len(nums) < 3:
        nums.insert(0, 0)
    return days * 86400 + nums[-3] * 3600 + nums[-2] * 60 + nums[-1]


def parse_lsof_cwds(text):
    """Map pid -> cwd from ``lsof -a -d cwd -Fpn`` output."""
    out = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def parse_ps(text):
    """Map pid -> {ppid, etime, args} from ``ps -axo pid=,ppid=,etime=,args=``."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        out[pid] = {"ppid": ppid, "etime": parse_etime(parts[2]), "args": parts[3]}
    return out


PRIVATE_PREFIXES = ("/private/tmp", "/private/var", "/private/etc")
SHORT_PREFIXES = ("/tmp", "/var", "/etc")


def _short_long_forms(path):
    """The path plus its other /private-prefixed or short form."""
    out = [path]
    for long_prefix, short_prefix in zip(PRIVATE_PREFIXES, SHORT_PREFIXES):
        if path == long_prefix or path.startswith(long_prefix + os.sep):
            out.append(short_prefix + path[len(long_prefix):])
        if path == short_prefix or path.startswith(short_prefix + os.sep):
            out.append("/private" + path)
    return out


def path_forms(path):
    """Every form of ``path`` a process may use: as given, real, short, long."""
    forms = []
    for base in (path, os.path.realpath(path)):
        for form in _short_long_forms(base):
            if form and form not in forms:
                forms.append(form)
    return forms


class Snapshot:
    """One process snapshot for the whole run.

    The first lookup builds two cheap indexes: every ancestor directory of
    every process working directory (all path forms), and one joined string
    of all arguments. The common call (no ignored PIDs) is then a set lookup
    plus a substring test instead of a loop over every process.
    """

    def __init__(self, ok=True, procs=None, cwds=None, error="", own_pid=None):
        self.ok = ok
        self.procs = procs or {}
        self.cwds = cwds or {}
        self.error = error
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self._dirs = None
        self._args = ""

    def children(self, pid):
        return [p for p, proc in self.procs.items() if proc.get("ppid") == pid]

    def index(self):
        """(set of ancestor dirs of every cwd, joined arguments)."""
        if self._dirs is None:
            dirs = set()
            args = []
            for pid, proc in self.procs.items():
                if pid == self.own_pid:
                    continue
                args.append(proc.get("args") or "")
                cwd = self.cwds.get(pid)
                if not cwd:
                    continue
                for form in path_forms(cwd):
                    cur = form
                    while True:
                        dirs.add(cur)
                        parent = os.path.dirname(cur)
                        if parent == cur:
                            break
                        cur = parent
            self._dirs = dirs
            self._args = "\n".join(args)
        return self._dirs, self._args


def take_snapshot(timeout=60):
    """ps plus every process cwd. Any failure means "everything is in use"."""
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,args="],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Snapshot(ok=False, error="ps failed: %s" % exc)
    if ps.returncode != 0:
        return Snapshot(ok=False, error="ps exited %s: %s" % (ps.returncode, ps.stderr.strip()))
    try:
        lsof = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Snapshot(ok=False, error="lsof failed: %s" % exc)
    cwds = parse_lsof_cwds(lsof.stdout)
    # The exit code alone is not a signal: lsof can exit nonzero while still
    # listing processes. The janitor always has a working directory, so a
    # scan that does not list it did not work and must not look empty.
    if os.getpid() not in cwds:
        return Snapshot(
            ok=False,
            error="lsof listed no working directory for the janitor (exit %s)"
            % lsof.returncode,
        )
    return Snapshot(ok=True, procs=parse_ps(ps.stdout), cwds=cwds)


def path_in_use(snapshot, path, ignore_pids=None):
    """True when a live process works in the path or names it in its args.

    ``path`` may be a file or a directory. The janitor's own pid is skipped.
    Returns (in_use, reason).
    """
    if snapshot is None:
        return True, "no-snapshot"
    if not snapshot.ok:
        return True, "snapshot-failed"
    ignore = set(ignore_pids or ())
    own = os.getpid()
    ignore.add(own)
    forms = path_forms(path)
    if not (ignore - {own}):
        dirs, args_blob = snapshot.index()
        for form in forms:
            if form in dirs:
                return True, "cwd"
            if form in args_blob:
                return True, "args"
        return False, ""
    real = os.path.realpath(path)
    for pid, proc in snapshot.procs.items():
        if pid in ignore:
            continue
        cwd = snapshot.cwds.get(pid)
        if cwd:
            rcwd = os.path.realpath(cwd)
            if rcwd == real or rcwd.startswith(real + os.sep):
                return True, "cwd-of-%d" % pid
            for form in _short_long_forms(cwd) + _short_long_forms(rcwd):
                if form in forms:
                    return True, "cwd-of-%d" % pid
        args = proc.get("args") or ""
        for form in forms:
            if form in args:
                return True, "args-of-%d" % pid
    return False, ""


def _split_args(args):
    try:
        return shlex.split(args, posix=True)
    except ValueError:
        return args.split()


def _arg_value(argv, flag):
    for i, item in enumerate(argv):
        if item == flag and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(flag + "="):
            return item[len(flag) + 1:]
    return None


def _is_helper_title(title):
    """True when a postgres child title is a known helper, not a client.

    A helper matches only when the title is the helper name exactly, or the
    helper name followed by a space and digits only (``io worker 3``). A
    prefix match would take a client whose database user starts with a helper
    name (``archiver accounting ...``) for a helper.
    """
    for helper in POSTGRES_HELPERS:
        if title == helper:
            return True
        if title.startswith(helper + " "):
            rest = title[len(helper) + 1:]
            if rest.isdigit():
                return True
    return False


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


def _dedupe_real(paths):
    out = []
    seen = set()
    for path in paths:
        if not path:
            continue
        real = os.path.realpath(os.path.expanduser(path))
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def _user_tmp_roots():
    roots = []
    confdir = None
    try:
        confdir = os.confstr("CS_DARWIN_USER_TEMP_DIR")
    except (AttributeError, OSError, ValueError):
        confdir = None
    if confdir:
        roots.append(confdir)
    try:
        roots.append(tempfile.gettempdir())
    except (AttributeError, OSError):
        pass
    roots.extend(["/tmp", "/private/tmp"])
    return _dedupe_real(roots)


def inside_nofollow(root, path):
    """Containment check that does not resolve the final path's symlinks."""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    return path == root or path.startswith(root + os.sep)


def inside(root, path):
    root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _glob_match(name, patterns):
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return pattern
    return ""


def read_last_run(state):
    """The last-run.json contents as ``{"sweepers": {name: entry}}``.

    A file in the old single-timestamp format is read as "every sweeper last
    ran at that time". A missing or unreadable file means no sweeper has run.
    """
    path = os.path.join(state, "last-run.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"sweepers": {}}
    if not isinstance(data, dict):
        return {"sweepers": {}}
    sweepers = data.get("sweepers")
    if isinstance(sweepers, dict):
        clean = {k: v for k, v in sweepers.items() if isinstance(v, dict)}
        return {"sweepers": clean}
    if "ended_at" in data:
        return {
            "sweepers": {name: {"ended_at": data.get("ended_at")} for name in SWEEPERS}
        }
    return {"sweepers": {}}


# --- the janitor -----------------------------------------------------------


class Janitor:
    def __init__(
        self,
        cfg,
        state,
        logs,
        apply=False,
        snapshot=None,
        now=None,
        config_file=None,
        scheduled=False,
        snapshot_factory=None,
    ):
        self.cfg = cfg
        self.state_dir = state
        self.log_dir = logs
        self.config_file = config_file or config_path()
        self.apply = bool(apply)
        self.scheduled = bool(scheduled)
        self.snapshot = snapshot
        self._snapshot_injected = snapshot is not None
        self.snapshot_factory = snapshot_factory or take_snapshot
        self.now_fn = now or time.time
        self.snapshot_ts = self.now_fn() if snapshot is not None else None
        self.start_ts = self.now_fn()
        self.removed = 0
        self.kept = 0
        self.errors = 0
        self.freed_bytes = 0
        self.per = {name: {"removed": 0, "kept": 0, "errors": 0} for name in SWEEPERS}
        self.per["all"] = {"removed": 0, "kept": 0, "errors": 0}
        self.actions = []
        self.keep_counts = {}
        self.raised = False
        self.deadline = None
        self._timeout_logged = set()
        self.timed_out = set()
        self.log = None
        self.log_path = ""

        self.tmp_roots = _user_tmp_roots()
        self.claude_container = os.path.join("/private/tmp", "claude-%d" % os.getuid())
        self.disposable_roots = _dedupe_real(
            _user_tmp_roots()
            + ["/tmp", "/private/tmp", "~/.cache/agent-dash/scratch"]
            + list(cfg.get("postgres_extra_roots") or [])
        )
        self.protect_globs = list(DEFAULT_TMP_PROTECT_GLOBS) + list(
            cfg.get("tmp_protect_globs") or []
        )
        self.worktree_protect_globs = [
            os.path.expanduser(p) for p in cfg.get("worktree_protect_globs") or []
        ]

        # Injectable guards: tests replace these.
        self.owner_ok = lambda st: st.st_uid == os.getuid()
        self.pid_alive = _pid_alive
        self.stop_postgres = self._stop_postgres

    # -- bookkeeping --------------------------------------------------------

    def now(self):
        return self.now_fn()

    def _take_snapshot(self):
        snap = self.snapshot_factory()
        self.snapshot = snap
        self.snapshot_ts = self.now()
        if not snap.ok:
            self.write({"type": "snapshot", "ok": False, "error": snap.error})
        return snap

    def current_snapshot(self):
        """The process snapshot, refreshed when the janitor's own is stale.

        A snapshot injected by a test is never replaced. A failed refresh
        returns the failed snapshot, so everything counts as in use.
        """
        if self.snapshot is None:
            return self._take_snapshot()
        if self._snapshot_injected:
            return self.snapshot
        max_age = float(self.cfg.get("snapshot_max_age_seconds", 120))
        if self.snapshot_ts is None or self.now() - self.snapshot_ts > max_age:
            return self._take_snapshot()
        return self.snapshot

    def open_log(self):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except OSError as exc:
            raise SystemExit("cannot create log dir %s: %s" % (self.log_dir, exc))
        stamp = utc_stamp(self.now())
        self.log_path = os.path.join(self.log_dir, "run-%s.jsonl" % stamp)
        self.log = open(self.log_path, "a", encoding="utf-8")
        return self.log_path

    def write(self, obj):
        if self.log is None:
            return
        try:
            self.log.write(json.dumps(obj, sort_keys=True) + "\n")
            self.log.flush()
        except OSError:
            pass

    def count_removed(self, sweeper, n=1, freed=0):
        n = int(n or 0)
        self.removed += n
        self.per.setdefault(sweeper, {"removed": 0, "kept": 0, "errors": 0})
        self.per[sweeper]["removed"] += n
        self.per["all"]["removed"] += n
        try:
            self.freed_bytes += int(freed or 0)
        except (TypeError, ValueError):
            pass

    def count_kept(self, sweeper, reason, n=1):
        n = int(n or 0)
        self.kept += n
        self.per.setdefault(sweeper, {"removed": 0, "kept": 0, "errors": 0})
        self.per[sweeper]["kept"] += n
        self.per["all"]["kept"] += n
        groups = self.keep_counts.setdefault(sweeper, {})
        groups[reason] = groups.get(reason, 0) + n

    def keep(self, sweeper, reason, target=None):
        self.count_kept(sweeper, reason, 1)
        if target is not None:
            self.write(
                {"type": "keep", "sweeper": sweeper, "target": target, "reason": reason}
            )

    def removed_item(self, sweeper, target, reason, size=None, verb="remove"):
        self.count_removed(sweeper, 1, size or 0)
        rec = {
            "type": "action",
            "sweeper": sweeper,
            "verb": verb,
            "target": target,
            "reason": reason,
            "mode": "apply" if self.apply else "dry-run",
        }
        if size is not None:
            rec["size"] = size
        self.actions.append(rec)
        self.write(rec)
        size_note = "" if size is None else ", %d B" % size
        if self.scheduled:
            # launchd appends stdout to launchd.log without limit; per-item
            # lines belong only in this run's jsonl.
            return
        print(
            "  %s %-9s %s  (%s%s)"
            % (
                "remove" if self.apply else "would remove",
                sweeper,
                target,
                reason,
                size_note,
            )
        )

    def error(self, sweeper, message):
        self.errors += 1
        self.per.setdefault(sweeper, {"removed": 0, "kept": 0, "errors": 0})
        self.per[sweeper]["errors"] += 1
        self.per["all"]["errors"] += 1
        self.write({"type": "error", "sweeper": sweeper, "message": message})
        print("  error    %-9s %s" % (sweeper, message), file=sys.stderr)

    def flush_keeps(self, sweeper):
        groups = self.keep_counts.pop(sweeper, None)
        if not groups:
            return
        self.write({"type": "kept", "sweeper": sweeper, "counts": groups})
        detail = ", ".join("%s=%d" % (k, groups[k]) for k in sorted(groups))
        print("  kept     %-9s %s" % (sweeper, detail))

    def expired(self):
        if self.deadline is None:
            return False
        if time.monotonic() < self.deadline:
            return False
        if self.deadline not in self._timeout_logged:
            self._timeout_logged.add(self.deadline)
        return True

    def _timed_out(self, name):
        self.timed_out.add(name)
        message = "%s sweeper ran out of its %ss budget" % (
            name,
            self.cfg.get("sweeper_budget_seconds"),
        )
        self.write({"type": "timeout", "sweeper": name, "message": message})
        self.error(name, message)

    def _run(self, argv, cwd=None, timeout=60, env=None):
        real_env = dict(os.environ)
        real_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        if env:
            real_env.update(env)
        try:
            cp = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=real_env,
            )
            return cp.returncode, cp.stdout, cp.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            return None, "", str(exc)

    # -- shared guards ------------------------------------------------------

    def still_inside(self, root, path):
        """Containment right before removal; symlinks are checked not followed."""
        if os.path.islink(path):
            return inside_nofollow(root, path)
        return inside(root, path)

    def in_use(self, path, ignore_pids=None):
        return path_in_use(self.current_snapshot(), path, ignore_pids=ignore_pids)

    def _remove_path(self, path):
        if os.path.islink(path):
            os.unlink(path)
            return
        if not os.path.lexists(path):
            return
        if not os.path.isdir(path):
            os.unlink(path)
            return

        def onerror(func, target, exc_info):
            # Fix a read-only permission once, then retry the same operation.
            try:
                os.chmod(os.path.dirname(target), 0o700)
            except OSError:
                pass
            try:
                os.chmod(target, 0o700)
            except OSError:
                pass
            func(target)

        shutil.rmtree(path, onerror=onerror)

    def _cheap_size(self, path):
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if stat.S_ISDIR(st.st_mode):
            return None
        return st.st_size

    def _scan_tmp_tree(self, path, cutoff, limit):
        """Walk the whole tree without following symlinks, collecting pid files.

        Stops early only when something newer than ``cutoff`` is found. At
        ``limit`` entries it stops and reports truncation, so the entry is
        kept rather than judged on a partial view. Returns
        ``(newest, truncated, pidfiles, readable)``; ``newest`` is None when
        part of the tree could not be read.
        """
        newest = None
        pidfiles = []
        seen = 0
        stack = [path]
        while stack:
            cur = stack.pop()
            seen += 1
            if seen > limit:
                return (newest if newest is not None else 0.0), True, pidfiles, True
            try:
                st = os.lstat(cur)
            except OSError:
                return None, False, pidfiles, False
            if newest is None or st.st_mtime > newest:
                newest = st.st_mtime
            if st.st_mtime > cutoff:
                return st.st_mtime, False, pidfiles, True
            if stat.S_ISDIR(st.st_mode):
                try:
                    with os.scandir(cur) as entries:
                        for entry in entries:
                            if entry.name == "postmaster.pid":
                                pidfiles.append(entry.path)
                            stack.append(entry.path)
                except OSError:
                    return None, False, pidfiles, False
        return (newest if newest is not None else 0.0), False, pidfiles, True

    def _live_postmaster_pid(self, pidfiles):
        """Return (pid, ok) for the first live postmaster.pid in ``pidfiles``.

        ``ok`` is False when any pid file cannot be read as a regular file
        with a numeric first line. An unreadable pid file must keep the entry:
        it is never proof that no server is running. Only a numeric PID that
        is not alive counts as no server.
        """
        for pidfile in pidfiles:
            if os.path.islink(pidfile) or not os.path.isfile(pidfile):
                return None, False
            try:
                with open(pidfile, "r", encoding="utf-8", errors="replace") as fh:
                    first = fh.readline().strip()
            except OSError:
                return None, False
            if not first.isdigit():
                return None, False
            pid = int(first)
            if self.pid_alive(pid):
                return pid, True
        return None, True

    # -- sweeper: postgres --------------------------------------------------

    def _has_client_backend(self, postmaster_pid):
        snapshot = self.current_snapshot()
        if snapshot is None or not snapshot.ok:
            return True
        for pid, proc in snapshot.procs.items():
            if proc.get("ppid") != postmaster_pid:
                continue
            args = proc.get("args") or ""
            title = ""
            if "postgres:" in args:
                title = args.split("postgres:", 1)[1].strip().lower()
            if not title:
                return True
            if _is_helper_title(title):
                continue
            return True
        return False

    def _stop_postgres(self, datadir, pid):
        pg_ctl = shutil.which("pg_ctl")
        if pg_ctl:
            rc, out, err = self._run(
                [pg_ctl, "-D", datadir, "stop", "-m", "fast", "-w", "-t", "30"],
                timeout=45,
            )
            if rc == 0:
                return True
            self.write(
                {
                    "type": "stop-failed",
                    "target": datadir,
                    "returncode": rc,
                    "stderr": (err or "").strip()[-500:],
                }
            )
            return False
        # The PID came from a snapshot that may be minutes old. Read this one
        # process again and signal only if it is still the same postmaster.
        rc, out, err = self._run(["ps", "-o", "args=", "-p", str(pid)], timeout=15)
        argv = _split_args((out or "").strip())
        value = _arg_value(argv, "-D") if argv else None
        same = (
            rc == 0
            and argv
            and os.path.basename(argv[0]) == "postgres"
            and value is not None
            and (value == datadir or os.path.realpath(value) == datadir)
        )
        if not same:
            self.write(
                {
                    "type": "stop-skipped",
                    "target": datadir,
                    "pid": pid,
                    "reason": "already gone",
                }
            )
            return True
        try:
            os.kill(pid, signal.SIGINT)
        except OSError as exc:
            self.write({"type": "stop-failed", "target": datadir, "error": str(exc)})
            return False
        deadline = time.time() + 30
        while time.time() < deadline:
            if not self.pid_alive(pid):
                return True
            time.sleep(0.25)
        return False

    def sweep_postgres(self):
        snapshot = self.current_snapshot()
        if not snapshot.ok:
            self.write(
                {"type": "snapshot", "ok": False, "error": snapshot.error}
            )
            self.count_kept("postgres", "snapshot-failed", len(snapshot.procs))
            return
        min_age = float(self.cfg["pg_min_age_hours"]) * 3600
        for pid, proc in sorted(snapshot.procs.items()):
            if self.expired():
                self._timed_out("postgres")
                return
            argv = _split_args(proc.get("args") or "")
            if not argv:
                continue
            if os.path.basename(argv[0]) != "postgres":
                continue
            datadir = _arg_value(argv, "-D")
            if not datadir:
                continue
            if not os.path.isabs(datadir):
                cwd = snapshot.cwds.get(pid)
                if not cwd:
                    self.keep("postgres", "unresolvable-data-dir", datadir)
                    continue
                datadir = os.path.join(cwd, datadir)
            datadir = os.path.realpath(datadir)
            if not any(inside(root, datadir) for root in self.disposable_roots):
                self.keep("postgres", "outside-disposable-roots", datadir)
                continue
            if proc.get("etime", 0) < min_age:
                self.keep("postgres", "too-young", datadir)
                continue
            if self._has_client_backend(pid):
                self.keep("postgres", "client-backend", datadir)
                continue
            ignore = set(snapshot.children(pid)) | {pid}
            used, why = self.in_use(datadir, ignore_pids=ignore)
            if used:
                self.keep("postgres", "in-use:" + why, datadir)
                continue
            if self.apply:
                if not self.stop_postgres(datadir, pid):
                    self.error("postgres", "server did not stop: %s (pid %d)" % (datadir, pid))
                    continue
            self.removed_item("postgres", datadir, "idle-disposable", verb="stop")

    # -- sweeper: tmp -------------------------------------------------------

    def _tmp_entries(self, root):
        """Yield (root, entry, is_session). The claude-<uid> container yields sessions."""
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return
        container = self.claude_container
        for name in names:
            path = os.path.join(root, name)
            if (
                container
                and os.path.realpath(path) == os.path.realpath(container)
                and os.path.isdir(path)
                and not os.path.islink(path)
            ):
                try:
                    projects = sorted(os.listdir(path))
                except OSError:
                    continue
                for project in projects:
                    project_path = os.path.join(path, project)
                    if not os.path.isdir(project_path) or os.path.islink(project_path):
                        continue
                    try:
                        sessions = sorted(os.listdir(project_path))
                    except OSError:
                        continue
                    for session in sessions:
                        session_path = os.path.join(project_path, session)
                        if os.path.isdir(session_path) and not os.path.islink(session_path):
                            yield root, session_path, True
                continue
            yield root, path, False

    def sweep_tmp(self):
        days = float(self.cfg["tmp_min_age_days"])
        session_days = float(self.cfg["session_min_age_days"])
        cutoff = self.now() - days * 86400
        session_cutoff = self.now() - session_days * 86400
        for root in self.tmp_roots:
            if self.expired():
                self._timed_out("tmp")
                return
            if not os.path.isdir(root):
                continue
            for entry_root, path, is_session in self._tmp_entries(root):
                if self.expired():
                    self._timed_out("tmp")
                    return
                if is_session:
                    self._consider_tmp_entry(entry_root, path, session_cutoff, session_days)
                else:
                    self._consider_tmp_entry(entry_root, path, cutoff, days)

    def _consider_tmp_entry(self, root, path, cutoff, days):
        name = os.path.basename(path)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            # It went away between listdir and stat: nothing to remove.
            return
        except OSError as exc:
            self.error("tmp", "cannot stat %s: %s" % (path, exc))
            return
        if not self.owner_ok(st):
            self.keep("tmp", "other-owner", path)
            return
        if stat.S_ISSOCK(st.st_mode) or stat.S_ISFIFO(st.st_mode):
            self.keep("tmp", "socket-or-fifo", path)
            return
        protected = _glob_match(name, self.protect_globs)
        if protected:
            self.keep("tmp", "protected-name:" + protected, path)
            return
        is_link = stat.S_ISLNK(st.st_mode)
        is_dir = stat.S_ISDIR(st.st_mode) and not is_link
        if is_dir and os.path.lexists(os.path.join(path, ".git")):
            self.keep("tmp", "git-repo", path)
            return
        limit = int(self.cfg.get("tmp_age_scan_max_entries", 200000))
        newest, truncated, pidfiles, readable = self._scan_tmp_tree(path, cutoff, limit)
        if not readable or newest is None:
            self.keep("tmp", "unreadable-age", path)
            return
        if truncated:
            self.keep("tmp", "age-scan-truncated", path)
            return
        if newest > cutoff:
            self.keep("tmp", "too-new", path)
            return
        if pidfiles:
            live, ok = self._live_postmaster_pid(pidfiles)
            if not ok:
                self.keep("tmp", "unreadable-postmaster-pid", path)
                return
            if live:
                self.keep("tmp", "live-postgres:%d" % live, path)
                return
        used, why = self.in_use(path)
        if used:
            self.keep("tmp", "in-use:" + why, path)
            return
        # The folder can be replaced or written to between the first lstat and
        # the removal. Re-stat it and keep it unless it is the same object.
        try:
            again = os.lstat(path)
        except OSError:
            self.keep("tmp", "changed-during-scan", path)
            return
        if (again.st_dev, again.st_ino, again.st_mtime) != (
            st.st_dev,
            st.st_ino,
            st.st_mtime,
        ):
            self.keep("tmp", "changed-during-scan", path)
            return
        if not self.still_inside(root, path):
            self.keep("tmp", "escaped-root", path)
            return
        size = self._cheap_size(path)
        if self.apply:
            try:
                self._remove_path(path)
            except OSError as exc:
                self.error("tmp", "cannot remove %s: %s" % (path, exc))
                return
        self.removed_item("tmp", path, "older-than-%gd" % float(days), size)

    # -- sweeper: worktrees -------------------------------------------------

    def _manager_cmd(self):
        cmd = self.cfg.get("worktree_manager_cmd") or ""
        return shutil.which(cmd) if cmd else None

    def sweep_worktrees(self):
        self._worktrees_part_a()
        if self.expired():
            self._timed_out("worktrees")
            return
        for repo in self._worktree_repos():
            if self.expired():
                self._timed_out("worktrees")
                return
            self._sweep_repo(repo)

    def _worktrees_part_a(self):
        exe = self._manager_cmd()
        if not exe:
            self.write(
                {
                    "type": "skip",
                    "sweeper": "worktrees",
                    "part": "manager",
                    "reason": "not on PATH: %s" % self.cfg.get("worktree_manager_cmd"),
                }
            )
            return
        rc, out, err = self._run([exe, "plan"], timeout=180)
        if rc != 0:
            self.write(
                {
                    "type": "skip",
                    "sweeper": "worktrees",
                    "part": "manager",
                    "reason": "plan failed rc=%s" % rc,
                    "stderr": (err or "").strip()[-500:],
                }
            )
            return
        try:
            top = json.loads(out.strip())
            plan_file = top["plan"]
        except (ValueError, KeyError, TypeError):
            self.write(
                {
                    "type": "skip",
                    "sweeper": "worktrees",
                    "part": "manager",
                    "reason": "plan output is not the expected JSON",
                }
            )
            return
        try:
            with open(plan_file, "r", encoding="utf-8") as fh:
                plan = json.load(fh)
            actions = plan["actions"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.write(
                {
                    "type": "skip",
                    "sweeper": "worktrees",
                    "part": "manager",
                    "reason": "cannot read plan %s: %s" % (plan_file, exc),
                }
            )
            return
        for action in actions:
            if self.expired():
                self._timed_out("worktrees")
                return
            if not isinstance(action, dict) or action.get("decision") != "candidate":
                continue
            path = action.get("path")
            sha = action.get("HEAD") or ""
            if not path or not sha:
                self.error("worktrees", "manager candidate without path or head: %r" % (action,))
                continue
            argv = [exe, "closeout", "--worktree", path, "--expected-head", sha]
            if self.apply:
                argv.append("--apply")
            rc, out, err = self._run(argv, timeout=180)
            self.write(
                {
                    "type": "action",
                    "sweeper": "worktrees",
                    "part": "manager",
                    "verb": "closeout",
                    "target": path,
                    "mode": "apply" if self.apply else "dry-run",
                    "returncode": rc,
                    "stdout": (out or "").strip()[-500:],
                }
            )
            if rc != 0:
                self.error(
                    "worktrees",
                    "closeout refused %s: rc=%s %s"
                    % (path, rc, (err or "").strip()[-300:]),
                )
                continue
            self.removed_item("worktrees", path, "manager-closeout", verb="closeout")

    def _worktree_repos(self):
        repos = []
        configured = list(self.cfg.get("repos") or [])
        if configured:
            for path in configured:
                path = os.path.realpath(os.path.expanduser(path))
                if os.path.isdir(path):
                    repos.append(path)
            return sorted(set(repos))
        for root in self.cfg.get("repo_roots") or []:
            root = os.path.realpath(os.path.expanduser(root))
            if not os.path.isdir(root):
                continue
            try:
                children = sorted(os.listdir(root))
            except OSError:
                continue
            for child in children:
                child_path = os.path.join(root, child)
                if not os.path.isdir(child_path) or os.path.islink(child_path):
                    continue
                if os.path.isdir(os.path.join(child_path, ".git")):
                    repos.append(child_path)
                try:
                    grandchildren = sorted(os.listdir(child_path))
                except OSError:
                    continue
                for grandchild in grandchildren:
                    grandchild_path = os.path.join(child_path, grandchild)
                    if not os.path.isdir(grandchild_path) or os.path.islink(grandchild_path):
                        continue
                    if os.path.isdir(os.path.join(grandchild_path, ".git")):
                        repos.append(grandchild_path)
        return sorted(set(repos))

    def _default_branch(self, repo):
        rc, out, err = self._run(
            ["git", "-C", repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            timeout=30,
        )
        if rc == 0 and out.strip().startswith("refs/remotes/origin/"):
            return out.strip()[len("refs/remotes/origin/"):]
        for candidate in ("main", "master"):
            rc, out, err = self._run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", "origin/" + candidate],
                timeout=30,
            )
            if rc == 0:
                return candidate
        return "main"

    @staticmethod
    def _parse_worktrees(text):
        entries = []
        for block in text.split("\n\n"):
            if not block.strip():
                continue
            item = {}
            for line in block.splitlines():
                if line.startswith("worktree "):
                    item["path"] = line[len("worktree "):]
                elif line.startswith("HEAD "):
                    item["head"] = line[len("HEAD "):]
                elif line.startswith("branch "):
                    item["branch"] = line[len("branch "):]
                elif line == "detached":
                    item["detached"] = True
                elif line == "locked" or line.startswith("locked "):
                    item["locked"] = True
                elif line == "prunable" or line.startswith("prunable "):
                    item["prunable"] = True
            if item.get("path"):
                entries.append(item)
        return entries

    def _sweep_repo(self, repo):
        default = self._default_branch(repo)
        rc, out, err = self._run(
            ["git", "-C", repo, "remote", "get-url", "origin"], timeout=30
        )
        if rc != 0:
            # No origin: nothing to compare against, so skip the repo quietly.
            self.write(
                {
                    "type": "skip",
                    "sweeper": "worktrees",
                    "repo": repo,
                    "reason": "no origin remote",
                }
            )
            return
        rc, out, err = self._run(
            ["git", "-C", repo, "fetch", "origin", "--quiet"],
            timeout=60,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        fetch_failed = rc != 0
        if fetch_failed:
            # Stale refs are not proof of a merge: remove nothing from this
            # repo in this run.
            self.write(
                {
                    "type": "warn",
                    "sweeper": "worktrees",
                    "repo": repo,
                    "message": "fetch failed rc=%s; removing nothing from this repo" % rc,
                    "stderr": (err or "").strip()[-300:],
                }
            )
        rc, out, err = self._run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=60
        )
        if rc != 0:
            self.error("worktrees", "worktree list failed in %s: %s" % (repo, (err or "").strip()))
            return
        main_path = os.path.realpath(repo)
        for entry in self._parse_worktrees(out):
            if self.expired():
                self._timed_out("worktrees")
                return
            self._consider_worktree(repo, entry, default, main_path, fetch_failed)
        if self.apply and not fetch_failed:
            rc, out, err = self._run(["git", "-C", repo, "worktree", "prune"], timeout=60)
            if rc != 0:
                self.error("worktrees", "worktree prune failed in %s: %s" % (repo, (err or "").strip()))

    def _protected_worktree(self, path):
        for pattern in self.worktree_protect_globs:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(os.path.realpath(path), pattern):
                return pattern
        return ""

    def _consider_worktree(self, repo, entry, default, main_path, fetch_failed=False):
        path = entry.get("path") or ""
        if not path:
            return
        real = os.path.realpath(path)
        if real == main_path:
            # The main checkout is not a candidate at all.
            return
        if not os.path.isdir(real):
            return
        branch_ref = entry.get("branch") or ""
        if entry.get("detached") or not branch_ref.startswith("refs/heads/"):
            self.keep("worktrees", "detached", path)
            return
        if entry.get("locked"):
            self.keep("worktrees", "locked", path)
            return
        branch = branch_ref[len("refs/heads/"):]
        if branch in ("main", "master"):
            self.keep("worktrees", "main-branch", path)
            return
        protected = self._protected_worktree(path)
        if protected:
            self.keep("worktrees", "protected-path:" + protected, path)
            return
        # Read the idle timestamps before any further git command: `git status`
        # refreshes and rewrites the index, which would make the worktree look
        # active on every run.
        rc, out, err = self._run(
            ["git", "-C", path, "rev-parse", "--git-dir"], timeout=60
        )
        if rc != 0 or not out.strip():
            self.keep("worktrees", "unreadable-admin-dir", path)
            return
        admin = out.strip()
        if not os.path.isabs(admin):
            admin = os.path.join(path, admin)
        rc, out, err = self._run(
            ["git", "-C", path, "log", "-1", "--format=%ct"], timeout=60
        )
        if rc != 0 or not out.strip().isdigit():
            self.keep("worktrees", "unreadable-commit-time", path)
            return
        newest = int(out.strip())
        # `index` is deliberately not part of the idle signal: any `git status`
        # rewrites it, and status scans over every worktree run daily.
        for name in ("HEAD", "logs/HEAD"):
            try:
                newest = max(newest, os.stat(os.path.join(admin, name)).st_mtime)
            except OSError:
                pass
        idle = float(self.cfg["worktree_min_idle_days"]) * 86400
        if self.now() - newest < idle:
            self.keep("worktrees", "recent", path)
            return
        rc, out, err = self._run(
            ["git", "-C", path, "status", "--porcelain", "--ignored"], timeout=60
        )
        if rc != 0:
            self.keep("worktrees", "unreadable-status", path)
            return
        for line in out.splitlines():
            if not line.strip():
                continue
            if line.startswith("!! "):
                tail = line[3:].strip().rstrip("/").split("/")[-1]
                if tail in ALLOWED_IGNORED:
                    continue
                self.keep("worktrees", "ignored-entry:" + tail, path)
                return
            self.keep("worktrees", "dirty", path)
            return
        for name in MERGE_STATE_FILES:
            if os.path.lexists(os.path.join(admin, name)):
                self.keep("worktrees", "merge-state:" + name, path)
                return
        used, why = self.in_use(path)
        if used:
            self.keep("worktrees", "in-use:" + why, path)
            return
        if fetch_failed:
            self.keep("worktrees", "fetch-failed", path)
            return
        tip = entry.get("head") or ""
        if not tip:
            self.keep("worktrees", "unreadable-head", path)
            return
        how = self._merged_how(repo, path, branch, default, tip)
        if not how:
            self.keep("worktrees", "unmerged", path)
            return
        if not self.apply:
            self.removed_item("worktrees", path, how)
            return
        # The merge proof and the gh call can take a long time. Re-read the
        # head and take a fresh process snapshot right before archiving.
        rc, out, err = self._run(["git", "-C", path, "rev-parse", "HEAD"], timeout=60)
        if rc != 0 or out.strip() != tip:
            self.keep("worktrees", "head-moved", path)
            return
        fresh = self._take_snapshot()
        used, why = path_in_use(fresh, path)
        if used:
            self.keep("worktrees", "in-use:" + why, path)
            return
        archive_ref = self._archive_worktree(repo, path, branch, tip)
        if not archive_ref:
            return
        rc, out, err = self._run(
            ["git", "-C", repo, "worktree", "remove", path], timeout=120
        )
        if rc != 0:
            self.error(
                "worktrees",
                "git worktree remove refused %s: %s" % (path, (err or "").strip()[-300:]),
            )
            return
        self.removed_item("worktrees", path, how)

    def _archive_worktree(self, repo, path, branch, tip):
        stamp = utc_stamp(self.now())
        ref = "refs/workspace-archive/janitor/%s/%s" % (stamp, branch)
        rc, out, err = self._run(
            ["git", "-C", repo, "update-ref", ref, tip], timeout=60
        )
        if rc != 0:
            self.error(
                "worktrees",
                "cannot archive %s at %s: %s" % (branch, ref, (err or "").strip()[-300:]),
            )
            return ""
        record = {
            "repo": repo,
            "path": path,
            "branch": branch,
            "sha": tip,
            "archive_ref": ref,
            "time": iso(self.now()),
        }
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(
                os.path.join(self.state_dir, "recovery.jsonl"), "a", encoding="utf-8"
            ) as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            self.error("worktrees", "cannot write recovery line for %s: %s" % (path, exc))
            return ""
        return ref

    def pr_merged_heads(self, repo, branch, base):
        """Head shas of merged pull requests for ``branch`` into ``base``."""
        gh = shutil.which("gh")
        if not gh:
            return []
        rc, out, err = self._run(
            [
                gh,
                "pr",
                "list",
                "--state",
                "merged",
                "--head",
                branch,
                "--base",
                base,
                "--json",
                "headRefOid",
                "--limit",
                "20",
            ],
            cwd=repo,
            timeout=30,
        )
        if rc != 0:
            self.write(
                {
                    "type": "warn",
                    "sweeper": "worktrees",
                    "repo": repo,
                    "message": "gh pr list failed for %s: rc=%s" % (branch, rc),
                }
            )
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [item.get("headRefOid") or "" for item in data if isinstance(item, dict)]

    def _merged_how(self, repo, path, branch, default, tip):
        upstream = "origin/" + default
        rc, out, err = self._run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", tip, upstream],
            timeout=60,
        )
        if rc == 0:
            return "ancestor-of-" + default
        if tip in self.pr_merged_heads(repo, branch, default):
            return "merged-pull-request"
        rc, out, err = self._run(
            ["git", "-C", repo, "merge-base", tip, upstream], timeout=60
        )
        if rc != 0 or not out.strip():
            return ""
        merge_base = out.strip()
        rc, out, err = self._run(
            [
                "git",
                "-C",
                repo,
                "commit-tree",
                tip + "^{tree}",
                "-p",
                merge_base,
                "-m",
                "x",
            ],
            timeout=60,
        )
        if rc != 0 or not out.strip():
            return ""
        synthetic = out.strip()
        rc, out, err = self._run(
            ["git", "-C", repo, "cherry", upstream, synthetic], timeout=60
        )
        if rc == 0 and any(
            line.lstrip().startswith("-") for line in out.splitlines() if line.strip()
        ):
            return "squash-merged"
        return ""

    # -- sweeper: scratch ---------------------------------------------------

    def sweep_scratch(self):
        cmd = self.cfg.get("agent_dash_cmd") or ""
        path = os.path.expanduser(cmd)
        if not cmd or not os.path.isfile(path):
            self.write(
                {
                    "type": "skip",
                    "sweeper": "scratch",
                    "reason": "agent_dash.py not found at %s" % path,
                }
            )
            return
        python = shutil.which("python3") or sys.executable
        days = float(self.cfg["scratch_min_age_days"])
        argv = [
            python,
            path,
            "scratch",
            "gc",
            "--older-than-days",
            ("%g" % days),
            "--json",
        ]
        if self.apply:
            argv.append("--apply")
        rc, out, err = self._run(argv, timeout=900)
        if rc != 0:
            self.error(
                "scratch",
                "scratch gc failed rc=%s: %s" % (rc, (err or "").strip()[-300:]),
            )
            return
        try:
            report = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.error("scratch", "scratch gc did not print JSON")
            return
        self.write({"type": "gc", "sweeper": "scratch", "report": report})
        if self.apply:
            count = report.get("removed_count") or 0
            freed = report.get("freed") or 0
        else:
            count = report.get("reclaimable_count") or 0
            freed = report.get("reclaimable") or 0
        kept = report.get("kept_count") or 0
        if count:
            self.count_removed("scratch", count, freed)
            self.actions.append(
                {
                    "type": "action",
                    "sweeper": "scratch",
                    "verb": "gc",
                    "target": report.get("root", ""),
                    "reason": "older-than-%gd" % days,
                    "mode": "apply" if self.apply else "dry-run",
                    "count": count,
                }
            )
            if not self.scheduled:
                print(
                    "  %s %-9s %d folder(s) under %s  (%s)"
                    % (
                        "remove" if self.apply else "would remove",
                        "scratch",
                        count,
                        report.get("root", ""),
                        "older-than-%gd" % days,
                    )
                )
        if kept:
            self.count_kept("scratch", "gc-kept", kept)
        if report.get("failed"):
            for failed in report["failed"]:
                self.error("scratch", "could not remove %s" % failed)

    # -- scheduling and state ----------------------------------------------

    def last_runs(self):
        """Map sweeper name -> its last-run entry."""
        return read_last_run(self.state_dir)["sweepers"]

    def due_sweepers(self, only=None, skip=None):
        """The sweepers that are due, in run order.

        A sweeper is due when it has no entry, its ``ended_at`` is missing,
        non-finite or in the future, or it is older than ``interval_days``.
        """
        only = set(only or [])
        skip = set(skip or [])
        last = self.last_runs()
        interval = float(self.cfg["interval_days"]) * 86400
        now = self.now()
        due = []
        for name in SWEEPERS:
            if only and name not in only:
                continue
            if name in skip:
                continue
            entry = last.get(name)
            if not entry:
                due.append(name)
                continue
            try:
                ended = float(entry.get("ended_at"))
            except (TypeError, ValueError):
                due.append(name)
                continue
            if not math.isfinite(ended) or ended > now or now - ended >= interval:
                due.append(name)
        return due

    def due(self):
        return bool(self.due_sweepers())

    def _write_sweeper_entry(self, name, end_ts):
        """Record one finished sweeper in last-run.json, atomically."""
        data = read_last_run(self.state_dir)
        per = self.per.get(name, {})
        data["sweepers"][name] = {
            "ended_at": end_ts,
            "removed": int(per.get("removed", 0)),
            "kept": int(per.get("kept", 0)),
            "errors": int(per.get("errors", 0)),
        }
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = os.path.join(
                self.state_dir, ".last-run.%d.tmp" % os.getpid()
            )
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(self.state_dir, "last-run.json"))
        except OSError as exc:
            self.error("run", "cannot write last-run.json: %s" % exc)

    def prune_logs(self):
        try:
            names = [
                name
                for name in os.listdir(self.log_dir)
                if name.startswith("run-") and name.endswith(".jsonl")
            ]
        except OSError:
            return
        names.sort()
        for name in names[:-LOG_KEEP] if len(names) > LOG_KEEP else []:
            try:
                os.unlink(os.path.join(self.log_dir, name))
            except OSError:
                pass

    # -- run ----------------------------------------------------------------

    def run(self, only=None, skip=None, sweepers=None):
        """Run the sweepers. Returns the process exit code."""
        only = set(only or [])
        skip = set(skip or [])
        self.start_ts = self.now()
        if self.log is None:
            self.open_log()
        mode = "apply" if self.apply else "dry-run"
        self.write(
            {
                "type": "run",
                "mode": mode,
                "pid": os.getpid(),
                "start": iso(self.start_ts),
                "log": self.log_path,
            }
        )
        if self.snapshot is None:
            self.snapshot = self.current_snapshot()
        if not self.snapshot.ok:
            self.write(
                {"type": "snapshot", "ok": False, "error": self.snapshot.error}
            )
            print(
                "  snapshot failed (%s): every path counts as in use"
                % self.snapshot.error
            )
        selected = [n for n in SWEEPERS if (not only or n in only) and n not in skip]
        funcs = sweepers or {name: getattr(self, "sweep_" + name) for name in SWEEPERS}
        aborted = False
        try:
            for name in selected:
                func = funcs.get(name)
                if func is None:
                    continue
                if not self.scheduled:
                    print("%s sweeper (%s)" % (name, mode))
                self.deadline = time.monotonic() + float(
                    self.cfg["sweeper_budget_seconds"]
                )
                ok = True
                try:
                    func()
                except Exception as exc:  # one sweeper never stops the others
                    ok = False
                    self.raised = True
                    self.error(name, "sweeper raised: %r" % (exc,))
                finally:
                    self.flush_keeps(name)
                # Only a sweeper that finished cleanly inside its budget counts
                # as done; a raised or timed-out one must run again next tick.
                if self.apply and ok and name not in self.timed_out:
                    self._write_sweeper_entry(name, self.now())
        except BaseException as exc:  # interrupted: no last-run, next tick reruns
            aborted = True
            self.write({"type": "aborted", "error": repr(exc)})
            print("JANITOR: aborted: %r" % (exc,), file=sys.stderr)
        self.deadline = None
        end_ts = self.now()
        self.write(
            {
                "type": "summary",
                "mode": mode,
                "removed": self.removed,
                "kept": self.kept,
                "errors": self.errors,
                "freed_bytes": self.freed_bytes,
                "end": iso(end_ts),
            }
        )
        self.prune_logs()
        if self.log is not None:
            try:
                self.log.close()
            except OSError:
                pass
            self.log = None
        if aborted or self.raised:
            return 1
        return 0

    def report(self):
        return {
            "mode": "apply" if self.apply else "dry-run",
            "removed": self.removed,
            "kept": self.kept,
            "errors": self.errors,
            "freed_bytes": self.freed_bytes,
            "sweepers": {name: dict(self.per.get(name, {})) for name in SWEEPERS},
            "keep_reasons": {name: dict(groups) for name, groups in self.keep_counts.items()},
            "actions": self.actions,
            "log": self.log_path,
        }

    def summary_line(self):
        return "JANITOR: mode=%s removed=%d kept=%d errors=%d freed_bytes=%d" % (
            "apply" if self.apply else "dry-run",
            self.removed,
            self.kept,
            self.errors,
            self.freed_bytes,
        )


# --- lock and commands -----------------------------------------------------


def acquire_lock(state):
    os.makedirs(state, exist_ok=True)
    fd = os.open(os.path.join(state, "lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def lock_held(state):
    fd = acquire_lock(state)
    if fd is None:
        return True
    release_lock(fd)
    return False


def _name_list(value):
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def cmd_run(cfg, args):
    jan = Janitor(
        cfg,
        state_dir(),
        log_dir(),
        apply=bool(args.apply or args.scheduled),
        config_file=config_path(),
        scheduled=bool(args.scheduled),
    )
    if args.scheduled:
        due = jan.due_sweepers(only=_name_list(args.only), skip=_name_list(args.skip))
        if not due:
            print("JANITOR: not due")
            return 0
        only = due
    else:
        only = _name_list(args.only)
    lock = acquire_lock(state_dir())
    if lock is None:
        print("already running")
        return 0
    try:
        rc = jan.run(only=only, skip=_name_list(args.skip))
    finally:
        release_lock(lock)
    if args.json:
        print(json.dumps(jan.report(), sort_keys=True))
    print(jan.summary_line())
    return rc


def _state_summary(cfg):
    last = read_last_run(state_dir())["sweepers"]
    interval = float(cfg["interval_days"])
    now = time.time()
    per = {}
    any_due = False
    for name in SWEEPERS:
        entry = last.get(name)
        if not entry:
            per[name] = {"last_run": None, "due": True, "due_in_hours": 0.0}
            any_due = True
            continue
        try:
            ended = float(entry.get("ended_at"))
        except (TypeError, ValueError):
            ended = None
        if ended is None or not math.isfinite(ended) or ended > now:
            # A missing, non-finite or future ended_at counts as due.
            per[name] = {
                "last_run": iso(ended) if ended is not None and math.isfinite(ended) else None,
                "due": True,
                "due_in_hours": 0.0,
            }
            any_due = True
            continue
        due_in = max(0.0, interval * 86400 - (now - ended))
        per[name] = {
            "last_run": iso(ended),
            "due": due_in <= 0,
            "due_in_hours": round(due_in / 3600.0, 2),
        }
        if due_in <= 0:
            any_due = True
    try:
        logs = len(
            [
                n
                for n in os.listdir(log_dir())
                if n.startswith("run-") and n.endswith(".jsonl")
            ]
        )
    except OSError:
        logs = 0
    return {
        "state_dir": state_dir(),
        "log_dir": log_dir(),
        "config": config_path(),
        "config_exists": os.path.exists(config_path()),
        "due": any_due,
        "interval_days": interval,
        "lock_held": lock_held(state_dir()),
        "log_files": logs,
        "sweepers": SWEEPERS,
        "sweeper_state": per,
    }


def cmd_status(cfg, args):
    data = _state_summary(cfg)
    if args.json:
        print(json.dumps(data, sort_keys=True))
        return 0
    print("state dir: %s" % data["state_dir"])
    print("log dir:   %s (%d run log(s))" % (data["log_dir"], data["log_files"]))
    print(
        "config:    %s (%s)"
        % (data["config"], "present" if data["config_exists"] else "all defaults")
    )
    print("interval:  %gd" % data["interval_days"])
    for name in data["sweepers"]:
        state = data["sweeper_state"][name]
        if state["last_run"]:
            when = "due now" if state["due"] else "next in %.1fh" % state["due_in_hours"]
            print("  %-9s last %s, %s" % (name, state["last_run"], when))
        else:
            print("  %-9s never, due now" % name)
    print("due:       %s" % ("yes" if data["due"] else "no"))
    print("lock:      %s" % ("held" if data["lock_held"] else "free"))
    return 0


def launchd_plist_text():
    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", installed_script(), "run", "--scheduled"],
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": 4, "Minute": 40},
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        },
        "StandardOutPath": os.path.join(log_dir(), "launchd.log"),
        "StandardErrorPath": os.path.join(log_dir(), "launchd.log"),
    }
    return plistlib.dumps(payload).decode("utf-8")


def cmd_install(args, cfg=None):
    source = os.path.abspath(__file__)
    target = installed_script()
    plist = plist_path()
    plan = [
        "copy %s to %s" % (source, target),
        "write %s" % plist,
        "launchctl bootout gui/%d/%s (ignore failure)" % (os.getuid(), LABEL),
        "launchctl bootstrap gui/%d %s" % (os.getuid(), plist),
    ]
    if not args.apply:
        print("install (dry run; add --apply to do it):")
        for line in plan:
            print("  would %s" % line)
        return 0
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.makedirs(launch_agents_dir(), exist_ok=True)
        os.makedirs(log_dir(), exist_ok=True)
        shutil.copy2(source, target)
        tmp = plist + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(launchd_plist_text())
        os.replace(tmp, plist)
    except OSError as exc:
        print("install failed: %s" % exc, file=sys.stderr)
        return 1
    jan = Janitor(cfg or dict(DEFAULTS), state_dir(), log_dir())
    jan._run(
        ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=60
    )
    rc, out, err = jan._run(
        ["launchctl", "bootstrap", "gui/%d" % os.getuid(), plist], timeout=60
    )
    if rc != 0:
        print(
            "launchctl bootstrap failed: %s" % (err or "").strip()[-500:],
            file=sys.stderr,
        )
        return 1
    print("installed %s and loaded %s" % (target, LABEL))
    return 0


def cmd_uninstall(args, cfg=None):
    target = installed_script()
    plist = plist_path()
    plan = [
        "launchctl bootout gui/%d/%s (ignore failure)" % (os.getuid(), LABEL),
        "remove %s" % plist,
        "remove %s" % target,
    ]
    if not args.apply:
        print("uninstall (dry run; add --apply to do it):")
        for line in plan:
            print("  would %s" % line)
        return 0
    jan = Janitor(cfg or dict(DEFAULTS), state_dir(), log_dir())
    jan._run(
        ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=60
    )
    for path in (plist, target):
        try:
            os.unlink(path)
        except OSError:
            pass
    print("uninstalled %s" % LABEL)
    return 0


# --- entry point -----------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="laptop_janitor.py",
        description="Scheduled cleanup of agent leftovers on macOS.",
    )
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="sweep (dry run unless --apply)")
    run_p.add_argument("--apply", action="store_true", help="actually remove")
    run_p.add_argument(
        "--scheduled",
        action="store_true",
        help="launchd entry point: apply only when due",
    )
    run_p.add_argument("--only", default="", help="comma list of sweepers to run")
    run_p.add_argument("--skip", default="", help="comma list of sweepers to skip")
    run_p.add_argument("--json", action="store_true", help="print a JSON report")

    status_p = sub.add_parser("status", help="show state, last run and due time")
    status_p.add_argument("--json", action="store_true")

    install_p = sub.add_parser("install", help="install the script and launchd agent")
    install_p.add_argument("--apply", action="store_true")

    uninstall_p = sub.add_parser("uninstall", help="remove the installed agent")
    uninstall_p.add_argument("--apply", action="store_true")

    sub.add_parser("print-plist", help="print the launchd plist text")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    if args.cmd == "print-plist":
        sys.stdout.write(launchd_plist_text())
        return 0
    try:
        cfg = load_config()
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 1
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "install":
        return cmd_install(args, cfg)
    if args.cmd == "uninstall":
        return cmd_uninstall(args, cfg)
    if args.cmd == "run":
        try:
            return cmd_run(cfg, args)
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
