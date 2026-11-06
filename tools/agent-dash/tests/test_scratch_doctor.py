"""Doctor warning for hub run folders that hold scratch-sized content (dash/scratchhub.py). Read-only."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dash import scratchhub

NOW = 1_800_000_000.0
MB = 1024 * 1024


def snapshot(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            try:
                out[str(p)] = p.stat().st_mtime_ns
            except OSError:
                pass
    return out


class RunFolderFindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.hub = self.base / "hub"
        self.state = self.base / "state"
        self.state.mkdir()
        self.old = os.environ.get("AGENT_DASH_STATE_DIR")
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state)
        self.cfg = {"hub_dir": str(self.hub), "state_dir": str(self.state)}
        self.build()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_STATE_DIR", None)
        else:
            os.environ["AGENT_DASH_STATE_DIR"] = self.old
        self.tmp.cleanup()

    def build(self):
        runs = self.hub / "runs"
        # Flagged: a Python environment at the top of the run folder.
        (runs / "ws1-plan-venv").mkdir(parents=True)
        (runs / "ws1-plan-venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        # Flagged: a Postgres data directory one level down (a cluster under the run folder).
        (runs / "ws2-exec-pg" / "pgdata").mkdir(parents=True)
        (runs / "ws2-exec-pg" / "pgdata" / "PG_VERSION").write_text("16\n", encoding="utf-8")
        # Flagged: more than 200 MB of images (sparse, so it costs no disk).
        (runs / "ws3-exec-images" / "images").mkdir(parents=True)
        (runs / "ws3-exec-images" / "images" / "page.png").write_bytes(b"")
        os.truncate(runs / "ws3-exec-images" / "images" / "page.png", 201 * MB)
        # Clean: prompts, reports and a small cache.
        (runs / "ws4-plan-clean").mkdir(parents=True)
        (runs / "ws4-plan-clean" / "out-ws4-plan-clean.md").write_text("PASS\n", encoding="utf-8")
        (runs / "ws4-plan-clean" / "cache").mkdir()
        (runs / "ws4-plan-clean" / "cache" / "small.bin").write_bytes(b"x" * 1024)
        # Flagged: a nested hub project with its own runs folder.
        (self.hub / "proj" / "runs" / "pj-exec-nested" / ".venv").mkdir(parents=True)
        (self.hub / "proj" / "runs" / "pj-exec-nested" / ".venv" / "pyvenv.cfg").write_text("x", encoding="utf-8")

    def labels(self, cfg=None):
        found = scratchhub.run_folder_findings(cfg if cfg is not None else self.cfg, NOW)
        return {Path(f["folder"]).name: f["reasons"] for f in found}

    def test_flags_env_postgres_and_big_caches(self):
        got = self.labels()
        self.assertIn("ws1-plan-venv", got)
        self.assertEqual(got["ws1-plan-venv"], ["a Python environment"])
        self.assertEqual(got["ws2-exec-pg"], ["a Postgres data directory"])
        self.assertTrue(any("MB of caches or images" in r for r in got["ws3-exec-images"]), got)
        self.assertIn("pj-exec-nested", got)

    def test_clean_folder_is_not_flagged(self):
        self.assertNotIn("ws4-plan-clean", self.labels())

    def test_no_hub_configured_means_no_findings(self):
        self.assertEqual(scratchhub.run_folder_findings({}, NOW), [])
        self.assertEqual(scratchhub.run_folder_findings({"hub_dir": str(self.base / "missing")}, NOW), [])

    def test_registered_run_folder_under_the_hub_is_included(self):
        extra = self.hub / "elsewhere" / "ws9-exec-reg"
        (extra / "venv").mkdir(parents=True)
        (extra / "venv" / "pyvenv.cfg").write_text("x", encoding="utf-8")
        (self.state / "run-dirs.txt").write_text(f"{extra}\n/outside/ws9-x\n", encoding="utf-8")
        self.assertIn("ws9-exec-reg", self.labels())

    def test_check_is_read_only(self):
        (self.state / "run-dirs.txt").write_text(f"{self.hub}/runs/ws1-plan-venv\n/gone/ws0-x\n", encoding="utf-8")
        before = snapshot(self.base)
        self.labels()
        self.assertEqual(snapshot(self.base), before)
        self.assertIn("/gone/ws0-x", (self.state / "run-dirs.txt").read_text(encoding="utf-8"))

    def test_doctor_wrapper_adds_a_warning_per_folder(self):
        from dash import doctor

        ctx = SimpleNamespace(cfg=self.cfg, now=NOW, problems=[], emitted=set(), groups={}, _ignored=[])
        doctor._collect_run_folders(ctx)
        codes = [p["code"] for p in ctx.problems]
        self.assertEqual(codes.count("run-folder-scratch"), 4)
        for item in ctx.problems:
            self.assertEqual(item["severity"], "warn")
            self.assertEqual(item["scope"], "global")
            self.assertIn(str(self.hub), item["detail"])
            self.assertIn("$AGENT_SCRATCH", item["detail"])

    def test_collect_problems_reports_it(self):
        from dash import doctor

        with mock.patch("dash.doctor._collect_dashboard", lambda ctx: None), \
                mock.patch("dash.doctor._collect_bundles", lambda ctx: None), \
                mock.patch("dash.doctor._collect_checkout", lambda ctx: None), \
                mock.patch("dash.doctor._collect_build", lambda ctx: None), \
                mock.patch("dash.doctor._collect_python", lambda ctx: None), \
                mock.patch("dash.doctor._collect_config", lambda ctx: None), \
                mock.patch("dash.doctor._collect_linear", lambda ctx: None), \
                mock.patch("dash.doctor._collect_page", lambda ctx: None), \
                mock.patch("dash.doctor._collect_roots", lambda ctx: None), \
                mock.patch("dash.doctor._collect_pricing", lambda ctx: None), \
                mock.patch("dash.doctor._collect_reconcile", lambda ctx: None), \
                mock.patch("dash.doctor._collect_orchestration", lambda ctx: None), \
                mock.patch("dash.doctor._collect_lifecycle", lambda ctx: None), \
                mock.patch("dash.doctor._collect_fleet", lambda ctx: None), \
                mock.patch("dash.doctor._collect_labels", lambda ctx: None):
            result = doctor.collect_problems(None, self.cfg, NOW, url="", health=None, processes=[])
        codes = [p["code"] for p in result["problems"]]
        self.assertEqual(codes.count("run-folder-scratch"), 4)


if __name__ == "__main__":
    unittest.main()
