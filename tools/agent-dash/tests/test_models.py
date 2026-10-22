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
            "glm-5.3": ("grok", "glm-5.3"),
            "glm-5.3-flash": ("grok", "glm-5.3-flash"),
            "muse-spark-1.3-contributor": ("muse", "muse-spark-1.3"),
            "muse-wonder-full-1.0": ("muse", "muse-wonder-1.0"),
            "claude-opus-5-5": ("claude", "opus"),
            "claude-haiku-5-5": ("claude", "haiku"),
            "<synthetic>": ("other", "other"),
            "dagster:catalog_load_job": ("dagster", "dagster:catalog_load_job"),
        }
        for model, want in cases.items():
            self.assertEqual(model_key(model), want, model)

    def test_unrecorded_model_uses_the_tier(self):
        self.assertEqual(
            model_key("identity_tcgjp_refresh_job", "LIGHT dagster"),
            ("dagster", "dagster:identity_tcgjp_refresh_job"),
        )
        self.assertEqual(run_model_key({"model": None, "tier": "grok", "source": "grok"}), "cursor-unknown")
        self.assertEqual(run_model_key({"model": "", "tier": "muse"}), "muse-unknown")
        self.assertEqual(run_model_key({"model": None, "tier": "haiku"}), "haiku")

    def test_grok_build_groups_as_grok(self):
        # Every Grok Build id keeps its model key under the grok family,
        # with or without a run hint; the launcher hint also covers ids
        # the catalog has never seen.
        for model in (
            "glm-5.3",
            "glm-5.3-flash",
            "deepseek-flash",
            "reflection-beam",
            "future-1",
        ):
            self.assertEqual(model_key(model, "glm glm"), ("grok", model), model)
            self.assertEqual(
                run_model_key({"model": model, "tier": "grok", "source": "glm"}),
                model,
                model,
            )
        for model in ("glm-5.3", "glm-5.3-flash", "deepseek-flash", "reflection-beam"):
            self.assertEqual(model_key(model)[0], "grok", model)
        self.assertEqual(model_key(None, "glm glm"), ("grok", "grok-unknown"))
        self.assertEqual(model_key("", "grok glm"), ("grok", "grok-unknown"))

    def test_commandcode_still_wins_over_grok_build(self):
        self.assertEqual(
            model_key("reflection/beam:free"), ("commandcode", "reflection/beam:free")
        )
        self.assertEqual(
            model_key("reflection-beam", "commandcode commandcode"),
            ("commandcode", "reflection-beam"),
        )
        self.assertEqual(
            run_model_key(
                {"model": "reflection-beam", "tier": "grok", "source": "glm"}
            ),
            "reflection-beam",
        )


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


class BeamFamilyTest(unittest.TestCase):
    def test_pi_beam_ids_map_to_the_beam_family(self):
        self.assertEqual(
            model_key("reflection/Beam-1.0-501B-A23B"), ("beam", "beam-1.0")
        )
        self.assertEqual(
            model_key("Beam-1.0-501B-A23B", "beam pi"), ("beam", "beam-1.0")
        )
        self.assertEqual(model_key("beam", "pi"), ("beam", "beam"))

    def test_grok_build_and_commandcode_win_over_beam(self):
        self.assertEqual(model_key("reflection-beam"), ("grok", "reflection-beam"))
        self.assertEqual(
            model_key("reflection/beam:free"),
            ("commandcode", "reflection/beam:free"),
        )
        self.assertEqual(
            model_key("reflection-beam", "pi"), ("grok", "reflection-beam")
        )

    def test_pi_glm_hint_does_not_read_as_grok_build(self):
        self.assertEqual(
            model_key("river/zai-org/GLM-5.3-Flash", "glm pi"),
            ("glm", "glm-5.3-flash"),
        )
        self.assertEqual(
            run_model_key(
                {
                    "model": "river/zai-org/GLM-5.3-Flash",
                    "tier": "glm",
                    "source": "pi",
                }
            ),
            "glm-5.3-flash",
        )
        self.assertEqual(
            run_model_key(
                {
                    "model": "reflection/Beam-1.0-501B-A23B",
                    "tier": "beam",
                    "source": "generic",
                }
            ),
            "beam-1.0",
        )

    def test_grok_build_hint_without_pi_still_groups_as_grok(self):
        self.assertEqual(model_key("glm-5.3", "grok glm"), ("grok", "glm-5.3"))
        self.assertEqual(model_key("future-1", "glm glm"), ("grok", "future-1"))


class OneRunPerProcessTest(unittest.TestCase):
    def test_same_label_and_pid_keeps_the_real_row(self):
        ghost = {"run_id": "grok:a:job", "label": "job", "status": "running", "pid": 7, "model": None, "out_bytes": None, "clock_source": "transcript", "source": "grok", "started_at": 1.0}
        real = dict(ghost, run_id="grok:b:job", model="grok-4.7-xhigh", out_bytes=900)
        other = dict(ghost, run_id="grok:c:other", label="other", pid=8)
        kept = [row["run_id"] for row in _drop_launch_echoes([ghost, real, other])]
        self.assertEqual(kept, ["grok:b:job", "grok:c:other"])


if __name__ == "__main__":
    unittest.main()
