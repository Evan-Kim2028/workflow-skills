"""Plan bundles on disk: discover, validate, scaffold, and update task status.

Content lives under AGENT_DASH_WORKSTREAMS (default ~/.local/share/agent-dash/workstreams).
One directory per bundle. The tool directory stays free of project text.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

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

  python3 agent_dash.py ws list
  python3 agent_dash.py ws new <ID> "<title>"
  python3 agent_dash.py ws validate <ID>
  python3 agent_dash.py ws render <ID>
  python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."]
  python3 agent_dash.py ws train <ID> <train-id> [--title T] [--branch B] [--status S] [--add id,id] [--remove id,id] [--pr N] [--shared f1,f2] [--gate "..."] [--note "..."]
  python3 agent_dash.py ws trains <ID>
  python3 agent_dash.py ws approve <ID> <item-id> <approved|denied|pending> [--note "..."] [--step "..."] [--reversible yes|no] [--conditions "..."]
  python3 agent_dash.py ws approvals [<ID>]

Status is todo, pr, prog, done, or deferred. deferred may set --moved-to and --note.
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
    from dash.locate import expand

    raw = os.environ.get("AGENT_DASH_WORKSTREAMS")
    if raw:
        return Path(expand(raw))
    return Path.home() / ".local" / "share" / "agent-dash" / "workstreams"


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
        found.append({"id": child.name, "title": title, "short": short, "order": order, "kind": kind})
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
    }


def _view_groups(value) -> list:
    if not isinstance(value, list):
        return []
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
            tasks.append(row)
        groups.append({"id": item.get("id"), "col": item.get("col"), "title": item.get("title"), "tasks": tasks})
    return groups


def bundle_plan_view(ws: str) -> dict | None:
    """Dashboard plan shape for a bundle that is not listed in plans.json."""
    path = bundle_dir(ws)
    if path is None:
        return None
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
    groups = _view_groups(dag.get("groups"))
    return {
        "ws": ws,
        "cols": cols,
        "groups": groups,
        "edges": edges,
        "phases": phases,
        "trains": sanitized_trains(data.get("trains")),
        "progress": plan_progress(groups),
        "start_prompt": start_prompt_text(ws),
    }


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
            "new_tasks": [{"id": f"{ws}.1", "title": "First task", "phase": "First phase", "size": "S"}],
        },
        "dag": {
            "cols": ["Shape", "Build", "Prove", "Ship", "Clean up"],
            "groups": [
                {
                    "id": "start",
                    "col": 0,
                    "title": "First group",
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
                    "name": "First phase",
                    "goal": "What this phase delivers.",
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


def start_prompt_text(ws: str, root: Path | None = None) -> str | None:
    """Text of START-PROMPT.md, or None when the bundle has no such file."""
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
    return text


def cmd_new(ws: str, title: str, root: Path | None = None) -> int:
    if not _ID.match(ws):
        print(f"bad id {ws}", flush=True)
        return 2
    if not title or not title.strip():
        print("title required", flush=True)
        return 2
    base = Path(root) if root is not None else workstreams_root()
    dest = base / ws
    if dest.exists():
        print(f"bundle exists: {dest}", flush=True)
        return 2
    orders = [item["order"] for item in list_bundles(base)]
    order = (max(orders) if orders else 0) + 10
    dest.mkdir(parents=True)
    _atomic_write(
        dest / "bundle.json",
        json.dumps(
            {"id": ws, "title": title.strip(), "short": title.strip(), "order": order, "kind": "workstream"},
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
            issues.append("plan.json: each group must be an object")
            continue
        gid = group.get("id")
        _require_str(issues, "plan.json", gid, "group id")
        if isinstance(gid, str):
            if gid in ids:
                issues.append(f"plan.json: duplicate group id {gid}")
            ids.add(gid)
        col = group.get("col")
        if not isinstance(col, int) or isinstance(col, bool) or col < 0 or (cols and col >= len(cols)):
            issues.append(f"plan.json: group {gid} col is outside dag.cols")
        _require_str(issues, "plan.json", group.get("title"), f"group {gid} title")
        tasks = group.get("tasks")
        if not isinstance(tasks, list):
            issues.append(f"plan.json: group {gid} tasks must be a list")
            continue
        for task in tasks:
            if not isinstance(task, dict):
                issues.append(f"plan.json: group {gid} has a task that is not an object")
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
            issues.append("plan.json: each phase must be an object")
            continue
        for key in ("name", "goal", "gate"):
            _require_str(issues, "plan.json", phase.get(key), f"phase {key}")


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
    """None when the bundle directory is missing. An empty record when the file is absent."""
    path = bundle_dir(ws, root)
    if path is None:
        return None
    file = path / "approvals.json"
    if not file.is_file():
        return empty_approvals(ws)
    data = _read_json(file)
    if not isinstance(data, dict):
        return empty_approvals(ws)
    return _public_approvals(data, ws)


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
) -> int:
    if not _ITEM.match(item_id):
        print(f"bad id {item_id}", flush=True)
        return 2
    if status not in APPROVAL_STATUSES:
        print(f"bad status {status}", flush=True)
        return 2
    path = bundle_dir(ws, root)
    if path is None:
        print(f"bundle not found: {ws}", flush=True)
        return 2
    file = path / "approvals.json"
    if file.is_file():
        data = _read_json(file)
        if not isinstance(data, dict):
            print("approvals.json unreadable", flush=True)
            return 2
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
            print("step required", flush=True)
            return 2
        if reversible is None:
            print("reversible required", flush=True)
            return 2
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
        data["decided_by"] = "owner"
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
    print(f"{ws} {item_id} {status}")
    return 0


def print_approvals(ws: str | None = None, root: Path | None = None) -> int:
    if ws:
        if bundle_dir(ws, root) is None:
            print(f"bundle not found: {ws}", flush=True)
            return 2
        records = [approvals_for(ws, root)]
    else:
        records = approvals_all(root)
    lines = []
    for record in records:
        if not record:
            continue
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


def set_task(ws: str, task_id: str, status: str, moved_to: str | None, note: str | None, root: Path | None = None) -> int:
    if status not in STATUSES:
        print(f"bad status {status}. Use one of:", flush=True)
        for key in STATUSES:
            print(f"  {key:9} {STATUS_MEANING[key]}", flush=True)
        return 2
    if status == "prog":
        print("note: prog means merged to main and waiting on a production step. For work still on a branch use wip; for approved or open-PR work use pr.", flush=True)
    if moved_to is not None and not _MOVED.match(moved_to):
        print(f"bad moved-to {moved_to}", flush=True)
        return 2
    if status != "deferred" and (moved_to or note):
        print("moved-to and note belong on deferred", flush=True)
        return 2
    path = bundle_dir(ws, root)
    if path is None:
        print(f"bundle not found: {ws}", flush=True)
        return 2
    plan_path = path / "plan.json"
    data = _read_json(plan_path)
    if not isinstance(data, dict):
        print("plan.json unreadable", flush=True)
        return 2
    dag = data.get("dag")
    if not isinstance(dag, dict) or not isinstance(dag.get("groups"), list):
        print("plan.json has no dag", flush=True)
        return 2
    found = False
    old_status: str | None = None
    for group in dag["groups"]:
        if not isinstance(group, dict):
            continue
        for task in group.get("tasks") or []:
            if isinstance(task, dict) and task.get("id") == task_id:
                old_status = task.get("status") if isinstance(task.get("status"), str) else None
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
        print(f"task not found: {task_id}", flush=True)
        return 2
    _atomic_write(plan_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if old_status != status:
        append_history(ws, task_id, old_status, status, root=root)
    print(f"{ws} {task_id} {status}")
    return 0


def append_history(
    ws: str,
    task_id: str,
    from_status: str | None,
    to_status: str,
    root: Path | None = None,
    now: float | None = None,
) -> None:
    """Append one ``{ts, task, from, to}`` line to the bundle's history file.

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
) -> int:
    if not isinstance(train_id, str) or not _TRAIN_ID.match(train_id):
        print(f"bad train id {train_id}", flush=True)
        return 2
    if status is not None and status not in TRAIN_STATUSES:
        print(f"bad status {status}. Use one of: {', '.join(TRAIN_STATUSES)}", flush=True)
        return 2
    if branch is not None and not branch.startswith("train/"):
        print(f'bad branch {branch}: must start with "train/"', flush=True)
        return 2
    if title is not None and not title.strip():
        print("title required", flush=True)
        return 2
    path = bundle_dir(ws, root)
    if path is None:
        print(f"bundle not found: {ws}", flush=True)
        return 2
    plan_path = path / "plan.json"
    data = _read_json(plan_path)
    if not isinstance(data, dict):
        print("plan.json unreadable", flush=True)
        return 2
    known = _dag_task_ids(data)
    trains = data.get("trains")
    if trains is None:
        trains = []
        data["trains"] = trains
    if not isinstance(trains, list):
        print("plan.json trains is not a list", flush=True)
        return 2
    found = None
    for item in trains:
        if isinstance(item, dict) and item.get("id") == train_id:
            found = item
            break
    if found is None:
        if title is None:
            print("title required for a new train", flush=True)
            return 2
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
            print(f"task not found: {task_id}", flush=True)
            return 2
        for other in trains:
            if other is not found and isinstance(other, dict) and task_id in (other.get("members") or []):
                other["members"].remove(task_id)
                print(f"moved {task_id} from {other.get('id')} to {train_id}", flush=True)
        if task_id not in members:
            members.append(task_id)
    if add and len(members) > TRAIN_MEMBER_LIMIT:
        print(f"train {train_id} has {len(members)} members; split it (limit {TRAIN_MEMBER_LIMIT})", flush=True)
        return 2
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


