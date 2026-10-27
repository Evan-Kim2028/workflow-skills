#!/usr/bin/env python3
"""Claude Code lead guard: keep a workstream lead wakeable and refilling.

Two rules, dispatched on ``hook_event_name``. Both fail open: a non-lead
session, a missing transcript, bad JSON, or any lookup failure exits 0
silently. Own errors append one line to the state-dir log and exit 0.

Rule 1 (PreToolUse, ``AskUserQuestion``): a lead with at least one
pending background task gets exit 2, so the question is asked in plain
text instead of freezing the session (a question tool-call blocks the
whole session, and worker completions cannot wake it).

Rule 2 (Stop): a lead with nothing pending, no ready task, and no live
unwatched worker may end its turn (exit 0). A lead that still has
something to do gets exit 2 naming up to 8 ready task ids with titles
and up to 8 live labels, so it launches, watches, or parks them instead
of sitting idle with nothing that will wake it.

A session is a lead when, within the last 12 h of its transcript, it
launched at least one worker: a Bash command the label guard treats as
a worker launch (:mod:`label_guard_hook` launcher detection, imported,
not copied) or an ``Agent``/``Task`` tool use. Labels like
``ws<N>-<mark>-<slug>`` name workstream ``WS<N>``; ``hub-*`` labels name
none, and hub-only leads skip the ready-task lookup. Non-lead sessions
always pass: this hook is installed globally and must not change
ordinary sessions.

Pending background tasks are started ids (explicit ``run_in_background``
launches, auto-backgrounded commands keyed on the result text, and
``Agent``/``Task`` launches whose result mentions running in the
background, an async launch, or an agent id) minus finished ids (both
ids from any ``<task-notification>``, ``Successfully stopped task``),
ignoring starts older than ``AGENT_DASH_LEAD_GUARD_MAX_AGE_S``
(default 8 h), plus unexpired ``ScheduleWakeup`` wakers.

Transcripts reach 20+ MB, so each line is prefiltered by substring
before ``json.loads``; a full Stop check stays well under 1.5 s.

Installs via ``sh scripts/install_hooks.sh --claude-lead-guard``
into ``~/.claude/settings.json`` (PreToolUse matcher
``AskUserQuestion`` plus a Stop hook, each ``"timeout": 10``).
``AGENT_DASH_LEAD_GUARD=off`` disables both rules.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

try:
    import label_guard_hook as _label_guard
except Exception:  # fail open: Bash launches just stop counting as leads
    _label_guard = None


def _log(text: str) -> None:
    """One best-effort line to the state-dir log. Never raises."""
    try:
        state = os.environ.get("AGENT_DASH_STATE_DIR") or str(
            Path.home() / ".local" / "share" / "agent-dash"
        )
        with open(os.path.join(state, "lead-guard.log"), "a", encoding="utf-8") as fh:
            fh.write(text[:500] + "\n")
    except Exception:
        pass


if _label_guard is None:
    _log("label_guard_hook import failed: Bash launches not counted as leads")

#: Guard switch and age/window knobs.
MAX_AGE_S = 8 * 3600
LEAD_WINDOW_S = 12 * 3600

#: Tool-use names that launch a subagent directly (no Bash needed).
_AGENT_TOOLS = frozenset({"Agent", "Task"})

#: A finished Agent/Task result never looks like one of these; a result
#: that mentions any of them is an async launch still in flight.
_ASYNC_HINTS = (
    "background",
    "async",
    "agent id",
    "agent_id",
    "agentid",
    "launched",
    "task-id",
    "task id",
)

#: Lines without one of these markers cannot change the verdict, so they
#: skip ``json.loads`` entirely (transcripts reach 20+ MB).
_INTERESTING = (
    "tool_use",
    "tool_result",
    "task-notification",
    "task-id",
    "ScheduleWakeup",
    "stopped task",
    "background",
)

_BG_EXPLICIT_RE = re.compile(
    r"Command running in background with ID:\s*([A-Za-z0-9_-]+)"
)
_BG_AUTO_RE = re.compile(r"moved to the background \(ID:\s*([A-Za-z0-9_-]+)\)")
_STOPPED_RE = re.compile(r"Successfully stopped task:\s*([A-Za-z0-9_-]+)")
_TASK_ID_RE = re.compile(r"<task-id>\s*([A-Za-z0-9_-]+)\s*</task-id>")
_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>\s*([A-Za-z0-9_-]+)\s*</tool-use-id>")
_WS_LABEL_RE = re.compile(r"^ws(\d+)-", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _parse_ts(raw) -> float | None:
    """Epoch seconds for an ISO ``...Z`` record timestamp, else None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _strings(record) -> list[str]:
    """Every string in a parsed JSON record, however nested."""
    out: list[str] = []
    stack = [record]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _result_text(content) -> str:
    """Plain text of a tool_result ``content`` (str or text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(json.dumps(item)[:2000])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class Facts:
    """Everything one transcript pass yields for both rules."""

    def __init__(self) -> None:
        self.launches: list[tuple[float | None, str | None]] = []
        self.labels: list[str] = []
        self.starts: dict[str, float | None] = {}
        self.started_by: dict[str, str] = {}
        self.bg_tool_uses: dict[str, float | None] = {}
        self.agent_starts: dict[str, float | None] = {}
        self.agent_results: dict[str, str] = {}
        self.finished: set[str] = set()
        self.wakeups: list[float] = []


def _record_ts(record: dict) -> float | None:
    return _parse_ts(record.get("timestamp"))


def _handle_assistant(record: dict, facts: Facts, ts: float | None) -> None:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        name = item.get("name")
        input_ = item.get("input") if isinstance(item.get("input"), dict) else {}
        tool_use_id = item.get("id")
        if name == "Bash":
            command = input_.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            try:
                labels = _label_guard.extract_labels(command) if _label_guard else []
            except Exception:
                labels = []
            if labels:
                facts.launches.append((ts, labels[0] if labels else None))
                facts.labels.extend(label for label in labels if label)
            if input_.get("run_in_background") is True and isinstance(tool_use_id, str):
                facts.bg_tool_uses[tool_use_id] = ts
        elif name in _AGENT_TOOLS and isinstance(tool_use_id, str):
            facts.launches.append((ts, None))
            facts.agent_starts[tool_use_id] = ts
        elif name == "ScheduleWakeup":
            try:
                delay = float(input_.get("delaySeconds", 0))
            except (TypeError, ValueError):
                continue
            if delay > 0 and ts is not None:
                facts.wakeups.append(ts + delay)


def _handle_user(record: dict, facts: Facts, ts: float | None) -> None:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        text = _result_text(item.get("content"))
        tool_use_id = item.get("tool_use_id")
        for found in _BG_EXPLICIT_RE.findall(text) + _BG_AUTO_RE.findall(text):
            facts.starts.setdefault(found, ts)
            if isinstance(tool_use_id, str):
                facts.started_by[tool_use_id] = found
        if isinstance(tool_use_id, str):
            if tool_use_id in facts.bg_tool_uses:
                # A background launch reports its task id here; without
                # one the command already finished, so drop the key.
                del facts.bg_tool_uses[tool_use_id]
            facts.agent_results[tool_use_id] = text


def scan_transcript(path: str) -> Facts | None:
    """One streaming pass over a transcript file. None when unreadable."""
    facts = Facts()
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return None
    try:
        for line in handle:
            if not any(marker in line for marker in _INTERESTING):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            ts = _record_ts(record)
            for text in _strings(record):
                if "<task-id>" in text or "<tool-use-id>" in text:
                    facts.finished.update(_TASK_ID_RE.findall(text))
                    facts.finished.update(_TOOL_USE_ID_RE.findall(text))
                if "stopped task" in text:
                    facts.finished.update(_STOPPED_RE.findall(text))
            kind = record.get("type")
            if kind == "assistant":
                _handle_assistant(record, facts, ts)
            elif kind == "user":
                _handle_user(record, facts, ts)
    except OSError:
        return None
    finally:
        try:
            handle.close()
        except Exception:
            pass
    # A notification may name only the tool-use id; close the task it
    # started too.
    for tool_use_id, task_id in facts.started_by.items():
        if tool_use_id in facts.finished:
            facts.finished.add(task_id)
    return facts


def workstreams(labels: list[str]) -> list[str]:
    """Sorted ``WS<N>`` ids named by session labels (``hub-*`` names none)."""
    found: set[str] = set()
    for label in labels:
        match = _WS_LABEL_RE.match(label or "")
        if match:
            found.add("WS%d" % int(match.group(1)))
    return sorted(found, key=lambda ws: int(ws[2:]))


def is_lead(facts: Facts, now: float, window_s: float = LEAD_WINDOW_S) -> bool:
    """True when the session launched a worker inside the lead window."""
    for ts, _label in facts.launches:
        age_ts = now if ts is None else ts
        if now - age_ts <= window_s:
            return True
    return False


def pending_tasks(
    facts: Facts, now: float, max_age_s: int = MAX_AGE_S
) -> list[str]:
    """Started ids still in flight (old starts ignored). Sorted."""
    out = []
    for task_id, ts in facts.starts.items():
        if task_id in facts.finished:
            continue
        age_ts = now if ts is None else ts
        if now - age_ts > max_age_s:
            continue
        out.append(task_id)
    for tool_use_id, ts in facts.bg_tool_uses.items():
        if tool_use_id in facts.finished:
            continue
        age_ts = now if ts is None else ts
        if now - age_ts > max_age_s:
            continue
        out.append(tool_use_id)
    for tool_use_id, ts in facts.agent_starts.items():
        if tool_use_id in facts.finished:
            continue
        result = facts.agent_results.get(tool_use_id)
        if result is not None and not any(
            hint in result.lower() for hint in _ASYNC_HINTS
        ):
            continue  # the finished report already arrived
        age_ts = now if ts is None else ts
        if now - age_ts > max_age_s:
            continue
        out.append(tool_use_id)
    return sorted(out)


def live_wakeups(facts: Facts, now: float) -> bool:
    """True when an unexpired ScheduleWakeup will still wake the lead."""
    return any(expiry > now for expiry in facts.wakeups)


def get_ready_tasks(ws: str) -> list[dict]:
    """Ready rows for ``ws`` (``agent_dash.py ws next <WS> --json``).

    Tests replace this module-level function; the production path is one
    subprocess with a 6 s timeout. The in-process ``print_next`` behind
    ``ws next`` prints text instead of returning rows, so the subprocess
    reading its ``--json`` shape is the clean call. Any failure (missing
    binary, timeout, bad JSON) reads as no ready tasks: fail open.
    """
    agent = os.environ.get("AGENT_DASH_BIN") or str(ROOT / "agent_dash.py")
    try:
        proc = subprocess.run(
            [sys.executable, agent, "ws", "next", ws, "--json"],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    ready = data.get("ready", [])
    return [row for row in ready if isinstance(row, dict)]


def list_processes() -> str:
    """One ``ps`` snapshot. Tests replace this module-level function."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""
    except Exception:
        return ""


