"""Storage budget: the daily footprint sample, its history and its projection.

No real store, no real tree: the metrics body is a fixture, the component
walkers are pointed at a temp dir, and the history file is a temp file.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import storagebudget as sb  # noqa: E402

GIB = 1024 ** 3


class _Usage:
    def __init__(self, total, used, free):
        self.total, self.used, self.free = total, used, free

METRICS = """# TYPE vl_compressed_data_size_bytes gauge
vl_compressed_data_size_bytes{type="inmemory"} 0
vl_compressed_data_size_bytes{type="small"} 1.2e+09
vl_compressed_data_size_bytes{type="big"} 1.4e+10
# TYPE vl_uncompressed_data_size_bytes gauge
vl_uncompressed_data_size_bytes{type="inmemory"} 0
vl_uncompressed_data_size_bytes{type="small"} 2.0e+09
vl_uncompressed_data_size_bytes{type="big"} 9.4e+10
# TYPE vl_free_disk_space_bytes gauge
vl_free_disk_space_bytes{path="/var/lib/victorialogs"} 3.5e+11
"""


class ParseMetricsTest(unittest.TestCase):
    def test_sums_per_storage_type_and_reads_the_path(self):
        out = sb.parse_metrics(METRICS)
        self.assertEqual(out["compressed"], 1_200_000_000 + 14_000_000_000)
        self.assertEqual(out["uncompressed"], 2_000_000_000 + 94_000_000_000)
        self.assertEqual(out["free_disk"], 350_000_000_000)
        self.assertEqual(out["data_path"], "/var/lib/victorialogs")

    def test_a_body_without_the_counters_is_empty(self):
        self.assertEqual(sb.parse_metrics("go_goroutines 12\n"), {})
        self.assertEqual(sb.parse_metrics(""), {})

    def test_a_down_store_is_not_an_error(self):
        with mock.patch.object(sb.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertEqual(sb.read_metrics("http://127.0.0.1:19428"), {})

    def test_a_non_loopback_url_is_never_called(self):
        with mock.patch.object(sb.urllib.request, "urlopen") as call:
            self.assertEqual(sb.read_metrics("http://example.com:19428"), {})
        call.assert_not_called()


class FormatTest(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(sb.human_bytes(0), "0 B")
        self.assertEqual(sb.human_bytes(1536), "1.5 KiB")
        self.assertEqual(sb.human_bytes(3 * GIB), "3.0 GiB")
        self.assertEqual(sb.human_bytes(None), "-")

    def test_human_days(self):
        self.assertEqual(sb.human_days(None), "-")
        self.assertEqual(sb.human_days(-1), "past")
        self.assertEqual(sb.human_days(400), "400 d")
        self.assertEqual(sb.human_days(812), "2.2 y")


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.dest = self.home / "sessions"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.scratch = self.home / "scratch"
        self.agentq = self.home / "agentq"
        self.originals = self.home / "originals"
        for path in (self.scratch, self.agentq / "jobs", self.originals):
            path.mkdir(parents=True, exist_ok=True)

    def _write(self, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def test_walk_counts_every_component(self):
        self._write(self.dest / "archive" / "pi" / "proj" / "a.0.tar.zst", 1000)
        self._write(self.dest / "archive" / "claude" / "b.0.tar.zst", 500)
        self._write(self.dest / "state" / "manifest.jsonl", 300)
        self._write(self.dest / "state" / "manifest-segments" / "seg-1-0000.jsonl.zst", 200)
        self._write(self.state / "log-archive" / "x.zst", 400)
        self._write(self.agentq / "jobs" / "j1" / "out.log", 700)
        self._write(self.scratch / "ws" / "big.bin", 900)
        self._write(self.originals / "s" / "a.jsonl", 100)
        with mock.patch.object(sb, "_archive_dest", lambda cfg=None: self.dest), mock.patch.object(
            sb, "_originals_roots", lambda: [str(self.originals)]
        ), mock.patch.object(sb, "_scratch_root", lambda: self.scratch), mock.patch.object(
            sb, "_agentq_home", lambda: self.agentq
        ), mock.patch.object(sb, "_state_dir", lambda cfg=None: self.state):
            sample = sb.measure(metrics={"compressed": 10_000, "uncompressed": 60_000}, now=1000.0)
        comp = sample["components"]
        self.assertEqual(comp["vlogs_compressed"]["bytes"], 10_000)
        self.assertEqual(comp["vlogs_uncompressed"]["bytes"], 60_000)
        self.assertEqual(comp["session_archive"], {"bytes": 1500, "units": 2})
        self.assertEqual(comp["session_manifest"], {"bytes": 500, "units": 2})
        self.assertEqual(comp["log_archive"], {"bytes": 400, "units": 1})
        self.assertEqual(comp["agentq_jobs"], {"bytes": 700, "units": 1})
        self.assertEqual(comp["scratch"], {"bytes": 900, "units": 1})
        self.assertEqual(comp["session_originals"], {"bytes": 100, "units": 1})
        self.assertEqual(sample["footprint"], 10_000 + 1500 + 500 + 400 + 700 + 900 + 100)
        self.assertGreater(sample["disk"]["total"], 0)
        self.assertEqual(sample["iso"], "1970-01-01T00:16:40Z")

    def test_walk_false_skips_the_trees(self):
        sample = sb.measure(metrics={"compressed": 5, "uncompressed": 9}, walk=False, now=1.0)
        self.assertEqual(sample["components"]["session_archive"]["bytes"], 0)
        self.assertEqual(sample["footprint"], 5)

    def test_a_missing_tree_is_zero_not_an_error(self):
        self.assertEqual(sb._dir_bytes(self.home / "nope"), (0, 0))
        self.assertEqual(sb._file_bytes(self.home / "nope"), (0, 0))
        self.assertEqual(sb._glob_bytes(self.home / "nope", "**/*.zst"), (0, 0))

    def test_a_symlink_is_not_followed(self):
        self._write(self.home / "real" / "a.bin", 100)
        os.symlink(self.home / "real", self.home / "link")
        self.assertEqual(sb._dir_bytes(self.home / "link"), (0, 0))


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "storage-budget.jsonl"

    def sample(self, ts, compressed, used=100, total=1000):
        return {
            "ts": ts,
            "iso": sb.iso(ts),
            "store": True,
            "disk": {"path": "/d", "total": total, "used": used, "free": total - used, "pct": 100.0 * used / total},
            "components": {
                name: {"bytes": compressed if name == "vlogs_compressed" else 0, "units": 0}
                for name, _label in sb.COMPONENTS
            },
            "footprint": compressed,
        }

    def test_append_and_read_back(self):
        sb.append_history(self.path, self.sample(1000.0, 10))
        sb.append_history(self.path, self.sample(2000.0, 20))
        self.assertEqual(len(sb.load_history(self.path)), 2)
        self.assertEqual(sb.last_sample(self.path)["footprint"], 20)

    def test_a_truncated_tail_line_is_skipped(self):
        sb.append_history(self.path, self.sample(1000.0, 10))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"ts": 2000, "foot')
        self.assertEqual(sb.last_sample(self.path)["footprint"], 10)

    def test_no_history_is_none(self):
        self.assertIsNone(sb.last_sample(self.path))
        self.assertEqual(sb.load_history(self.path), [])

    def test_growth_needs_two_samples(self):
        rows = [self.sample(1000.0, 10)]
        self.assertIsNone(sb.growth(rows, 1000.0)["footprint"])
        rows.append(self.sample(1000.0 + 2 * 86400, 10 + 2 * GIB))
        rates = sb.growth(rows, 1000.0 + 2 * 86400)
        self.assertAlmostEqual(rates["footprint"], GIB, delta=1.0)
        self.assertEqual(rates["span_days"], 2.0)

    def test_growth_ignores_samples_outside_the_window(self):
        now = 100 * 86400.0
        rows = [self.sample(now - 30 * 86400, 0), self.sample(now - 86400, 5 * GIB), self.sample(now, 6 * GIB)]
        rates = sb.growth(rows, now)
        self.assertAlmostEqual(rates["footprint"], GIB, delta=1.0)

    def test_a_same_hour_pair_reports_unknown(self):
        rows = [self.sample(1000.0, 0), self.sample(1000.0 + 60, 10)]
        self.assertIsNone(sb.growth(rows, 1060.0)["footprint"])

    def test_project_and_state(self):
        sample = self.sample(1000.0, 0, used=600, total=1000)
        rates = {"footprint": 10.0}
        proj = sb.project(sample, rates)
        self.assertEqual(proj["days"]["70"], 10.0)
        self.assertEqual(proj["days"]["78"], 18.0)
        self.assertEqual(proj["days"]["85"], 25.0)
        self.assertEqual(proj["days"]["80"], 20.0)
        self.assertEqual(sb.state_of(sample, proj), "alert")
        self.assertEqual(sb.state_of(sample, sb.project(sample, {"footprint": 0.6})), "warn")
        self.assertEqual(sb.state_of(sample, sb.project(sample, {"footprint": 0.1})), "ok")
        self.assertEqual(sb.state_of(self.sample(1.0, 0, used=700, total=1000), {"days": {}}), "warn")
        self.assertEqual(sb.state_of(self.sample(1.0, 0, used=780, total=1000), {"days": {}}), "alert")
        self.assertEqual(sb.state_of(sample, {"days": {"80": 100.0}}), "alert")
        self.assertEqual(sb.state_of(sample, {"days": {"80": 200.0}}), "warn")
        self.assertEqual(sb.state_of(sample, {"days": {"80": None}}), "ok")

    def test_a_flat_rate_projects_nothing(self):
        proj = sb.project(self.sample(1.0, 0), {"footprint": 0.0})
        self.assertIsNone(proj["days"]["80"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "storage-budget.jsonl"

    def test_run_records_and_reports(self):
        buf = io.StringIO()
        rc = sb.run(
            {"history": str(self.path), "metrics": {"compressed": 2 * GIB, "uncompressed": 9 * GIB}, "walk": False},
            out=buf,
            now=1000.0,
        )
        self.assertEqual(rc, 0)
        self.assertIn("storage budget", buf.getvalue())
        self.assertIn("VictoriaLogs store (compressed)", buf.getvalue())
        self.assertEqual(len(sb.load_history(self.path)), 1)

    def test_json_output(self):
        buf = io.StringIO()
        sb.run(
            {"history": str(self.path), "metrics": {"compressed": 1, "uncompressed": 2}, "walk": False, "json": True},
            out=buf,
            now=1000.0,
        )
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["sample"]["components"]["vlogs_compressed"]["bytes"], 1)
        self.assertIn("thresholds", payload)

    def test_scheduled_skips_a_fresh_sample(self):
        sb.append_history(self.path, {"ts": 1000.0, "disk": {}, "components": {}, "footprint": 0})
        buf = io.StringIO()
        rc = sb.run({"history": str(self.path), "scheduled": True, "walk": False}, out=buf, now=1000.0 + 3600)
        self.assertEqual(rc, 0)
        self.assertIn("skip", buf.getvalue())
        self.assertEqual(len(sb.load_history(self.path)), 1)

    def test_scheduled_records_an_old_sample(self):
        sb.append_history(self.path, {"ts": 1000.0, "disk": {}, "components": {}, "footprint": 0})
        buf = io.StringIO()
        sb.run(
            {"history": str(self.path), "scheduled": True, "walk": False, "metrics": {"compressed": 1}},
            out=buf,
            now=1000.0 + 30 * 3600,
        )
        self.assertEqual(len(sb.load_history(self.path)), 2)

    def test_an_alert_exits_one(self):
        sb.append_history(
            self.path,
            {
                "ts": 1000.0,
                "disk": {"path": "/d", "total": 1000, "used": 900, "free": 100, "pct": 90.0},
                "components": {},
                "footprint": 0,
            },
        )
        buf = io.StringIO()
        with mock.patch.object(sb.shutil, "disk_usage", lambda _p: _Usage(1000, 900, 100)):
            rc = sb.run(
                {"history": str(self.path), "walk": False, "metrics": {"compressed": 1}},
                out=buf,
                now=1000.0 + 30 * 3600,
            )
        self.assertEqual(rc, 1)
        self.assertIn("alert", buf.getvalue())

    def test_latest_without_history(self):
        payload = sb.latest(path=self.path)
        self.assertEqual(payload["state"], "unknown")
        self.assertIsNone(payload["sample"])
        self.assertIn("install", payload)

    def test_latest_reads_the_last_sample(self):
        sb.append_history(self.path, {"ts": 1000.0, "disk": {"total": 10, "used": 1, "pct": 10.0}, "components": {}, "footprint": 0})
        payload = sb.latest(path=self.path, now=1000.0)
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["samples"], 1)

    def test_parse_args(self):
        self.assertEqual(sb.parse_args(["--json"])["json"], True)
        self.assertEqual(sb.parse_args(["--no-record"])["record"], False)
        self.assertEqual(sb.parse_args(["--history", "/x"])["history"], "/x")
        self.assertIsInstance(sb.parse_args(["--nope"]), str)
        self.assertIsInstance(sb.parse_args(["--url"]), str)

    def test_main_accepts_the_budget_word(self):
        buf = io.StringIO()
        rc = sb.main(["budget", "--history", str(self.path), "--no-record", "--no-walk"], out=buf)
        self.assertEqual(rc, 0)
        self.assertIn("storage budget", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
