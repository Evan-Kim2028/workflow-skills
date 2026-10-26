"""Automatic worktree cleanup for workstream lanes (``ws gc``).

Workstream leads create one git worktree per lane and they pile up after
the lane merges. ``ws gc`` removes the ones that are finished. A
worktree belongs to a workstream when its branch matches a branch the
bundle names -- a train's ``branch`` in ``plan.json``, a lane's
``branch`` in ``docs/execution-plan.json`` when present -- or the
``ws<N>/`` / ``train/ws<N>-`` prefix convention (case-insensitive).

A worktree is finished when every task mapped to its branch has status
``prog``, ``done``, ``dropped`` or ``deferred``, OR its branch is merged
into ``origin/main``, OR the branch's pull request is merged. Safety
gates (all required): the main checkout is never touched; a path that
does not match the workstream's slug convention is never touched; dirty
worktrees are kept; branches with commits on no remote are kept unless
merged; worktrees a live worker is using are kept; a worktree is kept
when the live-process check itself cannot run. Ignored files keep a
worktree too, except regenerable ones (``.venv/``, ``node_modules/``,
caches, ``dist/``, ``build/``, ``*.egg-info/``), which are deleted
first. Removal uses ``git worktree remove`` without ``--force``
followed by ``git worktree prune``; the local branch goes only via
``git branch -d`` (never ``-D``), and only when merged. Every removal
and every keep appends one line to ``<bundle>/worktree-gc.log`` right
after its own decision (nothing is logged on a dry run).

Repos to scan come from every repo the bundle names (lane ``repo``
fields in ``docs/execution-plan.json`` resolved under
``~/Documents/projects/``, plus an optional ``repos`` list in
``bundle.json``), plus the main checkouts that own the worktrees found
by the prefix convention under ``~/Documents/projects/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dash import errors

FINISHED_TASK_STATUSES = ("prog", "done", "dropped", "deferred")

REGENERABLE_IGNORED = (
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".pyright/",
    "dist/",
    "build/",
    "*.egg-info/",
)
"""Ignored paths that never keep a worktree; they are deleted first.

