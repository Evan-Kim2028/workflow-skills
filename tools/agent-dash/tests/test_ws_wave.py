"""Parse check for the ws-wave workflow script."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "workflows" / "ws-wave.js"


class WsWaveTest(unittest.TestCase):
    def test_script_parses(self):
        # The workflow host wraps this file, which uses a top-level return.
        text = SCRIPT.read_text(encoding="utf-8").replace("export const meta", "const meta")
        wrapped = "async function __wsWaveCheck() {\n" + text + "\n}\n"
        proc = subprocess.run(["node", "--check", "-"], input=wrapped, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_review_focus_and_one_way_are_in_the_script(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "You MUST check and report each of these, each with evidence (command plus output, or file:line):",
            text,
        )
        self.assertIn("A check without evidence counts as failed.", text)
        self.assertIn("is oneWay and needs reviewFocus", text)
        self.assertIn("lane.review === false && !lane.oneWay", text)
        self.assertIn("checks", text)

    def test_doctor_gate_runs_before_the_wave(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("doctor --json --severity critical", text)
        self.assertIn("skipDoctor", text)
        self.assertIn("DOCTOR_GATE", text)
        self.assertIn("doctor-gate", text)
        self.assertIn("stopping the wave", text)
