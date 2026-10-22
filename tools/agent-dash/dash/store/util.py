"""Shared constants and pure helpers for the store package.

No dash imports: every other concern may import from here. Pure means no connection, no lock, no clock reads.
"""

from __future__ import annotations

import re

from datetime import datetime


TOOL_KEYS = ("grok", "muse", "glm", "claude", "deepseek", "codex", "other")


FINISHED = ("done", "killed", "died_early")


RANGE_S = 24 * 3600


_ESTIMATE_TOOL = "grok-context"


MERGE_WINDOW_S = 900.0


UNASSIGNED = "unassigned"


SAMPLE_FRESH_S = 20


RECENT_S = 24 * 3600


SAMPLE_KEEP_S = 48 * 3600


TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")


BILLED_FIELDS = ("input", "output", "cache_read", "cache_write")


PRUNE_BATCH = 2000


RUN_COLS = (
    "run_id",
    "source",
    "tier",
    "model",
    "label",
    "session_id",
    "parent_session_id",
    "workstream",
    "ws_confidence",
    "status",
    "verdict",
    "started_at",
    "ended_at",
    "clock_source",
    "out_bytes",
    "out_growing",
    "err_bytes",
    "pid",
    "out_path",
    "slice_of",
)


RUN_INPUT = (
    "source",
    "tier",
    "model",
    "label",
    "session_id",
    "parent_session_id",
    "status",
    "verdict",
    "started_at",
    "ended_at",
    "clock_source",
    "out_bytes",
    "err_bytes",
    "pid",
    "out_path",
    "slice_of",
)


SESSION_COLS = (
    "session_id",
    "tool",
    "cwd",
    "started_at",
    "status",
    "parent_session_id",
    "label",
    "workstream",
    "ws_confidence",
    "ws_hint",
)


# view_seq advances only when a write changes a value the snapshot or
# usage buckets expose. Per-poll telemetry (byte counters, growth
# flags, pids) rides the data sequence instead: it still invalidates
# content caches and retags ETags, but it does not wake an idle page.
RUN_VIEW_IGNORE = ("out_bytes", "out_growing", "err_bytes", "pid")


RUN_VIEW_COLS = tuple(col for col in RUN_COLS if col not in RUN_VIEW_IGNORE)


WS_STATUSES = ("backlog", "todo", "doing", "review", "done", "cancelled")


_WF = re.compile(r"(wf_[A-Za-z0-9-]+)")


_SEV_ORDER = {"critical": 0, "warn": 1, "info": 2}


def _manifest_family(run_id) -> str | None:
    """Source family of a launcher-manifest row id, or None.

    The launcher writes one manifest row per worker it starts while the
    worker's own log makes a second row for the same run. Cursor rows
    carry ``:cursor/``; muse/glm rows without a session uuid carry
    ``@<epoch>`` on the last segment (``muse:None:<label>@<epoch>``).
    Worker-row labels never end in ``@<digits>``, so the tail names
    manifest rows exactly.
    """
    if not isinstance(run_id, str):
        return None
    if ":cursor/" in run_id:
        return "grok"
    head, sep, _rest = run_id.partition(":")
    if sep and head in ("muse", "glm"):
        tail = run_id.rsplit(":", 1)[-1]
        if tail and re.fullmatch(r".*@\d+", tail):
            return head
    return None


def _hour(ts: float) -> int:
    return int(ts // 3600) * 3600


def _opt_int(row: dict, key: str):
    if key not in row or row[key] is None:
        return None
    return int(row[key])


def _same(existing: dict, merged: dict, cols) -> bool:
    for col in cols:
        if existing.get(col) != merged.get(col):
            return False
    return True


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _zeros() -> dict:
    return {key: 0 for key in TOKEN_FIELDS}


def _money(value):
    if value is None:
        return None
    return round(float(value), 6)


def _elapsed(run: dict, now: float):
    start = run.get("started_at")
    if start is None:
        return None
    if run.get("status") == "running" or run.get("ended_at") is None:
        end = now
    else:
        end = run["ended_at"]
    return max(0.0, float(end) - float(start))


def _in_ws(run: dict, ws: str | None) -> bool:
    if not ws or ws == "all":
        return True
    if ws == UNASSIGNED:
        return not run.get("workstream")
    return run.get("workstream") == ws


def _billed_total(tokens: dict) -> int:
    return sum((tokens or {}).get(key) or 0 for key in BILLED_FIELDS)


def _tool_key(run: dict) -> str | None:
    source = run.get("source") or ""
    if source == "dagster":
        return None
    tier = (run.get("tier") or "").lower()
    if source == "grok" or tier == "grok":
        return "grok"
    if source == "muse" or tier == "muse":
        return "muse"
    if source == "glm" or tier == "glm":
        return _grok_cli_key(run)
    if source in ("codex", "codex-exec") or tier == "codex":
        return "codex"
    if source == "commandcode" or tier == "commandcode":
        return "other"
    if source.startswith("claude") or tier in ("opus", "sonnet", "haiku"):
        return "claude"
    return None


def _grok_cli_key(run: dict) -> str:
    """Tool lane for a Grok Build run: the grok provider, not the CLI name.

    The family comes from the one derivation in ``dash.models`` (lazy import:
    this module takes no dash imports at load): every Grok Build run (the
    ``grok-build`` launcher kind behind source ``glm``) counts as ``grok``,
    whatever model it runs. Other sources never reach this helper.
    """
    from dash.models import model_key

    hint = " ".join(str(run.get(name) or "") for name in ("tier", "source"))
    fam, _key = model_key(run.get("model"), hint)
    if fam == "grok":
        return "grok"
    if fam == "deepseek":
        return "deepseek"
    if fam == "other":
        return "other"
    return "glm"


def is_prod_job_run(run: dict) -> bool:
    """True for a production (Dagster) job row. Production jobs are never in
    an agent list (snapshot running/recent, ``/api/runs`` by default, the
    timeline); they reach the page only as the ``prod_jobs`` and
    ``prod_jobs_finished`` counts."""
    return (run.get("source") or "") == "dagster"


def _is_counted_agent(run: dict) -> bool:
    """Interactive Claude and Codex lead sessions and shell launch rows are not agents."""
    source = run.get("source") or ""
    if source in ("claude-main", "codex", "shell", "dagster", ""):
        return False
    return _tool_key(run) in TOOL_KEYS


def marker_billed_tokens(run: dict) -> int:
    """Billed tokens carried on a run view's ``tokens`` fold, or 0.

    Run views always carry a zero-filled ``tokens`` dict; raw store rows
    carry none, which reads as 0 here (callers with raw rows must confirm
    the no-usage leg against the usage table themselves).
    """
    tokens = run.get("tokens")
    if not isinstance(tokens, dict):
        return 0
    return sum(int(tokens.get(key) or 0) for key in BILLED_FIELDS)


def is_marker_run(run: dict) -> bool:
    """True for a placeholder run row that must never read as a model bucket.

    A marker is a worker-source row (muse, glm, grok) with no model id, no
    verdict, and zero billed tokens: a manifest row for a launch that never
    produced output, a live process seen before its session/manifest/usage
    match, or an unlabeled session row with no completions. It is real
    activity while live, but it names no model, so counting it would invent
    a ``<family>-unknown`` ("model not recorded") bucket in the usage and
    live model legends. A worker run with tokens, a model, or a verdict is
    never a marker -- including a real zero-token completed run, which
    always carries a verdict, and an unknown-model run with tokens, which
    honestly reads as "model not recorded". Two quiet rows are never
    markers either: a row the liveness layer flagged ``stalled`` is a
    proven live-but-stuck process with its own pill and count, and a
    ``killed``/``died_early`` row is terminal death evidence the outcome
    layer reads as ``died``. Both are real agent outcomes, never
    placeholders.
    """
    if (run.get("source") or "") not in WORKER_SOURCES:
        return False
    if run.get("stalled"):
        return False
    if run.get("status") in ("killed", "died_early"):
        return False
    if run.get("model"):
        return False
    if run.get("verdict"):
        return False
    return marker_billed_tokens(run) <= 0


WORKER_SOURCES = frozenset({"muse", "glm", "grok"})


WRAPPER_SOURCES = frozenset({"claude-workflow"})


def _local_midnight(now: float) -> float:
    moment = datetime.fromtimestamp(float(now)).astimezone()
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _drop_launch_echoes(rows: list) -> list:
    """Merge launch-manifest rows into the run they record.

    A launcher writes a manifest row for every worker it starts, and the
    worker's own transcript (Cursor chat, muse session) makes a second row for
    the same run. A manifest row whose label matches a non-manifest run from
    the same source that started within ten minutes is that same run (a
    ws-wave Haiku wrapper shares its muse run's label but is not it): it is dropped, and its verdict
    and output size fill any gap in the twin. A manifest row with no twin stays.
    """
    by_label: dict = {}
    for row in rows:
        by_label.setdefault(row.get("label"), []).append(row)
    out = []
    for row in rows:
        start = row.get("started_at")
        twin = None
        if row.get("clock_source") == "manifest" and start is not None and row.get("label"):
            for other in by_label.get(row.get("label"), []):
                if (
                    other is not row
                    and other.get("clock_source") != "manifest"
                    and other.get("source") == row.get("source")
                    and other.get("started_at") is not None
                    and abs(other["started_at"] - start) <= 600
                ):
                    twin = other
                    break
        if twin is None:
            out.append(row)
            continue
        for key in ("verdict", "out_bytes", "workstream"):
            if not twin.get(key) and row.get(key):
                twin[key] = row[key]
    return _one_run_per_process(out)


def _one_run_per_process(rows: list) -> list:
    """Keep one running row per (label, pid).

    Two chats started in the same second can both be matched to one label, so
    one live process shows twice. The row with a model id and the most output
    is the real one.
    """
    best: dict = {}
    for row in rows:
        if row.get("status") != "running" or not row.get("pid"):
            continue
        key = (row.get("label"), row.get("pid"))
        rank = (bool(row.get("model")), row.get("out_bytes") or 0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, id(row))
    keep = {value[1] for value in best.values()}
    return [
        row
        for row in rows
        if row.get("status") != "running" or not row.get("pid") or id(row) in keep
    ]


def _active(run: dict, t: float, now: float) -> bool:
    start = run.get("started_at")
    if start is None or start > t:
        return False
    if run.get("status") == "running":
        return t <= now
    end = run.get("ended_at")
    if end is None:
        return False
    return t < end


def _sum_costs(costs: list, any_tokens: bool):
    if any(item is not None and item != 0 for item in costs) or any(item == 0 for item in costs):
        priced = [item for item in costs if item is not None]
        if not priced:
            return None if any_tokens else 0.0
        return sum(priced)
    if any_tokens:
        return None
    return 0.0


def _ws_sort(ws: str):
    import re

    match = re.fullmatch(r"WS(\d+)", ws or "")
    if match:
        return (0, int(match.group(1)), "")
    return (1, 0, ws or "")


def _collapse_workflows(runs: list, now: float) -> list:
    groups: dict = {}
    order: list = []
    solo = []
    for run in runs:
        match = _WF.search(run["run_id"] or "")
        if not match:
            solo.append(run)
            continue
        key = (run.get("session_id") or "", match.group(1))
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(run)
    collapsed = list(solo)
    for key in order:
        members = groups[key]
        wf = key[1]
        roots = [run for run in members if (run["run_id"] or "").endswith(":" + wf)]
        root = roots[0] if roots else min(members, key=lambda run: run["started_at"] or 0)
        row = dict(root)
        row["tokens"] = dict(root.get("tokens") or {})
        row["children"] = len(members) - 1
        if len(members) > 1:
            starts = [run["started_at"] for run in members if run["started_at"] is not None]
            if starts:
                row["started_at"] = min(starts)
            if any(run["status"] == "running" for run in members):
                row["status"] = "running"
                row["ended_at"] = None
            else:
                ends = [run["ended_at"] for run in members if run["ended_at"] is not None]
                if ends:
                    row["ended_at"] = max(ends)
            row["out_bytes"] = sum((run.get("out_bytes") or 0) for run in members)
            row["err_bytes"] = sum((run.get("err_bytes") or 0) for run in members)
            row["out_growing"] = any(run.get("out_growing") for run in members)
            tokens = {name: 0 for name in BILLED_FIELDS}
            costs = []
            for run in members:
                for name in BILLED_FIELDS:
                    tokens[name] += (run.get("tokens") or {}).get(name) or 0
                costs.append(run.get("cost_usd"))
            row["tokens"] = tokens
            priced = [item for item in costs if item is not None]
            row["cost_usd"] = _money(sum(priced)) if priced else None
            row["elapsed_s"] = _elapsed(row, now)
        collapsed.append(row)
    return collapsed
