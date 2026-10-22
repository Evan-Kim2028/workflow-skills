"""Plan bundles on disk: discover, validate, scaffold, and update task status.

Content lives under AGENT_DASH_WORKSTREAMS (default ~/.local/share/agent-dash/workstreams).
One directory per bundle. The tool directory stays free of project text.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from dash import errors, maplayout
from dash.plans import PlanNotFound, configured
from dash.plans import load as load_plan
from dash.plans import plan_progress

STATUSES = ("todo", "wip", "pr", "prog", "done", "deferred")
STATUS_MEANING = {
    "todo": "not started",
    "wip": "in progress: being built on a branch, not merged",
    "pr": "waiting to merge: built and approved, or a PR is open",
    "prog": "MERGED to main and waiting on a production step (not 'in progress')",
    "done": "merged and live, nothing left",
    "deferred": "deferred or moved to another workstream (needs --moved-to or --note)",
}
TRAIN_STATUSES = ("planned", "assembling", "gate", "pr", "merged", "dropped")
TRAIN_STATUS_MEANING = {
    "planned": "proposed at planning, members may change",
    "assembling": "member branches being merged into the train branch",
    "gate": "train-level integration checks running",
    "pr": "train PR open",
    "merged": "on main",
    "dropped": "train dissolved, members reassigned",
}
TRAIN_MEMBER_LIMIT = 8
APPROVAL_STATUSES = ("pending", "approved", "denied")
VERDICTS = ("WORTH_IT", "WORTH_IT_IF", "NOT_WORTH_IT")
SIZES = ("S", "M", "L")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_TRAIN_ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MOVED = re.compile(r"^WS\d+$")
_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ID_WORD = re.compile(r"^(?:TU-\d+[a-z]?|ADR|WS\d+[a-z]?|PR)$", re.I)
_CITE = re.compile(
    r"(?P<tok>(?:ADR\s+\d{3,4})|(?:TU-\d+[a-z]?)|(?:#\d{3,5}))(?![A-Za-z0-9])"
)
_MAX_BYTES = 2_000_000

WS_USAGE = """agent-dash workstream bundles

  python3 agent_dash.py ws list [--state S[,S...]|all]
  python3 agent_dash.py ws new <ID> "<title>" [--state planned|active|paused|done|archived|dropped]
  python3 agent_dash.py ws state <ID> <state> [--force] [--note "..."]
  python3 agent_dash.py ws archive <ID> [--force] [--note "..."]
  python3 agent_dash.py ws restore <ID>
  python3 agent_dash.py ws validate <ID>
  python3 agent_dash.py ws render <ID>
  python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."] [--force]
  python3 agent_dash.py ws train <ID> <train-id> [--title T] [--branch B] [--status S] [--add id,id] [--remove id,id] [--pr N] [--shared f1,f2] [--gate "..."] [--note "..."]
  python3 agent_dash.py ws trains <ID> [--json]
  python3 agent_dash.py ws plan <ID> [--json]
  python3 agent_dash.py ws approve <ID> <item-id> <approved|denied|pending> [--note "..."] [--step "..."] [--reversible yes|no] [--conditions "..."]
  python3 agent_dash.py ws approvals [<ID>] [--json]
  python3 agent_dash.py ws validate <ID> [--json]
  python3 agent_dash.py ws next <ID> [--limit N] [--json]
  python3 agent_dash.py ws waves <ID> [--json]
  python3 agent_dash.py ws overlap <ID> [--all] [--cross] [--json]
  python3 agent_dash.py ws gates <ID> [--json]
  python3 agent_dash.py ws deferred [<ID>] [--json]
  python3 agent_dash.py ws layout <ID> [--json]
  python3 agent_dash.py ws linear <link|sync|status|unlink> <ID> [--team KEY] [--json]
  python3 agent_dash.py ws linear comment <ID> <TASK-ID> <TEXT...>
  python3 agent_dash.py ws linear update <ID> [--health on-track|at-risk|off-track] [--dry-run]
  python3 agent_dash.py ws linear login
  python3 agent_dash.py ws linear logout

Status is todo, wip, pr, prog, done, or deferred. deferred may set --moved-to and --note.
Workstream state is planned, active, paused, done, archived, or dropped (default active).
`ws plan` lists the bundle's tasks (ids, titles, status, deps). `--json` prints
machine JSON to stdout; with it, human text goes to stderr.
Exit codes: 0 ok, 1 check failed, 2 usage error, 3 not found, 4 invalid data.
Train status is planned, assembling, gate, pr, merged, or dropped. A task belongs to at most one train.
A new approval is `ws approve <ID> <item-id> pending --step "..." --reversible yes|no --conditions "..."`.
Bundles are directories under AGENT_DASH_WORKSTREAMS.
"""


class Citation:
    __slots__ = ("start", "end", "line", "token")

    def __init__(self, start: int, end: int, line: int, token: str):
        self.start = start
        self.end = end
        self.line = line
        self.token = token


def workstreams_root() -> Path:
    from dash import dataroot as _dataroot

    return _dataroot.workstreams_dir()


def _prose_window(text: str, start: int) -> str:
    """Words that count as a phrase sit on the same line, after the last tag."""
    window = text[max(0, start - 80):start]
    newline = window.rfind("\n")
    if newline >= 0:
        window = window[newline + 1:]
    tag = window.rfind(">")
    if tag >= 0:
        window = window[tag + 1:]
    paren = window.rfind("(")
    if paren >= 0 and ")" not in window[paren:]:
        window = window[:paren]
    return window


def _phrase_before(text: str, start: int) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", _prose_window(text, start))
    words = [word for word in words if len(word) >= 3 and not _ID_WORD.match(word)]
    return len(words) >= 2


def _css_color(text: str, start: int) -> bool:
    window = text[max(0, start - 16):start].lower()
    return window.endswith(("color:", "background:", "fill:", "stroke:", 'fill="', 'stroke="'))


def naming_issues(text: str) -> list[Citation]:
    """Flag TU-n, #nnnn, and ADR nnnn with no phrase earlier on the same line."""
    found = []
    for match in _CITE.finditer(text):
        token = re.sub(r"\s+", " ", match.group("tok"))
        if token.startswith("#") and _css_color(text, match.start()):
            continue
        if _phrase_before(text, match.start()):
            continue
        line = text.count("\n", 0, match.start()) + 1
        found.append(Citation(match.start(), match.end(), line, token))
    return found


def list_bundles(root: Path | None = None) -> list[dict]:
    from dash.lifecycle import DEFAULT_STATE, STATES

    root = Path(root) if root is not None else workstreams_root()
    if not root.is_dir():
        return []
    found = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith(".") or not _SAFE.match(child.name):
            continue
        if child.name in (".", ".."):
            continue
        meta_path = child / "bundle.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        kind = meta.get("kind") if meta.get("kind") in ("program", "workstream") else "workstream"
        order = meta.get("order") if isinstance(meta.get("order"), int) else 0
        title = meta.get("title") if isinstance(meta.get("title"), str) and meta.get("title") else child.name
        short = meta.get("short") if isinstance(meta.get("short"), str) and meta.get("short") else title
        state = meta.get("state") if meta.get("state") in STATES else DEFAULT_STATE
        changed = meta.get("state_changed_at")
        if isinstance(changed, bool) or not isinstance(changed, (int, float)):
            changed = None
        archived = meta.get("archived_at")
        if isinstance(archived, bool) or not isinstance(archived, (int, float)):
            archived = None
        depends = meta.get("depends_on")
        if not isinstance(depends, list) or not all(isinstance(item, str) for item in depends):
            depends = []
        milestone = meta.get("milestone")
        if not isinstance(milestone, str):
            milestone = None
        summary = meta.get("summary")
        if not isinstance(summary, dict):
            summary = None
        found.append(
            {
                "id": child.name,
                "title": title,
                "short": short,
                "order": order,
                "kind": kind,
                "state": state,
                "state_changed_at": changed,
                "archived_at": archived,
                "depends_on": depends,
                "milestone": milestone,
                "summary": summary,
            }
        )
    found.sort(key=lambda item: (0 if item["kind"] == "program" or item["id"] == "OVERVIEW" else 1, item["order"], item["id"]))
    return found


def workstream_plan_ids() -> set[str]:
    ids = set(configured())
    for bundle in list_bundles():
        if bundle["kind"] == "workstream":
            ids.add(bundle["id"])
    return ids


def bundle_dir(ws: str, root: Path | None = None) -> Path | None:
    if not ws or ws in (".", "..") or not _SAFE.match(ws):
        return None
    base = (Path(root) if root is not None else workstreams_root()).resolve()
    path = (base / ws).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return None
    if path.parent != base or not path.is_dir():
        return None
    return path


def _read_json(path: Path, limit: int = _MAX_BYTES):
    try:
        if path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _str_list(value) -> list:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def sanitized_trains(value) -> list:
    """Plan-view shape for the optional top-level trains list. Unknown keys are dropped."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        pr = item.get("pr")
        out.append(
            {
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                "title": item.get("title") if isinstance(item.get("title"), str) else None,
                "branch": item.get("branch") if isinstance(item.get("branch"), str) else None,
                "members": _str_list(item.get("members")),
                "status": item.get("status") if isinstance(item.get("status"), str) else None,
                "pr": pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
                "shared_files": _str_list(item.get("shared_files")),
                "gate": item.get("gate") if isinstance(item.get("gate"), str) else None,
                "note": item.get("note") if isinstance(item.get("note"), str) else None,
            }
        )
    return out


def resolve_plan(ws: str) -> tuple[str | None, dict | None]:
    """The one plan resolver every endpoint uses: a bundle's plan.json wins over
    the plans-map file. Returns ``(source, body)`` with source ``bundle`` or
    ``map``, or ``(None, None)`` when neither has the workstream."""
    body = bundle_plan_view(ws)
    if body is not None:
        return "bundle", body
    try:
        return "map", load_plan(ws)
    except PlanNotFound:
        return None, None


def load_bundle_api(ws: str) -> dict | None:
    path = bundle_dir(ws)
    if path is None:
        return None
    meta = _read_json(path / "bundle.json")
    plan = _read_json(path / "plan.json")
    if not isinstance(meta, dict) or not isinstance(plan, dict):
        return None
    plan = dict(plan)
    plan["trains"] = sanitized_trains(plan.get("trains"))
    dag = plan.get("dag") if isinstance(plan.get("dag"), dict) else {}
    approvals = approvals_for(ws)
    return {
        "bundle": meta,
        "plan": plan,
        "progress": plan_progress(dag.get("groups")),
        "approvals": approvals if approvals is not None else empty_approvals(ws),
        "start_prompt": start_prompt_text(ws),
        "issues": validate_bundle(ws),
    }


def _view_groups(value, links: dict | None = None) -> list:
    """Plan groups as the dashboard view carries them. ``links`` maps task id
    to its Linear ``{identifier, url}``; a task with an entry gains ``linear``."""
    if not isinstance(value, list):
        return []
    links = links or {}
    groups = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tasks = []
        for task in item.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            row = {"id": task.get("id"), "title": task.get("title"), "status": task.get("status")}
            if task.get("moved_to"):
                row["moved_to"] = task.get("moved_to")
            if task.get("note"):
                row["note"] = task.get("note")
            deps = task.get("deps")
            if isinstance(deps, list):
                row["deps"] = [item for item in deps if isinstance(item, str)]
            if isinstance(task.get("kind"), str):
                row["kind"] = task.get("kind")
            notes = task.get("dep_notes")
            if isinstance(notes, dict):
                row["dep_notes"] = {key: value for key, value in notes.items() if isinstance(key, str) and isinstance(value, str)}
            if isinstance(task.get("wave"), str):
                row["wave"] = task.get("wave")
            files = task.get("files")
            if isinstance(files, list):
                row["files"] = [item for item in files if isinstance(item, str)]
            if isinstance(task.get("signal"), str):
                row["signal"] = task.get("signal")
            if isinstance(task.get("confirmed_at"), str):
                row["confirmed_at"] = task.get("confirmed_at")
            fresh = task.get("fresh_hours")
            if isinstance(fresh, (int, float)) and not isinstance(fresh, bool):
                row["fresh_hours"] = fresh
            if isinstance(task.get("received_as"), str):
                row["received_as"] = task.get("received_as")
            if isinstance(row["id"], str) and row["id"] in links:
                row["linear"] = links[row["id"]]
            tasks.append(row)
        groups.append({"id": item.get("id"), "col": item.get("col"), "title": item.get("title"), "tasks": tasks})
    return groups


#: ``bundle_plan_view`` results keyed by ``(base, ws)``: the entry also
#: holds the ``(mtime_ns, size)`` of every file the view reads, so an edit
#: to any of them misses precisely and an untouched bundle never re-reads
#: or re-validates. Entries are deep copies on the way out: ``/api/plan``
#: mutates the served body in place.
_PLAN_VIEW_CACHE: dict = {}
_PLAN_VIEW_LOCK = None  # lazy threading.Lock, so import stays side-effect free


def _plan_view_lock():
    global _PLAN_VIEW_LOCK
    if _PLAN_VIEW_LOCK is None:
        import threading

        _PLAN_VIEW_LOCK = threading.Lock()
    return _PLAN_VIEW_LOCK


def _file_stamp(path: Path):
    """``(mtime_ns, size)`` for a cache key, or None when unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def plans_map_path() -> Path | None:
    """The plans-map file ``resolve_plan`` falls back to, best effort."""
    try:
        from dash.locate import layered_path
    except Exception:
        layered_path = None
    if layered_path is not None:
        try:
            found = layered_path("plans", "AGENT_DASH_PLANS")
        except Exception:
            found = None
        if found is not None:
            return Path(found)
    try:
        from dash.plans import DEFAULT_PATH
    except Exception:
        return None
    return Path(DEFAULT_PATH)


