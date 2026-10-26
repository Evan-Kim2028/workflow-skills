"""Safe git worktree reaper for agent lanes.

A worktree is listed for removal only when every guard holds at once:
clean status, branch merged into the main ref with proof it contributed,
no live process sitting in it or naming it, older than the minimum age,
and absent from the skip list. Dry-run by default; nothing is removed
without ``--apply``.

Merged means the branch tip is an ancestor of the main ref and the
tip sits off the main ref first-parent line, so a worktree created fresh
from the main ref with no commits of its own is kept even though its tip
is trivially an ancestor. A merged pull request for the branch counts as
merged too, which covers squash merges whose tips never become
ancestors. Fast-forward merges read as fresh and are kept; that is the
safe direction, and ``git branch -d`` would refuse nothing here because
removal is skipped first.

Removal uses ``git worktree remove`` without ``--force`` and
``git branch -d`` without ``-D``, so git itself refuses anything
unexpected. Process checks read per-process cwd entries with bounded
timeouts and a capped pid list, falling back across Linux procfs and
the ``lsof`` tool found on other systems.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dash import errors

WORKTREES_USAGE = """agent-dash worktrees reaper

  python3 agent_dash.py worktrees reap --repo <path> [--apply] [--skip <path-or-branch>] [--min-age <dur>] [--json]

Dry-run by default: prints what would go, removes nothing. Pass --apply
to remove worktrees and delete their branches. Every guard must hold:
clean status, branch merged into origin/main with a real contribution
(or a merged pull request), no process cwd or command line points at
it, older than --min-age (default 3h, the worker timeout), and not on
the skip list. Durations look like 30s, 90m, 3h, 2d, or bare seconds.
"""

MAIN_REF = "origin/main"
DEFAULT_MIN_AGE_S = 3.0 * 3600.0
GIT_TIMEOUT_S = 30
GH_TIMEOUT_S = 15
PS_TIMEOUT_S = 10
MAX_PIDS = 4096

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdSMHD]?)\s*$")
_DURATION_SCALE = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(raw: str) -> float | None:
    """Duration text to seconds, or None when it does not parse."""
    match = _DURATION.match(raw or "")
    if not match:
        return None
    scale = _DURATION_SCALE.get(match.group(2).lower(), None)
    if scale is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value < 0:
        return None
    return value * scale


def _run_git(repo: str | Path, *args: str, timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run one git command inside ``repo``. Never raises."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def toplevel_of(repo: str | Path) -> str | None:
    """Absolute main-checkout path of ``repo``, or None outside git."""
    try:
        proc = _run_git(repo, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = (proc.stdout or "").strip()
    return os.path.realpath(top) if top else None


def list_worktrees(repo: str | Path) -> list[dict]:
    """Porcelain worktree list as dicts with path, branch, and flags."""
    try:
        proc = _run_git(repo, "worktree", "list", "--porcelain")
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    found: list[dict] = []
    current: dict | None = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            if current is not None:
                found.append(current)
            current = {
                "path": os.path.realpath(line[len("worktree "):].strip()),
                "branch": None,
                "bare": False,
                "detached": False,
            }
        elif line == "bare":
            if current is not None:
                current["bare"] = True
        elif line == "detached":
            if current is not None:
                current["detached"] = True
        elif line.startswith("branch "):
            if current is not None:
                ref = line[len("branch "):].strip()
                if ref.startswith("refs/heads/"):
                    ref = ref[len("refs/heads/"):]
                current["branch"] = ref
    if current is not None:
        found.append(current)
    return found


def worktree_age_s(path: str, now: float | None = None) -> float | None:
    """Age of the worktree directory in seconds, or None when unknown."""
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    moment = time.time() if now is None else now
    return moment - stamp


def is_clean(path: str) -> tuple[bool, str]:
    """True with an empty note when the worktree has no changes at all."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "git status did not finish"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return False, "git status failed: " + (detail[0] if detail else "unknown error")
    if (proc.stdout or "").strip():
        return False, "dirty worktree with uncommitted changes"
    return True, ""