Every entry is a directory name (``*.egg-info/`` matches any
``<name>.egg-info`` directory). A match at any depth counts: tool
caches, environments and build outputs are regenerable wherever they
sit. Anything ignored that matches none of these keeps the worktree.
"""

_REGENERABLE_NAMES = frozenset(
    entry[:-1] for entry in REGENERABLE_IGNORED if entry.endswith("/") and "*" not in entry
)

LIVE_CHECK_FAILED = "live-process check did not run; kept"
GC_TRIGGER_STATUSES = ("prog", "done", "dropped")

GIT_TIMEOUT_S = 30
GH_TIMEOUT_S = 15
PS_TIMEOUT_S = 10
FETCH_TIMEOUT_S = 60


def ws_number(ws: str) -> str:
    """Numeric part of a workstream id (``WS18`` -> ``18``). Empty when none."""
    digits = "".join(char for char in str(ws) if char.isdigit())
    return digits


def _slug_pattern(ws: str) -> "re.Pattern[str] | None":
    """Regex matching the ``ws<N>`` slug not followed by another digit.

    ``WS3`` matches ``ws3-foo`` but never ``ws30-foo``: the lookahead
    keeps workstream 3 away from workstream 30's worktrees.
    """
    number = ws_number(ws)
    if not number:
        return None
    return re.compile(r"ws" + re.escape(number) + r"(?![0-9])", re.IGNORECASE)


def branch_prefixes(ws: str) -> tuple[str, str]:
    """Lowercase ``(lane, train)`` branch prefixes for one workstream."""
    number = ws_number(ws).lower()
    return ("ws" + number + "/", "train/ws" + number + "-")


def slug_ok(ws: str, path: str) -> bool:
    """True when a worktree path matches the workstream's slug convention.

    Lane worktrees look like ``lor-wt-ws18-<slug>`` or
    ``silph-wt-ws18-<slug>``: the basename holds ``wt-`` and the
    workstream slug (``ws<N>``, case-insensitive). Anything else --
    including the main checkout -- never qualifies for removal.
    """
    base = os.path.basename(os.path.realpath(path))
    pattern = _slug_pattern(ws)
    if pattern is None:
        return False
    return "wt-" in base.lower() and pattern.search(base) is not None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bundle_branches(ws: str, root: Path | None = None) -> tuple[set[str], dict[str, set[str]], set[str]]:
    """Named branches, branch-to-tasks map, and repo names for one bundle.

    Returns ``(named, branch_tasks, repo_names)`` with every branch in
    lowercase. ``named`` holds train branches from ``plan.json`` plus
    lane and train branches from ``docs/execution-plan.json``.
    ``branch_tasks`` maps each branch to its member task ids.
    ``repo_names`` holds the lane ``repo`` fields (short names like
    ``lake-of-rage``), which :func:`resolve_repos` maps to checkouts.
    """
    from dash import bundles as _bundles

    named: set[str] = set()
    branch_tasks: dict[str, set[str]] = {}
    repo_names: set[str] = set()
    path = _bundles.bundle_dir(ws, root)
    if path is None:
        return named, branch_tasks, repo_names
    plan = _read_json(path / "plan.json")
    if isinstance(plan, dict):
        trains = plan.get("trains")
        if isinstance(trains, list):
            for train in trains:
                if not isinstance(train, dict):
                    continue
                branch = train.get("branch")
                if isinstance(branch, str) and branch.strip():
                    key = branch.strip().lower()
                    named.add(key)
                    members = train.get("members")
                    if isinstance(members, list):
                        tasks = {str(item) for item in members if isinstance(item, str) and item}
                        if tasks:
                            branch_tasks.setdefault(key, set()).update(tasks)
    exec_plan = _read_json(path / "docs" / "execution-plan.json")
    if isinstance(exec_plan, dict):
        for lane in _iter_exec_lanes(exec_plan):
            if not isinstance(lane, dict):
                continue
            branch = lane.get("branch")
            if isinstance(branch, str) and branch.strip():
                key = branch.strip().lower()
                named.add(key)
                tasks = lane.get("tasks")
                if isinstance(tasks, list):
                    ids = {str(item) for item in tasks if isinstance(item, str) and item}
                    if ids:
                        branch_tasks.setdefault(key, set()).update(ids)
            repo = lane.get("repo")
            if isinstance(repo, str) and repo.strip():
                repo_names.add(repo.strip())
        trains = exec_plan.get("trains")
        if isinstance(trains, list):
            for train in trains:
                if not isinstance(train, dict):
                    continue
                branch = train.get("branch")
                if isinstance(branch, str) and branch.strip():
                    key = branch.strip().lower()
                    named.add(key)
                    members = train.get("members")
                    if isinstance(members, list):
                        ids = {str(item) for item in members if isinstance(item, str) and item}
                        if ids:
                            branch_tasks.setdefault(key, set()).update(ids)
    return named, branch_tasks, repo_names


def _iter_exec_lanes(exec_plan: dict) -> list[dict]:
    """Lane dicts from every wave of an execution plan."""
    lanes: list[dict] = []
    waves = exec_plan.get("waves")
    if isinstance(waves, list):
        for wave in waves:
            if not isinstance(wave, dict):
                continue
            items = wave.get("lanes")
            if isinstance(items, list):
                lanes.extend(item for item in items if isinstance(item, dict))
    return lanes


def belongs_to_ws(ws: str, branch: str | None, named: set[str]) -> bool:
    """True when a branch belongs to the workstream: named or by prefix."""
    if not branch:
        return False
    lowered = branch.strip().lower()
    if lowered in named:
        return True
    lane_prefix, train_prefix = branch_prefixes(ws)
    return lowered.startswith(lane_prefix) or lowered.startswith(train_prefix)


def _is_git_repo(path: str) -> bool:
    """True when ``path`` is inside a git work tree or repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _projects_root() -> Path:
    return Path.home() / "Documents" / "projects"


