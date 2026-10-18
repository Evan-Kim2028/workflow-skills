"""One place that resolves every data path.

The dashboard is the app and its logic only; all data (the project
descriptor, workstream bundles, the sqlite state, run folders, hub files)
is local on the owner's computer by default and can be pointed elsewhere:
another local path or a mounted/synced cloud folder. A path may be any
local filesystem path. Real cloud clients are future work; the seam is the
``Storage`` protocol below (``read_text``/``write_text``/``list``/
``exists``/``replace``, implemented here by the local filesystem only).

Precedence everywhere is environment, then config, then the default, so
existing overrides keep working: ``AGENT_DASH_STATE_DIR``,
``AGENT_DASH_WORKSTREAMS``, ``AGENT_DASH_HUB_DIR``, and the config keys
``state_dir``, ``hub_dir`` (plus the new ``data_root``). The default data
root is today's ``~/.local/share/agent-dash``, so nothing moves.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

DATA_ROOT_ENV = "AGENT_DASH_DATA_ROOT"
STATE_ENV = "AGENT_DASH_STATE_DIR"
WORKSTREAMS_ENV = "AGENT_DASH_WORKSTREAMS"
HUB_ENV = "AGENT_DASH_HUB_DIR"

PROJECT_FILENAME = "project.json"


class Storage:
    """Tiny read/write seam for data files.

    Only the local filesystem implements it today (``LocalStorage``).
    A future cloud client implements these five methods and passes it
    to the helpers that accept a ``storage`` argument; path resolution
    in this module stays unchanged.
    """

    def read_text(self, path) -> str:
        raise NotImplementedError

    def write_text(self, path, text: str) -> None:
        raise NotImplementedError

    def exists(self, path) -> bool:
        raise NotImplementedError

    def list(self, path) -> list:
        raise NotImplementedError

    def replace(self, tmp, dest) -> None:
        raise NotImplementedError


class LocalStorage(Storage):
    """The only ``Storage`` today: plain local files."""

    def read_text(self, path) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path, text: str) -> None:
        Path(path).write_text(text, encoding="utf-8")

    def exists(self, path) -> bool:
        return Path(path).exists()

    def list(self, path) -> list:
        return sorted(Path(path).iterdir())

    def replace(self, tmp, dest) -> None:
        os.replace(tmp, dest)


storage = LocalStorage()


def default_root() -> Path:
    """The default data root: today's state dir, so nothing moves."""
    return Path.home() / ".local" / "share" / "agent-dash"


def _expand(value: str) -> str:
    from dash.locate import expand

    return expand(value)


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is not None and raw.strip():
        return _expand(raw.strip())
    return None


def _cfg_value(cfg: dict | None, key: str) -> str | None:
    if isinstance(cfg, dict):
        raw = cfg.get(key)
        if isinstance(raw, str) and raw.strip():
            return _expand(raw.strip())
    return None


def data_root(cfg: dict | None = None) -> Path:
    """The data root: ``AGENT_DASH_DATA_ROOT``, config ``data_root``, default."""
    return Path(_env(DATA_ROOT_ENV) or _cfg_value(cfg, "data_root") or str(default_root()))


def state_dir(cfg: dict | None = None) -> Path:
    """The state dir: ``AGENT_DASH_STATE_DIR``, config ``state_dir``, data root."""
    return Path(_env(STATE_ENV) or _cfg_value(cfg, "state_dir") or str(data_root(cfg)))


def workstreams_dir(cfg: dict | None = None) -> Path:
    """The bundles dir: ``AGENT_DASH_WORKSTREAMS``, else ``<data_root>/workstreams``."""
    override = _env(WORKSTREAMS_ENV)
    if override:
        return Path(override)
    return data_root(cfg) / "workstreams"


def hub_dir(cfg: dict | None = None) -> str:
    """The hub dir: ``AGENT_DASH_HUB_DIR``, config ``hub_dir``.

    Falls back to ``<data_root>/hub`` when that directory exists, else
    ``""`` (no hub), so a fresh install without a hub stays quiet.
    """
    override = _env(HUB_ENV) or _cfg_value(cfg, "hub_dir")
    if override:
        return override
    candidate = data_root(cfg) / "hub"
    try:
        if candidate.is_dir():
            return str(candidate)
    except OSError:
        pass
    return ""


def project_file(cfg: dict | None = None, root=None) -> Path:
    """The project descriptor: ``<data_root>/project.json``."""
    base = Path(root) if root is not None else data_root(cfg)
    return base / PROJECT_FILENAME


def data_root_hash(cfg: dict | None = None, root=None) -> str:
    """Short opaque id for a data root: sha256 of the resolved path.

    The hex digest names the data root without ever exposing the path,
    so ``GET /api/project`` can disambiguate instances. Empty or
    unreadable input hashes as itself.
    """
    base = Path(root) if root is not None else data_root(cfg)
    try:
        text = os.path.realpath(str(base or ""))
    except (OSError, ValueError):
        text = str(base or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