def branch_tip(repo: str | Path, branch: str) -> str | None:
    """Branch tip sha, or None when the branch is gone."""
    try:
        proc = _run_git(repo, "rev-parse", "--verify", branch)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    tip = (proc.stdout or "").strip()
    return tip or None


def is_ancestor(repo: str | Path, tip: str, ref: str = MAIN_REF) -> bool:
    """True when ``tip`` is an ancestor of ``ref``. Errors read as False."""
    try:
        proc = _run_git(repo, "merge-base", "--is-ancestor", tip, ref)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def first_parent_set(repo: str | Path, ref: str = MAIN_REF) -> set[str] | None:
    """Shas on the main ref first-parent line, or None when it cannot resolve."""
    try:
        proc = _run_git(repo, "rev-list", "--first-parent", ref)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return set((proc.stdout or "").split())


def gh_pr_merged(repo: str | Path, branch: str) -> bool:
    """True when ``gh`` reports a merged pull request for ``branch``."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--head", branch, "--json", "number", "--limit", "1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            cwd=str(repo),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        payload = json.loads(proc.stdout or "[]")
    except ValueError:
        return False
    return isinstance(payload, list) and len(payload) > 0


def merged_state(repo: str | Path, branch: str) -> tuple[bool, str]:
    """Merged verdict plus the human reason for the decision table."""
    tip = branch_tip(repo, branch)
    if tip is None:
        return False, "branch is gone from the repo"
    ancestor = is_ancestor(repo, tip)
    if ancestor:
        line = first_parent_set(repo)
        if line is not None and tip not in line:
            return True, "merged into " + MAIN_REF + " with contributions"
        if gh_pr_merged(repo, branch):
            return True, "pull request merged for " + branch
        if line is None:
            return False, "contribution check did not finish"
        return False, "no commits beyond the fork point (fresh from main)"
    if gh_pr_merged(repo, branch):
        return True, "pull request merged for " + branch
    return False, "tip is not an ancestor of " + MAIN_REF + " (unmerged)"


def _proc_pids() -> list[int]:
    """Bounded pid list from procfs, else one ps call. Never raises."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        entries = []
    pids = [int(name) for name in entries if name.isdigit()]
    if pids:
        return pids[:MAX_PIDS]
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[int] = []
    for token in (proc.stdout or "").split():
        try:
            out.append(int(token))
        except ValueError:
            continue
        if len(out) >= MAX_PIDS:
            break
    return out


def _has_procfs() -> bool:
    """True when per-process procfs entries are readable here."""
    try:
        return os.path.isdir("/proc/1")
    except OSError:
        return False


