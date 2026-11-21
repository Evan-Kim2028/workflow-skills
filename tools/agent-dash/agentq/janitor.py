"""Disk janitor for the agentq broker: scratch reaper, job-dir retention, disk pressure.

The laptop filled up because nothing removed the two things that grow without
bound: worker scratch (``$AGENT_SCRATCH``, ``~/.cache/agent-dash/scratch/<group>/<label>/``)
and the job folders under ``$AGENTQ_HOME/jobs/``. Measured on 2026-11-19: 95 GB of
scratch, much of it for jobs that had finished hours earlier, and 36 GB of job
folders. This module is the broker's answer, and it runs from ``Broker.tick``:

- :func:`reap_scratch` removes the scratch folder of a job that has been
  ``done``/``failed``/``cancelled`` for longer than ``janitor.scratch_grace_s``
  (default one hour). It only ever removes a folder that carries the launcher's
  owner marker (``.agent-scratch.json``) naming that job's label, and whose
  recorded run folder sits under that job's own ``jobs/<id>`` tree, so a folder
  agentq did not create, or one belonging to another job that reused the label,
  is never touched. A job opts out with ``keep_scratch: true`` in its prompt
  front matter.
- :func:`retention` shrinks the job folder of a terminal job older than
  ``janitor.jobs_keep_days`` (default 7). ``prompt.md``, ``report.md`` and the
  run's ``out-*.md`` stay, and the verdict lives in the job row; the pi streams,
  session logs and err logs are gzipped in place, or deleted when a single file
  is larger than :data:`GZIP_MAX_BYTES` (compressing a multi-gigabyte stream is
  not worth a broker tick).
- :func:`disk_state` reads free space on the volume holding the scratch root
  against ``slots.disk_floor_gb`` (default 25). Below the floor the broker holds
  new ``write`` and ``tests`` admissions (they queue, they never fail) and runs
  the reapers at once; it reopens only above the floor plus
  :data:`RESUME_MARGIN_GB`, so a reap that frees a few hundred MB does not flap.

Every reclaim appends one row to the slot history file (``slots/events.jsonl``,
outcome ``reclaimed``, with the bytes), so ``agentq slots --history`` shows the
reclaimed bytes per hour next to the slot totals.

Stdlib only. The scratch layout is the one ``dash/scratchdir.py`` writes; the
root and the owner marker name are repeated here so the broker does not import
the dashboard package.
"""

from __future__ import annotations

import fnmatch
import gzip
import json
import os
import shutil
from pathlib import Path

from agentq import db
from agentq import gc as gc_lib

#: The scratch root override ``dash/scratchdir.py`` reads, and its default.
SCRATCH_ROOT_ENV = "AGENT_DASH_SCRATCH_ROOT"
SCRATCH_ROOT_DEFAULT = Path(".cache") / "agent-dash" / "scratch"
#: The owner marker ``run_worker.py`` writes into every scratch folder it creates.
OWNER_FILE = ".agent-scratch.json"

DEFAULT_SCRATCH_GRACE_S = 3600.0
DEFAULT_JOBS_KEEP_DAYS = 7.0
DEFAULT_DISK_FLOOR_GB = 25.0
#: The gate reopens only this far above the floor, so a small reap cannot flap it.
RESUME_MARGIN_GB = 5.0
#: How often the reapers run when the disk is not under pressure.
JANITOR_INTERVAL_S = 300.0
#: Retention work per pass, so one tick never walks thousands of job folders.
RETENTION_JOBS_PER_PASS = 10
RETENTION_FILES_PER_JOB = 20
#: A file larger than this is deleted rather than gzipped.
GZIP_MAX_BYTES = 32 * 1024 * 1024

