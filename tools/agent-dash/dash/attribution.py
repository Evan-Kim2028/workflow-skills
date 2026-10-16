"""Workstream from a label, then from the session map when the label is empty."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "map.json"
LABEL_MAX = 200

_lock = threading.Lock()
_cache: dict = {}
_group = re.compile(r"\{(\d+)\}")


def clean_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > LABEL_MAX or "\n" in text or "\r" in text:
        return None
    return text


def attribute(
    label,
    session_id: str | None = None,
    path: Path | None = None,
    parent_session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (workstream, confidence). Confidence is label, regex, or session-default.

    Label rules win. A child run (parent_session_id set) with no label match
    falls back to its parent's session mapping, marked session-default.
    """
    rules = _rules(path)
    text = clean_label(label)
    if text:
        exact = rules["exact"].get(text)
        if exact:
            return exact, "label"
        for pattern, template in rules["regex"]:
            match = pattern.search(text)
            if not match:
                continue
            try:
                workstream = _expand(template, match)
            except IndexError:
                continue
            if workstream:
                return workstream, "regex"
    if isinstance(parent_session_id, str) and parent_session_id.strip():
        mapped = rules["sessions"].get(parent_session_id.strip())
        return (mapped, "session-default") if mapped else (None, None)
    session = session_id.strip() if isinstance(session_id, str) else ""
    mapped = rules["sessions"].get(session)
    if mapped:
        return mapped, "session-default"
    return None, None


def _expand(template: str, match: re.Match) -> str:
    def repl(hit: re.Match) -> str:
        return match.group(int(hit.group(1)))

    return _group.sub(repl, template)


def _rules(path: Path | None) -> dict:
    path = Path(path) if path is not None else _default_map()
    key = str(path)
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        hit = _cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        rules = _read(path)
        _cache[key] = (mtime, rules)
        return rules


def _default_map() -> Path:
    from dash.locate import layered_path

    found = layered_path("map", "AGENT_DASH_MAP")
    return found if found is not None else DEFAULT_PATH


def _builtin() -> dict:
    return {
        "exact": {},
        "regex": [
            ["^ws(\\d+)", "WS{1}"],
            ["WS(\\d+)\\.", "WS{1}"],
            ["^g-", "WS1"],
            ["^dash-", "TOOLS"],
        ],
        "sessions": {},
    }


def _read(path: Path) -> dict:
    empty = {"exact": {}, "regex": [], "sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        data = _builtin()
    except json.JSONDecodeError:
        return empty
    exact = {}
    for label, workstream in (data.get("exact") or {}).items():
        if isinstance(label, str) and isinstance(workstream, str):
            exact[label] = workstream
    compiled = []
    for item in data.get("regex") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        pattern, template = item
        if not isinstance(pattern, str) or not isinstance(template, str):
            continue
        try:
            compiled.append((re.compile(pattern), template))
        except re.error:
            continue
    sessions = {}
    for session_id, workstream in (data.get("sessions") or {}).items():
        if isinstance(session_id, str) and isinstance(workstream, str):
            sessions[session_id] = workstream
    return {"exact": exact, "regex": compiled, "sessions": sessions}
