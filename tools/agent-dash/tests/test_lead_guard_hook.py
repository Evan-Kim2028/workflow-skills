"""Lead-guard hook: synthetic transcripts in, exit codes out. No live state."""
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "lead_guard_hook.py"


def load():
    spec = importlib.util.spec_from_file_location("lead_guard_hook_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = load()

# A real worker launch shape (label-guard strict-passing label ws11-plan-foo).
MUSE_LAUNCH = (
    "nohup muse exec --trust-workspace --model muse-spark-1.3-contributor "
    "--reasoning-effort xhigh --workspace /repo --prompt-file /pad/muse/ws11-plan-foo.md "
    "--disable-reminders --user-input-auto-resolve --session-id 11111111-1111-4111-8111-111111111111 "
    "> /pad/muse/out-ws11-plan-foo.md 2> /pad/muse/err-ws11-plan-foo.log &"
)
HUB_LAUNCH = MUSE_LAUNCH.replace("ws11-plan-foo", "hub-coord-foo")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def old_iso(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def au(tool_id, name, input_, ts=None):
    return {
        "type": "assistant",
        "timestamp": ts or now_iso(),
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": input_}
            ],
        },
    }


def ures(tool_id, text, ts=None):
    return {
        "type": "user",
        "timestamp": ts or now_iso(),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text}
            ],
        },
    }


def notify(task_id=None, tool_use_id=None):
    body = "<task-notification>\n"
    if task_id is not None:
        body += "<task-id>%s</task-id>\n" % task_id
    if tool_use_id is not None:
        body += "<tool-use-id>%s</tool-use-id>\n" % tool_use_id
    body += "<output-file>/tmp/tasks/x.output</output-file>\n</task-notification>"
    return {
        "type": "queue-operation",
        "timestamp": now_iso(),
        "content": body,
    }


def write_transcript(tmpdir, records):
    path = os.path.join(tmpdir, "transcript.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def ask_payload(path):
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "q?"}]},
            "transcript_path": path,
        }
    )


def stop_payload(path, active=False):
    return json.dumps(
        {
            "hook_event_name": "Stop",
            "stop_hook_active": active,
            "transcript_path": path,
        }
    )


def bg_launch(tool_id="toolu_bg1", task_id="bbg111aaa", command=MUSE_LAUNCH):
    return [
        au(tool_id, "Bash", {"command": command, "run_in_background": True}),
        ures(
            tool_id,
            "Command running in background with ID: %s. Output is being "
            "written to: /tmp/tasks/%s.output." % (task_id, task_id),
        ),
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_ready = hook.get_ready_tasks
        self.old_ps = hook.list_processes
        self.addCleanup(setattr, hook, "get_ready_tasks", self.old_ready)
        self.addCleanup(setattr, hook, "list_processes", self.old_ps)
        hook.get_ready_tasks = lambda ws: []
        hook.list_processes = lambda: ""
        for key in ("AGENT_DASH_LEAD_GUARD", "AGENT_DASH_LEAD_GUARD_MAX_AGE_S"):
            if key in os.environ:
                old = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, old)

    def run_hook(self, payload, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            ["python3", str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        return proc.returncode, proc.stderr


class AskTests(Base):
    def test_non_lead_allowed(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_1", "Bash", {"command": "grep foo notes.md"}),
                ures("toolu_1", "no matches"),
            ],
        )
        code, err = self.run_hook(ask_payload(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(hook.check_ask(json.loads(ask_payload(path))), (0, ""))

    def test_lead_pending_refused(self):
        path = write_transcript(self.tmp.name, bg_launch())
        code, err = self.run_hook(ask_payload(path))
        self.assertEqual(code, 2)
        self.assertIn("plain text", err)
        code2, err2 = hook.check_ask(json.loads(ask_payload(path)))
        self.assertEqual(code2, 2)
        self.assertIn("plain text", err2)

    def test_lead_all_notified_allowed(self):
        path = write_transcript(
            self.tmp.name,
            bg_launch() + [notify(task_id="bbg111aaa", tool_use_id="toolu_bg1")],
        )
        code, err = self.run_hook(ask_payload(path))
        self.assertEqual(code, 0, err)

    def test_auto_backgrounded_counts_as_pending(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_f1", "Bash", {"command": MUSE_LAUNCH}),
                ures(
                    "toolu_f1",
                    "Command did not complete within its 120s timeout and was "
                    "moved to the background (ID: bautobg12). Output is being "
                    "written to: /tmp/tasks/bautobg12.output.",
                ),
            ],
        )
        self.assertEqual(
            hook.check_ask(json.loads(ask_payload(path)))[0], 2
        )

    def test_stopped_task_counts_as_finished(self):
        path = write_transcript(
            self.tmp.name,
            bg_launch(task_id="bstop001a") + [ures("toolu_x", "Successfully stopped task: bstop001a")],
        )
        self.assertEqual(hook.check_ask(json.loads(ask_payload(path))), (0, ""))

    def test_notification_tool_use_id_only(self):
        path = write_transcript(
            self.tmp.name, bg_launch() + [notify(tool_use_id="toolu_bg1")]
        )
        self.assertEqual(hook.check_ask(json.loads(ask_payload(path))), (0, ""))

    def test_notification_task_id_only(self):
        path = write_transcript(
            self.tmp.name, bg_launch() + [notify(task_id="bbg111aaa")]
        )
        self.assertEqual(hook.check_ask(json.loads(ask_payload(path))), (0, ""))

    def test_start_older_than_max_age_ignored(self):
        ts = old_iso(9)
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_o1", "Bash", {"command": MUSE_LAUNCH, "run_in_background": True}, ts=ts),
                ures(
                    "toolu_o1",
                    "Command running in background with ID: boldstart1. Output "
                    "is being written to: /tmp/tasks/boldstart1.output.",
                    ts=ts,
                ),
            ],
        )
        # The 9 h old launch still marks the session a lead (12 h window),
        # but the 9 h old start is past the 8 h max age: nothing pending.
        self.assertTrue(
            hook.is_lead(hook.scan_transcript(path), time.time())
        )
        self.assertEqual(hook.check_ask(json.loads(ask_payload(path))), (0, ""))

    def test_agent_async_launch_is_pending(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_a1", "Agent", {"prompt": "do the lane"}),
                ures("toolu_a1", "Agent launched with ID: ag_7c2; running in the background."),
            ],
        )
        code, err = hook.check_ask(json.loads(ask_payload(path)))
        self.assertEqual(code, 2)
        self.assertIn("plain text", err)

    def test_agent_finished_report_is_not_pending(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_a2", "Agent", {"prompt": "do the lane"}),
                ures("toolu_a2", "PASS. I updated the lane files and all scoped tests pass."),
            ],
        )
        self.assertEqual(hook.check_ask(json.loads(ask_payload(path))), (0, ""))


