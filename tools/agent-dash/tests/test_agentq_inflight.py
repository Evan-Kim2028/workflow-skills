"""The read-only ``agentq inflight report`` over fixture stores: calendar-minute denominators,
idle minutes, A/B arm schedule, window boundaries, and the CLI answer."""

import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq import cli, inflight
from agentq.proxy import MINUTE_ROW_COLUMNS, minute_stamp, write_minute_rows

T0 = 1_700_000_040 // 60 * 60
DS = "morph-dsv41flash-1017"
SHADOW = lambda t: "shadow"  # noqa: E731
ENFORCE = lambda t: "enforce"  # noqa: E731


def row(at: float, *, arm: str | None = "enforce", req: int = 30, billed: int = 3_000_000, ttfb: int = 6000,
        over: int = 0, limit: int | None = 28, in_flight: int = 10, model: str = DS) -> tuple:
    values = {
        "minute": minute_stamp(at), "model": model, "req": req, "ok": req, "r429": 0, "input": billed,
        "cache_read": 0, "output": 0, "uncached_est": 0, "wait_p50_ms": 0, "wait_p95_ms": 0, "target": 0.98,
        "upstream_ms_sum": 0, "upstream_n": req, "upstream_p50_ms": ttfb + 1000, "upstream_p95_ms": ttfb + 2000,
        "ttfb_p50_ms": ttfb, "in_flight_max": in_flight, "inflight_limit": limit, "inflight_mode": "enforce",
        "inflight_arm": arm, "inflight_over": over, "ttfb_le_p50_ms": None, "ttfb_gt_p50_ms": None,
    }
    return tuple(values[name] for name in MINUTE_ROW_COLUMNS)


