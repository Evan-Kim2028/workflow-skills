"""Reclaim finished run streams: ``agentq gc --runs-older-than 7d [--apply]``.

A job's run folders keep every byte its workers emitted. The pi event streams
(``runs/<n>/pi/stream-<label>.ndjson``) are the bulk of it: measured at 2.18 GB
across 2,199 files for about 30 live workers, and nothing reads a finished run's
stream except a cold store rebuild. ``report.md``, the run's ``out-*.md`` and
``*.meta.json`` are what ``agentq result`` and the dashboards need, so this
command removes the stream files only, and only for jobs that finished more than
N days ago.

Dry run unless ``--apply``: the plan is printed with a per-job breakdown and a
total, and nothing is unlinked. Nothing live is ever touched:

- only jobs in a terminal state (``done``, ``failed``, ``cancelled``) with a
  ``finished_at`` older than the cutoff are considered;
- a run folder with a ``running/`` pid file whose process is alive is skipped,
  whatever the job row says (a cancelled job's child may still be running);
- a file modified after the cutoff is skipped, so a run that is somehow still
  writing keeps its stream;
- only files named ``stream-*.ndjson`` / ``stream-*.jsonl`` under the job's own
  ``runs/`` tree are candidates, and symlinks are never followed;
- a job id must be plain lowercase hex (the generated ids are), and the job's
  ``runs`` folder must resolve back inside ``$AGENTQ_HOME/jobs``; an id shaped
  like ``../../victim`` or a symlinked ``runs`` folder plans and removes
  nothing, so a bad row or a planted link cannot reach outside the tree.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path

DEFAULT_DURATION = "7d"
DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
UNIT_S = {"": 86400.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 7 * 86400.0}
STREAM_PATTERNS = ("stream-*.ndjson", "stream-*.jsonl")
#: A job id as `agentq submit` generates it: plain lowercase hex, no path parts.
JOB_ID_RE = re.compile(r"[0-9a-f]{6,64}")
RUNNING = "running"
TERMINAL = ("done", "failed", "cancelled")


class DurationError(ValueError):
    """A ``--runs-older-than`` value the command cannot read."""


def parse_duration(text: str) -> float:
    """Seconds for ``30m``, ``12h``, ``7d``, ``2w`` or a bare number of days."""
    match = DURATION_RE.match(str(text))
    if not match:
        raise DurationError("expected a duration like 30m, 12h or 7d, not %r" % (text,))
    value, unit = match.groups()
    return float(value) * UNIT_S[unit]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, OverflowError):
        return True
    return True


def running_pids(run_dir: Path) -> list[int]:
    """Pids recorded in ``<run_dir>/running`` (one ``<pid>\\t<model>`` file per live label)."""
    pids = []
    folder = run_dir / RUNNING
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return pids
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        head = text.split("\t")[0].split()[0] if text else ""
        if head.isdigit():
            pids.append(int(head))
    return pids


def run_dir_live(run_dir: Path) -> bool:
    """True when any pid file under ``run_dir/running`` names a live process."""
    return any(pid_alive(pid) for pid in running_pids(run_dir))


def stream_files(root: Path):
    """Every ``stream-*.ndjson`` / ``stream-*.jsonl`` under ``root``, symlinks skipped."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not os.path.islink(os.path.join(dirpath, name)))
        for name in sorted(filenames):
            if not any(fnmatch.fnmatch(name, pattern) for pattern in STREAM_PATTERNS):
                continue
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            found.append(path)
    return found


def job_runs_root(home, job_id):
    """``(<home>/jobs/<id>/runs, None)``, or ``(None, reason)`` when it is not a plain folder.

    The id must be plain lowercase hex, and the resolved ``runs`` folder must sit
    inside the resolved ``jobs`` folder, so an id with path parts or a symlinked
    folder can never point the plan at another tree. The folder is returned as
    written (not resolved) so planned paths stay under ``$AGENTQ_HOME``.
    """
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        return None, "job id is not plain hex"
    jobs_dir = Path(home) / "jobs"
    job_root = jobs_dir / job_id / "runs"
    if job_root.is_symlink():
        return None, "runs folder is a symlink"
    if not job_root.is_dir():
        return None, "no runs folder"
    try:
        resolved = job_root.resolve(strict=True)
        jobs_resolved = jobs_dir.resolve(strict=True)
    except OSError:
        return None, "no runs folder"
    if jobs_resolved not in resolved.parents:
        return None, "runs folder resolves outside the jobs tree"
    return job_root, None


