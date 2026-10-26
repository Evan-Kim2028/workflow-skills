from unittest import mock
"""Tests for scripts/launch_codex.py: stream parsing, usage ledger, argv.

Synthetic fixtures only: a fabricated ``--json`` event stream, no live
state, no model calls, no subprocesses.
"""
import importlib.util
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import sys

sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "launch_codex", ROOT / "scripts" / "launch_codex.py"
)
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)

STREAM = (
    b'{"type":"thread.started","thread_id":"01a16401-9955-70cd-bf8c-19944db4f20c"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PASS\\nbuilt it"}}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":13885,"cached_input_tokens":11520,'
    b'"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":2}}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,'
    b'"cache_write_input_tokens":0,"output_tokens":10,"reasoning_output_tokens":0}}\n'
    b"not json at all\n"
)


class ParseStreamTests(unittest.TestCase):
    def test_thread_usage_and_text(self):
        parsed = launch.parse_stream(STREAM)
        self.assertEqual(parsed["thread_id"], "01a16401-9955-70cd-bf8c-19944db4f20c")
        self.assertEqual([entry["message_id"] for entry in parsed["usage"]], ["turn-1", "turn-2"])
        self.assertEqual(parsed["text"], "PASS\nbuilt it")

    def test_empty_stream_has_no_thread(self):
        parsed = launch.parse_stream(b"")
        self.assertIsNone(parsed["thread_id"])
        self.assertEqual(parsed["usage"], [])
        self.assertEqual(parsed["text"], "")

    def test_later_thread_start_does_not_replace_the_first(self):
        raw = (
            b'{"type":"thread.started","thread_id":"aaa"}\n'
            b'{"type":"thread.started","thread_id":"bbb"}\n'
        )
        self.assertEqual(launch.parse_stream(raw)["thread_id"], "aaa")

    def test_non_assistant_items_are_not_final_text(self):
        raw = b'{"type":"item.completed","item":{"id":"i1","type":"tool_call","text":"ls"}}\n'
        self.assertEqual(launch.parse_stream(raw)["text"], "")


class UsageRowTests(unittest.TestCase):
    def test_input_is_the_uncached_miss(self):
        counts = launch.usage_row(
            {"input_tokens": 13885, "cached_input_tokens": 11520,
             "cache_write_input_tokens": 0, "output_tokens": 5,
             "reasoning_output_tokens": 2}
        )
        self.assertEqual(counts["input"], 13885 - 11520)
        self.assertEqual(counts["cache_read"], 11520)
        self.assertEqual(counts["cache_write"], 0)
        self.assertEqual(counts["output"], 5)
        self.assertEqual(counts["reasoning"], 2)

    def test_missing_fields_read_as_zero(self):
        counts = launch.usage_row({})
        self.assertEqual(counts, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0})


class VerdictTests(unittest.TestCase):
    def test_first_line_wins(self):
        self.assertEqual(launch.verdict_of("PASS\nbuilt it"), "PASS")
        self.assertEqual(launch.verdict_of("blocked by quota"), "BLOCKED")
        self.assertEqual(launch.verdict_of("some notes"), "PARTIAL")
        self.assertEqual(launch.verdict_of(""), "PARTIAL")

    def test_quota_markers(self):
        self.assertTrue(launch.quota_hit("error 429 rate limit exceeded"))
        self.assertTrue(launch.quota_hit("insufficient credits for this run"))
        self.assertFalse(launch.quota_hit("PASS\nbuilt it"))


class ArgvTests(unittest.TestCase):
    def test_headless_command_line(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENT_DASH_CODEX_SANDBOX", None)
                argv = launch.build_argv("codex", "/ws", Path("/out/report.md"), "gpt-6-luna", "xhigh")
        self.assertEqual(argv[:3], ["codex", "exec", "--ephemeral"])
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
        self.assertIn('model_reasoning_effort="xhigh"', " ".join(argv))
        self.assertIn("workspace-write", argv)
        self.assertIn('approval_policy="never"', " ".join(argv))
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("-o") + 1], "/out/report.md")
        self.assertEqual(argv[-1], "-")
        # The prompt travels on stdin, never in argv.
        self.assertNotIn("do the task", " ".join(argv))

    def test_sandbox_env_override(self):
        with mock.patch.dict(os.environ, {"AGENT_DASH_CODEX_SANDBOX": "danger-full-access"}):
            argv = launch.build_argv("codex", "/ws", Path("/o.md"), "gpt-6-luna", "xhigh")
        self.assertEqual(argv[argv.index("-s") + 1], "danger-full-access")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENT_DASH_CODEX_SANDBOX": "bogus", "CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                argv = launch.build_argv("codex", "/ws", Path("/o.md"), "gpt-6-luna", "xhigh")
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")

    def test_label_prefix(self):
        self.assertEqual(
            launch.ensure_label_prefix("do the task\n", "ws1-plan-x"),
            "[label: ws1-plan-x]\ndo the task\n",
        )
        self.assertEqual(
            launch.ensure_label_prefix("[label: ws1-plan-x]\ndo it\n", "ws1-plan-x"),
            "[label: ws1-plan-x]\ndo it\n",
        )


