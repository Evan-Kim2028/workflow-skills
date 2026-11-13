"""agentq Morph rate governor.

A local HTTP proxy between pi and api.morphllm.com. Each request is admitted
only when every Morph budget it touches has room under the learned target,
otherwise it waits in a lane queue (urgent, normal, backlog). Upstream 429s are
retried inside the proxy and never reach the client. The limits are learned from
the 429 bodies and persisted in proxy_state.json.

Stdlib only (asyncio streams and ssl), so launchd runs the file with the system
python3 and needs no virtualenv. Streaming responses pass through chunk by chunk;
the usage object in the last SSE event is read on the way.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import contextlib
import faulthandler
import functools
import getpass
import hashlib
import json
import math
import os
import random
import re
import signal
import sqlite3
import ssl
import statistics
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

LANES = ("urgent", "normal", "backlog")
MORPH_FAMILIES = ("morph-dsv41flash", "morph-kimik31")
DS_FAMILY = MORPH_FAMILIES[0]
OTHER_FAMILY = "other"
FAMILY_KEYS = (*MORPH_FAMILIES, OTHER_FAMILY)
RESTORED_LANE = "restored"
CAPACITY = "capacity"
RESERVE = "reserve"
GUARD = "guard"
EMPTY = "empty"
INFLIGHT = "inflight"
INFLIGHT_MODES = ("shadow", "enforce", "off")
UNARMED = "unarmed"
ARMED = "armed"
SUSPENDED = "suspended"
FAST_LIMIT_QUIET_S = 30 * 60.0
RESERVE_REFILL_S = 30 * 60.0
RESERVE_TARGET_CEILING = 1.0
BACKLOG_AGING_S = 120.0
UNCACHED_RATIO_FLOOR = 0.25
UNCACHED_RATIO_CEIL = 1.25
UNCACHED_RATIO_ALPHA = 0.05
UNCACHED_LOG_EVERY = 50
DEFAULT_HOME = "~/.local/state/agentq"
DEFAULT_UPSTREAM = "https://api.morphllm.com/v1"
KEYCHAIN_SERVICE = "morph-api-key"
STATUS_PATH = "/agentq/status"
UPSTREAM_PREFIX = "/v1"
EX_CONFIG = 78
ROLLING = "rolling"
CALENDAR = "calendar"
WINDOW_MODES = (ROLLING, CALENDAR)
BILLED = "billed"
PROMPT_PLUS_MAX = "prompt_plus_max"
CHARGE_BASES = (BILLED, PROMPT_PLUS_MAX)
MINUTE_S = 60.0
CALIB_KEEP_S = 180.0
#: The governor's own log file, relative to the agentq home unless absolute.
#: Empty means stderr (the pre-rotation behaviour).
LOG_PATH = "logs/proxy.err.log"
#: Rotate the log at this size, keeping this many old files (``…log.1`` … ``…log.3``).
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3

DEFAULT_CONFIG = {
    "port": 18790,
    "upstream": DEFAULT_UPSTREAM,
    "window_s": 60.0,
    "start_target": 0.98,
    "target_floor": 0.6,
    "target_ceiling": 1.5,
    "target_cut": 0.97,
    "target_step_up": 0.02,
    "fast_until": 1.5,
    "fast_step_up": 0.05,
    "fast_interval_s": 10.0,
    "default_output_tokens": 800,
    "give_up_s": 240.0,
    "retry_floor_s": 1.0,
    "retry_jitter_s": 2.0,
    "aging_s": 900.0,
    "connect_timeout_s": 10.0,
    "upstream_idle_s": 120.0,
    "shutdown_s": 25.0,
    "max_tokens_cap": {},
    "window_mode": "rolling",
    "charge_basis": "billed",
    "calendar_jitter_s": 1.5,
    "backlog_max_frac": 0.9,
    "stall_dump_s": 2.0,
    "upstream_pool_max": 64,
    "upstream_pool_idle_s": 30.0,
    "log_path": LOG_PATH,
    "log_max_bytes": LOG_MAX_BYTES,
    "log_backups": LOG_BACKUPS,
    "reserve_refill_s": RESERVE_REFILL_S,
    "backlog_aging_s": BACKLOG_AGING_S,
    "budgets": {
        "total": {"limit_type": "total", "limit": 10_000_000},
        "output": {
            "limit_type": "output", "limit": 70_000, "model_prefix": "morph-dsv41flash",
            "window_mode": "calendar", "reserve": 210_000,
        },
        "kimi_output": {
            "limit_type": "output", "limit": 6_162, "model_prefix": "morph-kimik",
            "window_mode": "calendar", "reserve": 18_486,
        },
        "uncached": {
            "limit_type": "uncached", "limit": 24_290, "model_prefix": "morph-kimik",
            "window_mode": "calendar", "reserve": 72_870,
        },
        "uncached_ds": {
            "limit_type": "uncached", "limit": 400_000, "model_prefix": "morph-dsv41flash",
            "window_mode": "calendar", "reserve": 1_200_000,
        },
        "requests": {"limit_type": "requests", "limit": 1_000, "window_mode": "calendar"},
    },
    "inflight": {
        "mode": "shadow",
        "ab_period_s": 0,
        "min_limit": 16,
        "max_limit": 96,
        "interval_s": 10.0,
        "short_window_s": 30.0,
        "baseline_tau_s": 1800.0,
        "min_samples": 3,
        "min_gradient": 0.5,
        "smoothing": 0.5,
        "rearm_adjusts": 2,
    },
}

SATURATION_WAIT_S = 2.0
LANE_CACHE_S = 2.0
DB_WORKERS = 2
LAG_WINDOW_S = 60.0
REQUEST_RESERVE_S = 5.0
FINAL_RESERVE_S = 3.0
FINAL_BUSY_S = 1.0
LANE_CACHE_MAX = 10_000
PREFIX_STORE_JOBS = 256
ANONYMOUS_JOBS = frozenset({"", "none"})
BLOCK_CHARS = 1024
LARGE_BODY = 256 * 1024
MAINTAIN_S = 0.2
FLUSH_S = 5.0
MAX_REQUEST_BODY = 32 * 1024 * 1024
MAX_429_BODY = 1024 * 1024
MAX_WHOLE_BODY = 8 * 1024 * 1024
READ_CHUNK = 64 * 1024
ROW_WAIT_SAMPLES = 500
UPSTREAM_RECENT_S = 60.0

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
REQUEST_DROP = HOP_BY_HOP | {
    "host",
    "authorization",
    "content-length",
    "accept-encoding",
    "x-agentq-job",
}
RESPONSE_DROP = HOP_BY_HOP | {"content-length"}

LIMIT_PATTERNS = (
    ("output", re.compile(r"Limit:\s*(\d+)\s+output tokens/minute")),
    ("uncached", re.compile(r"Limit:\s*(\d+)\s+uncached input tokens/minute")),
    ("total", re.compile(r"Limit:\s*(\d+)\s+tokens/minute")),
)
LIMIT_TYPES = (*(name for name, _ in LIMIT_PATTERNS), "requests")

PROXY_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS proxy_jobs (
    job TEXT NOT NULL, model TEXT NOT NULL,
    first_ts REAL NOT NULL, last_ts REAL NOT NULL,
    req INT, ok INT, r429 INT,
    input INT, cache_read INT, output INT,
    wait_ms_total INT, upstream_ms_total INT,
    PRIMARY KEY (job, model))
"""

JOB_UPSERT_SQL = """
INSERT INTO proxy_jobs (job, model, first_ts, last_ts, req, ok, r429, input, cache_read, output,
    wait_ms_total, upstream_ms_total)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(job, model) DO UPDATE SET
    req = req + excluded.req,
    ok = ok + excluded.ok,
    r429 = r429 + excluded.r429,
    input = input + excluded.input,
    cache_read = cache_read + excluded.cache_read,
    output = output + excluded.output,
    wait_ms_total = wait_ms_total + excluded.wait_ms_total,
    upstream_ms_total = upstream_ms_total + excluded.upstream_ms_total,
    first_ts = min(first_ts, excluded.first_ts),
    last_ts = max(last_ts, excluded.last_ts)
"""

JOB_COUNTERS = ("req", "ok", "r429", "input", "cache_read", "output", "wait_ms", "upstream_ms")

PROXY_MINUTES_DDL = """
CREATE TABLE IF NOT EXISTS proxy_minutes (
    minute TEXT NOT NULL,
    model TEXT NOT NULL,
    req INT, ok INT, r429 INT,
    input INT, cache_read INT, output INT, uncached_est INT,
    wait_p50_ms INT, wait_p95_ms INT,
    target REAL,
    upstream_ms_sum INT, upstream_n INT, upstream_p50_ms INT, upstream_p95_ms INT,
    ttfb_p50_ms INT, in_flight_max INT,
    inflight_limit INT, inflight_mode TEXT, inflight_arm TEXT, inflight_over INT,
    ttfb_le_p50_ms INT, ttfb_gt_p50_ms INT,
    PRIMARY KEY (minute, model)
)
"""
PROXY_MINUTES_ADDED_COLUMNS = (
    ("upstream_ms_sum", "INT"),
    ("upstream_n", "INT"),
    ("upstream_p50_ms", "INT"),
    ("upstream_p95_ms", "INT"),
    ("ttfb_p50_ms", "INT"),
    ("in_flight_max", "INT"),
    ("inflight_limit", "INT"),
    ("inflight_mode", "TEXT"),
    ("inflight_arm", "TEXT"),
    ("inflight_over", "INT"),
    ("ttfb_le_p50_ms", "INT"),
    ("ttfb_gt_p50_ms", "INT"),
)
MINUTE_SEED_COLUMNS = (
    "req", "ok", "r429", "input", "cache_read", "output", "uncached_est",
    "upstream_ms_sum", "upstream_n", "in_flight_max", "inflight_over",
)


INFLIGHT_MINUTES_DDL = """
CREATE TABLE IF NOT EXISTS inflight_minutes (
    minute TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    arm TEXT NOT NULL,
    inflight_limit INT,
    enforcing INT
)
"""


PROXY_CALIB_DDL = """
CREATE TABLE IF NOT EXISTS proxy_calib (
    minute TEXT PRIMARY KEY,
    req INT, prompt_est INT, max_tokens INT, billed INT,
    first_429_s REAL, r429 INT
)
"""

CALIB_FIELDS = ("req", "prompt_est", "max_tokens", "billed")


class TruncatedBody(ValueError):
    """A body ended before its declared length or its terminating chunk."""


class UpstreamError(Exception):
    """Upstream sent something the proxy cannot frame (for example a missing status line)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class KeyMissingError(RuntimeError):
    """The Morph key is not in the Keychain and no override is set."""


UPSTREAM_FAILURES = (OSError, ssl.SSLError, asyncio.TimeoutError, UpstreamError, TruncatedBody, EOFError)


def failure_kind(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, TruncatedBody):
        return "truncated"
    if isinstance(exc, UpstreamError):
        return exc.kind
    return "connection"


def default_log(event: str, **fields: object) -> None:
    """Write one structured line to stderr. Callers pass only ids and counts."""
    sys.stderr.write(_log_line(event, fields))
    sys.stderr.flush()


def _log_line(event: str, fields: dict) -> str:
    """One log line. The format is parsed by the dashboard and the calibration
    scripts (``<stamp> agentq-proxy <event> <key>=<value>…``), so it is fixed."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pairs = "".join(f" {key}={value}" for key, value in fields.items())
    return f"{stamp} agentq-proxy {event}{pairs}\n"