def live_labels(labels: list[str], snapshot: str) -> list[str]:
    """Session labels with a live process line (plain substrings only)."""
    found = []
    for label in labels:
        if not label:
            continue
        for line in snapshot.splitlines():
            if label in line:
                found.append(label)
                break
    return sorted(set(found))


ASK_REFUSAL = (
    "Workers are running and AskUserQuestion would freeze this session so "
    "their completions cannot wake it. Ask the question in plain text in "
    "your reply instead, park only the task that needs the answer as "
    "BLOCKED with the question, keep every other lane running, and end "
    "the turn with watchers armed."
)


def check_ask(payload: dict) -> tuple[int, str]:
    """Rule 1: refuse AskUserQuestion for a lead with pending tasks."""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return 0, ""
    facts = scan_transcript(transcript)
    if facts is None:
        return 0, ""
    now = time.time()
    max_age_s = _env_int("AGENT_DASH_LEAD_GUARD_MAX_AGE_S", MAX_AGE_S)
    window_s = _env_int("AGENT_DASH_LEAD_GUARD_LEAD_WINDOW_S", LEAD_WINDOW_S)
    if not is_lead(facts, now, window_s):
        return 0, ""
    if not pending_tasks(facts, now, max_age_s):
        return 0, ""
    return 2, ASK_REFUSAL + "\n"


