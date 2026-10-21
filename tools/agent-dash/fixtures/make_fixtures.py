#!/usr/bin/env python3
"""Write synthetic API fixtures. Seeded RNG. Does not read the machine."""

import datetime
import json
import math
import random
import re
import sys
from pathlib import Path

SEED = 1
NOW = 1_761_999_960
COUNT = 150
WS1 = "00000000-0000-0000-0000-000000000001"
PLAN = "00000000-0000-0000-0000-000000000002"
WS2S = "00000000-0000-0000-0000-000000000003"
WS7S = "00000000-0000-0000-0000-000000000007"
MAINS = (WS1, PLAN, WS2S, WS7S)
TK_KEYS = ("backlog", "todo", "doing", "review", "done", "cancelled")
PLAN_WS = ("WS1", "WS2", "WS7", "WS9", "WS10", "WS11")
BOARD = (
    ("WS0", "Stabilize", (3, 1, 0, 0, 2, 0)),
    ("WS1", "Identity", (2, 1, 1, 0, 4, 0)),
    ("WS2", "Public API", (2, 0, 1, 1, 3, 0)),
    ("WS3", "Gateway", (1, 0, 0, 0, 1, 0)),
    ("WS4", "Offers", (1, 1, 0, 0, 1, 0)),
    ("WS5", "Clients", (1, 0, 0, 0, 2, 0)),
    ("WS6", "Cleanup", (2, 0, 0, 0, 0, 1)),
    ("WS7", "Launch", (1, 0, 1, 0, 2, 0)),
    ("WS9", "Relay", (1, 2, 0, 0, 0, 0)),
    ("WS10", "Ledger", (1, 1, 0, 0, 0, 0)),
    ("WS11", "Export", (0, 0, 0, 0, 1, 0)),
    ("WS12", "Sunset", (1, 1, 0, 0, 0, 0)),
)
#: Lifecycle state per fixture workstream (mirrors bundle.json ``state``;
#: absent reads as ``active`` on the API, so every row carries it here).
WS_STATES = {
    "WS0": "active", "WS1": "active", "WS2": "done", "WS3": "active",
    "WS4": "active", "WS5": "paused", "WS6": "active", "WS7": "active",
    "WS9": "planned", "WS10": "planned", "WS11": "archived", "WS12": "dropped",
}
#: States hidden from the default ``/api/workstreams`` scope.
HIDDEN_STATES = ("archived", "dropped")
#: ``depends_on`` per fixture workstream (mirrors bundle.json).
WS_DEPENDS = {"WS9": ["WS6"], "WS10": ["WS8", "WS9"]}
#: ``milestone`` (project phase name) per fixture workstream.
WS_MILESTONE = {"WS9": "Second wave", "WS10": "Second wave", "WS11": "First wave"}
DONE_TOTAL = sum(row[2][4] for row in BOARD)
BACKLOG_TOTAL = sum(row[2][0] for row in BOARD)
SESSION_DEFAULTS = {WS1: "WS1", PLAN: "PLANNING", WS2S: "WS2", WS7S: "WS7"}
PRICES = {
    "opus": (15.0, 75.0, 1.5, 18.75),
    "sonnet": (3.0, 15.0, 0.3, 3.75),
    "haiku": (1.0, 5.0, 0.1, 1.25),
}
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
CWD = "/home/ada/projects/demo"
NOTE = "Seeded synthetic data for the offline preview. Seed 1. No local sessions are read."
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RNG = random.Random(SEED)

sys.path.insert(0, str(ROOT))
from dash import taskmatch  # noqa: E402  (the shared run-to-task rule)
from dash import projecttimeline as timeline_mod  # noqa: E402  (the roadmap lane builder)
from dash import replay as replay_mod  # noqa: E402  (shared replay run entries and done moments)
from dash import maplayout as maplayout_mod  # noqa: E402  (the shared map layout standard)

RAW_PLANS = {
    "WS1": {
        "cols": ["Foundations", "Build", "Wire", "Prove"],
        "groups": [
            {"id": "scope", "col": 0, "title": "Scope", "tasks": [
                {"id": "1", "title": "Write the outline", "status": "done"},
                {"id": "4", "title": "Freeze the types", "status": "done"},
            ]},
            {"id": "store", "col": 0, "title": "Store", "tasks": [
                {"id": "12", "title": "Schema migration", "status": "prog"},
                {"id": "13", "title": "Backfill the empty tables", "status": "todo"},
            ]},
            {"id": "match", "col": 1, "title": "Match", "tasks": [
                {"id": "20", "title": "Matcher core", "status": "done"},
                {"id": "21", "title": "Conflict rule", "status": "pr"},
            ]},
            {"id": "cut", "col": 2, "title": "Cut over", "tasks": [
                {"id": "30", "title": "First source", "status": "prog"},
                {"id": "31", "title": "Second source", "status": "todo"},
            ]},
            {"id": "prove", "col": 3, "title": "Prove", "tasks": [
                {"id": "40", "title": "Gate the sample set", "status": "todo"},
            ]},
        ],
        "edges": [["scope", "store"], ["store", "match"], ["match", "cut"], ["cut", "prove"]],
        "phases": [
            {"name": "Shape", "goal": "Types and the store agree.", "gate": "The sample set loads."},
            {"name": "Cut over", "goal": "One source reads the new store.", "gate": "The gate task is done."},
        ],
        "trains": [
            {"id": "T1", "title": "store migration", "members": ["12", "13"], "status": "planned",
             "shared_files": ["store/schema.sql"], "gate": "pytest store/tests/test_schema.py",
             "note": "First guess at planning time."},
            {"id": "T2", "title": "matcher cutover", "branch": "train/matcher-cutover",
             "members": ["20", "21"], "status": "pr", "pr": 5361,
             "shared_files": ["match/rules.py"], "gate": "pytest match/tests/test_rules.py"},
        ],
    },
    "WS2": {
        "cols": ["Contract", "Routes", "Clients", "Remove"],
        "groups": [
            {"id": "contract", "col": 0, "title": "Contract", "tasks": [
                {"id": "WS2.1", "title": "Publish the route list", "status": "done"},
            ]},
            {"id": "routes", "col": 1, "title": "Routes", "tasks": [
                {"id": "WS2.3", "title": "Read route", "status": "done"},
                {"id": "WS2.4", "title": "List route", "status": "done"},
            ]},
            {"id": "clients", "col": 2, "title": "Clients", "tasks": [
                {"id": "WS2.8", "title": "Point the client at the new route", "status": "done"},
            ]},
            {"id": "remove", "col": 3, "title": "Remove", "tasks": [
                {"id": "WS2.9", "title": "Delete the old handler", "status": "done"},
            ]},
        ],
        "edges": [["contract", "routes"], ["routes", "clients"], ["routes", "remove"]],
        "phases": [
            {"name": "Routes", "goal": "The read route answers.", "gate": "A client can call it."},
            {"name": "Remove", "goal": "The old handler is gone.", "gate": "No caller remains."},
        ],
    },
    "WS9": {
        "cols": ["Wait", "Relay"],
        "groups": [
            {"id": "wait", "col": 0, "title": "Wait", "tasks": [
                {"id": "WS9.G1", "title": "Cleanup batch is merged", "status": "todo", "kind": "gate",
                 "signal": "WS6 cutover batch is merged", "confirmed_at": "2025-10-22T12:00:00Z", "fresh_hours": 24},
            ]},
            {"id": "relay", "col": 1, "title": "Relay", "tasks": [
                {"id": "WS9.1", "title": "Relay the seeded batch", "status": "todo", "deps": ["WS9.G1"],
                 "dep_notes": {"WS9.G1": "run only after the cleanup batch lands"}},
                {"id": "WS9.2", "title": "Prove the relay", "status": "todo", "deps": ["WS9.1"]},
            ]},
        ],
        "edges": [["wait", "relay"]],
        "phases": [
            {"name": "Wait", "goal": "The cleanup batch lands.", "gate": "The gate task is met."},
            {"name": "Relay", "goal": "The seeded batch relays.", "gate": "The prove task is done."},
        ],
    },
    "WS10": {
        "cols": ["Wait", "Ledger"],
        "groups": [
            {"id": "wait", "col": 0, "title": "Wait", "tasks": [
                {"id": "WS10.G1", "title": "Launch feed is steady", "status": "todo", "kind": "gate",
                 "signal": "WS8 launch feed is steady", "confirmed_at": "2025-10-20T12:00:00Z", "fresh_hours": 24},
                {"id": "WS10.G2", "title": "Relay loop is live", "status": "todo", "kind": "gate",
                 "signal": "WS9 relay loop is live", "confirmed_at": "2025-10-31T20:00:00Z", "fresh_hours": 24},
            ]},
            {"id": "ledger", "col": 1, "title": "Ledger", "tasks": [
                {"id": "WS10.1", "title": "Ledger the seeded rows", "status": "todo", "deps": ["WS10.G1", "WS10.G2"],
                 "dep_notes": {"WS10.G1": "run only after the feed is steady"}},
            ]},
        ],
        "edges": [["wait", "ledger"]],
        "phases": [
            {"name": "Wait", "goal": "Both signals confirm.", "gate": "Both gate tasks are met."},
            {"name": "Ledger", "goal": "The seeded rows ledger.", "gate": "The ledger task is done."},
        ],
    },
    "WS11": {
        "cols": ["Shape", "Ship"],
        "groups": [
            {"id": "shape", "col": 0, "title": "Shape", "tasks": [
                {"id": "WS11.1", "title": "Shape the export", "status": "done"},
                {"id": "WS11.2", "title": "Freeze the export columns", "status": "done"},
            ]},
            {"id": "ship", "col": 1, "title": "Ship", "tasks": [
                {"id": "WS11.3", "title": "Ship the export", "status": "done", "deps": ["WS11.1", "WS11.2"]},
            ]},
        ],
        "edges": [["shape", "ship"]],
        "phases": [
            {"name": "Shape", "goal": "The export columns freeze.", "gate": "The seeded export loads."},
            {"name": "Ship", "goal": "The export ships.", "gate": "The ship task is done."},
        ],
    },
    "WS7": {
        "cols": ["Frame", "Pages", "Data", "Ship"],
        "groups": [
            {"id": "frame", "col": 0, "title": "Frame", "tasks": [
                {"id": "WS7.FRAME", "title": "App shell", "status": "done"},
            ]},
            {"id": "pages", "col": 1, "title": "Pages", "tasks": [
                {"id": "WS7.LANDING", "title": "Landing page", "status": "prog"},
                {"id": "WS7.NOTES", "title": "Notes page", "status": "todo"},
            ]},
            {"id": "data", "col": 2, "title": "Data", "tasks": [
                {"id": "WS7.FEED", "title": "Feed the page from the API", "status": "pr"},
            ]},
            {"id": "ship", "col": 3, "title": "Ship", "tasks": [
                {"id": "WS7.CHECK", "title": "Check the empty and error states", "status": "todo"},
            ]},
        ],
        "edges": [["frame", "pages"], ["pages", "data"], ["data", "ship"]],
        "phases": [
            {"name": "Pages", "goal": "Landing renders with fixture data.", "gate": "The notes page has a layout."},
            {"name": "Ship", "goal": "Empty and error states are checked.", "gate": "The check task is done."},
        ],
    },
}