class StopTests(Base):
    def test_stop_hook_active_allowed(self):
        path = write_transcript(self.tmp.name, bg_launch())
        code, err = self.run_hook(stop_payload(path, active=True))
        self.assertEqual(code, 0, err)

    def test_lead_with_pending_allowed(self):
        path = write_transcript(self.tmp.name, bg_launch())
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_unexpired_wakeup_allowed(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
                au("toolu_w1", "ScheduleWakeup", {"delaySeconds": 3600}),
            ],
        )
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_ready_task_refused_and_listed(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
            ],
        )
        hook.get_ready_tasks = lambda ws: (
            [{"id": "WS11.1", "title": "Do the thing", "live_run": None}]
            if ws == "WS11"
            else []
        )
        code, err = hook.check_stop(json.loads(stop_payload(path)))
        self.assertEqual(code, 2)
        self.assertIn("WS11.1", err)

    def test_ready_gate_does_not_count(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
            ],
        )
        hook.get_ready_tasks = lambda ws: [
            {"id": "WS11.g", "title": "Outside signal", "kind": "gate", "live_run": None}
        ]
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_launch_outside_window_names_no_workstream(self):
        old = old_iso(20)
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l0", "Bash", {"command": MUSE_LAUNCH}, ts=old),
                ures("toolu_l0", "done", ts=old),
                au("toolu_h1", "Bash", {"command": HUB_LAUNCH}),
                ures("toolu_h1", "done"),
            ],
        )
        facts = hook.scan_transcript(path)
        self.assertEqual(hook.recent_labels(facts, time.time()), ["hub-coord-foo"])

        def boom(ws):
            raise AssertionError("an old launch must not drive the ready lookup")

        hook.get_ready_tasks = boom
        hook.list_processes = lambda: "  123 python3 scripts/launch_codex.py ws11-plan-foo /repo /pad/x.md /runs\n"
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_unresolved_label_fragment_never_matches_ps(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH.replace("ws11-plan-foo", "$L")}),
                ures("toolu_l1", "done"),
            ],
        )
        facts = hook.scan_transcript(path)
        self.assertTrue(hook.is_lead(facts, time.time()))
        self.assertEqual(hook.recent_labels(facts, time.time()), [])
        hook.list_processes = lambda: "  123 /bin/zsh -c echo $L and 2> /dev/null\n"
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_ready_task_with_live_run_does_not_count(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
            ],
        )
        hook.get_ready_tasks = lambda ws: [
            {"id": "WS11.1", "title": "Do the thing", "live_run": "ws11-exec-x"}
        ]
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_live_unwatched_worker_refused(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
            ],
        )
        hook.list_processes = lambda: (
            "  123 python3 scripts/launch_codex.py ws11-plan-foo /repo /pad/x.md /runs\n"
            "  456 /usr/bin/python3 agent_dash.py serve\n"
        )
        code, err = hook.check_stop(json.loads(stop_payload(path)))
        self.assertEqual(code, 2)
        self.assertIn("ws11-plan-foo", err)

    def test_quiet_lead_allowed(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
            ],
        )
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_non_lead_stop_allowed(self):
        path = write_transcript(
            self.tmp.name, [au("toolu_1", "Bash", {"command": "ls"}), ures("toolu_1", "x")]
        )

        def boom(ws):
            raise AssertionError("ready lookup must not run for non-leads")

        hook.get_ready_tasks = boom
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_hub_only_skips_ready_lookup(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_h1", "Bash", {"command": HUB_LAUNCH}),
                ures("toolu_h1", "done"),
            ],
        )
        facts = hook.scan_transcript(path)
        self.assertTrue(hook.is_lead(facts, time.time()))
        self.assertEqual(hook.workstreams(hook.recent_labels(facts, time.time())), [])

        def boom(ws):
            raise AssertionError("ready lookup must not run for hub-only leads")

        hook.get_ready_tasks = boom
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_lookup_timeout_allowed(self):
        path = write_transcript(
            self.tmp.name,
            [
                au("toolu_l1", "Bash", {"command": MUSE_LAUNCH}),
                ures("toolu_l1", "done"),
            ],
        )

        def slow(ws):
            raise subprocess.TimeoutExpired(cmd="ws next", timeout=6)

        hook.get_ready_tasks = slow
        self.assertEqual(hook.check_stop(json.loads(stop_payload(path))), (0, ""))

    def test_bad_json_and_missing_transcript_allowed(self):
        for raw in ("", "not json", "[]"):
            code, _err = self.run_hook(raw)
            self.assertEqual(code, 0, raw)
        code, _err = self.run_hook(stop_payload("/nonexistent-dir/nope.jsonl"))
        self.assertEqual(code, 0)
        self.assertEqual(
            hook.check_stop(json.loads(stop_payload("/nonexistent-dir/nope.jsonl"))),
            (0, ""),
        )


