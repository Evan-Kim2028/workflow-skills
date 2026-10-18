"""Workstream names from workstreams.tsv and issue counts from frontmatter.

The hub directory is optional. An empty config skips the source. A configured
directory that is not on disk is skipped too, so a fresh install stays healthy.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

NAME = "hub"
INTERVAL_S = 60
ISSUES_DIR = ""
WORKSTREAMS_TSV = ""
STATUSES = ("backlog", "todo", "doing", "review", "done", "cancelled")
_log = logging.getLogger("agent_dash.hub")


def poll(store, cfg, now: float) -> None:
    from dash import dataroot as _dataroot

    del now
    cfg = cfg or {}
    hub = _path(_dataroot.hub_dir(cfg))
    issues = _path(cfg.get("issues_dir"))
    catalog = _path(cfg.get("workstreams_tsv"))
    if hub is not None:
        if issues is None:
            issues = hub / "issues"
        if catalog is None:
            catalog = hub / "workstreams.tsv"
    if issues is None or catalog is None:
        return
    if not issues.is_dir() or not catalog.is_file():
        _log.warning("hub tracker not found")
        return
    names = _names(catalog)
    counts = {code: {status: 0 for status in STATUSES} for code in names}
    for path in sorted(issues.glob("*.md")):
        meta = _frontmatter(path)
        if not meta:
            continue
        ws = meta.get("workstream") or ""
        status = (meta.get("status") or "").lower()
        if ws not in counts or status not in counts[ws]:
            continue
        counts[ws][status] += 1
    rows = []
    for code, name in names.items():
        row = {"ws": code, "name": name}
        row.update(counts[code])
        rows.append(row)
    store.set_ws_counts(rows)


def _path(value) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(os.path.expanduser(os.path.expandvars(text)))


def _names(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    names = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0].strip()
        if not code:
            continue
        names[code] = parts[1].strip() if len(parts) > 1 else ""
    return names


def _frontmatter(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            blob = handle.read(4096)
    except OSError:
        return None
    text = blob.decode("utf-8", "replace")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    meta = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta
