"""Workstream from a label, then from the session map when the label is empty.

Inheritance (CONTRACT.md "Attribution"): a helper run with no explicit
workstream takes its parent session's workstream (confidence
``inherited``); a lead session with none infers its own from the workers
it launched, its worktree path or branch, or a bundle directory it wrote
to (confidence ``inferred``). Explicit attribution is never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "map.json"
LABEL_MAX = 200

#: Confidences for an explicitly named workstream. Never overwritten by
#: inheritance or inference.
EXPLICIT = frozenset({"label", "regex", "session-default"})
#: A helper run with no workstream takes its parent session's workstream.
INHERITED = "inherited"
#: A lead session's own workstream, inferred from workers, cwd, or writes.
INFERRED = "inferred"

#: Share of a lead's recent workers one workstream must hold to win.
DOMINANT_SHARE = 0.6
#: Fewest attributed workers needed before the dominant share counts.
DOMINANT_MIN_RUNS = 3
#: Lookback for "the workers it launched" (seconds).
WORKER_WINDOW_S = 6 * 3600

#: A branch prefix or worktree path segment naming a workstream,
#: e.g. ``ws2/notes`` or ``train-ws2-x``. ``wt-w2`` must not match.
_WS_SEGMENT = re.compile(r"(?:^|[/_.\-])ws(\d+)(?:[/_.\-]|$)", re.IGNORECASE)
#: A workstream id: a bundle directory name or the TOOLS scope.
_WS_ID = re.compile(r"^(WS\d+|TOOLS)$")

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


def is_explicit(confidence) -> bool:
    """True when an attribution must never be overwritten by inheritance."""
    return isinstance(confidence, str) and confidence in EXPLICIT


def attribute(
    label,
    session_id: str | None = None,
    path: Path | None = None,
    parent_session_id: str | None = None,
    parent_workstream: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (workstream, confidence).

    Explicit rules win with their confidence unchanged: the exact map,
    then the regex map, then the session map. A child run
    (parent_session_id set) with no label match inherits its parent
    session's resolved workstream, marked ``inherited``; without one it
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
        if isinstance(parent_workstream, str) and parent_workstream.strip():
            return parent_workstream.strip(), INHERITED
        mapped = rules["sessions"].get(parent_session_id.strip())
        return (mapped, "session-default") if mapped else (None, None)
    session = session_id.strip() if isinstance(session_id, str) else ""
    mapped = rules["sessions"].get(session)
    if mapped:
        return mapped, "session-default"
    return None, None


def dominant_workstream(
    workstreams,
    *,
    min_runs: int = DOMINANT_MIN_RUNS,
    min_share: float = DOMINANT_SHARE,
) -> str | None:
    """The workstream holding at least ``min_share`` of attributed workers.

    Needs at least ``min_runs`` attributed runs. Unattributed entries are
    ignored. Ties for first place (or a top share under ``min_share``)
    mean no dominance: a lead running several workstreams stays
    unassigned.
    """
    counts: dict[str, int] = {}
    for workstream in workstreams or []:
        if not isinstance(workstream, str) or not workstream.strip():
            continue
        key = workstream.strip()
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    if total < max(1, min_runs):
        return None
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    if ranked[0][1] / total < min_share:
        return None
    return ranked[0][0]


def infer_from_cwd(cwd, train_branches: dict | None = None) -> str | None:
    """A lead workstream from its worktree path or branch.

    First a bundle train's branch name contained in the path (``branch``
    maps to its workstream), then a generic ``ws<N>`` branch prefix or
    path segment. Anything else is not evidence.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    text = cwd.strip()
    if isinstance(train_branches, dict):
        for branch, workstream in train_branches.items():
            if (
                not isinstance(branch, str)
                or not branch
                or not isinstance(workstream, str)
                or not _WS_ID.match(workstream)
            ):
                continue
            # Worktree directories flatten branch slashes: a train branch
            # ``train/notes-cutover`` usually checks out as
            # ``.../train-notes-cutover``.
            if branch in text or branch.replace("/", "-") in text:
                return workstream
    match = _WS_SEGMENT.search(text)
    if match:
        return f"WS{match.group(1)}"
    return None


def bundles_root() -> Path:
    """Where workstream bundles live (env override, else the default)."""
    raw = os.environ.get("AGENT_DASH_WORKSTREAMS")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".local" / "share" / "agent-dash" / "workstreams"


def ws_from_bundle_path(path, root=None) -> str | None:
    """The workstream for a file written under a workstream bundle dir.

    Only the first path segment under the bundles root counts, and only
    when it is itself a workstream id. Never a prompt or argv: callers
    pass file paths from tool-use records, and only the bundle id is
    returned.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    base = Path(root).expanduser() if root is not None else bundles_root()
    try:
        rel = Path(path.strip()).expanduser().relative_to(base)
    except (ValueError, RuntimeError, OSError):
        return None
    first = rel.parts[0] if rel.parts else ""
    return first if _WS_ID.match(first) else None


def resolve_lead(
    workers=None,
    cwd=None,
    hint_workstream: str | None = None,
    train_branches: dict | None = None,
) -> tuple[str | None, str | None]:
    """A lead session's own workstream, in rule order, marked ``inferred``.

    First the dominant workstream of the workers it launched, then the
    worktree path or branch in its cwd, then a workstream bundle
    directory it wrote to. Returns (None, None) when nothing qualifies.
    """
    dominant = dominant_workstream(workers)
    if dominant:
        return dominant, INFERRED
    from_cwd = infer_from_cwd(cwd, train_branches)
    if from_cwd:
        return from_cwd, INFERRED
    if (
        isinstance(hint_workstream, str)
        and _WS_ID.match(hint_workstream.strip())
    ):
        return hint_workstream.strip(), INFERRED
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
