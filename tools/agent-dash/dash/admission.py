"""Race-safe execution leases shared by every agent-dash launcher.

Two launchers that touch the same file must not both start. This store is the
one place that decides, so the dispatcher, the wave runner and a manual launch
all ask the same question and get the same answer.

Design constraints:

* Declarations are exact. A path is canonicalized to a repository-relative
  POSIX path before it is compared, so ``./a/b.py``, ``a//b.py`` and
  ``a/../a/b.py`` are one file. Absolute paths, paths that escape the root and
  glob syntax are refused, because a lease that silently covers a pattern is a
  lease nobody can reason about.
* The lease identity is the absolute real path, and the relative name is only
  what the dashboard shows. Two worktrees that each declare ``src/mod.py`` are
  two files, and a declaration made relative to a worktree collides with the
  same file declared by its full path from the repository root.
* Every read-modify-write runs under ``fcntl.flock`` on ``admission.lock`` and
  lands through a temp file plus ``os.replace``. A crash never leaves a
  half-written ``admission.json``, and two claimers never both win.
* A lease is live while it is unexpired and its recorded pid is not provably
  dead. A dead pid is reaped at once. An unknown pid is never reaped, because
  a lease that outlives its process is cheaper than a stolen file.
* Release and reap are idempotent, so callers retry them freely.

State lives in ``admission.json`` beside ``admission.lock`` under
``$AGENT_DASH_STATE_DIR`` (default ``~/.local/share/agent-dash``). Stdlib only.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
import uuid

STATE_ENV = "AGENT_DASH_STATE_DIR"
STATE_FILE = "admission.json"
LOCK_FILE = "admission.lock"
STATE_VERSION = 2
DEFAULT_TTL_S = 900

_GLOB_RE = re.compile(r"[*?\[\]{}]")


class LeaseError(ValueError):
    """A declaration or a state file that cannot be trusted."""


def default_state_dir() -> str:
    """``$AGENT_DASH_STATE_DIR``, else ``~/.local/share/agent-dash``."""
    raw = os.environ.get(STATE_ENV, "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(os.path.expanduser("~"), ".local", "share", "agent-dash")


def canonical_path(path, root) -> str:
    """One repository-relative POSIX path, or :class:`LeaseError`.

    ``root`` is resolved through symlinks first, then the candidate is joined
    to it and resolved the same way, so a symlinked checkout and its real path
    produce the same answer. Empty entries, absolute paths, ``~`` paths, glob
    syntax and anything that lands outside ``root`` are refused.
    """
    root_real = os.path.realpath(os.path.expanduser(str(root)))
    text = str(path if path is not None else "").strip()
    if not text:
        raise LeaseError("empty path")
    if _GLOB_RE.search(text):
        raise LeaseError(f"glob syntax is not allowed: {text}")
    if text.startswith("~"):
        raise LeaseError(f"home-relative path is not allowed: {text}")
    if os.path.isabs(text):
        raise LeaseError(f"absolute path is not allowed: {text}")
    resolved = os.path.realpath(os.path.join(root_real, text))
    rel = os.path.relpath(resolved, root_real)
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise LeaseError(f"path outside root: {text}")
    return rel.replace(os.sep, "/")


def canonical_entries(files, root) -> list[tuple[str, str]]:
    """Canonicalize a declaration into ``(absolute, root-relative)`` pairs.

    The absolute real path is the lease identity, so two worktrees that each
    declare ``src/mod.py`` are two files and never collide. The relative path
    is what a human reads in the dashboard. Duplicates are dropped by
    identity and order is kept.

    A bare string is one path. An empty declaration is refused: a lease that
    covers nothing would admit every launcher.
    """
    if isinstance(files, str):
        items = [files]
    else:
        items = list(files or [])
    if not items:
        raise LeaseError("empty file declaration")
    root_real = os.path.realpath(os.path.expanduser(str(root)))
    seen: dict[str, tuple[str, str]] = {}
    for item in items:
        rel = canonical_path(item, root_real)
        absolute = os.path.realpath(os.path.join(root_real, rel))
        seen.setdefault(absolute, (absolute, rel))
    return list(seen.values())


def canonical_files(files, root) -> list[str]:
    """The root-relative names of a declaration, in declaration order."""
    return [rel for _, rel in canonical_entries(files, root)]


def _pid_alive(pid):
    """True, False, or None when the answer cannot be proven."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "leases": {}}