def _main_of_worktree(path: str) -> str | None:
    """Main checkout owning ``path``, via its own worktree list."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "worktree", "list", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            first = os.path.realpath(line[len("worktree "):].strip())
            return first or None
    return None


def resolve_repos(
    ws: str,
    bundle_dir: Path | None,
    repo_names: set[str],
) -> list[str]:
    """Main checkouts to scan for one workstream, deduplicated and sorted.

    Union of: the optional ``repos`` list in ``bundle.json`` (absolute
    paths, ``~/Documents/projects/`` names, or bundle-relative paths),
    lane ``repo`` names resolved under ``~/Documents/projects/``, and
    the default list -- the main checkouts owning the worktrees found by
    the prefix convention under ``~/Documents/projects/``.
    """
    found: dict[str, str] = {}

    def _add(candidate: str) -> None:
        top = _toplevel(candidate)
        if top is not None:
            found.setdefault(top, top)

    if bundle_dir is not None:
        meta = _read_json(bundle_dir / "bundle.json")
        if isinstance(meta, dict):
            repos = meta.get("repos")
            if isinstance(repos, list):
                for entry in repos:
                    if not isinstance(entry, str) or not entry.strip():
                        continue
                    text = entry.strip()
                    if os.path.isabs(text):
                        _add(text)
                        continue
                    projects_hit = _projects_root() / text
                    if _is_git_repo(str(projects_hit)):
                        _add(str(projects_hit))
                        continue
                    bundle_hit = bundle_dir / text
                    if _is_git_repo(str(bundle_hit)):
                        _add(str(bundle_hit))
    for name in repo_names:
        if "/" in name or name.lower().startswith("ws"):
            # Execution plans also name non-repo lanes ("WS18 bundle").
            continue
        candidate = _projects_root() / name
        if _is_git_repo(str(candidate)):
            _add(str(candidate))
    projects = _projects_root()
    try:
        entries = sorted(os.listdir(projects))
    except OSError:
        entries = []
    pattern = _slug_pattern(ws)
    for entry in entries:
        if "wt-" not in entry.lower():
            continue
        if pattern is None or pattern.search(entry) is None:
            continue
        full = str(projects / entry)
        if not os.path.isdir(full):
            continue
        main = _main_of_worktree(full)
        if main is not None and _is_git_repo(main):
            found.setdefault(os.path.realpath(main), os.path.realpath(main))
    return sorted(found.values())


def _toplevel(repo: str) -> str | None:
    """Absolute main-checkout path of ``repo``, or None outside git."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = (proc.stdout or "").strip()
    return os.path.realpath(top) if top else None


def _run_git(repo: str, *args: str, timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str] | None:
    """One git call inside ``repo``. None when it cannot run."""
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def fetch_repo(repo: str) -> None:
    """Best-effort ``git fetch origin``; failures are ignored."""
    _run_git(repo, "fetch", "origin", "--quiet", timeout=FETCH_TIMEOUT_S)


def task_statuses(ws: str, root: Path | None = None) -> dict[str, str]:
    """Task id to status from the bundle's ``plan.json`` dag."""
    from dash import bundles as _bundles

    path = _bundles.bundle_dir(ws, root)
    if path is None:
        return {}
    plan = _read_json(path / "plan.json")
    statuses: dict[str, str] = {}
    if not isinstance(plan, dict):
        return statuses
    dag = plan.get("dag")
    if not isinstance(dag, dict):
        return statuses
    groups = dag.get("groups")
    if not isinstance(groups, list):
        return statuses
    for group in groups:
        if not isinstance(group, dict):
            continue
        tasks = group.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = task.get("id")
            if isinstance(tid, str) and tid:
                status = task.get("status")
                statuses[tid] = status if isinstance(status, str) else "todo"
    return statuses


def merged_by_git(repo: str, branch: str) -> tuple[bool, str]:
    """True when ``branch`` is merged into ``origin/main``.

    Either proof counts: the tip is an ancestor of ``origin/main``, or
    ``git branch -r --contains`` names ``origin/main``. Runs after
    :func:`fetch_repo`.
    """
    tip_proc = _run_git(repo, "rev-parse", "--verify", branch)
    if tip_proc is None or tip_proc.returncode != 0:
        return False, "branch is gone from the repo"
    tip = (tip_proc.stdout or "").strip()
    if not tip:
        return False, "branch is gone from the repo"
    ancestor = _run_git(repo, "merge-base", "--is-ancestor", tip, "origin/main")
    if ancestor is not None and ancestor.returncode == 0:
        return True, "merged into origin/main"
    contains = _run_git(repo, "branch", "-r", "--contains", branch)
    if contains is not None and contains.returncode == 0:
        remotes = (contains.stdout or "").split()
        if any("origin/main" in item for item in remotes):
            return True, "merged into origin/main"
    return False, "tip is not an ancestor of origin/main (unmerged)"


