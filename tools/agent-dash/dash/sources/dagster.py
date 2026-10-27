"""Read-only Dagster runs from a remote webserver.

Off until ``dagster.ssh_host`` is set. One ssh and one GraphQL call per poll. Tags other than the allowlist are
dropped before anything is written. A failed call records last_error and
leaves stored rows alone.

The remote call is the slow part: a fresh ssh handshake alone costs
seconds (more over a jump host), and the webserver sometimes stalls
past the local sweep budget. Each ssh call is therefore bounded by
``dagster.ssh_timeout_s`` (default 20 s, capped by the sweep budget),
connections reuse one dashboard-owned multiplex socket, and close-out
lookups stop at the poll deadline and resume next poll. A fetch that
eats its whole timeout is remote latency, not a hard failure: the poll
keeps the cached rows and marks itself stale instead of erroring. Only
a fast failure (refused, auth, bad response) or a stale run past
``dagster.stale_error_after_s`` surfaces as an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

NAME = "dagster"
INTERVAL_S = 60
SURFACE_ERROR = True

DEFAULT_URL = "http://127.0.0.1:3000/graphql"
DEFAULT_MAX_BACKOFF_S = 300.0
DEFAULT_SSH_TIMEOUT_S = 20.0
SSH_BUDGET_MARGIN_S = 5.0
DEFAULT_STALE_ERROR_AFTER_S = 600.0
CONTROL_PERSIST_S = 600
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
# Wall (`now`) timestamp of the last fully successful poll, or None before
# the first success. Powers the ``stale`` health flag: while fetches time
# out, the stored rows are the cached result and the source is stale
# rather than failed.
last_success_ts = None
# Wall (`now`) timestamp when the current stale run began, or None while
# fresh. Past ``dagster.stale_error_after_s`` the stale run escalates to
# an error so a permanently dead remote stays visible.
stale_since_ts = None
# Close-outs skipped because the active page was full (ACTIVE_LIMIT).
# Surfaced on /api/health as ``dagster.skipped_closeouts``.
skipped_closeouts = 0
# Back-off after ssh failures (timeouts, ssh errors). The first failure keeps
# the normal interval; each further consecutive failure doubles the delay up to
# dagster.max_backoff_s. A poll inside the window returns without ssh.
# ``_clock`` is the monotonic clock the window is measured on (tests patch it).
_clock = time.monotonic
_fail_streak = 0
_backoff_s = 0.0
_next_attempt = None


def health_extra() -> dict:
    """Extra health payload merged by the registry under ``dagster``.

    ``backoff_s`` is the current delay after failures (0 when healthy) and
    ``next_poll_in_s`` the seconds until the next ssh attempt (0 when none is
    pending). ``stale`` is true while fetches time out and the stored rows
    are the last good result; ``last_success_age_s`` says how old that
    result is (None before the first success).
    """
    remaining = 0.0
    if _next_attempt is not None:
        remaining = max(0.0, _next_attempt - _clock())
    age = None
    if last_success_ts is not None:
        try:
            age = round(max(0.0, time.time() - float(last_success_ts)), 1)
        except (TypeError, ValueError):
            age = None
    return {
        "skipped_closeouts": skipped_closeouts,
        "backoff_s": _backoff_s,
        "next_poll_in_s": round(remaining, 1),
        "stale": stale_since_ts is not None,
        "last_success_age_s": age,
    }


def _max_backoff(cfg: dict) -> float:
    block = cfg.get("dagster") if isinstance(cfg.get("dagster"), dict) else {}
    try:
        value = float(block.get("max_backoff_s", DEFAULT_MAX_BACKOFF_S))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BACKOFF_S
    return value if value > 0 else DEFAULT_MAX_BACKOFF_S


def _ssh_timeout_s(cfg: dict) -> float:
    """Wall cap for one ssh call. ``dagster.ssh_timeout_s`` wins."""
    block = cfg.get("dagster") if isinstance(cfg.get("dagster"), dict) else {}
    try:
        value = float(block.get("ssh_timeout_s", DEFAULT_SSH_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_SSH_TIMEOUT_S
    return value if value > 0 else DEFAULT_SSH_TIMEOUT_S


def _stale_error_after_s(cfg: dict) -> float:
    """How long a stale run may last before it escalates to an error."""
    block = cfg.get("dagster") if isinstance(cfg.get("dagster"), dict) else {}
    try:
        value = float(block.get("stale_error_after_s", DEFAULT_STALE_ERROR_AFTER_S))
    except (TypeError, ValueError):
        return DEFAULT_STALE_ERROR_AFTER_S
    return value if value > 0 else DEFAULT_STALE_ERROR_AFTER_S


def _sweep_budget_s(cfg: dict) -> float:
    """The registry's shared join budget, so one ssh call cannot eat it all."""
    try:
        from dash.sources import POLL_BUDGET_S
    except Exception:
        POLL_BUDGET_S = 25.0
    try:
        value = float((cfg or {}).get("poll_budget_s", POLL_BUDGET_S))
    except (TypeError, ValueError):
        return POLL_BUDGET_S
    return value if value > 0 else POLL_BUDGET_S


