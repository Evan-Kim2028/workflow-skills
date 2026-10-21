"""Label guard across CLIs: real hook payload shapes in, exit codes out.

Covers the Claude Code payload (tool_name/tool_input), the Grok Build
payload (toolName/toolInput with the native run_terminal_command tool
name), and Muse-style key variants. The shim (shims/muse) is tested
with REAL_MUSE pointed at /bin/echo so no worker ever launches.
No live state, no model calls.
"""
import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "label_guard_hook.py"
SHIM = ROOT / "shims" / "muse"


def load():
    spec = importlib.util.spec_from_file_location("label_guard_hook_clis", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = load()

MUSE_BAD = (
    "nohup muse exec --trust-workspace --workspace /repo "
    "--prompt-file /x/muse/ws11-visuals.md --session-id 11111111-1111-4111-8111-111111111111 "
    "> /x/out.md 2> /x/err.log &"
)
MUSE_GOOD = MUSE_BAD.replace("ws11-visuals", "ws11-plan-visuals")


def run_hook(payload):
    proc = subprocess.run(
        ["python3", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "AGENT_DASH_WORKSTREAMS": "/nonexistent-ws-dir-for-tests"},
    )
    return proc.returncode, proc.stderr


class GrokPayloadTests(unittest.TestCase):
    """Grok PreToolUse payload: camelCase keys, native tool name."""

    def payload(self, command, tool="run_terminal_command"):
        return {
            "hook_event_name": "PreToolUse",
            "hookEventName": "pre_tool_use",
            "sessionId": "abc-123",
            "cwd": "/repo",
            "toolName": tool,
            "toolInput": {"command": command},
        }

    def test_valid_launch_passes(self):
        self.assertEqual(run_hook(self.payload(MUSE_GOOD)), (0, ""))

    def test_invalid_launch_refused(self):
        code, err = run_hook(self.payload(MUSE_BAD))
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)

    def test_bash_matcher_alias_name_also_works(self):
        code, err = run_hook(self.payload(MUSE_BAD, tool="Bash"))
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)

    def test_benign_command_passes(self):
        code, _ = run_hook(self.payload("grep 'muse exec' notes.md"))
        self.assertEqual(code, 0)

    def test_non_shell_tool_passes(self):
        code, _ = run_hook(self.payload(MUSE_BAD, tool="read_file"))
        self.assertEqual(code, 0)

    def test_malformed_payload_passes(self):
        for payload in (
            {},
            {"toolName": "run_terminal_command"},
            {"toolName": "run_terminal_command", "toolInput": {"command": ""}},
            {"toolName": "run_terminal_command", "toolInput": "not-a-dict"},
            {"toolName": "run_terminal_command", "toolInput": {"cmd": MUSE_BAD}},
        ):
            with self.subTest(payload=str(payload)[:60]):
                self.assertEqual(run_hook(payload)[0], 0)


class MusePayloadTests(unittest.TestCase):
    """Muse hook-test fixture shape: {"event", "stdin"} is unwrapped by
    the harness; the guard reads the stdin object. Variants documented."""

    def test_shell_key_variant(self):
        code, err = run_hook({"tool": "Shell", "input": {"command": MUSE_BAD}})
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)

    def test_unknown_tool_passes(self):
        code, _ = run_hook({"tool_name": "Write", "tool_input": {"command": MUSE_BAD}})
        self.assertEqual(code, 0)


class ShimTests(unittest.TestCase):
    """shims/muse with the real binary pointed at /bin/echo."""

    def setUp(self):
        self.env = {
            **os.environ,
            "LABEL_GUARD_REAL_MUSE": "/bin/echo",
            "LABEL_GUARD_HOOK": str(HOOK),
            "AGENT_DASH_WORKSTREAMS": "/nonexistent-ws-dir-for-tests",
        }

    def run_shim(self, *args):
        return subprocess.run(
            ["sh", str(SHIM), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )

    def test_bad_label_refused_without_running_real_binary(self):
        proc = self.run_shim(
            "exec", "--workspace", "/repo",
            "--prompt-file", "/x/muse/ws11-visuals.md",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("use ws11-plan-visuals", proc.stderr)
        self.assertNotIn("--prompt-file", proc.stdout)

    def test_good_label_passes_through(self):
        proc = self.run_shim(
            "exec", "--workspace", "/repo",
            "--prompt-file", "/x/muse/ws11-plan-foo.md",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ws11-plan-foo", proc.stdout)

    def test_non_exec_passes_through(self):
        proc = self.run_shim("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--help", proc.stdout)

    def test_exec_without_prompt_file_passes_through(self):
        proc = self.run_shim("exec", "do research")
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
