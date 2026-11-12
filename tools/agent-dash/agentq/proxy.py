"""agentq Morph rate governor.

A local HTTP proxy between pi and api.morphllm.com. Each request is admitted
only when every Morph budget it touches has room under the learned target,
otherwise it waits in a lane queue (urgent, normal, backlog). Upstream 429s
are retried inside the proxy and never reach the client. The limits are
learned from the 429 bodies and persisted in proxy_state.json.

Stdlib only (asyncio streams and ssl), so launchd runs the file with the
system python3 and needs no virtualenv. Streaming responses pass through
chunk by chunk; the usage object in the last SSE event is read on the way.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import random
import re
import signal
import sqlite3
import ssl
import subprocess
import sys
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

LANES = ("urgent", "normal", "backlog")
DEFAULT_HOME = "~/.local/state/agentq"
DEFAULT_UPSTREAM = "https://api.morphllm.com/v1"
KEYCHAIN_SERVICE = "morph-api-key"
STATUS_PATH = "/agentq/status"
UPSTREAM_PREFIX = "/v1"

DEFAULT_CONFIG = {
    "port": 8790,
    "upstream": DEFAULT_UPSTREAM,
    "window_s": 60.0,
    "start_target": 0.95,
    "target_floor": 0.6,
    "target_cut": 0.97,
    "target_step_up": 0.005,
    "default_output_tokens": 800,
    "give_up_s": 240.0,
    "retry_floor_s": 1.0,
    "retry_jitter_s": 2.0,
    "aging_s": 900.0,
    "budgets": {
        "total": {"limit": 10_000_000},
        "output": {"limit": 70_000, "model_prefix": "morph-dsv41flash"},
        "uncached": {"limit": 24_290, "model": "morph-kimik31"},
    },
}

SATURATION_WAIT_S = 2.0
LANE_CACHE_S = 2.0
LANE_CACHE_MAX = 10_000
PREFIX_STORE_JOBS = 64
MAINTAIN_S = 0.2
FLUSH_S = 5.0
MAX_REQUEST_BODY = 32 * 1024 * 1024
MAX_429_BODY = 1024 * 1024
MAX_WHOLE_BODY = 8 * 1024 * 1024
READ_CHUNK = 64 * 1024
ROW_WAIT_SAMPLES = 500

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

PROXY_MINUTES_DDL = """
CREATE TABLE IF NOT EXISTS proxy_minutes (
    minute TEXT NOT NULL,
    model TEXT NOT NULL,
    req INT, ok INT, r429 INT,
    input INT, cache_read INT, output INT, uncached_est INT,
    wait_p50_ms INT, wait_p95_ms INT,
    target REAL,
    PRIMARY KEY (minute, model)
)
"""


def default_log(event: str, **fields: object) -> None:
    """Write one structured line to stderr. Callers pass only ids and counts."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pairs = "".join(f" {key}={value}" for key, value in fields.items())
    sys.stderr.write(f"{stamp} agentq-proxy {event}{pairs}\n")
    sys.stderr.flush()


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

    The key is returned to the caller only. Errors name the source, never the value.
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
        raise SystemExit(f"agentq proxy: cannot read the Morph key from the Keychain ({type(exc).__name__})")
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise SystemExit(f"agentq proxy: no Morph key in the Keychain (service {KEYCHAIN_SERVICE})")
    return key


def tokens_for_chars(chars: int) -> int:
    """Rough token count: four characters per token, rounded up."""
    return (max(chars, 0) + 3) // 4


def common_prefix_len(first: str, second: str) -> int:
    """Length of the shared leading run, found by bisection over slice comparisons."""
    low, high = 0, min(len(first), len(second))
    while low < high:
        mid = (low + high + 1) // 2
        if first[:mid] == second[:mid]:
            low = mid
        else:
            high = mid - 1
    return low


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[index]


def minute_stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


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


def estimate_for(budget_name: str, prompt: int, output: int, uncached: int) -> int:
    """Token cost a request is expected to add to one budget before its real usage arrives."""
    if budget_name == "output":
        return output
    if budget_name == "uncached":
        return uncached
    return prompt + output


