"""The read-only ``agentq inflight report`` over fixture stores. Arms come only from the proxy's per-minute
records; traffic rows never define a minute, and no schedule is consulted."""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq import cli, inflight
from agentq.proxy import MINUTE_ROW_COLUMNS, minute_stamp, write_inflight_minute_rows, write_minute_rows

T0 = 1_700_000_040 // 60 * 60
DS = "morph-dsv41flash-1017"


def traffic(at: float, *, req: int = 30, billed: int = 3_000_000, ttfb: int = 6000, over: int = 0,
            arm: str | None = "enforce", limit: int | None = 28, in_flight: int = 10) -> tuple:
    values = {
        "minute": minute_stamp(at), "model": DS, "req": req, "ok": req, "r429": 0, "input": billed,
        "cache_read": 0, "output": 0, "uncached_est": 0, "wait_p50_ms": 0, "wait_p95_ms": 0, "target": 0.98,
        "upstream_ms_sum": 0, "upstream_n": req, "upstream_p50_ms": ttfb + 1000, "upstream_p95_ms": ttfb + 2000,
        "ttfb_p50_ms": ttfb, "in_flight_max": in_flight, "inflight_limit": limit, "inflight_mode": "enforce",
        "inflight_arm": arm, "inflight_over": over, "ttfb_le_p50_ms": None, "ttfb_gt_p50_ms": None,
    }
    return tuple(values[name] for name in MINUTE_ROW_COLUMNS)


def record(at: float, arm: str = "shadow", *, mode: str | None = None, limit: int | None = 28) -> tuple:
    return (minute_stamp(at), mode or arm, arm, limit, int(arm == "enforce"))


class StoreMixin:
    def store(self, traffic_rows: list[tuple] = (), records: list[tuple] = ()) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "agentq.db"
        if traffic_rows:
            write_minute_rows(path, list(traffic_rows))
        if records:
            write_inflight_minute_rows(path, list(records))
        return path

    def run_cli(self, home: Path, *args: str, now: float) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(["inflight", "report", *args], env={"AGENTQ_HOME": str(home)}, out=out, now=lambda: now)
        return code, out.getvalue()


