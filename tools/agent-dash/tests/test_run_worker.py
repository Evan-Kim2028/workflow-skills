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
        self.envs = []
        self.alive = alive
        self.quota_on = quota_on
        self.report = report
        self.n = 0

    def spawn(self, cmd, out_path, err_path, env=None):
        self.n += 1
        self.cmds.append(list(cmd))
        self.envs.append(dict(env or {}))
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

    def go(self, tier, launch, label="ws1-plan-lane-1", max_wait="0", no_preflight=True):
        from io import StringIO
        import contextlib

        argv = [tier, label, str(self.ws), str(self.prompt), str(self.run_dir), max_wait]
        if no_preflight:
            argv.append("--no-preflight")
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

    def test_example_lists_the_launch_tiers(self):
        data = registry()
        self.assertEqual(data["default"], "muse")
        self.assertEqual(data["auto_order"], ["muse", "grok", "deepseek-flash", "glm-flash", "glm"])
        self.assertEqual(
            set(data["workers"]),
            {"muse", "grok", "deepseek-flash", "glm-flash", "glm", "pi-deepseek", "pi-beam"},
        )
        kinds = {name: row["kind"] for name, row in data["workers"].items()}
        self.assertEqual(kinds["muse"], "muse")
        self.assertEqual(kinds["grok"], "cursor")
        self.assertEqual(kinds["glm"], "grok-build")
        self.assertEqual(kinds["glm-flash"], "grok-build")
        self.assertEqual(kinds["deepseek-flash"], "grok-build")
        self.assertEqual(kinds["pi-deepseek"], "pi")
        self.assertEqual(kinds["pi-beam"], "pi")
        caps = {name: row["cap"] for name, row in data["workers"].items()}
        self.assertEqual(caps["deepseek-flash"], 5)
        self.assertEqual(caps["glm-flash"], 5)
        self.assertEqual(caps["glm"], 2)
        self.assertEqual(caps["pi-deepseek"], 12)
        self.assertEqual(caps["pi-beam"], 5)
        for name, row in data["workers"].items():
            self.assertIn("[", row["pgrep"])
            self.assertTrue(row["enabled"])
            self.assertEqual(row["manifest"], "pi-sessions.tsv" if row["kind"] == "pi" else row["manifest"])
        # Every Grok Build tier cools on the GLM request-limit wording too.
        for name in ("deepseek-flash", "glm", "glm-flash"):
            self.assertIn("rate limit reached for requests", data["workers"][name]["quota_patterns"])

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
        self.assertEqual(row, "ws1-plan-lane-1\t%s\t1700000000\tmuse-spark-1.3-contributor\tmuse\tmuse" % SID)
        self.assertEqual((self.run_dir / "ws1-plan-lane-1.tier").read_text(encoding="utf-8").strip(), "muse")

    def test_auto_skips_a_worker_at_its_cap(self):
        self.counts = {"[m]use-bin|[m]use exec": 25}
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=grok", line)
        self.assertTrue(launch.cmds[0][0].endswith("launch_grok.sh"))
        self.assertEqual(launch.cmds[0][4], registry()["workers"]["grok"]["model"])
        text = (self.run_dir / "cursor-sessions.tsv").read_text(encoding="utf-8").strip()
        self.assertEqual(
            text,
            "ws1-plan-lane-1\t-\t1700000000\t%s\tgrok\tcursor" % registry()["workers"]["grok"]["model"],
        )

    def test_weak_label_refuses_before_launch(self):
        # No mark, no task match: exit 2 with the fix, nothing spawned,
        # no tier file, no manifest row. Workstreams point at the empty
        # temp dir so no live plan can rescue the label.
        self.counts = {}
        launch = Launch()
        old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root / "noworkstreams")
        try:
            code, line = self.go("default", launch, label="ws1-visuals")
        finally:
            if old_ws is None:
                os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
            else:
                os.environ["AGENT_DASH_WORKSTREAMS"] = old_ws
        self.assertEqual(code, 2)
        self.assertIn("reason=bad-label", line)
        self.assertEqual(launch.cmds, [])
        self.assertFalse((self.run_dir / "ws1-visuals.tier").exists())
        self.assertFalse((self.run_dir / "muse-sessions.tsv").exists())

    def test_legacy_hatch_warns_and_launches(self):
        self.counts = {}
        launch = Launch()
        old = os.environ.get("AGENT_DASH_ALLOW_LEGACY_LABELS")
        os.environ["AGENT_DASH_ALLOW_LEGACY_LABELS"] = "1"
        try:
            code, line = self.go("default", launch, label="ws1-visuals")
        finally:
            if old is None:
                os.environ.pop("AGENT_DASH_ALLOW_LEGACY_LABELS", None)
            else:
                os.environ["AGENT_DASH_ALLOW_LEGACY_LABELS"] = old
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertEqual(len(launch.cmds), 1)

    def test_append_manifest_refuses_a_weak_label(self):
        old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root / "noworkstreams")
        try:
            with self.assertRaises(ValueError):
                runner.append_manifest(
                    {"kind": "muse"}, self.run_dir, "ws1-visuals", "s", NOW, "m"
                )
        finally:
            if old_ws is None:
                os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
            else:
                os.environ["AGENT_DASH_WORKSTREAMS"] = old_ws
        self.assertFalse((self.run_dir / "muse-sessions.tsv").exists())

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
        self.assertEqual(row, ["ws1-plan-lane-1", SID, "1700000000", "glm-5.3", "glm", "grok-cli"])

    def test_sparse_grok_build_worker_lands_in_glm_manifest_with_model(self):
        # A grok-build tier without manifest/columns wiring (like a bare
        # deepseek-flash entry) still lands in glm-sessions.tsv with its
        # model, where the collectors read it — never in sessions.tsv.
        data = registry()
        data["workers"]["deepseek-flash"] = {
            "enabled": True,
            "kind": "grok-build",
            "model": "deepseek-flash",
            "cap": 10,
        }
        self.reg.write_text(json.dumps(data), encoding="utf-8")
        self.counts = {}
        launch = Launch()
        code, line = self.go("deepseek-flash", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=deepseek-flash", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[0], "grok")
        self.assertEqual(cmd[cmd.index("-m") + 1], "deepseek-flash")
        self.assertFalse((self.run_dir / "sessions.tsv").exists())
        row = (self.run_dir / "glm-sessions.tsv").read_text(encoding="utf-8").strip().split("\t")
        self.assertEqual(
            row, ["ws1-plan-lane-1", SID, "1700000000", "deepseek-flash", "deepseek-flash", "grok-cli"]
        )

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
        archived = (self.run_dir / "err-ws1-plan-lane-1.muse.log").read_text(encoding="utf-8")
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
        self.assertFalse((self.run_dir / "out-ws1-plan-lane-1.muse.md").exists())

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

    def test_pi_tier_launches_launch_pi_with_tier_and_records_it(self):
        # One launcher for every tier: a pi registry tier runs launch_pi.py
        # in the run folder's pi/ outdir and records its tier name, so the
        # dashboard reads pi-deepseek instead of the bare harness "pi".
        self.counts = {}
        launch = Launch()
        code, line = self.go("pi-deepseek", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=pi-deepseek", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[0], "python3")
        self.assertTrue(cmd[1].endswith("launch_pi.py"))
        self.assertEqual(cmd[5], str(self.run_dir / "pi"))
        self.assertEqual(cmd[cmd.index("--model") + 1], registry()["workers"]["pi-deepseek"]["model"])
        self.assertEqual(cmd[cmd.index("--tier") + 1], "pi-deepseek")
        self.assertEqual(launch.envs[0], {})
        row = (self.run_dir / "pi-sessions.tsv").read_text(encoding="utf-8").splitlines()[-1].split("\t")
        self.assertEqual(
            row[:4],
            ["ws1-plan-lane-1", SID, "1700000000", registry()["workers"]["pi-deepseek"]["model"]],
        )
        self.assertEqual(row[4], "pi-deepseek")

    def test_standby_env_file_switches_the_credential_without_reading_it(self):
        # A pi tier that declares standby_env_file launches on the standby
        # credential: the tier records <tier>-standby, the env file path goes
        # to the launcher as a variable, and its contents never travel.
        data = registry()
        data["workers"]["pi-deepseek"]["standby_env_file"] = "~/.config/pi/standby.env"
        self.reg.write_text(json.dumps(data), encoding="utf-8")
        self.counts = {}
        launch = Launch()
        code, line = self.go("pi-deepseek", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=pi-deepseek-standby", line)
        env_path = os.path.expanduser("~/.config/pi/standby.env")
        self.assertEqual(launch.envs[0], {"AGENT_DASH_PI_ENV_FILE": env_path})
        tier = (self.run_dir / "ws1-plan-lane-1.tier").read_text(encoding="utf-8").strip()
        self.assertEqual(tier, "pi-deepseek-standby")
        row = (self.run_dir / "pi-sessions.tsv").read_text(encoding="utf-8").splitlines()[-1].split("\t")
        self.assertEqual(row[4], "pi-deepseek-standby")

    def test_meta_file_records_tier_harness_and_paths(self):
        self.counts = {}
        launch = Launch()
        self.go("glm-flash", launch)
        meta = json.loads((self.run_dir / "ws1-plan-lane-1.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["tier"], "glm-flash")
        self.assertEqual(meta["harness"], "grok-cli")
        self.assertEqual(meta["model"], "glm-5.3-flash")
        self.assertEqual(meta["label"], "ws1-plan-lane-1")
        self.assertEqual(meta["pid"], 4001)
        self.assertTrue(meta["out_path"].endswith("out-ws1-plan-lane-1.md"))
        self.assertTrue(meta["err_path"].endswith("err-ws1-plan-lane-1.log"))

    def test_grok_build_harness_is_grok_in_the_meta_file(self):
        self.counts = {}
        launch = Launch()
        self.go("deepseek-flash", launch)
        meta = json.loads((self.run_dir / "ws1-plan-lane-1.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["harness"], "grok-cli")
        self.assertEqual(meta["tier"], "deepseek-flash")

    def test_request_rate_limit_wording_cools_the_tier(self):
        # GLM says "Rate limit reached for requests"; that is a quota hit,
        # not a done run, even when the out file has text.
        self.counts = {}

        class RateLimited(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                pid = super().spawn(cmd, out_path, err_path, env)
                Path(err_path).write_text("Rate limit reached for requests\n", encoding="utf-8")
                Path(out_path).write_text("partial text\n", encoding="utf-8")
                return pid

        launch = RateLimited()
        code, line = self.go("glm", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=quota", line)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["glm"], NOW + 30 * 60)

    def test_auto_skips_a_cooling_tier_for_the_next_one(self):
        (self.state / "worker-cooldowns.json").write_text(
            json.dumps({"deepseek-flash": NOW + 600}), encoding="utf-8"
        )
        self.counts = {"[m]use-bin|[m]use exec": 25, "[c]ursor-agent": 25}
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=glm-flash", line)

    def test_none_available_when_every_worker_is_at_cap(self):
        self.counts = {
            "[m]use-bin|[m]use exec": 25,
            "[c]ursor-agent": 25,
            "-m [g]lm-5.3($| )": 2,
            "-m [g]lm-5.3-flash": 5,
            "-m [d]eepseek-flash": 5,
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


class PreflightTests(unittest.TestCase):
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
        self.counts = {}
        from http.server import BaseHTTPRequestHandler, HTTPServer

        run_dir = str(self.run_dir)

        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/health":
                    body = b"{}"
                    self.send_response(404)
                else:
                    body = json.dumps(
                        {
                            "ok": True,
                            "sources": [],
                            "scratch_roots": [run_dir],
                            "run_dirs": [],
                            "warnings": [],
                        }
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.stub = HTTPServer(("127.0.0.1", 0), Stub)
        import threading

        self.stub_thread = threading.Thread(
            target=self.stub.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.stub_thread.start()
        self.keep_url = os.environ.get("AGENT_DASH_URL")
        os.environ["AGENT_DASH_URL"] = f"http://127.0.0.1:{self.stub.server_address[1]}"

    def tearDown(self):
        self.stub.shutdown()
        self.stub_thread.join(3)
        self.stub.server_close()
        if self.keep_url is None:
            os.environ.pop("AGENT_DASH_URL", None)
        else:
            os.environ["AGENT_DASH_URL"] = self.keep_url
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def pgrep(self, pattern):
        return self.counts.get(pattern, 0)

    def launch(self, tier, label="ws1-plan-lane-1", no_preflight=False):
        from io import StringIO
        import contextlib

        out, err = StringIO(), StringIO()
        launch = Launch()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner.run(
                [tier, label, str(self.ws), str(self.prompt), str(self.run_dir), "0"]
                + ([] if not no_preflight else ["--no-preflight"]),
                spawn=launch.spawn,
                pgrep=self.pgrep,
                sleep=lambda _s: None,
                now=lambda: NOW,
                alive=launch.is_alive,
                new_uuid=lambda: SID,
            )
        return code, out.getvalue().strip(), err.getvalue().strip(), launch

    def _write_registry(self, workers):
        data = registry()
        data["workers"] = workers
        self.reg.write_text(json.dumps(data), encoding="utf-8")

    def test_preflight_down_warns_and_launches(self):
        os.environ["AGENT_DASH_URL"] = "http://127.0.0.1:1"
        self.counts = {}
        code, out, err, launch = self.launch("default")
        self.assertEqual(code, 0)
        self.assertEqual(len(launch.cmds), 1)
        self.assertIn("preflight warning (dashboard-down)", err)
        self.assertIn("Launching anyway", err)
        self.assertIn("STATUS: done", out)

    def test_no_preflight_skips_the_gate(self):
        os.environ["AGENT_DASH_URL"] = "http://127.0.0.1:1"
        self.counts = {}
        code, out, _err, launch = self.launch("default", no_preflight=True)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", out)
        self.assertEqual(len(launch.cmds), 1)

    def test_preflight_passes_and_launches(self):
        self.counts = {}
        code, out, _err, launch = self.launch("default")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", out)
        self.assertIn("tier=muse", out)
        self.assertEqual(len(launch.cmds), 1)

    def test_preflight_blocks_unpriced_model(self):
        workers = registry()["workers"]
        workers["muse"] = dict(workers["muse"], model="no-such-model-xyz")
        self._write_registry(workers)
        self.counts = {}
        code, _out, err, launch = self.launch("muse")
        self.assertEqual(code, 1)
        self.assertEqual(launch.cmds, [])
        self.assertIn("preflight (unpriced-model)", err)

    def test_preflight_blocks_policy_breach(self):
        workers = registry()["workers"]
        workers["muse"] = dict(workers["muse"], extra_args=["--trust-workspace"])
        self._write_registry(workers)
        self.counts = {}
        code, _out, err, launch = self.launch("muse")
        self.assertEqual(code, 1)
        self.assertEqual(launch.cmds, [])
        self.assertIn("preflight (model-policy)", err)


if __name__ == "__main__":
    unittest.main()
