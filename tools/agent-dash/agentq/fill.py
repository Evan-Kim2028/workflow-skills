"""DeepSeek fill controller and launch pacing.

The controller holds the DeepSeek pool near ``fill.target_billed_per_min`` by submitting
filler jobs (``agentq.filler``) when the 5-minute billed average is low. Pacing caps how
fast DeepSeek jobs may start, from the uncached input the last minute already spent and
the first-request cost each recent launch will add.

Everything here is a pure function of values the caller reads, except the readers at the
bottom (``minute_rows``, ``recent_429``, ``job_counts``, ``measured_first_request``), which
read agentq.db and never write. The broker calls ``decide`` each tick and
``pacing_allowance`` at launch time; ``main`` prints one decision for a copy of the store.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
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

DEFAULT_FILL = {
    "enabled": False,
    "target_billed_per_min": 8_000_000,
    "max_workers": 64,
    "low_frac": 0.9,
    "high_frac": 0.97,
    "max_launch_per_tick": 4,
    "rate_min": 100_000,
    "rate_max": 250_000,
    "default_rate": 175_000,
    "gate_429_window_s": 120,
    "laptop_min_free_pct": 20,
    "laptop_max_load_per_cpu": 1.0,
}
DEFAULT_PACING = {
    "enabled": True,
    "uncached_limit": 400_000,
    "uncached_frac": 0.8,
    "first_request_uncached": 40_000,
    "measure_first_request": True,
    "measure_percentile": 75,
    "min_measured_jobs": 20,
    "measure_window_s": 86_400,
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


def per_worker_rate(billed_avg: float, active_avg: float | None, fill: dict) -> tuple[float, str]:
    """Billed tokens per minute one busy DeepSeek worker uses, clamped to [rate_min, rate_max].

    With no active-worker history the default rate stands in, and the source says so.
    """
    if not active_avg or active_avg <= 0:
        return float(fill["default_rate"]), "default"
    rate = billed_avg / active_avg
    return min(max(rate, float(fill["rate_min"])), float(fill["rate_max"])), "measured"


def desired_workers(target: float, rate: float, max_workers: int) -> int:
    """Busy DeepSeek workers needed to reach ``target`` at ``rate`` each, capped at ``max_workers``."""
    return min(int(max_workers), math.ceil(target / rate))


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


def gate_reason(status: dict | None, recent_429: int, laptop: tuple, fill: dict) -> str | None:
    """Why filler must wait now, or None. Any one of these holds filler: no proxy reading, DeepSeek
    requests blocked at the proxy, a DeepSeek 429 in the recent window, or the laptop short of memory
    or CPU. An unreadable gauge does not hold on its own."""
    if status is None:
        return "no proxy status; filler waits"
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


def plan_fill(
    *, enabled: bool, paused: bool, avg_billed: float, was_filling: bool, fill: dict, rate: float,
    running_ds: int, ready_real: int, queued_filler: int, gate: str | None,
) -> FillPlan:
    """The fill decision. Real DeepSeek work that is ready always goes first, so filler waits for it."""
    if not enabled:
        return FillPlan(False, 0, "fill disabled", "disabled")
    if paused:
        return FillPlan(False, 0, "filler paused", "paused")
    target = float(fill["target_billed_per_min"])
    low, high = fill["low_frac"] * target, fill["high_frac"] * target
    filling = filling_state(was_filling, avg_billed, target, fill["low_frac"], fill["high_frac"])
    average = f"5-min average {avg_billed / 1e6:.2f}M/min"
    if not filling:
        return FillPlan(False, 0, f"{average} at or above the {high / 1e6:.2f}M high mark; no filler", "high-mark")
    desired = desired_workers(target, rate, fill["max_workers"])
    if ready_real > 0:
        return FillPlan(True, 0, f"{ready_real} real DeepSeek jobs ready; they launch before filler", "real-ready")
    if gate is not None:
        return FillPlan(True, 0, gate, "gate")
    gap = desired - running_ds - ready_real
    if gap <= 0:
        return FillPlan(True, 0, f"{average}, below the {low / 1e6:.2f}M low mark; {running_ds} workers running covers desired {desired}", "covered")
    submit = min(int(fill["max_launch_per_tick"]), max(0, gap - queued_filler))
    if submit == 0:
        return FillPlan(True, 0, f"{average}; gap {gap} already covered by {queued_filler} queued filler jobs", "queued-covers")
    return FillPlan(True, submit, f"{average} below the {low / 1e6:.2f}M low mark; desired {desired}, running {running_ds}, submitting {submit}", "submitting")


def pacing_allowance(last_input: int, recent_launches: int, pacing: dict, measured: int | None) -> tuple[int, int]:
    """(DeepSeek launches allowed now, first-request cost used). The cost of each launch in the last minute
    is added to the last minute's uncached input, so a burst cannot pass the cap before its requests show."""
    est = int(measured) if measured else int(pacing["first_request_uncached"])
    cap = pacing["uncached_frac"] * pacing["uncached_limit"]
    projected = last_input + est * recent_launches
    return max(0, math.floor((cap - projected) / est)), est