#: Files kept at the job root whatever their size: the prompt, the report, the verdict.
KEEP_AT_JOB_ROOT = ("prompt.md", "report.md")
#: Small report copies kept anywhere in the job folder.
KEEP_PATTERNS = ("out-*.md",)
#: The bulk: pi event streams, pi session logs, launcher err logs.
LARGE_PATTERNS = ("stream-*.ndjson", "stream-*.jsonl", "*.log", "*.jsonl")

#: The ``jobs.detail`` prefix the disk gate writes, and how ``agentq status`` counts it.
HOLD_PREFIX = "hold disk: "
#: The job shapes the disk gate holds: a write job, or anything on a tests slot.
DISK_GATED_KINDS = ("write",)
DISK_GATED_RES = ("tests",)

DEFAULT_SETTINGS = {
    "scratch_grace_s": DEFAULT_SCRATCH_GRACE_S,
    "jobs_keep_days": DEFAULT_JOBS_KEEP_DAYS,
    "disk_floor_gb": DEFAULT_DISK_FLOOR_GB,
}


def _number(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def settings(raw_janitor=None, raw_slots=None) -> dict:
    """The janitor knobs: ``janitor.*`` in ``broker.json``, plus ``slots.disk_floor_gb``.

    ``slots.disk_floor_gb`` is read from the raw ``slots`` section because the slot
    caps are coerced to whole numbers and the floor may be fractional. ``resume_gb``
    is derived, never configured.
    """
    out = dict(DEFAULT_SETTINGS)
    if isinstance(raw_janitor, dict):
        for name in DEFAULT_SETTINGS:
            if name in raw_janitor:
                out[name] = _number(raw_janitor[name], out[name])
    if isinstance(raw_slots, dict) and "disk_floor_gb" in raw_slots:
        out["disk_floor_gb"] = _number(raw_slots["disk_floor_gb"], out["disk_floor_gb"])
    out["resume_gb"] = out["disk_floor_gb"] + RESUME_MARGIN_GB
    return out


def scratch_root() -> Path:
    """The scratch root: ``AGENT_DASH_SCRATCH_ROOT``, else ``~/.cache/agent-dash/scratch``."""
    raw = os.environ.get(SCRATCH_ROOT_ENV, "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / SCRATCH_ROOT_DEFAULT


def _existing(path) -> Path | None:
    """``path`` or its nearest existing parent, so a missing root still has a volume."""
    candidate = Path(path)
    for _ in range(40):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            return None
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def free_gb(path=None) -> float | None:
    """Free space in GB on the volume holding ``path`` (default the scratch root), or None."""
    probe = _existing(scratch_root() if path is None else path)
    if probe is None:
        return None
    try:
        return shutil.disk_usage(str(probe)).free / (1024.0 ** 3)
    except OSError:
        return None


def read_owner(folder) -> dict | None:
    """The owner marker of a scratch folder, or None when absent or unreadable."""
    try:
        data = json.loads((Path(folder) / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _confined(path, base) -> bool:
    """True when ``path`` resolves to a folder strictly inside ``base``."""
    try:
        resolved = Path(path).resolve(strict=True)
        root = Path(base).resolve(strict=True)
    except (OSError, ValueError):
        return False
    return root in resolved.parents


def scratch_index(root=None) -> dict[str, list[Path]]:
    """``label -> folders`` for every ``<root>/<group>/<label>`` that carries that label's marker.

    One walk of the root, so a pass over thousands of finished jobs costs one
    ``iterdir`` per group rather than one per job. A folder without a marker, or
    with a marker naming another label, was not made by that job's launcher and is
    left out.
    """
    base = Path(root) if root is not None else scratch_root()
    index: dict[str, list[Path]] = {}
    try:
        groups = sorted(base.iterdir())
    except OSError:
        return index
    for group in groups:
        try:
            if group.is_symlink() or not group.is_dir():
                continue
            children = sorted(group.iterdir())
        except OSError:
            continue
        for folder in children:
            try:
                if folder.is_symlink() or not folder.is_dir():
                    continue
            except OSError:
                continue
            owner = read_owner(folder)
            if owner is None:
                continue
            label = str(owner.get("label") or "")
            if not label or label != folder.name:
                continue
            if not _confined(folder, base):
                continue
            index.setdefault(label, []).append(folder)
    return index


def folder_bytes(path) -> int:
    """Bytes of the regular files under ``path``; symlinks are never followed or counted."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def front_matter(text: str) -> dict:
    """The prompt's ``---`` front matter as a flat dict; no front matter gives ``{}``."""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    meta: dict = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip().strip("\"'")
    return meta


TRUE_WORDS = ("1", "true", "yes", "on")


def opts_out(job) -> bool:
    """True when the job asked to keep its scratch (``keep_scratch`` in its prompt front matter).

    An unreadable prompt counts as an opt-out: the reaper never removes a folder it
    cannot prove the job wanted removed.
    """
    try:
        text = Path(str(job.get("prompt_path") or "")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return str(front_matter(text).get("keep_scratch", "")).strip().lower() in TRUE_WORDS


def _owned_by_job(folder, job, home, live_labels=frozenset()) -> bool:
    """True when the folder's marker names this job's label and, if it records a run folder,
    that run folder sits under this job's own ``jobs/<id>`` tree.

    A marker with no run folder cannot be tied to one job, so it is only this job's
    when no other job with the same label is still queued or running: labels are
    reused, and a folder shared by a live job must survive.
    """
    owner = read_owner(folder)
    if owner is None or str(owner.get("label") or "") != str(job.get("label") or ""):
        return False
    run_dir = str(owner.get("run_dir") or "").strip()
    if not run_dir:
        return str(job.get("label") or "") not in live_labels
    try:
        resolved = Path(run_dir).resolve(strict=False)
        job_root = (Path(home) / "jobs" / str(job.get("id") or "")).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return resolved == job_root or job_root in resolved.parents


def record(home, *, kind: str, label: str, path, size: int, now: float) -> None:
    """Append one reclaim row to ``slots/events.jsonl`` so ``agentq slots --history`` shows it.

    ``agentq.slots`` imports ``agentq.broker``, which imports this module, so the
    import is inside the call.
    """
    from agentq import slots as slots_lib

    slots_lib.record_event(
        home, kind=kind, label=label or "-", pid=None, wait_ms=0, hold_ms=0,
        outcome="reclaimed", argv=str(path)[:slots_lib.EVENT_ARGV_CHARS], bytes=int(size),
        clock=lambda: now,
    )


def reap_scratch(home, jobs, *, now: float, grace_s: float, root=None, index=None,
                 remove=None, live=None, record_events: bool = True) -> dict:
    """Remove the scratch folder of every terminal job that finished more than ``grace_s`` ago.

    ``jobs`` are job rows. A job is skipped when it is not terminal, finished
    recently, asked to keep its scratch, still has a live pid in its run folder, or
    has no folder carrying its own owner marker. Returns
    ``{"removed": [...], "kept": [...], "bytes": n}``; each removed entry carries
    ``job_id``, ``label``, ``path`` and ``bytes``.
    """
    cutoff = float(now) - float(grace_s)
    base = Path(root) if root is not None else scratch_root()
    unlink_tree = remove or shutil.rmtree
    is_live = live or gc_lib.run_dir_live
    removed: list[dict] = []
    kept: list[dict] = []
    candidates: list[dict] = []
    live_labels = {str(job.get("label") or "") for job in jobs
                   if job.get("state") not in gc_lib.TERMINAL}
    for job in jobs:
        job_id = job.get("id")
        state = job.get("state")
        if state not in gc_lib.TERMINAL:
            kept.append({"job_id": job_id, "reason": "state=%s" % (state,)})
            continue
        finished = job.get("finished_at")
        if finished is None or float(finished) >= cutoff:
            kept.append({"job_id": job_id, "reason": "finished recently"})
            continue
        if job.get("run_dir") and is_live(Path(str(job["run_dir"]))):
            kept.append({"job_id": job_id, "reason": "run still live"})
            continue
        candidates.append(job)
    if not candidates:
        # Nothing to reap: do not walk the scratch root at all. On a laptop holding
        # 95 GB of scratch that walk is the expensive part of a pass.
        return {"removed": removed, "kept": kept, "bytes": 0}
    folders = scratch_index(base) if index is None else index
    for job in candidates:
        job_id = job.get("id")
        label = str(job.get("label") or "")
        candidates_for_label = folders.get(label) or []
        if not candidates_for_label:
            kept.append({"job_id": job_id, "reason": "no scratch folder"})
            continue
        if opts_out(job):
            # Read the prompt only for a job that still has a folder to remove: on a
            # laptop with thousands of finished jobs that is thousands of file reads.
            kept.append({"job_id": job_id, "reason": "keep_scratch"})
            continue
        for folder in candidates_for_label:
            if not _owned_by_job(folder, job, home, live_labels):
                kept.append({"job_id": job_id, "reason": "not this job's folder: %s" % folder})
                continue
            if not _confined(folder, base):
                kept.append({"job_id": job_id, "reason": "outside the scratch root: %s" % folder})
                continue
            size = folder_bytes(folder)
            try:
                unlink_tree(str(folder))
            except OSError as exc:
                kept.append({"job_id": job_id, "reason": "remove failed: %s" % exc.__class__.__name__})
                continue
            removed.append({"job_id": job_id, "label": label, "path": str(folder), "bytes": size})
            if record_events:
                record(home, kind="scratch", label=label, path=folder, size=size, now=now)
    return {"removed": removed, "kept": kept, "bytes": sum(item["bytes"] for item in removed)}


def _gzip_file(path: Path) -> int:
    """Gzip ``path`` to ``path.gz`` and remove the original. Returns the bytes reclaimed."""
    before = path.stat().st_size
    dest = path.with_name(path.name + ".gz")
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        os.replace(tmp, dest)
        path.unlink()
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return max(0, before - dest.stat().st_size)


def _large_files(job_root: Path, *, cutoff: float, limit: int) -> list[Path]:
    """The retention candidates under one job folder, newest-modified last, capped at ``limit``."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(job_root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not os.path.islink(os.path.join(dirpath, name)))
        for name in sorted(filenames):
            if name.endswith(".gz") or name.endswith(".tmp"):
                continue
            if name in KEEP_AT_JOB_ROOT or any(fnmatch.fnmatch(name, p) for p in KEEP_PATTERNS):
                continue
            if not any(fnmatch.fnmatch(name, pattern) for pattern in LARGE_PATTERNS):
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            found.append(path)
            if len(found) >= limit:
                return found
    return found


def retention(home, jobs, *, now: float, keep_days: float, gzip_max: int = GZIP_MAX_BYTES,
              jobs_per_pass: int = RETENTION_JOBS_PER_PASS,
              files_per_job: int = RETENTION_FILES_PER_JOB, live=None,
              record_events: bool = True) -> dict:
    """Shrink the job folders of terminal jobs older than ``keep_days``.

    ``prompt.md``, ``report.md`` and the run's ``out-*.md`` stay, and the verdict
    lives in the job row. Every other large file (pi streams, session logs, err
    logs) is gzipped in place, or deleted when it is larger than ``gzip_max``.
    At most ``jobs_per_pass`` jobs and ``files_per_job`` files per job are touched,
    so one tick never walks the whole history. Returns
    ``{"jobs": [...], "bytes": n, "files": n}``.
    """
    cutoff = float(now) - float(keep_days) * 86400.0
    is_live = live or gc_lib.run_dir_live
    jobs_dir = Path(home) / "jobs"
    done: list[dict] = []
    for job in jobs:
        if len(done) >= jobs_per_pass:
            break
        job_id = job.get("id")
        if job.get("state") not in gc_lib.TERMINAL:
            continue
        finished = job.get("finished_at")
        if finished is None or float(finished) >= cutoff:
            continue
        if not isinstance(job_id, str) or gc_lib.JOB_ID_RE.fullmatch(job_id) is None:
            continue
        job_root = jobs_dir / job_id
        try:
            if job_root.is_symlink() or not job_root.is_dir():
                continue
            resolved = job_root.resolve(strict=True)
            base = jobs_dir.resolve(strict=True)
        except OSError:
            continue
        if base not in resolved.parents:
            continue
        if job.get("run_dir") and is_live(Path(str(job["run_dir"]))):
            continue
        files = _large_files(job_root, cutoff=cutoff, limit=files_per_job)
        if not files:
            continue
        freed = 0
        gzipped = 0
        deleted = 0
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            try:
                if size > gzip_max:
                    path.unlink()
                    freed += size
                    deleted += 1
                else:
                    freed += _gzip_file(path)
                    gzipped += 1
            except OSError:
                continue
        if not (gzipped or deleted):
            continue
        done.append({"job_id": job_id, "label": job.get("label") or "", "files": gzipped + deleted,
                     "gzipped": gzipped, "deleted": deleted, "bytes": freed})
        if record_events:
            record(home, kind="retention", label=str(job.get("label") or ""), path=job_root,
                   size=freed, now=now)
    return {"jobs": done, "bytes": sum(item["bytes"] for item in done),
            "files": sum(item["files"] for item in done)}


def run(home, conn, *, now: float, settings: dict, root=None, record_events: bool = True) -> dict:
    """One janitor pass: reap finished scratch, then shrink old job folders."""
    jobs = db.select_jobs(conn)
    scratch = reap_scratch(home, jobs, now=now, grace_s=settings["scratch_grace_s"], root=root,
                           record_events=record_events)
    kept = retention(home, jobs, now=now, keep_days=settings["jobs_keep_days"],
                     record_events=record_events)
    return {"scratch": scratch, "retention": kept}


def held_count(conn) -> int:
    """Queued jobs the disk gate is holding, from the ``jobs.detail`` the broker writes."""
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND detail LIKE ?", (HOLD_PREFIX + "%",)
    ).fetchone()
    return int(row[0]) if row else 0


def disk_state(conn, home, *, floor_gb: float, root=None, free=None) -> dict:
    """Free space on the scratch volume against the floor, and how many jobs the gate holds."""
    reading = free if free is not None else free_gb(root)
    return {
        "free_gb": reading,
        "floor_gb": float(floor_gb),
        "resume_gb": float(floor_gb) + RESUME_MARGIN_GB,
        "active": reading is not None and reading < float(floor_gb),
        "held": held_count(conn),
    }


def format_disk(state: dict) -> str:
    """The one line ``agentq status`` and ``agentq slots`` print for the disk gate."""
    free = "?" if state.get("free_gb") is None else "%.1f GB" % state["free_gb"]
    gate = "held" if state.get("active") else "open"
    return "disk: %s free, floor %.0f GB, %d job(s) held, gate %s" % (
        free, state.get("floor_gb") or 0.0, int(state.get("held") or 0), gate,
    )


def format_pass(report: dict) -> str:
    """One log line for a janitor pass that reclaimed anything."""
    scratch = report["scratch"]
    kept = report["retention"]
    return "janitor: scratch %d folder(s) %s, retention %d file(s) %s" % (
        len(scratch["removed"]), gc_lib.human_bytes(scratch["bytes"]),
        kept["files"], gc_lib.human_bytes(kept["bytes"]),
    )


def reclaimed(report: dict) -> int:
    """Total bytes a pass reclaimed."""
    return int(report["scratch"]["bytes"]) + int(report["retention"]["bytes"])

