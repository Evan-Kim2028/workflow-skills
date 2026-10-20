"""Reused labels fail loudly; task-id collisions warn. Temp state only."""

import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SID = "11111111-1111-4111-8111-111111111111"
NOW = 1_700_000_000.0


def load_run_worker():
    """Import the launcher fresh so helper changes are always covered."""
    path = ROOT / "scripts" / "run_worker.py"
    spec = importlib.util.spec_from_file_location("run_worker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = load_run_worker()

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_dash


def registry():
    """Example registry contents for hermetic launches."""
    return json.loads((ROOT / "workers.example.json").read_text(encoding="utf-8"))


class Launch:
    """Fake spawner: every spawn writes a passing report immediately."""

    def __init__(self, alive=False):
        self.cmds = []
        self.alive = alive
        self.n = 0

    def spawn(self, cmd, out_path, err_path):
        self.n += 1
        self.cmds.append(list(cmd))
        out_path = Path(out_path)
        err_path = Path(err_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.write_text("", encoding="utf-8")
        out_path.write_text("PASS\nbuilt it\n", encoding="utf-8")
        return 4000 + self.n

    def is_alive(self, pid):
        return self.alive


class ReuseTests(unittest.TestCase):
    """A finished label refuses with the prior run named and a round."""

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

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def pgrep(self, pattern):
        return self.counts.get(pattern, 0)

    def go(self, tier, launch, label, max_wait="0"):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = runner.run(
                [tier, label, str(self.ws), str(self.prompt), str(self.run_dir), max_wait,
                 "--no-preflight"],
                spawn=launch.spawn,
                pgrep=self.pgrep,
                sleep=lambda _s: None,
                now=lambda: NOW,
                alive=launch.is_alive,
                new_uuid=lambda: SID,
            )
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_finished_label_refuses_and_names_prior_run(self):
        launch = Launch(alive=False)
        code, line, _err = self.go("default", launch, "lane-1")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", line)
        self.assertEqual(len(launch.cmds), 1)
        code, line, err = self.go("default", launch, "lane-1")
        self.assertEqual(code, 2)
        self.assertIn("STATUS: failed", line)
        self.assertIn("reason=label-reuse", line)
        self.assertIn("already has a finished run", err)
        self.assertIn("tier=muse", err)
        self.assertIn("out-lane-1.md", err)
        self.assertIn("lane-1-r1", err)
        self.assertEqual(len(launch.cmds), 1)

    def test_live_label_still_waits(self):
        launch = Launch(alive=True)
        code, line, _err = self.go("default", launch, "live-1", max_wait="0")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: running", line)
        code, line, _err = self.go("default", launch, "live-1", max_wait="0")
        self.assertEqual(code, 0)
        self.assertIn("STATUS: running", line)
        self.assertEqual(len(launch.cmds), 1)

    def test_suggestion_skips_taken_rounds(self):
        launch = Launch(alive=False)
        code, _line, _err = self.go("default", launch, "lane-1")
        self.assertEqual(code, 0)
        (self.run_dir / "lane-1-r1.tier").write_text("muse\n", encoding="utf-8")
        code, _line, err = self.go("default", launch, "lane-1")
        self.assertEqual(code, 2)
        self.assertIn("lane-1-r2", err)
        self.assertEqual(len(launch.cmds), 1)

    def test_round_label_suggests_next_round(self):
        launch = Launch(alive=False)
        code, _line, _err = self.go("default", launch, "lane-1-r1")
        self.assertEqual(code, 0)
        code, _line, err = self.go("default", launch, "lane-1-r1")
        self.assertEqual(code, 2)
        self.assertIn("lane-1-r2", err)
        self.assertEqual(len(launch.cmds), 1)


def write_plan(workstreams, ws, task_ids):
    """Write a minimal plan.json with the given task ids."""
    bundle = workstreams / ws
    bundle.mkdir(parents=True, exist_ok=True)
    groups = [
        {
            "id": "g1",
            "tasks": [
                {"id": ident, "title": ident, "status": "todo"} for ident in task_ids
            ],
        }
    ]
    (bundle / "plan.json").write_text(
        json.dumps({"dag": {"groups": groups}}), encoding="utf-8"
    )


class LabelCheckTests(unittest.TestCase):
    """Strict verdicts plus the plan-task collision warning. Temp state only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workstreams = self.root / "workstreams"
        self.workstreams.mkdir()
        self.env = {
            "AGENT_DASH_WORKSTREAMS": os.environ.get("AGENT_DASH_WORKSTREAMS"),
        }
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.workstreams)

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def check(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_dash.cmd_label(list(args))
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_clean_label_has_no_warning(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        code, out, err = self.check("check", "ws11-plan-scout-10")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok: ws11-plan-scout-10 -> planning via convention")
        self.assertEqual(err, "")

    def test_label_flag_form_matches_positional(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        code, out, _err = self.check("check", "--label", "ws11-exec-foo")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok: ws11-exec-foo -> execution via convention")

    def test_flag_order_does_not_steal_the_label(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        code, out, _err = self.check("check", "--ws", "WS11", "ws11-exec-foo")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok: ws11-exec-foo -> execution via convention")

    def test_make_prints_canonical_label(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_dash.cmd_label(
                ["make", "--scope", "ws11", "--mark", "exec", "--slug", "foo"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "ws11-exec-foo")
        self.assertEqual(err.getvalue().strip(), "")

    def test_task_lane_warns_the_collision(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        code, out, _err = self.check("check", "ws11-pgload-hook", "--ws", "WS11")
        self.assertEqual(code, 0)
        self.assertIn("ok: ws11-pgload-hook -> execution via task", out)
        self.assertIn("warning", out)
        self.assertIn("WS11.pgload", out)

    def test_equal_task_id_warns(self):
        write_plan(self.workstreams, "WS11", ["ws11-pgload-hook"])
        code, out, _err = self.check("check", "ws11-pgload-hook")
        self.assertEqual(code, 0)
        self.assertIn("warning", out)
        self.assertIn("ws11-pgload-hook", out)

    def test_other_workstream_stays_quiet(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        write_plan(self.workstreams, "WS1", ["1"])
        code, out, _err = self.check("check", "ws11-exec-foo")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok: ws11-exec-foo -> execution via convention")

    def test_scoped_check_stays_quiet(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        code, out, _err = self.check("check", "ws11-plan-scout-10", "--ws", "WS11")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", out)

    def test_mark_conflict_warns_once(self):
        write_plan(self.workstreams, "WS11", ["WS11.pgload"])
        code, out, _err = self.check("check", "ws11-plan-pgload-hook", "--ws", "WS11")
        self.assertEqual(code, 0)
        self.assertIn("planning via convention", out)
        self.assertEqual(out.count("warning"), 1)

    def test_malformed_label_is_refused(self):
        code, _out, err = self.check("check", "bad/label")
        self.assertEqual(code, 2)
        self.assertIn("label refused", err)


if __name__ == "__main__":
    unittest.main()