def _cwd_table(pids: list[int]) -> dict[int, str] | None:
    """Pid to cwd map. None when no source answers. Never raises."""
    if _has_procfs():
        table: dict[int, str] = {}
        for pid in pids:
            try:
                table[pid] = os.path.realpath(os.readlink("/proc/" + str(pid) + "/cwd"))
            except OSError:
                continue
        return table
    try:
        proc = subprocess.run(
            ["lsof", "-d", "cwd", "-n", "-Fpn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    table = {}
    pid = -1
    for line in (proc.stdout or "").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = -1
        elif line.startswith("n") and pid > 0:
            table[pid] = os.path.realpath(line[1:])
    return table


def _command_table(pids: list[int]) -> dict[int, str] | None:
    """Pid to command line map. None when the source fails; never raises."""
    if _has_procfs():
        table = {}
        for pid in pids:
            try:
                with open("/proc/" + str(pid) + "/cmdline", "rb") as handle:
                    raw = handle.read(65536)
            except OSError:
                continue
            table[pid] = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        return table
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,command"],
            check=False,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    table = {}
    for line in (proc.stdout or "").splitlines()[1:]:
        head, _, tail = line.strip().partition(" ")
        try:
            pid = int(head.strip())
        except ValueError:
            continue
        if tail.strip():
            table[pid] = tail.strip()
    return table


def busy_map(paths: list[str]) -> dict[str, str]:
    """Worktree paths holding a live process, each mapped to its reason."""
    targets = {os.path.realpath(path): path for path in paths}
    mine = os.getpid()
    pids = [pid for pid in _proc_pids() if pid != mine]
    try:
        cwds = _cwd_table(pids)
    except Exception:
        cwds = None
    try:
        commands = _command_table(pids)
    except Exception:
        commands = None
    if cwds is None or commands is None:
        return {path: "process scan could not run here" for path in paths}
    busy: dict[str, str] = {}
    for pid in pids:
        cwd = (cwds or {}).get(pid)
        if cwd:
            for target, shown in targets.items():
                if shown in busy:
                    continue
                if cwd == target or cwd.startswith(target + os.sep):
                    busy[shown] = "process holds the worktree as its cwd"
        command = commands.get(pid, "")
        if command:
            for target, shown in targets.items():
                if shown in busy:
                    continue
                if target in command or shown in command:
                    busy[shown] = "worktree path is on a process command line"
        if len(busy) == len(targets):
            break
    return busy


def worktree_in_use(path: str) -> tuple[bool, str]:
    """True when a live process sits in the worktree or names it."""
    found = busy_map([path])
    reason = found.get(path, "")
    if reason.startswith("process holds") or reason.startswith("worktree path"):
        return True, reason
    if reason:
        return False, reason
    return False, ""


def main_branch_now(repo: str | Path) -> str | None:
    """Branch checked out in the main checkout, or None when unclear."""
    try:
        proc = _run_git(repo, "symbolic-ref", "--short", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    name = (proc.stdout or "").strip()
    return name or None


def decide(
    repo: str | Path,
    entry: dict,
    top: str,
    main_now: str | None,
    min_age_s: float,
    skips: set[str],
    now: float,
    busy_reason: str = "",
) -> dict:
    """One decision row for a worktree entry. Keeps on any doubt."""
    path = entry.get("path", "")
    branch = entry.get("branch")
    row = {"path": path, "branch": branch or "-", "decision": "keep", "reason": ""}
    if not branch:
        if entry.get("bare"):
            row["reason"] = "bare entry holds no checkout"
        elif entry.get("detached"):
            row["reason"] = "detached HEAD has no branch to verify"
        else:
            row["reason"] = "no branch to verify"
        return row
    if path == top:
        row["reason"] = "is the main checkout"
        return row
    if branch in skips or path in skips:
        row["reason"] = "on the skip list"
        return row
    if main_now is not None and branch == main_now:
        row["reason"] = "branch is checked out in the main checkout"
        return row
    clean, grime = is_clean(path)
    if not clean:
        row["reason"] = grime
        return row
    if busy_reason.startswith("process holds") or busy_reason.startswith("worktree path"):
        row["reason"] = busy_reason
        return row
    if busy_reason:
        row["reason"] = busy_reason
        return row
    age = worktree_age_s(path, now)
    if age is None:
        row["reason"] = "worktree age is unknown"
        return row
    if age < min_age_s:
        row["reason"] = "younger than the minimum age"
        return row
    merged, why = merged_state(repo, branch)
    if not merged:
        row["reason"] = why
        return row
    row["decision"] = "reap"
    row["reason"] = why + "; clean; idle; old enough"
    return row


def remove_worktree(repo: str | Path, path: str) -> tuple[bool, str]:
    """Remove one worktree without force. Reports ok plus detail."""
    try:
        proc = _run_git(repo, "worktree", "remove", path)
    except (OSError, subprocess.TimeoutExpired):
        return False, "git worktree remove did not finish"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return False, "git worktree remove refused: " + (detail[0] if detail else "unknown error")
    return True, "worktree removed"


def delete_branch(repo: str | Path, branch: str) -> tuple[bool, str]:
    """Delete one branch without force. Reports ok plus detail."""
    try:
        proc = _run_git(repo, "branch", "-d", branch)
    except (OSError, subprocess.TimeoutExpired):
        return False, "git branch -d did not finish"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return False, "git branch -d refused: " + (detail[0] if detail else "unknown error")
    return True, "branch deleted"


def collect(repo: str | Path, min_age_s: float, skips: set[str], now: float) -> tuple[str | None, list[dict]]:
    """Toplevel path plus one decision row per worktree. Empty on no git."""
    top = toplevel_of(repo)
    if top is None:
        return None, []
    main_now = main_branch_now(top)
    entries = [entry for entry in list_worktrees(top) if entry.get("path") != top and not entry.get("bare")]
    try:
        busy = busy_map([str(entry.get("path", "")) for entry in entries])
    except Exception:
        busy = {}
    rows: list[dict] = []
    for entry in entries:
        path = str(entry.get("path", ""))
        rows.append(decide(top, entry, top, main_now, min_age_s, skips, now, busy.get(path, "")))
    rows.sort(key=lambda item: item.get("path", ""))
    return top, rows


def apply_reaps(top: str, rows: list[dict], dry_run: bool) -> list[dict]:
    """Remove approved worktrees and delete their branches unless dry-run."""
    for row in rows:
        if row.get("decision") != "reap":
            continue
        if dry_run:
            row["reason"] = row.get("reason", "") + " (would remove; rerun with --apply)"
            continue
        ok, detail = remove_worktree(top, row["path"])
        if not ok:
            row["decision"] = "keep"
            row["reason"] = detail
            continue
        branch = row.get("branch", "")
        good, gone = delete_branch(top, branch)
        if good:
            row["reason"] = "removed worktree and deleted branch"
        else:
            row["reason"] = "removed worktree; " + gone
    return rows


def format_table(rows: list[dict]) -> str:
    """Human table with path, branch, decision, and reason columns."""
    head = ("path", "branch", "decision", "reason")
    widths = [len(text) for text in head]
    body: list[tuple[str, str, str, str]] = []
    for row in rows:
        cells = (
            str(row.get("path", "")),
            str(row.get("branch", "")),
            str(row.get("decision", "")),
            str(row.get("reason", "")),
        )
        body.append(cells)
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(head[index].ljust(widths[index]) for index in range(4))]
    for cells in body:
        lines.append("  ".join(cells[index].ljust(widths[index]) for index in range(4)))
    return "\n".join(lines)


def cmd_worktrees(argv: list[str]) -> int:
    """Dispatch ``worktrees reap``. Dry-run unless ``--apply`` is given."""
    args = list(argv or [])
    if not args or args[0] in ("-h", "--help"):
        print(WORKTREES_USAGE, end="")
        return 0 if args and args[0] in ("-h", "--help") else errors.USAGE
    if args[0] != "reap":
        print(WORKTREES_USAGE, end="", file=sys.stderr)
        return errors.USAGE
    repo: str | None = None
    apply = False
    json_mode = False
    min_age_raw = "3h"
    skips: set[str] = set()
    index = 1
    while index < len(args):
        token = args[index]
        if token == "--apply":
            apply = True
            index += 1
        elif token == "--json":
            json_mode = True
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
        else:
            print(WORKTREES_USAGE, end="", file=sys.stderr)
            return errors.USAGE
    if repo is None:
        print(
            "worktrees reap needs --repo <path>. Run: python3 agent_dash.py worktrees reap --repo <path>",
            file=sys.stderr,
        )
        return errors.USAGE
    min_age_s = parse_duration(min_age_raw)
    if min_age_s is None:
        print(
            "bad --min-age " + repr(min_age_raw) + ". Use like 30s, 90m, 3h, 2d. "
            "Run: python3 agent_dash.py worktrees reap --repo <path>",
            file=sys.stderr,
        )
        return errors.USAGE
    top, rows = collect(repo, min_age_s, skips, time.time())
    if top is None:
        print(
            "repo not found: " + str(repo) + ". Run: python3 agent_dash.py worktrees reap --repo <path>",
            file=sys.stderr,
        )
        return errors.NOT_FOUND
    rows = apply_reaps(top, rows, not apply)
    if json_mode:
        errors.emit_json({"repo": top, "apply": apply, "worktrees": rows})
        return errors.OK
    if not rows:
        errors.emit_human("no linked worktrees under " + top, json_mode)
        return errors.OK
    errors.emit_human(format_table(rows), json_mode)
    return errors.OK
