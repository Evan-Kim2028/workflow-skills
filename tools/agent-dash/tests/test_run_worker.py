"""Registry worker launcher. Fake processes only. No real agents."""
import contextlib
import importlib.util
import io
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


def without_scratch(env):
    """The launch env minus AGENT_SCRATCH (asserted separately)."""
    return {k: v for k, v in dict(env or {}).items() if k != "AGENT_SCRATCH"}


FIXTURE = HERE / "fixtures" / "workers_launcher.json"


def registry():
    """Launcher fixture: the tier names, caps and order these tests pin.

    Kept apart from the shipped file so a registry change does not move
    the launcher's behaviour tests; shipped_registry() reads the real one.
    """
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def shipped_registry():
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
            "AGENT_DASH_SCRATCH_ROOT": os.environ.get("AGENT_DASH_SCRATCH_ROOT"),
        }
        os.environ["AGENT_DASH_WORKERS"] = str(self.reg)
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state)
        os.environ["AGENT_DASH_SCRATCH_ROOT"] = str(self.root / "scratch")

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
        data = shipped_registry()
        self.assertEqual(data["default"], "pi-anthropic-haiku")
        self.assertEqual(data["auto_order"], ["pi-anthropic-haiku", "pi-deepseek-flash"])
        self.assertEqual(
            set(data["workers"]),
            {"pi-anthropic-haiku", "pi-deepseek-flash", "muse", "grok-build-47", "deepseek-flash",
             "glm", "glm-flash", "pi-beam", "codex-luna"},
        )
        kinds = {name: row["kind"] for name, row in data["workers"].items()}
        self.assertNotIn("cursor", kinds.values())  # retired; no tier may use it
        self.assertTrue(set(kinds.values()) <= set(runner.KINDS))
        self.assertEqual(kinds["muse"], "muse")
        self.assertEqual(kinds["grok-build-47"], "grok-build")
        self.assertEqual(kinds["glm"], "grok-build")
        self.assertEqual(kinds["glm-flash"], "grok-build")
        self.assertEqual(kinds["deepseek-flash"], "grok-build")
        self.assertEqual(kinds["pi-beam"], "pi")
        self.assertEqual(kinds["pi-anthropic-haiku"], "pi")
        self.assertEqual(kinds["codex-luna"], "codex")
        self.assertEqual(kinds["pi-deepseek-flash"], "pi")
        manifests = {name: row["manifest"] for name, row in data["workers"].items()}
        self.assertEqual(manifests["codex-luna"], "codex-sessions.tsv")
        self.assertEqual(manifests["pi-deepseek-flash"], "pi-sessions.tsv")
        self.assertEqual(manifests["pi-anthropic-haiku"], "pi-sessions.tsv")
        self.assertEqual(manifests["glm"], "glm-sessions.tsv")
        tiers = {name: row["pgrep"] for name, row in data["workers"].items()}
        self.assertIn("--tier codex-luna", tiers["codex-luna"])
        self.assertIn("--tier pi-deepseek-flash", tiers["pi-deepseek-flash"])
        self.assertIn("--tier pi-anthropic-haiku", tiers["pi-anthropic-haiku"])
        for name, row in data["workers"].items():
            self.assertIn("[", row["pgrep"])
            self.assertTrue(row["enabled"])
            self.assertIn("cap", row)
            self.assertIn("quota_patterns", row)
            self.assertIn("columns", row)
        # Every Grok Build tier cools on the GLM request-limit wording too.
        for name in ("grok-build-47", "deepseek-flash", "glm", "glm-flash"):
            self.assertIn("rate limit reached for requests", data["workers"][name]["quota_patterns"])
        for name in data["auto_order"]:
            self.assertIn(name, data["workers"])
            self.assertTrue(data["workers"][name]["enabled"])

    def test_every_shipped_tier_is_priced_and_policy_clean(self):
        from dash.doctor import effort_from_extra_args, model_policy_reason
        from dash.pricing import status

        for name, row in shipped_registry()["workers"].items():
            self.assertTrue(status(row["model"])["priced"], name)
            reason = model_policy_reason(row["kind"], row["model"], effort_from_extra_args(row), None)
            self.assertIsNone(reason, name)

    def _write_registry(self, workers):
        data = registry()
        data["workers"] = workers
        self.reg.write_text(json.dumps(data), encoding="utf-8")

    def _with_unpriced(self, names):
        workers = registry()["workers"]
        for name in names:
            workers[name] = dict(workers[name], model="no-such-model-xyz")
        self._write_registry(workers)

    def test_auto_skips_an_unpriced_tier_for_the_next_one(self):
        self._with_unpriced(["muse"])  # first in the fixture's auto_order
        self.counts = {}
        launch = Launch()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=glm-flash", line)
        self.assertEqual(launch.cmds[0][0], "grok")
        self.assertNotIn("muse", launch.cmds[0][0])
        self.assertIn("skipping muse", err.getvalue())
        self.assertIn("no catalog price", err.getvalue())

    def test_auto_skips_every_unpriced_tier_then_reports_none_available(self):
        self._with_unpriced(["muse", "glm-flash", "codex-luna", "pi-deepseek-flash"])
        self.counts = {}
        launch = Launch()
        with contextlib.redirect_stderr(io.StringIO()):
            code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("reason=none-available", line)
        self.assertEqual(launch.cmds, [])

    def test_named_unpriced_tier_keeps_launching_without_preflight(self):
        # Only auto skips unpriced tiers. A named tier still launches; the
        # preflight is what refuses it (test_preflight_blocks_unpriced_model).
        self._with_unpriced(["muse"])
        self.counts = {}
        launch = Launch()
        code, line = self.go("muse", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=muse", line)
        self.assertEqual(launch.cmds[0][0], "muse")

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
        # The muse gets the assembled copy, never the caller's file.
        self.assertIn(str(self.run_dir / "ws1-plan-lane-1.md"), cmd)
        self.assertNotIn(str(self.prompt), cmd)
        self.assertIn("muse-spark-1.3-contributor", cmd)
        row = (self.run_dir / "muse-sessions.tsv").read_text(encoding="utf-8").strip()
        self.assertEqual(row, "ws1-plan-lane-1\t%s\t1700000000\tmuse-spark-1.3-contributor\tmuse\tmuse" % SID)
        self.assertEqual((self.run_dir / "ws1-plan-lane-1.tier").read_text(encoding="utf-8").strip(), "muse")

    def test_auto_skips_a_worker_at_its_cap(self):
        self.counts = {"[m]use-bin|[m]use exec": 25}
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=glm-flash", line)
        self.assertEqual(launch.cmds[0][0], "grok")
        self.assertEqual(launch.cmds[0][launch.cmds[0].index("-m") + 1], "glm-5.3-flash")
        row = (self.run_dir / "glm-sessions.tsv").read_text(encoding="utf-8").strip().split("\t")
        self.assertEqual(
            row, ["ws1-plan-lane-1", SID, "1700000000", "glm-5.3-flash", "glm-flash", "grok-cli"]
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
        self.assertIn("tier=glm-flash", line)
        self.assertEqual(len(launch.cmds), 1)

    def test_glm_appends_the_run_rule_and_logs_the_model(self):
        self.counts = {}
        launch = Launch()
        code, line = self.go("glm", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=glm", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[:2], ["grok", "-p"])
        self.assertTrue(cmd[2].startswith("[label: ws1-plan-lane-1]\n"))
        self.assertTrue(cmd[2].endswith("do the task\n\n" + runner.RUN_RULE))
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
        self.assertIn("tier=glm-flash", line)
        self.assertEqual(len(launch.cmds), 2)
        self.assertEqual(launch.cmds[0][0], "muse")
        self.assertEqual(launch.cmds[1][0], "grok")
        self.assertEqual(launch.cmds[1][launch.cmds[1].index("-m") + 1], "glm-5.3-flash")
        archived = (self.run_dir / "err-ws1-plan-lane-1.muse.log").read_text(encoding="utf-8")
        self.assertIn("429", archived)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["muse"], NOW + 30 * 60)

    def test_quota_falls_back_through_the_owner_chain(self):
        # muse and glm-flash both rate-limited: auto walks the fallback
        # order down to codex-luna (muse -> glm-flash -> codex-luna).
        self.counts = {}

        class TwiceLimited(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                self.n += 1
                self.cmds.append(list(cmd))
                out_path = Path(out_path)
                err_path = Path(err_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                if self.n <= 2:
                    err_path.write_text("HTTP 429 rate limit\n", encoding="utf-8")
                    out_path.write_text("", encoding="utf-8")
                else:
                    err_path.write_text("", encoding="utf-8")
                    out_path.write_text(self.report, encoding="utf-8")
                return 4000 + self.n

        launch = TwiceLimited()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=codex-luna", line)
        self.assertEqual(len(launch.cmds), 3)
        self.assertEqual(launch.cmds[0][0], "muse")
        self.assertEqual(launch.cmds[1][0], "grok")
        self.assertTrue(launch.cmds[2][1].endswith("launch_codex.py"))
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["muse"], NOW + 30 * 60)
        self.assertEqual(cooled["glm-flash"], NOW + 30 * 60)
        self.assertNotIn("codex-luna", cooled)

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
        self.assertEqual(without_scratch(launch.envs[0]), {})
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
        self.assertEqual(without_scratch(launch.envs[0]), {"AGENT_DASH_PI_ENV_FILE": env_path})
        tier = (self.run_dir / "ws1-plan-lane-1.tier").read_text(encoding="utf-8").strip()
        self.assertEqual(tier, "pi-deepseek-standby")
        row = (self.run_dir / "pi-sessions.tsv").read_text(encoding="utf-8").splitlines()[-1].split("\t")
        self.assertEqual(row[4], "pi-deepseek-standby")

    def test_codex_tier_launches_launch_codex_with_tier_and_records_it(self):
        # A codex registry tier runs launch_codex.py in the run folder
        # itself (so the manifests source finds the codex-sessions.tsv
        # row beside out-<label>.md) and records its tier name.
        self.counts = {}
        launch = Launch()
        code, line = self.go("codex-luna", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=codex-luna", line)
        cmd = launch.cmds[0]
        self.assertEqual(cmd[0], "python3")
        self.assertTrue(cmd[1].endswith("launch_codex.py"))
        self.assertEqual(cmd[5], str(self.run_dir))
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-6-luna")
        self.assertEqual(cmd[cmd.index("--tier") + 1], "codex-luna")
        self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "xhigh")
        self.assertEqual(without_scratch(launch.envs[0]), {})
        row = (self.run_dir / "codex-sessions.tsv").read_text(encoding="utf-8").splitlines()[-1].split("\t")
        self.assertEqual(
            row[:4],
            ["ws1-plan-lane-1", SID, "1700000000", "gpt-6-luna"],
        )
        self.assertEqual(row[4], "codex-luna")
        self.assertEqual(row[5], "codex")

    def test_codex_harness_is_codex_in_the_meta_file(self):
        self.counts = {}
        launch = Launch()
        self.go("codex-luna", launch)
        meta = json.loads((self.run_dir / "ws1-plan-lane-1.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["harness"], "codex")
        self.assertEqual(meta["tier"], "codex-luna")
        self.assertEqual(meta["model"], "gpt-6-luna")

    def test_quota_marker_file_cools_the_tier(self):
        # A launcher QUOTA marker naming this label is a quota hit even
        # when the report tails show no quota wording (empty report).
        self.counts = {}

        class Marked(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                pid = super().spawn(cmd, out_path, err_path, env)
                Path(out_path).write_text("", encoding="utf-8")
                quota = self_run_dir / "QUOTA-ws1-plan-lane-1"
                quota.write_text("model\tgpt-6-luna\nlabel\tws1-plan-lane-1\n", encoding="utf-8")
                return pid

        self_run_dir = self.run_dir
        launch = Marked()
        code, line = self.go("codex-luna", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=quota", line)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["codex-luna"], NOW + 30 * 60)

    def test_stale_quota_marker_does_not_cool_the_tier(self):
        # A QUOTA marker older than this attempt's tier file belongs to a
        # previous attempt and never cools the relaunch.
        self.counts = {}
        stale = self.run_dir / "pi" / "QUOTA"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("model\triver/deepseek-ai/DeepSeek-V4.1-Flash-1017\nlabel\tws1-plan-lane-1\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        launch = Launch()
        code, line = self.go("pi-deepseek-flash", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=pi-deepseek-flash", line)
        self.assertFalse((self.state / "worker-cooldowns.json").exists())

    def test_pi_summary_retry_line_never_reads_quota(self):
        # For a pi tier the outer out file is launch_pi.py's summary line
        # and the outer err log its stderr; pi's stream and stderr stay in
        # the run folder's pi/ outdir. A recovered rate-limit retry
        # recorded there as rate_limit_retries=<n> must not match the pi
        # quota_patterns ("rate limit", "429", ...) and cool the tier.
        self.counts = {}

        class Retried(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                pid = super().spawn(cmd, out_path, err_path, env)
                Path(out_path).write_text(
                    "launched pi ws1-plan-lane-1"
                    " model river/deepseek-ai/DeepSeek-V4.1-Flash-1017"
                    " status done verdict PASS rate_limit_retries=1\n",
                    encoding="utf-8",
                )
                return pid

        launch = Retried()
        code, line = self.go("pi-deepseek-flash", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=pi-deepseek-flash", line)
        self.assertFalse((self.state / "worker-cooldowns.json").exists())

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

    def test_finished_report_discussing_quota_stays_done(self):
        # A complete PASS report may mention "rate limit", "quota" and
        # "429" (a run about fixing 429 handling); that is a done run,
        # never a quota cooldown (2026-10-30 false positive).
        self.counts = {}
        body = (
            "PASS\n\n"
            "## Report: fix 429 handling\n\n"
            "The launcher retried on rate limit responses and treated the\n"
            "quota wording in the report tails as a quota hit. The fix maps\n"
            "HTTP 429 to a retry, so the tier no longer cools down.\n"
        )
        body += "notes line\n" * 200
        self.assertGreater(len(body.strip()), runner.REPORT_MIN_BYTES)
        launch = Launch(report=body)
        code, line = self.go("glm-flash", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertIn("tier=glm-flash", line)
        self.assertFalse((self.state / "worker-cooldowns.json").exists())

    def test_glued_verdict_after_narration_stays_done(self):
        # grok-build plain output runs the narration into the report with no
        # newline ("...commit on the branch:PASS"); the verdict still counts.
        self.counts = {}
        body = "Let me read the quota scan and rate limit tests. " * 20
        body += "Now commit on the branch:PASS\n\nFixed the 429 quota scan.\n"
        body += "quota rate limit 429 notes\n" * 40
        self.assertGreater(len(body.strip()), runner.REPORT_MIN_BYTES)
        code, line = self.go("glm-flash", Launch(report=body))
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertFalse((self.state / "worker-cooldowns.json").exists())

    def test_short_report_with_rate_limit_wording_is_quota(self):
        # "Rate limit reached for requests" as the whole out file is the
        # real failure shape: a short report that ends in the limit.
        self.counts = {}

        class RateLimited(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                pid = super().spawn(cmd, out_path, err_path, env)
                Path(err_path).write_text("", encoding="utf-8")
                Path(out_path).write_text("Rate limit reached for requests\n", encoding="utf-8")
                return pid

        launch = RateLimited()
        code, line = self.go("glm", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=quota", line)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["glm"], NOW + 30 * 60)

    def test_quota_wording_in_err_cools_even_behind_a_long_report(self):
        # The err log is always scanned; a long finished report does not
        # hide limit wording that landed in stderr.
        self.counts = {}

        class ErrLimited(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                pid = super().spawn(cmd, out_path, err_path, env)
                Path(err_path).write_text("Rate limit reached for requests\n", encoding="utf-8")
                Path(out_path).write_text(
                    "PASS\n\n" + "unrelated body text\n" * 200, encoding="utf-8"
                )
                return pid

        launch = ErrLimited()
        code, line = self.go("glm", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=quota", line)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["glm"], NOW + 30 * 60)

    def test_truncated_long_report_ending_in_quota_wording_is_quota(self):
        # A long report with no verdict line that ends in the limit
        # wording (match inside the last REPORT_TAIL_BYTES) is a run the
        # limit cut off, so the tier cools.
        self.counts = {}
        body = "working on the task\n" * 200 + "Rate limit reached for requests\n"
        self.assertGreaterEqual(len(body.strip()), runner.REPORT_MIN_BYTES)
        self.assertLessEqual(
            len(body) - body.index("Rate limit"), runner.REPORT_TAIL_BYTES
        )

        class Truncated(Launch):
            def spawn(self, cmd, out_path, err_path, env=None):
                pid = super().spawn(cmd, out_path, err_path, env)
                Path(err_path).write_text("", encoding="utf-8")
                Path(out_path).write_text(body, encoding="utf-8")
                return pid

        launch = Truncated()
        code, line = self.go("glm", launch)
        self.assertEqual(code, 0)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=quota", line)
        cooled = json.loads((self.state / "worker-cooldowns.json").read_text(encoding="utf-8"))
        self.assertEqual(cooled["glm"], NOW + 30 * 60)

    def test_auto_skips_a_cooling_tier_for_the_next_one(self):
        (self.state / "worker-cooldowns.json").write_text(
            json.dumps({"muse": NOW + 600, "glm-flash": NOW + 600}), encoding="utf-8"
        )
        self.counts = {}
        launch = Launch()
        code, line = self.go("auto", launch)
        self.assertEqual(code, 0)
        self.assertIn("tier=codex-luna", line)

    def test_none_available_when_every_worker_is_at_cap(self):
        self.counts = {
            "[m]use-bin|[m]use exec": 25,
            "-m [g]lm-5.3-flash": 5,
            "[l]aunch_codex.py.*--tier codex-luna": 3,
            "[l]aunch_pi.py.*--tier pi-deepseek-flash": 5,
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


    def test_exclude_keeps_auto_off_the_failed_tier(self):
        self.counts = {}
        launch = Launch()
        from io import StringIO
        import contextlib

        argv = ["auto", "ws1-exec-lane-1-r1", str(self.ws), str(self.prompt), str(self.run_dir), "0",
                "--no-preflight", "--exclude=muse"]
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            code = runner.run(argv, spawn=launch.spawn, pgrep=self.pgrep, sleep=lambda _s: None,
                              now=lambda: NOW, alive=launch.is_alive, new_uuid=lambda: SID)
        self.assertEqual(code, 0)
        self.assertIn("tier=glm-flash", buf.getvalue())
        self.assertNotIn("tier=muse", buf.getvalue())
        self.assertNotEqual(launch.cmds[0][0], "muse")

    def test_eligible_never_offers_an_excluded_tier_on_a_later_observe(self):
        reg = registry()
        ctx = {
            "registry": reg,
            "hooks": {"now": lambda: NOW, "pgrep": lambda pattern: 0},
            "cooldowns": {},
            "minutes": 1,
            "exclude": {"muse"},
        }
        self.assertNotEqual(runner.eligible(ctx, set()), "muse")
        self.assertIsNotNone(runner.eligible(ctx, set()))

    def test_zero_wait_detaches_and_later_call_observes(self):
        launch = Launch(alive=True)
        code, line = self.go("muse", launch, label="ws1-exec-lane-1")
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("STATUS: running"), line)
        self.assertEqual(len(launch.cmds), 1)
        launch.alive = False
        code, line = self.go("muse", launch, label="ws1-exec-lane-1")
        self.assertEqual(len(launch.cmds), 1, "second call must observe, not relaunch")
        self.assertTrue(line.startswith("STATUS: done"), line)


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
            "AGENT_DASH_SCRATCH_ROOT": os.environ.get("AGENT_DASH_SCRATCH_ROOT"),
        }
        os.environ["AGENT_DASH_WORKERS"] = str(self.reg)
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state)
        os.environ["AGENT_DASH_SCRATCH_ROOT"] = str(self.root / "scratch")
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

    def test_preflight_passes_for_codex_and_pi_tiers(self):
        # gpt-6-luna and the DeepSeek V4.1 Flash pi id are priced, and the
        # codex tier carries the xhigh effort the policy requires.
        self.counts = {}
        code, out, _err, launch = self.launch("codex-luna", label="ws1-plan-codex")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", out)
        self.assertIn("tier=codex-luna", out)
        self.assertEqual(len(launch.cmds), 1)
        code, out, _err, launch = self.launch("pi-deepseek-flash", label="ws1-plan-pi")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", out)
        self.assertIn("tier=pi-deepseek-flash", out)

    def test_preflight_blocks_codex_policy_breach(self):
        workers = registry()["workers"]
        workers["codex-luna"] = dict(
            workers["codex-luna"], extra_args=["--reasoning-effort", "low"]
        )
        self._write_registry(workers)
        self.counts = {}
        code, _out, err, launch = self.launch("codex-luna")
        self.assertEqual(code, 1)
        self.assertEqual(launch.cmds, [])
        self.assertIn("preflight (model-policy)", err)

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


class CooldownRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = {
            "AGENT_DASH_WORKERS": os.environ.get("AGENT_DASH_WORKERS"),
            "AGENT_DASH_STATE_DIR": os.environ.get("AGENT_DASH_STATE_DIR"),
            "AGENT_DASH_SCRATCH_ROOT": os.environ.get("AGENT_DASH_SCRATCH_ROOT"),
        }
        os.environ["AGENT_DASH_WORKERS"] = str(self.root / "workers.json")
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state)
        os.environ["AGENT_DASH_SCRATCH_ROOT"] = str(self.root / "scratch")

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_concurrent_cooldown_writes_do_not_race(self):
        import threading

        path = runner.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        errors = []

        def writer(name):
            try:
                ctx = {
                    "hooks": {"now": lambda: NOW},
                    "minutes": 30,
                    "cooldowns": {},
                }
                runner.note_quota(ctx, name)
            except Exception as exc:
                errors.append(exc)

        threads = []
        for i in range(20):
            t = threading.Thread(target=writer, args=(f"tier-{i}",))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        data = runner.read_cooldowns(path)
        for i in range(20):
            self.assertIn(f"tier-{i}", data)

RULES_HEADING = "# Worker rules (include in every muse and Haiku worker prompt)"
TEMPLATE_TEXT = (
    "<!-- agent-dash:worker-rules v1 -->\n"
    + RULES_HEADING + "\n\nRules body.\n"
)


class PromptAssemblyTests(unittest.TestCase):
    """Label line, then the rules template, then the caller's text; idempotent."""

    def assemble(self, text, label="ws1-plan-x", template=TEMPLATE_TEXT, heading=RULES_HEADING):
        return runner.assemble_prompt(text, label, template, heading)

    def test_plain_prompt_gets_label_then_rules_then_text(self):
        out, notes = self.assemble("do the task\n")
        self.assertEqual(notes, [])
        self.assertEqual(out, "[label: ws1-plan-x]\n\n" + TEMPLATE_TEXT.rstrip("\n") + "\n\ndo the task\n")

    def test_assembling_its_own_output_changes_nothing(self):
        once, _ = self.assemble("do the task\n")
        twice, notes = self.assemble(once)
        self.assertEqual(twice, once)
        self.assertEqual(notes, [])

    def test_marker_prompt_is_not_given_rules_again(self):
        text = "[label: ws1-plan-x]\n" + TEMPLATE_TEXT + "\ndo it\n"
        out, _ = self.assemble(text)
        self.assertEqual(out.count(RULES_HEADING), 1)
        self.assertEqual(out.count("agent-dash:worker-rules"), 1)

    def test_pre_marker_pasted_rules_are_detected_by_heading(self):
        # Prompts written before the marker carry the heading, not the marker.
        pasted = RULES_HEADING + "\n\nAn old pasted block.\n\n## Task\ndo it\n"
        out, _ = self.assemble(pasted)
        self.assertEqual(out.count(RULES_HEADING), 1)
        self.assertNotIn("agent-dash:worker-rules", out)
        self.assertTrue(out.startswith("[label: ws1-plan-x]\n"))

    def test_caller_label_line_stays_first_and_is_not_repeated(self):
        out, notes = self.assemble("[label: ws1-plan-x]\ndo it\n")
        self.assertEqual(out.count("[label:"), 1)
        self.assertTrue(out.startswith("[label: ws1-plan-x]\n\n"))
        self.assertEqual(notes, [])

    def test_caller_label_for_another_run_is_kept_with_a_note(self):
        out, notes = self.assemble("[label: other-run]\ndo it\n")
        self.assertTrue(out.startswith("[label: other-run]\n"))
        self.assertEqual(out.count("[label:"), 1)
        self.assertEqual(len(notes), 1)
        self.assertIn("other-run", notes[0])

    def test_missing_template_warns_and_keeps_the_label_and_text(self):
        out, notes = self.assemble("do it\n", template=None, heading=None)
        self.assertEqual(out, "[label: ws1-plan-x]\n\ndo it\n")
        self.assertTrue(any("template missing" in n for n in notes))

    def test_read_rules_template_reads_the_repo_template(self):
        text, heading = runner.read_rules_template()
        self.assertIn(runner.RULES_MARKER, text)
        self.assertEqual(heading, RULES_HEADING)

    def test_shipped_template_carries_the_marker_once(self):
        text, _ = runner.read_rules_template()
        self.assertEqual(text.count(runner.RULES_MARKER), 1)
        self.assertEqual(text.splitlines()[0], runner.RULES_MARKER)


class PromptHandoffTests(unittest.TestCase):
    """Every tier launches on the assembled prompt; the caller's file is untouched.

    Borrows the fixture and runner helpers from RunWorkerTests without
    inheriting its test methods (which would run twice).
    """

    tearDown = RunWorkerTests.tearDown
    go = RunWorkerTests.go
    pgrep = RunWorkerTests.pgrep

    def setUp(self):
        RunWorkerTests.setUp(self)
        self.counts = {}
        self.original = self.prompt.read_text(encoding="utf-8")

    def assembled(self, label="ws1-plan-lane-1"):
        return (self.run_dir / f"{label}.md").read_text(encoding="utf-8")

    def check_tier(self, tier, launch):
        code, line = self.go(tier, launch)
        self.assertEqual(code, 0, line)
        text = self.assembled()
        self.assertTrue(text.startswith("[label: ws1-plan-lane-1]\n"))
        self.assertIn(runner.RULES_MARKER, text)
        self.assertTrue(text.endswith("do the task\n"))
        self.assertEqual(self.prompt.read_text(encoding="utf-8"), self.original)
        return launch.cmds[0], text

    def test_muse_tier_reads_the_assembled_prompt(self):
        cmd, _ = self.check_tier("muse", Launch())
        self.assertEqual(cmd[cmd.index("--prompt-file") + 1], str(self.run_dir / "ws1-plan-lane-1.md"))

    def test_grok_build_tier_sends_the_assembled_prompt(self):
        cmd, text = self.check_tier("glm", Launch())
        self.assertEqual(cmd[2], text + "\n" + runner.RUN_RULE)

    def test_pi_tier_passes_the_assembled_prompt_to_its_launcher(self):
        cmd, _ = self.check_tier("pi-deepseek", Launch())
        self.assertIn(str(self.run_dir / "ws1-plan-lane-1.md"), cmd)

    def test_codex_tier_passes_the_assembled_prompt_to_its_launcher(self):
        cmd, _ = self.check_tier("codex-luna", Launch())
        self.assertIn(str(self.run_dir / "ws1-plan-lane-1.md"), cmd)

    def test_cursor_command_uses_the_prompt_it_is_given(self):
        worker = {"kind": "cursor", "model": "m"}
        cmd = runner.command(worker, "ws1-x", "/ws", "/run/ws1-x.md", "-", "/run", "cursor")
        self.assertEqual(cmd[-2:], ["/run/ws1-x.md", "m"])

    def test_no_rules_launches_the_caller_file_as_written(self):
        from io import StringIO
        import contextlib

        launch = Launch()
        argv = ["muse", "ws1-plan-norules", str(self.ws), str(self.prompt),
                str(self.run_dir), "0", "--no-preflight", "--no-rules"]
        with contextlib.redirect_stdout(StringIO()):
            code = runner.run(argv, spawn=launch.spawn, pgrep=self.pgrep, sleep=lambda _s: None,
                              now=lambda: NOW, alive=launch.is_alive, new_uuid=lambda: SID)
        self.assertEqual(code, 0)
        self.assertIn(str(self.prompt), launch.cmds[0])
        self.assertFalse((self.run_dir / "ws1-plan-norules.md").exists())



if __name__ == "__main__":
    unittest.main()