def read_lane(db_path: Path, job: str) -> str:
    """Read a job's lane from agentq.db without writing. Anything missing means normal."""
    if not db_path.exists():
        return "normal"
    try:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            row = connection.execute("SELECT lane FROM jobs WHERE id = ?", (job,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return "normal"
    lane = row[0] if row else None
    return lane if lane in LANES else "normal"


def write_minute_rows(db_path: Path, rows: list[tuple]) -> None:
    """Upsert one proxy_minutes row per model per minute. Runs in a worker thread."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(PROXY_MINUTES_DDL)
        connection.executemany("INSERT OR REPLACE INTO proxy_minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        connection.commit()
    finally:
        connection.close()


@dataclass(eq=False)
class Entry:
    """One budget charge. In-flight charges hold the estimate; done ones hold real usage."""

    at: float
    amount: int
    lane: str
    done: bool = False


@dataclass(eq=False)
class Budget:
    """A rolling-window token budget with a learned limit and a target fraction of it."""

    name: str
    limit: int
    target: float
    model_prefix: str | None = None
    model: str | None = None
    entries: list[Entry] = field(default_factory=list)
    last_429: float = float("-inf")
    last_grow: float = 0.0
    saturated: bool = False

    def applies(self, model: str) -> bool:
        if self.model_prefix is not None:
            return model.startswith(self.model_prefix)
        if self.model is not None:
            return model == self.model
        return True

    def cap(self) -> float:
        return self.limit * self.target

    def used(self, now: float, window: float, lane: str | None = None) -> int:
        cutoff = now - window
        self.entries = [e for e in self.entries if not e.done or e.at > cutoff]
        return sum(e.amount for e in self.entries if lane is None or e.lane == lane)

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
    future: asyncio.Future | None = None
    not_before: float = 0.0
    attempts: int = 0
    holds: list[tuple[Budget, Entry]] = field(default_factory=list)


class Governor:
    """Admission, lane queues, learned limits and minute counters. All calls run on the event loop thread,
    except lane_for, which the proxy calls in a worker thread and which touches only its own cache."""

    def __init__(
        self,
        home: Path,
        config: dict,
        *,
        clock=time.monotonic,
        wall=time.time,
        log=default_log,
    ) -> None:
        self.home = Path(home)
        self.config = config
        self.clock = clock
        self.wall = wall
        self.log = log
        self.window = float(config["window_s"])
        start = clock()
        self.budgets: dict[str, Budget] = {}
        for name, spec in config["budgets"].items():
            self.budgets[name] = Budget(
                name=name,
                limit=int(spec["limit"]),
                target=float(config["start_target"]),
                model_prefix=spec.get("model_prefix"),
                model=spec.get("model"),
                last_grow=start,
            )
        self.queues: dict[str, deque[Ticket]] = {lane: deque() for lane in LANES}
        self.output_history: dict[str, deque[int]] = {}
        self.prefixes: OrderedDict[str, str] = OrderedDict()
        self.lane_cache: dict[str, tuple[str, float]] = {}
        self.minute_rows: dict[tuple[str, str], dict] = {}
        self.dirty: set[tuple[str, str]] = set()
        self.waits: deque[tuple[float, float]] = deque()
        self.load_state()

    @property
    def db_path(self) -> Path:
        return self.home / "agentq.db"

    @property
    def state_path(self) -> Path:
        return self.home / "proxy_state.json"

    def load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        for name, spec in (data.get("budgets") or {}).items():
            budget = self.budgets.get(name)
            if budget is None or not isinstance(spec, dict):
                continue
            budget.limit = int(spec.get("limit", budget.limit))
            floor = float(self.config["target_floor"])
            budget.target = min(1.0, max(floor, float(spec.get("target_frac", budget.target))))

    def save_state(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": self.wall(),
            "budgets": {
                name: {"limit": b.limit, "target_frac": round(b.target, 6)} for name, b in self.budgets.items()
            },
        }
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, self.state_path)

    def lane_for(self, job: str) -> str:
        if not job:
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

    def uncached_estimate(self, job: str, text: str) -> int:
        """Tokens beyond the longest prefix this job sent last time. An unknown job counts the whole prompt."""
        if not job:
            return tokens_for_chars(len(text))
        previous = self.prefixes.get(job)
        shared = common_prefix_len(previous, text) if previous is not None else 0
        self.prefixes[job] = text
        self.prefixes.move_to_end(job)
        while len(self.prefixes) > PREFIX_STORE_JOBS:
            self.prefixes.popitem(last=False)
        return tokens_for_chars(len(text) - shared)

    def make_ticket(
        self,
        *,
        job: str,
        lane: str,
        model: str,
        text: str,
        max_tokens: int | None,
        now: float,
    ) -> Ticket:
        prompt = tokens_for_chars(len(text))
        output = self.output_estimate(model, max_tokens)
        uncached = self.uncached_estimate(job, text)
        estimates = {
            b.name: estimate_for(b.name, prompt, output, uncached)
            for b in self.budgets.values()
            if b.applies(model)
        }
        return Ticket(
            job=job,
            lane=lane,
            model=model,
            estimates=estimates,
            uncached_est=uncached,
            queued_at=now,
            first_seen=now,
        )

    def enqueue(self, ticket: Ticket) -> None:
        self.queues[ticket.lane].append(ticket)

    def requeue_head(self, ticket: Ticket) -> None:
        self.queues[ticket.lane].appendleft(ticket)

    def drop(self, ticket: Ticket) -> None:
        """Remove a ticket that will never be sent: its client left or it gave up."""
        with contextlib.suppress(ValueError):
            self.queues[ticket.lane].remove(ticket)

    def promote_aged(self, now: float) -> None:
        """Normal tickets queued past aging_s join the urgent lane, kept in arrival order."""
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
        """Admit every queued ticket that fits, urgent first, FIFO within a lane."""
        now = self.clock()
        self.promote_aged(now)
        for budget in self.budgets.values():
            budget.saturated = False
        lower_waiting = bool(self.queues["normal"] or self.queues["backlog"])
        urgent_or_normal = bool(self.queues["urgent"] or self.queues["normal"])
        for lane in LANES:
            if lane == "backlog" and urgent_or_normal:
                break
            queue = self.queues[lane]
            while queue:
                head = queue[0]
                if head.not_before > now:
                    break
                blocker = self._blocker(head, now, lower_waiting)
                if blocker is not None:
                    blocker.saturated = True
                    break
                queue.popleft()
                self._admit(head, now)

    def _blocker(self, ticket: Ticket, now: float, lower_waiting: bool) -> Budget | None:
        """Return the first budget that cannot take this ticket now, or None when it fits everywhere.

        A budget with no charges in the window always admits one request, even an oversized one,
        so a large request cannot wait forever.
        """
        for name, estimate in ticket.estimates.items():
            budget = self.budgets[name]
            cap = budget.cap()
            used = budget.used(now, self.window)
            if used > 0 and used + estimate > cap:
                return budget
            if ticket.lane == "urgent" and lower_waiting:
                urgent_used = budget.used(now, self.window, lane="urgent")
                if urgent_used > 0 and urgent_used + estimate > 0.5 * cap:
                    return budget
        return None

    def _admit(self, ticket: Ticket, now: float) -> None:
        ticket.holds = []
        for name, estimate in ticket.estimates.items():
            budget = self.budgets[name]
            entry = Entry(at=now, amount=estimate, lane=ticket.lane)
            budget.entries.append(entry)
            ticket.holds.append((budget, entry))
        wait = now - ticket.queued_at
        self.waits.append((now, wait))
        row = self.row(ticket.model)
        row["req"] += 1
        if len(row["waits"]) < ROW_WAIT_SAMPLES:
            row["waits"].append(wait)
        if ticket.future is not None and not ticket.future.done():
            ticket.future.set_result(None)

    def finish_ok(self, ticket: Ticket, usage: dict | None) -> None:
        """Replace the estimates with real usage. Without usage the estimate stays, which errs on the safe side."""
        now = self.clock()
        prompt = cached = output = 0
        real: dict[str, int] = {}
        if usage:
            prompt, output, cached = usage_fields(usage)
            real = {"total": prompt + output, "output": output, "uncached": prompt - cached}
        for budget, entry in ticket.holds:
            entry.amount = real.get(budget.name, entry.amount)
            entry.done = True
            entry.at = now
        ticket.holds = []
        if output > 0:
            self.output_history.setdefault(ticket.model, deque(maxlen=20)).append(output)
        row = self.row(ticket.model)
        row["ok"] += 1
        row["input"] += prompt - cached
        row["cache_read"] += cached
        row["output"] += output
        row["uncached_est"] += ticket.uncached_est

    def release(self, ticket: Ticket) -> None:
        """Drop a charge that upstream did not bill: a 429, an error, or an unreachable upstream."""
        now = self.clock()
        for _, entry in ticket.holds:
            entry.amount = 0
            entry.done = True
            entry.at = now
        ticket.holds = []

    def learn_from_429(self, ticket: Ticket, body_text: str) -> None:
        """Adopt a stated limit when the body names one; cut the target of the budget it names.

        A body that names no limit cuts every budget the request touched. service_overloaded
        is a platform-wide signal, so it cuts the total budget only.
        """
        now = self.clock()
        self.row(ticket.model)["r429"] += 1
        stated = parse_stated_limit(body_text)
        if stated is not None:
            name, limit = stated
            targets = [self.budgets[name]] if name in self.budgets else []
            for budget in targets:
                if budget.limit != limit:
                    self.log("limit_adopted", budget=budget.name, old=budget.limit, new=limit)
                    budget.limit = limit
        elif "service_overloaded" in body_text:
            targets = [self.budgets["total"]] if "total" in self.budgets else []
        else:
            targets = [self.budgets[name] for name in ticket.estimates]
        floor = float(self.config["target_floor"])
        cut = float(self.config["target_cut"])
        for budget in targets:
            budget.target = max(floor, budget.target * cut)
            budget.last_429 = now
            self.log("target_cut", budget=budget.name, target=round(budget.target, 4), limit=budget.limit)
        self.save_state()

    def retry_delay(self, ticket: Ticket, now: float) -> float:
        wait = 0.0
        for name, estimate in ticket.estimates.items():
            budget = self.budgets[name]
            needed = budget.used(now, self.window) + estimate - budget.cap()
            wait = max(wait, budget.seconds_until_free(needed, now, self.window))
        floor = float(self.config["retry_floor_s"])
        jitter = float(self.config["retry_jitter_s"])
        return max(wait, floor) + random.uniform(0.0, jitter)

    def tick(self) -> None:
        """Raise a target by one step after a full window with no 429 and with demand still blocked."""
        now = self.clock()
        window = self.window
        step = float(self.config["target_step_up"])
        changed = False
        for budget in self.budgets.values():
            quiet = now - budget.last_429 >= window and now - budget.last_grow >= window
            if budget.saturated and budget.target < 1.0 and quiet:
                budget.target = min(1.0, budget.target + step)
                budget.last_grow = now
                changed = True
                self.log("target_raised", budget=budget.name, target=round(budget.target, 4))
        if changed:
            self.save_state()

    def row(self, model: str) -> dict:
        key = (minute_stamp(self.wall()), model)
        row = self.minute_rows.get(key)
        if row is None:
            row = {"req": 0, "ok": 0, "r429": 0, "input": 0, "cache_read": 0, "output": 0, "uncached_est": 0, "waits": []}
            self.minute_rows[key] = row
        self.dirty.add(key)
        return row

    def take_minute_rows(self) -> list[tuple]:
        """Return the rows changed since the last call, ready for write_minute_rows."""
        rows = []
        for key in self.dirty:
            row = self.minute_rows.get(key)
            if row is None:
                continue
            minute, model = key
            waits = sorted(row["waits"])
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
                )
            )
        self.dirty.clear()
        cutoff = minute_stamp(self.wall() - 120)
        for key in [k for k in self.minute_rows if k[0] < cutoff]:
            del self.minute_rows[key]
        return rows

    def status(self) -> dict:
        now = self.clock()
        window = self.window
        while self.waits and self.waits[0][0] < now - window:
            self.waits.popleft()
        queued_ages = [now - t.queued_at for lane in LANES for t in self.queues[lane]]
        saturated = any(wait > SATURATION_WAIT_S for _, wait in self.waits) or any(
            age > SATURATION_WAIT_S for age in queued_ages
        )
        budgets = {}
        for name, budget in self.budgets.items():
            per_lane = {lane: sum(1 for t in self.queues[lane] if name in t.estimates) for lane in LANES}
            budgets[name] = {
                "limit": budget.limit,
                "target_frac": round(budget.target, 4),
                "used_60s": budget.used(now, window),
                "queued": per_lane,
                "saturated": budget.saturated,
            }
        return {
            "ok": True,
            "window_s": window,
            "saturated": saturated,
            "queued": {lane: len(self.queues[lane]) for lane in LANES},
            "budgets": budgets,
        }


@dataclass(eq=False)
class Client:
    writer: asyncio.StreamWriter
    started: bool = False


@dataclass(eq=False)
class Upstream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    status: int
    headers: dict[str, str]

    def close(self) -> None:
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


def held_body(governor: "Governor") -> bytes:
    """The 429 body a client sees when its request was held past give_up_s."""
    seconds = float(governor.config["give_up_s"])
    return f"agentq: held {seconds:g} s, budget full".encode()


def parse_payload(body: bytes) -> dict | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def with_usage_option(payload: dict) -> bytes | None:
    """Return the body with stream_options.include_usage set, or None when the client already set it."""
    options = payload.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    if options.get("include_usage") is not None:
        return None
    options["include_usage"] = True
    payload["stream_options"] = options
    return json.dumps(payload, separators=(",", ":")).encode()


def is_event_stream(headers: dict[str, str]) -> bool:
    return "text/event-stream" in headers.get("content-type", "")


async def read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            return headers
        name, separator, value = line.decode("latin-1").partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()


async def iter_body(reader: asyncio.StreamReader, headers: dict[str, str]):
    """Yield body bytes as they arrive: chunked, length-delimited, or read-to-close."""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        while True:
            size_line = await reader.readline()
            if not size_line:
                return
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await read_headers(reader)
                return
            data = await reader.readexactly(size)
            await reader.readexactly(2)
            yield data
    elif "content-length" in headers:
        remaining = int(headers["content-length"])
        while remaining > 0:
            data = await reader.read(min(READ_CHUNK, remaining))
            if not data:
                return
            remaining -= len(data)
            yield data
    else:
        while True:
            data = await reader.read(READ_CHUNK)
            if not data:
                return
            yield data


async def read_all(reader: asyncio.StreamReader, headers: dict[str, str], cap: int) -> bytes:
    data = bytearray()
    async for chunk in iter_body(reader, headers):
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


class Proxy:
    """HTTP front end: admits requests through the Governor, forwards them, and retries 429s."""

    def __init__(self, governor: Governor, upstream_url: str, key: str) -> None:
        self.gov = governor
        self.upstream_url = upstream_url
        self.key = key
        scheme = urlsplit(upstream_url).scheme
        self.ssl_context = ssl.create_default_context() if scheme == "https" else None
        self.maintenance: asyncio.Task | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> asyncio.Server:
        self.maintenance = asyncio.create_task(self.maintain())
        return await asyncio.start_server(self.handle, host, port)

    async def close(self) -> None:
        if self.maintenance is not None:
            self.maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.maintenance
        await self.flush()
        self.gov.save_state()

    async def maintain(self) -> None:
        last_flush = self.gov.clock()
        while True:
            await asyncio.sleep(MAINTAIN_S)
            try:
                self.gov.pump()
                self.gov.tick()
                if self.gov.clock() - last_flush >= FLUSH_S:
                    last_flush = self.gov.clock()
                    await self.flush()
            except Exception as exc:
                self.gov.log("maintain_error", kind=type(exc).__name__)

    async def flush(self) -> None:
        rows = self.gov.take_minute_rows()
        if not rows:
            return
        try:
            await asyncio.to_thread(write_minute_rows, self.gov.db_path, rows)
        except sqlite3.Error as exc:
            self.gov.log("flush_error", kind=type(exc).__name__)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(writer)
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
        reader, writer = await asyncio.open_connection(
            url.hostname,
            port,
            ssl=self.ssl_context,
            server_hostname=url.hostname if secure else None,
        )
        try:
            target = url.path.rstrip("/") + path[len(UPSTREAM_PREFIX) :]
            lines = [
                f"{method} {target} HTTP/1.1",
                f"Host: {url.netloc}",
                f"Authorization: Bearer {self.key}",
                f"Content-Length: {len(body)}",
                "Accept-Encoding: identity",
                "Connection: close",
            ]
            for name, value in headers.items():
                if name not in REQUEST_DROP:
                    lines.append(f"{name.title()}: {value}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            await writer.drain()
            status_line = await reader.readline()
            status = int(status_line.decode("latin-1").split(" ", 2)[1])
            response_headers = await read_headers(reader)
        except BaseException:
            writer.close()
            raise
        return Upstream(reader, writer, status, response_headers)

    async def wait_turn(self, ticket: Ticket, reader: asyncio.StreamReader) -> str:
        """Block until the ticket is admitted. Returns admitted, gone (client closed) or expired (held too long).

        The deadline runs from the first enqueue, so queue wait and 429 retries share one give-up bound.
        """
        deadline = ticket.first_seen + float(self.gov.config["give_up_s"])
        watch = asyncio.ensure_future(reader.read(1))
        try:
            while True:
                if ticket.future.done():
                    return "admitted"
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
        loop = asyncio.get_running_loop()
        job = headers.get("x-agentq-job", "")
        payload = parse_payload(body)
        model = ""
        max_tokens = None
        streaming = False
        out_body = body
        if payload is not None:
            model = str(payload.get("model") or "")
            max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
            streaming = payload.get("stream") is True
            if streaming:
                out_body = with_usage_option(payload) or body
        text = out_body.decode("utf-8", "replace")
        lane = await asyncio.to_thread(gov.lane_for, job)
        ticket = gov.make_ticket(
            job=job,
            lane=lane,
            model=model,
            text=text,
            max_tokens=max_tokens,
            now=gov.clock(),
        )
        ticket.future = loop.create_future()
        gov.enqueue(ticket)
        gov.pump()
        while True:
            outcome = await self.wait_turn(ticket, reader)
            if outcome == "gone":
                gov.drop(ticket)
                gov.log("client_gone", job=job, model=model)
                return
            if outcome == "expired":
                gov.drop(ticket)
                gov.log("gave_up", job=job, model=model, attempts=ticket.attempts + 1)
                await send_simple(client, 429, held_body(gov), "text/plain")
                return
            try:
                upstream = await self.open_upstream(method, path, headers, out_body)
            except (OSError, ssl.SSLError) as exc:
                gov.release(ticket)
                gov.log("upstream_unreachable", kind=type(exc).__name__)
                await send_simple(client, 502, b'{"error":"upstream unreachable"}')
                return
            if upstream.status == 429:
                raw = await read_all(upstream.reader, upstream.headers, MAX_429_BODY)
                upstream.close()
                gov.release(ticket)
                gov.learn_from_429(ticket, raw.decode("utf-8", "replace"))
                now = gov.clock()
                if now - ticket.first_seen >= float(gov.config["give_up_s"]):
                    gov.log("gave_up", job=job, model=model, attempts=ticket.attempts + 1)
                    await send_simple(client, 429, held_body(gov), "text/plain")
                    return
                delay = gov.retry_delay(ticket, now)
                ticket.attempts += 1
                ticket.not_before = now + delay
                ticket.future = loop.create_future()
                gov.requeue_head(ticket)
                gov.log("retry", job=job, model=model, attempt=ticket.attempts + 1, delay_s=round(delay, 2))
                continue
            scanner = UsageScanner(is_event_stream(upstream.headers) or streaming)
            try:
                await send_head(client, upstream.status, upstream.headers)
                async for chunk in iter_body(upstream.reader, upstream.headers):
                    scanner.feed(chunk)
                    await send_chunk(client, chunk)
                await end_chunks(client)
            finally:
                upstream.close()
                scanner.finish()
                if upstream.status == 200:
                    gov.finish_ok(ticket, scanner.usage)
                else:
                    gov.release(ticket)
            gov.log(
                "request",
                job=job,
                model=model,
                lane=lane,
                status=upstream.status,
                attempts=ticket.attempts + 1,
            )
            return


async def run(governor: Governor, upstream_url: str, key: str, port: int) -> None:
    proxy = Proxy(governor, upstream_url, key)
    server = await proxy.start("127.0.0.1", port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    governor.log("started", port=port, upstream=urlsplit(upstream_url).netloc)
    await stop.wait()
    server.close()
    await proxy.close()
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
    key = load_key()
    asyncio.run(run(Governor(home, config), upstream, key, port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