class EnvOffTests(Base):
    def test_env_off_allows_ask(self):
        path = write_transcript(self.tmp.name, bg_launch())
        code, err = self.run_hook(
            ask_payload(path), env_extra={"AGENT_DASH_LEAD_GUARD": "off"}
        )
        self.assertEqual(code, 0, err)

    def test_env_off_allows_stop(self):
        path = write_transcript(self.tmp.name, bg_launch())
        code, err = self.run_hook(
            stop_payload(path), env_extra={"AGENT_DASH_LEAD_GUARD": "off"}
        )
        self.assertEqual(code, 0, err)


class InstallTests(unittest.TestCase):
    """The --claude-lead-guard installer against a fake HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text('{"model": "sonnet"}', encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home))

    def _install(self, *args):
        return subprocess.run(
            ["sh", str(ROOT / "scripts" / "install_hooks.sh"), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )

    def test_install_adds_both_entries_and_is_idempotent(self):
        for _ in range(2):
            proc = self._install("--claude-lead-guard")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "sonnet")
        pre = settings["hooks"]["PreToolUse"]
        ask = [item for item in pre if "lead_guard_hook.py" in json.dumps(item)]
        self.assertEqual(len(ask), 1)
        self.assertEqual(ask[0]["matcher"], "AskUserQuestion")
        self.assertEqual(ask[0]["hooks"][0]["timeout"], 10)
        command = ask[0]["hooks"][0]["command"]
        self.assertTrue(Path(command.split(" ", 1)[1]).is_file(), command)
        stop = settings["hooks"]["Stop"]
        shut = [item for item in stop if "lead_guard_hook.py" in json.dumps(item)]
        self.assertEqual(len(shut), 1)
        self.assertNotIn("matcher", shut[0])
        self.assertEqual(shut[0]["hooks"][0]["timeout"], 10)

    def test_uninstall_removes_lead_entries_only(self):
        self.assertEqual(self._install("--claude-label-guard").returncode, 0)
        self.assertEqual(self._install("--claude-lead-guard").returncode, 0)
        for _ in range(2):
            proc = self._install("--uninstall")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        blob = json.dumps(settings)
        self.assertNotIn("lead_guard_hook.py", blob)
        self.assertIn("label_guard_hook.py", blob)

    def test_help_names_flag(self):
        proc = self._install("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--claude-lead-guard", proc.stdout)


if __name__ == "__main__":
    unittest.main()
