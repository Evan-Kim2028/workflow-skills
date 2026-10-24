"""Label-guard hook: real hook payloads in, exit codes out. No live state."""
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "label_guard_hook.py"


def load():
    spec = importlib.util.spec_from_file_location("label_guard_hook_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = load()

# Exact command shapes from ~/.claude/CLAUDE.md (muse mechanics,
# cursor-agent mechanics), with concrete labels filled in.
MUSE_GOOD = (
    "nohup muse exec --trust-workspace --model muse-spark-1.3-contributor "
    "--reasoning-effort xhigh --workspace /repo --prompt-file /pad/muse/ws11-plan-foo.md "
    "--disable-reminders --user-input-auto-resolve --session-id 11111111-1111-4111-8111-111111111111 "
    "> /pad/muse/out-ws11-plan-foo.md 2> /pad/muse/err-ws11-plan-foo.log &"
)
MUSE_BAD = (
    "nohup muse exec --trust-workspace --model muse-spark-1.3-contributor "
    "--reasoning-effort xhigh --workspace /repo --prompt-file /x/muse/ws11-visuals.md "
    "--disable-reminders --user-input-auto-resolve --session-id 11111111-1111-4111-8111-111111111111 "
    "> /x/muse/out-ws11-visuals.md 2> /x/muse/err-ws11-visuals.log &"
)
GROK_GOOD = (
    "nohup ~/.claude/tools/agent-dash/scripts/launch_grok.sh ws12-plan-notes /repo "
    "/pad/cursor/ws12-plan-notes.md \"grok-4.7[context=500k,reasoning_effort=high,fast=false]\" "
    "< /dev/null > /pad/cursor/out-ws12-plan-notes.md 2> /pad/cursor/err-ws12-plan-notes.log &"
)
GROK_BAD = "nohup ~/.claude/tools/agent-dash/scripts/launch_grok.sh ws12-notes /repo /pad/cursor/ws12-notes.md &"
CODEX_GOOD = (
    "python3 ~/.claude/tools/agent-dash/scripts/launch_codex.py ws11-plan-foo /repo "
    "/pad/ws11-plan-foo.md /runs --model gpt-6-luna --tier codex-luna --reasoning-effort xhigh"
)
CODEX_BAD = "python3 scripts/launch_codex.py ws11-visuals /repo /pad/ws11-visuals.md /runs"
CODEX_EXEC_GOOD = (
    'codex exec --ephemeral --json -m gpt-6-luna -C /repo -o /runs/out-ws11-plan-foo.md - '
    '"[label: ws11-plan-foo] do the task"'
)
CODEX_EXEC_BAD = 'codex exec -m gpt-6-luna "do the task with no label"'


def payload(command, tool="Bash"):
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


class ExtractTests(unittest.TestCase):
    def test_valid_muse_launch_extracts_label(self):
        self.assertEqual(hook.extract_labels(MUSE_GOOD), ["ws11-plan-foo"])

    def test_invalid_muse_launch_extracts_label(self):
        self.assertEqual(hook.extract_labels(MUSE_BAD), ["ws11-visuals"])

    def test_grok_launcher(self):
        self.assertEqual(hook.extract_labels(GROK_GOOD), ["ws12-plan-notes"])
        self.assertEqual(hook.extract_labels(GROK_BAD), ["ws12-notes"])

    def test_codex_launcher(self):
        self.assertEqual(hook.extract_labels(CODEX_GOOD), ["ws11-plan-foo"])
        self.assertEqual(hook.extract_labels(CODEX_BAD), ["ws11-visuals"])

    def test_codex_exec_prompt(self):
        self.assertEqual(hook.extract_labels(CODEX_EXEC_GOOD), ["ws11-plan-foo"])
        self.assertEqual(hook.extract_labels(CODEX_EXEC_BAD), [""])
        # Stdin, subcommand, and flag-only launches are not checkable.
        self.assertEqual(
            hook.extract_labels('codex exec --json -m gpt-6-luna - < /tmp/prompt.md'), []
        )
        self.assertEqual(hook.extract_labels("codex exec resume --last"), [])
        self.assertEqual(hook.extract_labels("codex exec --json"), [])
        # A shell expansion in the prompt fails open.
        self.assertEqual(hook.extract_labels('codex exec "$PROMPT"'), [])

    def test_run_worker_shapes(self):
        self.assertEqual(
            hook.extract_labels("scripts/run_worker.sh muse ws11-plan-x /repo /p.md /runs"),
            ["ws11-plan-x"],
        )
        self.assertEqual(
            hook.extract_labels("/usr/bin/python3 scripts/run_worker.py auto ws11-plan-x /repo /p.md /runs"),
            ["ws11-plan-x"],
        )
        self.assertEqual(
            hook.extract_labels("python3 agent_dash.py report start --label ws11-plan-x --session s"),
            ["ws11-plan-x"],
        )
        self.assertEqual(hook.extract_labels("python3 agent_dash.py doctor"), [])

    def test_tsv_appends(self):
        self.assertEqual(
            hook.extract_labels("echo 'ws11-plan-x\ts\t1' >> /pad/muse-sessions.tsv"),
            ["ws11-plan-x"],
        )
        self.assertEqual(
            hook.extract_labels("printf 'ws11-plan-x\\t-\\t1\\n' >> /pad/cursor-sessions.tsv"),
            ["ws11-plan-x"],
        )
        self.assertEqual(
            hook.extract_labels("echo 'ws11-plan-x' >> /pad/codex-sessions.tsv"),
            ["ws11-plan-x"],
        )
        self.assertEqual(
            hook.extract_labels("echo 'ws11-plan-x' >> /pad/pi-sessions.tsv"),
            ["ws11-plan-x"],
        )
        # Headers and compound lines fail open.
        self.assertEqual(
            hook.extract_labels("echo 'label\\tsession_id\\tepoch' >> /pad/muse-sessions.tsv"), []
        )

    def test_benign_commands_pass(self):
        for command in (
            "grep 'muse exec' notes.md",
            "echo muse exec --prompt-file /x/muse/ws11-visuals.md",
            "while pgrep -f \"muse-bin.*scratchpad[/]muse/ws11-plan-foo\\.md\" >/dev/null; do sleep 10; done; echo \"ws11-plan-foo finished\"",
            "for p in $(pgrep -f '[m]use-bin.*ws11-plan-foo'); do kill $p; done",
            "python3 agent_dash.py doctor --all",
            "ls /pad/muse/",
        ):
            with self.subTest(command=command[:40]):
                self.assertEqual(hook.extract_labels(command), [])


PRINTF_ROW_GOOD = (
    "printf '%s\\t%s\\t%s\\n' 'hub-coord-agentdash-label-guard-clis' 'abc' '123' "
    ">> /pad/muse-sessions.tsv"
)
PRINTF_ROW_BAD = (
    "printf '%s\\t%s\\t%s\\n' 'ws11-visuals' 'sess' '1' >> /pad/muse-sessions.tsv"
)
# Same shape with shell variables: the label is unknowable -> fail open.
PRINTF_ROW_VARS = (
    'printf \'%s\\t%s\\t%s\\n\' "$label" "$sess" "$epoch" >> /pad/muse-sessions.tsv'
)
ECHO_ROW_GOOD = "echo -e 'hub-coord-agentdash-label-guard-clis\\tabc\\t123' >> /pad/muse-sessions.tsv"
ECHO_ROW_BAD = "echo 'ws11-visuals\\tsess\\t1' >> /pad/muse-sessions.tsv"
TEE_ROW_GOOD = "echo 'hub-coord-agentdash-label-guard-clis\\t-\\t1' | tee -a /pad/cursor-sessions.tsv"
TEE_ROW_BAD = "echo 'ws11-visuals\\t-\\t1' | tee -a /pad/cursor-sessions.tsv"
TEE_PRINTF_BAD = (
    "printf '%s\\t%s\\t%s\\n' 'ws11-visuals' sess 1 | tee -a /pad/muse-sessions.tsv"
)


class TsvAppendShapesTests(unittest.TestCase):
    """Manifest-append shapes people really use (live false-positive fix).

    A ``printf`` format string is never the label: the label is the first
    data argument filling the first ``%s``/``%b``. Unresolvable
    expansions fail open; concrete bad labels are still refused.
    """

    def setUp(self):
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = "/nonexistent-ws-dir-for-tests"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old

    def test_printf_format_string_is_never_the_label(self):
        self.assertEqual(
            hook.extract_labels(PRINTF_ROW_GOOD),
            ["hub-coord-agentdash-label-guard-clis"],
        )
        self.assertEqual(hook.judge(PRINTF_ROW_GOOD), (0, ""))

    def test_printf_bad_label_still_refused(self):
        self.assertEqual(hook.extract_labels(PRINTF_ROW_BAD), ["ws11-visuals"])
        code, err = hook.judge(PRINTF_ROW_BAD)
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)

    def test_printf_variables_fail_open(self):
        self.assertEqual(hook.extract_labels(PRINTF_ROW_VARS), [])
        self.assertEqual(hook.judge(PRINTF_ROW_VARS), (0, ""))

    def test_printf_percent_b_and_literal_format(self):
        self.assertEqual(
            hook.extract_labels("printf '%b\\t%b\\n' 'ws11-plan-foo' 'x' >> /pad/muse-sessions.tsv"),
            ["ws11-plan-foo"],
        )
        # No conversions: the literal format is the row (pre-existing shape).
        self.assertEqual(
            hook.extract_labels("printf 'ws11-plan-x\\t-\\t1\\n' >> /pad/cursor-sessions.tsv"),
            ["ws11-plan-x"],
        )
        # Conversions with no data args: fail open, never refuse '%s'.
        self.assertEqual(
            hook.extract_labels("printf '%s\\t%s\\n' >> /pad/muse-sessions.tsv"), []
        )

    def test_echo_rows(self):
        self.assertEqual(
            hook.extract_labels(ECHO_ROW_GOOD),
            ["hub-coord-agentdash-label-guard-clis"],
        )
        self.assertEqual(hook.judge(ECHO_ROW_GOOD), (0, ""))
        self.assertEqual(hook.extract_labels(ECHO_ROW_BAD), ["ws11-visuals"])
        code, err = hook.judge(ECHO_ROW_BAD)
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)

    def test_tee_rows(self):
        self.assertEqual(
            hook.extract_labels(TEE_ROW_GOOD),
            ["hub-coord-agentdash-label-guard-clis"],
        )
        self.assertEqual(hook.judge(TEE_ROW_GOOD), (0, ""))
        self.assertEqual(hook.extract_labels(TEE_ROW_BAD), ["ws11-visuals"])
        code, err = hook.judge(TEE_ROW_BAD)
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)
        self.assertEqual(hook.extract_labels(TEE_PRINTF_BAD), ["ws11-visuals"])
        self.assertEqual(hook.judge(TEE_PRINTF_BAD)[0], 2)
        # A non-sessions tee target is untouched.
        self.assertEqual(hook.extract_labels("echo hello | tee -a /pad/notes.txt"), [])

    def test_here_string_and_heredoc(self):
        self.assertEqual(
            hook.extract_labels("cat <<< 'ws11-plan-foo\\ts\\t1' >> /pad/muse-sessions.tsv"),
            ["ws11-plan-foo"],
        )
        self.assertEqual(
            hook.extract_labels(
                "cat >> /pad/muse-sessions.tsv <<EOF\nws11-visuals\\ts\\t1\nEOF"
            ),
            ["ws11-visuals"],
        )


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = "/nonexistent-ws-dir-for-tests"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old

    def test_valid_launch_passes(self):
        self.assertEqual(hook.judge(MUSE_GOOD), (0, ""))
        self.assertEqual(hook.judge(GROK_GOOD), (0, ""))
        self.assertEqual(hook.judge(CODEX_GOOD), (0, ""))
        self.assertEqual(hook.judge(CODEX_EXEC_GOOD), (0, ""))

    def test_invalid_launch_refuses_with_fix(self):
        code, err = hook.judge(MUSE_BAD)
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)
        code, err = hook.judge(CODEX_EXEC_BAD)
        self.assertEqual(code, 2)
        self.assertIn("label", err)

    def test_benign_passes(self):
        self.assertEqual(hook.judge("grep 'muse exec' notes.md"), (0, ""))

    def test_malformed_payload_passes(self):
        for raw in ("", "not json", "[]", "{}", '{"tool_name": "Edit"}'):
            with self.subTest(raw=raw[:20]):
                proc = subprocess.run(
                    ["python3", str(HOOK)],
                    input=raw,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(proc.returncode, 0, raw)


class InstallTests(unittest.TestCase):
    """The --claude-label-guard installer against a fake HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text('{"model": "sonnet"}', encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def _install(self, *args):
        return subprocess.run(
            ["sh", str(ROOT / "scripts" / "install_hooks.sh"), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )

    def test_install_adds_one_bash_entry_and_keeps_settings(self):
        for _ in range(2):
            proc = self._install("--claude-label-guard")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "sonnet")
        pre = settings["hooks"]["PreToolUse"]
        hits = [item for item in pre if "label_guard_hook.py" in json.dumps(item)]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["matcher"], "Bash")
        command = hits[0]["hooks"][0]["command"]
        self.assertTrue(Path(command.split(" ", 1)[1]).is_file(), command)

    def test_install_without_flag_still_gates_pushes(self):
        proc = self._install("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--claude-label-guard", proc.stdout)


class PipeTests(unittest.TestCase):
    """End to end through the real script: payload on stdin, exit code out."""

    def run_hook(self, command):
        proc = subprocess.run(
            ["python3", str(HOOK)],
            input=payload(command),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "AGENT_DASH_WORKSTREAMS": "/nonexistent-ws-dir-for-tests"},
        )
        return proc.returncode, proc.stderr

    def test_pipe_valid(self):
        code, _err = self.run_hook(MUSE_GOOD)
        self.assertEqual(code, 0)

    def test_pipe_invalid(self):
        code, err = self.run_hook(MUSE_BAD)
        self.assertEqual(code, 2)
        self.assertIn("use ws11-plan-visuals", err)

    def test_pipe_benign(self):
        code, _err = self.run_hook("grep 'muse exec' notes.md")
        self.assertEqual(code, 0)

    def test_pipe_fast(self):
        start = time.time()
        self.run_hook(MUSE_BAD)
        self.assertLess(time.time() - start, 5.0)


if __name__ == "__main__":
    unittest.main()
