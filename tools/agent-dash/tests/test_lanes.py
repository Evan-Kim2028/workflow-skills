"""Tests for dash/lanes.py and agent_dash.py lanes live. Fake process tables only."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import agent_dash
from dash import lanes as lanes_lib

STATIC_FORMAT = Path(__file__).resolve().parent.parent / "static" / "format.js"

ELAPSED_SAMPLES = [
    (0, "0s"),
    (90, "1m 30s"),
    (120, "2m 0s"),
    (359.6, "5m 59s"),
    (3599, "59m 59s"),
    (7200, "2h 0m"),
    (172799, "47h 59m"),
    (172800, "2d 0h"),
    (183610, "2d 3h"),
    (579599, "6d 16h"),
    (1209599, "13d 23h"),
    (1209600, "14d"),
    (1227600, "14d"),
    (31535999, "364d"),
    (31536000, "1y"),
    (34560000, "1y 35d"),
    (-5, "0s"),
]

SID_ONE = "11111111-1111-4111-8111-111111111111"
SID_TWO = "22222222-2222-4222-8222-222222222222"


def muse_row(pid, started, label="ws11-dash-lanes", session=SID_ONE, prompt="ws11-dash-lanes", model=None):
    """One muse live-worker row."""
    return {
        "pid": pid,
        "comm": "muse-bin",
        "started_at": float(started),
        "label": label,
        "prompt_label": prompt,
        "session_id": session,
        "model": model,
    }


def cursor_row(pid, started, label="ws11-dash-lanes", model=None):
    """One cursor-agent live-worker row."""
    return {
        "pid": pid,
        "comm": "cursor-agent",
        "started_at": float(started),
        "label": label,
        "prompt_label": None,
        "session_id": None,
        "model": model,
    }


class GroupingTests(unittest.TestCase):
    """Muse children sharing one run collapse to one lane."""

    def test_muse_spawn_storm_is_one_lane(self):
        """Three muse-bin processes on one session read as one lane."""
        rows = [
            muse_row(101, 1000.0),
            muse_row(102, 1001.0),
            muse_row(103, 1002.0),
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(len(lanes), 1)
        lane = lanes[0]
        self.assertEqual(lane["label"], "ws11-dash-lanes")
        self.assertEqual(lane["tier"], "muse")
        self.assertEqual(lane["pid"], 101)
        self.assertEqual(lane["processes"], 3)
        self.assertEqual(lane["pids"], [101, 102, 103])
        self.assertAlmostEqual(lane["elapsed_s"], 1000.0)

    def test_cursor_label_collapses(self):
        """Two cursor-agent processes on one label read as one lane."""
        rows = [
            cursor_row(201, 1500.0),
            cursor_row(202, 1501.0),
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["tier"], "grok")
        self.assertEqual(lanes[0]["processes"], 2)

    def test_unlabeled_rows_are_skipped(self):
        """Rows with no label session or prompt never become lanes."""
        rows = [
            {
                "pid": 301,
                "comm": "muse-bin",
                "started_at": 1000.0,
                "label": None,
                "prompt_label": None,
                "session_id": None,
            }
        ]
        self.assertEqual(lanes_lib.group_live_processes(rows, now=2000.0), [])

    def test_session_distinguishes_relaunch(self):
        """Same label under two session uuids reads as two lanes."""
        rows = [
            muse_row(401, 1000.0, session=SID_ONE),
            muse_row(402, 1100.0, session=SID_TWO),
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(len(lanes), 2)
        by_session = {lane["session_id"]: lane for lane in lanes}
        self.assertIn(SID_ONE, by_session)
        self.assertIn(SID_TWO, by_session)

    def test_prompt_label_names_lane(self):
        """A muse row with only a prompt label still names its lane."""
        rows = [
            {
                "pid": 501,
                "comm": "muse-bin",
                "started_at": 1000.0,
                "label": None,
                "prompt_label": "ws11-dash-lanes",
                "session_id": None,
            }
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["label"], "ws11-dash-lanes")
        self.assertEqual(lanes[0]["prompt_label"], "ws11-dash-lanes")

    def test_wrapper_and_worker_share_label(self):
        """A run_worker wrapper and its muse child are one lane."""
        rows = [
            {
                "pid": 601,
                "comm": "python3",
                "started_at": 999.0,
                "label": "ws11-dash-lanes",
                "prompt_label": None,
                "session_id": None,
            },
            muse_row(602, 1000.0),
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["tier"], "muse")
        self.assertEqual(lanes[0]["processes"], 2)


class TierTests(unittest.TestCase):
    """Tier mapping across worker families."""

    def test_claude_subagent_is_visible(self):
        """A claude comm row with a label reads as a claude lane."""
        rows = [
            {
                "pid": 701,
                "comm": "claude",
                "started_at": 1000.0,
                "label": "ws11-dash-lanes",
                "prompt_label": None,
                "session_id": None,
                "model": None,
            }
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["tier"], "claude")

    def test_claude_model_tier_wins(self):
        """A claude row with a sonnet model reads as sonnet."""
        rows = [
            {
                "pid": 702,
                "comm": "claude",
                "started_at": 1000.0,
                "label": "ws11-dash-lanes",
                "prompt_label": None,
                "session_id": None,
                "model": "claude-sonnet-5-5",
            }
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(lanes[0]["tier"], "sonnet")

    def test_grok_cli_model_splits_deepseek(self):
        """A grok comm row with a deepseek model reads as deepseek."""
        rows = [
            {
                "pid": 801,
                "comm": "grok",
                "started_at": 1000.0,
                "label": "ws11-dash-lanes",
                "prompt_label": None,
                "session_id": None,
                "model": "deepseek-flash",
            }
        ]
        lanes = lanes_lib.group_live_processes(rows, now=2000.0)
        self.assertEqual(lanes[0]["tier"], "deepseek")

    def test_format_elapsed(self):
        """Durations use the compact dashboard shape."""
        self.assertEqual(lanes_lib.format_elapsed(45), "45s")
        self.assertEqual(lanes_lib.format_elapsed(190), "3m 10s")
        self.assertEqual(lanes_lib.format_elapsed(7500), "2h 5m")

    def test_format_elapsed_matches_sample_table(self):
        """The lanes formatter returns the same strings as fmtDur (see
        ELAPSED_SAMPLES, shared with the page-rules JS test)."""
        for seconds, want in ELAPSED_SAMPLES:
            with self.subTest(seconds=seconds):
                self.assertEqual(lanes_lib.format_elapsed(seconds), want)

    def test_format_elapsed_dash_and_negative_handling(self):
        self.assertEqual(lanes_lib.format_elapsed(None), "-")
        self.assertEqual(lanes_lib.format_elapsed("abc"), "-")
        self.assertEqual(lanes_lib.format_elapsed(float("nan")), "-")
        self.assertEqual(lanes_lib.format_elapsed(float("inf")), "-")
        self.assertEqual(lanes_lib.format_elapsed(-5), "0s")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_format_elapsed_cross_checks_fmt_dur_in_node(self):
        """Run fmtDur in node over the same inputs and compare string by
        string with the Python mirror, across a wide spread of seconds."""
        inputs = [v for v, _ in ELAPSED_SAMPLES] + [
            0.4, 59.4, 59.5, 60, 3599.5, 3600, 86399.9, 172799.9, 1209599.5,
            31535999.9, 10 ** 9, 0.25 * 3600 * 48,
        ]
        harness = (
            "const fs = require('fs');\n"
            "var window = {};\n"
            "eval(fs.readFileSync(process.argv[2], 'utf8'));\n"
            "const inputs = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
            "process.stdout.write(JSON.stringify(inputs.map(function (v) { return window.AD.fmtDur(v); })));\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "fmtdur_cross.js"
            inputs_path = Path(tmp) / "inputs.json"
            harness_path.write_text(harness, encoding="utf-8")
            inputs_path.write_text(json.dumps(inputs), encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path), str(STATIC_FORMAT), str(inputs_path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        js_values = json.loads(proc.stdout)
        for seconds, js in zip(inputs, js_values):
            with self.subTest(seconds=seconds):
                self.assertEqual(lanes_lib.format_elapsed(seconds), js)


class CliTests(unittest.TestCase):
    """agent_dash.py lanes live uses the dashboard listing."""

    def test_live_counts_lanes_not_processes(self):
        """Nineteen processes on ten runs print ten lanes."""
        rows = []
        for index in range(10):
            label = "ws11-lane-%d" % index
            rows.append(
                {
                    "pid": 1000 + index * 2,
                    "comm": "muse-bin",
                    "started_at": 1000.0 + index,
                    "label": label,
                    "prompt_label": label,
                    "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-%012d" % index,
                }
            )
            rows.append(
                {
                    "pid": 2000 + index * 2,
                    "comm": "muse-bin",
                    "started_at": 1001.0 + index,
                    "label": label,
                    "prompt_label": label,
                    "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-%012d" % index,
                }
            )
        rows = rows[:19]
        with mock.patch(
            "dash.sources.process.live_worker_processes", return_value=rows
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = agent_dash.cmd_lanes(["live"])
        self.assertEqual(code, 0)
        self.assertIn("live lanes: 10 (from 19 processes)", buf.getvalue())

    def test_live_json_shape(self):
        """JSON mode emits count processes and lanes."""
        rows = [muse_row(901, 1000.0), muse_row(902, 1001.0)]
        with mock.patch(
            "dash.sources.process.live_worker_processes", return_value=rows
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = agent_dash.cmd_lanes(["live", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["processes"], 2)
        self.assertEqual(len(payload["lanes"]), 1)
        lane = payload["lanes"][0]
        self.assertEqual(lane["label"], "ws11-dash-lanes")
        self.assertEqual(lane["tier"], "muse")
        self.assertEqual(lane["pid"], 901)

    def test_live_rejects_unknown_subcommand(self):
        """Only lanes live is accepted."""
        self.assertEqual(agent_dash.cmd_lanes([]), 2)
        self.assertEqual(agent_dash.cmd_lanes(["running"]), 2)
        self.assertEqual(agent_dash.cmd_lanes(["live", "--bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
