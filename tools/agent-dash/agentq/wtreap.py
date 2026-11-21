"""Remove the auto worktree of a write job once it finishes with verdict PASS.

The broker gives every write job its own worktree (``<root>/<repo>/agentq-<id>``
on branch ``agentq/<id>``) and, before this module, never removed one: on
2026-11-21 the laptop held 113 of them, 55 for jobs that had already passed.
The launchd worktree janitor purges idle lanes on a timer whatever the verdict,
and under load its process scan gives up and keeps everything.

This reaper runs from the broker's janitor pass and only touches a worktree when
every guard holds:

- the job is ``done`` with verdict ``PASS`` and finished more than
  ``janitor.pass_worktree_grace_s`` ago (default 15 minutes, so the lead can
  still look at it);
- the job ran in an auto worktree (``orig_workspace`` set), its id is plain
  hex, and the folder is named ``agentq-<id>``;
- git confirms the folder is a linked worktree of ``orig_workspace`` checked out
  on ``agentq/<id>``, and ``git status --porcelain`` is empty, so nothing
  uncommitted or untracked is lost (ignored build folders such as ``.venv`` go);
- the job's run folder has no live pid, no process has its cwd inside the
  folder, and the process scan itself succeeded;
- the prompt does not set ``keep_worktree: true`` in its front matter.

Any other verdict (``BLOCKED``, ``PARTIAL``, ``DEAD``, cancelled) keeps its
worktree for the lead to inspect.

The branch is kept, so the work survives: ``git worktree add <path> agentq/<id>``
brings the checkout back. Removal is a rename into ``<root>/<repo>/.agentq-trash/``
on the same volume plus ``git worktree prune``, so the broker tick never waits on
deleting a 1 GB ``.venv``; a detached, niced ``rm -rf`` empties the trash.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

DEFAULT_GRACE_S = 900.0
DEFAULT_PER_PASS = 8
TRASH_DIR = ".agentq-trash"
GIT_TIMEOUT_S = 30.0
SCAN_TIMEOUT_S = 20.0
JOB_ID_RE = re.compile(r"[0-9a-f]{6,64}")
TRUE_WORDS = ("1", "true", "yes", "on")


def _git(cwd, *args) -> subprocess.CompletedProcess | None:
    """``git -C cwd args``, or None when git did not run."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None


def process_cwds() -> list[str] | None:
    """The cwd of every process ``lsof`` can see, or None when the scan failed."""
    try:
        done = subprocess.run(
            ["lsof", "-w", "-d", "cwd", "-Fn"], capture_output=True, text=True,
            timeout=SCAN_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode not in (0, 1) or not done.stdout:
        return None
    return [line[1:] for line in done.stdout.splitlines() if line.startswith("n/")]


def _busy(path: Path, cwds: list[str]) -> bool:
    real = os.path.realpath(path)
    prefix = real + os.sep
    return any(cwd == real or cwd.startswith(prefix) for cwd in cwds)


def keeps_worktree(job: dict, front_matter) -> bool:
    """True when the prompt asks to keep the worktree, or cannot be read."""
    try:
        text = Path(str(job.get("prompt_path") or "")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return str(front_matter(text).get("keep_worktree", "")).strip().lower() in TRUE_WORDS


def is_candidate(job: dict, *, now: float, grace_s: float) -> bool:
    """A passed auto-worktree job past the grace whose folder still exists."""
    if job.get("state") != "done" or job.get("verdict") != "PASS" or not job.get("orig_workspace"):
        return False
    finished = job.get("finished_at")
    if finished is None or float(finished) > float(now) - float(grace_s):
        return False
    job_id = str(job.get("id") or "")
    workspace = Path(str(job.get("workspace") or ""))
    return bool(JOB_ID_RE.fullmatch(job_id)) and workspace.name == f"agentq-{job_id}" and workspace.is_dir()


def why_kept(job: dict) -> str | None:
    """The git guard that keeps this worktree, or None when it is safe to remove."""
    job_id = str(job["id"])
    path = Path(str(job["workspace"]))
    repo = Path(str(job["orig_workspace"]))
    head = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if head is None or head.returncode != 0:
        return "git could not read the worktree"
    if head.stdout.strip() != f"agentq/{job_id}":
        return "checked out on %s, not agentq/%s" % (head.stdout.strip(), job_id)
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None or common.returncode != 0:
        return "git could not read the common dir"
    if os.path.realpath(common.stdout.strip()) != os.path.realpath(repo / ".git"):
        return "not a worktree of %s" % repo
    status = _git(path, "status", "--porcelain", "--untracked-files=normal")
    if status is None or status.returncode != 0:
        return "git status failed"
    if status.stdout.strip():
        return "uncommitted or untracked files"
    return None


def empty_trash(trash: Path) -> None:
    """Delete the trash folder's contents in a detached, niced process."""
    try:
        subprocess.Popen(
            ["nice", "-n", "15", "rm", "-rf", str(trash)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip
    except OSError:
        pass


def remove(job: dict, *, empty=empty_trash) -> str | None:
    """Move the worktree into the trash and prune it from git. Returns an error, or None."""
    path = Path(str(job["workspace"]))
    repo = Path(str(job["orig_workspace"]))
    trash = path.parent / TRASH_DIR / f"{path.name}-{int(time.time())}"
    try:
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.rename(path, trash)
    except OSError as exc:
        return "move failed: %s" % exc.__class__.__name__
    pruned = _git(repo, "worktree", "prune")
    empty(trash)
    if pruned is None or pruned.returncode != 0:
        return "moved, but git worktree prune failed"
    return None


def reap(jobs, *, now: float, grace_s: float, limit: int, front_matter, run_dir_live,
         cwds=process_cwds, empty=empty_trash) -> dict:
    """Remove up to ``limit`` passed jobs' worktrees. Returns ``{"removed": [...], "kept": [...]}``."""
    removed: list[dict] = []
    kept: list[dict] = []
    candidates = [job for job in jobs if is_candidate(job, now=now, grace_s=grace_s)]
    if not candidates or limit <= 0:
        return {"removed": removed, "kept": kept}
    scan = cwds()
    for job in candidates:
        if len(removed) >= limit:
            break
        job_id = job["id"]
        if scan is None:
            kept.append({"job_id": job_id, "reason": "process scan failed"})
            continue
        if job.get("run_dir") and run_dir_live(Path(str(job["run_dir"]))):
            kept.append({"job_id": job_id, "reason": "run still live"})
            continue
        if _busy(Path(str(job["workspace"])), scan):
            kept.append({"job_id": job_id, "reason": "a process is working inside it"})
            continue
        if keeps_worktree(job, front_matter):
            kept.append({"job_id": job_id, "reason": "keep_worktree"})
            continue
        reason = why_kept(job) or remove(job, empty=empty)
        if reason:
            kept.append({"job_id": job_id, "reason": reason})
            continue
        removed.append({"job_id": job_id, "label": job.get("label") or "", "path": str(job["workspace"])})
    return {"removed": removed, "kept": kept}
