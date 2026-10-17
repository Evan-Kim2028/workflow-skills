#!/usr/bin/env python3
"""Write synthetic API fixtures. Seeded RNG. Does not read the machine."""

import datetime
import json
import math
import random
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
PLAN_WS = ("WS1", "WS2", "WS7")
BOARD = (
    ("WS0", "Stabilize", (3, 1, 0, 0, 2, 0)),
    ("WS1", "Identity", (2, 1, 1, 0, 4, 0)),
    ("WS2", "Public API", (2, 0, 1, 1, 3, 0)),
    ("WS3", "Gateway", (1, 0, 0, 0, 1, 0)),
    ("WS4", "Offers", (1, 1, 0, 0, 1, 0)),
    ("WS5", "Clients", (1, 0, 0, 0, 2, 0)),
    ("WS6", "Cleanup", (2, 0, 0, 0, 0, 1)),
    ("WS7", "Launch", (1, 0, 1, 0, 2, 0)),
)
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
                {"id": "WS2.3", "title": "Read route", "status": "prog"},
                {"id": "WS2.4", "title": "List route", "status": "todo"},
            ]},
            {"id": "clients", "col": 2, "title": "Clients", "tasks": [
                {"id": "WS2.8", "title": "Point the client at the new route", "status": "pr"},
            ]},
            {"id": "remove", "col": 3, "title": "Remove", "tasks": [
                {"id": "WS2.9", "title": "Delete the old handler", "status": "todo"},
            ]},
        ],
        "edges": [["contract", "routes"], ["routes", "clients"], ["routes", "remove"]],
        "phases": [
            {"name": "Routes", "goal": "The read route answers.", "gate": "A client can call it."},
            {"name": "Remove", "goal": "The old handler is gone.", "gate": "No caller remains."},
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
        elif run["ended_at"] is not None and now - 3600 <= run["ended_at"] <= now + 5:
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
    return {
        "now": now,
        "seq": 4,
        "counts": _counts(chosen, now),
        "running": running,
        "recent": recent,
        "totals": {"tokens": tokens, "cost_usd": round(cost, 4), "unpriced_runs": unpriced},
        "by_workstream": by_ws,
        "_note": NOTE,
    }


def _reconcile(runs, now):
    snap = _snapshot(runs, now, None)
    totals = snap["totals"]["tokens"]
    rows = [row for row in snap["by_workstream"] if row["ws"] != "unassigned"]
    unassigned = [row for row in snap["by_workstream"] if row["ws"] == "unassigned"]
    empty = {"ws": "unassigned", "live_agents": 0, "finished_in_range": 0, "finished_today": 0, "tokens": 0, "cost_usd": 0.0, "last_activity": None}
    return {
        "now": now,
        "since": now - 12 * 3600,
        "until": now,
        "all": {
            "live_agents": snap["counts"]["live_agents"],
            "finished_in_range": snap["counts"]["finished_in_range"],
            "finished_today": snap["counts"]["finished_today"],
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


def _workstreams(runs, now):
    moment = datetime.datetime.fromtimestamp(now).astimezone()
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    for code, name, counts in BOARD:
        tk = dict(zip(TK_KEYS, counts))
        mine = [run for run in runs if run["workstream"] == code]
        tokens = sum(sum(run["tokens"][key] for key in TOKEN_KEYS) for run in mine)
        cost = round(sum(run["cost_usd"] or 0 for run in mine), 4)
        live = sum(1 for run in mine if run["status"] == "running" and _is_agent(run))
        fin = sum(1 for run in mine if _is_agent(run) and run["status"] in ("done", "killed", "died_early") and run.get("ended_at") is not None and now - 24 * 3600 <= run["ended_at"] <= now)
        rows.append({
            "ws": code,
            "name": name,
            "tk": tk,
            "live_agents": live,
            "finished_in_range": fin,
            "finished_today": sum(1 for run in mine if _is_agent(run) and run["status"] in ("done", "killed", "died_early") and run.get("ended_at") is not None and today <= run["ended_at"] <= now),
            "tokens": tokens,
            "cost_usd": cost,
            "last_activity": max([run["ended_at"] if run["ended_at"] is not None else run["started_at"] for run in mine] or [0]) or None,
            "plan": code in PLAN_WS,
        })
    return {"workstreams": rows, "_note": NOTE}


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
    return row


def _clean_groups(groups):
    return [
        {"id": group.get("id"), "col": group.get("col"), "title": group.get("title"),
         "tasks": [_clean_task(task) for task in group.get("tasks", [])]}
        for group in groups
    ]


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
        written[ws] = {
            "ws": ws,
            "cols": view["cols"],
            "groups": view["groups"],
            "edges": view["edges"],
            "phases": view["phases"],
            "trains": view["trains"],
            "progress": _progress(view["groups"]),
            "_note": "Seeded example plan for the fixture preview.",
        }
    return written


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
        "workstreams.json": _workstreams(runs, NOW),
        "reconcile.json": _reconcile(runs, NOW),
        "health.json": {
            "ok": True,
            "sources": [
                {"name": name, "last_poll": NOW - 1, "last_error": None, "lag_s": 1}
                for name in ("claude", "manifests", "muse", "grok_cli", "cursor", "process", "hub")
            ],
            "_note": NOTE,
        },
    }
    for ws, plan in plans.items():
        files[f"plan-{ws}.json"] = plan
    for name, doc in files.items():
        (HERE / name).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(runs)} runs, peak {peak}, {len(files)} files")


if __name__ == "__main__":
    main()