def active_average(samples: deque, now: float, active_now: int, window_s: float) -> float | None:
    """Mean busy-worker count over the last ``window_s`` seconds, after recording ``active_now``."""
    samples.append((now, active_now))
    while samples and samples[0][0] < now - window_s:
        samples.popleft()
    values = [count for _, count in samples]
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class Decision:
    """One controller reading: the inputs it used and the plan it reached."""

    plan: FillPlan
    billed_avg: float
    rate: float
    rate_source: str
    active_now: int
    direct_ds: int | None
    running_ds: int
    ready_real: int
    queued_filler: int
    running_filler: int
    recent_429: int
    gate: str | None
    last_input: int
    pacing_left: int
    pacing_cost: int


def decide(
    conn, now: float, *, fill: dict, pacing: dict, status: dict | None, ps_text: str | None,
    laptop: tuple, was_filling: bool, samples: deque, recent_launches: int, measured: int | None,
    paused: bool,
) -> Decision:
    """Read the store and return what the controller would do now. Writes nothing; ``samples`` is the
    caller's busy-worker history and gains this tick's reading."""
    rows = minute_rows(conn, now, WINDOW_MINUTES)
    billed_avg = sum(row["billed"] for row in rows.values()) / WINDOW_MINUTES
    counts = job_counts(conn, now)
    direct = direct_ds_workers(ps_text)
    active_now = counts["running_ds"] + (direct or 0)
    avg_active = active_average(samples, now, active_now, WINDOW_MINUTES * MINUTE_S)
    rate, source = per_worker_rate(billed_avg, avg_active, fill)
    recent = recent_429(conn, now, fill["gate_429_window_s"])
    gate = gate_reason(status, recent, laptop, fill)
    if direct is None and fill["enabled"] and gate is None:
        gate = "worker count unknown (ps failed); filler waits"
    plan = plan_fill(
        enabled=bool(fill["enabled"]), paused=paused, avg_billed=billed_avg, was_filling=was_filling,
        fill=fill, rate=rate, running_ds=active_now, ready_real=counts["ready_real"],
        queued_filler=counts["queued_filler"], gate=gate,
    )
    last_input = last_minute_input(conn, now)
    left, cost = pacing_allowance(last_input, recent_launches, pacing, measured)
    return Decision(
        plan=plan, billed_avg=billed_avg, rate=rate, rate_source=source, active_now=active_now,
        direct_ds=direct, running_ds=counts["running_ds"], ready_real=counts["ready_real"],
        queued_filler=counts["queued_filler"], running_filler=counts["running_filler"],
        recent_429=recent, gate=gate, last_input=last_input, pacing_left=left, pacing_cost=cost,
    )


def minute_stamp(ts: float) -> str:
    """The UTC minute of ``ts`` in proxy_minutes' format."""
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


