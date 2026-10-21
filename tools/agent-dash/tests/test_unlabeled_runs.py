"""Doctor backstop: weakly labeled runs surface per workstream.

Hermetic: temp store, empty temp workstreams dir (no plan can rescue
a weak label), catalog rules only. No server, no live state.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_dash import default_cfg  # noqa: E402
from dash.doctor import LABEL_GUARD_LANDED_AT, collect_problems  # noqa: E402
from dash.store import Store  # noqa: E402

NOW = LABEL_GUARD_LANDED_AT + 10_000.0
OLD = LABEL_GUARD_LANDED_AT - 100_000.0
FRESH = LABEL_GUARD_LANDED_AT + 100.0


class UnlabeledRunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.store = Store(self.state / "dash.sqlite")
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.ws)
        cfg = default_cfg()
        cfg.update({"state_dir": str(self.state), "data_root": str(self.state)})
        cfg["roots"]["scratchpads"] = []
        self.cfg = cfg

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _run(self, run_id, label, started, ws_hint="s", source="muse", slice_of=None):
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": source,
                "tier": source,
                "label": label,
                "session_id": ws_hint,
                "status": "done",
                "started_at": started,
                "slice_of": slice_of,
            }
        )

    def _usage(self, run_id, tokens):
        self.store.add_usage(
            {
                "ts": NOW,
                "session_id": "s",
                "run_id": run_id,
                "model": "m",
                "tool": "muse",
                "message_id": f"m-{run_id}",
                "input": tokens,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )

    def _collect(self):
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(
                self.store, self.cfg, NOW, health={"ok": True, "sources": []}, processes=[]
            )

    def _problems(self, result):
        return [item for item in result["problems"] if item["code"] == "unlabeled-runs"]

    def test_legacy_weak_run_is_warn_with_fix(self):
        self._run("muse:s1:ws9-junk-old", "ws9-junk-old", OLD)
        self._usage("muse:s1:ws9-junk-old", 1_500_000)
        found = self._problems(self._collect())
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["severity"], "warn")
        self.assertEqual(item["ws"], "WS9")
        self.assertIn("1.5M", item["title"])
        self.assertIn("use ws9-plan-junk-old", item["detail"])
        self.assertIn("label check ws9-junk-old --ws WS9", item["fix_command"])

    def test_fresh_weak_run_is_critical(self):
        self._run("muse:s1:ws9-junk-new", "ws9-junk-new", FRESH)
        found = self._problems(self._collect())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "critical")
        self.assertIn("use ws9-plan-junk-new", found[0]["detail"])

    def test_fresh_weak_glm_and_grok_runs_are_critical(self):
        self._run("glm:s1:ws9-junk-glm", "ws9-junk-glm", FRESH, source="glm")
        self._run("grok:s2:ws9-junk-grok", "ws9-junk-grok", FRESH, source="grok")
        found = self._problems(self._collect())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "critical")
        self.assertIn("ws9-junk-glm", found[0]["detail"])
        self.assertIn("ws9-junk-grok", found[0]["detail"])

    def test_unguarded_sources_never_flag(self):
        self._run("claude-main:s1:my-project-0e", "my-project-0e", FRESH, source="claude-main")
        self._run("claude-subagent:s2:ws9-junk-sub", "ws9-junk-sub", FRESH, source="claude-subagent")
        self._run("codex:s3:freeform", "freeform", FRESH, source="codex")
        self._run("codex-exec:s4:freeform-exec", "freeform-exec", FRESH, source="codex-exec")
        self._run("shell:s5:freeform-shell", "freeform-shell", FRESH, source="shell")
        self._run(
            "claude-main:s6:ws11-lead-a1e74059@WS11", "ws11-lead-a1e74059", FRESH,
            source="claude-main",
        )
        self._run(
            "muse:s7:ws11-lead-a1e74059@WS11", "ws11-lead-a1e74059", FRESH,
            slice_of="muse:s6:ws11-exec-primary",
        )
        self._run("muse:s8:documents-a4", "documents-a4", FRESH)
        self._run("muse:s9:my-project-36", "my-project-36", OLD)
        result = self._collect()
        self.assertEqual(self._problems(result), [])
        check = next(item for item in result["checks"] if item["name"] == "labels")
        self.assertTrue(check["ok"])

    def test_slice_of_excludes_guarded_source_run(self):
        self._run("muse:s1:ws9-junk-slice", "ws9-junk-slice", FRESH, slice_of="muse:s0:ws9-plan-x")
        self.assertEqual(self._problems(self._collect()), [])

    def test_strong_labels_stay_quiet(self):
        self._run("muse:s1:ws9-plan-foo", "ws9-plan-foo", OLD)
        self._run("muse:s2:ws9-f-switch-fix3", "ws9-f-switch-fix3", OLD)
        result = self._collect()
        self.assertEqual(self._problems(result), [])
        check = next(item for item in result["checks"] if item["name"] == "labels")
        self.assertTrue(check["ok"])

    def test_unassigned_groups_global(self):
        self._run("muse:s1:freeform", "freeform", OLD)
        found = self._problems(self._collect())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["scope"], "global")
        self.assertIsNone(found[0]["ws"])


if __name__ == "__main__":
    unittest.main()
