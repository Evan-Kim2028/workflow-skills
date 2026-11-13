"""DeepSeek fill controller and launch pacing.

The fill controller holds the DeepSeek pool near ``fill.target_billed_per_min`` by adding filler
workers in steps (``fill.step_workers`` at a time, each step judged after ``fill.step_interval_s``).
A step is judged by the billed gain per worker it actually added, against the long-run per-worker
rate measured before the step. Poor gain records a learned ceiling that expires after
``fill.ceiling_ttl_s``.

Pacing caps how fast DeepSeek jobs may start. It counts the current Morph minute's uncached input
(the proxy's live ``uncached_ds`` budget, in-flight charges included) plus an estimate for each
launch whose first request the proxy has not yet seen. Pending launches come from ``jobs.started_at``
in agentq.db, so a broker restart keeps the history.

Everything here is a pure function of values the caller reads, except the readers (``minute_rows``,
``recent_429``, ``job_counts``, ``pending_launches``, ``current_uncached``, ``measured_first_request``),
which read agentq.db and never write. The broker calls ``decide`` each tick and ``pacing_allowance``
at launch time; ``main`` prints one reading for a copy of the store.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from agentq import db
from agentq import filler as filler_lib

DS_FAMILY = "morph-dsv41flash"
DS_TIER = "pi-deepseek-flash"
DS_MODEL_LIKE = DS_FAMILY + "%"
FILLER_LABEL_LIKE = "%-backlog-filler-%"
LAUNCH_PI = "launch_pi.py"
DS_MARKERS = ("pi-deepseek-flash", DS_FAMILY)
QUEUE_JOB_ENV = re.compile(r"AGENTQ_JOB=[0-9a-f]{12}\b")
WINDOW_MINUTES = 5
MINUTE_S = 60.0
PS_TIMEOUT_S = 10.0
MEASURED_CLAMP = (10_000, 200_000)
UNCACHED_BUDGET = "uncached_ds"

DEFAULT_FILL = {
    "enabled": False,
    "target_billed_per_min": 8_000_000,
    "max_workers": 64,
    "low_frac": 0.9,
    "high_frac": 0.97,
    "step_workers": 4,
    "step_interval_s": 300.0,
    "min_marginal_frac": 0.5,
    "ceiling_ttl_s": 1800.0,
    "rate_window_s": 1800.0,
    "rate_min_samples": 3,
    "rate_min": 10_000.0,
    "bootstrap_rate": 150_000.0,
    "gate_429_window_s": 120.0,
    "laptop_min_free_pct": 20.0,
    "laptop_max_load_per_cpu": 1.0,
}
DEFAULT_PACING = {
    "enabled": True,
    "uncached_limit": 400_000,
    "uncached_frac": 0.8,
    "first_request_uncached": 40_000,
    "first_request_grace_s": 120.0,
    "measure_first_request": True,
    "measure_percentile": 75,
    "min_measured_jobs": 20,
    "measure_window_s": 86_400.0,
}


def section(override, defaults: dict) -> dict:
    """``defaults`` with the keys of ``override`` applied. Unknown keys and unparseable values are ignored."""
    out = dict(defaults)
    if not isinstance(override, dict):
        return out
    for name, value in override.items():
        if name not in defaults:
            continue
        default = defaults[name]
        try:
            if isinstance(default, bool):
                out[name] = bool(value)
            elif isinstance(default, int):
                out[name] = int(value)
            elif isinstance(default, float):
                out[name] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def filling_state(was_filling: bool, avg: float, target: float, low_frac: float, high_frac: float) -> bool:
    """Hysteresis: start filling below the low mark, stop above the high mark, keep the state between."""
    if avg < low_frac * target:
        return True
    if avg >= high_frac * target:
        return False
    return was_filling


def ds_blocked(status: dict | None) -> int:
    """DeepSeek requests the proxy is holding right now, summed over budgets.

    A proxy that reports no per-family count falls back to each budget's whole queue, which is the
    conservative reading.
    """
    if not isinstance(status, dict):
        return 0
    total = 0
    for budget in (status.get("budgets") or {}).values():
        if not isinstance(budget, dict):
            continue
        by_family = budget.get("blocked_by_family")
        if isinstance(by_family, dict):
            total += int(by_family.get(DS_FAMILY) or 0)
        else:
            queued = budget.get("queued") if isinstance(budget.get("queued"), dict) else {}
            total += sum(int(n) for n in queued.values() if isinstance(n, int))
    return total


def proxy_available(status: dict | None) -> bool:
    """True when the proxy answered with a status that has budgets. None, or an empty document, is unavailable."""
    return isinstance(status, dict) and bool(status.get("budgets")) and isinstance(status.get("budgets"), dict)


def gate_reason(status: dict | None, recent_429: int, laptop: tuple, fill: dict) -> str | None:
    """Why filler must wait now, or None. Any one of these holds filler: the proxy status is unavailable,
    DeepSeek requests blocked at the proxy, a DeepSeek 429 in the recent window, or the laptop short of
    memory or CPU. An unreadable gauge does not hold on its own."""
    if not proxy_available(status):
        return "proxy status unavailable; filler held"
    blocked = ds_blocked(status)
    if blocked > 0:
        return f"Morph gate: {blocked} DeepSeek requests waiting at the proxy"
    if recent_429 > 0:
        return f"Morph gate: {recent_429} DeepSeek 429s in the last {int(fill['gate_429_window_s'])} s"
    free, load = laptop
    if free is not None and free < fill["laptop_min_free_pct"]:
        return f"laptop gate: memory {free:.0f}% free"
    if load is not None and load >= fill["laptop_max_load_per_cpu"]:
        return f"laptop gate: load {load:.2f} per CPU"
    return None


@dataclass(frozen=True)
class FillPlan:
    """What the controller does this tick: whether it is filling, how many filler jobs to submit, and why."""

    filling: bool
    submit: int
    reason: str
    key: str


class FillController:
    """Stepwise filler controller that judges only probes.

    A probe is a submission that takes busy DeepSeek workers above the highest count reached since the
    ceiling was set (or above the ceiling itself). A probe is a step: it is judged ``step_interval_s``
    later by the billed gain per worker it actually started, against the long-run per-worker rate measured
    before the step. A gain counts as good only if it is positive and at least ``min_marginal_frac`` of
    that rate. A poor probe records a ceiling of max(the count before the probe, the current ceiling),
    never below the real (non-filler) DeepSeek count, for ``ceiling_ttl_s``.

    Refills at or below the peak are plain launches. They are not judged and never change the ceiling.
    After a ceiling expires, one probe past it is allowed and no further. While the long-run rate is
    unknown or below ``rate_min``, the controller takes no steps and judges nothing.
    """

    def __init__(self, ceiling: tuple[int, float] | None = None):
        self.filling = False
        self.step: dict | None = None
        self.ceiling = ceiling
        self.last_ceiling = ceiling[0] if ceiling else None
        self.peak = 0
        self.samples: deque = deque()
        self.new_ceiling: tuple[int, float] | None = None
        self.judgement: str | None = None

    def active_ceiling(self, now: float, fill: dict) -> int | None:
        """The learned ceiling while it is fresh, else None. An expired ceiling is dropped but remembered."""
        if self.ceiling is None:
            return None
        workers, at = self.ceiling
        if now - at >= fill["ceiling_ttl_s"]:
            self.ceiling = None
            return None
        return workers

    def worker_limit(self, now: float, fill: dict, real_running: int) -> tuple[int, str]:
        """The busy-worker limit now, and what sets it. A learned ceiling never sits below the real DeepSeek count."""
        cap = int(fill["max_workers"])
        ceiling = self.active_ceiling(now, fill)
        if ceiling is not None:
            return min(cap, max(ceiling, real_running)), "learned ceiling"
        if self.last_ceiling is not None:
            probe_limit = self.last_ceiling + int(fill["step_workers"])
            return min(cap, max(probe_limit, real_running)), "probe past expired ceiling"
        return cap, "max_workers"

    def measured_rate(self, now: float, before: float, fill: dict) -> float | None:
        """Median billed-per-busy-worker over the rate window, from samples taken before ``before``.

        None when there are too few samples or the median is below ``rate_min``: the rate is unknown.
        """
        window = fill["rate_window_s"]
        rates = [billed / active for ts, billed, active in self.samples if now - window <= ts < before and active > 0]
        if len(rates) < fill["rate_min_samples"]:
            return None
        rate = statistics.median(rates)
        return rate if rate >= fill["rate_min"] else None

    def _judge(self, now: float, billed_avg: float, active: int, real_running: int, fill: dict) -> None:
        step, self.step = self.step, None
        added = active - step["workers_before"]
        if added <= 0:
            self.judgement = "step void: no filler worker started, nothing to judge"
            return
        bootstrap = step.get("bootstrap", False)
        rate = fill["bootstrap_rate"] if bootstrap else self.measured_rate(now, step["start"], fill)
        if rate is None:
            self.judgement = "step not judged: per-worker rate unknown"
            return
        gain = (billed_avg - step["avg_before"]) / added
        if gain > 0 and gain >= fill["min_marginal_frac"] * rate:
            self.last_ceiling = None
            kind = "bootstrap step" if bootstrap else "step"
            self.judgement = f"{kind} of {added} judged good: {gain:,.0f} per added worker against {rate:,.0f}"
            return
        if bootstrap:
            workers = real_running
        else:
            workers = max(step["workers_before"], self.last_ceiling or 0, real_running)
        self.ceiling = (workers, now)
        self.last_ceiling = workers
        self.peak = active
        self.new_ceiling = self.ceiling
        self.judgement = (f"step of {added} judged poor: {gain:,.0f} per added worker against "
                          f"{rate:,.0f} per worker; ceiling {workers} workers")

    def _bootstrap(self, now: float, fill: dict, active: int, billed_avg: float, queued_filler: int,
                   average: str) -> FillPlan:
        """With no measured per-worker rate and no busy DeepSeek worker, take one bootstrap step judged against the
        prior rate.

        It waits while any worker is busy (so poor bootstraps cannot pile up idle filler), while a ceiling is
        fresh, while filler is still queued, and at the worker cap. A poor bootstrap sets a ceiling at the real
        count, so the next one comes only after the ceiling expires and the pool has drained.
        """
        to_add = min(int(fill["step_workers"]), int(fill["max_workers"]) - active - queued_filler)
        if active > 0 or self.active_ceiling(now, fill) is not None or queued_filler > 0 or to_add <= 0:
            return FillPlan(True, 0, "per-worker rate unknown; bootstrap waits on a ceiling, queued filler or the cap",
                            "rate-unknown")
        self.step = {"start": now, "workers_before": active, "avg_before": billed_avg, "added": to_add,
                     "bootstrap": True}
        return FillPlan(True, to_add, f"{average}; no measured per-worker rate, so one bootstrap step of {to_add} "
                        f"filler workers judged at the prior {fill['bootstrap_rate']:,.0f} per worker", "bootstrap")

    def tick(self, now: float, *, fill: dict, paused: bool, billed_avg: float, active: int,
             real_running: int, ready_real: int, queued_filler: int, gate: str | None) -> FillPlan:
        """One decision. ``active`` counts busy DeepSeek workers, filler included; ``real_running`` counts the rest."""
        if not fill["enabled"]:
            self.filling, self.step = False, None
            return FillPlan(False, 0, "fill disabled", "disabled")
        if paused:
            self.filling, self.step = False, None
            return FillPlan(False, 0, "filler paused", "paused")
        self.samples.append((now, billed_avg, active))
        while self.samples and self.samples[0][0] < now - fill["rate_window_s"]:
            self.samples.popleft()
        self.peak = max(self.peak, active)
        target = float(fill["target_billed_per_min"])
        low, high = fill["low_frac"] * target, fill["high_frac"] * target
        self.filling = filling_state(self.filling, billed_avg, target, fill["low_frac"], fill["high_frac"])
        if self.step is not None and now - self.step["start"] >= fill["step_interval_s"]:
            self._judge(now, billed_avg, active, real_running, fill)
        average = f"5-min average {billed_avg / 1e6:.2f}M/min"
        if not self.filling:
            return FillPlan(False, 0, f"{average} at or above the {high / 1e6:.2f}M high mark; no filler", "high-mark")
        if ready_real > 0:
            return FillPlan(True, 0, f"{ready_real} real DeepSeek jobs ready; they launch before filler", "real-ready")
        if gate is not None:
            return FillPlan(True, 0, gate, "gate")
        if self.step is not None:
            left = max(0.0, fill["step_interval_s"] - (now - self.step["start"]))
            return FillPlan(True, 0, f"step of {self.step['added']} in progress; judged in {left:.0f} s", "step-wait")
        if self.measured_rate(now, now, fill) is None:
            return self._bootstrap(now, fill, active, billed_avg, queued_filler, average)
        limit, cause = self.worker_limit(now, fill, real_running)
        room = limit - active - queued_filler
        if room <= 0:
            return FillPlan(True, 0, f"{average}; {active} busy at the {cause} of {limit}", "at-limit")
        to_add = min(int(fill["step_workers"]), room)
        top = max(self.peak, self.last_ceiling or 0)
        if active + to_add <= top:
            return FillPlan(True, to_add, f"{average}; refilling {to_add} to {active + to_add} busy (at or below "
                            f"the peak {top}; not a probe)", "refill")
        self.step = {"start": now, "workers_before": active, "avg_before": billed_avg, "added": to_add}
        return FillPlan(True, to_add, f"{average} below the {low / 1e6:.2f}M low mark; probing {to_add} "
                        f"filler workers from {active} busy (limit {limit})", "stepping")


def pacing_allowance(current: int, pending: int, pacing: dict, measured: int | None) -> tuple[int, int]:
    """(DeepSeek launches allowed now, first-request cost used).

    ``current`` is the Morph minute's uncached input so far. Each pending launch, whose first request
    the proxy has not counted yet, adds the first-request cost.
    """
    est = int(measured) if measured else int(pacing["first_request_uncached"])
    cap = pacing["uncached_frac"] * pacing["uncached_limit"]
    return max(0, math.floor((cap - current - est * pending) / est)), est


@dataclass(frozen=True)
class Decision:
    """One reading: the inputs the controller and pacing used, and the plan reached."""

    plan: FillPlan
    billed_avg: float
    active_now: int
    direct_ds: int | None
    running_ds: int
    ready_real: int
    queued_filler: int
    running_filler: int
    recent_429: int
    gate: str | None
    pacing_current: int
    pacing_pending: int
    pacing_source: str
    pacing_left: int
    pacing_cost: int


def decide(
    conn, now: float, *, controller: FillController, fill: dict, pacing: dict, status: dict | None,
    ps_text: str | None, laptop: tuple, paused: bool, measured: int | None,
) -> Decision:
    """Read the store and return what the controller and pacing would do now. The controller keeps its
    own history; nothing else is written."""
    counts = job_counts(conn, now)
    direct = direct_ds_workers(ps_text) if fill["enabled"] else None
    active = counts["running_ds"] + (direct or 0)
    billed_avg = sum(row["billed"] for row in minute_rows(conn, now, WINDOW_MINUTES).values()) / WINDOW_MINUTES
    recent = recent_429(conn, now, fill["gate_429_window_s"])
    gate = gate_reason(status, recent, laptop, fill)
    if fill["enabled"] and direct is None and gate is None:
        gate = "worker count unknown (ps failed); filler waits"
    plan = controller.tick(
        now, fill=fill, paused=paused, billed_avg=billed_avg, active=active,
        real_running=counts["running_ds"] - counts["running_filler"],
        ready_real=counts["ready_real"], queued_filler=counts["queued_filler"], gate=gate,
    )
    current, source, pending = pacing_inputs(conn, now, status, pacing)
    left, cost = pacing_allowance(current, pending, pacing, measured)
    return Decision(
        plan=plan, billed_avg=billed_avg, active_now=active, direct_ds=direct,
        running_ds=counts["running_ds"], ready_real=counts["ready_real"], queued_filler=counts["queued_filler"],
        running_filler=counts["running_filler"], recent_429=recent, gate=gate, pacing_current=current,
        pacing_pending=pending, pacing_source=source, pacing_left=left, pacing_cost=cost,
    )


def minute_stamp(ts: float) -> str:
    """The UTC minute of ``ts`` in proxy_minutes' format."""
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


