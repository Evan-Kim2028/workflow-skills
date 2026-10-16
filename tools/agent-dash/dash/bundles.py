"""Plan bundles on disk: discover, validate, scaffold, and update task status.

Content lives under AGENT_DASH_WORKSTREAMS (default ~/.local/share/agent-dash/workstreams).
One directory per bundle. The tool directory stays free of project text.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dash.plans import configured

STATUSES = ("todo", "pr", "prog", "done", "deferred")
VERDICTS = ("WORTH_IT", "WORTH_IT_IF", "NOT_WORTH_IT")
SIZES = ("S", "M", "L")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MOVED = re.compile(r"^WS\d+$")
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

Status is todo, pr, prog, done, or deferred. deferred may set --moved-to and --note.
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


def load_bundle_api(ws: str) -> dict | None:
    path = bundle_dir(ws)
    if path is None:
        return None
    meta = _read_json(path / "bundle.json")
    plan = _read_json(path / "plan.json")
    if not isinstance(meta, dict) or not isinstance(plan, dict):
        return None
    return {"bundle": meta, "plan": plan}


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
    return {"ws": ws, "cols": cols, "groups": _view_groups(dag.get("groups")), "edges": edges, "phases": phases}


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
                    "tasks": [{"id": f"{ws}.1", "title": "First task", "status": "todo"}],
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
            if status == "deferred":
                moved = task.get("moved_to")
                if moved is not None and (not isinstance(moved, str) or not _MOVED.match(moved)):
                    issues.append(f"plan.json: task {tid} moved_to must look like WS2")
                if "note" in task and not isinstance(task.get("note"), str):
                    issues.append(f"plan.json: task {tid} note must be a string")
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
    for name in ("bundle.json", "plan.json", "layout.json", "page.html", "visuals.html"):
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
        print(f"bad status {status}", flush=True)
        return 2
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
    for group in dag["groups"]:
        if not isinstance(group, dict):
            continue
        for task in group.get("tasks") or []:
            if isinstance(task, dict) and task.get("id") == task_id:
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
    print(f"{ws} {task_id} {status}")
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
    print(WS_USAGE, end="", flush=True)
    return 2