class RotatingLog:
    """A size-based rotating log writer, opened by path.

    The governor used to write every line to stderr, which launchd appends to
    ``logs/proxy.err.log`` with no bound (measured: 7.9 MB, about 100 lines a
    minute, no rotation). The governor now opens that file itself and rotates
    it at ``log_max_bytes`` (default 10 MB), keeping ``log_backups`` old files
    (default 3) as ``proxy.err.log.1`` … ``.3``.

    Stderr stays the startup-error sink: launchd still redirects it to the
    same path, so a failure before this writer exists (a missing key, a
    traceback) still reaches the file. Once the writer is open it owns
    rotation, and a stderr line launchd writes into a renamed file stays in
    that old file, which is why stderr is used for startup errors only.
    """

    def __init__(self, path, *, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self.error = None
        self._fh = None
        self._size = 0
        self._lock = threading.Lock()

    def backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def write(self, text: str) -> None:
        """Append one line, rotating first when the file is at its limit."""
        data = text.encode("utf-8", "replace")
        with self._lock:
            try:
                if self._fh is None:
                    self._open()
                self._fh.write(data)
                self._fh.flush()
                self._size += len(data)
                if self._size >= self.max_bytes:
                    self._rotate()
            except OSError as exc:  # a full disk must not stop the proxy
                self.error = exc
                self._fh = None

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError as exc:
                    self.error = exc

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0
        if self._size >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        for index in range(self.backups, 1, -1):
            older = self.backup_path(index - 1)
            if older.exists():
                os.replace(older, self.backup_path(index))
        if self.path.exists():
            os.replace(self.path, self.backup_path(1))
        self._fh = open(self.path, "ab")
        self._size = 0


def file_log(home, config: dict):
    """A governor log callback writing to the rotating file (``log_path``), or stderr
    when ``log_path`` is empty. Same line format as ``default_log``."""
    path = str(config.get("log_path", LOG_PATH) or "").strip()
    if not path:
        return default_log
    target = Path(path)
    if not target.is_absolute():
        target = Path(home) / target
    writer = RotatingLog(
        target,
        max_bytes=int(config.get("log_max_bytes", LOG_MAX_BYTES)),
        backups=int(config.get("log_backups", LOG_BACKUPS)),
    )

    def _log(event: str, **fields: object) -> None:
        writer.write(_log_line(event, fields))

    _log.writer = writer  # so a shutdown can close the file
    return _log


def merge_config(base: dict, overrides: dict) -> dict:
    """Overlay user settings on the defaults; budget specs merge per budget."""
    merged = json.loads(json.dumps(base))
    for key, value in overrides.items():
        if key == "budgets":
            for name, spec in value.items():
                merged["budgets"].setdefault(name, {}).update(spec)
        else:
            merged[key] = value
    return merged


def load_config(home: Path) -> dict:
    """Read proxy.json from the agentq home over the defaults; a missing file is fine."""
    path = home / "proxy.json"
    if not path.exists():
        return merge_config(DEFAULT_CONFIG, {})
    return merge_config(DEFAULT_CONFIG, json.loads(path.read_text()))


def load_key(environ: dict | None = None) -> str:
    """Return the Morph key from the override env var or the macOS Keychain.

    Errors name the source, never the value.
    """
    env = os.environ if environ is None else environ
    override = env.get("AGENTQ_UPSTREAM_KEY")
    if override:
        return override
    user = env.get("USER") or getpass.getuser()
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", user, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeyMissingError(type(exc).__name__) from exc
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise KeyMissingError(f"no entry for service {KEYCHAIN_SERVICE}")
    return key


def tokens_for_chars(chars: int) -> int:
    """Rough token count: four characters per token, rounded up."""
    return (max(chars, 0) + 3) // 4


def prefix_blocks(text: str) -> tuple[bytes, ...]:
    """Cumulative digests of a prompt's fixed-size blocks: entry k digests the first k+1 blocks. A job keeps
    these instead of the prompt text, and two prompts share their first k+1 blocks exactly when entry k
    matches, so a shared prefix is found with one comparison per probe."""
    chain = b""
    digests = []
    for start in range(0, len(text), BLOCK_CHARS):
        piece = text[start : start + BLOCK_CHARS].encode("utf-8", "replace")
        chain = hashlib.blake2b(chain + piece, digest_size=8).digest()
        digests.append(chain)
    return tuple(digests)


def is_anonymous(job: str) -> bool:
    """A request with no job id, or the literal "none" that pi sends when agentq did not launch it."""
    return job in ANONYMOUS_JOBS


def body_snippet(raw: bytes, limit: int = 300) -> str:
    """The first characters of an upstream error body, on one line with no quotes."""
    text = raw.decode("utf-8", "replace")[:limit]
    return text.translate(str.maketrans({"\n": " ", "\r": " ", '"': " ", "'": " "}))


def anonymous_key(blocks: tuple[bytes, ...]) -> str:
    """The prefix store key for an anonymous request: a digest of all its blocks."""
    return "anon:" + hashlib.sha1(b"".join(blocks)).hexdigest()


def _common_blocks(previous: tuple[bytes, ...], blocks: tuple[bytes, ...]) -> int:
    """How many leading whole blocks two prompts share. Entries are cumulative, so entry k matches exactly
    when the first k+1 blocks match. That is monotone, and a binary search finds the boundary in log steps."""
    low, high = 0, min(len(previous), len(blocks))
    while low < high:
        middle = (low + high + 1) // 2
        if previous[middle - 1] == blocks[middle - 1]:
            low = middle
        else:
            high = middle - 1
    return low


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[index]


def minute_stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


def minute_epoch(stamp: str) -> float:
    """The epoch second at the start of a proxy_minutes stamp."""
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M")))


def parse_stated_limit(text: str) -> tuple[str, int] | None:
    """Return the budget name and limit a 429 body states, if it states one."""
    for name, pattern in LIMIT_PATTERNS:
        match = pattern.search(text)
        if match:
            return name, int(match.group(1))
    return None


def usage_fields(usage: dict) -> tuple[int, int, int]:
    """Return (prompt tokens, completion tokens, cached prompt tokens) from a usage object."""
    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return prompt, completion, min(cached, prompt)


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def estimate_for(limit_type: str, prompt: int, output: int, uncached: int) -> int:
    """Token cost a request is expected to add to one budget before its real usage arrives."""
    if limit_type == "output":
        return output
    if limit_type == "uncached":
        return uncached
    if limit_type == "requests":
        return 1
    return prompt + output


def reserved_for(limit_type: str, prompt: int, requested: int, uncached: int) -> int:
    """Charge for a reservation: the requested max_tokens counts in full, as if Morph held it for the minute."""
    if limit_type == "output":
        return requested
    if limit_type == "uncached":
        return uncached
    if limit_type == "requests":
        return 1
    return prompt + requested


def clamp_ratio(value: float) -> float:
    """A learned uncached correction, kept between UNCACHED_RATIO_FLOOR and UNCACHED_RATIO_CEIL."""
    return min(UNCACHED_RATIO_CEIL, max(UNCACHED_RATIO_FLOOR, value))


def model_family(model: str) -> str:
    """The model name without its trailing version digits: morph-dsv41flash-1017 is morph-dsv41flash."""
    return re.sub(r"-\d+$", "", model)


def family_of(model: str) -> str:
    """The Morph family a request belongs to: one of MORPH_FAMILIES, or OTHER_FAMILY for any other model."""
    for family in MORPH_FAMILIES:
        if model.startswith(family):
            return family
    return OTHER_FAMILY


def seconds_into_minute(wall: float) -> float:
    return wall % MINUTE_S


def seconds_to_next_minute(wall: float) -> float:
    """Seconds until the next UTC :00. Exactly on :00 this is zero."""
    return (-wall) % MINUTE_S


@functools.lru_cache(maxsize=16)
def read_only_uri(db_path: Path) -> str:
    """The sqlite URI for read-only access to a store, resolved once per path (resolve() lstats every component)."""
    return f"{Path(db_path).resolve().as_uri()}?mode=ro"


def read_calib_counts(db_path: Path, minute: str) -> dict | None:
    """The stored proxy_calib row for one UTC minute as a seed, or None. Read-only."""
    try:
        connection = sqlite3.connect(read_only_uri(db_path), uri=True, timeout=1.0)
        try:
            found = connection.execute(
                "SELECT req, prompt_est, max_tokens, billed, first_429_s, r429 FROM proxy_calib WHERE minute = ?",
                (minute,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if found is None:
        return None
    req, prompt_est, max_tokens, billed, first_429_s, r429 = found
    overall = {
        "req": int(req or 0),
        "prompt_est": int(prompt_est or 0),
        "max_tokens": int(max_tokens or 0),
        "billed": int(billed or 0),
    }
    return {"overall": overall, "families": {}, "first_429_s": first_429_s, "r429": int(r429 or 0)}


def write_calib_rows(db_path: Path, rows: list[tuple], busy_s: float = 5.0) -> None:
    """Upsert one proxy_calib row per UTC minute. The minute is owned by this process, so each write
    carries the whole minute. Runs in a worker thread."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=busy_s)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_s * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(PROXY_CALIB_DDL)
        connection.executemany("INSERT OR REPLACE INTO proxy_calib VALUES (?,?,?,?,?,?,?)", rows)
        connection.commit()
    finally:
        connection.close()


def read_lane(db_path: Path, job: str) -> str:
    """Read a job's lane from agentq.db without writing. Anything missing means normal."""
    if not db_path.exists():
        return "normal"
    try:
        connection = sqlite3.connect(read_only_uri(db_path), uri=True, timeout=5.0)
        try:
            row = connection.execute("SELECT lane FROM jobs WHERE id = ?", (job,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return "normal"
    lane = row[0] if row else None
    return lane if lane in LANES else "normal"


def empty_minute_row() -> dict:
    return {
        "req": 0, "ok": 0, "r429": 0, "input": 0, "cache_read": 0, "output": 0, "uncached_est": 0,
        "upstream_ms_sum": 0, "upstream_n": 0, "in_flight_max": 0, "inflight_over": 0,
        "waits": [], "upstream_samples": [], "ttfb_samples": [], "ttfb_le_samples": [], "ttfb_gt_samples": [],
    }


def merge_row_seed(row: dict, seed: dict | None) -> None:
    """Add a stored minute's counters to an in-memory row. The in-flight peak keeps the larger value."""
    if not seed:
        return
    for name in MINUTE_SEED_COLUMNS:
        if name == "in_flight_max":
            row[name] = max(row[name], seed.get(name, 0))
        else:
            row[name] += seed.get(name, 0)


def merge_calib_seed(record: dict, seed: dict | None) -> None:
    """Add a stored calibration minute to an in-memory record: overall counters and 429s add, the first 429 keeps the earlier time."""
    if not seed:
        return
    for name in CALIB_FIELDS:
        record["overall"][name] += seed["overall"].get(name, 0)
    record["r429"] += seed.get("r429", 0)
    firsts = [value for value in (record["first_429_s"], seed.get("first_429_s")) if value is not None]
    record["first_429_s"] = min(firsts) if firsts else None


def ensure_minutes_schema(connection: sqlite3.Connection) -> None:
    """Create proxy_minutes, then add each latency column an older table lacks. Safe to run every time."""
    connection.execute(PROXY_MINUTES_DDL)
    present = {row[1] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
    for name, kind in PROXY_MINUTES_ADDED_COLUMNS:
        if name not in present:
            connection.execute(f"ALTER TABLE proxy_minutes ADD COLUMN {name} {kind}")


def read_minute_counts(db_path: Path, minute: str, model: str) -> dict | None:
    """The counters already stored for one model and minute, or None. Waits and latency samples start
    empty; the latency sums and counts carry over. Columns an older table lacks read as zero. Read-only."""
    try:
        connection = sqlite3.connect(read_only_uri(db_path), uri=True, timeout=1.0)
        try:
            present = {row[1] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
            selected = [name for name in MINUTE_SEED_COLUMNS if name in present]
            found = connection.execute(
                f"SELECT {', '.join(selected)} FROM proxy_minutes WHERE minute = ? AND model = ?",
                (minute, model),
            ).fetchone() if selected else None
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if found is None:
        return None
    row = empty_minute_row()
    for name, value in zip(selected, found):
        row[name] = int(value or 0)
    return row


def ms_or_none(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


def sample_percentile_ms(samples: list[int], fraction: float) -> int | None:
    """The fraction-th percentile of recorded millisecond samples, or None when there are none."""
    if not samples:
        return None
    return round(percentile(sorted(samples), fraction))


MINUTE_ROW_COLUMNS = (
    "minute", "model", "req", "ok", "r429", "input", "cache_read", "output", "uncached_est",
    "wait_p50_ms", "wait_p95_ms", "target", "upstream_ms_sum", "upstream_n", "upstream_p50_ms",
    "upstream_p95_ms", "ttfb_p50_ms", "in_flight_max", "inflight_limit", "inflight_mode", "inflight_arm",
    "inflight_over", "ttfb_le_p50_ms", "ttfb_gt_p50_ms",
)


def write_minute_rows(db_path: Path, rows: list[tuple], busy_s: float = 5.0) -> None:
    """Upsert one proxy_minutes row per model per minute. Runs in a worker thread."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=busy_s)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_s * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        ensure_minutes_schema(connection)
        marks = ",".join("?" * len(MINUTE_ROW_COLUMNS))
        columns = ",".join(MINUTE_ROW_COLUMNS)
        connection.executemany(f"INSERT OR REPLACE INTO proxy_minutes ({columns}) VALUES ({marks})", rows)
        connection.commit()
    finally:
        connection.close()


def write_inflight_minute_rows(db_path: Path, rows: list[tuple], busy_s: float = 5.0) -> None:
    """Upsert the limiter's record for each calendar minute the proxy was running: (minute, mode, arm, limit,
    enforcing). Written on every flush, so a minute keeps its last state. A minute with no row was not recorded
    (the proxy was down or not yet instrumented), and the report leaves it out. Runs in a worker thread."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=busy_s)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_s * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(INFLIGHT_MINUTES_DDL)
        connection.executemany(
            "INSERT OR REPLACE INTO inflight_minutes (minute, mode, arm, inflight_limit, enforcing) VALUES (?,?,?,?,?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def write_job_rows(db_path: Path, rows: list[tuple], busy_s: float = 5.0) -> None:
    """Add per-run deltas to proxy_jobs. Additive upserts, so two processes or restarts never overwrite.
    Runs in a worker thread."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=busy_s)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_s * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(PROXY_JOBS_DDL)
        connection.executemany(JOB_UPSERT_SQL, rows)
        connection.commit()
    finally:
        connection.close()


def write_state_file(path: Path, data: dict) -> None:
    """Replace the state file atomically. Runs in a worker thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)


class InflightLimiter:
    """Adaptive cap on DeepSeek requests in flight at Morph: a gradient controller on first-byte time.

    Every interval the short-run median first-byte time (the last short_window_s of successful
    DeepSeek responses) is compared with a long-run EWMA baseline (time constant baseline_tau_s).
    gradient = clamp(baseline / short, min_gradient, 1.0). The raw limit is limit * gradient + sqrt(limit),
    the queue allowance, and the limit moves smoothing of the way toward it, clamped to [min, max].

    mode decides what the limit does. "shadow" computes and records the limit but never holds a request.
    "enforce" holds DeepSeek requests above the limit in the pump. "off" computes nothing. With
    ab_period_s > 0 and mode "enforce", calendar periods alternate between an enforce arm (even periods
    since the epoch) and an off arm (odd periods), so the two can be compared on the same traffic.

    The limit is reported only after the first measurement (armed). Until then, and whenever the short
    run has too few samples, the limit does not move, so the limiter never blocks without data.
    """

    def __init__(self, settings: dict | None, now: float) -> None:
        values = DEFAULT_CONFIG["inflight"] | (settings or {})
        self.mode = str(values["mode"])
        if self.mode not in INFLIGHT_MODES:
            raise ValueError(f"inflight: mode must be one of {INFLIGHT_MODES}, not {self.mode!r}")
        self.ab_period_s = float(values["ab_period_s"])
        if self.ab_period_s < 0 or (self.ab_period_s and self.ab_period_s % MINUTE_S):
            raise ValueError(f"inflight: ab_period_s must be 0 or a whole number of minutes, not {self.ab_period_s}")
        self.min_limit = int(values["min_limit"])
        self.max_limit = int(values["max_limit"])
        if not 1 <= self.min_limit <= self.max_limit:
            raise ValueError(f"inflight: need 1 <= min_limit <= max_limit, got {self.min_limit}..{self.max_limit}")
        self.interval_s = float(values["interval_s"])
        self.short_window_s = float(values["short_window_s"])
        self.baseline_tau_s = float(values["baseline_tau_s"])
        self.min_samples = int(values["min_samples"])
        self.min_gradient = float(values["min_gradient"])
        self.smoothing = float(values["smoothing"])
        self.rearm_adjusts = max(1, int(values["rearm_adjusts"]))
        self.limit = float(self.max_limit)
        self.baseline_s: float | None = None
        self.samples: deque[tuple[float, float]] = deque()
        self.last_adjust = now
        self.state = UNARMED
        self.streak = 0

    @property
    def measuring(self) -> bool:
        return self.mode != "off"

    def active(self) -> bool:
        """The latched state: armed. Changes only in adjust(), never from the sample count at read time."""
        return self.measuring and self.state == ARMED

    def current_limit(self) -> int | None:
        """The limit the proxy computes and reports (shadow included); None unless the latched state is armed."""
        return round(self.limit) if self.active() else None

    def arm_at(self, wall: float) -> str:
        """Which arm the wall-clock time falls in: shadow, off, enforce, or off for a disabled limiter."""
        if self.mode != "enforce":
            return self.mode
        if not self.ab_period_s:
            return "enforce"
        return "enforce" if int(wall // self.ab_period_s) % 2 == 0 else "off"

    def enforcing(self, wall: float) -> bool:
        """True when a request above the limit must wait at this wall-clock time (and the latched state is armed)."""
        return self.active() and self.arm_at(wall) == "enforce"

    def observe(self, ttfb_s: float, now: float) -> None:
        self.samples.append((now, ttfb_s))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.short_window_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def short_run_s(self, now: float) -> float | None:
        """Median first-byte time over the short window, or None with fewer than min_samples."""
        self._prune(now)
        if len(self.samples) < self.min_samples:
            return None
        return statistics.median(value for _, value in self.samples)

    def adjust(self, now: float) -> bool:
        """The only place the state changes. Runs once per interval. Returns True when the reported limit changed.

        A short window with fewer than min_samples fresh samples suspends an armed limit at once and resets the
        re-arm streak. A suspended or unarmed limit re-arms only after rearm_adjusts consecutive adjusts that each
        find enough fresh samples; samples that arrive between adjusts never re-arm it. The baseline is kept
        across suspension.
        """
        if not self.measuring or now - self.last_adjust < self.interval_s:
            return False
        self.last_adjust = now
        before = self.current_limit()
        short = self.short_run_s(now)
        if short is None or short <= 0:
            self.streak = 0
            if self.state == ARMED:
                self.state = SUSPENDED
            return self.current_limit() != before
        if self.baseline_s is None:
            self.baseline_s = short
        if self.state != ARMED:
            self.streak += 1
            if self.streak < self.rearm_adjusts:
                return False
            self.state = ARMED
            self.streak = 0
            return self.current_limit() != before
        weight = 1.0 - math.exp(-self.interval_s / self.baseline_tau_s)
        self.baseline_s += weight * (short - self.baseline_s)
        gradient = min(1.0, max(self.min_gradient, self.baseline_s / short))
        raw = self.limit * gradient + math.sqrt(self.limit)
        self.limit = min(float(self.max_limit), max(float(self.min_limit), self.limit + self.smoothing * (raw - self.limit)))
        return self.current_limit() != before


class Reserve:
    """The burst reserve of one budget: its level, its capacity, and the overflow drained for each minute
    that still has charges (``drained``, keyed by minute stamp). A minute's drain follows its own charged
    total, so a charge that completes or is released a minute after its admission is reconciled against
    the admission minute, not the current one."""

    def __init__(
        self,
        capacity: float,
        *,
        refill_s: float = RESERVE_REFILL_S,
        wall: float = 0.0,
        level: float | None = None,
    ) -> None:
        if capacity < 0:
            raise ValueError(f"reserve capacity must be >= 0, not {capacity!r}")
        if refill_s <= 0:
            raise ValueError(f"reserve refill_s must be > 0, not {refill_s!r}")
        self.capacity = float(capacity)
        self.refill_s = float(refill_s)
        self.level = self.capacity if level is None else min(self.capacity, max(0.0, float(level)))
        self.wall = float(wall)
        self.drained: dict[str, float] = {}

    @property
    def refill_per_s(self) -> float:
        return self.capacity / self.refill_s

    def refill(self, wall: float) -> None:
        """Add the refill earned since the last call, capped at capacity. Earlier times are ignored."""
        if wall > self.wall:
            self.level = min(self.capacity, self.level + (wall - self.wall) * self.refill_per_s)
            self.wall = wall

    def reconcile(self, wall: float, ledger: dict[str, float], free: float) -> None:
        """Refill to ``wall``, then bring every minute's drain to its charged total.

        ``ledger`` maps each minute that still holds charges to their total; ``free`` is the allowance for one
        minute. A minute whose total grew drains the difference, and one whose total shrank (a completion
        below its estimate, or a release) refunds it, up to capacity. A minute that is no longer in the ledger
        has no charges left, so its drain stands and its record is dropped.
        """
        self.refill(wall)
        for minute in [m for m in self.drained if m not in ledger]:
            del self.drained[minute]
        for minute, used in ledger.items():
            owed = max(0.0, used - free)
            previous = self.drained.get(minute, 0.0)
            self.level = min(self.capacity, max(0.0, self.level - (owed - previous)))
            self.drained[minute] = owed

    def sync(self, wall: float, minute: str, used: float, free: float) -> None:
        """Reconcile a single minute, the view the replay uses when earlier minutes are settled."""
        self.reconcile(wall, {minute: used}, free)

    def admission_overflow(self, used: float, estimate: float, free: float, minute: str) -> float:
        """Reserve tokens one more charge of ``estimate`` would drain in ``minute``, given the minute's usage so far."""
        return max(0.0, used + estimate - free) - self.drained.get(minute, 0.0)

    def covers(self, overflow: float) -> bool:
        """True when the reserve can pay this overflow. An overflow larger than the whole reserve is
        covered by a full reserve, so one oversized request cannot wait forever."""
        return overflow <= 0 or self.level >= min(overflow, self.capacity)

    def seconds_until_covered(self, overflow: float) -> float:
        """Seconds of refill until ``covers(overflow)`` holds."""
        needed = min(overflow, self.capacity) - self.level
        if needed <= 0 or self.refill_per_s <= 0:
            return 0.0
        return needed / self.refill_per_s

    def empty(self) -> None:
        """A real 429 named this budget: its reserve is gone, not merely low."""
        self.level = 0.0

    def snapshot(self) -> dict:
        return {
            "level": round(self.level, 3),
            "wall": round(self.wall, 3),
            "drained": {minute: round(value, 3) for minute, value in self.drained.items()},
        }

    def restore(self, data: dict, now: float) -> None:
        """Load a saved snapshot. The level refills for the time since it was taken. The per-minute drains
        are kept, and the next reconcile drops any minute that no longer holds charges."""
        saved_wall = float(data.get("wall", now))
        self.level = min(self.capacity, max(0.0, float(data.get("level", self.capacity))))
        self.wall = min(saved_wall, now)
        self.refill(now)
        drained = data.get("drained")
        self.drained = (
            {str(minute): max(0.0, float(value)) for minute, value in drained.items()}
            if isinstance(drained, dict)
            else {}
        )


@dataclass(eq=False)
class Entry:
    """One budget charge. In-flight charges hold the estimate; done ones hold real usage."""

    at: float
    amount: int
    lane: str
    done: bool = False
    wall_at: float = 0.0


@dataclass(eq=False)
class Budget:
    """A rolling-window token budget with a learned limit and a target fraction of it.

    limit_type says which Morph limit this budget tracks (total, output or uncached). A stated limit
    is only ever adopted by a budget with the same limit_type that applies to the refused model.
    """

    name: str
    limit: int
    target: float
    limit_type: str = "total"
    model_prefix: str | None = None
    model: str | None = None
    hold_s: float = 600.0
    window_mode: str = ROLLING
    charge_basis: str = BILLED
    reserve: Reserve | None = None
    entries: list[Entry] = field(default_factory=list)
    last_429: float = float("-inf")
    last_cut: float = float("-inf")
    last_grow: float = 0.0
    fast_limit: float | None = None
    saturated: bool = False
    saturated_since_tick: bool = False

    def applies(self, model: str) -> bool:
        if self.model_prefix is not None:
            return model.startswith(self.model_prefix)
        if self.model is not None:
            return model == self.model
        return True

    def cap(self) -> float:
        return self.limit * self.target

    def prune(self, now: float, window: float) -> None:
        """Forget completed charges older than the window. Unfinished charges older than hold_s are
        expired too, so a lost request cannot hold budget forever."""
        cutoff = now - window
        hold_cutoff = now - self.hold_s
        self.entries = [
            e for e in self.entries if (e.done and e.at > cutoff) or (not e.done and e.at > hold_cutoff)
        ]

    def used(self, now: float, window: float, lane: str | None = None, wall_start: float | None = None) -> int:
        """Tokens charged in the current window. Rolling: completed charges newer than the window, plus
        every charge in flight. Calendar (wall_start given): charges admitted at or after wall_start."""
        if wall_start is None:
            self.prune(now, window)
            return sum(e.amount for e in self.entries if lane is None or e.lane == lane)
        return sum(
            e.amount for e in self.entries if e.wall_at >= wall_start and (lane is None or e.lane == lane)
        )

    def prune_settled(self, wall: float, minute_start: float) -> None:
        """Drop past minutes whose charges have all settled, and undone charges older than hold_s.

        A minute is dropped whole, never piecemeal, so a reserve drain is never reconciled against a partial
        ledger. A minute with a charge still in flight is kept, so that charge can settle into its own minute.
        """
        alive = [e for e in self.entries if e.done or wall - e.wall_at < self.hold_s]
        open_minutes = {minute_stamp(e.wall_at) for e in alive if not e.done}
        self.entries = [
            e for e in alive if e.wall_at >= minute_start or minute_stamp(e.wall_at) in open_minutes
        ]

    def seconds_until_free(self, needed: float, now: float, window: float) -> float:
        """Seconds until completed charges older than the window free `needed` tokens."""
        if needed <= 0:
            return 0.0
        freed = 0
        completed = sorted((e for e in self.entries if e.done and e.amount > 0), key=lambda e: e.at)
        for entry in completed:
            freed += entry.amount
            if freed >= needed:
                return max(0.0, entry.at + window - now)
        return 0.0


@dataclass(eq=False)
class Ticket:
    """One client request waiting for, or holding, admission."""

    job: str
    lane: str
    model: str
    estimates: dict[str, int]
    uncached_est: int
    queued_at: float
    first_seen: float
    waiting_since: float = 0.0
    future: asyncio.Future | None = None
    not_before: float = 0.0
    attempts: int = 0
    prompt_est: int = 0
    requested: int = 0
    admitted_minute: str = ""
    holds: list[tuple[Budget, Entry]] = field(default_factory=list)
    blocked_by: str | None = None
    backoff_by: str | None = None
    last_refusal: str | None = None
    reserve_seen: bool = False
    flight: bool = False
    over_counted: bool = False
    started_above: bool | None = None

    def blocking_budget(self, now: float) -> str | None:
        """The budget holding this waiting ticket now: the budget its 429 named while it backs off,
        else the budget that refused it in the last pump. None when no budget is known to hold it."""
        return self.backoff_by if self.not_before > now else self.blocked_by


class Governor:
    """Admission, lane queues, learned limits and minute counters. All calls run on the event loop
    thread, except lane_for, which the proxy calls in a worker thread and which touches only its own
    cache."""

    def __init__(
        self,
        home: Path,
        config: dict,
        *,
        clock=time.monotonic,
        wall=time.time,
        log=None,
    ) -> None:
        self.home = Path(home)
        self.config = config
        self.clock = clock
        self.wall = wall
        self.log = log or file_log(self.home, config)
        self.window = float(config["window_s"])
        self.backlog_max_frac = float(config.get("backlog_max_frac", DEFAULT_CONFIG["backlog_max_frac"]))
        start = clock()
        hold_s = float(config["give_up_s"]) + float(config["upstream_idle_s"])
        self.budgets: dict[str, Budget] = {}
        for name, spec in config["budgets"].items():
            window_mode = spec.get("window_mode", config["window_mode"])
            charge_basis = spec.get("charge_basis", config["charge_basis"])
            if window_mode not in WINDOW_MODES:
                raise ValueError(f"budget {name}: window_mode must be one of {WINDOW_MODES}, not {window_mode!r}")
            if charge_basis not in CHARGE_BASES:
                raise ValueError(f"budget {name}: charge_basis must be one of {CHARGE_BASES}, not {charge_basis!r}")
            limit_type = spec.get("limit_type", name)
            if limit_type not in LIMIT_TYPES:
                raise ValueError(f"budget {name}: limit_type must be one of {LIMIT_TYPES}, not {limit_type!r}")
            reserve = None
            if spec.get("reserve") is not None:
                if window_mode != CALENDAR:
                    raise ValueError(f"budget {name}: a burst reserve needs window_mode {CALENDAR!r}")
                reserve = Reserve(
                    float(spec["reserve"]),
                    refill_s=float(config.get("reserve_refill_s", RESERVE_REFILL_S)),
                    wall=self.wall(),
                )
            start_target = float(config["start_target"])
            if reserve is not None:
                start_target = min(start_target, RESERVE_TARGET_CEILING)
            self.budgets[name] = Budget(
                name=name,
                limit=int(spec["limit"]),
                target=start_target,
                limit_type=limit_type,
                model_prefix=spec.get("model_prefix"),
                model=spec.get("model"),
                hold_s=hold_s,
                window_mode=window_mode,
                charge_basis=charge_basis,
                reserve=reserve,
                last_grow=start,
            )
        self.uncached_ratio: dict[str, dict] = {}
        self.calib: dict[str, dict] = {}
        self.calib_dirty: set[str] = set()
        self.queues: dict[str, deque[Ticket]] = {lane: deque() for lane in LANES}
        self.output_history: dict[str, deque[int]] = {}
        self.prefixes: OrderedDict[str, tuple[int, tuple[bytes, ...]]] = OrderedDict()
        self.lane_cache: dict[str, tuple[str, float]] = {}
        self.seeder = None
        self.pending_seeds: set[tuple[str, object]] = set()
        self.minute_rows: dict[tuple[str, str], dict] = {}
        self.in_flight: dict[str, int] = {}
        self.upstream_recent: deque[tuple[float, str, float]] = deque()
        self.inflight = InflightLimiter(config.get("inflight"), start)
        self.loop_lag: deque[tuple[float, float]] = deque()
        self.ds_flight = 0
        self.dirty: set[tuple[str, str]] = set()
        self.job_rows: dict[tuple[str, str], dict] = {}
        self.waits: deque[tuple[float, float]] = deque()
        self.errors: dict[str, int] = {}
        self.state_dirty = False
        self.load_state()

    @property
    def db_path(self) -> Path:
        return self.home / "agentq.db"

    def minute_start(self) -> float:
        """Wall-clock time of the current UTC minute's :00."""
        wall = self.wall()
        return wall - seconds_into_minute(wall)

    def _used(self, budget: Budget, lane: str | None = None) -> int:
        """The budget's usage in its current window, in whichever window mode the budget uses."""
        if budget.window_mode == CALENDAR:
            if budget.reserve is None:
                budget.prune_settled(self.wall(), self.minute_start())
            return budget.used(self.clock(), self.window, lane, wall_start=self.minute_start())
        return budget.used(self.clock(), self.window, lane)

    @property
    def state_path(self) -> Path:
        return self.home / "proxy_state.json"

    def load_state(self) -> None:
        """Restore limits, targets and the rolling window. A window is carried over only when the
        state was saved less than one window ago; its usage is re-entered as one completed charge that
        expires when the old window would have, so a restart does not reopen an empty budget."""
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        saved_at = data.get("saved_at")
        age = self.wall() - float(saved_at) if isinstance(saved_at, (int, float)) else None
        for name, spec in (data.get("budgets") or {}).items():
            budget = self.budgets.get(name)
            if budget is None or not isinstance(spec, dict):
                continue
            budget.limit = int(spec.get("limit", budget.limit))
            floor = float(self.config["target_floor"])
            ceiling = float(self.config.get("target_ceiling", 1.0))
            target = min(ceiling, max(floor, float(spec.get("target_frac", budget.target))))
            if budget.reserve is not None:
                target = min(target, RESERVE_TARGET_CEILING)
            budget.target = target
            if spec.get("fast_limit") is not None:
                budget.fast_limit = float(spec["fast_limit"])
                budget.last_429 = self.clock()
            window_used = int(spec.get("window_used", 0))
            if age is not None and 0 <= age < self.window and window_used > 0:
                budget.entries.append(
                    Entry(
                        at=self.clock() - age,
                        amount=window_used,
                        lane=RESTORED_LANE,
                        done=True,
                        wall_at=self.wall() - age,
                    )
                )
            if budget.reserve is not None:
                saved_reserve = spec.get("reserve")
                if isinstance(saved_reserve, dict):
                    budget.reserve.restore(saved_reserve, self.wall())
                else:
                    budget.reserve.empty()
        for family, spec in (data.get("uncached_ratio") or {}).items():
            if isinstance(spec, dict) and isinstance(spec.get("ratio"), (int, float)):
                samples = _as_int(spec.get("samples"))
                self.uncached_ratio[family] = {"ratio": clamp_ratio(float(spec["ratio"])), "samples": samples}

    def snapshot_state(self) -> dict:
        budgets = {}
        for name, b in self.budgets.items():
            entry = {
                "limit": b.limit,
                "target_frac": round(b.target, 6),
                "fast_limit": None if b.fast_limit is None else round(b.fast_limit, 6),
                "window_used": self._used(b),
            }
            if b.reserve is not None:
                self.sync_reserve(b)
                entry["reserve"] = b.reserve.snapshot()
            budgets[name] = entry
        return {"saved_at": self.wall(), "budgets": budgets, "uncached_ratio": self.uncached_ratio}

    def save_state(self) -> None:
        """Write the state now, on the calling thread. The proxy uses the dirty flag instead."""
        write_state_file(self.state_path, self.snapshot_state())
        self.state_dirty = False

    def count_error(self, kind: str) -> None:
        self.errors[kind] = self.errors.get(kind, 0) + 1

    def cached_lane(self, job: str) -> str | None:
        """The job's lane when it is known without touching the store, else None. Never does I/O, so the
        request path only goes to a worker thread for a job it has not seen in the last LANE_CACHE_S."""
        if is_anonymous(job):
            return "normal"
        cached = self.lane_cache.get(job)
        if cached is not None and self.clock() - cached[1] < LANE_CACHE_S:
            return cached[0]
        return None

    def note_loop_lag(self, seconds: float) -> None:
        """Record how late the event loop ran one maintenance tick. A loop that is blocked shows up here."""
        now = self.clock()
        self.loop_lag.append((now, max(0.0, seconds)))
        while self.loop_lag and self.loop_lag[0][0] < now - LAG_WINDOW_S:
            self.loop_lag.popleft()

    def loop_lag_status(self) -> dict:
        now = self.clock()
        recent = [lag for t, lag in self.loop_lag if t >= now - LAG_WINDOW_S]
        return {
            "last_ms": round(self.loop_lag[-1][1] * 1000) if self.loop_lag else None,
            "max_60s_ms": round(max(recent) * 1000) if recent else None,
        }

    def lane_for(self, job: str) -> str:
        if is_anonymous(job):
            return "normal"
        now = self.clock()
        cached = self.lane_cache.get(job)
        if cached is not None and now - cached[1] < LANE_CACHE_S:
            return cached[0]
        if len(self.lane_cache) > LANE_CACHE_MAX:
            self.lane_cache.clear()
        lane = read_lane(self.db_path, job)
        self.lane_cache[job] = (lane, now)
        return lane

    def output_estimate(self, model: str, max_tokens: int | None) -> int:
        history = self.output_history.get(model)
        mean = sum(history) / len(history) if history else float(self.config["default_output_tokens"])
        estimate = int(round(mean))
        if max_tokens:
            estimate = min(estimate, int(max_tokens))
        return max(estimate, 1)

    def uncached_estimate(self, job: str, length: int, blocks: tuple[bytes, ...]) -> int:
        """Tokens beyond the longest prefix any stored entry shares with this request.

        Morph's prompt cache is shared across requests, so every stored entry is a candidate, not
        only this job's. Blocks are compared whole, so a partial most-recent block is not matched
        and the shared prefix can be short by up to one block (BLOCK_CHARS, about 256 tokens).

        An entry that this request extends (the same full blocks, and a longer prompt) is the same
        conversation grown, so it is replaced when the request is anonymous and its shared length is
        taken as the whole stored length, which can overcount by one block. Among candidates, an
        extension beats a plain shared prefix, and ties go to the least recently stored entry.
        Named jobs are keyed by job id. Anonymous requests are keyed by a digest of all their blocks
        and so do not collide across sessions. An empty store counts the whole prompt.
        """
        best_key: str | None = None
        best = (False, 0)
        for key, (previous_length, previous_blocks) in self.prefixes.items():
            common = _common_blocks(previous_blocks, blocks)
            extends = length > previous_length and common >= previous_length // BLOCK_CHARS
            shared = previous_length if extends else min(common * BLOCK_CHARS, previous_length, length)
            if (extends, shared) > best:
                best_key, best = key, (extends, shared)
        anonymous = is_anonymous(job)
        if anonymous and best[0] and best_key is not None:
            del self.prefixes[best_key]
        key = anonymous_key(blocks) if anonymous else job
        self.prefixes[key] = (length, blocks)
        self.prefixes.move_to_end(key)
        while len(self.prefixes) > PREFIX_STORE_JOBS:
            self.prefixes.popitem(last=False)
        return tokens_for_chars(length - best[1])

    def make_ticket(
        self,
        *,
        job: str,
        lane: str,
        model: str,
        length: int,
        blocks: tuple[bytes, ...],
        max_tokens: int | None,
        now: float,
    ) -> Ticket:
        prompt = tokens_for_chars(length)
        requested = _as_int(max_tokens)
        output = self.output_estimate(model, max_tokens)
        applicable = [b for b in self.budgets.values() if b.applies(model)]
        uncached = 0
        if any(b.limit_type == "uncached" for b in applicable):
            uncached = self.uncached_estimate(job, length, blocks)
        admitted_uncached = round(uncached * self.uncached_factor(model))
        estimates: dict[str, int] = {}
        for b in applicable:
            if b.charge_basis == PROMPT_PLUS_MAX:
                estimates[b.name] = reserved_for(b.limit_type, prompt, requested, admitted_uncached)
            else:
                estimates[b.name] = estimate_for(b.limit_type, prompt, output, admitted_uncached)
        return Ticket(
            job=job,
            lane=lane,
            model=model,
            estimates=estimates,
            uncached_est=uncached,
            queued_at=now,
            first_seen=now,
            waiting_since=now,
            prompt_est=prompt,
            requested=requested,
        )

    def uncached_factor(self, model: str) -> float:
        """The learned ratio of actual to estimated uncached input for this model's family, 1.0 until measured."""
        learned = self.uncached_ratio.get(family_of(model))
        return learned["ratio"] if learned else 1.0

    def learn_uncached(self, model: str, estimated: int, actual: int) -> None:
        """Fold one request's actual uncached input into its family's ratio. Logged every UNCACHED_LOG_EVERY samples."""
        if estimated <= 0:
            return
        family = family_of(model)
        learned = self.uncached_ratio.setdefault(family, {"ratio": 1.0, "samples": 0})
        sample = min(max(actual / estimated, 0.0), 2.0)
        learned["ratio"] = clamp_ratio((1.0 - UNCACHED_RATIO_ALPHA) * learned["ratio"] + UNCACHED_RATIO_ALPHA * sample)
        learned["samples"] += 1
        self.state_dirty = True
        if learned["samples"] % UNCACHED_LOG_EVERY == 0:
            self.log("uncached_estimate", family=family, ratio=round(learned["ratio"], 4), samples=learned["samples"])

    def minute_ledger(self, budget: Budget) -> dict[str, float]:
        """Each minute that still holds a charge on the budget, with the total charged in it."""
        ledger: dict[str, float] = {}
        for entry in budget.entries:
            key = minute_stamp(entry.wall_at)
            ledger[key] = ledger.get(key, 0.0) + entry.amount
        return ledger

    def sync_reserve(self, budget: Budget) -> None:
        """Reconcile a budget's burst reserve with every minute still holding charges, then drop settled
        minutes. A budget without a reserve is left alone."""
        if budget.reserve is None:
            return
        wall = self.wall()
        budget.reserve.reconcile(wall, self.minute_ledger(budget), budget.cap())
        budget.prune_settled(wall, self.minute_start())

    def enqueue(self, ticket: Ticket) -> None:
        self.queues[ticket.lane].append(ticket)

    def requeue_head(self, ticket: Ticket) -> None:
        self.queues[ticket.lane].appendleft(ticket)

    def drop(self, ticket: Ticket) -> None:
        """Remove a ticket that will never be sent: its client left or it gave up."""
        with contextlib.suppress(ValueError):
            self.queues[ticket.lane].remove(ticket)

    def promote_aged(self, now: float) -> None:
        """Backlog tickets queued past backlog_aging_s join the normal lane, so a steady stream of other work
        cannot hold them forever. Normal tickets queued past aging_s join the urgent lane, kept in arrival order."""
        backlog_aging = float(self.config.get("backlog_aging_s", BACKLOG_AGING_S))
        stale = [t for t in self.queues["backlog"] if now - t.queued_at > backlog_aging]
        if stale:
            for ticket in stale:
                self.queues["backlog"].remove(ticket)
                ticket.lane = "normal"
                self.log("backlog_aged", job=ticket.job, model=ticket.model)
            self.queues["normal"] = deque(sorted([*self.queues["normal"], *stale], key=lambda t: t.queued_at))
        aging = float(self.config["aging_s"])
        normal = self.queues["normal"]
        aged = [t for t in normal if now - t.queued_at > aging]
        if not aged:
            return
        for ticket in aged:
            normal.remove(ticket)
            ticket.lane = "urgent"
            self.log("aged_to_urgent", job=ticket.job, model=ticket.model)
        urgent = list(self.queues["urgent"]) + aged
        self.queues["urgent"] = deque(sorted(urgent, key=lambda t: t.queued_at))

    def pump(self) -> None:
        """Admit every queued ticket that fits, urgent first, FIFO within a lane.

        Blocking is per budget. A ticket that does not fit marks its blocking budget as blocked for
        the rest of this call, and every later ticket that touches a blocked budget waits, so order
        holds on each budget without stalling tickets that use other budgets. A ticket backing off
        after a 429 blocks nothing, and while it waits its budgets stay closed to backlog.
        Backlog runs only on budgets that no waiting urgent or normal ticket is blocked on.
        A backlog ticket also waits once its budget is past backlog_max_frac of its target, so
        normal and urgent work keep the rest of the budget. That hold does not raise the target.

        A block caused only by the urgent share guard closes its budget for the rest of the urgent
        lane. Normal and backlog tickets still use that budget, which is what the guard intends.
        """
        now = self.clock()
        self.promote_aged(now)
        for budget in self.budgets.values():
            budget.saturated = False
        lower_waiting = bool(self.queues["normal"] or self.queues["backlog"])
        blocked: set[str] = set()
        urgent_guarded: set[str] = set()
        for lane in LANES:
            if lane == "backlog":
                for waiting in (*self.queues["urgent"], *self.queues["normal"]):
                    if waiting.not_before > now:
                        blocked.update(waiting.estimates)
            queue = self.queues[lane]
            index = 0
            while index < len(queue):
                head = queue[index]
                closed = blocked | urgent_guarded if lane == "urgent" else blocked
                if head.not_before > now:
                    head.blocked_by = None
                    head.last_refusal = None
                    index += 1
                    continue
                if not closed.isdisjoint(head.estimates):
                    head.blocked_by = next(name for name in head.estimates if name in closed)
                    head.last_refusal = None
                    index += 1
                    continue
                if self.inflight_full(head):
                    if not head.over_counted:
                        head.over_counted = True
                        self.row(head.model)["inflight_over"] += 1
                    if self.inflight.enforcing(self.wall()):
                        head.blocked_by = None
                        head.last_refusal = INFLIGHT
                        index += 1
                        continue
                found = self._blocker(head, now, lower_waiting)
                if found is None:
                    del queue[index]
                    self._admit(head, now)
                    continue
                blocker, refusal = found
                head.blocked_by = blocker.name
                head.last_refusal = refusal
                if refusal in (RESERVE, EMPTY):
                    head.reserve_seen = True
                if refusal == CAPACITY:
                    blocker.saturated = True
                    blocker.saturated_since_tick = True
                (urgent_guarded if refusal == GUARD else blocked).add(blocker.name)
                index += 1

    def _blocker(self, ticket: Ticket, now: float, lower_waiting: bool) -> tuple[Budget, str] | None:
        """Return (budget, refusal) for the first budget that refuses this ticket now, or None.

        refusal is CAPACITY when the budget is full to its target, RESERVE when a backlog ticket would
        take the budget past ``backlog_max_frac`` of its target (the rest is kept for normal and urgent
        work), and GUARD when the budget has room and only the urgent share guard refuses: urgent
        tickets may hold at most half of a budget while lower lanes wait. A budget with no charges
        in the window always admits one request, even an oversized one, so a large request cannot
        wait forever.
        """
        for name, estimate in ticket.estimates.items():
            budget = self.budgets[name]
            cap = budget.cap()
            used = self._used(budget)
            if budget.reserve is not None:
                self.sync_reserve(budget)
                refusal = self._reserve_refusal(budget, ticket.lane, estimate, used)
                if refusal is not None:
                    return budget, refusal
            else:
                if used > 0 and used + estimate > cap:
                    return budget, CAPACITY
                if ticket.lane == "backlog" and used > 0 and used + estimate > self.backlog_max_frac * cap:
                    return budget, RESERVE
            if ticket.lane == "urgent" and lower_waiting:
                urgent_used = self._used(budget, lane="urgent")
                if urgent_used > 0 and urgent_used + estimate > 0.5 * cap:
                    return budget, GUARD
        return None

    def _reserve_refusal(self, budget: Budget, lane: str, estimate: int, used: float) -> str | None:
        """Why a charge on a budget with a burst reserve must wait, or None. The free allowance is the cap;
        only the overflow past it touches the reserve. Backlog never spends the reserve while its budget
        already has charges this minute (RESERVE). An idle budget admits one oversized backlog request that
        spends only its own overflow, so a request larger than the allowance cannot wait forever. Other work
        waits only when the reserve cannot pay the overflow (EMPTY)."""
        overflow = budget.reserve.admission_overflow(used, estimate, budget.cap(), minute_stamp(self.wall()))
        if overflow <= 0:
            return None
        if lane == "backlog" and used > 0:
            return RESERVE
        if not budget.reserve.covers(overflow):
            return EMPTY
        return None

    def inflight_limit(self) -> int | None:
        """The DeepSeek in-flight limit now (shadow included), or None when no fresh measurement backs one."""
        return self.inflight.current_limit()

    def inflight_above(self, ticket: Ticket) -> bool | None:
        """Whether a DeepSeek request sent now is above the cap (its own flight counted), or None when no cap is computed."""
        if family_of(ticket.model) != DS_FAMILY:
            return None
        limit = self.inflight_limit()
        return None if limit is None else self.ds_flight > limit

    def inflight_full(self, ticket: Ticket) -> bool:
        """True when this ticket is DeepSeek and admitting it would exceed the in-flight cap."""
        if family_of(ticket.model) != DS_FAMILY:
            return False
        limit = self.inflight_limit()
        return limit is not None and self.ds_flight >= limit

    def enter_flight(self, ticket: Ticket) -> None:
        if family_of(ticket.model) == DS_FAMILY and not ticket.flight:
            ticket.flight = True
            self.ds_flight += 1

    def leave_flight(self, ticket: Ticket) -> None:
        """End one DeepSeek admission. Safe to call more than once for the same ticket."""
        if ticket.flight:
            ticket.flight = False
            self.ds_flight -= 1

    def adjust_inflight(self) -> None:
        if self.inflight.adjust(self.clock()):
            self.log(
                "inflight_limit",
                limit=self.inflight_limit(),
                short_ms=ms_or_none(self.inflight.short_run_s(self.clock())),
                baseline_ms=ms_or_none(self.inflight.baseline_s),
            )

    def _admit(self, ticket: Ticket, now: float) -> None:
        ticket.holds = []
        ticket.blocked_by = None
        self.enter_flight(ticket)
        wall = self.wall()
        ticket.admitted_minute = minute_stamp(wall)
        for name, estimate in ticket.estimates.items():
            budget = self.budgets[name]
            entry = Entry(at=now, amount=estimate, lane=ticket.lane, wall_at=wall)
            budget.entries.append(entry)
            ticket.holds.append((budget, entry))
            self.sync_reserve(budget)
        self.note_calib_admitted(ticket)
        wait = now - ticket.queued_at
        if not ticket.reserve_seen:
            self.waits.append((now, wait))
        row = self.row(ticket.model)
        row["req"] += 1
        if len(row["waits"]) < ROW_WAIT_SAMPLES:
            row["waits"].append(wait)
        counters = self.job_counters(ticket)
        counters["req"] += 1
        counters["wait_ms"] += round((now - ticket.waiting_since) * 1000)
        if ticket.future is not None and not ticket.future.done():
            ticket.future.set_result(None)

    def finish_ok(
        self,
        ticket: Ticket,
        usage: dict | None,
        upstream_s: float | None = None,
        ttfb_s: float | None = None,
    ) -> None:
        """Replace the estimates with real usage. Without usage the estimate stays, which errs on the safe side.

        upstream_s and ttfb_s, when given, are this request's latency; they are recorded in the minute it completes.
        """
        now = self.clock()
        prompt = cached = output = 0
        real: dict[str, int] = {}
        if usage:
            prompt, output, cached = usage_fields(usage)
            real = {"total": prompt + output, "output": output, "uncached": prompt - cached, "requests": 1}
        for budget, entry in ticket.holds:
            if budget.charge_basis == BILLED:
                entry.amount = real.get(budget.limit_type, entry.amount)
            entry.done = True
            entry.at = now
            self.sync_reserve(budget)
        ticket.holds = []
        if usage and ticket.uncached_est > 0:
            self.learn_uncached(ticket.model, ticket.uncached_est, prompt - cached)
        self.note_calib_billed(ticket, prompt + output)
        if output > 0:
            self.output_history.setdefault(ticket.model, deque(maxlen=20)).append(output)
        row = self.row(ticket.model)
        row["ok"] += 1
        row["input"] += prompt - cached
        row["cache_read"] += cached
        row["output"] += output
        row["uncached_est"] += ticket.uncached_est
        counters = self.job_counters(ticket)
        counters["ok"] += 1
        counters["input"] += prompt - cached
        counters["cache_read"] += cached
        counters["output"] += output
        if upstream_s is not None and ttfb_s is not None:
            self.note_latency(ticket, upstream_s, ttfb_s)
            if family_of(ticket.model) == DS_FAMILY:
                self.inflight.observe(ttfb_s, self.clock())

    def note_latency(self, ticket: Ticket, upstream_s: float, ttfb_s: float) -> None:
        """Record one successful request's upstream time and time to first byte in the minute it completes."""
        row = self.row(ticket.model)
        upstream_ms = round(upstream_s * 1000)
        row["upstream_ms_sum"] += upstream_ms
        row["upstream_n"] += 1
        if len(row["upstream_samples"]) < ROW_WAIT_SAMPLES:
            row["upstream_samples"].append(upstream_ms)
        if len(row["ttfb_samples"]) < ROW_WAIT_SAMPLES:
            row["ttfb_samples"].append(round(ttfb_s * 1000))
        if ticket.started_above is not None:
            split = "ttfb_gt_samples" if ticket.started_above else "ttfb_le_samples"
            if len(row[split]) < ROW_WAIT_SAMPLES:
                row[split].append(round(ttfb_s * 1000))
        now = self.clock()
        self.upstream_recent.append((now, ticket.model, upstream_s))
        while self.upstream_recent and self.upstream_recent[0][0] < now - UPSTREAM_RECENT_S:
            self.upstream_recent.popleft()

    def upstream_started(self, ticket: Ticket) -> None:
        """Count one upstream request now in flight for its model. The minute's peak is the most seen at any start."""
        model = ticket.model
        self.in_flight[model] = self.in_flight.get(model, 0) + 1
        row = self.row(model)
        row["in_flight_max"] = max(row["in_flight_max"], self.in_flight[model])

    def upstream_finished(self, ticket: Ticket) -> None:
        """Count one upstream request that has ended, whatever its outcome."""
        self.leave_flight(ticket)
        remaining = self.in_flight.get(ticket.model, 0) - 1
        if remaining > 0:
            self.in_flight[ticket.model] = remaining
        else:
            self.in_flight.pop(ticket.model, None)

    def release(self, ticket: Ticket) -> None:
        """Drop a charge that upstream did not bill: a 429, an error, a truncated stream, or a client that left."""
        now = self.clock()
        for budget, entry in ticket.holds:
            entry.amount = 0
            entry.done = True
            entry.at = now
            self.sync_reserve(budget)
        ticket.holds = []

    def budgets_for_limit(self, limit_type: str, model: str) -> list[Budget]:
        """The budgets that track a stated limit type and apply to the model that was refused."""
        return [b for b in self.budgets.values() if b.limit_type == limit_type and b.applies(model)]

    def learn_from_429(self, ticket: Ticket, body_text: str) -> None:
        """Adopt a stated limit when the body names one; cut the target of the budgets it names.

        A stated limit goes only to budgets with its limit type that apply to the ticket's model,
        so one model's 429 never rewrites another model's limit. A body that names no usable limit
        cuts every budget the request touched. service_overloaded is a platform-wide signal, so it
        cuts the total budget only. A budget is cut at most once per fast_interval_s: a burst of 429s
        is one cut, and each 429 still counts and refreshes last_429.
        """
        now = self.clock()
        self.row(ticket.model)["r429"] += 1
        self.job_counters(ticket)["r429"] += 1
        stated = parse_stated_limit(body_text)
        named = self.budgets_for_limit(stated[0], ticket.model) if stated is not None else []
        self.note_calib_429(body_text, [b.name for b in named])
        if named:
            limit = stated[1]
            for budget in named:
                if budget.limit != limit:
                    self.log("limit_adopted", budget=budget.name, old=budget.limit, new=limit)
                    budget.limit = limit
            targets = named
        elif "service_overloaded" in body_text:
            targets = [self.budgets["total"]] if "total" in self.budgets else []
        else:
            targets = [self.budgets[name] for name in ticket.estimates]
        for budget in named:
            if budget.reserve is not None:
                budget.reserve.empty()
        if named:
            ticket.backoff_by = named[0].name
        elif "service_overloaded" in body_text and "total" in self.budgets:
            ticket.backoff_by = "total"
        else:
            ticket.backoff_by = None
        floor = float(self.config["target_floor"])
        cut = float(self.config["target_cut"])
        pace = float(self.config.get("fast_interval_s", 10.0))
        for budget in targets:
            budget.last_429 = now
            if now - budget.last_cut < pace:
                continue
            budget.last_cut = now
            budget.target = max(floor, budget.target * cut)
            budget.fast_limit = budget.target
            self.log("target_cut", budget=budget.name, target=round(budget.target, 4), limit=budget.limit)
        self.state_dirty = True

    def retry_delay(self, ticket: Ticket, now: float) -> float:
        """Seconds to wait before retrying a refused ticket. A rolling budget waits until enough
        completed charges age out. A calendar budget that is full waits for the next UTC :00, and the
        jitter is the calendar one."""
        wait = 0.0
        jitter = float(self.config["retry_jitter_s"])
        for name, estimate in ticket.estimates.items():
            budget = self.budgets[name]
            used = self._used(budget)
            if budget.reserve is not None:
                self.sync_reserve(budget)
                overflow = budget.reserve.admission_overflow(
                    used, estimate, budget.cap(), minute_stamp(self.wall())
                )
                if overflow > 0:
                    if ticket.lane == "backlog" and used > 0:
                        wait = max(wait, seconds_to_next_minute(self.wall()))
                        jitter = float(self.config["calendar_jitter_s"])
                    else:
                        wait = max(wait, budget.reserve.seconds_until_covered(overflow))
                continue
            if budget.window_mode == CALENDAR:
                if used + estimate > budget.cap():
                    wait = max(wait, seconds_to_next_minute(self.wall()))
                    jitter = float(self.config["calendar_jitter_s"])
                continue
            needed = used + estimate - budget.cap()
            wait = max(wait, budget.seconds_until_free(needed, now, self.window))
        floor = float(self.config["retry_floor_s"])
        return max(wait, floor) + random.uniform(0.0, jitter)

    def tick(self) -> None:
        """Raise a target while its budget is saturated and no 429 has come recently.

        Below the fast cap the target climbs by fast_step_up every fast_interval_s. The fast cap is
        fast_until, or the budget's fast_limit (the target a 429 cut left) when that is lower. Above
        the fast cap the target climbs by target_step_up once per full window, up to target_ceiling.
        A budget with no 429 for FAST_LIMIT_QUIET_S loses its fast_limit. Saturation counts if the
        current pump or any pump since the last tick marked the budget.
        """
        now = self.clock()
        window = self.window
        step = float(self.config["target_step_up"])
        ceiling = float(self.config.get("target_ceiling", 1.0))
        fast_until = float(self.config.get("fast_until", 1.5))
        fast_step = float(self.config.get("fast_step_up", 0.05))
        fast_interval = float(self.config.get("fast_interval_s", 10.0))
        for budget in self.budgets.values():
            demanded = budget.saturated or budget.saturated_since_tick
            budget.saturated_since_tick = False
            budget_ceiling = min(ceiling, RESERVE_TARGET_CEILING) if budget.reserve is not None else ceiling
            if budget.fast_limit is not None and now - budget.last_429 >= FAST_LIMIT_QUIET_S:
                self.log("fast_limit_cleared", budget=budget.name, fast_limit=round(budget.fast_limit, 4))
                budget.fast_limit = None
                self.state_dirty = True
            if not demanded:
                continue
            fast_cap = fast_until if budget.fast_limit is None else min(fast_until, budget.fast_limit)
            fast_cap = min(fast_cap, budget_ceiling)
            if budget.target < fast_cap:
                if now - budget.last_429 >= fast_interval and now - budget.last_grow >= fast_interval:
                    budget.target = min(fast_cap, budget.target + fast_step)
                    budget.last_grow = now
                    self.state_dirty = True
                    self.log("target_raised", budget=budget.name, target=round(budget.target, 4))
                continue
            quiet = now - budget.last_429 >= window and now - budget.last_grow >= window
            if budget.target < budget_ceiling and quiet:
                budget.target = min(budget_ceiling, budget.target + step)
                budget.last_grow = now
                self.state_dirty = True
                self.log("target_raised", budget=budget.name, target=round(budget.target, 4))

    def row(self, model: str) -> dict:
        """The in-memory counters for this model's current minute. The first use of a minute in this
        process seeds them from the stored row, because the flush REPLACEs that row.

        With a seeder (the proxy), the seed is read off the event loop: counting starts at once, the
        seed is merged when it lands, and the minute is not flushed until then."""
        key = (minute_stamp(self.wall()), model)
        row = self.minute_rows.get(key)
        if row is None:
            row = empty_minute_row()
            row["in_flight_max"] = self.in_flight.get(model, 0)
            self.minute_rows[key] = row
            if self.seeder is None:
                merge_row_seed(row, read_minute_counts(self.db_path, key[0], model))
            else:
                self.request_seed(("row", key))
        self.dirty.add(key)
        return row

    def request_seed(self, pending: tuple[str, object]) -> None:
        self.pending_seeds.add(pending)
        self.seeder(*pending)

    def apply_row_seed(self, key: tuple[str, str], seed: dict | None) -> None:
        self.pending_seeds.discard(("row", key))
        row = self.minute_rows.get(key)
        if row is not None:
            merge_row_seed(row, seed)

    def apply_calib_seed(self, minute: str, seed: dict | None) -> None:
        self.pending_seeds.discard(("calib", minute))
        record = self.calib.get(minute)
        if record is not None:
            merge_calib_seed(record, seed)

    def job_counters(self, ticket: Ticket) -> dict:
        """The per-run counters for one (job, model), created on first use. Anonymous requests count
        under job "none". The first and last times only ever widen, so re-sending them is harmless."""
        key = (ticket.job or "none", ticket.model)
        row = self.job_rows.get(key)
        if row is None:
            row = {name: 0 for name in JOB_COUNTERS} | {"first": None, "last": None}
            self.job_rows[key] = row
        now = self.wall()
        row["first"] = now if row["first"] is None else min(row["first"], now)
        row["last"] = now if row["last"] is None else max(row["last"], now)
        return row

    def take_job_rows(self) -> tuple[list[tuple], dict]:
        """Rows of per-run deltas to add to proxy_jobs, plus the counts they carry. Nothing is cleared
        here: settle_job_rows subtracts the counts once the write has succeeded."""
        rows = []
        snapshot = {}
        for key, row in self.job_rows.items():
            counts = {name: row[name] for name in JOB_COUNTERS}
            if not any(counts.values()):
                continue
            snapshot[key] = counts
            job, model = key
            rows.append(
                (
                    job,
                    model,
                    row["first"],
                    row["last"],
                    counts["req"],
                    counts["ok"],
                    counts["r429"],
                    counts["input"],
                    counts["cache_read"],
                    counts["output"],
                    counts["wait_ms"],
                    counts["upstream_ms"],
                )
            )
        return rows, snapshot

    def settle_job_rows(self, snapshot: dict) -> None:
        """Remove the counts a successful write carried. Events that arrived during the write stay."""
        for key, counts in snapshot.items():
            row = self.job_rows.get(key)
            if row is None:
                continue
            for name, value in counts.items():
                row[name] -= value
            if not any(row[name] for name in JOB_COUNTERS):
                del self.job_rows[key]

    def note_upstream(self, ticket: Ticket, seconds: float) -> None:
        self.job_counters(ticket)["upstream_ms"] += round(seconds * 1000)

    def take_minute_rows(self) -> list[tuple]:
        """Return the rows changed since the last call, ready for write_minute_rows."""
        rows = []
        for key in self.dirty:
            row = self.minute_rows.get(key)
            if row is None or ("row", key) in self.pending_seeds:
                continue
            minute, model = key
            waits = sorted(row["waits"])
            upstream = row["upstream_samples"]
            ttfb = row["ttfb_samples"]
            target = min((b.target for b in self.budgets.values() if b.applies(model)), default=0.0)
            rows.append(
                (
                    minute,
                    model,
                    row["req"],
                    row["ok"],
                    row["r429"],
                    row["input"],
                    row["cache_read"],
                    row["output"],
                    row["uncached_est"],
                    round(percentile(waits, 0.5) * 1000),
                    round(percentile(waits, 0.95) * 1000),
                    target,
                    row["upstream_ms_sum"],
                    row["upstream_n"],
                    sample_percentile_ms(upstream, 0.5),
                    sample_percentile_ms(upstream, 0.95),
                    sample_percentile_ms(ttfb, 0.5),
                    row["in_flight_max"],
                    *self.inflight_columns(model, minute),
                    row["inflight_over"],
                    sample_percentile_ms(row["ttfb_le_samples"], 0.5),
                    sample_percentile_ms(row["ttfb_gt_samples"], 0.5),
                )
            )
        self.dirty = {key for key in self.dirty if ("row", key) in self.pending_seeds}
        cutoff = minute_stamp(self.wall() - 120)
        for key in [k for k in self.minute_rows if k[0] < cutoff and ("row", k) not in self.pending_seeds]:
            del self.minute_rows[key]
        return rows

    def inflight_minute_row(self) -> tuple:
        """This calendar minute's limiter record, written whether or not any DeepSeek request arrived."""
        now = self.wall()
        return (
            minute_stamp(now),
            self.inflight.mode,
            self.inflight.arm_at(now),
            self.inflight_limit(),
            int(self.inflight.enforcing(now)),
        )

    def inflight_columns(self, model: str, minute: str) -> tuple:
        """The limit, mode and arm recorded on a row. DeepSeek rows only; other models record nothing."""
        if family_of(model) != DS_FAMILY:
            return (None, None, None)
        return (self.inflight_limit(), self.inflight.mode, self.inflight.arm_at(minute_epoch(minute)))

    def calib_record(self, minute: str) -> dict:
        """The calibration counters for one UTC minute, seeded from proxy_calib on first use in this process.
        With a seeder the seed is merged when it lands, as row() does."""
        record = self.calib.get(minute)
        if record is None:
            record = {"overall": {name: 0 for name in CALIB_FIELDS}, "families": {}, "first_429_s": None, "r429": 0}
            self.calib[minute] = record
            if self.seeder is None:
                merge_calib_seed(record, read_calib_counts(self.db_path, minute))
            else:
                self.request_seed(("calib", minute))
        self.calib_dirty.add(minute)
        return record

    def _calib_buckets(self, minute: str, model: str) -> tuple[dict, dict]:
        """The overall and per-family counters for a minute and model."""
        record = self.calib_record(minute)
        family = record["families"].setdefault(model_family(model), {name: 0 for name in CALIB_FIELDS})
        return record["overall"], family

    def note_calib_admitted(self, ticket: Ticket) -> None:
        """Count one admitted request: its prompt estimate and its capped requested max_tokens."""
        overall, family = self._calib_buckets(ticket.admitted_minute, ticket.model)
        for bucket in (overall, family):
            bucket["req"] += 1
            bucket["prompt_est"] += ticket.prompt_est
            bucket["max_tokens"] += ticket.requested

    def note_calib_billed(self, ticket: Ticket, billed: int) -> None:
        """Add a completed request's real prompt plus completion tokens to the minute it was admitted in."""
        if not ticket.admitted_minute:
            return
        overall, family = self._calib_buckets(ticket.admitted_minute, ticket.model)
        overall["billed"] += billed
        family["billed"] += billed

    def note_calib_429(self, body_text: str, budget_names: list[str]) -> None:
        """Log one upstream 429 with the current minute so far: where in the minute it fell, the budgets
        the body's stated limit was applied to, and the admitted and billed totals for this minute."""
        now = self.wall()
        minute = minute_stamp(now)
        record = self.calib_record(minute)
        second = round(seconds_into_minute(now), 2)
        record["r429"] += 1
        if record["first_429_s"] is None:
            record["first_429_s"] = second
        stated = parse_stated_limit(body_text)
        overall = record["overall"]
        self.log(
            "calib",
            minute=minute,
            limit_type=stated[0] if stated else None,
            budget=",".join(budget_names) or None,
            sec=second,
            first_429_s=record["first_429_s"],
            r429=record["r429"],
            req=overall["req"],
            prompt_est=overall["prompt_est"],
            max_tokens=overall["max_tokens"],
            prompt_max=overall["prompt_est"] + overall["max_tokens"],
            billed=overall["billed"],
            families=json.dumps(record["families"], separators=(",", ":"), sort_keys=True),
        )

    def take_calib_rows(self) -> list[tuple]:
        """Rows of the minutes changed since the last call, ready for write_calib_rows. Minutes older than
        CALIB_KEEP_S are forgotten; a late charge for one reseeds it from proxy_calib."""
        rows = []
        for minute in self.calib_dirty:
            record = self.calib.get(minute)
            if record is None or ("calib", minute) in self.pending_seeds:
                continue
            overall = record["overall"]
            rows.append(
                (
                    minute,
                    overall["req"],
                    overall["prompt_est"],
                    overall["max_tokens"],
                    overall["billed"],
                    record["first_429_s"],
                    record["r429"],
                )
            )
        self.calib_dirty = {minute for minute in self.calib_dirty if ("calib", minute) in self.pending_seeds}
        cutoff = minute_stamp(self.wall() - CALIB_KEEP_S)
        for minute in [m for m in self.calib if m < cutoff and ("calib", m) not in self.pending_seeds]:
            del self.calib[minute]
        return rows

    def upstream_status(self, now: float) -> dict:
        """Upstream requests in flight now per model, and the last minute's p50 and p95 upstream time per model."""
        while self.upstream_recent and self.upstream_recent[0][0] < now - UPSTREAM_RECENT_S:
            self.upstream_recent.popleft()
        by_model: dict[str, list[int]] = {}
        for _, model, upstream_s in self.upstream_recent:
            by_model.setdefault(model, []).append(round(upstream_s * 1000))
        last = {
            model: {
                "n": len(values),
                "p50_ms": sample_percentile_ms(values, 0.5),
                "p95_ms": sample_percentile_ms(values, 0.95),
            }
            for model, values in by_model.items()
        }
        return {"in_flight": dict(self.in_flight), "last_60s": last}

    def status(self) -> dict:
        now = self.clock()
        window = self.window
        while self.waits and self.waits[0][0] < now - window:
            self.waits.popleft()
        queued_ages = [
            now - t.queued_at
            for lane in LANES
            for t in self.queues[lane]
            if lane != "backlog" or t.last_refusal in (CAPACITY, GUARD)
        ]
        saturated = any(wait > SATURATION_WAIT_S for _, wait in self.waits) or any(
            age > SATURATION_WAIT_S for age in queued_ages
        )
        budgets = {}
        for name, budget in self.budgets.items():
            self.sync_reserve(budget)
            per_lane = {lane: sum(1 for t in self.queues[lane] if name in t.estimates) for lane in LANES}
            queued_by_family = dict.fromkeys(FAMILY_KEYS, 0)
            blocked_by_family = dict.fromkeys(FAMILY_KEYS, 0)
            for lane in LANES:
                for ticket in self.queues[lane]:
                    if name not in ticket.estimates:
                        continue
                    family = family_of(ticket.model)
                    queued_by_family[family] += 1
                    if ticket.blocking_budget(now) == name:
                        blocked_by_family[family] += 1
            budgets[name] = {
                "limit": budget.limit,
                "target_frac": round(budget.target, 4),
                "used_60s": self._used(budget),
                "free": round(budget.cap()),
                "queued": per_lane,
                "queued_by_family": queued_by_family,
                "blocked_by_family": blocked_by_family,
                "saturated": budget.saturated,
                "limit_type": budget.limit_type,
                "window_mode": budget.window_mode,
                "charge_basis": budget.charge_basis,
            }
            if budget.window_mode == CALENDAR:
                budgets[name]["seconds_to_reset"] = round(seconds_to_next_minute(self.wall()), 2)
            if budget.reserve is not None:
                budgets[name]["reserve"] = {
                    "level": round(budget.reserve.level),
                    "capacity": round(budget.reserve.capacity),
                    "refill_per_min": round(budget.reserve.refill_per_s * MINUTE_S),
                    "drained_60s": round(budget.reserve.drained.get(minute_stamp(self.wall()), 0.0)),
                }
        inflight_queued = sum(
            1
            for lane in LANES
            for t in self.queues[lane]
            if t.last_refusal == INFLIGHT
        )
        return {
            "ok": True,
            "window_s": window,
            "saturated": saturated,
            "queued": {lane: len(self.queues[lane]) for lane in LANES},
            "errors": dict(self.errors),
            "budgets": budgets,
            "uncached_estimate": {
                family: {"ratio": round(learned["ratio"], 4), "samples": learned["samples"]}
                for family, learned in self.uncached_ratio.items()
            },
            "upstream": self.upstream_status(now),
            "loop_lag_ms": self.loop_lag_status(),
            "inflight_limit": self.inflight_limit(),
            "inflight_max": self.inflight.max_limit if self.inflight.measuring else None,
            "inflight_mode": self.inflight.mode,
            "inflight_arm": self.inflight.arm_at(self.wall()),
            "inflight_enforcing": self.inflight.enforcing(self.wall()),
            "inflight_in_flight": self.ds_flight,
            "inflight_queued": inflight_queued,
            "ttfb_baseline_ms": ms_or_none(self.inflight.baseline_s),
            "ttfb_short_ms": ms_or_none(self.inflight.short_run_s(now)),
        }


@dataclass(eq=False)
class Client:
    writer: asyncio.StreamWriter | None
    started: bool = False
    aborted: bool = False


def formatted_stack(frame) -> str:
    """One line per frame, outermost first, joined with ' | '. Newlines never appear in the result."""
    if frame is None:
        return "unavailable"
    lines = [line.strip() for block in traceback.format_stack(frame) for line in block.splitlines()]
    return " | ".join(line for line in lines if line)


class StallWatchdog(threading.Thread):
    def __init__(
        self,
        *,
        loop_thread_id: int,
        dump_s: float,
        log: Callable[..., None],
        clock: Callable[[], float],
        interval_s: float = 0.25,
    ) -> None:
        super().__init__(name="agentq-watchdog", daemon=True)
        self.loop_thread_id = loop_thread_id
        self.dump_s = dump_s
        self.log = log
        self.clock = clock
        self.interval_s = interval_s
        self.beat_at = clock()
        self.episodes = 0
        self._episode_open = False
        self._halt = threading.Event()

    def beat(self) -> None:
        self.beat_at = self.clock()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.wait(self.interval_s):
            self.check()

    def check(self) -> None:
        """One look at the loop. Writes a stall line the first time a stall is seen; no-op otherwise."""
        lag = self.clock() - self.beat_at
        if lag <= self.dump_s:
            self._episode_open = False
            return
        if self._episode_open:
            return
        self._episode_open = True
        self.episodes += 1
        frame = sys._current_frames().get(self.loop_thread_id)
        self.log("loop_stall", ms=round(lag * 1000), stack=formatted_stack(frame))


class UpstreamPool:
    """Idle keep-alive connections to the upstream: bounded, expiring, never shared while in use.

    A connection goes back only after a complete 200 response with length or chunked framing, and only
    when its body was read to the end. Anything else is closed by the caller.
    """

    def __init__(self, max_idle: int, idle_s: float, clock=time.monotonic) -> None:
        self.max_idle = int(max_idle)
        self.idle_s = float(idle_s)
        self.clock = clock
        self.idle: deque[tuple[float, str, asyncio.StreamReader, asyncio.StreamWriter]] = deque()

    @staticmethod
    def usable(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        return not writer.is_closing() and not reader.at_eof()

    def prune(self) -> None:
        """Close and drop every idle connection that expired or is no longer usable, wherever it sits."""
        now = self.clock()
        kept: deque[tuple[float, str, asyncio.StreamReader, asyncio.StreamWriter]] = deque()
        while self.idle:
            entry = self.idle.popleft()
            stamp, _, reader, writer = entry
            if now - stamp > self.idle_s or not self.usable(reader, writer):
                writer.close()
            else:
                kept.append(entry)
        self.idle = kept

    def take(self, key: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """An idle connection to this upstream, most recently used first, or None. Never waits."""
        self.prune()
        for index in range(len(self.idle) - 1, -1, -1):
            _, host, reader, writer = self.idle[index]
            if host == key and self.usable(reader, writer):
                del self.idle[index]
                return reader, writer
        return None

    def give(self, key: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.max_idle <= 0 or not self.usable(reader, writer):
            writer.close()
            return
        self.prune()
        while len(self.idle) >= self.max_idle:
            _, _, _, old = self.idle.popleft()
            old.close()
        self.idle.append((self.clock(), key, reader, writer))

    def close_all(self) -> None:
        while self.idle:
            _, _, _, writer = self.idle.popleft()
            writer.close()


def keep_alive_framed(status: int, status_line: bytes, headers: dict[str, str]) -> bool:
    """Whether a response leaves its connection usable: a 200 over HTTP/1.1 with a declared end and no close."""
    if status != 200 or not status_line.startswith(b"HTTP/1.1"):
        return False
    if "close" in headers.get("connection", "").lower():
        return False
    return "chunked" in headers.get("transfer-encoding", "").lower() or "content-length" in headers


@dataclass(eq=False)
class Upstream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    status: int
    headers: dict[str, str]
    key: str = ""
    reusable: bool = False
    pool: UpstreamPool | None = None
    released: bool = False

    def release(self) -> None:
        """Hand the connection back to the pool after its body was read to the end, or close it."""
        if self.reusable and self.pool is not None and not self.released:
            self.released = True
            self.pool.give(self.key, self.reader, self.writer)
        else:
            self.close()

    def close(self) -> None:
        if not self.released:
            self.released = True
            self.writer.close()


class UsageScanner:
    """Finds the last usage object in an SSE stream, or in a whole JSON body, without buffering the stream."""

    def __init__(self, streaming: bool) -> None:
        self.streaming = streaming
        self.pending = bytearray()
        self.whole = bytearray()
        self.usage: dict | None = None

    def feed(self, chunk: bytes) -> None:
        if not self.streaming:
            if len(self.whole) < MAX_WHOLE_BODY:
                self.whole += chunk
            return
        self.pending += chunk
        while True:
            end = self.pending.find(b"\n")
            if end < 0:
                return
            line = bytes(self.pending[:end])
            del self.pending[: end + 1]
            self._scan_line(line)

    def finish(self) -> None:
        if self.streaming:
            if self.pending:
                self._scan_line(bytes(self.pending))
                self.pending.clear()
            return
        try:
            payload = json.loads(bytes(self.whole))
        except ValueError:
            return
        self._take(payload)

    def _scan_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            payload = json.loads(data)
        except ValueError:
            return
        self._take(payload)

    def _take(self, payload: object) -> None:
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            self.usage = payload["usage"]


def parse_payload(body: bytes) -> dict | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def dump_payload(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def with_usage_option(payload: dict) -> bytes | None:
    """Return the body with stream_options.include_usage set, or None when the client already set it."""
    options = payload.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    if options.get("include_usage") is not None:
        return None
    options["include_usage"] = True
    payload["stream_options"] = options
    return dump_payload(payload)


def max_tokens_cap_for(model: str, caps: dict) -> int | None:
    """The cap of the longest configured prefix that the model name starts with, or None."""
    matches = [prefix for prefix in caps if model.startswith(prefix)]
    if not matches:
        return None
    return int(caps[max(matches, key=len)])


def cap_max_tokens(payload: dict, caps: dict) -> bool:
    """Lower max_tokens and max_completion_tokens to the model's cap when they are above it.
    Returns whether the payload changed."""
    cap = max_tokens_cap_for(str(payload.get("model") or ""), caps)
    if cap is None:
        return False
    changed = False
    for name in ("max_tokens", "max_completion_tokens"):
        value = payload.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > cap:
            payload[name] = cap
            changed = True
    return changed


def prepare_body(body: bytes, caps: dict | None = None) -> tuple[dict | None, bytes, int, tuple[bytes, ...]]:
    """Parse the request, apply the max_tokens cap, set include_usage on streams, and describe the prompt.
    Pure, so it can run in a thread."""
    payload = parse_payload(body)
    out_body = body
    if payload is not None:
        capped = cap_max_tokens(payload, caps or {})
        with_usage = with_usage_option(payload) if payload.get("stream") is True else None
        if with_usage is not None:
            out_body = with_usage
        elif capped:
            out_body = dump_payload(payload)
    text = out_body.decode("utf-8", "replace")
    return payload, out_body, len(text), prefix_blocks(text)


async def prepare_body_async(body: bytes, caps: dict | None = None) -> tuple[dict | None, bytes, int, tuple[bytes, ...]]:
    if len(body) < LARGE_BODY:
        return prepare_body(body, caps)
    return await asyncio.to_thread(prepare_body, body, caps)


def is_event_stream(headers: dict[str, str]) -> bool:
    return "text/event-stream" in headers.get("content-type", "")


async def bounded(awaitable, idle_s: float | None):
    """Wait for one read, at most idle_s seconds. None means no bound."""
    if idle_s is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, idle_s)


async def read_headers(reader: asyncio.StreamReader, idle_s: float | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await bounded(reader.readline(), idle_s)
        if line in (b"\r\n", b"\n", b""):
            return headers
        name, separator, value = line.decode("latin-1").partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()


async def iter_body(reader: asyncio.StreamReader, headers: dict[str, str], idle_s: float | None = None):
    """Yield body bytes as they arrive: chunked, length-delimited, or read-to-close.

    A chunked or length-delimited body that ends early raises TruncatedBody. Read-to-close
    ends at EOF by definition.
    """
    if "chunked" in headers.get("transfer-encoding", "").lower():
        while True:
            size_line = await bounded(reader.readline(), idle_s)
            if not size_line:
                raise TruncatedBody("chunk size missing")
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError as exc:
                raise UpstreamError("bad_chunk") from exc
            if size == 0:
                await read_headers(reader, idle_s)
                return
            try:
                data = await bounded(reader.readexactly(size), idle_s)
                await bounded(reader.readexactly(2), idle_s)
            except asyncio.IncompleteReadError as exc:
                raise TruncatedBody("chunk cut short") from exc
            yield data
    elif "content-length" in headers:
        remaining = int(headers["content-length"])
        while remaining > 0:
            data = await bounded(reader.read(min(READ_CHUNK, remaining)), idle_s)
            if not data:
                raise TruncatedBody("body ended before content-length")
            remaining -= len(data)
            yield data
    else:
        while True:
            data = await bounded(reader.read(READ_CHUNK), idle_s)
            if not data:
                return
            yield data


async def read_all(reader: asyncio.StreamReader, headers: dict[str, str], cap: int, idle_s: float | None = None) -> bytes:
    data = bytearray()
    async for chunk in iter_body(reader, headers, idle_s):
        if len(data) + len(chunk) > cap:
            data += chunk[: max(cap - len(data), 0)]
            break
        data += chunk
    return bytes(data)


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes] | None:
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").strip().split(" ")
    if len(parts) < 2:
        raise ValueError("malformed request line")
    headers = await read_headers(reader)
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = bytearray()
        async for chunk in iter_body(reader, headers):
            body += chunk
            if len(body) > MAX_REQUEST_BODY:
                raise ValueError("request body too large")
        body_bytes = bytes(body)
    else:
        length = int(headers.get("content-length") or 0)
        if length > MAX_REQUEST_BODY:
            raise ValueError("request body too large")
        body_bytes = await reader.readexactly(length) if length else b""
    return parts[0], parts[1].split("?", 1)[0], headers, body_bytes


def parse_status_line(line: bytes) -> int:
    parts = line.decode("latin-1").split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise UpstreamError("bad_status_line")
    return int(parts[1])


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Upstream"


async def send_head(client: Client, status: int, headers: dict[str, str]) -> None:
    lines = [f"HTTP/1.1 {status} {_reason(status)}"]
    for name, value in headers.items():
        if name not in RESPONSE_DROP:
            lines.append(f"{name.title()}: {value}")
    lines += ["Transfer-Encoding: chunked", "Connection: close"]
    client.started = True
    client.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
    await client.writer.drain()


async def send_chunk(client: Client, data: bytes) -> None:
    client.writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
    await client.writer.drain()


async def end_chunks(client: Client) -> None:
    client.writer.write(b"0\r\n\r\n")
    await client.writer.drain()


async def send_simple(client: Client, status: int, body: bytes, content_type: str = "application/json") -> None:
    lines = [
        f"HTTP/1.1 {status} {_reason(status)}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    client.started = True
    client.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await client.writer.drain()


def held_body(governor: Governor) -> bytes:
    """The 429 body a client sees when its request was held past give_up_s."""
    seconds = float(governor.config["give_up_s"])
    return f"agentq: held {seconds:g} s, budget full".encode()


class Proxy:
    """HTTP front end: admits requests through the Governor, forwards them, and retries 429s."""

    def __init__(self, governor: Governor, upstream_url: str, key: str) -> None:
        self.gov = governor
        self.upstream_url = upstream_url
        self.key = key
        scheme = urlsplit(upstream_url).scheme
        self.ssl_context = ssl.create_default_context() if scheme == "https" else None
        self.maintenance: asyncio.Task | None = None
        self.active: set[asyncio.Task] = set()
        self.flush_task: asyncio.Task | None = None
        self.flush_lock = asyncio.Lock()
        self.watchdog: StallWatchdog | None = None
        self.seed_tasks: set[asyncio.Task] = set()
        self.closed = False
        self.pool = UpstreamPool(governor.config["upstream_pool_max"], governor.config["upstream_pool_idle_s"])
        governor.seeder = self._start_seed
        self.db_executor = ThreadPoolExecutor(max_workers=DB_WORKERS, thread_name_prefix="agentq-db")

    async def run_db(self, fn, *args, executor: ThreadPoolExecutor | None = None, **kwargs):
        """Run one store or state write on the dedicated writer threads, never on the default executor the
        request path uses for lane lookups and body parsing. Shutdown passes its own executor."""
        call = functools.partial(fn, *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(executor or self.db_executor, call)

    def _start_seed(self, kind: str, key: object) -> None:
        """Governor hook: read a stored minute off the loop, on the writer threads."""
        task = asyncio.get_running_loop().create_task(self._seed(kind, key))
        self.seed_tasks.add(task)
        task.add_done_callback(self.seed_tasks.discard)

    async def _seed(self, kind: str, key: object) -> None:
        try:
            if kind == "row":
                minute, model = key
                seed = await self.run_db(read_minute_counts, self.gov.db_path, minute, model)
                self.gov.apply_row_seed(key, seed)
            else:
                seed = await self.run_db(read_calib_counts, self.gov.db_path, key)
                self.gov.apply_calib_seed(key, seed)
        except Exception as exc:
            self.gov.log("seed_error", kind=type(exc).__name__)
            if kind == "row":
                self.gov.apply_row_seed(key, None)
            else:
                self.gov.apply_calib_seed(key, None)

    async def drain_seeds(self, until: float) -> None:
        """Let pending seed reads land until the loop clock reaches ``until``. A seed still pending then is cancelled
        and released as empty: the minute keeps its in-memory counts and loses only the stored seed. A cancelled
        wrapper does not stop a read already running on a writer thread, which is why final writes use their own."""
        timeout = max(0.0, until - asyncio.get_running_loop().time())
        if self.seed_tasks:
            await asyncio.wait(set(self.seed_tasks), timeout=timeout)
        for task in list(self.seed_tasks):
            task.cancel()
        abandoned = list(self.gov.pending_seeds)
        for kind, key in abandoned:
            if kind == "row":
                self.gov.apply_row_seed(key, None)
            else:
                self.gov.apply_calib_seed(key, None)
        if abandoned:
            self.gov.log("seed_timeout", count=len(abandoned))

    def schedule_flush(self) -> None:
        """Start a flush in the background unless one is already running, so no write can hold up admission."""
        if self.flush_task is None or self.flush_task.done():
            self.flush_task = asyncio.create_task(self.flush_all())
            self.flush_task.add_done_callback(self._flush_finished)

    def _flush_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.gov.log("flush_error", kind=type(task.exception()).__name__)

    async def flush_all(self) -> None:
        await self.flush()
        await self.flush_state()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> asyncio.Server:
        self.watchdog = StallWatchdog(
            loop_thread_id=threading.get_ident(),
            dump_s=float(self.gov.config["stall_dump_s"]),
            log=self.gov.log,
            clock=time.monotonic,
        )
        self.watchdog.start()
        self.maintenance = asyncio.create_task(self.maintain())
        return await asyncio.start_server(self.handle, host, port)

    async def drain(self, timeout: float) -> None:
        """Let in-flight connections finish for up to timeout seconds, then cancel what remains."""
        pending = set(self.active)
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        remaining = list(self.active)
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

    async def shutdown(self, shutdown_s: float) -> None:
        """Stop serving and persist, all inside one budget of shutdown_s seconds. In-flight requests get the budget
        minus REQUEST_RESERVE_S; seed reads and then the final writes get what is left, with the final writes
        always keeping FINAL_RESERVE_S."""
        deadline = asyncio.get_running_loop().time() + shutdown_s
        await self.drain(max(0.0, shutdown_s - REQUEST_RESERVE_S))
        await self.close(deadline)

    async def close(self, deadline: float | None = None) -> None:
        """Persist everything and release the threads. ``deadline`` is a loop-clock time; the default is shutdown_s
        from now. Seed reads are drained only up to deadline minus FINAL_RESERVE_S. The final writes run on a
        fresh thread with a short busy timeout, so they never queue behind a seed read that is still running."""
        if self.closed:
            return
        self.closed = True
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + float(self.gov.config["shutdown_s"])
        if self.watchdog is not None:
            self.watchdog.stop()
        await self.drain_seeds(deadline - FINAL_RESERVE_S)
        self.pool.close_all()
        if self.maintenance is not None:
            self.maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.maintenance
        if self.flush_task is not None:
            await asyncio.wait({self.flush_task}, timeout=max(0.0, deadline - FINAL_RESERVE_S - loop.time()))
        await self.persist_final(deadline)
        self.db_executor.shutdown(wait=False, cancel_futures=True)

    async def persist_final(self, deadline: float) -> None:
        """Write the pending counters and the learned state on a new thread, with a short busy timeout, and give up
        at the deadline. A write that is still running then is left to finish on its own thread."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentq-final")
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(self._final_writes(executor), timeout=max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            self.gov.log("final_timeout")
        except Exception as exc:
            self.gov.log("final_error", kind=type(exc).__name__)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _final_writes(self, executor: ThreadPoolExecutor) -> None:
        await self._write_pending(executor=executor, busy_s=FINAL_BUSY_S)
        await self.flush_state(force=True, executor=executor)

    async def maintain(self) -> None:
        loop = asyncio.get_running_loop()
        last_flush = self.gov.clock()
        last_tick = loop.time()
        while True:
            await asyncio.sleep(MAINTAIN_S)
            now = loop.time()
            self.gov.note_loop_lag(now - last_tick - MAINTAIN_S)
            last_tick = now
            if self.watchdog is not None:
                self.watchdog.beat()
            try:
                self.gov.pump()
                self.gov.tick()
                self.gov.adjust_inflight()
                self.pool.prune()
                due = self.gov.clock() - last_flush >= FLUSH_S
                if due:
                    last_flush = self.gov.clock()
                    self.gov.state_dirty = True
                if due or self.gov.state_dirty:
                    self.schedule_flush()
            except Exception as exc:
                self.gov.log("maintain_error", kind=type(exc).__name__)

    async def flush(self) -> None:
        """Write everything the governor has pending. Serialized, so two flushes never take and write the same counters."""
        async with self.flush_lock:
            await self._write_pending()

    async def _write_pending(self, executor: ThreadPoolExecutor | None = None, busy_s: float | None = None) -> None:
        options = {} if busy_s is None else {"busy_s": busy_s}
        rows = self.gov.take_minute_rows()
        if rows:
            try:
                await self.run_db(write_minute_rows, self.gov.db_path, rows, executor=executor, **options)
            except sqlite3.Error as exc:
                self.gov.log("flush_error", kind=type(exc).__name__)
        record = [self.gov.inflight_minute_row()]
        try:
            await self.run_db(write_inflight_minute_rows, self.gov.db_path, record, executor=executor, **options)
        except sqlite3.Error as exc:
            self.gov.log("flush_error", kind=type(exc).__name__)
        calib_rows = self.gov.take_calib_rows()
        if calib_rows:
            try:
                await self.run_db(write_calib_rows, self.gov.db_path, calib_rows, executor=executor, **options)
            except sqlite3.Error as exc:
                self.gov.log("flush_error", kind=type(exc).__name__)
        job_rows, snapshot = self.gov.take_job_rows()
        if not job_rows:
            return
        try:
            await self.run_db(write_job_rows, self.gov.db_path, job_rows, executor=executor, **options)
        except sqlite3.Error as exc:
            self.gov.log("flush_error", kind=type(exc).__name__)
            return
        self.gov.settle_job_rows(snapshot)

    async def flush_state(self, force: bool = False, executor: ThreadPoolExecutor | None = None) -> None:
        if not (self.gov.state_dirty or force):
            return
        snapshot = self.gov.snapshot_state()
        self.gov.state_dirty = False
        try:
            await self.run_db(write_state_file, self.gov.state_path, snapshot, executor=executor)
        except OSError as exc:
            self.gov.state_dirty = True
            self.gov.log("state_error", kind=type(exc).__name__)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(writer)
        task = asyncio.current_task()
        self.active.add(task)
        try:
            request = await read_request(reader)
            if request is None:
                return
            method, path, headers, body = request
            if path == STATUS_PATH and method == "GET":
                await send_simple(client, 200, json.dumps(self.gov.status()).encode())
            elif path.startswith(UPSTREAM_PREFIX + "/"):
                await self.forward(method, path, headers, body, client, reader)
            else:
                await send_simple(client, 404, b'{"error":"not found"}')
        except (ConnectionError, OSError, EOFError):
            return
        except ValueError as exc:
            self.gov.log("bad_request", kind=type(exc).__name__)
            await self._fail(client, 400, b'{"error":"bad request"}')
        except Exception as exc:
            self.gov.log("proxy_error", kind=type(exc).__name__)
            await self._fail(client, 502, b'{"error":"proxy error"}')
        finally:
            self.active.discard(task)
            if client.aborted:
                writer.transport.abort()
            else:
                writer.close()

    async def _fail(self, client: Client, status: int, body: bytes) -> None:
        if client.started:
            return
        with contextlib.suppress(Exception):
            await send_simple(client, status, body)

    async def open_upstream(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Upstream:
        url = urlsplit(self.upstream_url)
        secure = url.scheme == "https"
        port = url.port or (443 if secure else 80)
        connect_s = float(self.gov.config["connect_timeout_s"])
        idle_s = float(self.gov.config["upstream_idle_s"])
        key = self.upstream_url
        pooled = self.pool.take(key)
        if pooled is not None:
            return await self._send(pooled[0], pooled[1], key, method, path, headers, body, url, idle_s)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                url.hostname,
                port,
                ssl=self.ssl_context,
                server_hostname=url.hostname if secure else None,
            ),
            connect_s,
        )
        return await self._send(reader, writer, key, method, path, headers, body, url, idle_s)

    async def _send(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        key: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        url,
        idle_s: float,
    ) -> Upstream:
        try:
            target = url.path.rstrip("/") + path[len(UPSTREAM_PREFIX) :]
            lines = [
                f"{method} {target} HTTP/1.1",
                f"Host: {url.netloc}",
                f"Authorization: Bearer {self.key}",
                f"Content-Length: {len(body)}",
                "Accept-Encoding: identity",
            ]
            for name, value in headers.items():
                if name not in REQUEST_DROP:
                    lines.append(f"{name.title()}: {value}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            await bounded(writer.drain(), idle_s)
            status_line = await bounded(reader.readline(), idle_s)
        except BaseException:
            writer.close()
            raise
        try:
            status = parse_status_line(status_line)
            response_headers = await read_headers(reader, idle_s)
        except BaseException:
            writer.close()
            raise
        reusable = keep_alive_framed(status, status_line, response_headers)
        return Upstream(reader, writer, status, response_headers, key=key, reusable=reusable, pool=self.pool)

    async def wait_turn(self, ticket: Ticket, reader: asyncio.StreamReader) -> str:
        """Block until the ticket is admitted. Returns admitted, gone (client closed) or expired (held too long).

        The deadline runs from the first enqueue, so queue wait and 429 retries share one give-up bound.
        A client that closed by the time it was admitted counts as gone, so nothing is sent for it.
        """
        deadline = ticket.first_seen + float(self.gov.config["give_up_s"])
        watch = asyncio.ensure_future(reader.read(1))
        try:
            while True:
                if ticket.future.done():
                    return "gone" if reader.at_eof() else "admitted"
                remaining = deadline - self.gov.clock()
                if remaining <= 0:
                    return "expired"
                done, _ = await asyncio.wait(
                    {ticket.future, watch},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if watch in done:
                    try:
                        data = watch.result()
                    except (OSError, EOFError):
                        return "gone"
                    if not data:
                        return "gone"
                    watch = asyncio.ensure_future(reader.read(1))
        finally:
            watch.cancel()

    async def forward(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        client: Client,
        reader: asyncio.StreamReader,
    ) -> None:
        gov = self.gov
        job = headers.get("x-agentq-job", "")
        payload, out_body, length, blocks = await prepare_body_async(body, gov.config.get("max_tokens_cap"))
        model = ""
        max_tokens = None
        streaming = False
        if payload is not None:
            model = str(payload.get("model") or "")
            max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
            streaming = payload.get("stream") is True
        lane = gov.cached_lane(job)
        if lane is None:
            lane = await asyncio.to_thread(gov.lane_for, job)
        ticket = gov.make_ticket(
            job=job,
            lane=lane,
            model=model,
            length=length,
            blocks=blocks,
            max_tokens=max_tokens,
            now=gov.clock(),
        )
        ticket.future = asyncio.get_running_loop().create_future()
        gov.enqueue(ticket)
        gov.pump()
        try:
            await self._serve(ticket, reader, client, method, path, headers, out_body, streaming)
        finally:
            if ticket.holds:
                gov.release(ticket)
            gov.leave_flight(ticket)

    async def _serve(
        self,
        ticket: Ticket,
        reader: asyncio.StreamReader,
        client: Client,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        streaming: bool,
    ) -> None:
        """Send the ticket upstream, retrying 429s, and settle its charge on every exit path.

        Every path either settles the charge (finish_ok on a 200 that ends cleanly) or releases it,
        and forward's finally releases whatever is left.
        """
        gov = self.gov
        idle_s = float(gov.config["upstream_idle_s"])
        while True:
            outcome = await self.wait_turn(ticket, reader)
            if outcome == "gone":
                gov.drop(ticket)
                gov.release(ticket)
                gov.log("client_gone", job=ticket.job, model=ticket.model)
                return
            if outcome == "expired":
                gov.drop(ticket)
                gov.release(ticket)
                gov.log("gave_up", job=ticket.job, model=ticket.model, attempts=ticket.attempts + 1)
                await send_simple(client, 429, held_body(gov), "text/plain")
                return
            upstream = None
            sent_at = gov.clock()
            gov.upstream_started(ticket)
            ticket.started_above = gov.inflight_above(ticket)
            try:
                upstream = await self.open_upstream(method, path, headers, body)
                ttfb_s = gov.clock() - sent_at
                if upstream.status == 429:
                    raw = await read_all(upstream.reader, upstream.headers, MAX_429_BODY, idle_s)
                    gov.note_upstream(ticket, gov.clock() - sent_at)
                    upstream.close()
                    upstream = None
                    gov.log(
                        "upstream_429",
                        job=ticket.job,
                        model=ticket.model,
                        attempt=ticket.attempts + 1,
                        body=body_snippet(raw),
                    )
                    gov.release(ticket)
                    gov.learn_from_429(ticket, raw.decode("utf-8", "replace"))
                    now = gov.clock()
                    if now - ticket.first_seen >= float(gov.config["give_up_s"]):
                        gov.log("gave_up", job=ticket.job, model=ticket.model, attempts=ticket.attempts + 1)
                        await send_simple(client, 429, held_body(gov), "text/plain")
                        return
                    delay = gov.retry_delay(ticket, now)
                    ticket.attempts += 1
                    ticket.not_before = now + delay
                    ticket.waiting_since = now
                    ticket.future = asyncio.get_running_loop().create_future()
                    gov.requeue_head(ticket)
                    gov.log("retry", job=ticket.job, model=ticket.model, attempt=ticket.attempts + 1, delay_s=round(delay, 2))
                    continue
                scanner = UsageScanner(is_event_stream(upstream.headers) or streaming)
                await send_head(client, upstream.status, upstream.headers)
                async for chunk in iter_body(upstream.reader, upstream.headers, idle_s):
                    scanner.feed(chunk)
                    await send_chunk(client, chunk)
                await end_chunks(client)
                scanner.finish()
                upstream.release()
                upstream_s = gov.clock() - sent_at
                gov.note_upstream(ticket, upstream_s)
                if upstream.status == 200:
                    gov.finish_ok(ticket, scanner.usage, upstream_s=upstream_s, ttfb_s=ttfb_s)
                else:
                    gov.release(ticket)
                gov.log(
                    "request",
                    job=ticket.job,
                    model=ticket.model,
                    lane=ticket.lane,
                    status=upstream.status,
                    attempts=ticket.attempts + 1,
                )
                return
            except UPSTREAM_FAILURES as exc:
                kind = failure_kind(exc)
                gov.count_error(kind)
                gov.release(ticket)
                gov.log(
                    "upstream_failed",
                    job=ticket.job,
                    model=ticket.model,
                    kind=kind,
                    client_started=client.started,
                )
                if client.started:
                    client.aborted = True
                else:
                    status = 504 if kind == "timeout" else 502
                    await self._fail(client, status, json.dumps({"error": kind}).encode())
                return
            finally:
                gov.upstream_finished(ticket)
                if upstream is not None:
                    upstream.close()


async def run(governor: Governor, upstream_url: str, key: str, port: int) -> None:
    proxy = Proxy(governor, upstream_url, key)
    server = await proxy.start("127.0.0.1", port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    governor.log("started", port=port, upstream=urlsplit(upstream_url).netloc)
    await stop.wait()
    server.close()
    governor.log("draining", in_flight=len(proxy.active))
    await proxy.shutdown(float(governor.config["shutdown_s"]))
    governor.log("stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentq-proxy", description="Morph rate governor on 127.0.0.1.")
    parser.add_argument("--home", default=os.environ.get("AGENTQ_HOME", DEFAULT_HOME))
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    home = Path(os.path.expanduser(args.home))
    config = load_config(home)
    port = args.port or int(os.environ.get("AGENTQ_PROXY_PORT", config["port"]))
    upstream = os.environ.get("AGENTQ_UPSTREAM", config["upstream"])
    try:
        key = load_key()
    except KeyMissingError:
        sys.stderr.write(
            f"agentq proxy: no Morph key in the Keychain (service {KEYCHAIN_SERVICE}); "
            f"exit {EX_CONFIG}, launchd throttles the restart\n"
        )
        return EX_CONFIG
    governor = Governor(home, config)
    try:
        asyncio.run(run(governor, upstream, key, port))
    finally:
        writer = getattr(governor.log, "writer", None)
        if writer is not None:
            writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