def confined(path, root) -> bool:
    """True when ``path`` is a stream file whose resolved path is under the resolved ``root``."""
    try:
        candidate = Path(str(path))
        if candidate.is_symlink():
            return False
        resolved = candidate.resolve(strict=True)
        base = Path(str(root)).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return False
    if base not in resolved.parents:
        return False
    return any(fnmatch.fnmatch(resolved.name, pattern) for pattern in STREAM_PATTERNS)


def plan(home, jobs, *, older_than_s: float, now: float, live=run_dir_live):
    """What ``apply`` would remove: one entry per stream file, oldest job first.

    ``jobs`` are the job rows to consider (dicts with ``id``, ``state`` and
    ``finished_at``). Entries carry ``job_id``, ``root`` (the job's ``runs``
    folder), ``path``, ``size`` and ``finished_at``; ``skipped`` keeps the
    reasons a job was passed over so the dry run can say why.
    """
    cutoff = float(now) - float(older_than_s)
    items = []
    skipped = []
    for job in jobs:
        job_id = job.get("id")
        state = job.get("state")
        finished = job.get("finished_at")
        if state not in TERMINAL:
            skipped.append({"job_id": job_id, "reason": "state=%s" % (state,)})
            continue
        if finished is None or float(finished) >= cutoff:
            skipped.append({"job_id": job_id, "reason": "finished recently"})
            continue
        job_root, reason = job_runs_root(home, job_id)
        if job_root is None:
            if reason != "no runs folder":
                skipped.append({"job_id": job_id, "reason": reason})
            continue
        for run_dir in sorted(job_root.iterdir()):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            files = stream_files(run_dir)
            if not files:
                continue
            if live(run_dir):
                skipped.append({"job_id": job_id, "reason": "live: %s" % run_dir.name})
                continue
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime >= cutoff:
                    skipped.append({"job_id": job_id, "reason": "modified recently: %s" % path.name})
                    continue
                items.append(
                    {
                        "job_id": job_id,
                        "root": str(job_root),
                        "path": str(path),
                        "size": stat.st_size,
                        "finished_at": float(finished),
                    }
                )
    return items, skipped


def apply(items, unlink=os.unlink):
    """Unlink the planned files. Returns ``(removed, freed_bytes, errors)``.

    Each path is re-confined to its planned job ``runs`` folder before the
    unlink, so an item built by hand (or a folder swapped after planning) cannot
    delete outside the tree. A path that is already gone is not an error.
    """
    removed = 0
    freed = 0
    errors = []
    for item in items:
        path = str(item.get("path") or "")
        candidate = Path(path)
        try:
            present = candidate.exists() or candidate.is_symlink()
        except OSError:
            present = False
        if not present:
            continue
        if not confined(path, item.get("root")):
            errors.append({"path": path, "error": "outside the job runs tree"})
            continue
        try:
            unlink(item["path"])
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append({"path": item["path"], "error": exc.__class__.__name__})
            continue
        removed += 1
        freed += int(item.get("size") or 0)
    return removed, freed, errors


def human_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f TB" % value  # pragma: no cover - loop always returns


def summarize(items, limit: int = 10) -> list[str]:
    """Lines for the dry run: total, then the largest jobs by reclaimable bytes."""
    per_job: dict[str, dict] = {}
    for item in items:
        slot = per_job.setdefault(item["job_id"], {"files": 0, "size": 0, "newest": 0.0})
        slot["files"] += 1
        slot["size"] += int(item.get("size") or 0)
        slot["newest"] = max(slot["newest"], float(item.get("finished_at") or 0.0))
    lines = [
        "%d stream files, %s reclaimable across %d finished jobs"
        % (len(items), human_bytes(sum(int(item.get("size") or 0) for item in items)), len(per_job))
    ]
    ranked = sorted(per_job.items(), key=lambda pair: pair[1]["size"], reverse=True)
    for job_id, slot in ranked[:limit]:
        lines.append(
            "  %s  %d file(s)  %s  finished %s"
            % (
                job_id,
                slot["files"],
                human_bytes(slot["size"]),
                time.strftime("%Y-%m-%d", time.localtime(slot["newest"])) if slot["newest"] else "?",
            )
        )
    if len(ranked) > limit:
        lines.append("  … %d more jobs" % (len(ranked) - limit))
    return lines