class StoreMixin:
    def store(self, rows: list[tuple]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "agentq.db"
        write_minute_rows(path, rows)
        return path


class ReportWindowTest(StoreMixin, unittest.TestCase):

    def test_the_reviewer_probe_thirty_requests_twice_in_five_minutes_is_twelve_a_minute(self) -> None:
        path = self.store([row(T0, req=30), row(T0 + 4 * 60, req=30)])
        _, summary = inflight.report(path, 5 / 60, schedule=ENFORCE, now=T0 + 5 * 60 + 20)
        self.assertEqual(summary["enforce"]["minutes"], 5)
        self.assertEqual(summary["enforce"]["idle_minutes"], 3)
        self.assertEqual(summary["enforce"]["req_per_min"], 12.0)

    def test_a_row_five_minutes_apart_is_inside_a_six_minute_window_and_outside_a_five_minute_one(self) -> None:
        path = self.store([row(T0, req=30), row(T0 + 5 * 60, req=30)])
        _, six = inflight.report(path, 6 / 60, schedule=ENFORCE, now=T0 + 6 * 60 + 20)
        self.assertEqual(six["enforce"]["req_per_min"], 10.0)
        _, five = inflight.report(path, 5 / 60, schedule=ENFORCE, now=T0 + 5 * 60 + 20)
        self.assertEqual(five["enforce"]["req_per_min"], 6.0)

    def test_the_minute_in_progress_is_never_counted(self) -> None:
        path = self.store([row(T0, req=30), row(T0 + 60, req=30), row(T0 + 120, req=999)])
        _, summary = inflight.report(path, 2 / 60, schedule=ENFORCE, now=T0 + 120 + 20)
        self.assertEqual(summary["enforce"]["minutes"], 2)
        self.assertEqual(summary["enforce"]["req_per_min"], 30.0)

    def test_over_limit_share_divides_by_every_calendar_minute(self) -> None:
        rows = [row(T0 + k * 60, over=2 if k < 2 else 0) for k in range(10)]
        _, summary = inflight.report(self.store(rows), 10 / 60, schedule=ENFORCE, now=T0 + 10 * 60 + 5)
        self.assertEqual(summary["enforce"]["over_limit_minute_frac"], 0.2)

    def test_latency_medians_use_only_minutes_with_a_row(self) -> None:
        rows = [row(T0, ttfb=4000), row(T0 + 120, ttfb=8000)]
        _, summary = inflight.report(self.store(rows), 4 / 60, schedule=ENFORCE, now=T0 + 4 * 60 + 5)
        self.assertEqual(summary["enforce"]["ttfb_p50_ms"], 6000)

    def test_idle_minutes_take_the_arm_of_the_ab_schedule(self) -> None:
        period = 600
        aligned = T0 // 1200 * 1200  # first ten minutes are an enforce period, the next ten an off period

        def schedule(t: float) -> str:
            return "enforce" if int(t // period) % 2 == 0 else "off"

        path = self.store([row(aligned + k * 60, arm=None, req=30) for k in range(5)])
        _, summary = inflight.report(path, 20 / 60, schedule=schedule, now=aligned + 20 * 60 + 5)
        self.assertEqual(summary["enforce"]["minutes"], 10)
        self.assertEqual(summary["enforce"]["idle_minutes"], 5)
        self.assertEqual(summary["enforce"]["req_per_min"], 15.0)
        self.assertEqual(summary["off"]["minutes"], 10)
        self.assertEqual(summary["off"]["idle_minutes"], 10)
        self.assertEqual(summary["off"]["req_per_min"], 0.0)

    def test_minutes_before_the_first_instrumented_minute_are_excluded(self) -> None:
        legacy = row(T0, arm=None, req=500)
        instrumented = row(T0 + 10 * 60, arm="shadow", req=30)
        path = self.store([legacy, instrumented])
        text, summary = inflight.report(path, 20 / 60, schedule=SHADOW, now=T0 + 20 * 60 + 5)
        self.assertEqual(summary["shadow"]["minutes"], 10)
        self.assertEqual(summary["shadow"]["req_per_min"], 3.0)
        self.assertIn("instrumented from", text)

    def test_shadow_schedule_puts_every_idle_minute_in_the_shadow_arm(self) -> None:
        path = self.store([row(T0, arm=None, req=30)])
        _, summary = inflight.report(path, 3 / 60, schedule=SHADOW, now=T0 + 3 * 60 + 5)
        self.assertEqual(list(summary), ["shadow"])
        self.assertEqual(summary["shadow"]["minutes"], 3)

    def test_an_empty_store_is_reported_not_crashed(self) -> None:
        text, summary = inflight.report(Path("/nonexistent/agentq.db"), 1, schedule=SHADOW, now=T0 + 60)
        self.assertEqual(summary, {})
        self.assertEqual(text, "no DeepSeek minutes in the window")


class InflightCliTest(StoreMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.path = self.store([row(T0 + k * 60, req=30) for k in range(3)])
        self.home = self.path.parent

    def run_cli(self, *args: str, now: float = T0 + 3 * 60 + 5) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(["inflight", "report", *args], env={"AGENTQ_HOME": str(self.home)}, out=out, now=lambda: now)
        return code, out.getvalue()

    def test_the_cli_prints_the_table_and_answers_once(self) -> None:
        code, text = self.run_cli("--hours", "1")
        self.assertEqual(code, 0)
        self.assertIn("enforce", text)
        self.assertEqual(text.count("ANSWER:"), 1)

    def test_an_empty_window_is_a_negative_fact(self) -> None:
        out = io.StringIO()
        code = cli.main(["inflight", "report", "--hours", "1"], env={"AGENTQ_HOME": str(self.home / "none")},
                        out=out, now=lambda: T0 + 60)
        self.assertEqual(code, 1)
        self.assertIn("ANSWER: no DeepSeek minutes recorded", out.getvalue())

    def test_the_report_never_writes(self) -> None:
        before = self.path.stat().st_mtime_ns
        inflight.report(self.path, 1, schedule=SHADOW, now=T0 + 3 * 60 + 5)
        connection = sqlite3.connect(self.path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM proxy_minutes").fetchone()[0], 3)
        connection.close()
        self.assertEqual(self.path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
