"""Registry worker launcher. Fake processes only. No real agents."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SID = "11111111-1111-4111-8111-111111111111"
NOW = 1_700_000_000.0


def load():
    path = ROOT / "scripts" / "run_worker.py"
    spec = importlib.util.spec_from_file_location("run_worker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = load()


def registry():
    return json.loads((ROOT / "workers.example.json").read_text(encoding="utf-8"))


class Launch:
    def __init__(self, alive=False, quota_on=0, report="PASS\nbuilt it\n"):
        self.cmds = []
        self.alive = alive
        self.quota_on = quota_on
        self.report = report
        self.n = 0

    def spawn(self, cmd, out_path, err_path):
        self.n += 1
        self.cmds.append(list(cmd))
        out_path = Path(out_path)
        err_path = Path(err_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.n == self.quota_on:
            err_path.write_text("HTTP 429 rate limit\n", encoding="utf-8")
            out_path.write_text("", encoding="utf-8")
        else:
            err_path.write_text("", encoding="utf-8")
            out_path.write_text(self.report, encoding="utf-8")
        return 4000 + self.n

    def is_alive(self, pid):
        return self.alive


class RunWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("do the task\n", encoding="utf-8")
        self.run_dir = self.root / "runs"
        self.reg = self.root / "workers.json"
        self.reg.write_text(json.dumps(registry()), encoding="utf-8")
        self.env = {
            "AGENT_DASH_WORKERS": os.environ.get("AGENT_DASH_WORKERS"),
            "AGENT_DASH_STATE_DIR": os.environ.get("AGENT_DASH_STATE_DIR"),
        }
        os.environ["AGENT_DASH_WORKERS"] = str(self.reg)
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state)

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def go(self, tier, launch, label="lane-1", max_wait="0"):
        from io import StringIO
        import contextlib

        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = runner.run(
                [tier, label, str(self.ws), str(self.prompt), str(self.run_dir), max_wait],
                spawn=launch.spawn,
                pgrep=self.pgrep,
                sleep=lambda _s: None,
                now=lambda: NOW,
                alive=launch.is_alive,
                new_uuid=lambda: SID,
            )
        return code, buf.getvalue().strip()

    def pgrep(self, pattern):
        return self.counts.get(pattern, 0)

    def test_example_lists_the_four_workers(self):
        data = registry()
        self.assertEqual(data["default"], "muse")
        self.assertEqual(data["auto_order"], ["muse", "grok", "glm", "glm-flash"])
        self.assertEqual(set(data["workers"]), {"muse", "grok", "glm", "glm-flash"})
        kinds = {name: row["kind"] for name, row in data["workers"].items()}
        self.assertEqual(kinds["muse"], "muse")
        self.assertEqual(kinds["grok"], "cursor")
        self.assertEqual(kinds["glm"], "grok-build")
        self.assertEqual(kinds["glm-flash"], "grok-build")
        for row in data["workers"].values():
            self.assertIn("[", row["pgrep"])
            self.assertTrue(row["enabled"])

    def test_default_launches_muse_and_writes_the_manifest(self):
        self.counts = {}
        launch = Launch()
        code, line = self.go("default", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=muse", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[0], "muse")
        self.assertIn("--session-id", cmd)
        self.assertIn(SID, cmd)
        self.assertIn("--prompt-file", cmd)
        self.assertIn(str(self.prompt), cmd)
        self.assertIn("muse-spark-1.3-contributor", cmd)
        row = (self.run_dir / "muse-sessions.tsv").read_text(encoding="utf-8").strip()
        self.assertEqual(row, "lane-1\t%s\t1700000000" % SID)
        self.assertEqual((self.run_dir / "lane-1.tier").read_text(encoding="utf-8").strip(), "muse")

    def test_auto_skips_a_worker_at_its_cap(self):
        self.counts = {"[m]use-bin|[m]use exec": 25}
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=grok", line)
        self.assertTrue(launch.cmds[0][0].endswith("launch_grok.sh"))
        self.assertEqual(launch.cmds[0][4], registry()["workers"]["grok"]["model"])
        text = (self.run_dir / "cursor-sessions.tsv").read_text(encoding="utf-8").strip()
        self.assertEqual(text, "lane-1\t-\t1700000000")

    def test_auto_skips_a_worker_in_cooldown(self):
        self.counts = {}
        (self.state / "worker-cooldowns.json").write_text(
            json.dumps({"muse": NOW + 100}), encoding="utf-8"
        )
        launch = Launch()
        _code, line = self.go("auto", launch)
        self.assertIn("tier=grok", line)
        self.assertEqual(len(launch.cmds), 1)

    def test_glm_appends_the_run_rule_and_logs_the_model(self):
        self.counts = {}
        launch = Launch()
        code, line = self.go("glm", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=glm", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[:3], ["grok", "-p", "do the task\n\n" + runner.RUN_RULE])
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "glm-5.3")
        self.assertEqual(cmd[cmd.index("-s") + 1], SID)
        self.assertIn("bypassPermissions", cmd)
        self.assertIn("plain", cmd)
        row = (self.run_dir / "glm-sessions.tsv").read_text(encoding="utf-8").strip().split("\t")
        self.assertEqual(row, ["lane-1", SID, "1700000000", "glm-5.3"])

    def test_quota_on_auto_relaunches_the_next_tier(self):
        self.counts = {}
        launch = Launch(quota_on=1)
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=grok", line)
        self.assertEqual(len(launch.cmds), 2)
        self.assertEqual(launch.cmds[0][0], "muse")
        self.assertTrue(launch.cmds[1][0].endswith("launch_grok.sh"))
        archived = (self.run_dir / "err-lane-1.muse.log").read_text(encoding="utf-8")
        self.assertIn("429", archived)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["muse"], NOW + 30 * 60)

    def test_named_tier_quota_does_not_relaunch(self):
        self.counts = {}
        launch = Launch(quota_on=1)
        code, line = self.go("muse", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=quota", line)
        self.assertIn("tier=muse", line)
        self.assertEqual(len(launch.cmds), 1)
        self.assertFalse((self.run_dir / "out-lane-1.muse.md").exists())

    def test_a_later_call_only_waits(self):
        self.counts = {}
        launch = Launch(alive=True)
        code, line = self.go("auto", launch, max_wait="0")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: running", line)
        self.assertEqual(len(launch.cmds), 1)
        code, line = self.go("glm", launch, max_wait="0")
        self.assertIn("STATUS: running", line)
        self.assertIn("tier=muse", line)
        self.assertEqual(len(launch.cmds), 1)

    def test_an_exited_child_is_not_alive(self):
        out = self.run_dir / "out-child.md"
        err = self.run_dir / "err-child.log"
        pid = runner.default_spawn(["/bin/sh", "-c", "echo PASS"], out, err)
        deadline = time.monotonic() + 5
        while runner.pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(runner.pid_alive(pid))
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        self.assertEqual(out.read_text(encoding="utf-8"), "PASS\n")

    def test_none_available_when_every_worker_is_at_cap(self):
        self.counts = {
            "[m]use-bin|[m]use exec": 25,
            "[c]ursor-agent": 25,
            "-m [g]lm-5.3($| )": 5,
            "-m [g]lm-5.3-flash": 5,
        }
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("reason=none-available", line)
        self.assertEqual(launch.cmds, [])

    def test_launch_registers_the_run_dir_once(self):
        from dash.rundirs import registered_run_dirs

        self.counts = {}
        launch = Launch()
        code, _line = self.go("default", launch)
        self.assertEqual(code, 0)
        listed = (self.state / "run-dirs.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(listed, [str(self.run_dir)])
        # A second call for the same label only waits; registration dedupes.
        code, _line = self.go("default", launch)
        self.assertEqual(code, 0)
        listed = (self.state / "run-dirs.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(listed, [str(self.run_dir)])
        self.assertEqual(registered_run_dirs(self.state), [str(self.run_dir)])

    def test_registration_skips_missing_dirs_and_prunes_stale_ones(self):
        import time as _time

        from dash.rundirs import MAX_ENTRIES, register_run_dir, registered_run_dirs

        self.run_dir.mkdir(exist_ok=True)
        gone = self.root / "gone"
        gone.mkdir()
        old = self.root / "old"
        old.mkdir()
        self.assertTrue(register_run_dir(self.state, gone))
        self.assertTrue(register_run_dir(self.state, self.run_dir))
        self.assertTrue(register_run_dir(self.state, old))
        gone.rmdir()
        ancient = _time.time() - 15 * 86400
        os.utime(old, (ancient, ancient))
        self.assertEqual(registered_run_dirs(self.state), [str(self.run_dir)])
        kept = (self.state / "run-dirs.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(kept, [str(self.run_dir)])
        self.assertLessEqual(len(kept), MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