def check_stop(payload: dict) -> tuple[int, str]:
    """Rule 2: refuse Stop for a lead that still has something to do."""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return 0, ""
    facts = scan_transcript(transcript)
    if facts is None:
        return 0, ""
    now = time.time()
    max_age_s = _env_int("AGENT_DASH_LEAD_GUARD_MAX_AGE_S", MAX_AGE_S)
    window_s = _env_int("AGENT_DASH_LEAD_GUARD_LEAD_WINDOW_S", LEAD_WINDOW_S)
    if not is_lead(facts, now, window_s):
        return 0, ""
    if pending_tasks(facts, now, max_age_s) or live_wakeups(facts, now):
        return 0, ""
    ready: list[tuple[str, dict]] = []
    for ws in workstreams(facts.labels):
        try:
            rows = get_ready_tasks(ws)
        except Exception:
            rows = []
        for row in rows:
            if not row.get("live_run"):
                ready.append((ws, row))
    try:
        snapshot = list_processes()
    except Exception:
        snapshot = ""
    alive = live_labels(facts.labels, snapshot or "")
    if not ready and not alive:
        return 0, ""
    bits = []
    for ws, row in ready[:8]:
        bits.append("%s %s" % (row.get("id", "?"), (row.get("title") or "")[:80]))
    err = "Lead guard: this session launched workers and still has work "
    if ready:
        err += "with nothing running to do it. Ready: %s. " % "; ".join(bits)
    else:
        err += "with nothing left to wake it. "
    if alive:
        err += "Live workers with no watcher: %s. " % ", ".join(alive[:8])
    err += (
        "Before ending the turn, launch the ready tasks up to the tier "
        "caps, arm one background watcher per live worker, or mark each "
        "ready task you cannot start as BLOCKED with the reason; a "
        "question for the owner goes in plain text; if the owner told you "
        "to stop, say so and end the turn again "
        "(this check fires once per stop)."
    )
    return 2, err + "\n"


def main() -> int:
    try:
        raw = sys.stdin.read(2 * 1024 * 1024)
    except Exception:
        return 0
    if not raw or not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    try:
        if not isinstance(payload, dict):
            return 0
        if str(os.environ.get("AGENT_DASH_LEAD_GUARD", "")).lower() in (
            "off",
            "0",
            "false",
            "no",
        ):
            return 0
        event = payload.get("hook_event_name")
        if event == "PreToolUse":
            tool = payload.get(
                "tool_name", payload.get("toolName", payload.get("tool", ""))
            )
            if tool != "AskUserQuestion":
                return 0
            code, err = check_ask(payload)
        elif event == "Stop":
            if payload.get("stop_hook_active"):
                return 0
            code, err = check_stop(payload)
        else:
            return 0
        if err:
            sys.stderr.write(err)
        return code
    except Exception as exc:  # fail open on anything unexpected
        _log("hook failed: %s" % exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
