"""Storage budget: the daily footprint sample, its history and its projection.

No real store, no real tree: the metrics body is a fixture, the component
walkers are pointed at a temp dir, and the history file is a temp file.
"""

import io
import json
import os
import sys
import tempfile
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

    def test_the_report_shows_on_disk_and_apparent(self):
        components = {
            name: {"bytes": 0, "physical": 0, "units": 0} for name, _label in sb.COMPONENTS
        }
        components["worktrees"] = {
            "bytes": 4 * GIB,
            "physical": 1 * GIB,
            "units": 10,
            "count": 3,
            "idle_hours": 50.0,
            "idle_path": "/w/old",
        }
        sample = {
            "ts": 1000.0,
            "iso": sb.iso(1000.0),
            "disk": {"path": "/d", "total": 10 * GIB, "used": 1 * GIB, "free": 9 * GIB, "pct": 10.0},
            "components": components,
            "footprint": 4 * GIB,
            "footprint_physical": 1 * GIB,
        }
        payload = {
            "state": "ok",
            "sample": sample,
            "growth": {"components": {}, "footprint": 1.0, "footprint_logical": 4.0, "span_days": 2.0},
            "projection": {"days": {"70": 10.0, "78": 20.0, "85": 30.0}, "rate_bytes_per_day": 1.0},
        }
        buf = io.StringIO()
        sb.format_report(payload, out=buf)
        text = buf.getvalue()
        self.assertIn("on disk", text)
        self.assertIn("apparent", text)
        self.assertIn("git worktrees", text)
        self.assertIn("worktrees: 3, oldest idle 50 h (/w/old)", text)
        self.assertIn("apparent rate", text)


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.dest = self.home / "sessions"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.scratch = self.home / "scratch"
        self.originals = self.home / "originals"
        for path in (self.scratch, self.originals):
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
        self._write(self.scratch / "ws" / "big.bin", 900)
        self._write(self.originals / "s" / "a.jsonl", 100)
        worktrees = {"bytes": 4000, "physical": 1000, "units": 3, "count": 2, "idle_hours": 5.0}
        with mock.patch.object(sb, "_archive_dest", lambda cfg=None: self.dest), mock.patch.object(
            sb, "_originals_roots", lambda: [str(self.originals)]
        ), mock.patch.object(sb, "_scratch_root", lambda: self.scratch), mock.patch.object(sb, "_state_dir", lambda cfg=None: self.state), mock.patch.object(
            sb, "_worktrees", lambda now: dict(worktrees)
        ):
            sample = sb.measure(metrics={"compressed": 10_000, "uncompressed": 60_000}, now=1000.0)
        comp = sample["components"]
        self.assertEqual(comp["vlogs_compressed"]["bytes"], 10_000)
        self.assertEqual(comp["vlogs_compressed"]["physical"], 10_000)
        self.assertEqual(comp["vlogs_uncompressed"]["bytes"], 60_000)
        self.assertEqual((comp["session_archive"]["bytes"], comp["session_archive"]["units"]), (1500, 2))
        self.assertEqual((comp["session_manifest"]["bytes"], comp["session_manifest"]["units"]), (500, 2))
        self.assertEqual((comp["log_archive"]["bytes"], comp["log_archive"]["units"]), (400, 1))
        self.assertEqual((comp["scratch"]["bytes"], comp["scratch"]["units"]), (900, 1))
        self.assertEqual((comp["session_originals"]["bytes"], comp["session_originals"]["units"]), (100, 1))
        self.assertEqual(comp["worktrees"]["count"], 2)
        self.assertEqual(sample["footprint"], 10_000 + 1500 + 500 + 400 + 900 + 4000 + 100)
        self.assertEqual(sample["footprint_physical"], 10_000 + 1500 + 500 + 400 + 900 + 1000 + 100)
        self.assertGreater(sample["disk"]["total"], 0)
        self.assertEqual(sample["iso"], "1970-01-01T00:16:40Z")

    def test_walk_false_skips_the_trees(self):
        sample = sb.measure(metrics={"compressed": 5, "uncompressed": 9}, walk=False, now=1.0)
        self.assertEqual(sample["components"]["session_archive"]["bytes"], 0)
        self.assertEqual(sample["components"]["worktrees"]["physical"], 0)
        self.assertEqual(sample["footprint"], 5)
        self.assertEqual(sample["footprint_physical"], 5)

    def test_a_missing_tree_is_zero_not_an_error(self):
        self.assertEqual(sb._tree(self.home / "nope")["bytes"], 0)
        self.assertEqual(sb._tree(self.home / "nope")["units"], 0)
        self.assertEqual(sb._file_tree(self.home / "nope")["bytes"], 0)
        self.assertEqual(sb._glob_tree(self.home / "nope", "**/*.zst")["bytes"], 0)

    def test_a_symlink_is_not_followed(self):
        self._write(self.home / "real" / "a.bin", 100)
        os.symlink(self.home / "real", self.home / "link")
        self.assertEqual(sb._tree(self.home / "link")["bytes"], 0)

    def test_a_small_file_is_counted_at_its_apparent_size(self):
        self._write(self.home / "small.bin", 1000)
        tree = sb._tree(self.home)
        self.assertEqual(tree["bytes"], 1000)
        self.assertEqual(tree["physical"], 1000)
        self.assertEqual(tree["small"], 1)
        self.assertEqual(tree["probed"], 0)

    def test_a_deep_tree_is_walked_once(self):
        # The walk shares a directory queue between threads; a deep tree with
        # more directories than threads must still be counted exactly once.
        for i in range(40):
            self._write(self.home / f"d{i}" / f"e{i}" / "f.bin", 10)
        tree = sb._tree(self.home)
        self.assertEqual(tree["units"], 40)
        self.assertEqual(tree["bytes"], 400)
        self.assertEqual(tree["physical"], 400)

    def test_an_unprobeable_file_falls_back_to_apparent(self):
        self._write(self.home / "big.bin", sb.PHYSICAL_MIN_BYTES + 1)
        with mock.patch.object(sb, "_extent_offset", lambda path: None):
            tree = sb._tree(self.home)
        self.assertEqual(tree["physical"], sb.PHYSICAL_MIN_BYTES + 1)
        self.assertEqual(tree["probed"], 0)

    def test_extent_offset_is_none_when_the_open_fails(self):
        with mock.patch.object(sb.os, "open", side_effect=OSError("nope")):
            self.assertIsNone(sb._extent_offset(str(self.home / "x")))

    @unittest.skipUnless(sys.platform == "darwin", "APFS clones are macOS only")
    def test_a_clone_is_counted_once_on_disk(self):
        import subprocess

        self._write(self.home / "a.bin", 2 * 1024 * 1024)
        subprocess.run(["cp", "-c", str(self.home / "a.bin"), str(self.home / "b.bin")], check=True)
        tree = sb._tree(self.home)
        self.assertEqual(tree["units"], 2)
        self.assertEqual(tree["bytes"], 4 * 1024 * 1024)
        if tree["probed"] != 2:
            self.skipTest("the volume does not answer F_LOG2PHYS_EXT")
        self.assertLess(tree["physical"], tree["bytes"])
        self.assertLessEqual(tree["physical"], 3 * 1024 * 1024)

    @unittest.skipUnless(sys.platform == "darwin", "APFS clones are macOS only")
    def test_a_clone_across_two_roots_is_counted_once(self):
        import subprocess

        first = self.home / "first"
        second = self.home / "second"
        first.mkdir()
        second.mkdir()
        self._write(first / "big.bin", 2 * 1024 * 1024)
        subprocess.run(["cp", "-c", str(first / "big.bin"), str(second / "big.bin")], check=True)
        seen: dict = {}
        one = sb._tree(first, seen=seen)
        two = sb._tree(second, seen=seen)
        if one["probed"] != 1 or two["probed"] != 1:
            self.skipTest("the volume does not answer F_LOG2PHYS_EXT")
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(sum(seen.values()), 3 * 1024 * 1024)

    def test_worktrees_counts_roots_and_the_oldest_idle(self):
        base = self.home / "worktrees"
        projects = self.home / "projects"
        old = base / "repo" / "old-task"
        new = base / "repo" / "new-task"
        nested = projects / "proj" / ".worktrees" / "nested-task"
        for root in (old, new, nested):
            (root / ".git").mkdir(parents=True)
            (root / ".git" / "index").write_bytes(b"x" * 10)
            (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
            (root / "file.bin").write_bytes(b"y" * 100)
        now = 1_000_000.0
        for root, hours in ((old, 100), (new, 2), (nested, 5)):
            for name in ("index", "HEAD"):
                stamp = now - hours * 3600
                os.utime(root / ".git" / name, (stamp, stamp))
        with mock.patch.dict(os.environ, {sb.WORKTREES_ENV: str(base)}), mock.patch.object(
            sb, "PROJECTS_ROOT", str(projects)
        ):
            out = sb._worktrees(now)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["units"], 9)
        self.assertEqual(out["bytes"], 3 * (10 + 21 + 100))
        self.assertEqual(out["idle_hours"], 100.0)
        self.assertEqual(out["idle_path"], os.path.realpath(str(old)))

    def test_a_nested_worktrees_dir_is_not_counted_twice(self):
        base = self.home / "worktrees"
        outer = base / "repo" / "task"
        inner = outer / ".worktrees" / "inner"
        for root in (outer, inner):
            (root / ".git").mkdir(parents=True)
            (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        with mock.patch.dict(os.environ, {sb.WORKTREES_ENV: str(base)}), mock.patch.object(
            sb, "PROJECTS_ROOT", str(self.home / "no-projects")
        ):
            roots = sb._worktree_roots()
        self.assertEqual(roots, [os.path.realpath(str(outer))])

    def test_a_linked_worktree_reads_its_gitdir(self):
        root = self.home / "wt"
        gitdir = self.home / "main" / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        root.mkdir()
        (root / ".git").write_text("gitdir: %s\n" % gitdir, encoding="utf-8")
        (gitdir / "index").write_bytes(b"x")
        now = 1_000_000.0
        os.utime(gitdir / "index", (now - 3 * 3600, now - 3 * 3600))
        self.assertAlmostEqual(sb._idle_hours(str(root), now), 3.0, places=1)
        self.assertIsNone(sb._idle_hours(str(self.home / "nope"), now))


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "storage-budget.jsonl"

    def sample(self, ts, compressed, used=100, total=1000, physical=None):
        return {
            "ts": ts,
            "iso": sb.iso(ts),
            "store": True,
            "disk": {"path": "/d", "total": total, "used": used, "free": total - used, "pct": 100.0 * used / total},
            "components": {
                name: {
                    "bytes": compressed if name == "vlogs_compressed" else 0,
                    "physical": (compressed if physical is None else physical)
                    if name == "vlogs_compressed"
                    else 0,
                    "units": 0,
                }
                for name, _label in sb.COMPONENTS
            },
            "footprint": compressed,
            "footprint_physical": compressed if physical is None else physical,
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

    def test_growth_uses_the_on_disk_bytes(self):
        now = 1000.0 + 2 * 86400
        rows = [self.sample(1000.0, 10 * GIB, physical=1 * GIB), self.sample(now, 20 * GIB, physical=2 * GIB)]
        rates = sb.growth(rows, now)
        self.assertAlmostEqual(rates["footprint"], 0.5 * GIB, delta=1.0)
        self.assertAlmostEqual(rates["footprint_logical"], 5 * GIB, delta=1.0)
        self.assertAlmostEqual(rates["components"]["vlogs_compressed"], 0.5 * GIB, delta=1.0)

    def test_an_old_sample_without_physical_reads_apparent(self):
        now = 1000.0 + 2 * 86400
        old = self.sample(1000.0, 1 * GIB)
        for comp in old["components"].values():
            comp.pop("physical", None)
        old.pop("footprint_physical", None)
        rates = sb.growth([old, self.sample(now, 3 * GIB)], now)
        self.assertAlmostEqual(rates["footprint"], 1 * GIB, delta=1.0)

    def test_the_projection_uses_the_on_disk_rate(self):
        sample = self.sample(1000.0, 0, used=600, total=1000)
        proj = sb.project(sample, {"footprint": 10.0, "footprint_logical": 100.0})
        self.assertEqual(proj["days"]["80"], 20.0)
        self.assertEqual(proj["rate_bytes_per_day"], 10.0)


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

    def test_latest_fills_the_on_disk_figures_of_an_old_sample(self):
        sb.append_history(
            self.path,
            {
                "ts": 1000.0,
                "disk": {"total": 10, "used": 1, "pct": 10.0},
                "components": {"scratch": {"bytes": 500, "units": 2}},
                "footprint": 500,
            },
        )
        payload = sb.latest(path=self.path, now=1000.0)
        self.assertEqual(payload["sample"]["components"]["scratch"]["physical"], 500)
        self.assertEqual(payload["sample"]["footprint_physical"], 500)

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