class SandboxResolutionTests(unittest.TestCase):
    def _write_config(self, directory, body):
        path = Path(directory) / "config.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_env_wins_over_config(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(tmp, 'sandbox_mode = "read-only"\n')
            env = {"AGENT_DASH_CODEX_SANDBOX": "danger-full-access", "CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                mode, source = launch.codex_sandbox_with_source()
        self.assertEqual(mode, "danger-full-access")
        self.assertIn("env", source)

    def test_config_mode_used_when_env_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(tmp, 'sandbox_mode = "danger-full-access"\n')
            env = {"CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENT_DASH_CODEX_SANDBOX", None)
                mode, source = launch.codex_sandbox_with_source()
        self.assertEqual(mode, "danger-full-access")
        self.assertIn("config", source)

    def test_table_keys_do_not_count_as_top_level(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(tmp, '[profiles.full]\nsandbox_mode = "danger-full-access"\n')
            env = {"CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENT_DASH_CODEX_SANDBOX", None)
                mode, source = launch.codex_sandbox_with_source()
        self.assertEqual(mode, "workspace-write")
        self.assertEqual(source, "default")

    def test_invalid_config_falls_back_to_default(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(tmp, 'sandbox_mode = "bogus"\n')
            env = {"CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENT_DASH_CODEX_SANDBOX", None)
                mode, source = launch.codex_sandbox_with_source()
        self.assertEqual(mode, "workspace-write")
        self.assertEqual(source, "default")

    def test_missing_config_falls_back_to_default(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENT_DASH_CODEX_SANDBOX", None)
                mode, source = launch.codex_sandbox_with_source()
        self.assertEqual(mode, "workspace-write")
        self.assertEqual(source, "default")

    def test_invalid_env_falls_through_to_config(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(tmp, 'sandbox_mode = "read-only"\n')
            env = {"AGENT_DASH_CODEX_SANDBOX": "bogus", "CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                mode, source = launch.codex_sandbox_with_source()
        self.assertEqual(mode, "read-only")
        self.assertIn("config", source)

    def test_argv_carries_resolved_sandbox(self):
        argv = launch.build_argv(
            "codex", "/ws", Path("/o.md"), "gpt-6-luna", "xhigh", "danger-full-access"
        )
        self.assertEqual(argv[argv.index("-s") + 1], "danger-full-access")


class SandboxHintTests(unittest.TestCase):
    def test_operation_not_permitted_triggers_hint(self):
        self.assertTrue(launch.sandbox_hint_needed("Operation not permitted"))
        self.assertTrue(launch.sandbox_hint_needed("ssh: connect to host failed"))
        self.assertTrue(launch.sandbox_hint_needed("Could not resolve host example.com"))
        self.assertTrue(launch.sandbox_hint_needed("Network is unreachable"))

    def test_clean_output_needs_no_hint(self):
        self.assertFalse(launch.sandbox_hint_needed("PASS\nbuilt it"))
        self.assertFalse(launch.sandbox_hint_needed(""))


class DoctorTests(unittest.TestCase):
    def test_doctor_exits_zero_without_a_model(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = launch.main(["--doctor"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        for needle in ("codex:", "version:", "model:", "effort:", "config:", "sandbox:"):
            self.assertIn(needle, out)

    def test_check_alias_matches_doctor(self):
        import contextlib
        import io

        for flag in ("--check", "doctor", "check"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = launch.main([flag])
            self.assertEqual(code, 0)
            self.assertIn("sandbox:", buf.getvalue())

    def test_doctor_warns_under_workspace_write(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODEX_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENT_DASH_CODEX_SANDBOX", None)
                lines = launch.doctor_lines()
        self.assertTrue(any(line.startswith("sandbox: workspace-write") for line in lines))
        self.assertTrue(any("warning:" in line for line in lines))

    def test_doctor_names_env_source(self):
        with mock.patch.dict(
            os.environ, {"AGENT_DASH_CODEX_SANDBOX": "danger-full-access"}, clear=False
        ):
            lines = launch.doctor_lines()
        self.assertTrue(
            any("danger-full-access" in line and "env" in line for line in lines)
        )
        self.assertFalse(any("warning:" in line for line in lines))


class MainTests(unittest.TestCase):
    def test_weak_label_refuses_before_launch(self):
        old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = "/nonexistent-ws-dir-for-tests"
        try:
            code = launch.main(
                ["ws1-visuals", "/tmp", "/tmp/prompt.md", "/tmp/out", "--model", "gpt-6-luna"]
            )
        finally:
            if old is None:
                os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
            else:
                os.environ["AGENT_DASH_WORKSTREAMS"] = old
        self.assertEqual(code, 2)

    def test_bad_argc_is_usage(self):
        self.assertEqual(launch.main(["only-a-label"]), 2)


if __name__ == "__main__":
    unittest.main()
