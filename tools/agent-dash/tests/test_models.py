import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STATIC = Path(__file__).resolve().parents[1] / "static"

from dash.models import HARNESS_NAMES, harness_of, model_key, run_model_key
from dash.projectfeed import _tool_key as _feed_tool_key
from dash.store import _drop_launch_echoes
from dash.store.util import _tool_key


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


class BeamFamilyTest(unittest.TestCase):
    def test_beam_ids_map_to_beam(self):
        cases = {
            ("reflection/beam:free", "pi pi"): ("beam", "beam"),
            ("reflection/Beam-1.0-501B-A23B", "pi pi"): ("beam", "beam-1.0"),
            ("reflection/Beam-1.0-501B-A23B:xhigh", "generic beam"): ("beam", "beam-1.0"),
            ("reflection-beam", "glm glm"): ("beam", "beam"),
            ("reflection/beam:free", "commandcode commandcode"): ("beam", "beam"),
            ("", "beam"): ("beam", "beam-unknown"),
        }
        for (model, hint), want in cases.items():
            self.assertEqual(model_key(model, hint), want, (model, hint))
            self.assertEqual(
                run_model_key({"model": model or None, "tier": hint.split()[0], "source": hint.split()[-1]}),
                want[1],
                (model, hint),
            )

    def test_tool_key_for_beam_pi_generic_and_commandcode_rows(self):
        for run in (
            {"source": "pi", "tier": "pi", "model": "reflection/beam:free"},
            {"source": "pi", "tier": "pi", "model": "reflection/Beam-1.0-501B-A23B"},
            {"source": "generic", "tier": "beam", "model": "reflection/Beam-1.0-501B-A23B:xhigh"},
            {"source": "glm", "tier": "glm", "model": "reflection-beam"},
            {"source": "commandcode", "tier": "commandcode", "model": "reflection/beam:free"},
        ):
            self.assertEqual(_tool_key(run), "beam", run)
            self.assertEqual(_feed_tool_key(run), "beam", run)
        both = {"source": "commandcode", "tier": "commandcode", "model": "minimax/minimax-m3.1-flash:free"}
        self.assertEqual(_tool_key(both), "commandcode")
        self.assertEqual(_feed_tool_key(both), "commandcode")
        deepseek = {"source": "pi", "tier": "deepseek", "model": "river/deepseek-ai/DeepSeek-V4.1-Flash-1017"}
        self.assertEqual(_tool_key(deepseek), "deepseek")
        self.assertEqual(_feed_tool_key(deepseek), "deepseek")


class HarnessTest(unittest.TestCase):
    def test_harness_of_reads_source_and_generic_tier(self):
        cases = {
            ("claude-main", "opus"): "claude",
            ("muse", "muse"): "muse",
            ("grok", "grok"): "cursor",
            ("cursor", "grok"): "cursor",
            ("glm", "glm"): "grok-cli",
            ("pi", "beam"): "pi",
            ("pi", "deepseek"): "pi",
            ("commandcode", "commandcode"): "commandcode",
            ("codex", "codex"): "codex",
            ("codex-exec", "codex"): "codex",
            ("dagster", ""): "dagster",
            ("generic", "generic"): "generic",
            ("generic", "beam"): "generic",
            ("generic", "grok"): "cursor",
            ("generic", "opus"): "claude",
            ("generic", "glm"): "grok-cli",
            ("weird", ""): "weird",
        }
        for (source, tier), want in cases.items():
            self.assertEqual(harness_of({"source": source, "tier": tier}), want, (source, tier))
        self.assertEqual(HARNESS_NAMES["grok-cli"], "Grok Build")
        self.assertEqual(HARNESS_NAMES["commandcode"], "Command Code")
        self.assertEqual(HARNESS_NAMES["pi"], "pi")

    def test_beam_runs_report_pi_harness_with_beam_model(self):
        run = {"source": "pi", "tier": "beam", "model": "reflection/Beam-1.0-501B-A23B:xhigh"}
        self.assertEqual(harness_of(run), "pi")
        self.assertEqual(run_model_key(run), "beam-1.0")