def attribute(label, session_id):
    """Regex rules, then the example session map. No exact-label table."""
    text = label or ""
    if text.startswith("ws") and len(text) > 2 and text[2].isdigit():
        end = 2
        while end < len(text) and text[end].isdigit():
            end += 1
        return f"WS{int(text[2:end])}", "regex"
    dot = text.find("WS")
    if dot >= 0:
        rest = text[dot + 2 :]
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits and rest[len(digits):].startswith("."):
            return f"WS{int(digits)}", "regex"
    if text.startswith("g-"):
        return "WS1", "regex"
    if text.startswith("dash-"):
        return "TOOLS", "regex"
    if session_id in SESSION_DEFAULTS:
        return SESSION_DEFAULTS[session_id], "session-default"
    return None, None


def cost_usd(model, tier, inp, out, cache_read, cache_write):
    fam = None
    text = (model or "").lower()
    for name in ("opus", "sonnet", "haiku"):
        if name in text or tier == name:
            fam = name
            break
    if fam is None or fam not in PRICES:
        return None
    pin, pout, pread, pwrite = PRICES[fam]
    raw = (inp * pin + out * pout + cache_read * pread + cache_write * pwrite) / 1_000_000
    return round(raw, 4)


def synthetic_plan(ws):
    """Stand-in DAG used when a workstream has no plan file."""
    cols = ["Shape", "Build", "Wire", "Check", "Ship"]
    names = ["Scope", "Types", "Store", "Readers", "Cutover", "Prove"]
    cycle = ["done", "prog", "pr", "todo"]
    groups = []
    n = 0
    for index, name in enumerate(names):
        width = 5 if index == len(names) - 1 else 4
        tasks = []
        for _ in range(width):
            n += 1
            tasks.append({"id": str(n), "title": f"{name} {n}", "status": cycle[n % 4]})
        groups.append({"id": name.lower(), "col": min(index, 4), "title": name, "tasks": tasks})
    edges = [[groups[i]["id"], groups[i + 1]["id"]] for i in range(len(groups) - 1)]
    return {
        "ws": ws,
        "cols": cols,
        "groups": groups,
        "edges": edges,
        "phases": [{"name": "Shape", "goal": "The stand-in has a column for each stage.", "gate": "Twenty-five tasks are listed."}],
        "_note": "Synthesized stand-in for a workstream with no plan file.",
    }


def _tokens():
    return {
        "input": RNG.randint(20, 4000),
        "output": RNG.randint(10, 2000),
        "cache_read": RNG.randint(0, 8000),
        "cache_write": RNG.randint(0, 1500),
    }


def _clock(source):
    return {
        "claude-main": "transcript",
        "claude-subagent": "transcript",
        "claude-workflow": "transcript",
        "muse": "muse_log",
        "glm": "manifest",
        "grok": "manifest",
        "shell": "mtime",
    }.get(source, "mtime")


def _family(source):
    if source in ("claude-main", "claude-subagent", "claude-workflow", "shell"):
        return "claude"
    return source if source in ("grok", "muse", "glm") else "claude"


def _run(n, source, tier, model, label, session, parent, status, started, ended):
    ws, confidence = attribute(label, None if parent else session)
    if parent and confidence == "session-default":
        ws, confidence = None, None
    tokens = _tokens()
    cost = cost_usd(model, tier, tokens["input"], tokens["output"], tokens["cache_read"], tokens["cache_write"])
    end = ended if ended is not None else NOW
    row = {
        "run_id": f"{source}:{session}:{label}:{n}",
        "source": source,
        "tier": tier,
        "model": model,
        "label": label,
        "workstream": ws,
        "ws_confidence": confidence,
        "session_id": session,
        "parent_session_id": parent,
        "status": status,
        "verdict": None,
        "started_at": started,
        "ended_at": ended,
        "elapsed_s": round(max(0.0, end - started), 1),
        "clock_source": _clock(source),
        "out_bytes": 0 if status == "died_early" else 80 + (n % 40),
        "out_growing": status == "running",
        "err_bytes": 24 if status in ("killed", "died_early") else 0,
        "pid": 41000 + n if status == "running" else None,
        "rss_kb": 90000 + n if status == "running" else None,
        "tokens": tokens,
        "cost_usd": cost,
    }
    if source == "claude-workflow":
        row["children"] = 2 + (n % 3)
    if status == "done":
        row["verdict"] = ("PASS", "PARTIAL", "BLOCKED", "APPROVE", "APPROVE_WITH_CHANGES", "REQUEST_CHANGES")[n % 6]
    return row


def _spec(kind, n):
    table = {
        "opus": ("claude-main", "opus", "claude-opus-4"),
        "haiku": ("claude-subagent", "haiku", "claude-haiku-4"),
        "sonnet": ("claude-subagent", "sonnet", "claude-sonnet-4"),
        "muse": ("muse", "muse", "muse-1"),
        "glm": ("glm", "glm", "glm-4"),
        "grok": ("grok", "grok", "grok-4"),
        "shell": ("shell", "haiku", "claude-haiku-4"),
        "workflow": ("claude-workflow", "sonnet", "claude-sonnet-4"),
    }
    return table[kind]


