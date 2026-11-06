"""Doctor check: run folders under a hub that hold scratch-sized content.

A hub run folder is durable state (prompts, reports, out and err files).
Environments, Postgres data directories, and caches or images belong in
``$AGENT_SCRATCH``. This module finds run folders under the registered hub
root that still hold them and returns one finding per folder. It only reads:
no delete, no move, no write.

Run folders are ``<hub>/runs/*`` and ``<hub>/*/runs/*``, plus any registered
run folder (``run-dirs.txt``) that lies under the hub. A folder is flagged
when it holds a Python environment (``pyvenv.cfg``, at the top or one level
down), a Postgres data directory (``PG_VERSION``, same depth), or more than
``CACHE_WARN_BYTES`` in cache or image files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

CACHE_WARN_BYTES = 200 * 1024 * 1024
CACHE_DIR_NAMES = frozenset({"cache", "caches", ".cache", "images", "image", "img", "downloads", "decompressed"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"})
MAX_FOLDERS = 500
MAX_ENTRIES = 200_000
TIME_BUDGET_S = 15.0


def hub_roots(cfg) -> list[Path]:
    """The registered hub root (config ``hub_dir``), as a list of zero or one."""
    from dash.decisions import resolve_hub_dir

    hub = resolve_hub_dir(cfg)
    return [hub] if hub is not None else []


def _child_dirs(path: Path):
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError:
                continue
    except OSError:
        return


def run_folders(hub: Path, cfg=None) -> list[Path]:
    """Run folders under one hub root, sorted and capped at ``MAX_FOLDERS``.

    Reads ``run-dirs.txt`` without pruning or rewriting it (the doctor is
    read-only); only entries that exist under the hub are kept.
    """
    folders = set()
    candidates = [hub / "runs", *[child / "runs" for child in _child_dirs(hub)]]
    for runs in candidates:
        if runs.is_dir() and not runs.is_symlink():
            folders.update(_child_dirs(runs))
    try:
        from dash.rundirs import FILE_NAME, _read_lines, state_dir_of

        for line in _read_lines(state_dir_of(cfg) / FILE_NAME):
            path = Path(line)
            if str(path).startswith(str(hub) + os.sep) and path.is_dir():
                folders.add(path)
    except Exception:
        pass
    return sorted(folders, key=lambda p: str(p))[:MAX_FOLDERS]


def _has_at_depth_one(folder: Path, name: str) -> bool:
    if (folder / name).is_file():
        return True
    return any((child / name).is_file() for child in _child_dirs(folder))


def _cache_bytes(folder: Path, deadline: float) -> int:
    """Bytes of cache-named directories and image files under ``folder``."""
    total = 0
    seen = 0
    stack = [(folder, False)]
    while stack:
        current, in_cache = stack.pop()
        if time.monotonic() > deadline or seen > MAX_ENTRIES:
            break
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), in_cache or entry.name.lower() in CACHE_DIR_NAMES))
                elif entry.is_file(follow_symlinks=False):
                    suffix = os.path.splitext(entry.name)[1].lower()
                    if in_cache or suffix in IMAGE_SUFFIXES:
                        total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def inspect_folder(folder: Path, deadline: float) -> dict:
    """Findings for one run folder: ``reasons`` names what it holds (empty when clean)."""
    reasons = []
    venv = _has_at_depth_one(folder, "pyvenv.cfg")
    if venv:
        reasons.append("a Python environment")
    if _has_at_depth_one(folder, "PG_VERSION"):
        reasons.append("a Postgres data directory")
    cache = _cache_bytes(folder, deadline)
    if cache > CACHE_WARN_BYTES:
        reasons.append(f"{cache // (1024 * 1024)} MB of caches or images")
    return {"folder": str(folder), "reasons": reasons, "cache_bytes": cache}


def run_folder_findings(cfg, now: float | None = None) -> list[dict]:
    """Findings for every flagged run folder under the registered hub root."""
    roots = hub_roots(cfg)
    if not roots:
        return []
    deadline = time.monotonic() + TIME_BUDGET_S
    out = []
    for hub in roots:
        for folder in run_folders(hub, cfg=cfg):
            if time.monotonic() > deadline:
                return out
            item = inspect_folder(folder, deadline)
            if item["reasons"]:
                out.append(item)
    return out
