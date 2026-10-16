import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.models import model_key, run_model_key
from dash.store import _drop_launch_echoes


class ModelKeyTest(unittest.TestCase):
    def test_families_and_models(self):
        cases = {
            "composer-2.5": ("cursor", "composer-2.5"),
            "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]": ("cursor", "grok-4.7"),
            "grok-4.7-high": ("cursor", "grok-4.7"),
            "glm-5.3": ("glm", "glm-5.3"),
            "glm-5.3-flash": ("glm", "glm-5.3-flash"),
            "muse-spark-1.3-contributor": ("muse", "muse-spark-1.3"),
            "muse-wonder-full-1.0": ("muse", "muse-wonder-1.0"),
            "claude-opus-5-5": ("claude", "opus"),
            "claude-haiku-5-5": ("claude", "haiku"),
            "<synthetic>": ("other", "other"),
        }
        for model, want in cases.items():
            self.assertEqual(model_key(model), want, model)

    def test_unrecorded_model_uses_the_tier(self):
        self.assertEqual(run_model_key({"model": None, "tier": "grok", "source": "grok"}), "cursor-unknown")
        self.assertEqual(run_model_key({"model": "", "tier": "muse"}), "muse-unknown")
        self.assertEqual(run_model_key({"model": None, "tier": "haiku"}), "haiku")


class LaunchEchoTest(unittest.TestCase):
    def test_manifest_echo_with_a_twin_is_dropped(self):
        real = {"run_id": "grok:chat:g-cut", "label": "g-cut", "source": "grok", "status": "done", "started_at": 1000.0, "ended_at": 1200.0}
        echo = {"run_id": "grok:sess:cursor/g-cut", "label": "g-cut", "source": "grok", "status": "unknown", "started_at": 1005.0, "ended_at": None, "clock_source": "manifest", "out_bytes": 0}
        alone = dict(echo, run_id="grok:sess:cursor/solo", label="solo")
        late = dict(echo, run_id="grok:sess:cursor/g-cut-later", started_at=5000.0)
        real["clock_source"] = "transcript"
        done_echo = dict(echo, run_id="grok:sess:cursor/g-cut-2", status="done", ended_at=1200.0, out_bytes=900, verdict="PASS")
        kept = [row["run_id"] for row in _drop_launch_echoes([real, echo, done_echo, alone, late])]
        self.assertEqual(kept, [real["run_id"], alone["run_id"], late["run_id"]])
        self.assertEqual(real["verdict"], "PASS")

    def test_wrapper_with_the_same_label_is_not_a_twin(self):
        wrapper = {"run_id": "claude-workflow:s:wf/a", "label": "ws4-WS4.3-deal-sql", "source": "claude-workflow", "status": "running", "started_at": 1000.0, "ended_at": None, "clock_source": "transcript"}
        worker = {"run_id": "muse:u:ws4-WS4.3-deal-sql", "label": "ws4-WS4.3-deal-sql", "source": "muse", "status": "running", "started_at": 1010.0, "ended_at": None, "clock_source": "manifest", "pid": 9}
        kept = [row["run_id"] for row in _drop_launch_echoes([wrapper, worker])]
        self.assertEqual(kept, [wrapper["run_id"], worker["run_id"]])


class OneRunPerProcessTest(unittest.TestCase):
    def test_same_label_and_pid_keeps_the_real_row(self):
        ghost = {"run_id": "grok:a:job", "label": "job", "status": "running", "pid": 7, "model": None, "out_bytes": None, "clock_source": "transcript", "source": "grok", "started_at": 1.0}
        real = dict(ghost, run_id="grok:b:job", model="grok-4.7-xhigh", out_bytes=900)
        other = dict(ghost, run_id="grok:c:other", label="other", pid=8)
        kept = [row["run_id"] for row in _drop_launch_echoes([ghost, real, other])]
        self.assertEqual(kept, ["grok:b:job", "grok:c:other"])


if __name__ == "__main__":
    unittest.main()