def minute_rows(conn, now: float, minutes: int) -> dict[str, dict]:
    """DeepSeek billed tokens per minute for the last ``minutes`` complete UTC minutes, keyed by minute stamp.
    Billed is input + cache_read + output, as Morph counts it. A minute with no rows is absent."""
    end = math.floor(now / 60) * 60
    found = conn.execute(
        """SELECT minute, SUM(input) AS input, SUM(cache_read) AS cache_read, SUM(output) AS output
           FROM proxy_minutes WHERE model LIKE ? AND minute >= ? AND minute < ? GROUP BY minute""",
        (DS_MODEL_LIKE, minute_stamp(end - MINUTE_S * minutes), minute_stamp(end)),
    ).fetchall()
    return {
        row["minute"]: {
            "input": int(row["input"] or 0),
            "billed": int((row["input"] or 0) + (row["cache_read"] or 0) + (row["output"] or 0)),
        }
        for row in found
    }


def last_minute_input(conn, now: float) -> int:
    """DeepSeek uncached input in the last complete UTC minute (0 when none was recorded)."""
    return minute_rows(conn, now, 1).get(minute_stamp(math.floor(now / 60) * 60 - MINUTE_S), {}).get("input", 0)


def recent_429(conn, now: float, seconds: float) -> int:
    """DeepSeek upstream 429s in the minutes that overlap the last ``seconds``."""
    row = conn.execute(
        "SELECT COALESCE(SUM(r429), 0) FROM proxy_minutes WHERE model LIKE ? AND minute >= ?",
        (DS_MODEL_LIKE, minute_stamp(now - seconds)),
    ).fetchone()
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

    A job with one request has that request's uncached input as its whole input, so the percentile of
    those is the launch cost. None when fewer than ``min_measured_jobs`` jobs have been seen in the window.
    The percentile (75 by default) leans high, because short jobs would understate a full session start.
    """
    rows = conn.execute(
        "SELECT input FROM proxy_jobs WHERE model LIKE ? AND req = 1 AND input > 0 AND last_ts >= ?",
        (DS_MODEL_LIKE, now - pacing["measure_window_s"]),
    ).fetchall()
    values = sorted(int(row[0]) for row in rows)
    if len(values) < pacing["min_measured_jobs"]:
        return None
    rank = max(1, math.ceil(pacing["measure_percentile"] / 100 * len(values)))
    low, high = MEASURED_CLAMP
    return min(max(values[rank - 1], low), high)


def main(argv=None, out=None) -> int:
    """Print one controller reading from an agentq home: what filler and pacing would do now, and why.

    Read-only: it reads the store, the live proxy status and the laptop gauges, and writes nothing.
    Busy-worker history is not kept by a one-shot read, so the average is the current count.
    Run it on a copy of the store when the live broker is running, so the copy is what gets migrated.
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
            conn, now, fill=fill, pacing=pacing, status=broker_lib.proxy_status(),
            ps_text=process_table(), laptop=broker_lib.Gauges().read(15.0),
            was_filling=False, samples=deque(), recent_launches=0, measured=measured,
            paused=filler_lib.paused(conn),
        )
        due = filler_lib.next_target(conn, targets, head_of=filler_lib.module_head)
    finally:
        conn.close()
    plan = decision.plan
    print(f"fill enabled={fill['enabled']} target={fill['target_billed_per_min']:,}/min "
          f"low={fill['low_frac'] * fill['target_billed_per_min']:,.0f} high={fill['high_frac'] * fill['target_billed_per_min']:,.0f}", file=out)
    print(f"5-min billed average {decision.billed_avg:,.0f}/min; per-worker rate {decision.rate:,.0f} ({decision.rate_source})", file=out)
    print(f"DeepSeek busy: queue {decision.running_ds} (filler {decision.running_filler}), direct {decision.direct_ds}; "
          f"ready real {decision.ready_real}; queued filler {decision.queued_filler}", file=out)
    print(f"Morph gate: {decision.gate or 'open'}; DeepSeek 429s in gate window {decision.recent_429}", file=out)
    cost_source = "measured" if measured else "default"
    print(f"pacing: last-minute uncached {decision.last_input:,}, launch cost {decision.pacing_cost:,} ({cost_source}), "
          f"launches allowed now {decision.pacing_left}", file=out)
    print(f"next filler target: {due[0].key} at {due[1][:12]}" if due else "next filler target: none due", file=out)
    print(f"ANSWER: would submit {plan.submit} filler job(s) now; filling={plan.filling}; {plan.reason}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