class LeaseStore:
    """The lease file, its lock, and the operations that keep them honest."""

    def __init__(self, state_dir=None):
        self.state_dir = os.path.abspath(os.path.expanduser(str(state_dir or default_state_dir())))
        self.state_path = os.path.join(self.state_dir, STATE_FILE)
        self.lock_path = os.path.join(self.state_dir, LOCK_FILE)

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(self.state_dir, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                body = json.load(handle)
        except FileNotFoundError:
            return _empty_state()
        except (OSError, ValueError) as exc:
            raise LeaseError(f"unreadable lease state {self.state_path}: {exc}") from exc
        if not isinstance(body, dict) or body.get("version") != STATE_VERSION:
            raise LeaseError(f"unsupported lease state version in {self.state_path}")
        leases = body.get("leases")
        if not isinstance(leases, dict):
            raise LeaseError(f"malformed lease state in {self.state_path}")
        return body

    def _write(self, state: dict) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".admission-", suffix=".tmp", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            dir_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    @staticmethod
    def _is_live(lease: dict, now: float) -> bool:
        if float(lease.get("expires_at", 0)) <= now:
            return False
        return _pid_alive(lease.get("pid")) is not False

    def _reap_locked(self, state: dict, now: float) -> list[str]:
        leases = state["leases"]
        gone = [lease_id for lease_id, lease in leases.items() if not self._is_live(lease, now)]
        for lease_id in gone:
            del leases[lease_id]
        return gone

    @staticmethod
    def _conflicts(leases: dict, paths: list[str], now: float) -> list[dict]:
        wanted = set(paths)
        found = []
        for lease_id, lease in leases.items():
            if not LeaseStore._is_live(lease, now):
                continue
            held = lease.get("paths") or []
            overlap = wanted.intersection(held)
            if overlap:
                names = [rel for absolute, rel in zip(held, lease.get("files") or []) if absolute in overlap]
                found.append(
                    {
                        "lease_id": lease_id,
                        "owner": lease.get("owner"),
                        "ws": lease.get("ws"),
                        "task": lease.get("task"),
                        "files": names,
                    }
                )
        return sorted(found, key=lambda item: item["lease_id"])

    def _claim_locked(self, state, owner, ws, task, entries, ttl_s, now) -> dict:
        paths = [absolute for absolute, _ in entries]
        conflicts = self._conflicts(state["leases"], paths, now)
        if conflicts:
            return {"ok": False, "reason": "conflict", "lease_id": None, "conflicts": conflicts}
        lease_id = uuid.uuid4().hex
        state["leases"][lease_id] = {
            "owner": owner,
            "pid": os.getpid(),
            "ws": ws,
            "task": task,
            "files": [rel for _, rel in entries],
            "paths": paths,
            "created_at": now,
            "heartbeat_at": now,
            "expires_at": now + float(ttl_s),
        }
        return {"ok": True, "reason": "ok", "lease_id": lease_id, "conflicts": []}

    def claim(self, owner, ws, task, files, ttl_s=DEFAULT_TTL_S, now=None, root=None) -> dict:
        """Take a lease over ``files`` or report the leases that block it.

        ``files`` are canonicalized against ``root`` when given, else against
        the current working directory, so a caller declares paths the way it
        would type them. A declaration that cannot be canonicalized is refused
        with ``reason: invalid`` rather than raising, so a launcher can report
        it and stop.
        """
        now = time.time() if now is None else float(now)
        try:
            entries = canonical_entries(files, os.getcwd() if root is None else root)
        except LeaseError as exc:
            return {"ok": False, "reason": "invalid", "lease_id": None, "conflicts": [], "error": str(exc)}
        with self._locked():
            state = self._read()
            self._reap_locked(state, now)
            result = self._claim_locked(state, owner, ws, task, entries, ttl_s, now)
            self._write(state)
        return result

    def renew(self, lease_id, owner, ttl_s=DEFAULT_TTL_S, now=None) -> dict:
        """Extend a lease the caller owns. A missing, expired or foreign lease fails."""
        now = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            self._reap_locked(state, now)
            lease = state["leases"].get(lease_id)
            if lease is None:
                self._write(state)
                return {"ok": False, "reason": "missing", "lease_id": lease_id}
            if lease.get("owner") != owner:
                self._write(state)
                return {"ok": False, "reason": "owner", "lease_id": lease_id}
            lease["heartbeat_at"] = now
            lease["expires_at"] = now + float(ttl_s)
            self._write(state)
            return {"ok": True, "reason": "ok", "lease_id": lease_id, "expires_at": lease["expires_at"]}

    def release(self, lease_id, owner) -> dict:
        """Drop a lease the caller owns. Releasing a gone lease is a no-op success."""
        with self._locked():
            state = self._read()
            lease = state["leases"].get(lease_id)
            if lease is None:
                return {"ok": True, "reason": "missing", "lease_id": lease_id, "released": False}
            if lease.get("owner") != owner:
                return {"ok": False, "reason": "owner", "lease_id": lease_id, "released": False}
            del state["leases"][lease_id]
            self._write(state)
            return {"ok": True, "reason": "ok", "lease_id": lease_id, "released": True}

    def release_owner(self, owner) -> dict:
        """Drop every lease held by ``owner``. Used when a run is killed."""
        with self._locked():
            state = self._read()
            gone = [lease_id for lease_id, lease in state["leases"].items() if lease.get("owner") == owner]
            for lease_id in gone:
                del state["leases"][lease_id]
            if gone:
                self._write(state)
            return {"ok": True, "reason": "ok", "released": sorted(gone)}

    def reap(self, now=None) -> dict:
        """Drop expired leases and leases whose pid is provably dead."""
        now = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            gone = self._reap_locked(state, now)
            if gone:
                self._write(state)
            return {"ok": True, "reason": "ok", "reaped": sorted(gone)}

    def active(self, now=None) -> list[dict]:
        """Every live lease, oldest first, after reaping what is already gone."""
        now = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            gone = self._reap_locked(state, now)
            if gone:
                self._write(state)
            leases = state["leases"]
        rows = [dict({"lease_id": lease_id}, **lease) for lease_id, lease in leases.items()]
        return sorted(rows, key=lambda row: (row.get("created_at", 0), row["lease_id"]))

    def snapshot(self, now=None) -> dict:
        """The whole lease table as one JSON-safe body, for the dashboard."""
        now = time.time() if now is None else float(now)
        leases = self.active(now=now)
        return {"version": STATE_VERSION, "now": now, "count": len(leases), "leases": leases}

    def _admit_locked(self, state, owner, ws, task, entries, max_concurrent, ttl_s, now) -> dict:
        live = len(state["leases"])
        if max_concurrent is not None and live >= int(max_concurrent):
            return {"ok": False, "reason": "capacity", "lease_id": None, "conflicts": [], "active": live}
        result = self._claim_locked(state, owner, ws, task, entries, ttl_s, now)
        result["active"] = live + (1 if result["ok"] else 0)
        return result

    def admit_launch(
        self, owner, ws, task, files, max_concurrent, ttl_s=DEFAULT_TTL_S, now=None, root=None
    ) -> dict:
        """Capacity and conflict check plus the claim, under one lock.

        ``files`` are canonicalized against ``root`` when given, else against
        the current working directory. ``max_concurrent`` caps live leases
        across every owner. A cap of ``None`` means no cap. The reason is
        stable: ``ok``, ``capacity``, ``conflict`` or ``invalid``.
        """
        now = time.time() if now is None else float(now)
        try:
            entries = canonical_entries(files, os.getcwd() if root is None else root)
        except LeaseError as exc:
            return {
                "ok": False,
                "reason": "invalid",
                "lease_id": None,
                "conflicts": [],
                "active": None,
                "error": str(exc),
            }
        with self._locked():
            state = self._read()
            self._reap_locked(state, now)
            result = self._admit_locked(state, owner, ws, task, entries, max_concurrent, ttl_s, now)
            self._write(state)
        return result

    def admit_capacity(self, owner, ws, task, max_concurrent, ttl_s=DEFAULT_TTL_S, now=None) -> dict:
        """Take a launch-count lease that covers no file, under the same lock.

        A capacity-only lease is how a launcher that owns no file still counts
        against ``max_concurrent``. It shares the lock, the cap, the TTL, the
        owner token and the release and reap rules with :meth:`admit_launch`,
        and returns the same stable reasons: ``ok`` or ``capacity``.
        """
        now = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            self._reap_locked(state, now)
            result = self._admit_locked(state, owner, ws, task, [], max_concurrent, ttl_s, now)
            self._write(state)
        return result