def pr_merged(repo: str, branch: str) -> bool:
    """True when ``gh`` reports a merged pull request for ``branch``.

    Silently False when ``gh`` is missing, the lookup fails, or the
    pull request is not merged.
    """
    if shutil.which("gh") is None:
        return False
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "state"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            cwd=repo,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        return False
    if isinstance(payload, dict):
        return payload.get("state") == "MERGED"
    if isinstance(payload, list):
        return any(isinstance(item, dict) and item.get("state") == "MERGED" for item in payload)
    return False


def has_unpushed(repo: str, branch: str) -> bool:
    """True when the branch holds commits on no remote. Doubt reads True."""
    proc = _run_git(repo, "rev-list", "--count", branch, "--not", "--remotes")
    if proc is None or proc.returncode != 0:
        return True
    try:
        return int((proc.stdout or "").strip()) > 0
    except ValueError:
        return True


def _ignored_is_regenerable(rel: str) -> bool:
    """True when an ignored path is a regenerable cache, env, or build output."""
    parts = [part for part in rel.replace("\\", "/").split("/") if part not in ("", ".")]
    for part in parts:
        if part in _REGENERABLE_NAMES or part.endswith(".egg-info"):
            return True
    return False


def _ignored_paths(path: str) -> list[str] | None:
    """Ignored paths in the worktree, or None when the scan cannot run."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "status", "--porcelain=v1", "--ignored=matching", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    found: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if line.startswith("!!"):
            rel = line[2:].strip()
            if rel:
                found.append(rel)
    return found


def _remove_regenerable(path: str, ignored: list[str]) -> None:
    """Delete allowlisted ignored dirs so plain ``git worktree remove`` succeeds.

    Only the outermost allowlisted directory of each ignored path goes;
    anything outside the worktree is never touched. Never raises.
    """
    base = os.path.realpath(path)
    targets: dict[str, None] = {}
    for rel in ignored:
        parts = [part for part in rel.replace("\\", "/").split("/") if part not in ("", ".")]
        for index, part in enumerate(parts):
            if part in _REGENERABLE_NAMES or part.endswith(".egg-info"):
                targets[os.path.join(base, *parts[: index + 1])] = None
                break
    for target in targets:
        try:
            real = os.path.realpath(target)
            if real != base and real.startswith(base + os.sep) and not os.path.islink(real):
                shutil.rmtree(real, ignore_errors=True)
        except OSError:
            continue


def is_dirty(path: str) -> tuple[bool, str]:
    """(dirty, reason) from ``git status`` including untracked and ignored files.

    Ignored files keep the worktree unless every one of them is a
    regenerable cache, environment, or build output (see
    :data:`REGENERABLE_IGNORED`).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", path, "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, "dirty worktree: git status did not finish"
    if proc.returncode != 0:
        return True, "dirty worktree: git status failed"
    if (proc.stdout or "").strip():
        return True, "dirty, kept"
    ignored = _ignored_paths(path)
    if ignored is None:
        return True, "dirty worktree: ignored-file scan did not finish"
    foreign = [rel for rel in ignored if not _ignored_is_regenerable(rel)]
    if foreign:
        return True, "dirty, kept: ignored files present (" + foreign[0] + ")"
    return False, ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def snapshot_processes() -> dict | None:
    """One process snapshot for the whole gc run: argv lines plus cwd map.

    A single pid pass, a single cwd pass, and a single command pass
    serve every worktree, so a busy laptop pays the scan once instead
    of once per worktree. Returns None when the scan cannot run, so
    callers fail closed and keep the worktree. Never raises.
    """
    from dash import worktrees as _worktrees

    try:
        pids = _worktrees._proc_pids()
    except Exception:
        return None
    if not pids:
        return None
    try:
        cwds = _worktrees._cwd_table(pids)
    except Exception:
        cwds = None
    try:
        commands = _worktrees._command_table(pids)
    except Exception:
        commands = {}
    if not cwds and not commands:
        return None
    argv_lines = [str(pid) + " " + cmd for pid, cmd in sorted(commands.items()) if cmd]
    return {"argv": argv_lines, "cwds": dict(cwds or {})}


def _pid_of_argv_line(line: str) -> tuple[int | None, str]:
    """(pid, command) from one ``ps -axo pid,command`` line."""
    head, _, tail = line.strip().partition(" ")
    try:
        return int(head.strip()), tail.strip()
    except ValueError:
        return None, ""


