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

QUERY = """
query DagsterRuns($since: Float!) {
  active: runsOrError(limit: 60, filter: {statuses: [QUEUED, STARTING, STARTED, CANCELING]}) {
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
        raw_runs = _collect(payload, now)
    except (json.JSONDecodeError, ValueError) as exc:
        last_error = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("dagster: %s", last_error)
        return
    for raw in raw_runs:
        row = _map(raw, now)
        if row is not None:
            store.upsert_run(row)
    last_error = None


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
    if not isinstance(payload, dict):
        raise ValueError("bad response")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("graphql error")
    found = {}
    for key in ("recent", "active"):
        block = data.get(key)
        if not isinstance(block, dict) or block.get("__typename") != "Runs":
            detail = ""
            if isinstance(block, dict):
                detail = str(block.get("message") or block.get("__typename") or "")
            raise ValueError(_clip(f"{key}: {detail or 'missing'}"))
        results = block.get("results")
        if not isinstance(results, list):
            raise ValueError(f"{key}: no results")
        for raw in results:
            if not isinstance(raw, dict):
                continue
            if key == "recent" and not _ended_in_window(raw, now):
                continue
            run_id = raw.get("runId")
            if isinstance(run_id, str):
                found[run_id] = raw
    return list(found.values())


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