def minute_rows(conn, now: float, minutes: int) -> dict[str, dict]:
    """DeepSeek billed tokens per minute for the last ``minutes`` complete UTC minutes, keyed by minute stamp.
    Billed is input + cache_read + output, as Morph counts it. A minute with no rows is absent."""
    end = math.floor(now / 60) * 60
    try:
        found = conn.execute(
            """SELECT minute, SUM(input) AS input, SUM(cache_read) AS cache_read, SUM(output) AS output
               FROM proxy_minutes WHERE model LIKE ? AND minute >= ? AND minute < ? GROUP BY minute""",
            (DS_MODEL_LIKE, minute_stamp(end - MINUTE_S * minutes), minute_stamp(end)),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {
        row["minute"]: {
            "input": int(row["input"] or 0),
            "billed": int((row["input"] or 0) + (row["cache_read"] or 0) + (row["output"] or 0)),
        }
        for row in found
    }


def current_uncached(status: dict | None, conn, now: float) -> tuple[int, str]:
    """DeepSeek uncached input in the current Morph minute, and where it was read from.

    The proxy's live ``uncached_ds`` budget is the source, since it counts in-flight charges. Without a
    proxy reading, the store's current-minute row stands in (the proxy flushes it every few seconds).

    Window mode: ``used_60s`` is the calendar minute only when the budget's window_mode is ``calendar``.
    A rolling budget also holds the previous minute's charges, so just after a minute boundary it
    overcounts. That is conservative, so pacing is left as it is; the live proxy sets uncached_ds to calendar.
    """
    budget = ((status or {}).get("budgets") or {}).get(UNCACHED_BUDGET) if isinstance(status, dict) else None
    if isinstance(budget, dict) and isinstance(budget.get("used_60s"), int):
        return int(budget["used_60s"]), "proxy"
    try:
        row = conn.execute(
            """SELECT SUM(input) FROM proxy_minutes WHERE model LIKE ? AND minute <= ?
               GROUP BY minute ORDER BY minute DESC LIMIT 1""",
            (DS_MODEL_LIKE, minute_stamp(now)),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, "store"
    return (int(row[0] or 0) if row else 0), "store"


def pacing_inputs(conn, now: float, status: dict | None, pacing: dict) -> tuple[int, str, int]:
    """(current uncached input, its source, pending launches) for the pacing allowance.

    With the proxy available, a requestless launch counts only inside the first-request grace period.
    With it unavailable, every requestless launch counts: the proxy is not seeing traffic, so nothing
    can be ruled out, and the store's last known minute stands in for the current one.
    """
    current, source = current_uncached(status, conn, now)
    grace = pacing["first_request_grace_s"] if proxy_available(status) else None
    return current, source, pending_launches(conn, now, grace)


def pending_launches(conn, now: float, grace_s: float | None) -> int:
    """DeepSeek jobs whose first request the proxy has not recorded yet.

    A job with a proxy_jobs row has been seen (its usage is in the live budget). A job without one may
    still be starting, so it counts at the first-request estimate. With ``grace_s`` only jobs started
    within it count; None counts every such job. Each launch is counted once, and the count comes from
    the store, so a broker restart keeps it.
    """
    since = None if grace_s is None else now - grace_s
    try:
        row = conn.execute(
            """SELECT COUNT(*) FROM jobs j WHERE j.state = 'running' AND j.tier = ?
               AND (? IS NULL OR j.started_at >= ?)
               AND NOT EXISTS (SELECT 1 FROM proxy_jobs p WHERE p.job = j.id AND p.model LIKE ?)""",
            (DS_TIER, since, since, DS_MODEL_LIKE),
        ).fetchone()
    except sqlite3.OperationalError:
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state = 'running' AND tier = ? AND (? IS NULL OR started_at >= ?)",
            (DS_TIER, since, since),
        ).fetchone()
    return int(row[0])


def recent_429(conn, now: float, seconds: float) -> int:
    """DeepSeek upstream 429s in the minutes that overlap the last ``seconds``."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(r429), 0) FROM proxy_minutes WHERE model LIKE ? AND minute >= ?",
            (DS_MODEL_LIKE, minute_stamp(now - seconds)),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0])


def job_counts(conn, now: float) -> dict[str, int]:
    """DeepSeek jobs by role: running (queue and filler), ready real (urgent or normal lane), queued and
    running filler."""
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN state = 'running' AND tier = ? THEN 1 ELSE 0 END) AS running_ds,
             SUM(CASE WHEN state = 'queued' AND tier = ? AND lane IN ('urgent', 'normal')
                       AND not_before <= ? THEN 1 ELSE 0 END) AS ready_real,
             SUM(CASE WHEN state = 'queued' AND label LIKE ? THEN 1 ELSE 0 END) AS queued_filler,
             SUM(CASE WHEN state = 'running' AND label LIKE ? THEN 1 ELSE 0 END) AS running_filler
           FROM jobs""",
        (DS_TIER, DS_TIER, now, FILLER_LABEL_LIKE, FILLER_LABEL_LIKE),
    ).fetchone()
    return {name: int(row[name] or 0) for name in ("running_ds", "ready_real", "queued_filler", "running_filler")}