def live_user_of(path: str, snap: dict | None = None) -> str:
    """Reason when a live process uses ``path``, else an empty string.

    Any running process whose cwd or argv contains the worktree path
    blocks the gc. Argv is scanned with ``pgrep -fl <path>`` plus the
    shared snapshot's command table; cwd comes from the shared snapshot
    (``/proc`` when present, else one ``lsof`` pass from the reaper's
    guards in :mod:`dash.worktrees`). Pass the run's
    :func:`snapshot_processes` result as ``snap`` to reuse it; without
    one, a fresh snapshot is taken. When the check itself cannot run --
    ``pgrep``/``ps``/``lsof`` missing, timed out, or ``ps`` exiting
    non-zero (``pgrep`` exit 1, no match, is normal) -- the worktree is
    kept with :data:`LIVE_CHECK_FAILED`. Never raises.
    """
    mine = os.getpid()
    target = os.path.realpath(path)
    needles = {target, path}
    try:
        proc = subprocess.run(
            ["pgrep", "-fl", target],
            check=False,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return LIVE_CHECK_FAILED
    if proc.returncode == 0:
        for line in (proc.stdout or "").splitlines():
            pid, tail = _pid_of_argv_line(line)
            if pid is None or pid == mine or not _pid_alive(pid):
                continue
            if any(needle in line for needle in needles):
                return f"live worker holds the worktree (pid {pid}): {tail[:120]}"
    elif proc.returncode != 1:
        return LIVE_CHECK_FAILED
    try:
        if snap is None:
            snap = snapshot_processes()
            if snap is None:
                return LIVE_CHECK_FAILED
        elif snap.get("failed"):
            return LIVE_CHECK_FAILED
        argv_lines: list[str] = list(snap.get("argv") or [])
        cwds: dict = dict(snap.get("cwds") or {})
    except Exception:
        return LIVE_CHECK_FAILED
    for line in argv_lines:
        pid, tail = _pid_of_argv_line(line)
        if pid is None or pid == mine or not tail:
            continue
        if any(needle in tail for needle in needles):
            if not _pid_alive(pid):
                continue
            return f"live worker holds the worktree (pid {pid}): {tail[:120]}"
    for pid, cwd in cwds.items():
        if pid == mine:
            continue
        if cwd and (cwd == target or cwd.startswith(target + os.sep)):
            return f"live worker holds the worktree as its cwd (pid {pid})"
    return ""


def decide(
    ws: str,
    repo_top: str,
    entry: dict,
    branch_tasks: dict[str, set[str]],
    statuses: dict[str, str],
    merged_cache: dict[str, tuple[bool, str]],
    pr_cache: dict[str, bool],
    snap: dict | None = None,
) -> dict:
    """One decision row for a worktree entry. Keeps on any doubt."""
    path = entry.get("path", "")
    branch = entry.get("branch")
    row = {"repo": repo_top, "path": path, "branch": branch or "-", "action": "keep", "reason": ""}
    if not branch:
        if entry.get("bare"):
            row["reason"] = "bare entry holds no checkout"
        elif entry.get("detached"):
            row["reason"] = "detached HEAD has no branch to verify"
        else:
            row["reason"] = "no branch to verify"
        return row
    if path == repo_top:
        row["reason"] = "is the main checkout"
        return row
    if not slug_ok(ws, path):
        row["reason"] = "path does not match the " + ws + " slug convention; kept"
        return row
    dirty, grime = is_dirty(path)
    if dirty:
        row["reason"] = grime
        return row
    busy = live_user_of(path, snap)
    if busy:
        row["reason"] = busy if busy == LIVE_CHECK_FAILED else busy + "; kept"
        return row
    key = branch.strip().lower()
    if key not in merged_cache:
        merged_cache[key] = merged_by_git(repo_top, branch)
    merged, merge_why = merged_cache[key]
    if not merged and has_unpushed(repo_top, branch):
        row["reason"] = "unpushed, kept"
        return row
    mapped = sorted(branch_tasks.get(key, set()))
    if mapped:
        waiting = [tid for tid in mapped if statuses.get(tid, "todo") not in FINISHED_TASK_STATUSES]
        tasks_done = not waiting
    else:
        waiting = []
        tasks_done = False
    if key not in pr_cache:
        pr_cache[key] = pr_merged(repo_top, branch) if not merged else False
    pull_done = pr_cache[key]
    if tasks_done:
        why = "tasks finished (" + ",".join(mapped) + ")"
    elif merged:
        why = merge_why
    elif pull_done:
        why = "pull request merged for " + branch
    else:
        if mapped:
            row["reason"] = "waiting on tasks (" + ",".join(waiting) + "); kept"
        else:
            row["reason"] = merge_why + "; kept"
        return row
    row["action"] = "remove"
    row["reason"] = why + ("; merged" if merged and not tasks_done else "") + "; clean; idle"
    return row


def collect(
    ws: str, root: Path | None = None, fetch: bool = True
) -> tuple[Path | None, list[str], list[dict]]:
    """Bundle dir, scanned repos, and one decision row per worktree.

    Only worktrees whose branch belongs to the workstream produce rows;
    unrelated worktrees are skipped silently and never logged. Pass
    ``fetch=False`` for read-only callers (``--dry-run``, ``ws render``):
    no ``git fetch`` runs, so nothing touches the network.
    """
    from dash import bundles as _bundles
    from dash import worktrees as _worktrees

    bundle_dir = _bundles.bundle_dir(ws, root)
    if bundle_dir is None:
        return None, [], []
    named, branch_tasks, repo_names = bundle_branches(ws, root)
    repos = resolve_repos(ws, bundle_dir, repo_names)
    statuses = task_statuses(ws, root)
    snap = snapshot_processes()
    if snap is None:
        snap = {"failed": True, "argv": [], "cwds": {}}
    rows: list[dict] = []
    for repo in repos:
        if fetch:
            fetch_repo(repo)
        top = _toplevel(repo)
        if top is None:
            continue
        merged_cache: dict[str, tuple[bool, str]] = {}
        pr_cache: dict[str, bool] = {}
        for entry in _worktrees.list_worktrees(top):
            branch = entry.get("branch")
            if not belongs_to_ws(ws, branch, named):
                continue
            rows.append(decide(ws, top, entry, branch_tasks, statuses, merged_cache, pr_cache, snap))
    rows.sort(key=lambda item: item.get("path", ""))
    return bundle_dir, repos, rows


def remove_worktree(repo_top: str, path: str) -> tuple[bool, str]:
    """``git worktree remove`` without force. Reports ok plus detail.

    Allowlisted ignored directories are deleted first: plain
    ``git worktree remove`` sometimes refuses untracked-but-ignored
    content, and these paths are regenerable.
    """
    ignored = _ignored_paths(path)
    if ignored:
        allowlisted = [rel for rel in ignored if _ignored_is_regenerable(rel)]
        if allowlisted:
            _remove_regenerable(path, allowlisted)
    proc = _run_git(repo_top, "worktree", "remove", path)
    if proc is None:
        return False, "git worktree remove did not finish"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return False, "git worktree remove refused: " + (detail[0] if detail else "unknown error")
    _run_git(repo_top, "worktree", "prune")
    return True, "worktree removed"


def delete_branch(repo_top: str, branch: str) -> tuple[bool, str]:
    """``git branch -d`` without force. Reports ok plus detail."""
    proc = _run_git(repo_top, "branch", "-d", branch)
    if proc is None:
        return False, "git branch -d did not finish"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return False, "git branch -d refused: " + (detail[0] if detail else "unknown error")
    return True, "branch deleted"


def append_log(bundle_dir: Path, rows: list[dict], now: float | None = None) -> None:
    """Append one line per row: ISO time, path, branch, action, reason."""
    stamp = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).isoformat()
    lines = []
    for row in rows:
        action = str(row.get("action", "keep"))
        lines.append(
            "\t".join(
                [
                    stamp,
                    str(row.get("path", "")),
                    str(row.get("branch", "")),
                    action,
                    str(row.get("reason", "")),
                ]
            )
        )
    if not lines:
        return
    try:
        with open(bundle_dir / "worktree-gc.log", "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except OSError as exc:
        print(f"worktree gc log not recorded: {exc}", file=sys.stderr)


def apply_rows(rows: list[dict], dry_run: bool, bundle_dir: Path | None = None) -> list[dict]:
    """Remove approved worktrees (and merged branches) unless dry-run.

    Every non-dry-run row is appended to ``<bundle>/worktree-gc.log``
    right after its own decision; dry runs log nothing.
    """
    if dry_run:
        for row in rows:
            if row.get("action") == "remove":
                row["reason"] = row.get("reason", "") + " (would remove; rerun without --dry-run)"
        return rows
    by_repo: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("action") == "remove":
            by_repo.setdefault(str(row.get("repo", "")), []).append(row)
        elif bundle_dir is not None:
            append_log(bundle_dir, [row])
    for repo_top, repo_rows in by_repo.items():
        for row in repo_rows:
            ok, detail = remove_worktree(repo_top, str(row["path"]))
            if not ok:
                row["action"] = "keep"
                row["reason"] = detail
            else:
                branch = str(row.get("branch", ""))
                merged, _why = merged_by_git(repo_top, branch)
                if merged:
                    good, gone = delete_branch(repo_top, branch)
                    if good:
                        row["reason"] = "removed worktree and deleted branch"
                    else:
                        row["reason"] = "removed worktree; " + gone
                else:
                    row["reason"] = str(row.get("reason", "")) + "; removed worktree; branch kept (unmerged)"
            if bundle_dir is not None:
                append_log(bundle_dir, [row])
    return rows


def summarize(rows: list[dict]) -> tuple[int, int]:
    """``(live, removable)`` counts for a gc run."""
    live = len(rows)
    removable = sum(1 for row in rows if row.get("action") == "remove")
    return live, removable


def format_table(rows: list[dict]) -> str:
    """Human table with path, branch, action, and reason columns."""
    head = ("path", "branch", "action", "reason")
    widths = [len(text) for text in head]
    body: list[tuple[str, str, str, str]] = []
    for row in rows:
        cells = (
            str(row.get("path", "")),
            str(row.get("branch", "")),
            str(row.get("action", "")),
            str(row.get("reason", "")),
        )
        body.append(cells)
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(head[index].ljust(widths[index]) for index in range(4))]
    for cells in body:
        lines.append("  ".join(cells[index].ljust(widths[index]) for index in range(4)))
    return "\n".join(lines)