class BeamJsTests(unittest.TestCase):
    def test_models_js_harness_and_series(self):
        script = (
            "const fs=require('fs');"
            "global.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const assert=require('assert');"
            "const ADM=window.ADM;"
            "assert.strictEqual(ADM.harnessOf({source:'pi',tier:'beam'}),'pi');"
            "assert.strictEqual(ADM.harnessOf({source:'pi',tier:'beam',harness:'pi'}),'pi');"
            "assert.strictEqual(ADM.harnessOf({source:'commandcode',tier:'commandcode'}),'commandcode');"
            "assert.strictEqual(ADM.harnessOf({source:'glm',tier:'glm'}),'grok-cli');"
            "assert.strictEqual(ADM.harnessOf({source:'generic',tier:'beam'}),'generic');"
            "assert.strictEqual(ADM.harnessOf({source:'generic',tier:'grok'}),'cursor');"
            "assert.strictEqual(ADM.harnessOf({source:'codex-exec',tier:'codex'}),'codex');"
            "assert.strictEqual(ADM.harnessOf({source:'weird',tier:''}),'weird');"
            "assert.strictEqual(ADM.harnessName('grok-cli'),'Grok Build');"
            "assert.strictEqual(ADM.harnessName('pi'),'pi');"
            "assert.strictEqual(ADM.seriesName('beam-1.0@pi'),'Beam 1.0 \\u00B7 pi');"
            "assert.strictEqual(ADM.seriesName('beam@commandcode'),'Beam \\u00B7 Command Code');"
            "assert.strictEqual(ADM.seriesName('deepseek-flash@pi'),'DeepSeek V4.1 Flash \\u00B7 pi');"
            "assert.strictEqual(ADM.seriesName('deepseek-flash@grok-cli'),'DeepSeek V4.1 Flash \\u00B7 Grok Build');"
            "assert.strictEqual(ADM.seriesName('beam-1.0'),'Beam 1.0');"
            "assert.deepStrictEqual(ADM.splitSeries('beam-1.0@pi'),{model:'beam-1.0',harness:'pi'});"
            "var sibs=['beam-1.0@pi','beam-1.0@commandcode'];"
            "var single=ADM.seriesColor('beam-1.0@pi',['beam-1.0@pi']);"
            "assert.strictEqual(single,ADM.colorOf('beam-1.0'));"
            "var a=ADM.seriesColor('beam-1.0@pi',sibs),b=ADM.seriesColor('beam-1.0@commandcode',sibs);"
            "assert.notStrictEqual(a,b);"
            "assert.ok(a.indexOf('--s11')>=0 && b.indexOf('--s11')>=0);"
            "assert.deepStrictEqual(ADM.sortSeries(['deepseek-flash@pi','beam-1.0@pi']),['deepseek-flash@pi','beam-1.0@pi']);"
            "assert.deepStrictEqual(ADM.sortSeries(['beam-1.0@pi','beam-1.0@commandcode']),['beam-1.0@commandcode','beam-1.0@pi']);"
            "assert.deepStrictEqual(ADM.FAM.beam.order,['beam-1.0','beam']);"
            "console.log('models harness proof ok');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "models.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("models harness proof ok", proc.stdout)

    def test_models_js_beam_family(self):
        script = (
            "const fs=require('fs');"
            "global.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const assert=require('assert');"
            "const keyOf=window.ADM.keyOf;"
            "assert.deepStrictEqual(keyOf('reflection/beam:free','pi pi'),['beam','beam']);"
            "assert.deepStrictEqual(keyOf('reflection/Beam-1.0-501B-A23B','pi pi'),['beam','beam-1.0']);"
            "assert.deepStrictEqual(keyOf('reflection/Beam-1.0-501B-A23B:xhigh','generic beam'),['beam','beam-1.0']);"
            "assert.deepStrictEqual(keyOf('reflection-beam','glm glm'),['beam','beam']);"
            "assert.deepStrictEqual(keyOf('reflection/beam:free','commandcode commandcode'),['beam','beam']);"
            "assert.deepStrictEqual(keyOf('','beam'),['beam','beam-unknown']);"
            "assert.deepStrictEqual(keyOf('','commandcode'),['commandcode','commandcode-unknown']);"
            "assert.strictEqual(window.ADM.nameOf('beam-1.0'),'Beam 1.0');"
            "assert.strictEqual(window.ADM.nameOf('beam'),'Beam');"
            "assert.strictEqual(window.ADM.nameOf('poolside/laguna-s-2.1-free'),'laguna-s-2.1-free');"
            "assert.strictEqual(window.ADM.nameOf('stealth/glyph-cluster:free'),'glyph-cluster:free');"
            "assert.strictEqual(window.ADM.famOf('beam-1.0'),'beam');"
            "assert.strictEqual(window.ADM.FAMS.indexOf('beam'),window.ADM.FAMS.indexOf('deepseek')+1);"
            "assert.strictEqual(window.ADM.FAM.commandcode.order.indexOf('reflection/beam:free'),-1);"
            "assert.strictEqual(window.ADM.FAM.beam.hue,'--s11');"
            "console.log('models beam family proof ok');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "models.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("models beam family proof ok", proc.stdout)


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