def _fetch_allowance_s(cfg: dict) -> float:
    """Seconds one ssh fetch may take: the configured ssh timeout, capped
    so the whole poll (fetch plus close-outs) still fits the sweep budget."""
    budget = _sweep_budget_s(cfg)
    cap = max(2.0, budget - SSH_BUDGET_MARGIN_S)
    return max(2.0, min(_ssh_timeout_s(cfg), cap))


def _control_path(cfg: dict) -> str | None:
    """Dashboard-owned ssh multiplex socket pattern, or None for plain ssh.

    ``dagster.control_path`` names it outright. Otherwise
    ``<state_dir>/ssh-cm/%h_%p_%r`` persists one master across polls (and
    across the back-off gaps), so only the first poll after a cold start
    pays the handshake. A missing/unmakable directory falls back to
    plain ssh rather than failing the poll.
    """
    block = cfg.get("dagster") if isinstance(cfg.get("dagster"), dict) else {}
    explicit = block.get("control_path")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    state_dir = (cfg or {}).get("state_dir")
    if not isinstance(state_dir, str) or not state_dir.strip():
        state_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "agent-dash")
    directory = os.path.join(state_dir, "ssh-cm")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return None
    return os.path.join(directory, "%h_%p_%r")


def _is_slow_timeout(err: str | None, elapsed: float, timeout: float) -> bool:
    """True when the fetch ate (nearly) its whole timeout: remote latency,
    not a fast failure. An instant error string with no wait behind it
    stays on the error path even when it names a timeout."""
    if not err or "timed out" not in err:
        return False
    try:
        limit = max(0.0, float(timeout) - 1.0)
    except (TypeError, ValueError):
        return False
    return elapsed >= limit


def _mark_stale(cfg: dict, now: float) -> None:
    """Record a timed-out fetch: keep the cached rows, no error — unless
    the stale run outlasts its grace, when it escalates so a dead remote
    stays visible. Back-off still applies so a struggling remote is not
    hammered."""
    global last_error, stale_since_ts
    if stale_since_ts is None:
        stale_since_ts = now
    _record_failure(cfg)
    if now - stale_since_ts > _stale_error_after_s(cfg):
        last_error = _clip(f"stale: no successful dagster poll for {int(now - stale_since_ts)}s")
        log.warning("dagster: %s", last_error)
    else:
        last_error = None
        log.warning(
            "dagster: fetch timed out; serving cached rows (stale since %s)",
            int(stale_since_ts),
        )


def _mark_fresh(now: float) -> None:
    """A fully successful poll clears any stale run."""
    global last_error, last_success_ts, stale_since_ts
    last_error = None
    last_success_ts = now
    stale_since_ts = None


def _record_success() -> None:
    global _fail_streak, _backoff_s, _next_attempt
    _fail_streak = 0
    _backoff_s = 0.0
    _next_attempt = None


def _record_failure(cfg: dict) -> None:
    """Count one ssh failure and schedule the next attempt after the back-off delay."""
    global _fail_streak, _backoff_s, _next_attempt
    _fail_streak += 1
    delay = INTERVAL_S * (2 ** min(_fail_streak - 1, 30))
    _backoff_s = float(min(delay, _max_backoff(cfg)))
    _next_attempt = _clock() + _backoff_s
    log.warning("dagster: backing off %gs after %d failed poll(s)", _backoff_s, _fail_streak)


def poll(store, cfg, now: float) -> None:
    global last_error
    try:
        _poll(store, cfg or {}, float(now))
    except Exception as exc:
        last_error = _clip(f"{type(exc).__name__}: {exc}")
        log.warning("dagster: %s", last_error)


def ssh_argv(host: str, url: str, curl_timeout_s: int = 20, control_path: str | None = None) -> list[str]:
    argv = [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "BatchMode=yes",
    ]
    if control_path:
        argv += [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPath=" + control_path,
            "-o",
            f"ControlPersist={CONTROL_PERSIST_S}",
        ]
    argv += [host, remote_command(url, curl_timeout_s)]
    return argv


