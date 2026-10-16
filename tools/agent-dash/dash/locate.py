"""Path defaults and layered JSON. Nothing here is a personal machine path.

Code defaults come from $HOME. A shipped *.example.json documents a working
layout. *.local.json, when present, replaces the example file for that stem.
Environment variables win over both.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]

_SCRATCH_KEYS = (
    "scratchpads",
    "scratch",
    "scratchpad_root",
    "scratch_root",
    "claude_tmp",
)


def home() -> Path:
    return Path(os.path.expanduser("~"))


def expand(value: str) -> str:
    text = os.path.expanduser(os.path.expandvars(str(value)))
    if "$HOME" in text:
        text = text.replace("$HOME", str(home()))
    return text


def layered_path(stem: str, env: str, directory: Path | None = None) -> Path | None:
    """Local file replaces the example. ``env`` names an optional path override."""
    override = os.environ.get(env)
    if override:
        return Path(expand(override))
    base = directory if directory is not None else TOOL_ROOT
    local = base / f"{stem}.local.json"
    example = base / f"{stem}.example.json"
    plain = base / f"{stem}.json"
    if local.is_file():
        return local
    if example.is_file():
        return example
    if plain.is_file():
        return plain
    return None


def scratch_roots(cfg: dict | None) -> list[str]:
    """Directories named by config. Missing, empty, and unmatched globs yield []."""
    if not isinstance(cfg, dict):
        return []
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    raw = None
    for key in _SCRATCH_KEYS:
        if _present(roots.get(key)):
            raw = roots.get(key)
            break
        if _present(cfg.get(key)):
            raw = cfg.get(key)
            break
    if raw is None:
        return []
    return expand_roots(raw)


def expand_roots(raw) -> list[str]:
    items = _items(raw)
    found = []
    seen = set()
    for item in items:
        text = expand(str(item)).strip()
        if not text:
            continue
        if any(ch in text for ch in "*?["):
            matches = sorted(glob.glob(text))
        elif os.path.isdir(text):
            matches = [text]
        else:
            matches = []
        for match in matches:
            if os.path.isdir(match) and match not in seen:
                seen.add(match)
                found.append(match)
    return found


def _present(value) -> bool:
    if value is None or value == "" or value == []:
        return False
    return True


def _items(raw) -> list:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []
