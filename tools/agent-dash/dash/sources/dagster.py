"""Read-only Dagster runs from a remote webserver.

Off until ``dagster.ssh_host`` is set. One ssh and one GraphQL call per poll. Tags other than the allowlist are
dropped before anything is written. A failed call records last_error and
leaves stored rows alone.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

NAME = "dagster"
INTERVAL_S = 60
SURFACE_ERROR = True

DEFAULT_URL = "http://127.0.0.1:3000/graphql"
WINDOW_S = 2 * 3600
TAG_KEYS = ("dagster/partition", "lor/ops_wave", "lor/slot_class", "lor/claim_gib")

_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_URL = re.compile(r"^https?://[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._~:/?-]*)?$")
_RUN_ID = re.compile(r"^[A-Za-z0-9-]{8,80}$")
_JOB = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_TIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

ACTIVE = {"QUEUED", "STARTING", "STARTED", "CANCELING"}
TERMINAL = {"SUCCESS": "SUCCESS", "FAILURE": "FAILURE", "CANCELED": "CANCELED"}
ACTIVE_LIMIT = 200
BY_ID_BATCH = 50

QUERY = """
query DagsterRuns($since: Float!) {
  active: runsOrError(limit: 200, filter: {statuses: [QUEUED, STARTING, STARTED, CANCELING]}) {
    __typename
    ...RunSet
  }
  recent: runsOrError(
    limit: 60
    filter: {statuses: [SUCCESS, FAILURE, CANCELED], updatedAfter: $since}
  ) {
    __typename
    ...RunSet
  }
}

fragment RunSet on RunsOrError {
  ... on Runs {
    results {
      runId
      jobName
      status
      startTime
      endTime
      creationTime
      tags { key value }
    }
  }
  ... on PythonError { message }
  ... on InvalidPipelineRunsFilterError { message }
}
"""

QUERY_BY_ID = """
query DagsterRunsById($ids: [String!]) {
  byId: runsOrError(limit: 200, filter: {runIds: $ids}) {
    __typename
    ... on Runs {
      results {
        runId
        jobName
        status
        startTime
        endTime
        creationTime
        tags { key value }
      }
    }
    ... on PythonError { message }
    ... on InvalidPipelineRunsFilterError { message }
  }
}
"""

log = logging.getLogger("agent_dash.dagster")
last_error = None


def poll(store, cfg, now: float) -> None:
    global last_error
    try:
        _poll(store, cfg or {}, float(now))
    except Exception as exc:
        last_error = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("dagster: %s", last_error)


def ssh_argv(host: str, url: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "BatchMode=yes",
        host,
        remote_command(url),
    ]


def remote_command(url: str) -> str:
    return (
        'curl -s -m 20 -X POST -H "Content-Type: application/json" '
        + url
        + " --data-binary @-"
    )


def select_tags(tags) -> dict:
    """Keep only the allowlisted tag keys. Other keys are discarded."""
    kept = {}
    if not isinstance(tags, list):
        return kept
    for item in tags:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if key not in TAG_KEYS or key in kept or not isinstance(value, str):
            continue
        text = value.strip()
        if not text or len(text) > 200 or "\n" in text or "\r" in text:
            continue
        kept[key] = text
    return kept


def _poll(store, cfg: dict, now: float) -> None:
    global last_error
    settings = _settings(cfg)
    if settings is None:
        last_error = None
        return
    host, url = settings
    body = json.dumps({"query": QUERY, "variables": {"since": now - WINDOW_S}}).encode("utf-8")
    text, err = _ssh(host, url, body)
    if err:
        last_error = _clip(err)
        log.warning("dagster: %s", last_error)
        return
    try:
        payload = json.loads(text or "")
        active_raw, recent_raw = _split(payload, now)
    except (json.JSONDecodeError, ValueError) as exc:
        last_error = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("dagster: %s", last_error)
        return
    found = {}
    for raw in list(active_raw) + list(recent_raw):
        if isinstance(raw, dict) and isinstance(raw.get("runId"), str):
            found[raw["runId"]] = raw
    for raw in found.values():
        row = _map(raw, now)
        if row is not None:
            store.upsert_run(row)
    active_ids = [rid for rid in found if rid in {r.get("runId") for r in active_raw}]
    active_ids = sorted({rid for rid in active_ids if isinstance(rid, str)})
    full_page = len(active_raw) >= ACTIVE_LIMIT
    _remember_active(store, active_ids, now, full_page)
    last_error = None
    if full_page:
        log.warning("dagster: active page full (%d); skipping close-out", len(active_raw))
        return
    _close_absent(store, host, url, active_ids, now)


def _settings(cfg: dict):
    block = cfg.get("dagster") if isinstance(cfg.get("dagster"), dict) else {}
    enabled = block.get("enabled", True)
    if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in ("0", "false", "no", "off")):
        return None
    host = str(block.get("ssh_host") or "").strip()
    url = str(block.get("graphql_url") or DEFAULT_URL).strip()
    if not host:
        return None
    if not _HOST.match(host) or not _URL.match(url):
        raise ValueError("dagster config rejected")
    return host, url


def _ssh(host: str, url: str, body: bytes):
    try:
        proc = subprocess.run(
            ssh_argv(host, url),
            input=body,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "ssh timed out"
    except OSError as exc:
        return None, _clip(f"ssh failed: {exc}")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = err[-1] if err else f"ssh exit {proc.returncode}"
        return None, _clip(detail)
    text = proc.stdout.decode("utf-8", "replace")
    if not text.strip():
        return None, "empty response"
    return text, None


def _collect(payload: dict, now: float) -> list:
    active_raw, recent_raw = _split(payload, now)
    found = {}
    for raw in list(active_raw) + list(recent_raw):
        if isinstance(raw, dict) and isinstance(raw.get("runId"), str):
            found[raw["runId"]] = raw
    return list(found.values())


def _split(payload: dict, now: float) -> tuple[list, list]:
    if not isinstance(payload, dict):
        raise ValueError("bad response")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("graphql error")
    active_raw = _results(data, "active")
    recent_raw = [raw for raw in _results(data, "recent") if _ended_in_window(raw, now)]
    return active_raw, recent_raw


def _results(data: dict, key: str) -> list:
    block = data.get(key)
    if not isinstance(block, dict) or block.get("__typename") != "Runs":
        detail = ""
        if isinstance(block, dict):
            detail = str(block.get("message") or block.get("__typename") or "")
        raise ValueError(_clip(f"{key}: {detail or 'missing'}"))
    results = block.get("results")
    if not isinstance(results, list):
        raise ValueError(f"{key}: no results")
    return [raw for raw in results if isinstance(raw, dict)]


def _remember_active(store, active_ids: list, now: float, full_page: bool) -> None:
    if not callable(getattr(store, "set_dagster_active", None)):
        return
    if not callable(getattr(store, "note_dagster_seen", None)):
        return
    try:
        store.set_dagster_active(active_ids, now, full_page)
        full = [
            rid if rid.startswith("dagster:") else "dagster:" + rid
            for rid in active_ids
        ]
        store.note_dagster_seen(full, now)
    except Exception as exc:
        log.warning("dagster state: %s", type(exc).__name__)


def _close_absent(store, host: str, url: str, active_ids: list, now: float) -> None:
    getter = getattr(store, "dagster_open_runs", None)
    if not callable(getter):
        return
    try:
        stored = getter()
    except Exception:
        return
    active = set(active_ids)
    missing = []
    for row in stored:
        run_id = row.get("run_id") or ""
        short = run_id[len("dagster:"):] if run_id.startswith("dagster:") else run_id
        if short not in active and run_id not in active:
            missing.append(row)
    if not missing:
        return
    by_id = {}
    fetched = set()
    for index in range(0, len(missing), BY_ID_BATCH):
        chunk = missing[index:index + BY_ID_BATCH]
        ids = []
        for row in chunk:
            run_id = row.get("run_id") or ""
            short = run_id[len("dagster:"):] if run_id.startswith("dagster:") else run_id
            if short:
                ids.append(short)
        try:
            found = _fetch_by_id(host, url, ids)
        except Exception as exc:
            log.warning("dagster by-id: %s", type(exc).__name__)
            continue
        fetched.update(ids)
        by_id.update(found)
    for row in missing:
        run_id = row.get("run_id") or ""
        short = run_id[len("dagster:"):] if run_id.startswith("dagster:") else run_id
        if short not in fetched:
            continue
        raw = by_id.get(short)
        if raw is not None:
            mapped = _map_close(raw, now)
            if mapped is not None:
                try:
                    store.upsert_run(mapped)
                except Exception:
                    log.warning("dagster close: %s", type(Exception).__name__)
            continue
        _close_unknown(store, row, now)


def _fetch_by_id(host: str, url: str, ids: list) -> dict:
    """Read-only fetch of runs by id. Returns runId -> raw run."""
    if not ids:
        return {}
    body = json.dumps({"query": QUERY_BY_ID, "variables": {"ids": list(ids)}}).encode("utf-8")
    text, err = _ssh(host, url, body)
    if err:
        raise ValueError(_clip(err))
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{type(exc).__name__}: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("graphql error")
    block = data.get("byId")
    if not isinstance(block, dict) or block.get("__typename") != "Runs":
        detail = ""
        if isinstance(block, dict):
            detail = str(block.get("message") or block.get("__typename") or "")
        raise ValueError(_clip(f"byId: {detail or 'missing'}"))
    results = block.get("results")
    if not isinstance(results, list):
        raise ValueError("byId: no results")
    found = {}
    for raw in results:
        if isinstance(raw, dict) and isinstance(raw.get("runId"), str):
            found[raw["runId"]] = raw
    return found


def _close_unknown(store, row: dict, now: float) -> None:
    """Dagster no longer knows this run: close it as finished/UNKNOWN."""
    run_id = row.get("run_id")
    if not run_id:
        return
    last_seen = None
    getter = getattr(store, "dagster_last_seen", None)
    if callable(getter):
        try:
            last_seen = getter(run_id)
        except Exception:
            last_seen = None
        if last_seen is None and run_id.startswith("dagster:"):
            try:
                last_seen = getter(run_id[len("dagster:"):])
            except Exception:
                last_seen = None
    ended = last_seen or row.get("started_at") or float(now)
    log.warning("dagster: closing unknown run %s as finished/UNKNOWN", run_id)
    try:
        store.upsert_run(
            {
                "run_id": run_id,
                "source": "dagster",
                "status": "finished",
                "verdict": "UNKNOWN",
                "ended_at": ended,
                "pid": None,
            }
        )
    except Exception:
        log.warning("dagster close: %s", type(Exception).__name__)


def _ended_in_window(raw: dict, now: float) -> bool:
    end = _epoch(raw.get("endTime"))
    if end is None:
        return True
    return now - WINDOW_S <= end <= now + 300


def _map(raw: dict, now: float):
    run_id = raw.get("runId")
    job = raw.get("jobName")
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        return None
    if not isinstance(job, str) or not _JOB.match(job):
        return None
    status_name = str(raw.get("status") or "").upper()
    if status_name not in ACTIVE and status_name not in TERMINAL:
        return None
    if status_name in TERMINAL and not _ended_in_window(raw, now):
        return None
    tags = select_tags(raw.get("tags"))
    status, verdict = _status(status_name)
    if status is None:
        return None
    started = _epoch(raw.get("startTime")) or _epoch(raw.get("creationTime"))
    ended = _epoch(raw.get("endTime")) if status == "finished" else None
    tier = tags.get("lor/slot_class")
    if tier is not None and not _TIER.match(tier):
        tier = None
    return {
        "run_id": "dagster:" + run_id,
        "source": "dagster",
        "tier": tier,
        "model": job,
        "label": _label(job, tags),
        "status": status,
        "verdict": verdict,
        "started_at": started,
        "ended_at": ended,
        "clock_source": "dagster",
    }


def _map_close(raw: dict, now: float):
    """Map a by-id close-out result. Unlike `_map`, a terminal run is stored
    no matter how long ago it ended: it finished during a gap the recent
    window never saw, and dropping it would leave it running forever."""
    run_id = raw.get("runId")
    job = raw.get("jobName")
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        return None
    if not isinstance(job, str) or not _JOB.match(job):
        return None
    status_name = str(raw.get("status") or "").upper()
    if status_name not in ACTIVE and status_name not in TERMINAL:
        return None
    tags = select_tags(raw.get("tags"))
    status, verdict = _status(status_name)
    if status is None:
        return None
    started = _epoch(raw.get("startTime")) or _epoch(raw.get("creationTime"))
    ended = _epoch(raw.get("endTime")) if status == "finished" else None
    tier = tags.get("lor/slot_class")
    if tier is not None and not _TIER.match(tier):
        tier = None
    return {
        "run_id": "dagster:" + run_id,
        "source": "dagster",
        "tier": tier,
        "model": job,
        "label": _label(job, tags),
        "status": status,
        "verdict": verdict,
        "started_at": started,
        "ended_at": ended,
        "clock_source": "dagster",
    }


def _status(name: str):
    if name == "QUEUED":
        return "queued", None
    if name in ("STARTING", "STARTED", "CANCELING"):
        return "running", None
    if name in TERMINAL:
        return "finished", TERMINAL[name]
    return None, None


def _label(job: str, tags: dict) -> str:
    base = tags.get("lor/ops_wave") or job
    part = tags.get("dagster/partition")
    if part:
        base = f"{base}:{part}"
    return base[:200]


def _epoch(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    if number > 10**12:
        number = number / 1000.0
    if number <= 0:
        return None
    return number


def _clip(text: str) -> str:
    return str(text).replace("\n", " ").strip()[:200]
