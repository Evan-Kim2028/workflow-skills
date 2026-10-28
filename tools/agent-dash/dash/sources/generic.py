"""Generic run/usage drop file for any agent harness.

One JSON object per line in ``generic.jsonl`` under the state directory.
No prompts, no argv, no secrets. Labels only.

Run line (status shown in the run list and on the plan map)::

    {"type": "run", "label": "ws4-WS4.1-notes-index", "status": "running",
     "model": "my-model", "tier": "generic", "harness": "generic",
     "session_id": "abc", "started_at": 1700000000.0}

``harness`` is optional and names the CLI that ran (``grok`` for Grok Build,
``pi``, ``commandcode``); a recorded harness wins over the source guess.

Usage line (tokens and cost for the run above)::

    {"type": "usage", "session_id": "abc", "run_id": "generic:abc:ws4-WS4.1-notes-index",
     "tool": "generic", "message_id": "turn-1", "model": "my-model",
     "input": 100, "output": 50}

Workstream comes from the label through the map file, the same as every
other source. The plan map pins the run when the label holds the task id.
``tier`` only decides the live-agent tool bucket: the tile counts the
``grok``, ``muse``, ``glm`` and ``claude`` buckets, so a generic run shows
in the running table and on its task chip either way.
"""

from __future__ import annotations

import json
import logging
import os

NAME = "generic"
INTERVAL_S = 2

FILE_NAME = "generic.jsonl"
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024
LABEL_MAX = 200
MODEL_MAX = 200

STATUSES = ("running", "done", "killed", "died_early", "unknown")

#: Launcher spellings that mean the run finished without a report. They
#: must not fall through to the default ``running``: a finished attempt
#: whose final line says ``failed`` would otherwise read live forever.
FAILED_STATUSES = ("failed", "failure")

_log = logging.getLogger("agent_dash.generic")


def default_run_id(label: str, session_id: str | None = None, run_id: str | None = None) -> str:
    """Stable run id shared by the source and the report helper."""
    if run_id:
        return run_id
    if session_id:
        return f"generic:{session_id}:{label}"
    return f"generic:{label}"


def tier_of(model: str | None) -> str:
    """Map a model name onto a dashboard tier, else generic."""
    if not model or not isinstance(model, str):
        return "generic"
    text = model.lower()
    for name in ("beam", "opus", "sonnet", "haiku", "muse", "glm", "grok"):
        if name in text:
            return name
    return "generic"


def poll(store, cfg, now: float) -> None:
    try:
        _poll(store, cfg or {}, float(now))
    except Exception as exc:
        _log.warning("generic poll: %s", type(exc).__name__)
    # After (not inside) the read: _poll returns early on a quiet
    # file, and a restart with quiet files is exactly when a
    # still-NULL run needs its model filled from usage.
    _repair(store, now)


def _poll(store, cfg: dict, now: float) -> None:
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return
    per_file, budget = _budgets(cfg)
    try:
        st = os.stat(path)
    except OSError:
        return
    key = os.path.abspath(path)
    start = 0
    rec = store.get_offset(key)
    if rec and rec[0] == int(st.st_ino):
        try:
            start = int(rec[1])
        except (TypeError, ValueError):
            start = 0
    if start < 0 or start > st.st_size:
        start = 0
    if start == st.st_size:
        return
    want = min(st.st_size - start, per_file, budget)
    if want <= 0:
        return
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(want)
    except OSError:
        return
    nl = data.rfind(b"\n")
    if nl < 0:
        return
    chunk = data[: nl + 1]
    consumed = nl + 1
    offset = 0
    while offset < len(chunk):
        end = chunk.find(b"\n", offset)
        raw = chunk[offset:] if end < 0 else chunk[offset:end]
        line_at = start + offset
        offset = len(chunk) if end < 0 else end + 1
        if not raw.strip():
            continue
        _apply_line(store, raw, line_at, now)
    try:
        store.set_offset(key, int(st.st_ino), start + consumed, int(st.st_size), float(st.st_mtime))
    except Exception:
        _log.warning("generic offset")


def _repair(store, now: float) -> None:
    """Fill NULL run models from usage. Unknown stores keep the old behavior."""
    backfill = getattr(store, "backfill_tool_models", None)
    if not callable(backfill):
        return
    try:
        backfill(NAME, now)
    except Exception:
        _log.warning("generic backfill")


