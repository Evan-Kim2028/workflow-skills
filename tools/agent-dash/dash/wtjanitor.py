"""Disk janitor for agent worktrees: reap merged lanes, delete stale build artifacts.

The reaper in :mod:`dash.worktrees` only removes worktrees whose work is
already on the main ref. Unmerged lanes are correctly kept, but each one
can carry a regenerable checkout of dependencies (``.venv`` ~1.3 GB,
``node_modules``, tool caches) that holds disk for months. The janitor
runs the reaper first, then deletes only those artifact folders from the
worktrees that remain, and only when the worktree has had no source file
touched for ``--artifact-age`` (24h by default).

Guards, all of which must hold before a folder is deleted:

- the worktree is not the main checkout and is not on the skip list;
- no process has its cwd inside it or names it on a command line;
- the worktree has no tracked or untracked source file modified inside
  the artifact window (artifact folders themselves are ignored);
- the folder name is a known artifact name (``.venv``, ``node_modules``,
  ``.pytest_cache``, ``.ruff_cache``, ``.mypy_cache``, ``__pycache__``,
  ``.tox``; ``dist``/``build`` only when git ignores them);
- git does not track anything under it;
- ``du`` could measure it first.

Disk-pressure mode: when free space on the volume is below
``--pressure-pct`` (12%), the artifact age drops to 6h so a full disk
clears itself without waiting a day. The run says so in its output.

Dry-run by default. ``--daily-maintenance`` additionally prunes the uv
cache and (when the installed agentq supports it) collects old run
folders, at most once per 24h, tracked in ``janitor_state.json`` under
the state dir. Stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dash import dataroot, errors, worktrees

JANITOR_USAGE = """agent-dash worktrees janitor

  python3 agent_dash.py worktrees janitor --repo <path> [--apply]
      [--min-age 4h] [--artifact-age 24h] [--pressure-pct 12]
      [--pressure-age 6h] [--skip <path-or-branch>] [--daily-maintenance]
      [--state-file <path>] [--json]