def plans_map_stamps() -> tuple:
    """Stamps of the plans-map file and the plan files it points at.

    Best effort: an unreadable map contributes its (missing) stamp only.
    """
    try:
        path = plans_map_path()
    except Exception:
        return (None,)
    stamps = [_file_stamp(path) if path is not None else None]
    if path is None:
        return tuple(stamps)
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return tuple(stamps)
    targets = mapping.get("plans") if isinstance(mapping, dict) else None
    if isinstance(targets, dict):
        for target in targets.values():
            if isinstance(target, str) and target:
                stamps.append(_file_stamp(Path(target)))
    return tuple(stamps)


def plans_stamp(root: Path | None = None) -> tuple:
    """``(mtime_ns, size)`` of every bundle ``plan.json``, ``bundle.json`` and
    ``linear.json``.

    The workstream-row cache keys on this: row progress reads the plans
    and fallback rows read bundle titles (lifecycle state rides the
    caller's metadata map), so a bundle edit retags precisely and
    untouched bundles never re-read.
    """
    from dash import linear

    base = Path(root) if root is not None else workstreams_root()
    try:
        children = sorted(
            path for path in base.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and (path / "bundle.json").is_file()
        )
    except OSError:
        return ()
    stamps = [
        (_file_stamp(child / "plan.json"), _file_stamp(child / "bundle.json"), linear.stamp(child.name, base))
        for child in children
    ]
    try:
        stamps.append(plans_map_stamps())
    except Exception:
        pass
    return tuple(stamps)


def bundle_plan_view(ws: str) -> dict | None:
    """Dashboard plan shape for a bundle that is not listed in plans.json.

    Cached by the ``(mtime_ns, size)`` of ``plan.json``, ``bundle.json``,
    ``approvals.json``, and ``START-PROMPT.md``: the hot paths
    (``/api/state`` progress, risks, workstream rows) read a dozen plans
    per request, and the validation below re-reads all three JSON files.
    """
    import copy

    from dash import linear

    path = bundle_dir(ws)
    if path is None:
        return None
    base = str(path.parent)
    stamps = tuple(_file_stamp(path / name) for name in ("plan.json", "bundle.json", "approvals.json", "START-PROMPT.md"))
    stamps += (linear.stamp(ws),)
    lock = _plan_view_lock()
    with lock:
        hit = _PLAN_VIEW_CACHE.get((base, ws))
        if hit is not None and hit[0] == stamps:
            return copy.deepcopy(hit[1])
    data = _read_json(path / "plan.json")
    if not isinstance(data, dict):
        return None
    dag = data.get("dag") if isinstance(data.get("dag"), dict) else {}
    narrative = data.get("narrative") if isinstance(data.get("narrative"), dict) else {}
    cols = [item for item in dag.get("cols") or [] if isinstance(item, str)]
    edges = []
    for item in dag.get("edges") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            edges.append([item[0], item[1]])
    phases = []
    for item in narrative.get("phases") or []:
        if isinstance(item, dict):
            phases.append({"name": item.get("name"), "goal": item.get("goal"), "gate": item.get("gate")})
    groups = _view_groups(dag.get("groups"), linear.task_links(ws))
    view = {
        "ws": ws,
        "cols": cols,
        "groups": groups,
        "edges": edges,
        "phases": phases,
        "trains": sanitized_trains(data.get("trains")),
        "progress": plan_progress(groups),
        "start_prompt": start_prompt_text(ws),
        "issues": validate_bundle(ws),
    }
    page_link = linear.page_link(ws)
    if page_link is not None:
        view["linear"] = page_link
    with lock:
        if len(_PLAN_VIEW_CACHE) > 256:
            _PLAN_VIEW_CACHE.clear()
        _PLAN_VIEW_CACHE[(base, ws)] = (stamps, view)
    return copy.deepcopy(view)