def build_runs():
    runs = []
    n = 0

    def add(kind, label, session, parent, status, started, ended):
        nonlocal n
        source, tier, model = _spec(kind, n)
        runs.append(_run(n, source, tier, model, label, session, parent, status, started, ended))
        n += 1

    add("muse", "ws7-landing", "00000000-0000-0000-0000-000000000010", WS1, "running", NOW - 4000, None)
    add("muse", "ws1-12", "00000000-0000-0000-0000-000000000011", WS1, "running", NOW - 120, None)
    add("grok", "ws2-3", "00000000-0000-0000-0000-000000000030", WS2S, "running", NOW - 120, None)
    add("opus", "ws1-4", WS1, None, "running", NOW - 120, None)
    add("glm", "ws7-notes", "00000000-0000-0000-0000-000000000020", WS7S, "running", NOW - 120, None)
    add("muse", "ws2-4", "00000000-0000-0000-0000-000000000012", WS2S, "running", NOW - 120, None)
    add("haiku", "ws1-4", "00000000-0000-0000-0000-000000000040", WS1, "running", NOW - 120, None)
    add("grok", "ws7-notes", "00000000-0000-0000-0000-000000000031", WS7S, "running", NOW - 120, None)

    burst_start, burst_end = NOW - 1800, NOW - 1200
    burst = (
        [("grok", "g-review"), ("grok", "dash-preview")]
        + [("grok", label) for label in ("ws1-4", "ws2-4", "ws7-notes", "ws1-4", "ws2-4", "ws7-notes")]
        + [("muse", label) for label in ("ws1-schema", "ws2-3", "ws7-notes", "ws1-4", "ws2-4", "ws7-landing")]
        + [("glm", label) for label in ("ws1-4", "ws2-4", "ws7-notes", "ws1-4", "ws2-4")]
        + [("haiku", label) for label in ("ws1-4", "ws2-4", "ws7-notes", "ws1-4", "ws2-4")]
    )
    assert len(burst) == 24
    for kind, label in burst:
        sid = f"00000000-0000-0000-0000-{200 + n:012d}"
        parent = MAINS[n % 4]
        add(kind, label, sid, parent, "done", burst_start, burst_end)

    samples = (
        ("killed", "ws7-notes", "muse"),
        ("killed", "ws2-4", "grok"),
        ("died_early", "ws1-4", "glm"),
        ("died_early", "ws2-3", "muse"),
        ("unknown", "ws1-schema", "haiku"),
        ("unknown", "ws7-notes", "grok"),
    )
    for status, label, kind in samples:
        sid = f"00000000-0000-0000-0000-{300 + n:012d}"
        add(kind, label, sid, WS1, status, NOW - 3500, NOW - 3400)

    add("muse", "plan-notes", PLAN, None, "done", NOW - 20000, NOW - 19000)
    add("opus", "demo-main", WS1, None, "done", NOW - 21000, NOW - 20000)
    add("opus", "demo-plan", PLAN, None, "done", NOW - 21000, NOW - 20000)
    add("opus", "ws2-4", WS2S, None, "done", NOW - 21000, NOW - 20000)
    add("opus", "ws7-notes", WS7S, None, "done", NOW - 21000, NOW - 20000)

    kinds = ("opus", "haiku", "sonnet", "muse", "glm", "grok", "shell", "workflow")
    labels = ("ws1-4", "ws2-4", "ws7-notes")
    need = COUNT - len(runs)
    if need < 0:
        raise SystemExit(f"fixed runs already {len(runs)}")
    begin = NOW - 6 * 3600
    for i in range(need):
        kind = kinds[i % len(kinds)]
        label = labels[i % 3]
        slot = i // 2
        started = begin + slot * 100
        ended = started + 80
        if kind == "opus":
            session, parent = MAINS[i % 4], None
            label = {WS1: "ws1-4", PLAN: "demo-plan", WS2S: "ws2-4", WS7S: "ws7-notes"}[session]
        else:
            session = f"00000000-0000-0000-0000-{400 + i:012d}"
            parent = MAINS[i % 4]
        add(kind, label, session, parent, "done", started, ended)
    if len(runs) != COUNT:
        raise SystemExit(f"expected {COUNT} runs, got {len(runs)}")
    return runs


PHASE_ORDER = ("planning", "execution", "review", "coordination", "unclassified")


def _phase_of_label(label, parent):
    """Mirror of catalog/phases.json for fixture runs (mirrored, never
    imported: fixtures must not read the machine). Explicit ws marks,
    review affixes, planning/review/doing words, unmapped lead runs,
    else unclassified."""
    text = label or ""
    hit = re.search(
        r"ws\d+[-_. ]?(plan(?:ning)?|exec(?:ution)?|review|rev|coord(?:ination)?)\b",
        text, re.IGNORECASE)
    if hit:
        token = hit.group(1).lower()
        if token.startswith("plan"):
            return "planning"
        if token.startswith("exec"):
            return "execution"
        if token.startswith("coord"):
            return "coordination"
        return "review"
    if re.search(r"^r-|^rev-|^fix\\b|-rev\\d*$|-fix\\d*$|-rev-", text, re.IGNORECASE):
        return "review"
    if re.search(r"ws\d+[-._]\d", text, re.IGNORECASE):
        return "execution"
    low = text.lower()
    planning = ("plan", "design", "spec", "audit", "scope", "research", "adr",
                "measure", "map", "survey", "architect", "baseline", "overview", "docs")
    if any(word in low for word in planning):
        return "planning"
    review = ("review", "verify", "qa", "adversarial", "approv", "check", "fix")
    if any(word in low for word in review):
        return "review"
    doing = ("build", "ship", "deploy", "train-", "lander", "landing", "cut",
             "ops", "emit", "rebuild", "launch", "gold")
    if any(word in low for word in doing):
        return "execution"
    if any(word in low for word in ("agentdash", "harness", "sync", "status")):
        return "coordination"
    if not parent:
        return "planning"
    return "unclassified"


def _phase_split(mine):
    """Lifetime {phase: {tokens, cost_usd}} over fixture runs, endpoint shape."""
    out = {phase: {"tokens": 0, "cost_usd": 0.0} for phase in PHASE_ORDER}
    for run in mine:
        phase = _phase_of_label(run.get("label"), run.get("parent_session_id"))
        billed = sum(run["tokens"][key] for key in TOKEN_KEYS)
        out[phase]["tokens"] += billed
        if run["cost_usd"] is not None:
            out[phase]["cost_usd"] = round(out[phase]["cost_usd"] + run["cost_usd"], 4)
    return out


def _is_agent(run):
    """Mirror of dash.store._is_counted_agent: lead sessions and shell rows are not agents."""
    source = run.get("source") or ""
    if source in ("claude-main", "shell", "dagster", ""):
        return False
    tier = (run.get("tier") or "").lower()
    if source == "grok" or tier == "grok":
        return True
    if source == "muse" or tier == "muse":
        return True
    if source == "glm" or tier == "glm":
        return True
    return source.startswith("claude") or tier in ("opus", "sonnet", "haiku")


def _snapshot(runs, now, ws):
    chosen = [run for run in runs if ws is None or run["workstream"] == ws]
    running = []
    recent = []
    for run in chosen:
        if run["status"] == "running":
            view = dict(run)
            view.pop("children", None)
            running.append(view)
        elif run["ended_at"] is not None and now - 24 * 3600 <= run["ended_at"] <= now + 5:
            view = dict(run)
            view.pop("children", None)
            recent.append(view)
    recent.sort(key=lambda run: run["ended_at"], reverse=True)
    recent = recent[:50]
    tokens = {key: 0 for key in TOKEN_KEYS}
    tokens["reasoning"] = 0
    cost = 0.0
    unpriced = 0
    moment = datetime.datetime.fromtimestamp(now).astimezone()
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    buckets = {}
    for run in chosen:
        for key in TOKEN_KEYS:
            tokens[key] += run["tokens"][key]
        if run["cost_usd"] is None:
            unpriced += 1
        else:
            cost += run["cost_usd"]
        key = run["workstream"] or "unassigned"
        row = buckets.setdefault(key, {"ws": key, "live_agents": 0, "finished_in_range": 0, "finished_today": 0, "tokens": 0, "cost_usd": 0.0, "last_activity": 0})
        if run["status"] == "running" and _is_agent(run):
            row["live_agents"] += 1
        if _is_agent(run) and run["status"] in ("done", "killed", "died_early") and run.get("ended_at") is not None and now - 24 * 3600 <= run["ended_at"] <= now:
            row["finished_in_range"] += 1
            if today <= run["ended_at"] <= now:
                row["finished_today"] += 1
        row["tokens"] += sum(run["tokens"][part] for part in TOKEN_KEYS)
        if run["cost_usd"] is not None:
            row["cost_usd"] = round(row["cost_usd"] + run["cost_usd"], 4)
        stamp = run["ended_at"] if run["ended_at"] is not None else run["started_at"]
        row["last_activity"] = max(row["last_activity"], stamp)
    by_ws = [buckets[key] for key in sorted(buckets)]
    for row in by_ws:
        row["tokens_all_time"] = row["tokens"]
        row["cost_usd_all_time"] = row["cost_usd"]
    stamps = [run["ended_at"] if run["ended_at"] is not None else run["started_at"] for run in chosen]
    return {
        "now": now,
        "seq": 4,
        "counts": _counts(chosen, now),
        "running": running,
        "recent": recent,
        "totals": {"tokens": tokens, "cost_usd": round(cost, 4), "unpriced_runs": unpriced},
        "totals_all_time": {"tokens": dict(tokens), "cost_usd": round(cost, 4)},
        "last_activity": max(stamps) if stamps else None,
        "by_workstream": by_ws,
        "_note": NOTE,
    }


