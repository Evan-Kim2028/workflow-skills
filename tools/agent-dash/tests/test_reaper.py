"""Dead-run reaper: reap_dead_runs, the reaper source, and the deploy pass.

Fake pid liveness and real temp files only: no real agents, no live state
dir. Covers the close rules (dead pid + quiet logs -> DEAD, rate limit in
the err log -> QUOTA, live pid and fresh evidence stay), the skip rules
(dagster, slices, no-evidence rows), the configurable quiet window, the
collector's cfg wiring, and deploy.reap_stale_rows against a temp state
dir.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash import deploy as deploy_lib
from dash.liveness import REAP_IDLE_S, sibling_err_path
from dash.store import Store

HERE = Path(__file__).resolve().parent


def load_reaper_source():
    import importlib.util

    path = HERE.parent / "dash" / "sources" / "reaper.py"
    spec = importlib.util.spec_from_file_location("dash_sources_reaper", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reaper = load_reaper_source()

NOW = 1_800_000_000.0
OLD = NOW - 40 * 60
QUIET = NOW - 30 * 60
FRESH = NOW - 60.0


class ReaperStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pad = Path(self.tmp.name)
        self.store = Store(":memory:")
        self._alive_patch = mock.patch("dash.store.running._pid_alive", self._pid_alive)
        self._alive_patch.start()
        self.alive_pids = set()

    def tearDown(self):
        self._alive_patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def _pid_alive(self, pid):
        return int(pid) in self.alive_pids

    def _out_file(self, label, mtime):
        path = self.pad / ("out-%s.md" % label)
        path.write_text("partial report\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return str(path)

    def _run(self, run_id, label, mtime=QUIET, source="pi", status="running", pid=424242, started=OLD, slice_of=None):
        row = {
            "run_id": run_id,
            "source": source,
            "label": label,
            "status": status,
            "pid": pid,
            "started_at": started,
            "out_path": self._out_file(label, mtime),
        }
        if slice_of:
            row["slice_of"] = slice_of
        self.store.upsert_run(row)
        return row

    def test_dead_pid_quiet_logs_close_dead(self):
        self._run("pi:s1:ws14-plan-x", "ws14-plan-x", pid=424242)
        reaped = self.store.reap_dead_runs(NOW)
        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0]["run_id"], "pi:s1:ws14-plan-x")
        self.assertEqual(reaped[0]["verdict"], "DEAD")
        row = self.store.get_run("pi:s1:ws14-plan-x")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "DEAD")
        self.assertEqual(row["ended_at"], QUIET)
        self.assertIsNone(row["pid"])

    def test_alive_pid_is_never_touched(self):
        self.alive_pids.add(424242)
        self._run("pi:s2:ws14-exec-y", "ws14-exec-y")
        self.assertEqual(self.store.reap_dead_runs(NOW), [])
        row = self.store.get_run("pi:s2:ws14-exec-y")
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["ended_at"])

    def test_fresh_out_file_keeps_the_row(self):
        self._run("glm:s3:ws16-exec-z", "ws16-exec-z", mtime=FRESH)
        self.assertEqual(self.store.reap_dead_runs(NOW), [])
        self.assertEqual(self.store.get_run("glm:s3:ws16-exec-z")["status"], "running")

    def test_rate_limit_err_closes_quota(self):
        out = self.pad / "out-ws16-grok-q.md"
        out.write_text("partial\n", encoding="utf-8")
        os.utime(out, (QUIET, QUIET))
        err = self.pad / "err-ws16-grok-q.log"
        err.write_text("Error: 429 Rate limit reached for requests\n", encoding="utf-8")
        os.utime(err, (QUIET, QUIET))
        self.store.upsert_run(
            {
                "run_id": "glm:s4:ws16-grok-q",
                "source": "glm",
                "label": "ws16-grok-q",
                "status": "running",
                "pid": 424242,
                "started_at": OLD,
                "out_path": str(out),
            }
        )
        reaped = self.store.reap_dead_runs(NOW)
        self.assertEqual([entry["verdict"] for entry in reaped], ["QUOTA"])
        row = self.store.get_run("glm:s4:ws16-grok-q")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "QUOTA")

    def test_existing_verdict_is_kept(self):
        self.store.upsert_run(
            {
                "run_id": "pi:s5:ws14-partial",
                "source": "pi",
                "label": "ws14-partial",
                "status": "running",
                "pid": 424242,
                "started_at": OLD,
                "verdict": "PARTIAL",
                "out_path": self._out_file("ws14-partial", QUIET),
            }
        )
        reaped = self.store.reap_dead_runs(NOW)
        self.assertEqual([entry["run_id"] for entry in reaped], ["pi:s5:ws14-partial"])
        row = self.store.get_run("pi:s5:ws14-partial")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "PARTIAL")

    def test_dagster_rows_and_slices_are_skipped(self):
        self._run("dagster:abc", "nightly", source="dagster")
        self._run("muse:s6:lane", "lane", slice_of="muse:s5:lead")
        self.assertEqual(self.store.reap_dead_runs(NOW), [])
        self.assertEqual(self.store.get_run("dagster:abc")["status"], "running")
        self.assertEqual(self.store.get_run("muse:s6:lane")["status"], "running")

    def test_no_evidence_row_is_skipped(self):
        self.store.upsert_run(
            {
                "run_id": "shell:s7:x",
                "source": "shell",
                "label": "x",
                "status": "running",
                "pid": 424242,
            }
        )
        self.assertEqual(self.store.reap_dead_runs(NOW), [])
        self.assertEqual(self.store.get_run("shell:s7:x")["status"], "running")

    def test_idle_window_is_configurable(self):
        self._run("pi:s8:w5", "w5", mtime=NOW - 5 * 60)
        self.assertEqual(self.store.reap_dead_runs(NOW, idle_s=6 * 60), [])
        self.assertEqual(self.store.get_run("pi:s8:w5")["status"], "running")
        reaped = self.store.reap_dead_runs(NOW, idle_s=4 * 60)
        self.assertEqual([entry["run_id"] for entry in reaped], ["pi:s8:w5"])

    def test_default_window_is_ten_minutes(self):
        self._run("pi:s9:w9", "w9", mtime=NOW - (REAP_IDLE_S - 30))
        self.assertEqual(self.store.reap_dead_runs(NOW), [])
        reaped = self.store.reap_dead_runs(NOW + 60)
        self.assertEqual([entry["run_id"] for entry in reaped], ["pi:s9:w9"])

    def test_source_filter_narrows_the_pass(self):
        self._run("pi:s10:a", "a", source="pi")
        self._run("glm:s11:b", "b", source="glm")
        reaped = self.store.reap_dead_runs(NOW, sources=("glm",))
        self.assertEqual([entry["run_id"] for entry in reaped], ["glm:s11:b"])
        self.assertEqual(self.store.get_run("pi:s10:a")["status"], "running")


class SiblingErrTests(unittest.TestCase):
    def test_launcher_names_map(self):
        self.assertEqual(
            sibling_err_path("/pads/run/out-ws1-plan-a.md"),
            "/pads/run/err-ws1-plan-a.log",
        )

    def test_non_launcher_names_return_none(self):
        self.assertIsNone(sibling_err_path("/sessions/abc.jsonl"))
        self.assertIsNone(sibling_err_path("out-.md"))
        self.assertIsNone(sibling_err_path(None))


class ReaperSourceTests(unittest.TestCase):
    def test_idle_s_defaults_and_cfg(self):
        self.assertEqual(reaper.idle_s({}), REAP_IDLE_S)
        self.assertEqual(reaper.idle_s(None), REAP_IDLE_S)
        self.assertEqual(reaper.idle_s({"reaper": {"idle_minutes": 5}}), 300.0)
        self.assertEqual(reaper.idle_s({"reaper": {}}), REAP_IDLE_S)
        self.assertEqual(reaper.idle_s({"reaper": {"idle_minutes": "bogus"}}), REAP_IDLE_S)
        self.assertEqual(reaper.idle_s({"reaper": {"idle_minutes": 0}}), REAP_IDLE_S)

    def test_poll_reaps_with_cfg_window(self):
        seen = {}

        class FakeStore:
            def reap_dead_runs(self, now, idle_s=None):
                seen["now"] = now
                seen["idle_s"] = idle_s
                return [{"run_id": "pi:s12:c", "verdict": "DEAD", "idle_s": 601.0}]

        reaper.poll(FakeStore(), {"reaper": {"idle_minutes": 2}}, NOW)
        self.assertEqual(seen["idle_s"], 120.0)
        self.assertEqual(seen["now"], NOW)

    def test_poll_swallows_store_failures(self):
        class BadStore:
            def reap_dead_runs(self, now, idle_s=None):
                raise RuntimeError("boom")

        reaper.poll(BadStore(), {}, NOW)


class DeployReapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._alive_patch = mock.patch("dash.store.running._pid_alive", lambda pid: False)
        self._alive_patch.start()

    def tearDown(self):
        self._alive_patch.stop()
        self.tmp.cleanup()

    def test_missing_db_reports_skipped(self):
        self.assertEqual(deploy_lib.reap_stale_rows(self.state), -1)

    def test_reap_pass_closes_stale_rows_before_the_gate(self):
        out = self.state / "out-lane.md"
        out.write_text("partial\n", encoding="utf-8")
        quiet = time.time() - 30 * 60
        os.utime(out, (quiet, quiet))
        with Store(self.state / "dash.sqlite") as store:
            store.upsert_run(
                {
                    "run_id": "pi:s13:lane",
                    "source": "pi",
                    "label": "lane",
                    "status": "running",
                    "pid": 424242,
                    "started_at": quiet,
                    "out_path": str(out),
                }
            )
        self.assertEqual(deploy_lib.reap_stale_rows(self.state), 1)
        with Store(self.state / "dash.sqlite") as store:
            row = store.get_run("pi:s13:lane")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "DEAD")


if __name__ == "__main__":
    unittest.main()
