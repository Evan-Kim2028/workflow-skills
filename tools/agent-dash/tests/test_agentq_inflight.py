"""The read-only ``agentq inflight report`` over a fixture store: arms, summaries and the table."""

import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq import cli, inflight
from agentq.proxy import MINUTE_ROW_COLUMNS, minute_stamp, write_minute_rows

NOW = 1_700_000_000.0 // 60 * 60
DS = "morph-dsv41flash-1017"


def row(minute_offset: int, *, arm: str | None, billed: int, req: int, ttfb: int, upstream: int, limit=None, over=0,
        le=None, gt=None, in_flight=10, mode="enforce", model=DS) -> tuple:
    minute = minute_stamp(NOW - minute_offset * 60)
    values = {
        "minute": minute, "model": model, "req": req, "ok": req, "r429": 0, "input": billed, "cache_read": 0,
        "output": 0, "uncached_est": 0, "wait_p50_ms": 0, "wait_p95_ms": 0, "target": 0.98,
        "upstream_ms_sum": upstream * req, "upstream_n": req, "upstream_p50_ms": upstream, "upstream_p95_ms": upstream,
        "ttfb_p50_ms": ttfb, "in_flight_max": in_flight, "inflight_limit": limit, "inflight_mode": mode,
        "inflight_arm": arm, "inflight_over": over, "ttfb_le_p50_ms": le, "ttfb_gt_p50_ms": gt,
    }
    return tuple(values[name] for name in MINUTE_ROW_COLUMNS)


class InflightReportTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / "agentq.db"
        rows = [
            row(1, arm="enforce", billed=5_000_000, req=40, ttfb=6000, upstream=18000, limit=28, over=2, le=5500, gt=7000),
            row(2, arm="enforce", billed=5_200_000, req=42, ttfb=6400, upstream=19000, limit=28, over=0, le=6000, gt=None),
            row(3, arm="off", billed=5_100_000, req=50, ttfb=11000, upstream=24000, limit=28, over=4, le=None, gt=11000),
            row(4, arm="off", billed=5_000_000, req=48, ttfb=12000, upstream=25000, limit=28, over=3, le=None, gt=12000),
            row(5, arm=None, billed=3_000_000, req=20, ttfb=5000, upstream=12000, limit=None, mode=None),
        ]
        write_minute_rows(self.path, rows)

    def test_rows_are_grouped_by_the_arm_each_minute_fell_in(self) -> None:
        summary = inflight.summarize(inflight.read_minutes(self.path, NOW - 3600, NOW))
        self.assertEqual(sorted(summary), ["enforce", "none", "off"])
        self.assertEqual(summary["enforce"]["minutes"], 2)
        self.assertEqual(summary["enforce"]["billed_per_min"], 5_100_000)
        self.assertEqual(summary["off"]["ttfb_p50_ms"], 11500)
        self.assertEqual(summary["enforce"]["over_limit_minute_frac"], 0.5)
        self.assertEqual(summary["off"]["over_limit_minute_frac"], 1.0)
        self.assertEqual(summary["enforce"]["ttfb_le_p50_ms"], 5750)
        self.assertIsNone(summary["off"]["ttfb_le_p50_ms"])

    def test_the_table_has_one_line_per_arm(self) -> None:
        text, _ = inflight.report(self.path, 1, now=NOW)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("arm"))
        self.assertEqual([line.split()[0] for line in lines[1:]], ["enforce", "none", "off"])

    def test_the_cli_prints_the_table_and_answers_once(self) -> None:
        out = io.StringIO()
        code = cli.main(["inflight", "report", "--hours", "1"], env={"AGENTQ_HOME": str(self.home)}, out=out,
                        now=lambda: NOW)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("enforce", text)
        self.assertEqual(text.count("ANSWER:"), 1)

    def test_an_empty_window_is_a_negative_fact_not_a_crash(self) -> None:
        out = io.StringIO()
        code = cli.main(["inflight", "report", "--hours", "1"], env={"AGENTQ_HOME": str(self.home / "none")}, out=out,
                        now=lambda: NOW)
        self.assertEqual(code, 1)
        self.assertIn("ANSWER: no DeepSeek minutes recorded", out.getvalue())

    def test_the_report_never_writes(self) -> None:
        before = self.path.stat().st_mtime_ns
        inflight.report(self.path, 1, now=NOW)
        connection = sqlite3.connect(self.path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM proxy_minutes").fetchone()[0], 5)
        connection.close()
        self.assertEqual(self.path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
