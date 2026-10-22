"""Tests for dash.sources.generic and the report helper. No live sessions."""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash.sources import generic  # noqa: E402

NOW = 1_792_000_000.0


class MemStore:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.usage = []

    def get_offset(self, path):
        return self.offsets.get(path)

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset)

    def upsert_run(self, row):
        self.runs[row["run_id"]] = dict(row)

    def add_usage(self, row):
        self.usage.append(dict(row))


def _write_state(lines: list[str]) -> tuple[str, dict]:
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "generic.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    return tmp, {"state_dir": tmp.name}


class GenericUnitTests(unittest.TestCase):
    def test_run_id_and_tier(self):
        self.assertEqual(generic.default_run_id("ws4-a", "s1"), "generic:s1:ws4-a")
        self.assertEqual(generic.default_run_id("ws4-a"), "generic:ws4-a")
        self.assertEqual(generic.default_run_id("ws4-a", "s1", "custom"), "custom")
        self.assertEqual(generic.tier_of("muse-spark-1.3-contributor"), "muse")
        self.assertEqual(generic.tier_of("glm-5.3"), "glm")
        self.assertEqual(generic.tier_of("grok-4.7-high"), "grok")
        self.assertEqual(generic.tier_of("claude-opus-5-5"), "opus")
        self.assertEqual(generic.tier_of("reflection/Beam-1.0-501B-A23B:xhigh"), "beam")
        self.assertEqual(generic.tier_of("reflection-beam"), "beam")
        self.assertEqual(generic.tier_of("some-other-model"), "generic")
        self.assertEqual(generic.tier_of(None), "generic")

    def test_run_line(self):
        tmp, cfg = _write_state(
            [json.dumps({"type": "run", "label": "ws4-WS4.1-demo", "model": "demo-model"})]
        )
        try:
            store = MemStore()
            generic.poll(store, cfg, NOW)
            run = store.runs.get("generic:ws4-WS4.1-demo")
            self.assertIsNotNone(run)
            self.assertEqual(run["source"], "generic")
            self.assertEqual(run["label"], "ws4-WS4.1-demo")
            self.assertEqual(run["status"], "running")
            self.assertEqual(run["model"], "demo-model")
            self.assertIsNotNone(run.get("started_at"))
            # Second poll reads nothing new.
            before = len(store.runs)
            generic.poll(store, cfg, NOW + 1)
            self.assertEqual(len(store.runs), before)
        finally:
            tmp.cleanup()

    def test_bad_lines_skipped(self):
        tmp, cfg = _write_state(
            [
                "not json",
                json.dumps({"type": "run"}),
                json.dumps({"type": "run", "label": ""}),
                json.dumps({"type": "usage", "tool": "generic"}),
                json.dumps({"type": "nonsense", "label": "ws4-x"}),
            ]
        )
        try:
            store = MemStore()
            generic.poll(store, cfg, NOW)
            self.assertEqual(store.runs, {})
            self.assertEqual(store.usage, [])
        finally:
            tmp.cleanup()

    def test_usage_line(self):
        tmp, cfg = _write_state(
            [
                json.dumps(
                    {
                        "type": "usage",
                        "session_id": "s1",
                        "run_id": "generic:s1:ws4-WS4.1-demo",
                        "tool": "generic",
                        "message_id": "turn-1",
                        "model": "demo-model",
                        "input": 100,
                        "output": 50,
                    }
                )
            ]
        )
        try:
            store = MemStore()
            generic.poll(store, cfg, NOW)
            self.assertEqual(len(store.usage), 1)
            row = store.usage[0]
            self.assertEqual(row["run_id"], "generic:s1:ws4-WS4.1-demo")
            self.assertEqual(row["input"], 100)
            self.assertEqual(row["output"], 50)
        finally:
            tmp.cleanup()

    def test_finish_keeps_model(self):
        row = generic._run_row(
            {"type": "run", "label": "ws4-WS4.1-demo", "status": "done", "verdict": "PASS"},
            NOW,
        )
        self.assertNotIn("model", row)
        self.assertNotIn("started_at", row)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["verdict"], "PASS")
        self.assertIsNotNone(row.get("ended_at"))

    def test_end_to_end_with_real_store(self):
        from dash.store import Store

        tmp = tempfile.TemporaryDirectory()
        try:
            state = os.path.join(tmp.name, "state")
            os.makedirs(state)
            dest = os.path.join(state, "generic.jsonl")
            run_id = "generic:s9:ws4-WS4.1-demo"
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"type": "run", "label": "ws4-WS4.1-demo", "run_id": run_id,
                         "model": "demo-model", "status": "running", "started_at": NOW}
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {"type": "usage", "session_id": "s9", "run_id": run_id,
                         "tool": "generic", "message_id": "t1", "model": "demo-model",
                         "input": 100, "output": 50, "ts": NOW}
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {"type": "run", "label": "ws4-WS4.1-demo", "run_id": run_id,
                         "status": "done", "verdict": "PASS", "ended_at": NOW + 10}
                    )
                    + "\n"
                )
            cfg = {"state_dir": state}
            with Store(os.path.join(state, "dash.sqlite")) as store:
                generic.poll(store, cfg, NOW + 20)
                runs = [dict(row) for row in store._conn.execute("SELECT * FROM runs")]
                self.assertEqual(len(runs), 1)
                run = runs[0]
                self.assertEqual(run["status"], "done")
                self.assertEqual(run["verdict"], "PASS")
                self.assertEqual(run["model"], "demo-model")
                self.assertEqual(run["workstream"], "WS4")
                usage = list(store._conn.execute("SELECT * FROM usage_events"))
                self.assertEqual(len(usage), 1)
        finally:
            tmp.cleanup()


class ReportCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = os.environ.get("AGENT_DASH_STATE_DIR")
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_STATE_DIR"] = self.tmp.name
        os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
        if "AGENT_DASH_WORKSTREAMS" in os.environ:
            del os.environ["AGENT_DASH_WORKSTREAMS"]

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("AGENT_DASH_STATE_DIR", None)
        else:
            os.environ["AGENT_DASH_STATE_DIR"] = self.old_state
        if self.old_ws is not None:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def test_report_start_usage_finish(self):
        import agent_dash

        label = "ws4-WS4.1-demo"
        self.assertEqual(
            agent_dash.cmd_report(["start", "--label", label, "--model", "demo-model"]), 0
        )
        self.assertEqual(
            agent_dash.cmd_report(
                ["usage", "--label", label, "--model", "demo-model",
                 "--input", "100", "--output", "50"]
            ),
            0,
        )
        self.assertEqual(agent_dash.cmd_report(["finish", "--label", label, "--verdict", "PASS"]), 0)
        dest = Path(self.tmp.name) / "generic.jsonl"
        lines = [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["type"] for line in lines], ["run", "usage", "run"])
        run_ids = {line["run_id"] for line in lines}
        self.assertEqual(len(run_ids), 1)
        # The file the helper wrote must poll cleanly.
        store = MemStore()
        generic.poll(store, {"state_dir": self.tmp.name}, NOW)
        run = next(iter(store.runs.values()))
        self.assertEqual(run["verdict"], "PASS")
        self.assertEqual(len(store.usage), 1)

    def test_report_errors(self):
        import agent_dash

        self.assertEqual(agent_dash.cmd_report([]), 2)
        self.assertEqual(agent_dash.cmd_report(["start"]), 2)
        self.assertEqual(agent_dash.cmd_report(["finish", "--label", "ws4-x"]), 2)
        self.assertEqual(
            agent_dash.cmd_report(["usage", "--label", "ws4-x", "--input", "1"]), 2
        )

    def test_report_finish_rejects_unknown_verdict(self):
        import agent_dash

        self.assertEqual(
            agent_dash.cmd_report(["finish", "--label", "ws4-x", "--verdict", "PAS"]), 2
        )
        dest = Path(self.tmp.name) / "generic.jsonl"
        self.assertFalse(dest.exists())

    def test_report_run_dir_registers_folder(self):
        import agent_dash

        rundir = Path(self.tmp.name) / "runs"
        rundir.mkdir()
        self.assertEqual(agent_dash.cmd_report(["--run-dir", str(rundir)]), 0)
        registered = (Path(self.tmp.name) / "run-dirs.txt").read_text(encoding="utf-8")
        self.assertIn(str(rundir), registered)
        self.assertEqual(agent_dash.cmd_report(["--run-dir"]), 2)


if __name__ == "__main__":
    unittest.main()