def _reconcile(runs, now):
    snap = _snapshot(runs, now, None)
    totals = snap["totals"]["tokens"]
    rows = [row for row in snap["by_workstream"] if row["ws"] != "unassigned"]
    unassigned = [row for row in snap["by_workstream"] if row["ws"] == "unassigned"]
    empty = {"ws": "unassigned", "live_agents": 0, "finished_in_range": 0, "finished_today": 0, "tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0, "cost_usd_all_time": 0.0, "last_activity": None}
    return {
        "now": now,
        "since": now - 24 * 3600,
        "until": now,
        "all": {
            "live_agents": snap["counts"]["live_agents"],
            "finished_in_range": snap["counts"]["finished_in_range"],
            "finished_today": snap["counts"]["finished_today"],
            "tokens": sum(totals[key] for key in TOKEN_KEYS),
            "cost_usd": snap["totals"]["cost_usd"],
        },
        "all_time": {
            "tokens": sum(totals[key] for key in TOKEN_KEYS),
            "cost_usd": snap["totals"]["cost_usd"],
        },
        "rows": rows,
        "unassigned": unassigned[0] if unassigned else empty,
        "mismatches": [],
        "_note": NOTE,
    }


def _usage(runs, key_of):
    buckets = {}
    for run in runs:
        hour = int(run["started_at"] // 3600) * 3600
        key = key_of(run)
        row = buckets.setdefault((hour, key), {"t": hour, "key": key, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost_usd": 0.0})
        for part in TOKEN_KEYS:
            row[part] += run["tokens"][part]
        if run["cost_usd"] is not None:
            row["cost_usd"] = round(row["cost_usd"] + run["cost_usd"], 4)
    return {"buckets": [buckets[key] for key in sorted(buckets)], "_note": NOTE}


def _concurrency(runs, now):
    start = min(int(run["started_at"]) for run in runs)
    start = start // 60 * 60
    end = now // 60 * 60
    points = []
    t = start
    while t <= end:
        point = {"t": t, "grok": 0, "muse": 0, "glm": 0, "claude": 0}
        for run in runs:
            stop = run["ended_at"] if run["ended_at"] is not None else now + 1
            if run["started_at"] <= t < stop:
                point[_family(run["source"])] += 1
        points.append(point)
        t += 60
    return {"points": points, "_note": NOTE}


def _sessions(runs):
    grouped = {}
    for run in runs:
        sid = run["session_id"]
        if not sid:
            continue
        grouped.setdefault(sid, []).append(run)
    rows = []
    for sid, items in grouped.items():
        tokens = sum(sum(run["tokens"][key] for key in TOKEN_KEYS) for run in items)
        cost = round(sum(run["cost_usd"] or 0 for run in items), 4)
        label = items[0]["label"]
        ws, _conf = attribute(label, sid)
        tool = _family(items[0]["source"])
        if tool == "grok":
            tool = "cursor"
        rows.append({
            "session_id": sid,
            "tool": "claude" if items[0]["source"].startswith("claude") else tool,
            "cwd": CWD,
            "label": label,
            "workstream": ws,
            "started_at": min(run["started_at"] for run in items),
            "status": "busy" if any(run["status"] == "running" for run in items) else "done",
            "runs": len(items),
            "tokens": tokens,
            "cost_usd": cost,
        })
    rows.sort(key=lambda row: row["started_at"])
    return {"sessions": rows, "_note": NOTE}


def _workstreams(runs, now, plans, scope="default"):
    """Fixture mirror of GET /api/workstreams: the same rollup row plus the
    plan progress object (null when the workstream has no plan) plus the
    lifecycle fields (state, state_changed_at, archived_at, depends_on,
    milestone; absent reads as active on the API, so every row carries
    them here). The default scope drops archived/dropped rows;
    ``scope="all"`` keeps every state."""
    moment = datetime.datetime.fromtimestamp(now).astimezone()
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    for code, name, counts in BOARD:
        state = WS_STATES.get(code, "active")
        if scope != "all" and state in HIDDEN_STATES:
            continue
        tk = dict(zip(TK_KEYS, counts))
        mine = [run for run in runs if run["workstream"] == code]
        tokens = sum(sum(run["tokens"][key] for key in TOKEN_KEYS) for run in mine)
        cost = round(sum(run["cost_usd"] or 0 for run in mine), 4)
        live = sum(1 for run in mine if run["status"] == "running" and _is_agent(run))
        fin = sum(1 for run in mine if _is_agent(run) and run["status"] in ("done", "killed", "died_early") and run.get("ended_at") is not None and now - 24 * 3600 <= run["ended_at"] <= now)
        plan = plans.get(code)
        rows.append({
            "ws": code,
            "name": name,
            "tk": tk,
            "state": state,
            "state_changed_at": now - 3 * 86400 if state != "active" else None,
            "archived_at": now - 2 * 86400 if state == "archived" else None,
            "depends_on": list(WS_DEPENDS.get(code, [])),
            "milestone": WS_MILESTONE.get(code),
            "progress": dict(plan["progress"]) if plan and isinstance(plan.get("progress"), dict) else None,
            "live_agents": live,
            "finished_in_range": fin,
            "finished_today": sum(1 for run in mine if _is_agent(run) and run["status"] in ("done", "killed", "died_early") and run.get("ended_at") is not None and today <= run["ended_at"] <= now),
            "tokens": tokens,
            "cost_usd": cost,
            "tokens_all_time": tokens,
            "cost_usd_all_time": cost,
            "by_phase": _phase_split(mine),
            "last_activity": max([run["ended_at"] if run["ended_at"] is not None else run["started_at"] for run in mine] or [0]) or None,
            "plan": code in PLAN_WS,
        })
    return {"workstreams": rows, "_note": NOTE}


def _project(runs, now, plans):
    """Fixture mirror of GET /api/project: the project.json identity plus
    the phase roll-up (state per phase), counts by workstream state, and
    lifetime totals. Missing project.json means name null on the API; the
    fixture always carries one so the offline preview lands on it."""
    counts = {state: 0 for state in ("planned", "active", "paused", "done", "archived", "dropped")}
    for _code, _name, _counts in BOARD:
        counts[WS_STATES.get(_code, "active")] += 1
    tokens = {key: 0 for key in TOKEN_KEYS}
    cost = 0.0
    for run in runs:
        for key in TOKEN_KEYS:
            tokens[key] += run["tokens"][key]
        if run["cost_usd"] is not None:
            cost += run["cost_usd"]
    billed = sum(tokens[key] for key in TOKEN_KEYS)
    return {
        "id": None,
        "name": "Seeded demo project",
        "short": "demo",
        "description": "Seeded synthetic project for the offline preview.",
        "goals": ["Seeded lanes stay green", "The offline preview renders every view"],
        "started": "2025-10-01",
        "target": None,
        "phases": [
            {"id": "wave-1", "title": "First wave", "summary": "Seeded lanes land.",
             "gate": "All seeded checks pass.", "state": "done"},
            {"id": "wave-2", "title": "Second wave", "summary": "Planned lanes start.",
             "gate": "Gates confirm and approvals land.", "state": "active"},
        ],
        "counts": counts,
        "totals": {"tokens": billed, "cost_usd": round(cost, 4), "runs": len(runs)},
        "data_root_hash": "seeded0000",
        "_note": NOTE,
    }


def _timeline(runs, now, plans):
    """Fixture mirror of GET /api/project/timeline: one lane per fixture
    workstream from the seeded runs, plan progress, and the project block.
    Fixture bundles carry no history files, so active lanes without seeded
    runs read before_history and planned lanes read as estimates — the
    same honesty rules as the live endpoint."""
    bundles = []
    for i, (code, name, _counts) in enumerate(BOARD):
        bundles.append({
            "id": code, "title": name, "short": code, "order": (i + 1) * 10,
            "kind": "workstream", "state": WS_STATES.get(code, "active"),
            "state_changed_at": now - 30 * 86400, "archived_at": None,
            "depends_on": list(WS_DEPENDS.get(code, [])), "milestone": WS_MILESTONE.get(code),
        })
    usages = {}
    for code, _name, _ in BOARD:
        mine = [run for run in runs if run.get("workstream") == code]
        starts = [run["started_at"] for run in mine if run.get("started_at") is not None]
        stamps = []
        for run in mine:
            stamp = run.get("ended_at") if run.get("ended_at") is not None else run.get("started_at")
            if stamp is not None:
                stamps.append(stamp)
        tokens = sum(run["tokens"][key] for run in mine for key in TOKEN_KEYS)
        cost = sum(run["cost_usd"] for run in mine if run["cost_usd"] is not None)
        usages[code] = {"runs": len(mine), "tokens": tokens, "cost": cost,
                        "first_run_at": min(starts) if starts else None,
                        "last_run_at": max(stamps) if stamps else None}
    progresses = {}
    for code, _name, _ in BOARD:
        if code in plans:
            progresses[code] = dict(plans[code]["progress"])
        else:
            progresses[code] = {"done": 0, "total": 0, "pct": 0}
    project = _project(runs, now, plans)
    doc = timeline_mod.build_timeline(
        bundles, {}, progresses, usages,
        {"started": project["started"], "target": project["target"], "phases": project["phases"]}, now)
    doc["_note"] = NOTE
    return doc
def _projects():
    """Fixture mirror of GET /api/projects: two seeded registry rows.
    Seeded names and loopback ports only, never filesystem paths: the
    endpoint carries id, name, port, running, and current, and the
    offline preview uses it to draw the header switcher menu."""
    return {
        "projects": [
            {"id": "demo", "name": "Seeded demo project", "port": 8765,
             "running": True, "current": True},
            {"id": "second", "name": "Seeded second project", "port": 8766,
             "running": False, "current": False},
        ],
        "current": "demo",
        "_note": NOTE,
    }


def _archive(now, plans):
    """Fixture mirror of GET /api/archive: one frozen summary card per
    archived workstream, newest first, plus the archived lifetime share.
    The card mirrors dash/lifecycle.py freeze_summary plus id, title,
    archived_at, and the closing-report basename (never a path or body)."""
    plan = plans["WS11"]
    cards = [{
        "ws": "WS11",
        "state": "archived",
        "frozen_at": now - 2 * 86400,
        "started_at": now - 30 * 86400,
        "finished_at": now - 2 * 86400,
        "duration_s": 28 * 86400,
        "tasks": dict(plan["progress"]),
        "trains": {"total": 0, "by_status": {}},
        "approvals": {"pending": 0, "approved": 2, "denied": 0, "total": 2},
        "usage": {"tokens_all_time": 0, "cost_usd_all_time": 0.0, "reachable": True,
                  "url": "seeded", "note": "seeded fixture: no runs attribute to WS11"},
        "closing_report": True,
        "note": None,
        "id": "WS11",
        "title": "Export",
        "archived_at": now - 2 * 86400,
        "report": "2025-10-30-closing.md",
    }]
    return {
        "archive": cards,
        "totals": {"workstreams": len(cards), "tokens": 0, "cost_usd": 0.0, "runs": 0},
        "_note": NOTE,
    }


def _approvals():
    """Fixture mirror of GET /api/approvals?ws=all: the long-standing
    seeded blocks plus pending items on the planned lanes, so the
    start-readiness table can count approvals pending per workstream."""
    return {
        "approvals": [
            {"ws": "WS1", "decided_at": "2026-04-02T15:00:00Z", "decided_by": "owner", "items": [
                {"id": "sample-load", "step": "Load the sample set before cutover",
                 "status": "pending", "reversible": False, "conditions": "The sample set is on disk."},
                {"id": "route-name", "step": "Keep the public route name",
                 "status": "approved", "reversible": True, "conditions": "Clients already call this route."},
            ]},
            {"ws": "WS2", "decided_at": "2026-04-02T15:04:00Z", "decided_by": "owner", "items": [
                {"id": "route-list", "step": "Publish the route list",
                 "status": "approved", "reversible": True, "conditions": "The contract is frozen."},
                {"id": "client-contract", "step": "Freeze the client contract",
                 "status": "approved", "reversible": True, "conditions": "Callers can read the new shape."},
                {"id": "error-shape", "step": "Keep the old error shape",
                 "status": "approved", "reversible": False, "conditions": "Existing clients parse this body."},
                {"id": "read-route", "step": "Ship the read route",
                 "status": "approved", "reversible": True, "conditions": "The sample set loads."},
                {"id": "old-client", "step": "Retire the duplicate client",
                 "status": "approved", "reversible": False, "conditions": "No caller still imports it."},
            ]},
            {"ws": "WS7", "decided_at": "2026-04-02T15:10:00Z", "decided_by": "owner", "items": [
                {"id": "checklist", "step": "Publish the launch checklist",
                 "status": "approved", "reversible": True, "conditions": "The gate task is written down."},
                {"id": "rollback", "step": "Keep the rollback note",
                 "status": "approved", "reversible": True, "conditions": "The note names the previous route."},
                {"id": "hold-note", "step": "Hold the launch note",
                 "status": "denied", "reversible": False, "conditions": "The gate task is still open."},
            ]},
            {"ws": "WS9", "decided_at": None, "decided_by": None, "items": [
                {"id": "relay-go", "step": "Start the relay once the cleanup batch lands",
                 "status": "pending", "reversible": True, "conditions": "The gate task is met."},
                {"id": "relay-scope", "step": "Freeze the relay scope",
                 "status": "pending", "reversible": True, "conditions": "The seeded batch is listed."},
            ]},
            {"ws": "WS10", "decided_at": None, "decided_by": None, "items": [
                {"id": "ledger-go", "step": "Start the ledger once both signals confirm",
                 "status": "pending", "reversible": False, "conditions": "Both gate tasks are met."},
            ]},
        ],
        "_note": NOTE,
    }


def _decisions():
    """Fixture mirror of GET /api/decisions: newest-first rows with status
    pills, one http(s) link (renders as an anchor), one plain-text link,
    one absent link, and one undated row that sinks to the end."""
    rows = [
        {"id": "start-relay", "title": "Start the relay once the cleanup batch lands",
         "status": "pending", "owner": "lead", "date": "2026-04-03",
         "workstream": "WS9", "area": "Schedule", "link": None},
        {"id": "freeze-relay-scope", "title": "Freeze the relay scope",
         "status": "approved", "owner": "owner", "date": "2026-04-02",
         "workstream": "WS9", "area": "Scope", "link": "https://example.com/notes/relay-scope"},
        {"id": "hold-launch-note", "title": "Hold the launch note",
         "status": "denied", "owner": "owner", "date": "2026-04-01",
         "workstream": "WS7", "area": "Launch", "link": "plan:WS7"},
        {"id": "retire-dup-client", "title": "Retire the duplicate client",
         "status": "approved", "owner": "owner", "date": None,
         "workstream": "WS2", "area": "Cleanup"},
    ]
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"decisions": rows, "counts": counts, "source": {"present": True}, "_note": NOTE}


def _feed(now):
    """Fixture mirror of GET /api/project/feed: day-grouped newest-first
    lines across two days, one collapsed bulk minute, one archive move,
    one decided approval, one finish summary, and an earliest stamp for
    the honest-gap footer."""
    day = 86400
    t0 = now - 300
    t1 = now - 3600
    t2 = now - day - 100
    t3 = now - day - 7200
    items = [
        {"ts": t0, "day": "2025-11-01", "ws": "WS7", "name": "Launch", "kind": "run",
         "text": "4 agents finished in Launch (WS7); 3 pass, 1 partial", "target": "WS7"},
        {"ts": t0 - 60, "day": "2025-11-01", "ws": None, "name": "", "kind": "tasks",
         "text": "3 tasks moved to done across Identity (WS1) (2), Launch (WS7) (1)", "target": None},
        {"ts": t1, "day": "2025-11-01", "ws": "WS9", "name": "Relay", "kind": "state",
         "text": "Relay (WS9) moved from planned to active", "target": "WS9"},
        {"ts": t1 - 30, "day": "2025-11-01", "ws": "WS7", "name": "Launch", "kind": "approval",
         "text": "Launch (WS7): Hold the launch note denied", "target": "WS7"},
        {"ts": t2, "day": "2025-10-31", "ws": "WS1", "name": "Identity", "kind": "task",
         "text": "Identity (WS1): task WS1.21 moved to pr", "target": "WS1"},
        {"ts": t2 - 45, "day": "2025-10-31", "ws": "WS11", "name": "Export", "kind": "archive",
         "text": "Export (WS11) was archived", "target": "WS11"},
        {"ts": t3, "day": "2025-10-31", "ws": "WS1", "name": "Identity", "kind": "first-run",
         "text": "first agent run in Identity (WS1)", "target": "WS1"},
    ]
    items.sort(key=lambda item: item["ts"], reverse=True)
    return {
        "feed": items,
        "total": len(items),
        "limit": 50,
        "earliest_ts": min(item["ts"] for item in items),
        "now": now,
        "_note": NOTE,
    }


def _progress(groups):
    """Mirror of dash.plans.plan_progress: done of total-excluding-deferred, half-up pct."""
    counts = {key: 0 for key in ("todo", "wip", "pr", "prog", "done", "deferred")}
    for group in groups:
        for task in group.get("tasks", []):
            if task.get("status") in counts:
                counts[task["status"]] += 1
    total = sum(counts[key] for key in ("todo", "wip", "pr", "prog", "done"))
    pct = int(math.floor(100 * counts["done"] / total + 0.5)) if total else 0
    return {"done": counts["done"], "total": total, "counts": counts, "pct": pct}


def _clean_task(task):
    row = {"id": task.get("id"), "title": task.get("title"), "status": task.get("status")}
    deps = task.get("deps")
    if isinstance(deps, list):
        row["deps"] = [entry for entry in deps if isinstance(entry, str)]
    if isinstance(task.get("kind"), str):
        row["kind"] = task.get("kind")
    notes = task.get("dep_notes")
    if isinstance(notes, dict):
        row["dep_notes"] = {key: value for key, value in notes.items() if isinstance(key, str) and isinstance(value, str)}
    # Gate fields pass through every plan API sanitized like other fields.
    if isinstance(task.get("signal"), str):
        row["signal"] = task["signal"]
    if isinstance(task.get("confirmed_at"), str):
        row["confirmed_at"] = task["confirmed_at"]
    if isinstance(task.get("fresh_hours"), (int, float)) and not isinstance(task.get("fresh_hours"), bool):
        row["fresh_hours"] = task["fresh_hours"]
    return row


def _clean_groups(groups):
    return [
        {"id": group.get("id"), "col": group.get("col"), "title": group.get("title"),
         "tasks": [_clean_task(task) for task in group.get("tasks", [])]}
        for group in groups
    ]


LINEAR_WS1 = {"url": "https://linear.app/example/project/pipeline-doctor", "synced_at": NOW - 240}
LINEAR_TASKS = {
    "0": {"identifier": "PD-1", "url": "https://linear.app/example/issue/PD-1"},
    "2": {"identifier": "PD-3", "url": "https://linear.app/example/issue/PD-3"},
}


def _plans():
    """Fixture plan views. WS1 resolves from its bundle (bundle wins over the
    plans map), so the plan view and the execution map agree on one source."""
    written = {}
    plan_dir = ROOT / "plans"
    plan_dir.mkdir(exist_ok=True)
    bundle_plan = json.loads((HERE / "bundles" / "WS1" / "plan.json").read_text(encoding="utf-8"))
    bundle_dag = bundle_plan.get("dag", {})
    bundle_view = {
        "cols": bundle_dag.get("cols", []),
        "groups": _clean_groups(bundle_dag.get("groups", [])),
        "edges": bundle_dag.get("edges", []),
        "phases": (bundle_plan.get("narrative") or {}).get("phases", []),
        "trains": bundle_plan.get("trains", []),
    }
    for ws, body in RAW_PLANS.items():
        raw = {"ws": ws, "dag": {"cols": body["cols"], "groups": body["groups"], "edges": body["edges"]}, "narrative": {"phases": body["phases"]}}
        # Refresh the live example plans that already exist; fixture-only
        # plans (WS9+) stay out of the live plans/ directory.
        if (plan_dir / f"{ws.lower()}.json").exists():
            (plan_dir / f"{ws.lower()}.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        if ws == "WS1":
            view = bundle_view
        else:
            view = {
                "cols": body["cols"],
                "groups": body["groups"],
                "edges": body["edges"],
                "phases": body["phases"],
                "trains": body.get("trains", []),
            }
        doc = {
            "ws": ws,
            "cols": view["cols"],
            "groups": view["groups"],
            "edges": view["edges"],
            "phases": view["phases"],
            "trains": view["trains"],
            "progress": _progress(view["groups"]),
            "_note": "Seeded example plan for the fixture preview.",
        }
        # The offline page consumes the same server-computed map layout as
        # the live page (dash/maplayout.py); the map falls back to today's
        # behaviour when these keys are absent.
        layout_view = {key: doc[key] for key in ("ws", "cols", "groups", "edges", "phases", "trains")}
        doc["layout"] = maplayout_mod.layout(layout_view)
        doc["map_warnings"] = maplayout_mod.map_warnings(layout_view)
        if ws == "WS1":
            doc["linear"] = dict(LINEAR_WS1)
            for group in doc["groups"]:
                for task in group["tasks"]:
                    if task["id"] in LINEAR_TASKS:
                        task["linear"] = dict(LINEAR_TASKS[task["id"]])
        written[ws] = doc
    return written


def _replay_doc(runs, ws, groups):
    """Offline replay fixture: same shape as GET /api/replay, mapped with
    the shared rule (no task map in fixtures, plan ids from the fixture
    plan view, so the offline page's chips and replay agree)."""
    by_id = {}
    for group in groups:
        for task in group.get("tasks", []):
            by_id[task["id"]] = task
    ids = list(by_id)
    per = {tid: [] for tid in by_id}
    for run in runs:
        if run.get("source") == "dagster":
            continue
        owner = run.get("workstream")
        if owner and owner != ws:
            continue
        tid = taskmatch.task_of(run.get("label"), ws, {}, ids)
        if tid is None or tid not in per:
            continue
        per[tid].append(replay_mod.replay_run_item(run, run["started_at"], run["ended_at"]))
    first_active = {}
    done_at = {}
    observed = set()
    for tid, items in per.items():
        starts = [item["start"] for item in items if item["start"] is not None]
        first_active[tid] = min(starts) if starts else None
        done_at[tid] = None
        if by_id[tid].get("status") == "done":
            moment = replay_mod.done_moment(items)
            if moment is not None:
                done_at[tid] = moment
                observed.add(tid)
    seen = [first_active[tid] for tid in by_id if first_active[tid] is not None]
    seen += [done_at[tid] for tid in observed if done_at[tid] is not None]
    earliest = min(seen) if seen else None
    inferred = set()
    for tid in by_id:
        if by_id[tid].get("status") == "done" and tid not in observed:
            done_at[tid] = earliest
            inferred.add(tid)
    events = []
    for tid, items in per.items():
        for item in items:
            if item["start"] is None:
                continue
            events.append({
                "t": item["start"], "kind": "active", "task": tid,
                "label": item["label"], "family": item["family"], "end": item["end"],
            })
    for tid in by_id:
        if done_at[tid] is not None:
            events.append({"t": done_at[tid], "kind": "done", "task": tid, "inferred": tid in inferred})
    events.sort(key=lambda row: (row["t"], 0 if row["kind"] == "active" else 1))
    tasks = {
        tid: {
            "status": by_id[tid].get("status"),
            "first_active": first_active[tid],
            "done_at": done_at[tid],
            "inferred": tid in inferred,
            "runs": sorted(per[tid], key=lambda item: item["start"] or 0),
        }
        for tid in by_id
    }
    return {
        "ws": ws,
        "span": {"start": events[0]["t"], "end": events[-1]["t"]} if events else None,
        "tasks": tasks,
        "events": events,
        "observed": sum(1 for tid in by_id if done_at[tid] is not None and tid not in inferred),
        "inferred": len(inferred),
        "_note": NOTE,
    }


def _burnup(runs, now, plans):
    """Fixture mirror of GET /api/project/burnup: recorded task moves vs
    plan totals plus per-day cost and token burn by workstream, on UTC
    days. Recorded moves are spread over the week so the area, the dashed
    plan line, and the pre-history gap band all render; the archived lane
    keeps an undated gap (no history) and the planned lanes stay at zero.
    Cost and tokens re-cut the fixture runs by UTC day and workstream."""
    today = int(now // 86400) * 86400
    days = [today - offset * 86400 for offset in range(7, -1, -1)]
    order = ["WS1", "WS2", "WS7", "WS9", "WS10", "WS11", "unassigned"]
    first = days[1] + 3600
    by_ws = {}
    for ws in order:
        plan = plans.get(ws)
        total = plan["progress"]["total"] if plan else None
        done = plan["progress"]["done"] if plan else 0
        if ws in ("WS1", "WS2", "WS7"):
            recorded = done
            undated = 0
            gap = None
            seen = 0
            cumulative = []
            for pos in range(len(days)):
                if 1 <= pos <= 6 and seen < recorded:
                    seen += 1
                cumulative.append(seen)
        elif ws == "WS11":
            recorded = 0
            undated = done
            n = "task" if undated == 1 else "tasks"
            first_day = datetime.datetime.fromtimestamp(first, datetime.timezone.utc)
            gap = "no history before %04d-%02d-%02d: %d done %s not dated" % (
                first_day.year, first_day.month, first_day.day, undated, n)
            cumulative = [0] * len(days)
        else:
            recorded = 0
            undated = 0
            gap = None
            cumulative = [0] * len(days)
        by_ws[ws] = {
            "cumulative": cumulative,
            "plan_total": total,
            "recorded": recorded,
            "undated": undated,
            "gap": gap,
            "first_recorded": first if ws in ("WS1", "WS2", "WS7") else None,
        }
    project = [0] * len(days)
    for ws in order:
        for pos, value in enumerate(by_ws[ws]["cumulative"]):
            project[pos] += value
    cost_ws = {ws: [0.0] * len(days) for ws in order}
    token_ws = {ws: [0] * len(days) for ws in order}
    unpriced = [0] * len(days)
    for run in runs:
        day = int(run["started_at"] // 86400) * 86400
        if day not in days:
            continue
        pos = days.index(day)
        ws = run["workstream"] or "unassigned"
        if ws not in cost_ws:
            continue
        billed = sum(run["tokens"][key] for key in TOKEN_KEYS)
        token_ws[ws][pos] += billed
        if run["cost_usd"] is not None:
            cost_ws[ws][pos] = round(cost_ws[ws][pos] + run["cost_usd"], 4)
        else:
            unpriced[pos] += billed
    return {
        "burnup": {
            "days": days,
            "day": "UTC",
            "first_recorded": first,
            "workstreams": order,
            "tasks": {
                "by_workstream": by_ws,
                "project": project,
                "plan_total": sum(by_ws[ws]["plan_total"] or 0 for ws in order),
            },
            "cost": {
                "by_workstream": cost_ws,
                "project": [round(sum(cost_ws[ws][pos] for ws in order), 4) for pos in range(len(days))],
                "unpriced_tokens": unpriced,
            },
            "tokens": {
                "by_workstream": token_ws,
                "project": [sum(token_ws[ws][pos] for ws in order) for pos in range(len(days))],
            },
        },
        "_note": NOTE,
    }


def _wsprogress(runs, now, plans):
    """Fixture mirror of GET /api/ws/progress: done-% steps with the
    recorded prog band, milestone first-cross times, and runs started
    per hour. Done moments spread over the last day so the steps, the
    milestone dots, and the hourly bars all render; a workstream with no
    done tasks keeps empty points (the card reads "No recorded task
    moves yet")."""
    docs = {}
    for ws, plan in plans.items():
        prog = plan.get("progress") or {}
        total = prog.get("total") or 0
        done = prog.get("done") or 0
        prog_now = (prog.get("counts") or {}).get("prog") or 0
        span = min(30 * 3600, max(3600, done * 1800))
        start = now - span
        dones = [start + (i + 1) * span / max(1, done) for i in range(done)]
        points = [
            {
                "t": moment,
                "done": pos + 1,
                "prog": round(prog_now * (pos + 1) / max(1, done)),
                "pct": round((pos + 1) / total * 1000) / 10 if total else 0.0,
            }
            for pos, moment in enumerate(dones)
        ]
        milestones = []
        for mark in (20, 40, 60, 80, 100):
            need = math.ceil(total * mark / 100) if total else 0
            if total and need <= len(dones):
                moment = dones[need - 1]
                milestones.append(
                    {"pct": mark, "t": moment, "done": need,
                     "elapsed": moment - start, "reached": True}
                )
            else:
                milestones.append(
                    {"pct": mark, "t": None, "done": need,
                     "elapsed": None, "reached": False}
                )
        per = {}
        for run in runs:
            if (run.get("workstream") or None) != ws:
                continue
            moment = run.get("started_at")
            if moment is None:
                continue
            hour = int(moment // 3600) * 3600
            per[hour] = per.get(hour, 0) + 1
        end = max(dones) if dones else start
        docs[ws] = {
            "wsprogress": {
                "ws": ws,
                "total": total,
                "done": done,
                "pct": prog.get("pct"),
                "counts": prog.get("counts") or {},
                "start": start,
                "end": end,
                "now": now,
                "points": points,
                "milestones": milestones,
                "runs_per_hour": [{"t": hour, "n": per[hour]} for hour in sorted(per)],
                "runs_total": sum(per.values()),
                "undated_runs": 0,
                "observed": done,
                "inferred": 0,
                "dated": done,
                "undated": 0,
                "gap": None,
                "backfilled": [],
            },
            "_note": NOTE,
        }
    return docs


def _wsprogress_all(docs, now):
    """Fixture mirror of GET /api/ws/progress?ws=all: the project total
    over the per-workstream docs (same merge rule as the server: summed
    step values at the union of event moments over the summed total)."""
    bodies = [doc["wsprogress"] for doc in docs.values()]
    bodies = [b for b in bodies if (b.get("total") or 0) > 0]
    total = sum(b["total"] for b in bodies)
    done = sum(b["done"] for b in bodies)
    counts = {}
    for body in bodies:
        for name, value in (body.get("counts") or {}).items():
            counts[name] = counts.get(name, 0) + value
    ticks = sorted({pt["t"] for body in bodies for pt in body["points"]})

    def _at(points, moment):
        value = (0, 0)
        for pt in points:
            if pt["t"] <= moment:
                value = (pt["done"], pt["prog"])
            else:
                break
        return value

    points = []
    for tick in ticks:
        part_done = sum(_at(b["points"], tick)[0] for b in bodies)
        part_prog = sum(_at(b["points"], tick)[1] for b in bodies)
        points.append(
            {
                "t": tick,
                "done": part_done,
                "prog": part_prog,
                "pct": round(part_done / total * 1000) / 10 if total else 0.0,
            }
        )
    milestones = []
    for mark in (20, 40, 60, 80, 100):
        need = math.ceil(total * mark / 100) if total else 0
        crossed = next((pt["t"] for pt in points if pt["done"] >= need), None)
        if total and crossed is not None:
            milestones.append(
                {"pct": mark, "t": crossed, "done": need,
                 "elapsed": crossed - points[0]["t"] if points else 0.0,
                 "reached": True}
            )
        else:
            milestones.append(
                {"pct": mark, "t": None, "done": need,
                 "elapsed": None, "reached": False}
            )
    per = {}
    for body in bodies:
        for row in body["runs_per_hour"]:
            per[row["t"]] = per.get(row["t"], 0) + row["n"]
    undated = sum(b.get("undated") or 0 for b in bodies)
    tasks_word = "task" if undated == 1 else "tasks"
    merged = {
        "ws": "all",
        "workstreams": sorted(b["ws"] for b in bodies),
        "total": total,
        "done": done,
        "pct": round(done / total * 1000) / 10 if total else 0.0,
        "counts": counts,
        "start": min(b["start"] for b in bodies),
        "end": max(b["end"] for b in bodies),
        "now": now,
        "points": points,
        "milestones": milestones,
        "runs_per_hour": [{"t": hour, "n": per[hour]} for hour in sorted(per)],
        "runs_total": sum(b.get("runs_total") or 0 for b in bodies),
        "undated_runs": sum(b.get("undated_runs") or 0 for b in bodies),
        "observed": sum(b.get("observed") or 0 for b in bodies),
        "inferred": sum(b.get("inferred") or 0 for b in bodies),
        "dated": sum(b.get("dated") or 0 for b in bodies),
        "undated": undated,
        "gap": ("no recorded task moves: %d done %s not dated" % (undated, tasks_word))
        if undated
        else None,
        "backfilled": [task for b in bodies for task in (b.get("backfilled") or [])],
    }
    return {"wsprogress": merged, "_note": NOTE}


def _counts(runs, now):
    tools = ("grok", "muse", "glm", "claude")
    live_by = {key: 0 for key in tools}
    seen = set()
    live_n = 0
    finished_today = 0
    finished_range = 0
    by_verdict = {}
    moment = datetime.datetime.fromtimestamp(now).astimezone()
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    range_since = now - 24 * 3600
    for run in runs:
        source = run.get("source") or ""
        if source in ("claude-main", "shell", "dagster", ""):
            continue
        tier = (run.get("tier") or "").lower()
        if source == "grok" or tier == "grok":
            tool = "grok"
        elif source == "muse" or tier == "muse":
            tool = "muse"
        elif source == "glm" or tier == "glm":
            tool = "glm"
        elif source.startswith("claude") or tier in ("opus", "sonnet", "haiku"):
            tool = "claude"
        else:
            continue
        if run["status"] == "running":
            key = (tool, run.get("label") or run["run_id"])
            if key in seen:
                continue
            seen.add(key)
            live_by[tool] += 1
            live_n += 1
            continue
        if run["status"] not in ("done", "killed", "died_early") or run.get("ended_at") is None:
            continue
        ended = float(run["ended_at"])
        if today <= ended <= now:
            finished_today += 1
        if range_since <= ended <= now:
            finished_range += 1
            verdict = run.get("verdict") or "none"
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
    return {
        "live_agents": live_n,
        "live_by_tool": live_by,
        "finished_today": finished_today,
        "finished_in_range": finished_range,
        "finished_by_verdict": by_verdict,
    }


def _graph(runs, plans):
    """Fixture mirror of GET /api/project/graph: the pure builder run over
    the fixture workstreams (BOARD names, WS_STATES, WS_DEPENDS,
    WS_MILESTONE), the fixture plan views (gate signals included), and
    live-agent counts from the fixture runs. generated_at is the fixture
    NOW so the offline preview is stable."""
    from dash import projectgraph as projectgraph_mod

    bundles = []
    for code, name, _counts in BOARD:
        row = {"id": code, "title": name, "state": WS_STATES.get(code, "active")}
        if code in WS_DEPENDS:
            row["depends_on"] = list(WS_DEPENDS[code])
        if code in WS_MILESTONE:
            row["milestone"] = WS_MILESTONE[code]
        bundles.append(row)
    live = {}
    for run in runs:
        if run["status"] == "running" and _is_agent(run) and run["workstream"]:
            live[run["workstream"]] = live.get(run["workstream"], 0) + 1
    progress = {}
    for code, doc in plans.items():
        if isinstance(doc.get("progress"), dict):
            progress[code] = doc["progress"]
    body = projectgraph_mod.build_graph(bundles, plans, live, progress, NOW)
    body["_note"] = NOTE
    return body


def _check(runs, conc):
    peak = max(sum(point[key] for key in ("grok", "muse", "glm", "claude")) for point in conc["points"])
    if not 20 <= peak <= 40:
        raise SystemExit(f"concurrency peak {peak} outside 20..40")
    if len(conc["points"]) <= 60:
        raise SystemExit("concurrency series is too short")
    ids = [run["run_id"] for run in runs]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate run id")
    return peak


def _phases(runs, now):
    """Fixture mirror of GET /api/project/phases: per-workstream billed
    tokens and cost split by phase (the _phase_split mirror over fixture
    run labels), plus cumulative-by-day cost and token series per phase
    on UTC days. Eight days like the burn-up; fixture runs land on the
    last days and earlier days read zero."""
    today = int(now // 86400) * 86400
    days = [today - offset * 86400 for offset in range(7, -1, -1)]
    names = {code: name for code, name, _counts in BOARD}
    order = [code for code, _name, _counts in BOARD]
    workstreams = []
    for ws in order:
        mine = [run for run in runs if (run["workstream"] or "unassigned") == ws]
        split = _phase_split(mine)
        by_phase = {}
        for phase in PHASE_ORDER:
            by_phase[phase] = {
                "tokens": {key: 0 for key in ("input", "output", "cache_read", "cache_write", "reasoning")},
                "billed_tokens": split[phase]["tokens"],
                "cost_usd": split[phase]["cost_usd"],
                "unpriced_tokens": 0,
                "agents": sum(1 for run in mine if _phase_of_label(run.get("label"), run.get("parent_session_id")) == phase and _is_agent(run)),
                "runs": sum(1 for run in mine if _phase_of_label(run.get("label"), run.get("parent_session_id")) == phase),
                "hours": 0.0,
            }
        per_day_cost = {phase: [0.0] * len(days) for phase in PHASE_ORDER}
        per_day_tokens = {phase: [0] * len(days) for phase in PHASE_ORDER}
        for run in mine:
            day = int(run["started_at"] // 86400) * 86400
            if day not in days:
                continue
            pos = days.index(day)
            phase = _phase_of_label(run.get("label"), run.get("parent_session_id"))
            billed = sum(run["tokens"][key] for key in TOKEN_KEYS)
            per_day_tokens[phase][pos] += billed
            if run["cost_usd"] is not None:
                per_day_cost[phase][pos] = round(per_day_cost[phase][pos] + run["cost_usd"], 4)
        series = {}
        for phase in PHASE_ORDER:
            running_cost = 0.0
            running_tokens = 0
            cum_cost = []
            cum_tokens = []
            for pos in range(len(days)):
                running_cost = round(running_cost + per_day_cost[phase][pos], 4)
                running_tokens += per_day_tokens[phase][pos]
                cum_cost.append(running_cost)
                cum_tokens.append(running_tokens)
            series[phase] = {
                "cost": per_day_cost[phase],
                "tokens": per_day_tokens[phase],
                "cumulative_cost": cum_cost,
                "cumulative_tokens": cum_tokens,
            }
        total = {key: 0 for key in ("input", "output", "cache_read", "cache_write", "reasoning")}
        total_cost = round(sum(split[phase]["cost_usd"] for phase in PHASE_ORDER), 4)
        workstreams.append({
            "ws": ws,
            "name": names.get(ws, ""),
            "by_phase": by_phase,
            "total": {"tokens": total, "cost_usd": total_cost},
            "days": days,
            "series": series,
        })
    grand = {"tokens": {key: 0 for key in ("input", "output", "cache_read", "cache_write", "reasoning")}, "cost_usd": 0.0}
    for item in workstreams:
        grand["cost_usd"] = round(grand["cost_usd"] + item["total"]["cost_usd"], 4)
    unc = sum(item["by_phase"]["unclassified"]["billed_tokens"] for item in workstreams)
    billed = sum(item["by_phase"][phase]["billed_tokens"] for item in workstreams for phase in PHASE_ORDER)
    return {
        "scope": "24h",
        "since": now - 86400,
        "until": now,
        "day": "UTC",
        "rules_hash": "fixture",
        "rules_version": 1,
        "caption": ("Split by kind of work: planning, execution, review, coordination. "
                    "Explicit plan, exec, review and coord marks first, "
                    "then review affixes, plan task matches, planning, review, doing and "
                    "coordination words, unmapped lead sessions and early exploration. "
                    "The rest reads Unclassified."),
        "workstreams": workstreams,
        "total": grand,
        "unclassified_share": round(unc / billed, 4) if billed else 0.0,
        "_note": NOTE,
    }


def main():
    runs = build_runs()
    plans = _plans()
    snap = _snapshot(runs, NOW, None)
    conc = _concurrency(runs, NOW)
    peak = _check(runs, conc)
    files = {
        "runs.json": {"runs": runs, "_note": NOTE},
        "snapshot.json": snap,
        "snapshot-WS1.json": _snapshot(runs, NOW, "WS1"),
        "snapshot-WS2.json": _snapshot(runs, NOW, "WS2"),
        "snapshot-WS7.json": _snapshot(runs, NOW, "WS7"),
        "usage-model-hour.json": _usage(runs, lambda run: run["tier"]),
        "usage-workstream-hour.json": _usage(runs, lambda run: run["workstream"] or "unassigned"),
        "concurrency.json": conc,
        "sessions.json": _sessions(runs),
        "workstreams.json": _workstreams(runs, NOW, plans),
        "workstreams-all.json": _workstreams(runs, NOW, plans, scope="all"),
        "project.json": _project(runs, NOW, plans),
        "timeline.json": _timeline(runs, NOW, plans),
        "projects.json": _projects(),
        "archive.json": _archive(NOW, plans),
        "approvals.json": _approvals(),
        "decisions.json": _decisions(),
        "project/feed.json": _feed(NOW),
        "reconcile.json": _reconcile(runs, NOW),
        "graph.json": _graph(runs, plans),
        "burnup.json": _burnup(runs, NOW, plans),
        "phases.json": _phases(runs, NOW),
        "health.json": {
            "ok": True,
            "sources": [
                {
                    "name": name,
                    "last_poll": NOW - 1,
                    "last_error": None,
                    "lag_s": 1,
                    "last_duration_s": 0.1,
                    "interval_s": interval,
                    "stalled": False,
                }
                for name, interval in (
                    ("claude", 2),
                    ("manifests", 2),
                    ("muse", 2),
                    ("grok_cli", 2),
                    ("cursor", 2),
                    ("process", 5),
                    ("hub", 60),
                )
            ],
            "_note": NOTE,
        },
    }
    for ws, plan in plans.items():
        files[f"plan-{ws}.json"] = plan
    for ws, plan in plans.items():
        files[f"replay-{ws}.json"] = _replay_doc(runs, ws, plan["groups"])
    prog_docs = _wsprogress(runs, NOW, plans)
    for ws, doc in prog_docs.items():
        files[f"ws-progress-{ws}.json"] = doc
    files["ws-progress-all.json"] = _wsprogress_all(prog_docs, NOW)
    for name, doc in files.items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(runs)} runs, peak {peak}, {len(files)} files")


if __name__ == "__main__":
    main()