def cmd_gc(ws: str, dry_run: bool = False, json_mode: bool = False, root: Path | None = None) -> int:
    """Run the worktree gc for one workstream. Returns an exit code."""
    from dash import bundles as _bundles

    if _bundles.bundle_dir(ws, root) is None:
        message = errors.bundle_not_found(ws)
        if json_mode:
            errors.emit_json({"ws": ws, "ok": False, "error": message})
        else:
            print(message, flush=True)
        return errors.NOT_FOUND
    bundle_dir, repos, rows = collect(ws, root, fetch=not dry_run)
    if bundle_dir is None:
        message = errors.bundle_not_found(ws)
        if json_mode:
            errors.emit_json({"ws": ws, "ok": False, "error": message})
        else:
            print(message, flush=True)
        return errors.NOT_FOUND
    rows = apply_rows(rows, dry_run, bundle_dir)
    live, removable = summarize(rows)
    if json_mode:
        errors.emit_json(
            {
                "ws": ws,
                "dry_run": dry_run,
                "repos": repos,
                "live": live,
                "removable": removable,
                "worktrees": rows,
            }
        )
        return errors.OK
    if not rows:
        print(f"(no {ws} worktrees under " + (", ".join(repos) if repos else "any scanned repo") + ")")
    else:
        errors.emit_human(format_table(rows), json_mode)
    print(f"worktrees: {live} live, {removable} removable")
    return errors.OK


def gc_after_task(ws: str, root: Path | None = None) -> tuple[int, int]:
    """Best-effort apply-gc for the ``ws task`` hook. Returns ``(live, removable)``.

    Raises on failure; the caller prints the warning and keeps the
    status change.
    """
    from dash import bundles as _bundles

    bundle_dir = _bundles.bundle_dir(ws, root)
    if bundle_dir is None:
        raise ValueError(errors.bundle_not_found(ws))
    _dir, _repos, rows = collect(ws, root)
    rows = apply_rows(rows, False, bundle_dir)
    return summarize(rows)


def dry_run_summary(ws: str, root: Path | None = None) -> tuple[int, int]:
    """``(live, removable)`` counts without removing, fetching, or logging. Raises on failure."""
    _dir, _repos, rows = collect(ws, root, fetch=False)
    return summarize(rows)