def remote_command(url: str, curl_timeout_s: int = 20) -> str:
    try:
        limit = int(curl_timeout_s)
    except (TypeError, ValueError):
        limit = 20
    limit = max(5, limit)
    return (
        f'curl -s -m {limit} -X POST -H "Content-Type: application/json" '
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
    global last_error, skipped_closeouts
    settings = _settings(cfg)
    if settings is None:
        _mark_fresh(now)
        _record_success()
        return
    host, url = settings
    if _next_attempt is not None and _clock() < _next_attempt:
        # Inside the back-off window: no ssh. last_error stays as the last failure.
        return
    allowance = _fetch_allowance_s(cfg)
    mux = _control_path(cfg)
    curl_timeout = max(5, int(allowance - 5))
    body = json.dumps({"query": QUERY, "variables": {"since": now - WINDOW_S}}).encode("utf-8")
    fetch_start = _clock()
    text, err = _ssh(host, url, body, timeout_s=allowance, curl_timeout_s=curl_timeout, control_path=mux)
    elapsed = _clock() - fetch_start
    if err:
        if _is_slow_timeout(err, elapsed, allowance):
            # Remote latency, not a hard failure: keep the cached rows
            # and mark the source stale instead of erroring.
            _mark_stale(cfg, now)
            return
        last_error = _clip(err)
        log.warning("dagster: %s", last_error)
        _record_failure(cfg)
        return
    _record_success()
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
    _mark_fresh(now)
    if full_page:
        skipped_closeouts += 1
        log.warning("dagster: active page full (%d); skipping close-out", len(active_raw))
        return
    # Close-outs share what's left of the allowance: chunks that do not
    # fit defer to the next poll instead of blowing the sweep budget.
    deadline = _clock() + max(0.0, allowance - elapsed - 1.0)
    _close_absent(store, host, url, active_ids, now, control_path=mux, fetch_timeout_s=allowance, deadline=deadline)


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


def _ssh(
    host: str,
    url: str,
    body: bytes,
    timeout_s: float = DEFAULT_SSH_TIMEOUT_S,
    curl_timeout_s: int = 20,
    control_path: str | None = None,
):
    try:
        limit = float(timeout_s)
    except (TypeError, ValueError):
        limit = DEFAULT_SSH_TIMEOUT_S
    limit = limit if limit > 0 else DEFAULT_SSH_TIMEOUT_S
    try:
        proc = subprocess.run(
            ssh_argv(host, url, curl_timeout_s, control_path),
            input=body,
            capture_output=True,
            timeout=limit,
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


def _close_absent(
    store,
    host: str,
    url: str,
    active_ids: list,
    now: float,
    control_path: str | None = None,
    fetch_timeout_s: float = DEFAULT_SSH_TIMEOUT_S,
    deadline: float | None = None,
) -> None:
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
        if deadline is not None and _clock() >= deadline - 2.0:
            # Out of poll budget: the rest keep their open rows and the
            # next poll picks them up from the top.
            log.warning(
                "dagster: close-out deferred (%d of %d remaining)",
                len(missing) - index,
                len(missing),
            )
            break
        chunk = missing[index:index + BY_ID_BATCH]
        ids = []
        for row in chunk:
            run_id = row.get("run_id") or ""
            short = run_id[len("dagster:"):] if run_id.startswith("dagster:") else run_id
            if short:
                ids.append(short)
        try:
            found = _fetch_by_id(host, url, ids, timeout_s=fetch_timeout_s, control_path=control_path)
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


def _fetch_by_id(
    host: str,
    url: str,
    ids: list,
    timeout_s: float = DEFAULT_SSH_TIMEOUT_S,
    control_path: str | None = None,
) -> dict:
    """Read-only fetch of runs by id. Returns runId -> raw run."""
    if not ids:
        return {}
    body = json.dumps({"query": QUERY_BY_ID, "variables": {"ids": list(ids)}}).encode("utf-8")
    try:
        limit = float(timeout_s)
    except (TypeError, ValueError):
        limit = DEFAULT_SSH_TIMEOUT_S
    text, err = _ssh(
        host,
        url,
        body,
        timeout_s=limit if limit > 0 else DEFAULT_SSH_TIMEOUT_S,
        curl_timeout_s=max(5, int((limit if limit > 0 else DEFAULT_SSH_TIMEOUT_S) - 5)),
        control_path=control_path,
    )
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
