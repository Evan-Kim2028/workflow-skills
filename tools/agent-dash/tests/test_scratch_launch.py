"""Every worker tier launches with AGENT_SCRATCH, created before the spawn. Fake processes only."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NOW = 1_700_000_000.0
SID = "22222222-2222-4222-8222-222222222222"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_worker_scratch", ROOT / "scripts" / "run_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = load_runner()


class FakeSpawn:
    """Records argv and env; writes a PASS report so the launch finishes at once."""

    def __init__(self):
        self.envs = []
        self.scratch_seen = []

    def __call__(self, cmd, out_path, err_path, env=None):
        env = dict(env or {})
        self.envs.append(env)
        if "AGENT_SCRATCH" in env:
            self.scratch_seen.append(Path(env["AGENT_SCRATCH"]).is_dir())
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("PASS\nbuilt it, with a long enough report body to count as a report.\n" * 3, encoding="utf-8")
        Path(err_path).write_text("", encoding="utf-8")
        return 4242


class ScratchLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("do the task\n", encoding="utf-8")
        self.run_dir = self.root / "hub" / "runs"
        self.scratch = self.root / "scratch"
        self.reg = self.root / "workers.json"
        data = json.loads((ROOT / "workers.example.json").read_text(encoding="utf-8"))
        self.registry = data
        self.reg.write_text(json.dumps(data), encoding="utf-8")
        keys = ("AGENT_DASH_WORKERS", "AGENT_DASH_STATE_DIR", "AGENT_DASH_SCRATCH_ROOT", "AGENT_DASH_ALLOW_LEGACY_LABELS")
        self.saved = {k: os.environ.get(k) for k in keys}
        os.environ["AGENT_DASH_WORKERS"] = str(self.reg)
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.root / "state")
        os.environ["AGENT_DASH_SCRATCH_ROOT"] = str(self.scratch)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def launch(self, tier, label, spawn):
        argv = [tier, label, str(self.ws), str(self.prompt), str(self.run_dir), "0", "--no-preflight"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = runner.run(argv, spawn=spawn, pgrep=lambda _p: 0, sleep=lambda _s: None,
                              now=lambda: NOW, alive=lambda _p: False, new_uuid=lambda: SID)
        return code, buf.getvalue()

    def tiers(self):
        out = []
        for name, worker in self.registry["workers"].items():
            if worker.get("kind") in runner.KINDS and worker.get("enabled", True):
                out.append(name)
        return sorted(out)

    def test_every_tier_gets_agent_scratch_before_spawn(self):
        names = self.tiers()
        self.assertGreaterEqual(len(names), 5, names)
        for i, tier in enumerate(names):
            label = f"ws1-plan-tier-{i}"
            spawn = FakeSpawn()
            code, line = self.launch(tier, label, spawn)
            self.assertEqual(code, 0, (tier, line))
            self.assertEqual(len(spawn.envs), 1, tier)
            expected = self.scratch / "WS1" / label
            self.assertEqual(spawn.envs[0].get("AGENT_SCRATCH"), str(expected), tier)
            self.assertEqual(spawn.scratch_seen, [True], f"{tier}: folder must exist at spawn")
            owner = json.loads((expected / ".agent-scratch.json").read_text(encoding="utf-8"))
            self.assertEqual(owner["label"], label)
            self.assertEqual(owner["run_dir"], str(self.run_dir))

    def test_scratch_is_outside_the_run_folder(self):
        spawn = FakeSpawn()
        self.launch("muse", "ws1-plan-outside", spawn)
        scratch = Path(spawn.envs[0]["AGENT_SCRATCH"])
        self.assertFalse(str(scratch).startswith(str(self.run_dir)))
        self.assertFalse((self.run_dir / "ws1-plan-outside" / ".agent-scratch.json").exists())

    def test_pi_standby_keeps_its_credential_var_and_scratch(self):
        data = dict(self.registry)
        data["workers"] = json.loads(json.dumps(self.registry["workers"]))
        data["workers"]["pi-deepseek"]["standby_env_file"] = "~/.config/pi/standby.env"
        self.reg.write_text(json.dumps(data), encoding="utf-8")
        spawn = FakeSpawn()
        code, line = self.launch("pi-deepseek", "ws1-plan-standby", spawn)
        self.assertEqual(code, 0)
        env = spawn.envs[0]
        self.assertEqual(env["AGENT_DASH_PI_ENV_FILE"], os.path.expanduser("~/.config/pi/standby.env"))
        self.assertEqual(env["AGENT_SCRATCH"], str(self.scratch / "WS1" / "ws1-plan-standby"))

    def test_launch_env_helper(self):
        self.assertEqual(runner.launch_env({}, None), None)
        self.assertEqual(runner.launch_env({}, "/s"), {"AGENT_SCRATCH": "/s"})

    def test_unwritable_scratch_root_still_exports_the_path(self):
        blocker = self.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        os.environ["AGENT_DASH_SCRATCH_ROOT"] = str(blocker)
        spawn = FakeSpawn()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _line = self.launch("muse", "ws1-plan-blocked", spawn)
        self.assertEqual(code, 0)
        self.assertEqual(spawn.envs[0]["AGENT_SCRATCH"], str(blocker / "WS1" / "ws1-plan-blocked"))
        self.assertIn("scratch warning", err.getvalue())


if __name__ == "__main__":
    unittest.main()
