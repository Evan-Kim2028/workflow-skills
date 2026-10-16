"""Registry worker launcher. Fake workers, plus one real short-lived child. No real agents."""
import importlib.util
import json
import os
import subprocess
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

    def go(self, tier, launch, label="lane-1", max_wait="0", model=None):
        from io import StringIO
        import contextlib

        argv = [tier, label, str(self.ws), str(self.prompt), str(self.run_dir), max_wait]
        if model is not None:
            argv.append(model)
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = runner.run(
                argv,
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

    def test_example_lists_the_workers(self):
        data = registry()
        self.assertEqual(data["default"], "muse")
        self.assertEqual(data["auto_order"], ["muse", "grok", "composer", "glm", "glm-flash"])
        self.assertEqual(set(data["workers"]), {"muse", "grok", "composer", "glm", "glm-flash"})
        self.assertEqual(data["auto_order"].index("composer"), data["auto_order"].index("grok") + 1)
        kinds = {name: row["kind"] for name, row in data["workers"].items()}
        self.assertEqual(kinds["muse"], "muse")
        self.assertEqual(kinds["grok"], "cursor")
        self.assertEqual(kinds["composer"], "cursor")
        self.assertEqual(kinds["glm"], "grok-build")
        self.assertEqual(kinds["glm-flash"], "grok-build")
        composer = data["workers"]["composer"]
        self.assertEqual(composer["model"], "composer-2.5")
        self.assertEqual(composer["cap"], 25)
        self.assertEqual(composer["manifest"], "cursor-sessions.tsv")
        self.assertLess(composer["cost_rank"], data["workers"]["grok"]["cost_rank"])
        self.assertEqual(data["workers"]["grok"]["model"], "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]")
        for name in ("grok", "composer"):
            row = data["workers"][name]
            self.assertEqual(row["pgrep"], runner.cursor_pgrep(row["model"]))
        for row in data["workers"].values():
            self.assertIn("[", row["pgrep"])
            self.assertTrue(row["enabled"])
            self.assertIn("model", row["columns"])

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
        self.assertEqual(row, "lane-1\t%s\t1700000000\tmuse-spark-1.3-contributor" % SID)
        self.assertEqual((self.run_dir / "lane-1.tier").read_text(encoding="utf-8").splitlines(), ["muse", "muse-spark-1.3-contributor"])
        self.assertIn("model=muse-spark-1.3-contributor", line)

    def test_auto_skips_a_worker_at_its_cap(self):
        self.counts = {"[m]use-bin|[m]use exec": 25}
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=grok", line)
        self.assertTrue(launch.cmds[0][0].endswith("launch_grok.sh"))
        self.assertEqual(launch.cmds[0][4], registry()["workers"]["grok"]["model"])
        text = (self.run_dir / "cursor-sessions.tsv").read_text(encoding="utf-8").strip()
        self.assertEqual(text, "lane-1\t-\t1700000000\t" + registry()["workers"]["grok"]["model"])

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

    def test_none_available_when_every_worker_is_at_cap(self):
        data = registry()
        self.counts = {
            "[m]use-bin|[m]use exec": 25,
            runner.cursor_pgrep(data["workers"]["grok"]["model"]): 25,
            runner.cursor_pgrep(data["workers"]["composer"]["model"]): 25,
            "-m [g]lm-5.3($| )": 5,
            "-m [g]lm-5.3-flash": 5,
        }
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("reason=none-available", line)
        self.assertEqual(launch.cmds, [])

    def test_cursor_pgrep_matches_only_that_model(self):
        import re

        data = registry()
        grok_model = data["workers"]["grok"]["model"]
        comp_model = data["workers"]["composer"]["model"]
        grok_pat = runner.cursor_pgrep(grok_model)
        comp_pat = runner.cursor_pgrep(comp_model)
        grok_cmd = "cursor-agent -p --model %s --force --trust" % grok_model
        comp_cmd = "cursor-agent -p --model %s --force --trust" % comp_model
        bare = "cursor-agent -p --force --trust"
        self.assertRegex(grok_cmd, grok_pat)
        self.assertNotRegex(comp_cmd, grok_pat)
        self.assertNotRegex(bare, grok_pat)
        self.assertRegex(comp_cmd, comp_pat)
        self.assertNotRegex(grok_cmd, comp_pat)
        self.assertIsNone(re.search(grok_pat, "pgrep -f " + grok_pat))
        self.assertIsNone(re.search(comp_pat, "pgrep -f " + comp_pat))

    def test_cursor_cap_ignores_other_cursor_agents(self):
        self.counts = {
            "[m]use-bin|[m]use exec": 25,
            "[c]ursor-agent": 60,
        }
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=grok", line)
        self.assertTrue(launch.cmds[0][0].endswith("launch_grok.sh"))
        self.assertEqual(launch.cmds[0][4], registry()["workers"]["grok"]["model"])

    def test_cursor_cap_is_per_model(self):
        data = registry()
        self.counts = {
            "[m]use-bin|[m]use exec": 25,
            runner.cursor_pgrep(data["workers"]["grok"]["model"]): 25,
        }
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=composer", line)
        self.assertEqual(launch.cmds[0][4], "composer-2.5")

    def test_model_override_per_kind(self):
        self.counts = {}
        grok_model = registry()["workers"]["grok"]["model"]

        launch = Launch()
        code, line = self.go("muse", launch, model="custom-muse-9")
        self.assertEqual(code, 0)
        self.assertIn("model=custom-muse-9", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "custom-muse-9")
        row = (self.run_dir / "muse-sessions.tsv").read_text(encoding="utf-8").strip().split("\t")
        self.assertEqual(row[-1], "custom-muse-9")

        launch = Launch()
        code, line = self.go("grok", launch, label="lane-g", model="composer-2.5")
        self.assertEqual(code, 0)
        self.assertIn("model=composer-2.5", line)
        self.assertEqual(launch.cmds[0][4], "composer-2.5")
        row = (self.run_dir / "cursor-sessions.tsv").read_text(encoding="utf-8").strip().split("\t")
        self.assertEqual(row, ["lane-g", "-", "1700000000", "composer-2.5"])

        launch = Launch()
        code, line = self.go("glm", launch, label="lane-m", model="glm-custom-1")
        self.assertEqual(code, 0)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[cmd.index("-m") + 1], "glm-custom-1")
        self.assertIn("model=glm-custom-1", line)
        row = (self.run_dir / "glm-sessions.tsv").read_text(encoding="utf-8").strip().split("\t")
        self.assertEqual(row, ["lane-m", SID, "1700000000", "glm-custom-1"])

        launch = Launch()
        code, line = self.go("composer", launch, label="lane-c", model=grok_model)
        self.assertEqual(code, 0)
        self.assertEqual(launch.cmds[0][4], grok_model)
        self.assertIn("model=" + grok_model, line)

    def test_invalid_model_rejected(self):
        self.counts = {}
        for bad in ("has space", "semi;colon", "$(sub)", "quote'x", "pipe|x", "star*", "slash/x", "a\nb"):
            launch = Launch()
            code, line = self.go("muse", launch, model=bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("reason=bad-model", line)
            self.assertEqual(launch.cmds, [], bad)

    def test_empty_model_keeps_the_registry_model(self):
        self.counts = {}
        launch = Launch()
        code, line = self.go("glm", launch, model="")
        self.assertEqual(code, 0)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[cmd.index("-m") + 1], "glm-5.3")
        self.assertIn("model=glm-5.3", line)

    def test_recall_observes_stored_model(self):
        self.counts = {}
        launch = Launch(alive=True)
        code, line = self.go("muse", launch, model="custom-muse-9", max_wait="0")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: running", line)
        self.assertIn("model=custom-muse-9", line)
        code, line = self.go("glm", launch, model="other-model", max_wait="0")
        self.assertIn("STATUS: running", line)
        self.assertIn("tier=muse", line)
        self.assertIn("model=custom-muse-9", line)
        self.assertNotIn("other-model", line)
        self.assertEqual(len(launch.cmds), 1)
        stored = (self.run_dir / "lane-1.tier").read_text(encoding="utf-8").splitlines()
        self.assertEqual(stored, ["muse", "custom-muse-9"])

    def test_old_tier_file_without_model_still_waits(self):
        self.counts = {}
        self.run_dir.mkdir()
        (self.run_dir / "lane-1.tier").write_text("muse\n", encoding="utf-8")
        (self.run_dir / "out-lane-1.md").write_text("PASS\n", encoding="utf-8")
        launch = Launch()
        code, line = self.go("glm", launch, model="composer-2.5")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=muse", line)
        self.assertIn("model=muse-spark-1.3-contributor", line)
        self.assertEqual(launch.cmds, [])
        self.assertEqual((self.run_dir / "lane-1.tier").read_text(encoding="utf-8").splitlines(), ["muse"])

    def test_reaps_a_finished_child_before_max_wait(self):
        from io import StringIO
        import contextlib

        self.run_dir.mkdir()
        label = "reap"
        out = self.run_dir / ("out-%s.md" % label)
        err = self.run_dir / ("err-%s.log" % label)
        pid = runner.default_spawn(["/bin/sh", "-c", "exit 0"], out, err)
        try:
            state = wait_until_settled(pid)
            self.assertTrue(state.startswith("Z"), "expected an unreaped child, got %r" % state)
            os.kill(pid, 0)
            (self.run_dir / ("%s.pid" % label)).write_text("%s\n" % pid, encoding="utf-8")
            (self.run_dir / ("%s.tier" % label)).write_text("muse\n", encoding="utf-8")
            argv = ["muse", label, str(self.ws), str(self.prompt), str(self.run_dir), "30"]
            buf = StringIO()
            started = time.monotonic()
            with contextlib.redirect_stdout(buf):
                code = runner.run(argv)
            elapsed = time.monotonic() - started
            line = buf.getvalue().strip()
            self.assertEqual(code, 0)
            self.assertNotIn("STATUS: running", line)
            self.assertRegex(line, r"STATUS: (done|failed)")
            self.assertLess(elapsed, 5)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            reap_test_child(pid)


def wait_until_settled(pid, timeout=5):
    deadline = time.monotonic() + timeout
    state = ""
    while True:
        proc = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        state = proc.stdout.strip()
        if state.startswith("Z") or state == "" or time.monotonic() >= deadline:
            return state
        time.sleep(0.02)


def reap_test_child(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    proc = runner._children.pop(pid, None)
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