def print_trains(ws: str, root: Path | None = None) -> int:
    path = bundle_dir(ws, root)
    if path is None:
        print(f"bundle not found: {ws}", flush=True)
        return 2
    data = _read_json(path / "plan.json")
    if not isinstance(data, dict):
        print("plan.json unreadable", flush=True)
        return 2
    trains = data.get("trains") or []
    if not isinstance(trains, list) or not trains:
        print("(no trains)")
        return 0
    dag_status: dict[str, str] = {}
    dag = data.get("dag")
    if isinstance(dag, dict):
        for group in dag.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for task in group.get("tasks") or []:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    dag_status[task["id"]] = task.get("status") or "todo"
    print("TRAIN\tSTATUS\tBRANCH\tPR\tMEMBERS\tSHARED")
    for item in trains:
        if not isinstance(item, dict):
            continue
        members = item.get("members") if isinstance(item.get("members"), list) else []
        member_text = ",".join(f"{member}({dag_status.get(member, '?')})" for member in members) or "-"
        shared = item.get("shared_files") if isinstance(item.get("shared_files"), list) else []
        print(
            "\t".join(
                [
                    _cell(item.get("id")),
                    _cell(item.get("status")),
                    _cell(item.get("branch") or "-"),
                    _cell(item.get("pr") if item.get("pr") is not None else "-"),
                    _cell(member_text),
                    str(len(shared)),
                ]
            )
        )
    return 0


