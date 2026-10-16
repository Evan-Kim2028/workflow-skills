import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.attribution import attribute, clean_label

HUB = "00000000-0000-0000-0000-000000000001"
PLAN = "00000000-0000-0000-0000-000000000002"


class AttributionTest(unittest.TestCase):
    def test_regex_rules(self):
        self.assertEqual(attribute("ws7-landing"), ("WS7", "regex"))
        self.assertEqual(attribute("ws2-3"), ("WS2", "regex"))
        self.assertEqual(attribute("ws1-12"), ("WS1", "regex"))
        self.assertEqual(attribute("WS1.4"), ("WS1", "regex"))
        self.assertEqual(attribute("dash-preview"), ("TOOLS", "regex"))
        self.assertEqual(attribute("g-review"), ("WS1", "regex"))

    def test_label_beats_session(self):
        self.assertEqual(attribute("ws7-landing", HUB), ("WS7", "regex"))

    def test_session_defaults(self):
        self.assertEqual(attribute(None, HUB), ("WS1", "session-default"))
        self.assertEqual(attribute("", PLAN), ("PLANNING", "session-default"))
        self.assertEqual(attribute("hello", HUB), ("WS1", "session-default"))

    def test_child_does_not_inherit_session(self):
        self.assertEqual(attribute("hello", HUB, parent_session_id="parent"), (None, None))
        self.assertEqual(attribute(None, HUB, parent_session_id="parent"), (None, None))
        self.assertEqual(attribute("ws2-3", HUB, parent_session_id="parent"), ("WS2", "regex"))

    def test_blank(self):
        self.assertEqual(attribute("nope-label"), (None, None))
        self.assertEqual(attribute(None, "not-a-session"), (None, None))

    def test_exact_beats_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(
                json.dumps({"exact": {"ws7-landing": "WS9"}, "regex": [["^ws(\\d+)", "WS{1}"]], "sessions": {}}),
                encoding="utf-8",
            )
            self.assertEqual(attribute("ws7-landing", path=path), ("WS9", "label"))
            self.assertEqual(attribute("ws2-3", path=path), ("WS2", "regex"))

    def test_reloads_on_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(json.dumps({"exact": {}, "regex": [], "sessions": {}}), encoding="utf-8")
            self.assertEqual(attribute("zzz", path=path), (None, None))
            path.write_text(
                json.dumps({"exact": {"zzz": "WS3"}, "regex": [], "sessions": {}}),
                encoding="utf-8",
            )
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertEqual(attribute("zzz", path=path), ("WS3", "label"))

    def test_clean_label_drops_prompt_sized_text(self):
        self.assertIsNone(clean_label("x" * 500))
        self.assertIsNone(clean_label("has\nnewline"))
        self.assertEqual(clean_label("  ws7-landing  "), "ws7-landing")


if __name__ == "__main__":
    unittest.main()