def direct_ds_workers(ps_text: str | None) -> int | None:
    """DeepSeek pi workers started outside the queue (no AGENTQ_JOB in their environment), or None
    when the process table was not read."""
    if ps_text is None:
        return None
    return sum(
        1
        for line in ps_text.splitlines()
        if LAUNCH_PI in line and any(marker in line for marker in DS_MARKERS) and not QUEUE_JOB_ENV.search(line)
    )


def process_table(run=subprocess.run) -> str | None:
    """``ps -E`` output with environments, or None when ps fails."""
    try:
        result = run(["ps", "-E", "-axo", "pid=,command="], capture_output=True, text=True,
                     timeout=PS_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def measured_first_request(conn, now: float, pacing: dict) -> int | None:
    """The first-request uncached cost, measured from single-request DeepSeek jobs in proxy_jobs.

    A job with one request has that request's uncached input as its whole input, so a percentile of
    those is the launch cost. None when fewer than ``min_measured_jobs`` jobs have been seen in the window.
    The percentile (75 by default) leans high, because short jobs would understate a full session start.
    """
    try:
        rows = conn.execute(
            "SELECT input FROM proxy_jobs WHERE model LIKE ? AND req = 1 AND input > 0 AND last_ts >= ?",
            (DS_MODEL_LIKE, now - pacing["measure_window_s"]),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    values = sorted(int(row[0]) for row in rows)
    if len(values) < pacing["min_measured_jobs"]:
        return None
    rank = max(1, math.ceil(pacing["measure_percentile"] / 100 * len(values)))
    low, high = MEASURED_CLAMP
    return min(max(values[rank - 1], low), high)


def main(argv=None, out=None) -> int:
    """Print one reading from an agentq home: what fill and pacing would do now, and why.

    Read-only: it reads the store, the live proxy status and the laptop gauges, and writes nothing.
    A one-shot read has no step history, so the controller starts from a clean state and its first
    step is what it would do on a fresh broker. Run it on a copy of the store when the live broker runs.
    """
    from agentq import broker as broker_lib

    parser = argparse.ArgumentParser(prog="agentq.fill", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--home", required=True, help="agentq home holding agentq.db and broker.json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    home = Path(args.home).expanduser()
    try:
        data = json.loads((home / "broker.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    fill = section(data.get("fill"), DEFAULT_FILL)
    pacing = section(data.get("pacing"), DEFAULT_PACING)
    try:
        seed = json.loads((home / filler_lib.CONFIG_NAME).read_text(encoding="utf-8"))
        targets, _ = filler_lib.load_targets(seed if isinstance(seed, dict) else {})
    except (OSError, ValueError):
        targets = []
    conn = db.connect(home)
    try:
        now = time.time()
        measured = measured_first_request(conn, now, pacing) if pacing["measure_first_request"] else None
        decision = decide(
            conn, now, controller=FillController(), fill=fill, pacing=pacing, status=broker_lib.proxy_status(),
            ps_text=process_table(), laptop=broker_lib.Gauges().read(15.0), paused=filler_lib.paused(conn),
            measured=measured,
        )
        due = filler_lib.next_target(conn, targets, head_of=filler_lib.module_head)
    finally:
        conn.close()
    plan = decision.plan
    print(f"fill enabled={fill['enabled']} target={fill['target_billed_per_min']:,}/min "
          f"low={fill['low_frac'] * fill['target_billed_per_min']:,.0f} high={fill['high_frac'] * fill['target_billed_per_min']:,.0f}", file=out)
    print(f"DeepSeek billed 5-min average {decision.billed_avg:,.0f}/min; busy {decision.active_now} "
          f"(queue {decision.running_ds}, filler {decision.running_filler}, direct {decision.direct_ds})", file=out)
    print(f"ready real {decision.ready_real}; queued filler {decision.queued_filler}; "
          f"Morph gate: {decision.gate or 'open'}; DeepSeek 429s in gate window {decision.recent_429}", file=out)
    cost_source = "measured" if measured else "default"
    print(f"pacing: current minute uncached {decision.pacing_current:,} ({decision.pacing_source}), "
          f"{decision.pacing_pending} pending launch(es) at {decision.pacing_cost:,} ({cost_source}), "
          f"launches allowed now {decision.pacing_left}", file=out)
    print(f"next filler target: {due[0].key} at {due[1][:12]}" if due else "next filler target: none due", file=out)
    print(f"ANSWER: would submit {plan.submit} filler job(s) now; filling={plan.filling}; {plan.reason}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
