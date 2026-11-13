"""DeepSeek fill controller and launch pacing.

The fill controller adds filler workers while the DeepSeek 5-minute billed average is below the low mark:
at most ``step_workers`` every ``step_interval_s``, and only while every gate is open (proxy status
available, no DeepSeek request blocked at the proxy, no DeepSeek 429 recently, the in-flight limit not
binding, and the laptop within the filler bars). It stops at the high mark and never takes busy DeepSeek
workers above ``max_workers``. Nothing is judged and nothing is killed.

Pacing caps how fast DeepSeek jobs may start. It counts the current Morph minute's uncached input (the
proxy's live ``uncached_ds`` budget, in-flight charges included) plus an estimate for each launch whose
first request the proxy has not yet recorded. Pending launches come from ``jobs.started_at`` in agentq.db,
so a broker restart keeps the history. The live proxy's rolling window can overcount just after a minute
boundary; that is conservative, and the live proxy is set to calendar windows for ``uncached_ds``.

Everything here is a pure function of values the caller reads, except the readers (``minute_rows``,
``recent_429``, ``job_counts``, ``pending_launches``, ``current_uncached``, ``measured_first_request``),
which read agentq.db and never write. The broker calls ``decide`` each tick and ``pacing_allowance`` at
launch time; ``main`` prints one reading for a copy of the store.
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
from agentq import headroom as headroom_lib

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
    "step_interval_s": 120.0,
    "gate_429_window_s": 120.0,
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


def inflight_gate(status: dict) -> str | None:
    """Hold filler while DeepSeek is at the in-flight limit and the limit is below its maximum.

    Only an enforced limit holds filler: the test is requests held by it. In shadow or the A/B off arm the
    limit is advisory and holds nothing; the 2026-11-13 A/B (enforce 5.99M billed/min vs off 7.43M at
    -0.3 s first byte) showed the advisory limit is too conservative to gate on. A limit at its maximum
    never holds filler here: the budgets decide that.
    """
    limit = status.get("inflight_limit")
    maximum = status.get("inflight_max")
    if not isinstance(limit, int) or not isinstance(maximum, int) or limit >= maximum:
        return None
    if status.get("inflight_enforcing") is True:
        held = status.get("inflight_queued")
        if isinstance(held, int) and held > 0:
            return f"Morph slow: in-flight limit {limit} reached"
        return None
    return None


def gate_reason(status: dict | None, recent_429: int, laptop: headroom_lib.Headroom | None, laptop_cfg: dict,
                fill: dict) -> str | None:
    """Why filler must wait now, or None. Any one of these holds filler: the proxy status is unavailable,
    DeepSeek requests blocked at the proxy, a DeepSeek 429 in the recent window, the DeepSeek in-flight limit
    reached, or the laptop past the filler bars (CPU busy, memory free or swap used; stricter than the
    real-work bars so filler never pushes real work out). An unreadable measure does not hold on its own."""
    if not proxy_available(status):
        return "proxy status unavailable; filler held"
    blocked = ds_blocked(status)
    if blocked > 0:
        return f"Morph gate: {blocked} DeepSeek requests waiting at the proxy"
    if recent_429 > 0:
        return f"Morph gate: {recent_429} DeepSeek 429s in the last {int(fill['gate_429_window_s'])} s"
    slow = inflight_gate(status)
    if slow is not None:
        return slow
    if laptop is None:
        return None
    breach = headroom_lib.pressure(
        laptop, max_cpu_busy=laptop_cfg["fill_max_cpu_busy"], min_free_pct=laptop_cfg["fill_min_free_pct"],
        max_swap_frac=laptop_cfg["fill_max_swap_frac"],
    )
    return f"laptop gate: {breach[1]}" if breach else None


@dataclass(frozen=True)
class FillPlan:
    """What the controller does this tick: whether it is filling, how many filler jobs to submit, and why."""

    filling: bool
    submit: int
    reason: str
    key: str


GATE_DECAY_S = 600.0


class FillController:
    """Gate-driven filler controller: fill while the DeepSeek billed average is below the low mark.

    The controller tracks ``level``, the filler population it has reached. While filling and every gate is
    open, filler that finished is replaced up to ``level`` at once (only pacing applies). Growth above
    ``level`` is limited to ``step_workers`` per ``step_interval_s``, and a growth step must launch before the
    next one. Above the high mark ``level`` is set to the current filler population, so nothing grows. While a
    gate is closed nothing is submitted; after ``GATE_DECAY_S`` closed, ``level`` decays to the current
    population. Nothing is judged and nothing is killed. ``state()`` and ``from_state()`` persist ``level``,
    ``last_step_at`` and the gate-closure start, so a restart keeps the step interval.
    """

    def __init__(self):
        self.filling = False
        self.level: int | None = None
        self.last_step_at: float | None = None
        self.gate_since: float | None = None

    def state(self) -> dict:
        return {"level": self.level, "last_step_at": self.last_step_at, "gate_since": self.gate_since}

    @classmethod
    def from_state(cls, data) -> "FillController":
        controller = cls()
        if not isinstance(data, dict):
            return controller
        level, last, since = data.get("level"), data.get("last_step_at"), data.get("gate_since")
        controller.level = int(level) if isinstance(level, int) and not isinstance(level, bool) else None
        controller.last_step_at = float(last) if isinstance(last, (int, float)) else None
        controller.gate_since = float(since) if isinstance(since, (int, float)) else None
        return controller

    def tick(self, now: float, *, fill: dict, paused: bool, billed_avg: float, active: int, filler_busy: int,
             ready_real: int, queued_filler: int, gate: str | None) -> FillPlan:
        """One decision. ``active`` counts busy DeepSeek workers (real and filler); ``filler_busy`` and
        ``queued_filler`` count the filler population."""
        if not fill["enabled"]:
            self.filling = False
            return FillPlan(False, 0, "fill disabled", "disabled")
        if paused:
            self.filling = False
            return FillPlan(False, 0, "filler paused", "paused")
        max_workers = int(fill["max_workers"])
        population = filler_busy + queued_filler
        target = float(fill["target_billed_per_min"])
        low, high = fill["low_frac"] * target, fill["high_frac"] * target
        self.filling = filling_state(self.filling, billed_avg, target, fill["low_frac"], fill["high_frac"])
        if self.level is None:
            self.level = population
        self.level = min(self.level, max_workers)
        average = f"5-min average {billed_avg / 1e6:.2f}M/min"
        if not self.filling:
            self.level = min(population, max_workers)
            self.gate_since = None
            return FillPlan(False, 0, f"{average} at or above the {high / 1e6:.2f}M high mark; level held at "
                            f"{self.level} filler", "high-mark")
        if ready_real > 0:
            return FillPlan(True, 0, f"{ready_real} real DeepSeek jobs ready; they launch before filler", "real-ready")
        if gate is not None:
            if self.gate_since is None:
                self.gate_since = now
            if now - self.gate_since >= GATE_DECAY_S:
                self.level = min(population, max_workers)
            return FillPlan(True, 0, f"{gate}; level {self.level} held", "gate")
        self.gate_since = None
        room = max_workers - active - queued_filler
        if room <= 0:
            return FillPlan(True, 0, f"{average}; {active} busy DeepSeek workers at max_workers {max_workers}",
                            "at-max")
        if population < self.level:
            to_add = min(self.level - population, room)
            return FillPlan(True, to_add, f"{average}; replacing {to_add} finished filler toward level "
                            f"{self.level}", "refill")
        if queued_filler > 0:
            return FillPlan(True, 0, f"{average}; {queued_filler} filler job(s) from the last step still waiting "
                            "to launch", "step-wait")
        interval = float(fill["step_interval_s"])
        if self.last_step_at is not None and now - self.last_step_at < interval:
            return FillPlan(True, 0, f"{average}; next step in {self.last_step_at + interval - now:.0f} s",
                            "step-wait")
        to_add = min(int(fill["step_workers"]), room)
        self.last_step_at = now
        self.level = min(max_workers, population + to_add)
        return FillPlan(True, to_add, f"{average} below the {low / 1e6:.2f}M low mark; adding {to_add} filler "
                        f"workers to level {self.level}", "stepping")


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
    ps_text: str | None, laptop: headroom_lib.Headroom | None, laptop_cfg: dict, paused: bool,
    measured: int | None,
) -> Decision:
    """Read the store and return what the controller and pacing would do now. The controller keeps its
    own history; nothing else is written."""
    counts = job_counts(conn, now)
    direct = direct_ds_workers(ps_text) if fill["enabled"] else None
    active = counts["running_ds"] + (direct or 0)
    billed_avg = sum(row["billed"] for row in minute_rows(conn, now, WINDOW_MINUTES).values()) / WINDOW_MINUTES
    recent = recent_429(conn, now, fill["gate_429_window_s"])
    gate = gate_reason(status, recent, laptop, laptop_cfg, fill)
    if fill["enabled"] and direct is None and gate is None:
        gate = "worker count unknown (ps failed); filler waits"
    plan = controller.tick(
        now, fill=fill, paused=paused, billed_avg=billed_avg, active=active,
        filler_busy=counts["running_filler"], ready_real=counts["ready_real"], queued_filler=counts["queued_filler"],
        gate=gate,
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
            ps_text=process_table(),
            laptop=headroom_lib.reading_from_state(headroom_lib.load_state(home), now),
            laptop_cfg=broker_lib.load_config(home)["laptop"], paused=filler_lib.paused(conn),
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