class ReportAccountingTest(StoreMixin, unittest.TestCase):
    def test_probe_a_idle_minutes_across_the_whole_hour_count_when_recorded(self) -> None:
        """Rows only one and six minutes before now, but the limiter ran all hour: 60 minutes, not 6."""
        now = T0 + 60 * 60 + 20
        records = [record(T0 + k * 60, "shadow") for k in range(60)]
        path = self.store([traffic(T0 + 59 * 60, req=60, billed=600), traffic(T0 + 54 * 60, req=60, billed=600)], records)
        _, summary = inflight.report(path, 1, now=now)
        shadow = summary["shadow"]
        self.assertEqual(shadow["minutes"], 60)
        self.assertEqual(shadow["active_minutes"], 2)
        self.assertEqual(shadow["idle_minutes"], 58)
        self.assertEqual(shadow["req_per_min"], 2.0)
        self.assertEqual(shadow["billed_per_min"], 20)

    def test_probe_b_a_legacy_store_with_no_records_reports_nothing(self) -> None:
        """A pre-limiter row with arm null, and no records, must not become a shadow experiment."""
        path = self.store([traffic(T0, req=30, arm=None, limit=None)], records=[])
        text, summary = inflight.report(path, 1, now=T0 + 3 * 60 + 5)
        self.assertEqual(summary, {})
        self.assertIn("no recorded minutes", text)

    def test_probe_b_traffic_outside_the_recorded_minutes_is_ignored(self) -> None:
        path = self.store(
            [traffic(T0, req=500, arm=None, limit=None), traffic(T0 + 10 * 60, req=30)],
            [record(T0 + 10 * 60 + k * 60, "shadow") for k in range(3)],
        )
        _, summary = inflight.report(path, 20 / 60, now=T0 + 20 * 60 + 5)
        self.assertEqual(summary["shadow"]["minutes"], 3)
        self.assertEqual(summary["shadow"]["req_per_min"], 10.0)

    def test_probe_c_a_historical_arm_survives_a_later_configuration_change(self) -> None:
        """Ten enforce minutes and ten off minutes were recorded. The current config now says off: the record wins."""
        now = T0 + 20 * 60 + 5
        records = [record(T0 + k * 60, "enforce" if k < 10 else "off") for k in range(20)]
        path = self.store([traffic(T0 + k * 60, req=60, arm="enforce") for k in range(10)], records)
        _, summary = inflight.report(path, 20 / 60, now=now)
        self.assertEqual(summary["enforce"]["minutes"], 10)
        self.assertEqual(summary["off"]["minutes"], 10)
        self.assertEqual(summary["enforce"]["req_per_min"], 60.0)
        self.assertEqual(summary["off"]["req_per_min"], 0.0)

    def test_a_proxy_down_gap_is_excluded_and_counted(self) -> None:
        records = [record(T0 + k * 60, "shadow") for k in list(range(10)) + list(range(20, 30))]
        path = self.store([traffic(T0 + 5 * 60, req=30)], records)
        text, summary = inflight.report(path, 30 / 60, now=T0 + 30 * 60 + 5)
        self.assertEqual(summary["shadow"]["minutes"], 20)
        self.assertIn("20 recorded minutes; 10 minutes excluded", text)

    def test_the_minute_in_progress_is_never_counted(self) -> None:
        records = [record(T0 + k * 60, "shadow") for k in range(3)]
        path = self.store([traffic(T0 + 120, req=999)], records)
        _, summary = inflight.report(path, 2 / 60, now=T0 + 120 + 20)
        self.assertEqual(summary["shadow"]["minutes"], 2)
        self.assertEqual(summary["shadow"]["req_per_min"], 0.0)

    def test_over_limit_share_divides_by_recorded_minutes(self) -> None:
        records = [record(T0 + k * 60, "enforce") for k in range(10)]
        path = self.store([traffic(T0 + k * 60, over=2, arm="enforce") for k in range(2)], records)
        _, summary = inflight.report(path, 10 / 60, now=T0 + 10 * 60 + 5)
        self.assertEqual(summary["enforce"]["over_limit_minute_frac"], 0.2)

    def test_latency_medians_use_only_minutes_with_traffic(self) -> None:
        records = [record(T0 + k * 60, "enforce") for k in range(4)]
        path = self.store([traffic(T0, ttfb=4000, arm="enforce"), traffic(T0 + 120, ttfb=8000, arm="enforce")], records)
        _, summary = inflight.report(path, 4 / 60, now=T0 + 4 * 60 + 5)
        self.assertEqual(summary["enforce"]["ttfb_p50_ms"], 6000)

    def test_an_empty_store_is_reported_not_crashed(self) -> None:
        text, summary = inflight.report(Path("/nonexistent/agentq.db"), 1, now=T0 + 60)
        self.assertEqual(summary, {})
        self.assertIn("no recorded minutes", text)

    def test_the_report_never_writes(self) -> None:
        path = self.store([traffic(T0)], [record(T0, "shadow")])
        before = path.stat().st_mtime_ns
        inflight.report(path, 1, now=T0 + 3 * 60 + 5)
        connection = sqlite3.connect(path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM inflight_minutes").fetchone()[0], 1)
        connection.close()
        self.assertEqual(path.stat().st_mtime_ns, before)


class InflightCliTest(StoreMixin, unittest.TestCase):
    def test_the_cli_prints_the_window_line_and_answers_once(self) -> None:
        path = self.store([traffic(T0)], [record(T0 + k * 60, "shadow") for k in range(3)])
        code, text = self.run_cli(path.parent, "--hours", "1", now=T0 + 3 * 60 + 5)
        self.assertEqual(code, 0)
        self.assertIn("3 recorded minutes; 57 minutes excluded", text)
        self.assertEqual(text.count("ANSWER:"), 1)

    def test_probe_c_through_the_cli_ignores_a_changed_proxy_json(self) -> None:
        now = T0 + 20 * 60 + 5
        records = [record(T0 + k * 60, "enforce" if k < 10 else "off") for k in range(20)]
        path = self.store([traffic(T0 + k * 60, req=60, arm="enforce") for k in range(10)], records)
        (path.parent / "proxy.json").write_text(json.dumps({"inflight": {"mode": "off"}}))
        code, text = self.run_cli(path.parent, "--hours", "20", now=now)
        self.assertEqual(code, 0)
        enforce = [line for line in text.splitlines() if line.startswith("enforce")][0]
        self.assertIn(" 10 ", enforce)
        self.assertIn(" 60.0 ", enforce)

    def test_no_recorded_minutes_is_a_negative_fact(self) -> None:
        path = self.store([traffic(T0, arm=None, limit=None)])
        code, text = self.run_cli(path.parent, "--hours", "1", now=T0 + 60)
        self.assertEqual(code, 1)
        self.assertIn("ANSWER: no recorded DeepSeek limiter minutes", text)


if __name__ == "__main__":
    unittest.main()