Dry-run by default: prints what would go, removes nothing. Pass --apply
to reap merged worktrees (as `worktrees reap` does) and delete stale
regenerable artifact folders (.venv, node_modules, tool caches) from the
worktrees that remain. A worktree counts as untouched when no source
file in it changed inside --artifact-age (default 24h). When free space
on the volume is below --pressure-pct (default 12%), the artifact age
drops to --pressure-age (default 6h) and the run says so. A worktree
with a live process inside it is never touched, and nothing git tracks
is ever deleted. --daily-maintenance adds `uv cache prune` and, when
the installed agentq supports it, `agentq gc --apply --runs-older-than
7d`, at most once per 24h.
"""

ARTIFACT_DIRS = (
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    ".tox",
)
IGNORED_ONLY_DIRS = ("dist", "build")
ALL_ARTIFACT_NAMES = frozenset(ARTIFACT_DIRS + IGNORED_ONLY_DIRS)

DEFAULT_MIN_AGE_S = 4 * 3600.0
DEFAULT_ARTIFACT_AGE_S = 24 * 3600.0
DEFAULT_PRESSURE_PCT = 12.0
DEFAULT_PRESSURE_AGE_S = 6 * 3600.0

DU_TIMEOUT_S = 60
SCAN_DEADLINE_S = 30.0
CMD_TIMEOUT_S = 900
MAINTENANCE_INTERVAL_S = 24 * 3600.0
STATE_FILE_NAME = "janitor_state.json"
AGENTQ_GC_ARGS = ("gc", "--apply", "--runs-older-than", "7d")


def disk_free_pct(path: str | Path) -> float | None:
    """Free space on the volume holding ``path`` as a percentage, or None."""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return usage.free * 100.0 / usage.total


def dir_size_bytes(path: str | Path) -> int | None:
    """Apparent size of one path via ``du -sk``, or None when it cannot tell."""
    try:
        proc = subprocess.run(
            ["du", "-skx", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=DU_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or "").strip().split()
    if not first:
        return None
    try:
        return int(first[0]) * 1024
    except ValueError:
        return None


def source_mtime(path: str | Path, deadline: float) -> float | None:
    """Newest mtime of any non-artifact file under ``path``.

    Artifact folders (and ``.git``) are skipped, so a dependency checkout
    cannot make a worktree look used. Returns None when the scan fails or
    runs past ``deadline`` (``time.monotonic()`` timestamp); callers keep
    the worktree in that case.
    """
    root = str(path)
    try:
        root_stamp = os.stat(root).st_mtime
    except OSError:
        return None
    newest: float | None = None
    stack = [root]
    while stack:
        if time.monotonic() > deadline:
            return None
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            if current == root:
                return None
            continue
        for entry in entries:
            name = entry.name
            if name in ALL_ARTIFACT_NAMES or name == ".git":
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                stamp = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
    # A worktree with no source files at all falls back to its own mtime.
    return root_stamp if newest is None else newest


def git_tracks(path: str | Path, name: str) -> bool:
    """True when git tracks anything under ``name`` inside the worktree."""
    try:
        proc = worktrees._run_git(path, "ls-files", "--", name)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if proc.returncode != 0:
        return True
    return bool((proc.stdout or "").strip())


def git_ignores(path: str | Path, name: str) -> bool:
    """True when ``name`` is ignored by git in the worktree."""
    try:
        proc = worktrees._run_git(path, "check-ignore", "-q", "--", name)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def artifact_names(path: str | Path) -> list[str]:
    """Known artifact folder names present in one worktree, sorted."""
    found: list[str] = []
    for name in sorted(ARTIFACT_DIRS) + sorted(IGNORED_ONLY_DIRS):
        candidate = os.path.join(str(path), name)
        try:
            if os.path.isdir(candidate) and not os.path.islink(candidate):
                found.append(name)
        except OSError:
            continue
    return found


def plan_artifacts(
    path: str,
    artifact_age_s: float,
    now: float,
    deadline: float,
    skips: set[str],
) -> tuple[list[dict], str]:
    """Artifact plan for one worktree plus a note when it was skipped whole."""
    if path in skips:
        return [], "on the skip list"
    names = artifact_names(path)
    if not names:
        return [], ""
    newest = source_mtime(path, deadline)
    if newest is None:
        return [], "source scan did not finish"
    age = now - newest
    if age < artifact_age_s:
        return [], "touched " + _human_duration(age) + " ago"
    rows: list[dict] = []
    for name in names:
        target = os.path.join(path, name)
        row = {"path": path, "artifact": name, "target": target, "bytes": 0,
               "decision": "keep", "reason": ""}
        if git_tracks(path, name):
            row["reason"] = "git tracks files under it"
        elif name in IGNORED_ONLY_DIRS and not git_ignores(path, name):
            row["reason"] = "not listed in .gitignore"
        else:
            size = dir_size_bytes(target)
            if size is None:
                row["reason"] = "du could not measure it"
            else:
                row["bytes"] = size
                row["decision"] = "delete"
                row["reason"] = "untouched for " + _human_duration(age)
        rows.append(row)
    return rows, ""


def remove_tree(path: str | Path) -> bool:
    """Delete one directory tree, retrying read-only entries. Never raises."""

    def _retry(func, target, _exc):
        try:
            os.chmod(os.path.dirname(target), 0o700)
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(str(path), onexc=_retry)
        else:
            shutil.rmtree(str(path), onerror=_retry)
    except OSError:
        pass
    return not os.path.exists(str(path))


def _human_bytes(count: int) -> str:
    """Bytes as a short human string."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%d B" % count


