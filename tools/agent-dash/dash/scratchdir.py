"""Disposable scratch folder per worker run.

Environments, local Postgres clusters, decompressed caches, and downloaded
images belong in ``$AGENT_SCRATCH``, not in the run folder. Hub run folders
are durable (prompts, reports, out and err files, decisions); scratch is
the part that grows and is safe to delete once the run has finished.

Layout::

    <scratch root>/<workstream or hub>/<label>/

The root is ``AGENT_DASH_SCRATCH_ROOT`` when set, else
``~/.cache/agent-dash/scratch``. The workstream key comes from the label
(``ws4-WS4.1-notes-index`` -> ``WS4``; anything else -> ``hub``).

``run_worker.py`` calls :func:`ensure` before every launch and exports the
path as ``AGENT_SCRATCH``. :func:`ensure` also writes an owner marker
(``.agent-scratch.json``) that names the label and the run folder, so
``scratch gc`` can tell whether the run is still live. A folder without a
marker was not made by a launcher and ``scratch gc`` never touches it.

Stdlib only; never reads a prompt, a report, or an env file.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ROOT_ENV = "AGENT_DASH_SCRATCH_ROOT"
ENV_NAME = "AGENT_SCRATCH"
OWNER_FILE = ".agent-scratch.json"
HUB_KEY = "hub"

_WS_RE = re.compile(r"^ws([1-9][0-9]*)(?:-|$)", re.IGNORECASE)
_SAFE_LABEL = re.compile(r"^[\w.+-]{1,200}$")


def scratch_root() -> Path:
    """The scratch root: ``AGENT_DASH_SCRATCH_ROOT``, else the cache default."""
    raw = os.environ.get(ROOT_ENV, "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".cache" / "agent-dash" / "scratch"


def workstream_key(label: str) -> str:
    """``WS<n>`` for a workstream label, ``hub`` for everything else."""
    text = str(label or "")
    try:
        from dash import labels as label_lib

        parsed = label_lib.parse(text)
    except Exception:
        parsed = None
    if parsed and parsed.get("scope", HUB_KEY) != HUB_KEY:
        return str(parsed["scope"]).upper()
    match = _WS_RE.match(text)
    if match:
        return f"WS{match.group(1)}"
    return HUB_KEY


def scratch_path(label: str, root=None) -> Path:
    """``<root>/<workstream or hub>/<label>``. Never creates anything."""
    text = str(label or "")
    if not _SAFE_LABEL.fullmatch(text) or text in (".", ".."):
        raise ValueError(f"unsafe scratch label: {text!r}")
    base = Path(root) if root is not None else scratch_root()
    return base / workstream_key(text) / text


def ensure(label: str, run_dir=None, root=None, now: float | None = None) -> Path:
    """Create the run's scratch folder and its owner marker. Returns the path.

    The marker is written once; a retry under the same label keeps the
    first run folder recorded. Raises OSError only when the folder itself
    cannot be created; a failed marker write is ignored (the folder still
    works, and gc keeps a folder it cannot verify).
    """
    path = scratch_path(label, root)
    path.mkdir(parents=True, exist_ok=True)
    owner = path / OWNER_FILE
    if owner.exists():
        return path
    moment = time.time() if now is None else float(now)
    record = {
        "label": str(label),
        "workstream": workstream_key(label),
        "run_dir": str(run_dir) if run_dir is not None else "",
        "created_at": int(moment),
    }
    tmp = owner.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, owner)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def read_owner(folder) -> dict | None:
    """The owner marker of a scratch folder, or None when absent or unreadable."""
    try:
        data = json.loads((Path(folder) / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