def read_page(ws: str) -> bytes | None:
    path = bundle_dir(ws)
    if path is None:
        return None
    page = (path / "page.html").resolve()
    try:
        page.relative_to(path)
    except ValueError:
        return None
    if page.parent != path or not page.is_file():
        return None
    try:
        if page.stat().st_size > _MAX_BYTES:
            return None
        return page.read_bytes()
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _bundle_lock(path: Path):
    """Cross-process exclusive lock for one bundle's read-modify-write cycles.

    ``_atomic_write`` makes each write atomic, not the read-modify-write
    cycle: two ``ws task`` writers in the same second would both read the
    same ``plan.json`` and the last ``os.replace`` would silently drop the
    other's change. Holding this lock across the read and the write keeps
    every update. History appends stay outside the lock (one O_APPEND line
    is already atomic).
    """
    path.mkdir(parents=True, exist_ok=True)
    with open(path / ".lock", "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _template_plan(ws: str) -> dict:
    return {
        "workstream": ws,
        "decisions": [
            {
                "id": "D1",
                "question": "What is the open question?",
                "decision": "State the decision in one imperative sentence.",
                "alternatives": [{"option": "The other option", "why_not": "Why it lost."}],
                "evidence": ["where the evidence lives"],
                "why": "Why this decision.",
                "risk": "What breaks if this is wrong.",
                "reversible": True,
                "review_needed": "What a reviewer checks.",
            }
        ],
        "motivation": {
            "today": [{"pain": "What hurts today.", "measure": "A number with a unit."}],
            "after": [{"gain": "What is better after.", "measure": "A number with a unit."}],
            "costs": [{"cost": "What it costs.", "measure": "A number with a unit."}],
            "not_better": ["Where this is not better."],
            "verdict": "WORTH_IT_IF",
            "verdict_reason": "Say why, in a few sentences.",
            "conditions": ["What has to be true."],
        },
        "agent_friendly": {
            "litmus": "One measurable sentence for the exit criterion.",
            "litmus_check": "The command or test that proves it.",
            "requirements": [
                {
                    "n": 1,
                    "title": "First requirement",
                    "mechanism": "The file, test, or command that enforces it.",
                    "verify": "the command to run",
                }
            ],
            "new_tasks": [{"id": f"{ws}.1", "title": "First task", "phase": "First section", "size": "S"}],
        },
        "dag": {
            # One starting section: the planner adds the 4 to 5 columns
            # from the map checklist as the plan takes shape, so a fresh
            # bundle carries zero MAP warnings.
            "cols": ["Shape"],
            "groups": [
                {
                    "id": "start",
                    "col": 0,
                    "title": "First section",
                    "tasks": [{"id": f"{ws}.1", "title": "First task", "status": "todo", "deps": []}],
                }
            ],
            "edges": [],
        },
        "narrative": {
            "thesis": "One sentence on why this workstream exists.",
            "end_state": "What is true when this workstream is done.",
            "phases": [
                {
                    "name": "First section",
                    "goal": "What this section delivers.",
                    "gate": "What must be true to leave it.",
                }
            ],
        },
        "trains": [],
    }


def _template_layout() -> dict:
    return {
        "why_title": "Why this workstream",
        "why_visual": [],
        "end_title": "End state",
        "end_body": [],
        "plan_title": "Plan",
        "plan_extra": [],
        "agent_title": "Agent-friendly",
        "agent_visual": [],
        "review_note": "Each decision names the evidence a reviewer can check.",
        "ref_sections": [],
    }


def _template_start_prompt(ws: str, title: str) -> str:
    lower = ws.lower()
    return (
        f"You are the {ws} ({title}) lead.\n"
        "\n"
        "Read this bundle's kickoff brief, KICKOFF.md, when that file is there, and run the workstream from it.\n"
        "The plan, the waves, and the done criteria live in the bundle.\n"
        "\n"
        f"Label each worker run `{lower}-<task-id>-<slug>`.\n"
        f"Move a task with `python3 agent_dash.py ws task {ws} <task-id> <status>`.\n"
        f"Keep the planned merge trains current with `python3 agent_dash.py ws train {ws} <train-id> --title ...`.\n"
        f"Read `ws approvals {ws}` before any production step. Run one production step at a time.\n"
        "\n"
        f"The plan view for {ws} is on the local dashboard.\n"
    )


def _stable_tool_dir(tool_dir: Path) -> Path:
    """The tool directory, written through ``releases/CURRENT`` when it is a release.

    A deployed tool runs from ``<X>/releases/<sha>/tools/agent-dash`` (or from a
    flat ``<X>/releases/<sha>``). When ``<X>/releases/CURRENT`` resolves to that
    same release, the path goes through the stable link so it survives the next
    deploy. Any other directory keeps its own path.
    """
    tool_dir = Path(tool_dir)
    candidates = []
    if tool_dir.parent.name == "tools" and len(tool_dir.parents) >= 2:
        release = tool_dir.parents[1]
        candidates.append((release, tool_dir.relative_to(release)))
    candidates.append((tool_dir, Path(".")))
    for release, rel in candidates:
        current = release.parent / "CURRENT"
        if release.parent.name != "releases" or not current.exists():
            continue
        if os.path.realpath(current) == os.path.realpath(release):
            return current / rel
    return tool_dir


def linear_start_section(ws: str, root: Path | None = None) -> str:
    """The generated Linear section appended to the start prompt of a linked bundle.

    It names the running tool's ``agent_dash.py`` by absolute path, so the
    commands work from any cwd. It is generated, never written into START-PROMPT.md.
    """
    from dash import linear

    page = linear.page_link(ws, root) or {}
    url = page.get("url") or "not synced yet; run the status command below"
    tool = shlex.quote(str(_stable_tool_dir(Path(__file__).resolve().parents[1]) / "agent_dash.py"))
    py = f"python3 {tool}"
    return "\n".join(
        [
            "## Linear",
            "",
            f"Project: {url}",
            "Status syncs both ways within a minute; the plan owns titles, descriptions and the tracker section.",
            "",
            "Your duties for this workstream:",
            f"1. Change task status only with `{py} ws task {ws} <task-id> <status>`.",
            f"2. After each worker verdict, post one line with `{py} ws linear comment {ws} <task-id> \"<verdict> <PR link>\"`.",
            f"3. Put the `Fixes ...` line from `{py} ws trains {ws}` in every train PR body.",
            f"4. Post `{py} ws linear update {ws}` before standup, or rely on `tracker.update.daily_hour` when it is set. Add `--health at-risk` or `--health off-track` when the workstream is at risk.",
            f"5. At the start of each session, check `{py} ws linear status {ws}` and `{py} doctor`.",
            "6. Read issues with a Linear MCP tool when one is available. Never edit issue titles or descriptions in Linear.",
            f"7. Approvals labelled `approval` are decided by the owner in Linear or with `{py} ws approve {ws} <item-id> approved|denied`.",
            "",
        ]
    )


def start_prompt_text(ws: str, root: Path | None = None) -> str | None:
    """Text of START-PROMPT.md, or None when the bundle has no such file.

    A Linear-linked bundle gets the generated Linear section appended; an
    unlinked bundle gets the file text unchanged.
    """
    path = bundle_dir(ws, root)
    if path is None:
        return None
    file = path / "START-PROMPT.md"
    try:
        if not file.is_file() or file.stat().st_size > _MAX_BYTES:
            return None
        text = file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    from dash import linear

    if not linear.is_linked(ws, root):
        return text
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + linear_start_section(ws, root)


def cmd_new(ws: str, title: str, root: Path | None = None, state: str = "active") -> int:
    import time as _time

    from dash.lifecycle import STATES

    if not _ID.match(ws):
        print(f"bad bundle id {ws!r}: use 1-32 chars, letters, digits, _ or -", flush=True)
        return errors.USAGE
    if not title or not title.strip():
        print("title required: pass a quoted title, e.g. python3 agent_dash.py ws new WS4 \"Notes index\"", flush=True)
        return errors.USAGE
    if state not in STATES:
        print(f"unknown state {state!r}. Use one of: {', '.join(STATES)}", flush=True)
        return errors.USAGE
    base = Path(root) if root is not None else workstreams_root()
    dest = base / ws
    if dest.exists():
        print(f"bundle {ws} already exists at {dest}. Run: python3 agent_dash.py ws validate {ws}", flush=True)
        return errors.USAGE
    orders = [item["order"] for item in list_bundles(base)]
    order = (max(orders) if orders else 0) + 10
    dest.mkdir(parents=True)
    _atomic_write(
        dest / "bundle.json",
        json.dumps(
            {
                "id": ws,
                "title": title.strip(),
                "short": title.strip(),
                "order": order,
                "kind": "workstream",
                "state": state,
                "state_changed_at": _time.time(),
            },
            indent=2,
        )
        + "\n",
    )
    _atomic_write(dest / "plan.json", json.dumps(_template_plan(ws), indent=2) + "\n")
    _atomic_write(dest / "layout.json", json.dumps(_template_layout(), indent=2) + "\n")
    _atomic_write(dest / "START-PROMPT.md", _template_start_prompt(ws, title.strip()))
    _atomic_write(
        dest / "visuals.html",
        '<header>\n<p class="thesis">Replace this thesis. Render copies narrative.thesis over it.</p>\n</header>\n',
    )
    print(f"created {dest}")
    return 0


def _require_str(issues: list[str], label: str, value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty string")


def _validate_dag(issues: list[str], dag) -> None:
    if not isinstance(dag, dict):
        issues.append("plan.json: dag must be an object")
        return
    cols = dag.get("cols")
    if not isinstance(cols, list) or not cols or not all(isinstance(item, str) and item for item in cols):
        issues.append("plan.json: dag.cols must be a non-empty list of strings")
        cols = []
    groups = dag.get("groups")
    if not isinstance(groups, list):
        issues.append("plan.json: dag.groups must be a list")
        return
    seen = set()
    ids = set()
    for group in groups:
        if not isinstance(group, dict):
            issues.append("plan.json: each section (a dag.groups entry) must be an object")
            continue
        gid = group.get("id")
        _require_str(issues, "plan.json", gid, "section id")
        if isinstance(gid, str):
            if gid in ids:
                issues.append(f"plan.json: duplicate section id {gid}")
            ids.add(gid)
        col = group.get("col")
        if not isinstance(col, int) or isinstance(col, bool) or col < 0 or (cols and col >= len(cols)):
            issues.append(f"plan.json: section {gid} col is outside dag.cols")
        _require_str(issues, "plan.json", group.get("title"), f"section {gid} title")
        tasks = group.get("tasks")
        if not isinstance(tasks, list):
            issues.append(f"plan.json: section {gid} tasks must be a list")
            continue
        for task in tasks:
            if not isinstance(task, dict):
                issues.append(f"plan.json: section {gid} has a task that is not an object")
                continue
            tid = task.get("id")
            _require_str(issues, "plan.json", tid, "task id")
            _require_str(issues, "plan.json", task.get("title"), f"task {tid} title")
            if isinstance(tid, str):
                if tid in seen:
                    issues.append(f"plan.json: duplicate task id {tid}")
                seen.add(tid)
            status = task.get("status")
            if status not in STATUSES:
                issues.append(f"plan.json: task {tid} status {status!r} is not {', '.join(STATUSES)}")
            kind = task.get("kind")
            if kind is not None and kind not in ("task", "gate"):
                issues.append(f'plan.json: task {tid} kind {kind!r} is not "task" or "gate"')
            if status == "deferred":
                moved = task.get("moved_to")
                if moved is not None and (not isinstance(moved, str) or not _MOVED.match(moved)):
                    issues.append(f"plan.json: task {tid} moved_to must look like WS2")
                if "note" in task and not isinstance(task.get("note"), str):
                    issues.append(f"plan.json: task {tid} note must be a string")
            if "files" in task:
                files = task.get("files")
                if not isinstance(files, list) or not files or not all(isinstance(item, str) and item.strip() for item in files):
                    issues.append(f"plan.json: task {tid} files must be a non-empty list of glob strings")
            if "signal" in task and (not isinstance(task.get("signal"), str) or not task.get("signal").strip()):
                issues.append(f"plan.json: task {tid} signal must be a non-empty string")
            if "confirmed_at" in task:
                from dash.orchestrate import _parse_when

                if not isinstance(task.get("confirmed_at"), str) or _parse_when(task.get("confirmed_at")) is None:
                    issues.append(f"plan.json: task {tid} confirmed_at must be an ISO-8601 UTC string")
            if "fresh_hours" in task:
                fresh = task.get("fresh_hours")
                if isinstance(fresh, bool) or not isinstance(fresh, (int, float)) or not float(fresh) > 0:
                    issues.append(f"plan.json: task {tid} fresh_hours must be a number greater than 0")
            if "received_as" in task and (not isinstance(task.get("received_as"), str) or not task.get("received_as").strip()):
                issues.append(f"plan.json: task {tid} received_as must be a non-empty string")
    for group in groups:
        for task in (group.get("tasks") if isinstance(group, dict) and isinstance(group.get("tasks"), list) else []):
            if not isinstance(task, dict):
                continue
            tid = task.get("id")
            notes = task.get("dep_notes")
            if notes is not None:
                _validate_dep_notes(issues, tid, task.get("deps"), notes)
            if "deps" not in task:
                continue
            deps = task.get("deps")
            if not isinstance(deps, list) or not all(isinstance(item, str) for item in deps):
                issues.append(f"plan.json: task {task.get('id')} deps must be a list of task ids")
                continue
            for dep in deps:
                if dep not in seen:
                    issues.append(f"plan.json: task {task.get('id')} waits on unknown task {dep}")
                elif dep == task.get("id"):
                    issues.append(f"plan.json: task {dep} waits on itself")
    edges = dag.get("edges")
    if not isinstance(edges, list):
        issues.append("plan.json: dag.edges must be a list")
        return
    for edge in edges:
        if not isinstance(edge, (list, tuple)) or len(edge) < 2:
            issues.append("plan.json: each edge must be a pair")
            continue
        if edge[0] not in ids or edge[1] not in ids:
            issues.append(f"plan.json: edge {edge[0]} -> {edge[1]} names an unknown group")


def _validate_dep_notes(issues: list[str], tid, deps, notes) -> None:
    """dep_notes keys must name one of the task's own deps; values are short prose."""
    if not isinstance(notes, dict):
        issues.append(f"plan.json: task {tid} dep_notes must be an object")
        return
    known = deps if isinstance(deps, list) and all(isinstance(item, str) for item in deps) else []
    for key, value in notes.items():
        if key not in known:
            issues.append(f"plan.json: task {tid} dep_notes for {key} is not one of its deps")
        if not isinstance(value, str) or not value.strip():
            issues.append(f"plan.json: task {tid} dep_notes for {key} must be a non-empty string")
        elif len(value) > 280:
            issues.append(f"plan.json: task {tid} dep_notes for {key} must be a short string (280 chars or fewer)")
        else:
            for hit in naming_issues(value):
                issues.append(f"plan.json: task {tid} dep_notes for {key}: bare citation {hit.token}")


def _validate_decisions(issues: list[str], decisions) -> None:
    if not isinstance(decisions, list):
        issues.append("plan.json: decisions must be a list")
        return
    required = ("id", "question", "decision", "alternatives", "evidence", "why", "risk", "reversible", "review_needed")
    for item in decisions:
        if not isinstance(item, dict):
            issues.append("plan.json: each decision must be an object")
            continue
        for key in required:
            if key not in item:
                issues.append(f"plan.json: decision {item.get('id')} missing {key}")
        if "reversible" in item and not isinstance(item.get("reversible"), bool):
            issues.append(f"plan.json: decision {item.get('id')} reversible must be true or false")
        alts = item.get("alternatives")
        if not isinstance(alts, list):
            continue
        for alt in alts:
            if not isinstance(alt, dict) or not isinstance(alt.get("option"), str) or not isinstance(alt.get("why_not"), str):
                issues.append(f"plan.json: decision {item.get('id')} alternative needs option and why_not")


def _validate_motivation(issues: list[str], mot) -> None:
    if not isinstance(mot, dict):
        issues.append("plan.json: motivation must be an object")
        return
    if mot.get("verdict") not in VERDICTS:
        issues.append("plan.json: motivation.verdict must be WORTH_IT, WORTH_IT_IF, or NOT_WORTH_IT")
    _require_str(issues, "plan.json", mot.get("verdict_reason"), "motivation.verdict_reason")
    for key, left, right in (("today", "pain", "measure"), ("after", "gain", "measure"), ("costs", "cost", "measure")):
        rows = mot.get(key)
        if not isinstance(rows, list):
            issues.append(f"plan.json: motivation.{key} must be a list")
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get(left), str) or not isinstance(row.get(right), str):
                issues.append(f"plan.json: motivation.{key} rows need {left} and {right}")
    if not isinstance(mot.get("not_better"), list):
        issues.append("plan.json: motivation.not_better must be a list")


def _validate_agent(issues: list[str], agent) -> None:
    if not isinstance(agent, dict):
        issues.append("plan.json: agent_friendly must be an object")
        return
    _require_str(issues, "plan.json", agent.get("litmus"), "agent_friendly.litmus")
    _require_str(issues, "plan.json", agent.get("litmus_check"), "agent_friendly.litmus_check")
    reqs = agent.get("requirements")
    if not isinstance(reqs, list):
        issues.append("plan.json: agent_friendly.requirements must be a list")
        return
    for item in reqs:
        if not isinstance(item, dict):
            issues.append("plan.json: a requirement is not an object")
            continue
        if not isinstance(item.get("n"), int) or isinstance(item.get("n"), bool):
            issues.append("plan.json: requirement n must be an integer")
        for key in ("title", "mechanism", "verify"):
            _require_str(issues, "plan.json", item.get(key), f"requirement {key}")
    tasks = agent.get("new_tasks") or []
    if not isinstance(tasks, list):
        issues.append("plan.json: agent_friendly.new_tasks must be a list")
        return
    for item in tasks:
        if not isinstance(item, dict):
            issues.append("plan.json: a new task is not an object")
            continue
        for key in ("id", "title", "phase"):
            _require_str(issues, "plan.json", item.get(key), f"new task {key}")
        if item.get("size") not in SIZES:
            issues.append(f"plan.json: new task {item.get('id')} size must be S, M, or L")


def _validate_narrative(issues: list[str], narrative, require: bool) -> None:
    if narrative is None and not require:
        return
    if not isinstance(narrative, dict):
        issues.append("plan.json: narrative must be an object")
        return
    phases = narrative.get("phases")
    if phases is None:
        return
    if not isinstance(phases, list):
        issues.append("plan.json: narrative.phases must be a list")
        return
    for phase in phases:
        if not isinstance(phase, dict):
            issues.append("plan.json: each section (a narrative.phases entry) must be an object")
            continue
        for key in ("name", "goal", "gate"):
            _require_str(issues, "plan.json", phase.get(key), f"section {key}")


def empty_approvals(ws: str) -> dict:
    return {"ws": ws, "decided_at": None, "decided_by": None, "items": []}


def _public_approvals(data: dict, ws: str) -> dict:
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    decided_at = data.get("decided_at")
    decided_by = data.get("decided_by")
    return {
        "ws": data.get("ws") if isinstance(data.get("ws"), str) else ws,
        "decided_at": decided_at if isinstance(decided_at, str) else None,
        "decided_by": decided_by if isinstance(decided_by, str) else None,
        "items": items,
    }


def approvals_for(ws: str, root: Path | None = None) -> dict | None:
    """None when the bundle directory is missing. An empty record when the file is absent.

    Cached by the ``approvals.json`` ``(mtime_ns, size)``: risks and the
    approvals roll-up read every bundle's file per request. Copies on the
    way out so callers never mutate the cached record.
    """
    import copy

    path = bundle_dir(ws, root)
    if path is None:
        return None
    file = path / "approvals.json"
    stamp = _file_stamp(file)
    lock = _plan_view_lock()
    with lock:
        hit = _PLAN_VIEW_CACHE.get(("approvals", str(path), ws))
        if hit is not None and hit[0] == stamp:
            return copy.deepcopy(hit[1])
    if not file.is_file():
        record = empty_approvals(ws)
    else:
        data = _read_json(file)
        if not isinstance(data, dict):
            record = empty_approvals(ws)
        else:
            record = _public_approvals(data, ws)
    with lock:
        if len(_PLAN_VIEW_CACHE) > 256:
            _PLAN_VIEW_CACHE.clear()
        _PLAN_VIEW_CACHE[("approvals", str(path), ws)] = (stamp, record)
    return copy.deepcopy(record)


def approvals_all(root: Path | None = None) -> list[dict]:
    rows = []
    for bundle in list_bundles(root):
        record = approvals_for(bundle["id"], root)
        if record is not None:
            rows.append(record)
    return rows


def _validate_approvals(issues: list[str], ws: str, data) -> None:
    if not isinstance(data, dict):
        issues.append("approvals.json: must be an object")
        return
    if data.get("ws") != ws:
        issues.append(f"approvals.json: ws {data.get('ws')!r} does not match directory {ws}")
    if "decided_at" not in data:
        issues.append("approvals.json: decided_at is required")
    elif data.get("decided_at") is not None and not isinstance(data.get("decided_at"), str):
        issues.append("approvals.json: decided_at must be a string or null")
    if "decided_by" not in data:
        issues.append("approvals.json: decided_by is required")
    elif data.get("decided_by") is not None and not isinstance(data.get("decided_by"), str):
        issues.append("approvals.json: decided_by must be a string or null")
    items = data.get("items")
    if not isinstance(items, list):
        issues.append("approvals.json: items must be a list")
        return
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            issues.append("approvals.json: each item must be an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not _ITEM.match(item_id):
            issues.append(f"approvals.json: bad item id {item_id!r}")
        elif item_id in seen:
            issues.append(f"approvals.json: duplicate item id {item_id}")
        else:
            seen.add(item_id)
        _require_str(issues, "approvals.json", item.get("step"), f"item {item_id} step")
        if item.get("status") not in APPROVAL_STATUSES:
            issues.append(f"approvals.json: item {item_id} status must be pending, approved, or denied")
        if not isinstance(item.get("reversible"), bool):
            issues.append(f"approvals.json: item {item_id} reversible must be true or false")
        if not isinstance(item.get("conditions"), str):
            issues.append(f"approvals.json: item {item_id} conditions must be a string")
        if "note" in item and not isinstance(item.get("note"), str):
            issues.append(f"approvals.json: item {item_id} note must be a string")


def _cell(value) -> str:
    return str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")


def set_approval(
    ws: str,
    item_id: str,
    status: str,
    step: str | None,
    reversible: bool | None,
    conditions: str | None,
    note: str | None,
    root: Path | None = None,
    origin: str = "local",
) -> int:
    """Set one approval item's status and write approvals.json.

    ``origin="local"`` (the owner's ``ws approve``) records the change in the
    Linear outbox while the bundle ``.lock`` is held, so a concurrent pull
    either sees the row or has not read the old value yet. ``origin="linear"``
    (a pull) writes nothing and returns ``errors.SKIPPED_PENDING`` when a local
    row for the item is still in the outbox; the local decision wins and
    reaches Linear on the next flush. Decisions made with ``origin="linear"``
    record ``decided_by="linear"``, others ``"owner"``.
    """
    from dash import linear

    if not _ITEM.match(item_id):
        print(f"bad approval id {item_id!r}: use letters, digits, _, ., - (max 64 chars)", flush=True)
        return errors.USAGE
    if status not in APPROVAL_STATUSES:
        print(f"unknown approval status {status!r}. Use one of: {', '.join(APPROVAL_STATUSES)}", flush=True)
        return errors.USAGE
    path = bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    file = path / "approvals.json"
    with _bundle_lock(path):
        if origin == "linear" and linear.has_pending(ws, "approval", item_id, root):
            return errors.SKIPPED_PENDING
        if file.is_file():
            data = _read_json(file)
            if not isinstance(data, dict):
                print(errors.approvals_unreadable(ws), flush=True)
                return errors.INVALID_DATA
        else:
            data = empty_approvals(ws)
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        found = None
        for item in items:
            if isinstance(item, dict) and item.get("id") == item_id:
                found = item
                break
        if found is None:
            if step is None or not str(step).strip():
                print(f"step required for a new approval. Run: python3 agent_dash.py ws approve {ws} {item_id} pending --step \"...\" --reversible yes|no --conditions \"...\"", flush=True)
                return errors.USAGE
            if reversible is None:
                print(f"reversible required for a new approval. Run: python3 agent_dash.py ws approve {ws} {item_id} pending --step \"...\" --reversible yes|no --conditions \"...\"", flush=True)
                return errors.USAGE
            found = {
                "id": item_id,
                "step": str(step).strip(),
                "status": status,
                "reversible": reversible,
                "conditions": conditions or "",
            }
            if note is not None:
                found["note"] = note
            items.append(found)
        else:
            found["status"] = status
            if step is not None:
                found["step"] = step
            if reversible is not None:
                found["reversible"] = reversible
            if conditions is not None:
                found["conditions"] = conditions
            if note is not None:
                found["note"] = note
        if status in ("approved", "denied"):
            data["decided_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data["decided_by"] = "linear" if origin == "linear" else "owner"
        else:
            data.setdefault("decided_at", None)
            data.setdefault("decided_by", None)
        payload = {
            "ws": ws,
            "decided_at": data.get("decided_at"),
            "decided_by": data.get("decided_by"),
            "items": items,
        }
        _atomic_write(file, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        if origin == "local":
            linear.enqueue(ws, "approval", item_id, root=root)
    print(f"{ws} {item_id} {status}")
    return 0


def _say(text: str, json_mode: bool = False) -> None:
    """Human text: stdout normally, stderr when ``--json`` is set."""
    import sys as _sys

    print(text, file=_sys.stderr if json_mode else _sys.stdout, flush=True)


def print_approvals(ws: str | None = None, root: Path | None = None, json_mode: bool = False) -> int:
    if ws:
        if bundle_dir(ws, root) is None:
            _say(errors.bundle_not_found(ws), json_mode)
            return errors.NOT_FOUND
        records = [approvals_for(ws, root)]
    else:
        records = approvals_all(root)
    records = [record for record in records if record]
    if json_mode:
        payload: dict = {"approvals": records}
        if ws:
            payload["ws"] = ws
        errors.emit_json(payload)
        return 0
    lines = []
    for record in records:
        for item in record.get("items") or []:
            if not isinstance(item, dict):
                continue
            way = "reversible" if item.get("reversible") else "one-way"
            lines.append(
                "\t".join(
                    [
                        _cell(record.get("ws")),
                        _cell(item.get("id")),
                        _cell(item.get("status")),
                        way,
                        _cell(item.get("step")),
                        _cell(item.get("conditions")),
                    ]
                )
            )
    if not lines:
        print("(no approvals)")
        return 0
    print("WS\tID\tSTATUS\tWAY\tSTEP\tCONDITIONS")
    for line in lines:
        print(line)
    return 0


def _parse_yes_no(value: str) -> bool | None:
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _dag_task_ids(plan: dict) -> set[str]:
    ids = set()
    dag = plan.get("dag")
    if not isinstance(dag, dict):
        return ids
    for group in dag.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for task in group.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                ids.add(task["id"])
    return ids


def _validate_trains(issues: list[str], plan: dict) -> None:
    trains = plan.get("trains")
    if trains is None:
        return
    if not isinstance(trains, list):
        issues.append("plan.json: trains must be a list")
        return
    known = _dag_task_ids(plan)
    seen: set[str] = set()
    owner: dict[str, str] = {}
    for item in trains:
        if not isinstance(item, dict):
            issues.append("plan.json: each train must be an object")
            continue
        tid = item.get("id")
        if not isinstance(tid, str) or not _TRAIN_ID.match(tid):
            issues.append(f"plan.json: bad train id {tid!r}")
            continue
        if tid in seen:
            issues.append(f"plan.json: duplicate train id {tid}")
        seen.add(tid)
        _require_str(issues, "plan.json", item.get("title"), f"train {tid} title")
        branch = item.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch.startswith("train/")):
            issues.append(f'plan.json: train {tid} branch must start with "train/"')
        status = item.get("status")
        if status not in TRAIN_STATUSES:
            issues.append(f"plan.json: train {tid} status {status!r} is not {', '.join(TRAIN_STATUSES)}")
        members = item.get("members")
        if not isinstance(members, list):
            issues.append(f"plan.json: train {tid} members must be a list")
        else:
            for member in members:
                if member not in known:
                    issues.append(f"plan.json: train {tid} has unknown task {member}")
                elif member in owner:
                    issues.append(f"plan.json: task {member} is in two trains ({owner[member]} and {tid})")
                else:
                    owner[member] = tid
            if len(members) > TRAIN_MEMBER_LIMIT:
                issues.append(f"train {tid} has {len(members)} members; split it (limit {TRAIN_MEMBER_LIMIT})")
        pr = item.get("pr")
        if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool)):
            issues.append(f"plan.json: train {tid} pr must be an integer")
        shared = item.get("shared_files")
        if shared is not None and (not isinstance(shared, list) or not all(isinstance(entry, str) for entry in shared)):
            issues.append(f"plan.json: train {tid} shared_files must be a list of strings")
        for key in ("gate", "note"):
            if key in item and item.get(key) is not None and not isinstance(item.get(key), str):
                issues.append(f"plan.json: train {tid} {key} must be a string")
        for key in ("title", "note"):
            text = item.get(key)
            if isinstance(text, str):
                for hit in naming_issues(text):
                    issues.append(f"plan.json: train {tid} {key}: bare citation {hit.token}")


def _validate_lifecycle(issues: list[str], ws: str, meta: dict, root: Path | None = None) -> None:
    """Additive bundle.json lifecycle fields: state, stamps, summary, deps, milestone."""
    from dash.lifecycle import STATES

    if "state" in meta and meta.get("state") not in STATES:
        issues.append(
            f"bundle.json: state {meta.get('state')!r} is not {', '.join(STATES)}. "
            f"Run: python3 agent_dash.py ws state {ws} <one of: {', '.join(STATES)}>"
        )
    for key in ("state_changed_at", "archived_at"):
        if key in meta and meta.get(key) is not None and (
            isinstance(meta.get(key), bool) or not isinstance(meta.get(key), (int, float))
        ):
            issues.append(
                f"bundle.json: {key} must be an epoch number or null. "
                f"Run: python3 agent_dash.py ws state {ws} <state> to reset it"
            )
    if "summary" in meta and meta.get("summary") is not None and not isinstance(meta.get("summary"), dict):
        issues.append(
            f"bundle.json: summary must be an object or null. Run: python3 agent_dash.py ws validate {ws}"
        )
    if "depends_on" in meta:
        depends = meta.get("depends_on")
        if not isinstance(depends, list) or not all(isinstance(item, str) for item in depends):
            issues.append(
                f"bundle.json: depends_on must be a list of workstream ids. "
                f"Run: python3 agent_dash.py ws list --state all"
            )
        else:
            known = {item["id"] for item in list_bundles(root)}
            for dep in depends:
                if dep == "OVERVIEW":
                    issues.append(
                        "bundle.json: depends_on must not name OVERVIEW (a program bundle, not a workstream). "
                        f"Run: python3 agent_dash.py ws list --state all"
                    )
                elif dep not in known:
                    issues.append(
                        f"bundle.json: depends_on names unknown workstream {dep}. "
                        f"Run: python3 agent_dash.py ws list --state all"
                    )
    if "milestone" in meta and meta.get("milestone") is not None and not isinstance(meta.get("milestone"), str):
        issues.append(
            f"bundle.json: milestone must be a string or null. Run: python3 agent_dash.py ws validate {ws}"
        )


def validate_bundle(ws: str, root: Path | None = None) -> list[str]:
    path = bundle_dir(ws, root)
    if path is None:
        return [f"{ws}: bundle not found"]
    issues: list[str] = []
    meta = _read_json(path / "bundle.json")
    plan = _read_json(path / "plan.json")
    if not isinstance(meta, dict):
        issues.append("bundle.json: unreadable")
        meta = {}
    else:
        if meta.get("id") != path.name:
            issues.append(f"bundle.json: id {meta.get('id')!r} does not match directory {path.name}")
        for key in ("title", "short"):
            _require_str(issues, "bundle.json", meta.get(key), key)
        if not isinstance(meta.get("order"), int) or isinstance(meta.get("order"), bool):
            issues.append("bundle.json: order must be an integer")
        if meta.get("kind") not in ("program", "workstream"):
            issues.append("bundle.json: kind must be program or workstream")
        _validate_lifecycle(issues, ws, meta, root)
    if not isinstance(plan, dict):
        issues.append("plan.json: unreadable")
    else:
        kind = meta.get("kind")
        if kind == "program":
            _validate_narrative(issues, plan.get("narrative"), require=True)
            if "dag" in plan:
                _validate_dag(issues, plan.get("dag"))
        else:
            if "dag" not in plan:
                issues.append("plan.json: dag is required for a workstream")
            else:
                _validate_dag(issues, plan.get("dag"))
            if "narrative" not in plan:
                issues.append("plan.json: narrative is required")
            else:
                _validate_narrative(issues, plan.get("narrative"), require=True)
            if "decisions" in plan:
                _validate_decisions(issues, plan.get("decisions"))
            if "motivation" in plan:
                _validate_motivation(issues, plan.get("motivation"))
            if "agent_friendly" in plan:
                _validate_agent(issues, plan.get("agent_friendly"))
        _validate_trains(issues, plan)
    approvals_path = path / "approvals.json"
    if approvals_path.is_file():
        raw = _read_json(approvals_path)
        if raw is None:
            issues.append("approvals.json: unreadable")
        else:
            _validate_approvals(issues, path.name, raw)
    for name in ("bundle.json", "plan.json", "layout.json", "page.html", "visuals.html", "approvals.json"):
        file = path / name
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            issues.append(f"{name}: unreadable")
            continue
        for hit in naming_issues(text):
            issues.append(f"{name}:{hit.line}: bare citation {hit.token}")
    return issues


def plan_map_warnings(ws: str, root: Path | None = None) -> list[dict]:
    """Layer C lint dicts (MAP101 to MAP111) for a bundle's plan.

    Warnings, never errors: empty when the plan is missing or unreadable,
    so callers keep their own not-found and invalid-plan paths.
    """
    path = bundle_dir(ws, root)
    if path is None:
        return []
    plan = _read_json(path / "plan.json")
    if not isinstance(plan, dict):
        return []
    dag = plan.get("dag") if isinstance(plan.get("dag"), dict) else {}
    narrative = plan.get("narrative") if isinstance(plan.get("narrative"), dict) else {}
    view = {
        "ws": ws,
        "cols": [item for item in dag.get("cols") or [] if isinstance(item, str)],
        "groups": dag.get("groups") if isinstance(dag.get("groups"), list) else [],
        "edges": dag.get("edges") if isinstance(dag.get("edges"), list) else [],
        "phases": narrative.get("phases") if isinstance(narrative.get("phases"), list) else [],
        "trains": plan.get("trains") if isinstance(plan.get("trains"), list) else [],
    }
    try:
        return maplayout.map_warnings(view)
    except Exception as exc:
        _map_unavailable(exc)
        return []


def _map_unavailable(exc: Exception) -> None:
    """Warnings channel for a map layout failure: stderr, exit code untouched."""
    print(f"warning: map layout unavailable ({type(exc).__name__})", file=sys.stderr)


def _print_map_warnings(warnings: list[dict]) -> None:
    """One ``warning MAPnnn: ...`` line per lint finding, with the fix hint."""
    if not warnings:
        return
    print("warnings:")
    for warn in warnings:
        print(f"warning {warn['code']}: {warn['title']} ({warn['detail']}). Fix: {warn['fix']}")


def print_layout(ws: str, json_mode: bool = False) -> int:
    """Print the computed map layout for a workstream without touching the page."""
    body, message, code = _resolve_body(ws)
    if body is None:
        assert message is not None
        _say(message, json_mode)
        return code
    view = body if isinstance(body, dict) else {}
    result: dict | None = None
    warnings: list[dict] = []
    try:
        result = maplayout.layout(view)
    except Exception as exc:
        _map_unavailable(exc)
    try:
        warnings = maplayout.map_warnings(view)
    except Exception as exc:
        _map_unavailable(exc)
    if json_mode:
        errors.emit_json({"ws": ws, "layout": result, "map_warnings": warnings})
        return 0
    if result is None:
        _print_map_warnings(warnings)
        return 0
    print(
        f"{ws} map layout: {result['density']} density "
        f"({result['live_tasks']} live, {result['deferred_tasks']} deferred), "
        f"arrows {result['arrows']['mode']} ({result['arrows']['reason']}, {result['group_edges']} section edges)"
    )
    parts = [f"{col['title']}[{col['tasks']} {col['state']}]" for col in result["columns"]]
    print(f"columns ({len(parts)}): {' '.join(parts)}")
    if result["folded_rail"] is not None:
        print(f"folded rail: {result['folded_rail']['label']}")
    balance = result["balance"]
    if balance["tallest"] is not None:
        tall = balance["tallest"]
        short = balance["shortest"]
        print(f"tallest {tall['title']} ({tall['tasks']} tasks) vs shortest {short['title']} ({short['tasks']} tasks)")
    counts = {"open": 0, "collapsed-done": 0, "slim": 0}
    for group in result["groups"]:
        counts[group["state"]] = counts.get(group["state"], 0) + 1
    print(
        f"sections ({len(result['groups'])}): {counts.get('open', 0)} open, "
        f"{counts.get('collapsed-done', 0)} collapsed-done, {counts.get('slim', 0)} slim"
    )
    for group in result["groups"]:
        if group["state"] == "collapsed-done":
            print(f"  collapsed \"{group['title']}\" ({group['id']}): {group['live_tasks']} live, {group['more_token']}")
        elif group["state"] == "slim" and group.get("slim_summary") is not None:
            summary = group["slim_summary"]
            print(f"  slim \"{group['title']}\" ({group['id']}): ✓{summary['done']}/{summary['total']}")
    labels = result["labels"]
    collisions = f", {len(labels['collisions'])} collisions disambiguated" if labels["collisions"] else ""
    print(f"labels: {labels['shortened']} shortened{collisions}")
    fan_in = ", ".join(f"{row['id']} ({row['deps']} deps)" for row in result["arrows"]["fan_in"])
    print(f"fan-in: {fan_in if fan_in else 'none'}")
    trains = result["trains"]
    if trains["caption"] is not None:
        print(f"trains: {len(trains['tagged'])} tagged, {len(trains['untagged'])} untagged — {trains['caption']}")
    elif trains["tagged"]:
        print(f"trains: {', '.join(str(tid) for tid in trains['legend'])}")
    else:
        print("trains: none")
    print(f"phases: {result['phases_line'] if result['phases_line'] else '(no phases)'}")
    _print_map_warnings(warnings)
    if not warnings:
        print("(no MAP warnings)")
    return 0


def set_task(
    ws: str,
    task_id: str,
    status: str,
    moved_to: str | None,
    note: str | None,
    root: Path | None = None,
    force: bool = False,
    origin: str = "local",
) -> int:
    """Set one task's status in plan.json and append history.

    ``origin="local"`` (the owner's ``ws task``) records the change in the
    Linear outbox while the bundle ``.lock`` is held, so a concurrent pull
    either sees the row or has not read the old value yet. ``origin="linear"``
    (a pull) writes nothing, appends no history and prints nothing, and
    returns ``errors.SKIPPED_PENDING`` when a local row for the task is still
    in the outbox; the local status wins and reaches Linear on the next flush.
    """
    from dash import linear

    if status not in STATUSES:
        print(f"unknown status {status!r}. Use one of: {', '.join(STATUSES)}", flush=True)
        for key in STATUSES:
            print(f"  {key:9} {STATUS_MEANING[key]}", flush=True)
        print(f"Run: python3 agent_dash.py ws plan {ws} to see the tasks first", flush=True)
        return errors.USAGE
    if status == "prog":
        print("note: prog means merged to main and waiting on a production step. For work still on a branch use wip; for approved or open-PR work use pr.", flush=True)
    if moved_to is not None and not _MOVED.match(moved_to):
        print(f"bad moved-to {moved_to!r}: use WSn, e.g. WS6", flush=True)
        return errors.USAGE
    if status != "deferred" and (moved_to or note):
        print(f"moved-to and note belong on deferred. Run: python3 agent_dash.py ws task {ws} {task_id} deferred --moved-to WS<n> --note ...", flush=True)
        return errors.USAGE
    path = bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    plan_path = path / "plan.json"
    with _bundle_lock(path):
        if origin == "linear" and linear.has_pending(ws, "task", task_id, root):
            return errors.SKIPPED_PENDING
        data = _read_json(plan_path)
        if not isinstance(data, dict):
            print(errors.plan_unreadable(ws), flush=True)
            return errors.INVALID_DATA
        dag = data.get("dag")
        if not isinstance(dag, dict) or not isinstance(dag.get("groups"), list):
            print(f"plan.json for {ws} has no dag. Run: python3 agent_dash.py ws validate {ws}", flush=True)
            return errors.INVALID_DATA
        found = False
        old_status: str | None = None
        known = 0
        for group in dag["groups"]:
            if not isinstance(group, dict):
                continue
            for task in group.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                known += 1
                if task.get("id") == task_id:
                    old_status = task.get("status") if isinstance(task.get("status"), str) else None
                    if not force and old_status in ("done", "deferred") and status in ("wip", "pr"):
                        print(f"refusing to move {ws} {task_id} from {old_status} to {status} (use --force)", flush=True)
                        return errors.USAGE
                    task["status"] = status
                    if status == "deferred":
                        if moved_to is not None:
                            task["moved_to"] = moved_to
                        if note is not None:
                            task["note"] = note
                    else:
                        task.pop("moved_to", None)
                        task.pop("note", None)
                    found = True
        if not found:
            print(errors.task_not_found(ws, task_id, known), flush=True)
            return errors.NOT_FOUND
        _atomic_write(plan_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if origin == "local":
            linear.enqueue(ws, "task", task_id, root=root)
    if old_status != status:
        append_history(ws, task_id, old_status, status, root=root, by=origin if origin != "local" else None)
    print(f"{ws} {task_id} {status}")
    return 0


def append_history(
    ws: str,
    task_id: str,
    from_status: str | None,
    to_status: str,
    root: Path | None = None,
    now: float | None = None,
    by: str | None = None,
) -> None:
    """Append one ``{ts, task, from, to}`` line to the bundle's history file.

    ``by`` names the writer when it is not the local owner (``"linear"``).

    The plan write above stays temp-file-plus-replace; a history line is a
    single append, flushed and fsynced so one line is atomic. A failed
    append warns but keeps the plan write (the plan is the source of truth).
    """
    import time as _time

    path = bundle_dir(ws, root)
    if path is None:
        print(f"history not recorded: bundle not found: {ws}", flush=True)
        return
    row: dict = {"ts": _time.time() if now is None else now, "task": task_id, "to": to_status}
    if isinstance(from_status, str) and from_status:
        row["from"] = from_status
    if by is not None:
        row["by"] = by
    try:
        with open(path / "history.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except OSError as exc:
        print(f"history not recorded for {ws} {task_id}: {exc}", flush=True)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


def set_train(
    ws: str,
    train_id: str,
    title: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    pr: int | None = None,
    shared: list[str] | None = None,
    gate: str | None = None,
    note: str | None = None,
    root: Path | None = None,
    origin: str = "local",
) -> int:
    if not isinstance(train_id, str) or not _TRAIN_ID.match(train_id):
        print(f"bad train id {train_id!r}: use letters, digits, ., _, - (max 40 chars)", flush=True)
        return errors.USAGE
    if status is not None and status not in TRAIN_STATUSES:
        print(errors.unknown_train_status(status), flush=True)
        return errors.USAGE
    if branch is not None and not branch.startswith("train/"):
        print(f'bad branch {branch!r}: must start with "train/", e.g. train/notes-cutover', flush=True)
        return errors.USAGE
    if title is not None and not title.strip():
        print("title required: pass --title \"...\"", flush=True)
        return errors.USAGE
    path = bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    plan_path = path / "plan.json"
    with _bundle_lock(path):
        data = _read_json(plan_path)
        if not isinstance(data, dict):
            print(errors.plan_unreadable(ws), flush=True)
            return errors.INVALID_DATA
        known = _dag_task_ids(data)
        trains = data.get("trains")
        if trains is None:
            trains = []
            data["trains"] = trains
        if not isinstance(trains, list):
            print(f"plan.json trains for {ws} is not a list. Run: python3 agent_dash.py ws validate {ws}", flush=True)
            return errors.INVALID_DATA
        found = None
        for item in trains:
            if isinstance(item, dict) and item.get("id") == train_id:
                found = item
                break
        if found is None:
            if title is None:
                print(f"title required for a new train. Run: python3 agent_dash.py ws train {ws} {train_id} --title \"...\"", flush=True)
                return errors.USAGE
            found = {"id": train_id, "title": title.strip(), "members": [], "status": "planned"}
            trains.append(found)
        elif title is not None:
            found["title"] = title.strip()
        members = found.get("members")
        if not isinstance(members, list):
            members = []
            found["members"] = members
        for task_id in remove or []:
            if task_id in members:
                members.remove(task_id)
        for task_id in add or []:
            if task_id not in known:
                print(errors.task_not_found(ws, task_id, len(known)), flush=True)
                return errors.NOT_FOUND
            for other in trains:
                if other is not found and isinstance(other, dict) and task_id in (other.get("members") or []):
                    other["members"].remove(task_id)
                    print(f"moved {task_id} from {other.get('id')} to {train_id}", flush=True)
            if task_id not in members:
                members.append(task_id)
        if add and len(members) > TRAIN_MEMBER_LIMIT:
            print(f"train {train_id} has {len(members)} members; split it (limit {TRAIN_MEMBER_LIMIT}). Run: python3 agent_dash.py ws trains {ws}", flush=True)
            return errors.USAGE
        if branch is not None:
            found["branch"] = branch
        if status is not None:
            found["status"] = status
        if pr is not None:
            found["pr"] = pr
        if shared is not None:
            found["shared_files"] = shared
        if gate is not None:
            found["gate"] = gate
        if note is not None:
            found["note"] = note
        _atomic_write(plan_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"{ws} train {train_id} {found.get('status')}")
    return 0


def _train_rows(ws: str, root: Path | None = None) -> tuple[list | None, dict]:
    """Structured trains for one bundle, plus the dag task statuses."""
    path = bundle_dir(ws, root)
    if path is None:
        return None, {}
    data = _read_json(path / "plan.json")
    if not isinstance(data, dict):
        return False, {}
    dag_status: dict[str, str] = {}
    dag = data.get("dag")
    if isinstance(dag, dict):
        for group in dag.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for task in group.get("tasks") or []:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    dag_status[task["id"]] = task.get("status") or "todo"
    trains = data.get("trains") or []
    if not isinstance(trains, list):
        return False, {}
    rows = []
    for item in trains:
        if not isinstance(item, dict):
            continue
        members = item.get("members") if isinstance(item.get("members"), list) else []
        rows.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "branch": item.get("branch"),
                "pr": item.get("pr"),
                "members": members,
                "member_status": {member: dag_status.get(member, "?") for member in members},
                "shared_files": item.get("shared_files") if isinstance(item.get("shared_files"), list) else [],
                "gate": item.get("gate"),
                "note": item.get("note"),
            }
        )
    return rows, dag_status


def print_trains(ws: str, root: Path | None = None, json_mode: bool = False) -> int:
    rows, dag_status = _train_rows(ws, root)
    if rows is None:
        _say(errors.bundle_not_found(ws), json_mode)
        return errors.NOT_FOUND
    if rows is False:
        _say(errors.plan_unreadable(ws), json_mode)
        return errors.INVALID_DATA
    from dash import linear

    linked = linear.is_linked(ws, root)
    if linked:
        from dash import linear_sync

        for item in rows:
            item["linear_refs"] = linear_sync.train_refs(ws, item, root)
    if json_mode:
        errors.emit_json({"ws": ws, "trains": rows})
        return 0
    if not rows:
        print("(no trains)")
        return 0
    header = "TRAIN\tSTATUS\tBRANCH\tPR\tMEMBERS\tSHARED"
    header += "\tLINEAR" if linked else ""
    print(header)
    for item in rows:
        members = item.get("members") or []
        member_text = ",".join(f"{member}({dag_status.get(member, '?')})" for member in members) or "-"
        shared = item.get("shared_files") or []
        cells = [
            _cell(item.get("id")),
            _cell(item.get("status")),
            _cell(item.get("branch") or "-"),
            _cell(item.get("pr") if item.get("pr") is not None else "-"),
            _cell(member_text),
            str(len(shared)),
        ]
        if linked:
            cells.append(_cell(item.get("linear_refs") or "-"))
        print("\t".join(cells))
    return 0


def print_plan(ws: str, json_mode: bool = False) -> int:
    """List one bundle's tasks: ids, titles, status, deps."""
    from dash.plans import PlanError

    try:
        _source, body = resolve_plan(ws)
    except PlanError:
        _say(errors.plan_unreadable(ws), json_mode)
        return errors.INVALID_DATA
    if not isinstance(body, dict):
        _say(f"plan not found for {ws}. Run: python3 agent_dash.py ws list", json_mode)
        return errors.NOT_FOUND
    groups = []
    for group in body.get("groups") or []:
        if not isinstance(group, dict):
            continue
        tasks = []
        for task in group.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            deps = task.get("deps")
            tasks.append(
                {
                    "id": task.get("id"),
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "deps": [item for item in deps if isinstance(item, str)] if isinstance(deps, list) else [],
                }
            )
        groups.append({"id": group.get("id"), "title": group.get("title"), "tasks": tasks})
    if json_mode:
        errors.emit_json({"ws": ws, "groups": groups})
        return 0
    if not groups:
        print(f"(no plan for {ws})")
        return 0
    print(f"plan {ws}: {sum(len(group['tasks']) for group in groups)} tasks")
    for group in groups:
        print(f"section {group.get('id')}: {group.get('title')}")
        for task in group["tasks"]:
            deps = f" (deps: {', '.join(task['deps'])})" if task["deps"] else ""
            print(f"  {task.get('id')} [{task.get('status')}] {task.get('title')}{deps}")
    return 0


def _dash_url() -> str:
    """Dashboard base URL (``AGENT_DASH_URL`` wins, for tests and mirrors)."""
    import os as _os

    return _os.environ.get("AGENT_DASH_URL") or "http://127.0.0.1:8765"


def _resolve_body(ws: str):
    """(body, None, 0) or (None, message, code) for orchestration commands."""
    from dash.plans import PlanError

    if bundle_dir(ws) is None:
        try:
            _source, body = resolve_plan(ws)
        except PlanError:
            return None, errors.plan_unreadable(ws), errors.INVALID_DATA
        if not isinstance(body, dict):
            return None, errors.bundle_not_found(ws), errors.NOT_FOUND
        return body, None, 0
    try:
        _source, body = resolve_plan(ws)
    except PlanError:
        return None, errors.plan_unreadable(ws), errors.INVALID_DATA
    if not isinstance(body, dict):
        return None, errors.plan_unreadable(ws), errors.INVALID_DATA
    return body, None, 0


def _history_starts_dones(ws: str) -> tuple[dict, dict]:
    """(starts, dones) from history.jsonl: first touch and done moment."""
    from dash.replay import read_history

    starts: dict[str, float] = {}
    dones: dict[str, float] = {}
    for row in read_history(ws):
        task = row.get("task")
        ts = row.get("ts")
        if not isinstance(task, str) or not isinstance(ts, (int, float)):
            continue
        if task not in starts or ts < starts[task]:
            starts[task] = float(ts)
        if row.get("to") == "done" and (task not in dones or ts > dones[task]):
            dones[task] = float(ts)
    return starts, dones


def print_next(ws: str, limit: int | None = None, json_mode: bool = False) -> int:
    """Ready tasks ranked by critical-path impact, plus stalled/dep-jump warnings."""
    from dash import orchestrate

    body, message, code = _resolve_body(ws)
    if body is None:
        _say(message, json_mode)
        return code
    _by_id, _deps, order = orchestrate.plan_tasks(body)
    runs = orchestrate.dashboard_runs(_dash_url(), ws)
    live_source = "api" if runs is not None else "unreachable"
    live = orchestrate.live_by_task(runs or [], ws, order)
    ready = orchestrate.ready_queue(body, live)
    if limit is not None:
        ready = ready[:limit]
    stalled = orchestrate.stalled_tasks(body, live)
    starts, dones = _history_starts_dones(ws)
    replay = orchestrate.dashboard_replay(_dash_url(), ws)
    if replay:
        replay_starts, replay_dones = orchestrate.replay_starts_dones(replay)
        for tid, ts in replay_starts.items():
            if tid not in starts or ts < starts[tid]:
                starts[tid] = ts
        for tid, ts in replay_dones.items():
            if tid not in dones:
                dones[tid] = ts
    jumps = orchestrate.dep_jumps(body, starts, dones)
    if json_mode:
        errors.emit_json({"ws": ws, "ready": ready, "stalled": stalled, "dep_jumps": jumps, "live": live_source})
        return 0
    print(f"ready in {ws} ({len(ready)}):")
    for row in ready:
        unblocks = ",".join(row["unblocks"]) or "-"
        train = row["train"] or "-"
        live_label = row["live_run"] or "none"
        print(f"  {row['id']} chain={row['chain']} dependents={row['dependents']} unblocks={unblocks} train={train} live={live_label} {row['title']}")
    if not ready:
        print("  (none: every todo task still waits on a dep)")
    if stalled:
        print(f"stalled ({len(stalled)} started with no live run):")
        for row in stalled:
            print(f"  {row['id']} [{row['status']}] {row['title']}")
    if jumps:
        print(f"dep-jump ({len(jumps)} started before a dep finished):")
        for row in jumps:
            print(f"  {row['task']} started before {row['dep']} finished")
    if live_source == "unreachable":
        print(f"(live run data unavailable: dashboard unreachable at {_dash_url()})")
    return 0


def print_waves(ws: str, json_mode: bool = False) -> int:
    """Max-parallel waves, critical path, best span vs actual, max parallelism."""
    from dash import orchestrate

    body, message, code = _resolve_body(ws)
    if body is None:
        _say(message, json_mode)
        return code
    wave_ids = orchestrate.waves(body)
    path = orchestrate.critical_path(body)
    replay = orchestrate.dashboard_replay(_dash_url(), ws)
    durations = orchestrate.replay_durations(replay)
    dur_path = orchestrate.critical_path(body, durations) if durations else list(path)
    dur_total = sum(durations.get(tid, 0.0) for tid in dur_path) if durations else None
    span = replay.get("span") if isinstance(replay, dict) else None
    actual_span = None
    if isinstance(span, dict):
        try:
            actual_span = float(span["end"]) - float(span["start"])
        except (KeyError, TypeError, ValueError):
            actual_span = None
    width = max((len(wave) for wave in wave_ids), default=0)
    payload = {
        "ws": ws,
        "waves": wave_ids,
        "sizes": [len(wave) for wave in wave_ids],
        "critical_path": path,
        "critical_path_duration": dur_path,
        "critical_path_seconds": dur_total,
        "best_span_tasks": len(path),
        "actual_span_seconds": actual_span,
        "max_parallel": width,
        "durations": "observed" if durations else "unavailable",
    }
    if json_mode:
        errors.emit_json(payload)
        return 0
    print(f"waves in {ws} ({len(wave_ids)} waves, max parallel {width}):")
    for index, wave in enumerate(wave_ids):
        print(f"  wave {index} ({len(wave)}): {', '.join(wave)}")
    print(f"critical path ({len(path)} tasks): {' -> '.join(path) if path else '-'}")
    if durations:
        print(f"critical path by observed duration ({dur_total:.0f}s): {' -> '.join(dur_path)}")
    else:
        print("(no observed durations: dashboard unreachable or no mapped runs yet)")
    if actual_span is not None:
        print(f"best span {len(path)} tasks vs actual {actual_span:.0f}s wall clock")
    else:
        print(f"best span {len(path)} tasks vs actual unknown")
    return 0


def _next_wave_ids(body) -> list[str]:
    """Ids in the first wave holding an unfinished, non-deferred task."""
    from dash import orchestrate

    by_id, _deps, _order = orchestrate.plan_tasks(body)
    for wave in orchestrate.waves(body):
        if any(by_id[tid].get("status") not in ("done", "deferred") for tid in wave if tid in by_id):
            return list(wave)
    return []


def _cross_declared() -> list[dict]:
    """Declared-file overlap pairs across different workstreams (wip/pr only)."""
    from dash import orchestrate

    per_ws: dict[str, dict[str, list[str]]] = {}
    for bundle in list_bundles():
        if bundle.get("kind") != "workstream":
            continue
        _source, body = resolve_plan(bundle["id"])
        if not isinstance(body, dict):
            continue
        by_id, _deps, order = orchestrate.plan_tasks(body)
        footprints = {}
        for tid in order:
            if by_id[tid].get("status") not in ("wip", "pr"):
                continue
            files = orchestrate.task_files(by_id[tid])
            if files:
                footprints[tid] = files
        if footprints:
            per_ws[bundle["id"]] = footprints
    rows = []
    names = sorted(per_ws)
    for pos, left_ws in enumerate(names):
        for right_ws in names[pos + 1:]:
            for left_tid, mine in per_ws[left_ws].items():
                for right_tid, theirs in per_ws[right_ws].items():
                    shared = orchestrate.files_overlap(mine, theirs)
                    if shared:
                        rows.append(
                            {
                                "ws_a": left_ws,
                                "a": left_tid,
                                "ws_b": right_ws,
                                "b": right_tid,
                                "files": shared,
                                "source": "declared",
                            }
                        )
    return rows


def print_overlap(ws: str | None, all_waves: bool = False, cross: bool = False, json_mode: bool = False) -> int:
    """File-overlap warnings: declared footprints, plus inferred branch diffs."""
    from dash import orchestrate

    if cross:
        pairs = _cross_declared()
        if json_mode:
            errors.emit_json({"cross": True, "pairs": pairs})
            return 0
        if not pairs:
            print("(no cross-workstream file overlaps among wip/pr tasks)")
            return 0
        print(f"cross-workstream overlaps ({len(pairs)}):")
        for row in pairs:
            print(f"  {row['ws_a']}:{row['a']} <> {row['ws_b']}:{row['b']} [{row['source']}]")
            for shared in row["files"]:
                print(f"    {shared}")
        return 0
    if ws is None:
        _say("overlap needs a workstream id (or --cross). Run: python3 agent_dash.py ws overlap <ID>", json_mode)
        return errors.USAGE
    body, message, code = _resolve_body(ws)
    if body is None:
        _say(message, json_mode)
        return code
    by_id, _deps, _order = orchestrate.plan_tasks(body)
    if all_waves:
        scope = _next_wave_ids(body)
        scope_name = "next wave"
    else:
        scope = [tid for tid in by_id if by_id[tid].get("status") in ("wip", "pr")]
        scope_name = "wip/pr"
    pairs = orchestrate.overlaps(body, scope)
    runs = orchestrate.dashboard_runs(_dash_url(), ws)
    if runs:
        pairs.extend(orchestrate.inferred_overlaps(body, runs, scope, orchestrate.run_out_paths()))
    if json_mode:
        errors.emit_json({"ws": ws, "scope": scope_name, "pairs": pairs})
        return 0
    if not pairs:
        print(f"(no file overlaps in {ws} {scope_name})")
        return 0
    print(f"overlaps in {ws} {scope_name} ({len(pairs)}):")
    for row in pairs:
        print(f"  {row['a']} <> {row['b']} [{row['source']}]")
        for shared in row["files"]:
            print(f"    {shared}")
    return 0


def print_gates(ws: str, json_mode: bool = False) -> int:
    """Every gate with its signal, freshness, and blocked dependents."""
    import time as _time

    from dash import orchestrate

    body, message, code = _resolve_body(ws)
    if body is None:
        _say(message, json_mode)
        return code
    gates = orchestrate.stale_gates(body, _time.time())
    if json_mode:
        errors.emit_json({"ws": ws, "gates": gates})
        return 0
    if not gates:
        print(f"(no gates in {ws})")
        return 0
    print(f"gates in {ws} ({len(gates)}):")
    for row in gates:
        state = "STALE" if row["stale"] else "fresh"
        blocked = ",".join(row["blocked"]) or "-"
        print(f"  {row['id']} [{row['status']}] {state} signal={row['signal'] or '-'} confirmed={row['confirmed_at'] or '-'} blocks={blocked}")
    return 0


def print_deferred(ws: str | None, json_mode: bool = False) -> int:
    """Deferred tasks with receiving workstream and pickup state."""
    from dash import orchestrate

    if ws is not None:
        body, message, code = _resolve_body(ws)
        if body is None:
            _say(message, json_mode)
            return code
        bodies = {ws: body}
        from dash.plans import PlanError

        by_id, _deps, _order = orchestrate.plan_tasks(body)
        for task in by_id.values():
            if isinstance(task, dict) and task.get("status") == "deferred":
                moved = task.get("moved_to")
                if isinstance(moved, str) and moved not in bodies:
                    try:
                        _source, target = resolve_plan(moved)
                    except PlanError:
                        continue
                    if isinstance(target, dict):
                        bodies[moved] = target
    else:
        bodies = {}
        for bundle in list_bundles():
            if bundle.get("kind") != "workstream":
                continue
            _source, body = resolve_plan(bundle["id"])
            if isinstance(body, dict):
                bodies[bundle["id"]] = body
    rows = orchestrate.deferred_ledger(bodies)
    if json_mode:
        errors.emit_json({"ws": ws or "all", "deferred": rows})
        return 0
    if not rows:
        print(f"(no deferred tasks in {ws or 'any workstream'})")
        return 0
    print(f"deferred ({len(rows)}):")
    for row in rows:
        if row["received"]:
            state = f"picked up as {row['received']}"
        elif row["moved_to"]:
            state = "ORPHAN: nobody picked it up"
        else:
            state = "ORPHAN: no receiving workstream"
        print(f"  {row['ws']}:{row['id']} -> {row['moved_to'] or '-'} {state} {row['title']}")
    return 0


LINEAR_SUBCOMMANDS = ("link", "sync", "status", "unlink", "comment", "update")
LINEAR_COMMENT_LIMIT_HINT = "Run: python3 agent_dash.py ws linear comment <ID> <TASK-ID> <TEXT...>"


def _cmd_linear_auth(args: list[str], team: str | None) -> int:
    """``ws linear login`` and ``ws linear logout``: the account sign-in, no flags and no workstream id."""
    from dash import linear_auth

    sub, rest = args[0], args[1:]
    if team is not None or rest:
        print(f"ws linear {sub} takes no arguments. Run: python3 agent_dash.py ws linear {sub}", flush=True)
        return errors.USAGE
    if sub == "logout":
        return linear_auth.logout()
    return linear_auth.login()


def _cmd_linear_comment(args: list[str]) -> int:
    """``ws linear comment <WS> <TASK-ID> <TEXT...>``: queue one comment on the task's Linear issue. ``args`` starts with ``comment``."""
    from dash import linear

    if len(args) < 4:
        print(f"ws linear comment needs a workstream id, a task id and text. {LINEAR_COMMENT_LIMIT_HINT}", flush=True)
        return errors.USAGE
    text_args = args[3:]
    if text_args[:1] == ["--"]:
        text_args = text_args[1:]
    ws, task_id, text = args[1], args[2], " ".join(text_args)
    path = bundle_dir(ws)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    if not linear.is_linked(ws):
        print(f"{ws} is not linked to Linear. Run: python3 agent_dash.py ws linear link {ws} --team ENG", flush=True)
        return errors.USAGE
    plan = _read_json(path / "plan.json")
    known = _dag_task_ids(plan) if isinstance(plan, dict) else set()
    if task_id not in known:
        print(errors.task_not_found(ws, task_id, len(known)), flush=True)
        return errors.NOT_FOUND
    try:
        linear.enqueue_comment(ws, task_id, text)
    except ValueError as exc:
        print(f"comment not queued: {exc}. {LINEAR_COMMENT_LIMIT_HINT}", flush=True)
        return errors.USAGE
    print(f"queued comment for {task_id}; it posts on the next sync", flush=True)
    return errors.OK


def _cmd_linear_update(args: list[str]) -> int:
    """``ws linear update <WS> [--health on-track|at-risk|off-track] [--dry-run]``: one standup update to Linear."""
    from dash import linear_update

    health: str | None = None
    dry_run = False
    rest: list[str] = []
    pos = 0
    while pos < len(args):
        arg = args[pos]
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--health":
            if pos + 1 >= len(args) or args[pos + 1].startswith("-"):
                print("--health needs on-track, at-risk or off-track. Run: python3 agent_dash.py ws linear update <ID> --health on-track", flush=True)
                return errors.USAGE
            health = args[pos + 1]
            pos += 1
        elif arg.startswith("-"):
            print(f"unknown flag {arg!r} for ws linear update. Run: python3 agent_dash.py ws linear update <ID> [--health on-track] [--dry-run]", flush=True)
            return errors.USAGE
        else:
            rest.append(arg)
        pos += 1
    if len(rest) != 1:
        print("ws linear update needs one workstream id. Run: python3 agent_dash.py ws linear update <ID> [--dry-run]", flush=True)
        return errors.USAGE
    return linear_update.post(rest[0], health=health, dry_run=dry_run)


def _cmd_linear(rest: list[str], json_mode: bool = False) -> int:
    """``ws linear <link|sync|status|unlink> <WS> [--team KEY]``: Linear mirror of one bundle."""
    args = list(rest)
    if args[:1] == ["comment"]:
        return _cmd_linear_comment(args)
    team: str | None = None
    if "--team" in args:
        pos = args.index("--team")
        if pos + 1 >= len(args) or args[pos + 1].startswith("-"):
            print("--team needs a Linear team key, e.g. --team ENG. Run: python3 agent_dash.py ws linear link <ID> --team ENG", flush=True)
            return errors.USAGE
        team = args[pos + 1]
        del args[pos:pos + 2]
    if not args:
        print(WS_USAGE, end="", flush=True)
        return errors.USAGE
    if args[0] in ("login", "logout"):
        return _cmd_linear_auth(args, team)
    sub = args[0]
    if sub == "update":
        if team is not None:
            print("--team applies to link only. Run: python3 agent_dash.py ws linear update <ID>", flush=True)
            return errors.USAGE
        return _cmd_linear_update(args[1:])
    if sub not in LINEAR_SUBCOMMANDS:
        print(f"unknown linear subcommand {sub!r}. Use one of: {', '.join(LINEAR_SUBCOMMANDS)}. Run: python3 agent_dash.py ws linear status <ID>", flush=True)
        return errors.USAGE
    if len(args) != 2:
        print(f"ws linear {sub} needs one workstream id. Run: python3 agent_dash.py ws linear {sub} <ID>", flush=True)
        return errors.USAGE
    ws = args[1]
    if sub == "link" and not team:
        print(f"team required to link. Run: python3 agent_dash.py ws linear link {ws} --team ENG", flush=True)
        return errors.USAGE
    if sub != "link" and team is not None:
        print(f"--team applies to link only. Run: python3 agent_dash.py ws linear {sub} {ws}", flush=True)
        return errors.USAGE
    from dash import linear_sync

    return linear_sync.run(sub, ws, team_key=team, json_mode=json_mode)


def cmd_ws(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(WS_USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else errors.USAGE
    cmd = argv[0]
    rest = argv[1:]
    json_mode = False
    if rest[:1] == ["--json"]:
        json_mode = True
        rest = rest[1:]
    elif rest[-1:] == ["--json"] and not (cmd == "linear" and rest[:1] == ["comment"]):
        json_mode = True
        rest = rest[:-1]
    if cmd == "list":
        from dash import lifecycle as _lifecycle

        return _lifecycle.cmd_list(rest, json_mode=json_mode)
    if cmd == "new":
        from dash import lifecycle as _lifecycle

        state = "active"
        args = list(rest)
        if "--state" in args:
            pos = args.index("--state")
            if pos < 2 or pos + 1 >= len(args):
                print(WS_USAGE, end="", flush=True)
                return 2
            state = args[pos + 1]
            del args[pos:pos + 2]
        if len(args) != 2:
            print(WS_USAGE, end="", flush=True)
            return 2
        if state not in _lifecycle.STATES:
            print(f"unknown state {state!r}. Use one of: {', '.join(_lifecycle.STATES)}", flush=True)
            return 2
        return cmd_new(args[0], args[1], state=state)
    if cmd == "state":
        from dash import lifecycle as _lifecycle

        return _lifecycle.cmd_state(rest, json_mode=json_mode)
    if cmd == "archive":
        from dash import lifecycle as _lifecycle

        return _lifecycle.cmd_archive(rest, json_mode=json_mode)
    if cmd == "restore":
        from dash import lifecycle as _lifecycle

        return _lifecycle.cmd_restore(rest, json_mode=json_mode)
    if cmd == "validate":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        ws = rest[0]
        if bundle_dir(ws) is None:
            _say(errors.bundle_not_found(ws), json_mode)
            return errors.NOT_FOUND
        issues = validate_bundle(ws)
        warnings = plan_map_warnings(ws)
        if json_mode:
            errors.emit_json({"ws": ws, "ok": not issues, "issues": issues, "warnings": warnings})
            return errors.INVALID_DATA if issues else errors.OK
        for issue in issues:
            print(issue)
        _print_map_warnings(warnings)
        if issues:
            print(f"validate failed for {ws}: {len(issues)} issues. Fix the files above, then re-run: python3 agent_dash.py ws validate {ws}")
            return errors.INVALID_DATA
        print(f"OK {ws}")
        return 0
    if cmd == "layout":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_layout(rest[0], json_mode=json_mode)
    if cmd == "plan":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_plan(rest[0], json_mode=json_mode)
    if cmd == "render":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        path = bundle_dir(rest[0])
        if path is None:
            _say(errors.bundle_not_found(rest[0]), json_mode)
            return errors.NOT_FOUND
        from dash.planrender import RenderError, render

        try:
            render(path)
        except RenderError as exc:
            _say(f"render failed for {rest[0]}: {exc}. Run: python3 agent_dash.py ws validate {rest[0]}", json_mode)
            return errors.INVALID_DATA
        print(f"wrote {path / 'page.html'}")
        return 0
    if cmd == "task":
        if len(rest) < 3:
            print(WS_USAGE, end="", flush=True)
            return 2
        moved = None
        note = None
        force = False
        flags = rest[3:]
        index = 0
        while index < len(flags):
            flag = flags[index]
            if flag == "--moved-to" and index + 1 < len(flags):
                moved = flags[index + 1]
                index += 2
            elif flag == "--note" and index + 1 < len(flags):
                note = flags[index + 1]
                index += 2
            elif flag == "--force":
                force = True
                index += 1
            else:
                print(WS_USAGE, end="", flush=True)
                return 2
        return set_task(rest[0], rest[1], rest[2], moved, note, force=force)
    if cmd == "train":
        if len(rest) < 2:
            print(WS_USAGE, end="", flush=True)
            return 2
        title = branch = status = gate = note = None
        add = remove = shared = None
        pr = None
        flags = rest[2:]
        index = 0
        valued = ("--title", "--branch", "--status", "--add", "--remove", "--pr", "--shared", "--gate", "--note")
        while index < len(flags):
            flag = flags[index]
            if flag in valued and index + 1 < len(flags):
                value = flags[index + 1]
                if flag == "--title":
                    title = value
                elif flag == "--branch":
                    branch = value
                elif flag == "--status":
                    status = value
                elif flag == "--add":
                    add = _split_csv(value)
                elif flag == "--remove":
                    remove = _split_csv(value)
                elif flag == "--pr":
                    try:
                        pr = int(value)
                    except ValueError:
                        print(f"bad pr {value!r}: must be an integer, e.g. --pr 100", flush=True)
                        return errors.USAGE
                elif flag == "--shared":
                    shared = _split_csv(value)
                elif flag == "--gate":
                    gate = value
                else:
                    note = value
                index += 2
            else:
                print(WS_USAGE, end="", flush=True)
                return 2
        return set_train(rest[0], rest[1], title, branch, status, add, remove, pr, shared, gate, note)
    if cmd == "trains":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_trains(rest[0], json_mode=json_mode)
    if cmd == "linear":
        return _cmd_linear(rest, json_mode=json_mode)
    if cmd == "approve":
        if len(rest) < 3:
            print(WS_USAGE, end="", flush=True)
            return 2
        step = None
        reversible = None
        conditions = None
        note = None
        flags = rest[3:]
        index = 0
        while index < len(flags):
            flag = flags[index]
            if flag in ("--step", "--reversible", "--conditions", "--note") and index + 1 < len(flags):
                value = flags[index + 1]
                if flag == "--step":
                    step = value
                elif flag == "--reversible":
                    reversible = _parse_yes_no(value)
                    if reversible is None:
                        print(f"unknown reversible {value!r}. Use yes or no, e.g. --reversible no", flush=True)
                        return errors.USAGE
                elif flag == "--conditions":
                    conditions = value
                else:
                    note = value
                index += 2
            else:
                print(WS_USAGE, end="", flush=True)
                return 2
        return set_approval(rest[0], rest[1], rest[2], step, reversible, conditions, note)
    if cmd == "approvals":
        if len(rest) > 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_approvals(rest[0] if rest else None, json_mode=json_mode)
    if cmd == "next":
        ws = None
        limit = None
        flags = list(rest)
        while flags:
            flag = flags.pop(0)
            if flag == "--limit" and flags:
                try:
                    limit = int(flags.pop(0))
                except ValueError:
                    print(f"bad --limit value: use an integer, e.g. python3 agent_dash.py ws next {ws or '<ID>'} --limit 5", flush=True)
                    return errors.USAGE
                if limit <= 0:
                    print(f"bad --limit {limit}: use a positive integer, e.g. python3 agent_dash.py ws next {ws or '<ID>'} --limit 5", flush=True)
                    return errors.USAGE
            elif ws is None and not flag.startswith("-"):
                ws = flag
            else:
                print(WS_USAGE, end="", flush=True)
                return errors.USAGE
        if ws is None:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_next(ws, limit=limit, json_mode=json_mode)
    if cmd == "waves":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_waves(rest[0], json_mode=json_mode)
    if cmd == "overlap":
        ws = None
        all_waves = False
        cross = False
        for flag in rest:
            if flag == "--all":
                all_waves = True
            elif flag == "--cross":
                cross = True
            elif ws is None and not flag.startswith("-"):
                ws = flag
            else:
                print(WS_USAGE, end="", flush=True)
                return errors.USAGE
        return print_overlap(ws, all_waves=all_waves, cross=cross, json_mode=json_mode)
    if cmd == "gates":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_gates(rest[0], json_mode=json_mode)
    if cmd == "deferred":
        if len(rest) > 1:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
        return print_deferred(rest[0] if rest else None, json_mode=json_mode)
    print(WS_USAGE, end="", flush=True)
    return errors.USAGE