def _apply_line(store, raw: bytes, line_at: int, now: float) -> None:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return
    if not isinstance(obj, dict):
        return
    kind = obj.get("type")
    if kind == "run":
        row = _run_row(obj, now)
        if row is not None:
            try:
                store.upsert_run(row)
            except Exception:
                _log.warning("generic run")
    elif kind == "usage":
        row = _usage_row(obj, line_at, now)
        if row is not None:
            try:
                store.add_usage(row)
            except Exception:
                _log.warning("generic usage")


def _run_row(obj: dict, now: float) -> dict | None:
    label = _label(obj.get("label"))
    if not label:
        return None
    session_id = _text(obj.get("session_id"), 200)
    run_id = _text(obj.get("run_id"), 300) or default_run_id(label, session_id)
    model = _text(obj.get("model"), MODEL_MAX)
    tier = _text(obj.get("tier"), 64)
    if tier is None and model is not None:
        tier = tier_of(model)
    status = obj.get("status")
    if isinstance(status, str) and status.strip().lower() in FAILED_STATUSES:
        status = "died_early"
    elif status not in STATUSES:
        status = "running"
    verdict = obj.get("verdict")
    verdict = verdict.strip() if isinstance(verdict, str) and verdict.strip() else None
    row: dict = {
        "run_id": run_id,
        "source": "generic",
        "label": label,
        "status": status,
        "clock_source": "manifest",
    }
    pid = _int(obj.get("pid"), None)
    if pid is not None and pid > 0:
        row["pid"] = pid
    if session_id is not None:
        row["session_id"] = session_id
    parent = _text(obj.get("parent_session_id"), 200)
    if parent is not None:
        row["parent_session_id"] = parent
    if model is not None:
        row["model"] = model
    if tier is not None:
        row["tier"] = tier
    harness = _text(obj.get("harness"), 64)
    if harness is not None:
        row["harness"] = harness
    if verdict is not None:
        row["verdict"] = verdict
    started = _num(obj.get("started_at"), None)
    if started is None and status == "running":
        started = now
    if started is not None:
        row["started_at"] = started
    ended = _num(obj.get("ended_at"), None)
    if ended is None and status in ("done", "killed", "died_early"):
        ended = now
    if ended is not None:
        row["ended_at"] = ended
    if isinstance(obj.get("out_bytes"), int) and obj["out_bytes"] >= 0:
        row["out_bytes"] = obj["out_bytes"]
    if isinstance(obj.get("err_bytes"), int) and obj["err_bytes"] >= 0:
        row["err_bytes"] = obj["err_bytes"]
    return row


def _usage_row(obj: dict, line_at: int, now: float) -> dict | None:
    session_id = _text(obj.get("session_id"), 200)
    if not session_id:
        return None
    tool = _text(obj.get("tool"), 64) or "generic"
    run_id = _text(obj.get("run_id"), 300)
    message_id = _text(obj.get("message_id"), 300) or "%s:%d" % (run_id or session_id, line_at)
    return {
        "ts": _num(obj.get("ts"), now),
        "session_id": session_id,
        "run_id": run_id,
        "model": _text(obj.get("model"), MODEL_MAX),
        "tool": tool,
        "message_id": message_id,
        "input": _int(obj.get("input"), 0),
        "output": _int(obj.get("output"), 0),
        "cache_read": _int(obj.get("cache_read"), 0),
        "cache_write": _int(obj.get("cache_write"), 0),
        "reasoning": _int(obj.get("reasoning"), 0),
    }


def _path(cfg: dict) -> str | None:
    explicit = cfg.get("generic_log")
    if explicit:
        return os.path.expanduser(str(explicit))
    state = cfg.get("state_dir")
    if state:
        return os.path.join(os.path.expanduser(str(state)), FILE_NAME)
    return None


def _budgets(cfg: dict) -> tuple[int, int]:
    per_file, per_tick = PER_FILE_DEFAULT, PER_TICK_DEFAULT
    for key in ("read_budgets", "read_budget"):
        budgets = cfg.get(key)
        if isinstance(budgets, dict):
            try:
                per_file = int(budgets.get("per_file", per_file))
            except (TypeError, ValueError):
                pass
            try:
                per_tick = int(budgets.get("per_tick", per_tick))
            except (TypeError, ValueError):
                pass
    for key, current in (("read_budget_per_file", per_file), ("read_budget_per_tick", per_tick)):
        try:
            value = cfg.get(key)
            if value is not None:
                current = int(value)
        except (TypeError, ValueError):
            pass
        if key == "read_budget_per_file":
            per_file = current
        else:
            per_tick = current
    return per_file, per_tick


def _label(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > LABEL_MAX or "/" in text or "\\" in text or text.startswith("."):
        return None
    return text


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or "\n" in text or "\r" in text:
        return None
    return text


def _num(value, default):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    return default