def cmd_ws(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(WS_USAGE, end="")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "list":
        rows = list_bundles()
        if not rows:
            print("(no bundles)")
            return 0
        for item in rows:
            print(f"{item['id']}\t{item['kind']}\t{item['order']}\t{item['title']}")
        return 0
    if cmd == "new":
        if len(rest) != 2:
            print(WS_USAGE, end="", flush=True)
            return 2
        return cmd_new(rest[0], rest[1])
    if cmd == "validate":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return 2
        issues = validate_bundle(rest[0])
        if issues:
            for issue in issues:
                print(issue)
            return 1
        print(f"OK {rest[0]}")
        return 0
    if cmd == "render":
        if len(rest) != 1:
            print(WS_USAGE, end="", flush=True)
            return 2
        path = bundle_dir(rest[0])
        if path is None:
            print(f"bundle not found: {rest[0]}", flush=True)
            return 2
        from dash.planrender import RenderError, render

        try:
            render(path)
        except RenderError as exc:
            print(str(exc), flush=True)
            return 1
        print(f"wrote {path / 'page.html'}")
        return 0
    if cmd == "task":
        if len(rest) < 3:
            print(WS_USAGE, end="", flush=True)
            return 2
        moved = None
        note = None
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
            else:
                print(WS_USAGE, end="", flush=True)
                return 2
        return set_task(rest[0], rest[1], rest[2], moved, note)
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
                        print(f"bad pr {value}: must be an integer", flush=True)
                        return 2
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
            return 2
        return print_trains(rest[0])
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
                        print("reversible must be yes or no", flush=True)
                        return 2
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
            return 2
        return print_approvals(rest[0] if rest else None)
    print(WS_USAGE, end="", flush=True)
    return 2