def _human_duration(seconds: float) -> str:
    """Duration as a short human string."""
    value = max(0.0, float(seconds))
    if value < 3600.0:
        return "%dm" % int(value // 60)
    if value < 86400.0:
        return "%.1fh" % (value / 3600.0)
    return "%.1fd" % (value / 86400.0)


def default_state_file() -> Path:
    """State file for the once-a-day maintenance runs."""
    return Path(dataroot.state_dir()) / STATE_FILE_NAME


def load_state(path: str | Path) -> dict:
    """Maintenance state as a dict; a broken file reads as empty."""
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(path: str | Path, state: dict) -> None:
    """Write the maintenance state, creating its folder. Never raises."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with open(str(tmp), "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(str(tmp), str(target))
    except OSError:
        pass


def _run_cmd(args: list[str], timeout: int = CMD_TIMEOUT_S) -> tuple[int, str]:
    """Run one maintenance command. Returns exit code (-1 on failure) and tail text."""
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, "did not finish"
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return proc.returncode, (tail[-1] if tail else "")


_AGENTQ_GC_SUPPORTED: list[bool] = []


def agentq_gc_supported() -> bool:
    """True when the installed agentq advertises `gc --runs-older-than`.

    The answer is cached for the life of the process, so a run of the
    janitor asks the CLI at most once.
    """
    if _AGENTQ_GC_SUPPORTED:
        return _AGENTQ_GC_SUPPORTED[-1]
    found = False
    if shutil.which("agentq") is not None:
        try:
            proc = subprocess.run(
                ["agentq", "gc", "--help"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            text = (proc.stdout or "") + (proc.stderr or "")
            found = proc.returncode == 0 and "--runs-older-than" in text
        except (OSError, subprocess.TimeoutExpired):
            found = False
    _AGENTQ_GC_SUPPORTED.append(found)
    return found


def run_maintenance(
    state_file: str | Path,
    apply: bool,
    now: float | None = None,
) -> list[dict]:
    """Run the once-a-day disk upkeep. Returns one result row per action."""
    moment = time.time() if now is None else now
    state = load_state(state_file)
    results: list[dict] = []
    plans: list[tuple[str, str, list[str]]] = [("uv_cache_prune", "uv cache prune", ["uv", "cache", "prune"])]
    if shutil.which("agentq") is not None and agentq_gc_supported():
        plans.append(("agentq_gc", "agentq gc --apply --runs-older-than 7d", ["agentq", *AGENTQ_GC_ARGS]))
    else:
        results.append({"action": "agentq_gc", "command": "agentq gc --apply --runs-older-than 7d",
                        "decision": "skip", "reason": "installed agentq has no --runs-older-than yet"})
    for key, shown, argv in plans:
        last = state.get(key)
        last_ts = float(last) if isinstance(last, (int, float)) else 0.0
        if moment - last_ts < MAINTENANCE_INTERVAL_S:
            results.append({"action": key, "command": shown, "decision": "skip",
                            "reason": "ran " + _human_duration(moment - last_ts) + " ago"})
            continue
        if not apply:
            results.append({"action": key, "command": shown, "decision": "run",
                            "reason": "due; would run (rerun with --apply)"})
            continue
        if shutil.which(argv[0]) is None:
            results.append({"action": key, "command": shown, "decision": "skip",
                            "reason": argv[0] + " is not on PATH"})
            continue
        state[key] = moment
        save_state(state_file, state)
        code, tail = _run_cmd(argv)
        if code == 0:
            results.append({"action": key, "command": shown, "decision": "run",
                            "reason": "ok" + (": " + tail if tail else "")})
        else:
            results.append({"action": key, "command": shown, "decision": "fail",
                            "reason": "exit " + str(code) + (": " + tail if tail else "")})
    return results


def _parse_pct(raw: str) -> float | None:
    """Percentage text to a float, or None when it does not parse."""
    try:
        value = float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 100:
        return None
    return value


def cmd_janitor(argv: list[str]) -> int:
    """Dispatch ``worktrees janitor``. Dry-run unless ``--apply`` is given."""
    repo: str | None = None
    apply = False
    json_mode = False
    daily = False
    min_age_raw = "4h"
    artifact_age_raw = "24h"
    pressure_age_raw = "6h"
    pressure_raw = str(DEFAULT_PRESSURE_PCT)
    state_file: str | None = None
    skips: set[str] = set()
    args = list(argv or [])
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--apply":
            apply = True
            index += 1
        elif token == "--json":
            json_mode = True
            index += 1
        elif token == "--daily-maintenance":
            daily = True
            index += 1
        elif token == "--repo" and index + 1 < len(args):
            repo = args[index + 1]
            index += 2
        elif token == "--skip" and index + 1 < len(args):
            skips.add(args[index + 1])
            skips.add(os.path.realpath(args[index + 1]))
            index += 2
        elif token == "--min-age" and index + 1 < len(args):
            min_age_raw = args[index + 1]
            index += 2
        elif token == "--artifact-age" and index + 1 < len(args):
            artifact_age_raw = args[index + 1]
            index += 2
        elif token == "--pressure-age" and index + 1 < len(args):
            pressure_age_raw = args[index + 1]
            index += 2
        elif token == "--pressure-pct" and index + 1 < len(args):
            pressure_raw = args[index + 1]
            index += 2
        elif token == "--state-file" and index + 1 < len(args):
            state_file = args[index + 1]
            index += 2
        else:
            print(JANITOR_USAGE, end="", file=sys.stderr)
            return errors.USAGE
    if repo is None:
        print(
            "worktrees janitor needs --repo <path>. Run: "
            "python3 agent_dash.py worktrees janitor --repo <path>",
            file=sys.stderr,
        )
        return errors.USAGE
    min_age_s = worktrees.parse_duration(min_age_raw)
    artifact_age_s = worktrees.parse_duration(artifact_age_raw)
    pressure_age_s = worktrees.parse_duration(pressure_age_raw)
    pressure_pct = _parse_pct(pressure_raw)
    for label, value in (
        ("--min-age", min_age_s),
        ("--artifact-age", artifact_age_s),
        ("--pressure-age", pressure_age_s),
        ("--pressure-pct", pressure_pct),
    ):
        if value is None:
            print(
                "bad " + label + " " + repr(
                    {"--min-age": min_age_raw, "--artifact-age": artifact_age_raw,
                     "--pressure-age": pressure_age_raw, "--pressure-pct": pressure_raw}[label]
                ) + ". Durations look like 30s, 90m, 3h, 2d; the percentage is 0-100. "
                "Run: python3 agent_dash.py worktrees janitor --repo <path>",
                file=sys.stderr,
            )
            return errors.USAGE
    top = worktrees.toplevel_of(repo)
    if top is None:
        print(
            "repo not found: " + str(repo) + ". Run: "
            "python3 agent_dash.py worktrees janitor --repo <path>",
            file=sys.stderr,
        )
        return errors.NOT_FOUND
    now = time.time()
    dry_run = not apply
    free_pct = disk_free_pct(top)
    pressure = free_pct is not None and free_pct < pressure_pct
    effective_age_s = min(artifact_age_s, pressure_age_s) if pressure else artifact_age_s
    lines: list[str] = []
    if pressure:
        lines.append(
            "disk pressure: %.1f%% free below %.1f%%, artifact age %s -> %s"
            % (free_pct, pressure_pct, _human_duration(artifact_age_s), _human_duration(pressure_age_s))
        )

    _, rows = worktrees.collect(top, min_age_s, skips, now)
    for row in rows:
        if row.get("decision") == "reap":
            row["bytes"] = dir_size_bytes(row["path"]) or 0
    rows = worktrees.apply_reaps(top, rows, dry_run)
    reaped = [row for row in rows if row.get("decision") == "reap" and row.get("bytes")]
    for row in reaped:
        verb = "would reap" if dry_run else "reaped"
        lines.append("%s %s %s (%s)" % (verb, row["path"], _human_bytes(row.get("bytes", 0)), row.get("reason", "")))

    remaining = [
        row["path"]
        for row in rows
        if row.get("decision") != "reap" and os.path.isdir(row.get("path", ""))
    ]
    try:
        busy = worktrees.busy_map(remaining)
    except Exception:
        busy = {path: "process scan could not run here" for path in remaining}
    artifact_rows: list[dict] = []
    for path in remaining:
        if busy.get(path):
            continue
        deadline = time.monotonic() + SCAN_DEADLINE_S
        planned, note = plan_artifacts(path, effective_age_s, now, deadline, skips)
        if note:
            continue
        for row in planned:
            if row["decision"] == "delete" and apply:
                if remove_tree(row["target"]):
                    row["reason"] = row["reason"] + "; deleted"
                else:
                    row["decision"] = "keep"
                    row["reason"] = "delete failed"
            artifact_rows.append(row)
            if row["decision"] == "delete":
                verb = "would delete" if dry_run else "deleted"
                lines.append("%s %s %s (%s)" % (verb, row["target"], _human_bytes(row["bytes"]), row["reason"]))
            elif apply or json_mode:
                lines.append("kept %s (%s)" % (row["target"], row["reason"]))

    reaped_count = sum(1 for row in rows if row.get("decision") == "reap")
    reaped_bytes = sum(int(row.get("bytes") or 0) for row in reaped)
    artifact_count = sum(1 for row in artifact_rows if row["decision"] == "delete")
    artifact_bytes = sum(int(row.get("bytes") or 0) for row in artifact_rows if row["decision"] == "delete")
    artifact_worktrees = len({row["path"] for row in artifact_rows if row["decision"] == "delete"})
    freed_bytes = reaped_bytes + artifact_bytes

    maintenance: list[dict] = []
    if daily:
        state_target = state_file or str(default_state_file())
        maintenance = run_maintenance(state_target, apply, now)
        for row in maintenance:
            lines.append("%s %s (%s)" % (row["decision"], row["command"], row["reason"]))
        if not apply:
            lines.append("maintenance runs are planned only; rerun with --apply")

    payload = {
        "repo": top,
        "apply": apply,
        "dry_run": dry_run,
        "pressure": pressure,
        "free_pct": free_pct,
        "artifact_age_s": effective_age_s,
        "reaped": [row for row in rows if row.get("decision") == "reap"],
        "artifacts": artifact_rows,
        "maintenance": maintenance,
        "counts": {
            "reaped": reaped_count,
            "artifacts": artifact_count,
            "worktrees": artifact_worktrees,
            "freed_bytes": freed_bytes,
        },
    }
    if json_mode:
        errors.emit_json(payload)
        return errors.OK
    answer = (
        "ANSWER: reaped=%d artifacts=%d worktrees=%d freed_bytes=%d freed=%s "
        "pressure=%d artifact_age=%ds apply=%d"
        % (
            reaped_count,
            artifact_count,
            artifact_worktrees,
            freed_bytes,
            _human_bytes(freed_bytes),
            1 if pressure else 0,
            int(effective_age_s),
            1 if apply else 0,
        )
    )
    lines.append(answer)
    print("\n".join(lines))
    return errors.OK
