"""Workstream dispatcher: loop logic, state, inbox, wait cursor, pidfile lock.

Fake launcher and liveness only. No worker is ever started.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash import dispatch  # noqa: E402

WS = "WS9"
LABEL_A = "ws9-exec-a"
LABEL_B = "ws9-exec-b"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeWorker:
    """Stands in for run_worker.py: launches record argv, observes follow scripted reports."""

    def __init__(self):
        self.argvs = []
        self.alive_pids = set()
        self.pids = {}
        self.reports = {}
        self.next_pid = 5000

    def __call__(self, argv):
        self.argvs.append(list(argv))
        label = argv[3]
        run_dir = Path(argv[6])
        tier_file = run_dir / f"{label}.tier"
        out = run_dir / f"out-{label}.md"
        err = run_dir / f"err-{label}.log"
        if not tier_file.exists():
            run_dir.mkdir(parents=True, exist_ok=True)
            tier = "glm-flash" if "--exclude=muse" in argv else "muse"
            self.next_pid += 1
            pid = self.next_pid
            self.pids[label] = pid
            self.alive_pids.add(pid)
            tier_file.write_text(tier + "\n", encoding="utf-8")
            (run_dir / f"{label}.pid").write_text(f"{pid}\n", encoding="utf-8")
            return f"STATUS: running out={out} err={err} tier={tier}"
        tier = tier_file.read_text(encoding="utf-8").strip()
        pid = int((run_dir / f"{label}.pid").read_text(encoding="utf-8").strip())
        if pid in self.alive_pids:
            return f"STATUS: running out={out} err={err} tier={tier}"
        report = self.reports.get(label, "PASS\nok\n")
        out.write_text(report, encoding="utf-8")
        err.write_text("", encoding="utf-8")
        if report.strip():
            return f"STATUS: done out={out} err={err} tier={tier}"
        return f"STATUS: failed out={out} err={err} tier={tier} reason=empty-report"

    def finish(self, label, report=None):
        if report is not None:
            self.reports[label] = report
        self.alive_pids.discard(self.pids[label])

    def launches_for(self, label):
        return [argv for argv in self.argvs if argv[3] == label]


def plan_body(statuses=None, deps=None):
    statuses = statuses or {}
    deps = deps or {}
    tasks = []
    for tid in ("WS9.A", "WS9.B", "WS9.C"):
        tasks.append(
            {"id": tid, "title": f"Task {tid}", "status": statuses.get(tid, "todo"), "deps": deps.get(tid, [])}
        )
    return {"dag": {"groups": [{"id": "G1", "tasks": tasks}]}}


class DispatchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundles" / WS
        self.bundle.mkdir(parents=True)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.env = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root / "bundles")
        self.clock = Clock()
        self.worker = FakeWorker()
        self.write_plan(plan_body())
        self.write_config(slots=2)
        for tid in ("WS9.A", "WS9.B", "WS9.C"):
            self.write_prompt(tid)

    def tearDown(self):
        if self.env is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.env
        self.tmp.cleanup()

    def write_plan(self, body):
        (self.bundle / "plan.json").write_text(json.dumps(body), encoding="utf-8")

    def read_plan(self):
        return json.loads((self.bundle / "plan.json").read_text(encoding="utf-8"))

    def status_of(self, tid):
        for task in self.read_plan()["dag"]["groups"][0]["tasks"]:
            if task["id"] == tid:
                return task["status"]
        return None

    def write_config(self, **changes):
        cfg = {"slots": 2, "tier": "auto", "workspace": str(self.workspace)}
        cfg.update(changes)
        (self.bundle / "dispatch.json").write_text(json.dumps(cfg), encoding="utf-8")

    def write_prompt(self, tid, front=""):
        prompts = self.bundle / "prompts"
        prompts.mkdir(exist_ok=True)
        (prompts / f"{tid}.md").write_text(front + f"do {tid}\n", encoding="utf-8")

    def set_status(self, tid, status, expect=None):
        plan = self.read_plan()
        for task in plan["dag"]["groups"][0]["tasks"]:
            if task["id"] == tid:
                if expect is not None and task["status"] not in expect:
                    return False
                task["status"] = status
        self.write_plan(plan)
        return True

    def dispatcher(self, launcher=None):
        return dispatch.Dispatcher(
            WS,
            self.bundle,
            launcher=launcher or self.worker,
            alive=lambda pid: pid in self.worker.alive_pids,
            runs=lambda: [],
            set_status=self.set_status,
            load_body=self.read_plan,
            now=self.clock,
        )

    def events(self, after=0):
        return dispatch.read_events(self.bundle, after)

    def types(self):
        return [event["type"] for event in self.events()]


class LaunchTests(DispatchTestCase):
    def test_launches_up_to_slots_and_marks_wip(self):
        disp = self.dispatcher()
        result = disp.tick()
        self.assertEqual(result["launched"], 2)
        self.assertEqual(self.status_of("WS9.A"), "wip")
        self.assertEqual(self.status_of("WS9.B"), "wip")
        self.assertEqual(self.status_of("WS9.C"), "todo")
        argv = self.worker.launches_for(LABEL_A)[0]
        self.assertEqual(argv[2], "auto")
        self.assertEqual(argv[7], "0", "max-wait 0 means launch and return")
        second = disp.tick()
        self.assertEqual(second["launched"], 0, "no slot is free while both workers live")
        self.assertEqual(len(self.worker.argvs), 2)
        self.assertEqual(self.types().count("launched"), 2)

    def test_needs_prompt_fires_once_then_launches_when_written(self):
        (self.bundle / "prompts" / "WS9.C.md").unlink()
        self.write_config(slots=3)
        disp = self.dispatcher()
        for _ in range(3):
            disp.tick()
        self.assertEqual([e["task"] for e in self.events() if e["type"] == "needs-prompt"], ["WS9.C"])
        self.assertEqual(self.status_of("WS9.C"), "todo")
        self.write_prompt("WS9.C")
        disp.tick()
        self.assertEqual(self.status_of("WS9.C"), "wip")
        self.assertEqual(len(self.worker.launches_for("ws9-exec-c")), 1)

    def test_restart_adopts_live_runs_without_relaunch(self):
        self.write_config(slots=3)
        self.dispatcher().tick()
        self.assertEqual(len(self.worker.argvs), 3)
        restarted = self.dispatcher()
        self.assertEqual(set(restarted.tracked_running()), {"WS9.A", "WS9.B", "WS9.C"})
        self.assertEqual(restarted.tick()["launched"], 0)
        self.assertEqual(len(self.worker.argvs), 3, "adopted runs are never relaunched")
        self.assertEqual(len(self.worker.launches_for(LABEL_A)), 1)

    def test_capacity_and_failed_tier_cap_counts_only_our_slots(self):
        self.write_config(slots=1)
        disp = self.dispatcher()
        self.assertEqual(disp.tick()["launched"], 1)
        self.assertEqual(disp.tick()["launched"], 0)

    def test_status_write_failure_keeps_the_run_tracked(self):
        def broken(task_id, status):
            raise RuntimeError("disk full")

        disp = dispatch.Dispatcher(
            WS,
            self.bundle,
            launcher=self.worker,
            alive=lambda pid: pid in self.worker.alive_pids,
            runs=lambda: [],
            set_status=broken,
            load_body=self.read_plan,
            now=self.clock,
        )
        disp.tick()
        self.assertIn("WS9.A", disp.tracked_running())
        disp.tick()
        self.assertEqual(len(self.worker.launches_for(LABEL_A)), 1, "a tracked run is never launched twice")
        errors = [e for e in self.events() if e["type"] == "error" and e.get("phase") == "status"]
        self.assertEqual({e["task"] for e in errors}, {"WS9.A", "WS9.B"})

    def test_none_available_stops_the_launch_pass_quietly(self):
        def refuse(argv):
            return "STATUS: failed out=x err=y tier=auto reason=none-available"

        disp = self.dispatcher(launcher=refuse)
        result = disp.tick()
        self.assertEqual(result["launched"], 0)
        self.assertEqual(self.types(), [])

    def test_gate_rows_are_never_launched(self):
        plan = self.read_plan()
        plan["dag"]["groups"][0]["tasks"][0]["kind"] = "gate"
        self.write_plan(plan)
        self.dispatcher().tick()
        self.assertEqual(self.status_of("WS9.A"), "todo")
        self.assertEqual(len(self.worker.launches_for(LABEL_A)), 0)


class OutcomeTests(DispatchTestCase):
    def test_pass_write_moves_to_pr_and_pass_read_to_done(self):
        self.write_prompt("WS9.B", front="---\nkind: read\n---\n")
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nbuilt it\n")
        self.worker.finish(LABEL_B, "**PASS** read it\n")
        disp.tick()
        self.assertEqual(self.status_of("WS9.A"), "pr")
        self.assertEqual(self.status_of("WS9.B"), "done")
        finished = [e for e in self.events() if e["type"] == "finished"]
        self.assertEqual({e["task"]: e["verdict"] for e in finished}, {"WS9.A": "PASS", "WS9.B": "PASS"})
        self.assertNotIn("needs-lead", self.types())

    def test_partial_stays_wip_and_asks_for_the_lead(self):
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "PARTIAL\nlane 2 still red\n")
        disp.tick()
        self.assertEqual(self.status_of("WS9.A"), "wip")
        lead = [e for e in self.events() if e["type"] == "needs-lead"]
        self.assertEqual([(e["task"], e["reason"]) for e in lead], [("WS9.A", "PARTIAL")])

    def test_no_verdict_retries_once_on_another_tier_then_needs_lead(self):
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "")
        disp.tick()
        retry_label = "ws9-exec-a-r1"
        retry = self.worker.launches_for(retry_label)
        self.assertEqual(len(retry), 1)
        self.assertIn("--exclude=muse", retry[0])
        self.assertEqual(retry[0][2], "auto")
        self.assertIn("retried", self.types())
        retried = [e for e in self.events() if e["type"] == "retried"][0]
        self.assertEqual((retried["from_tier"], retried["to_tier"]), ("muse", "glm-flash"))
        rows = (self.bundle / "reports" / "decisions.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 2, "header plus one retry row")
        self.worker.finish(retry_label, "")
        disp.tick()
        launched = [e["label"] for e in self.events() if e["type"] == "launched"]
        self.assertEqual(launched.count(retry_label), 1, "the retry is launched once, never a third time")
        lead = [e for e in self.events() if e["type"] == "needs-lead"]
        self.assertEqual([(e["task"], e["reason"]) for e in lead], [("WS9.A", "no-verdict-twice")])
        self.assertEqual(self.types().count("finished"), 2)

    def test_overdue_run_flags_the_lead_once_and_is_not_killed(self):
        self.write_prompt("WS9.A", front="---\nmax_wait: 10\n---\n")
        disp = self.dispatcher()
        disp.tick()
        self.clock.advance(20)
        disp.tick()
        disp.tick()
        overdue = [e for e in self.events() if e.get("reason") == "overdue"]
        self.assertEqual(len(overdue), 1)
        self.assertIn(self.worker.pids[LABEL_A], self.worker.alive_pids)

    def test_one_task_error_is_logged_and_the_loop_goes_on(self):
        self.write_prompt("WS9.A", front="---\nkind: sideways\n---\n")
        disp = self.dispatcher()
        disp.tick()
        disp.tick()
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["task"], "WS9.A")
        self.assertEqual(len(self.worker.launches_for(LABEL_B)), 1)
        self.assertEqual(self.status_of("WS9.B"), "wip")


class MergeAndIdleTests(DispatchTestCase):
    def test_merge_wave_fires_at_min_ready_once(self):
        self.write_config(slots=2, merge_wave={"min_ready": 2, "max_age_min": 60})
        self.write_plan(plan_body({"WS9.A": "pr", "WS9.B": "pr"}))
        disp = self.dispatcher()
        disp.tick()
        disp.tick()
        waves = [e for e in self.events() if e["type"] == "merge-wave"]
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0]["tasks"], ["WS9.A", "WS9.B"])

    def test_merge_wave_fires_on_oldest_age(self):
        self.write_config(slots=2, merge_wave={"min_ready": 3, "max_age_min": 1})
        self.write_plan(plan_body({"WS9.A": "pr"}))
        disp = self.dispatcher()
        disp.tick()
        self.assertEqual([e for e in self.events() if e["type"] == "merge-wave"], [])
        self.clock.advance(61)
        disp.tick()
        waves = [e for e in self.events() if e["type"] == "merge-wave"]
        self.assertEqual([(w["tasks"], w["reason"]) for w in waves], [(["WS9.A"], "age")])

    def test_idle_emits_once_when_only_blocked_work_remains(self):
        self.write_plan(plan_body({"WS9.A": "wip", "WS9.C": "done"}, deps={"WS9.B": ["WS9.A"]}))
        self.write_config(slots=2)
        disp = self.dispatcher()
        disp.tick()
        disp.tick()
        idle = [e for e in self.events() if e["type"] == "idle"]
        self.assertEqual(len(idle), 1)
        self.assertIn("WS9.B", idle[0]["todo"])


class StateAndWaitTests(DispatchTestCase):
    def test_dry_run_changes_nothing(self):
        disp = self.dispatcher()
        result = disp.tick(dry_run=True)
        self.assertEqual(result["launched"], 2)
        kinds = [e["type"] for e in result["events"]]
        self.assertEqual(kinds, ["would-launch", "would-launch"])
        self.assertEqual(self.worker.argvs, [])
        self.assertEqual(self.status_of("WS9.A"), "todo")
        self.assertFalse((self.bundle / "inbox.jsonl").exists())
        self.assertFalse((self.bundle / "dispatch-state.json").exists())

    def test_paused_holds_launches_until_resumed(self):
        self.write_config(slots=2, paused=True)
        disp = self.dispatcher()
        self.assertEqual(disp.tick()["launched"], 0)
        self.write_config(slots=2, paused=False)
        self.assertEqual(disp.tick()["launched"], 2)

    def test_bad_config_pauses_and_reports_once(self):
        (self.bundle / "dispatch.json").write_text("{not json", encoding="utf-8")
        disp = self.dispatcher()
        disp.tick()
        disp.tick()
        self.assertEqual(self.types(), ["error"])
        self.assertEqual(self.worker.argvs, [])

    def test_state_is_written_atomically_with_heartbeat(self):
        self.dispatcher().tick()
        state = json.loads((self.bundle / "dispatch-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["heartbeat"], self.clock())
        self.assertEqual(state["tasks"]["WS9.A"]["status"], "running")
        self.assertFalse((self.bundle / "dispatch-state.json.tmp").exists())

    def test_wait_returns_events_past_the_cursor_and_advances_it(self):
        disp = self.dispatcher()
        self.assertEqual(dispatch.wait_for_events(self.bundle, timeout=0, sleep=lambda s: None)[0], 3)
        disp.emit("idle", todo=["WS9.B"])
        disp.emit("idle", todo=["WS9.C"])
        code, events = dispatch.wait_for_events(self.bundle, timeout=0, sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertEqual([e["seq"] for e in events], [1, 2])
        self.assertEqual(dispatch.cursor_of(self.bundle), 2)
        self.assertEqual(dispatch.wait_for_events(self.bundle, timeout=0, sleep=lambda s: None)[0], 3)
        disp.emit("stopped")
        code, events = dispatch.wait_for_events(self.bundle, timeout=0, sleep=lambda s: None)
        self.assertEqual([e["type"] for e in events], ["stopped"])
        code, events = dispatch.wait_for_events(self.bundle, after=0, timeout=0, sleep=lambda s: None)
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])

    def test_seq_survives_a_restart(self):
        self.dispatcher().emit("idle", todo=["WS9.A"])
        self.assertEqual(self.dispatcher().emit("stopped")["seq"], 2)

    def test_pidfile_lock_allows_one_dispatcher(self):
        first = dispatch.Lock(self.bundle)
        second = dispatch.Lock(self.bundle)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()


class VerdictAndParseTests(unittest.TestCase):
    def test_verdict_reads_the_first_non_empty_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.md"
            cases = {
                "PASS\nrest": "PASS",
                "\n\n**PARTIAL** lane\n": "PARTIAL",
                "BLOCKED: no key": "BLOCKED",
                "Passed the suite\nPASS": "NONE",
                "": "NONE",
            }
            for text, want in cases.items():
                path.write_text(text, encoding="utf-8")
                self.assertEqual(dispatch.verdict_of(path), want, text)
            self.assertEqual(dispatch.verdict_of(Path(tmp) / "missing.md"), "NONE")

    def test_status_line_parse(self):
        got = dispatch.parse_status("STATUS: failed out=/a/b out-x.md err=/a/err-x.log tier=muse reason=quota")
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["tier"], "muse")
        self.assertEqual(got["reason"], "quota")
        self.assertEqual(got["out"], "/a/b out-x.md")

    def test_label_uses_round_suffix_on_relaunch(self):
        self.assertEqual(dispatch.build_label("WS9", "WS9.A", 0), LABEL_A)
        self.assertEqual(dispatch.build_label("WS9", "WS9.A", 1), "ws9-exec-a-r1")


class FormatTests(unittest.TestCase):
    def test_format_event_is_one_compact_line(self):
        line = dispatch.format_event(
            {"seq": 4, "type": "finished", "task": "WS9.A", "label": "ws9-exec-a", "verdict": "PASS", "tier": None}
        )
        self.assertEqual(line, "#4 finished WS9.A ws9-exec-a verdict=PASS")


class Fatal(BaseException):
    """Escapes the loop's ``except Exception`` the way a crash in the loop itself would."""


class ReviewFixTests(DispatchTestCase):
    def build(self, runs=lambda: [], set_status=None, launcher=None):
        return dispatch.Dispatcher(
            WS,
            self.bundle,
            launcher=launcher or self.worker,
            alive=lambda pid: pid in self.worker.alive_pids,
            runs=runs,
            set_status=set_status or self.set_status,
            load_body=self.read_plan,
            now=self.clock,
        )

    def test_once_refuses_to_tick_while_the_daemon_holds_the_lock(self):
        lock = dispatch.Lock(self.bundle)
        self.assertTrue(lock.acquire())
        calls = []
        real = dispatch.default_launcher
        dispatch.default_launcher = lambda argv: calls.append(list(argv)) or "STATUS: running out=x err=y tier=muse"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = dispatch.cmd_dispatch(["once", WS])
        finally:
            dispatch.default_launcher = real
            lock.release()
        self.assertEqual(code, 4)
        self.assertEqual(calls, [], "a second dispatcher must not launch while the daemon owns the workstream")

    def test_dashboard_unreachable_holds_launches_because_live_runs_are_unknown(self):
        disp = self.build(runs=lambda: None)
        result = disp.tick()
        self.assertEqual(result["launched"], 0)
        self.assertEqual(self.worker.argvs, [], "unknown live runs must not be treated as none")
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual([e["phase"] for e in errors], ["dashboard"])

    def test_pass_moves_the_task_even_when_its_wip_write_failed(self):
        def flaky(tid, status, expect=None):
            if status == "wip":
                raise RuntimeError("bundle busy")
            self.set_status(tid, status)

        disp = self.build(set_status=flaky)
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()
        disp.tick()
        self.assertEqual(self.status_of("WS9.A"), "pr")
        launched = [e["label"] for e in self.events() if e["type"] == "launched" and e["task"] == "WS9.A"]
        self.assertEqual(launched, [LABEL_A], "a passed task is never relaunched")

    def test_stale_run_dir_from_lost_state_is_not_observed_as_this_round(self):
        run_dir = self.bundle / "runs" / LABEL_A
        run_dir.mkdir(parents=True)
        (run_dir / f"{LABEL_A}.tier").write_text("muse\n", encoding="utf-8")
        (run_dir / f"{LABEL_A}.pid").write_text("4999\n", encoding="utf-8")
        (run_dir / f"out-{LABEL_A}.md").write_text("PASS\nold round\n", encoding="utf-8")
        self.build().tick()
        self.assertEqual(self.worker.launches_for(LABEL_A), [], "the old round's report must not be reused")
        self.assertEqual(len(self.worker.launches_for("ws9-exec-a-r1")), 1)
        self.assertEqual(self.status_of("WS9.A"), "wip")

    def test_live_run_dir_from_lost_state_is_adopted_not_relaunched(self):
        run_dir = self.bundle / "runs" / LABEL_A
        run_dir.mkdir(parents=True)
        (run_dir / f"{LABEL_A}.tier").write_text("muse\n", encoding="utf-8")
        (run_dir / f"{LABEL_A}.pid").write_text("5001\n", encoding="utf-8")
        self.worker.alive_pids.add(5001)
        self.build().tick()
        self.assertEqual(self.worker.launches_for(LABEL_A), [])
        self.assertEqual(self.worker.launches_for("ws9-exec-a-r1"), [], "the live run is adopted, not doubled")
        self.assertEqual(self.status_of("WS9.A"), "wip")

    def test_torn_inbox_line_does_not_swallow_the_next_event(self):
        self.bundle.mkdir(parents=True, exist_ok=True)
        (self.bundle / "inbox.jsonl").write_text(
            '{"seq": 1, "type": "idle", "task": null, "label": null}\n{"seq": 2, "type": "lau', encoding="utf-8"
        )
        self.build().emit("stopped")
        self.assertEqual([e["type"] for e in self.events(after=1)], ["stopped"])

    def test_wait_without_a_timeout_gives_up_after_the_default(self):
        self.assertEqual(dispatch.WAIT_TIMEOUT_S, 1800)
        clock = Clock()
        code, events = dispatch.wait_for_events(self.bundle, after=0, clock=clock, sleep=clock.advance)
        self.assertEqual((code, events), (3, []))
        self.assertGreaterEqual(clock.t - 1_000_000.0, dispatch.WAIT_TIMEOUT_S)
        self.assertLess(clock.t - 1_000_000.0, dispatch.WAIT_TIMEOUT_S + 2)

    def test_a_crash_outside_the_task_loop_still_leaves_error_and_stopped_events(self):
        disp = self.build()

        def crash(*args, **kwargs):
            raise Fatal("loop died")

        disp.tick = crash
        lock = dispatch.Lock(self.bundle)
        self.assertTrue(lock.acquire())
        with self.assertRaises(Fatal):
            disp.loop(lock, poll_s=0)
        types = self.types()
        self.assertIn("error", types)
        self.assertEqual(types[-1], "stopped")
        self.assertTrue(lock.acquire(), "the lock is released after a crash")
        lock.release()

    def test_retry_waits_for_a_fixed_config_when_dispatch_json_breaks_mid_run(self):
        self.build().tick()
        (self.bundle / "dispatch.json").write_text("{not json", encoding="utf-8")
        self.worker.finish(LABEL_A, "")
        self.build().tick()
        self.assertEqual(self.worker.launches_for("ws9-exec-a-r1"), [], "a broken config holds the retry")
        self.assertEqual(self.build().status_summary()["tasks"]["WS9.A"]["status"], "retry-pending")
        self.assertNotIn("needs-lead", self.types(), "the task is not stranded or handed to the lead")
        self.write_config(slots=2)
        self.build().tick()
        self.assertEqual(len(self.worker.launches_for("ws9-exec-a-r1")), 1, "the retry goes out on the stored workspace")

    def test_a_state_file_that_is_not_an_object_does_not_stop_startup(self):
        (self.bundle / "dispatch-state.json").write_text("[1]\n", encoding="utf-8")
        disp = self.build()
        disp.tick()
        self.assertEqual(self.status_of("WS9.A"), "wip", "startup survives a damaged state file and launches")

    def test_a_retry_keeps_its_exclusion_on_every_observation(self):
        disp = self.build()
        disp.tick()
        self.worker.finish(LABEL_A, "")
        disp.tick()
        retry_label = "ws9-exec-a-r1"
        self.worker.finish(retry_label, "")
        self.build().tick()
        argvs = self.worker.launches_for(retry_label)
        self.assertGreaterEqual(len(argvs), 2, "one launch and one observation")
        for argv in argvs:
            self.assertIn("--exclude=muse", argv)

    def test_a_reused_pid_is_not_taken_for_the_dispatcher(self):
        (self.bundle / "dispatch.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.assertIsNone(dispatch.live_pid(self.bundle))
        killed = []
        real = dispatch.os.kill
        dispatch.os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                dispatch._stop(WS, self.bundle)
        finally:
            dispatch.os.kill = real
        self.assertEqual([sig for _pid, sig in killed if sig != 0], [], "stop never signals a process that is not this workstream's dispatcher")

    def test_expected_status_guards_the_plan_write(self):
        seen = []

        def record(tid, status, expect=None):
            seen.append((tid, status, expect))
            return True

        disp = self.build(set_status=record)
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()
        self.assertIn(("WS9.A", "pr", ("wip", "todo")), seen)

    def test_verdict_accepts_a_verdict_label_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.md"
            path.write_text("Verdict: PASS\nall good\n", encoding="utf-8")
            self.assertEqual(dispatch.verdict_of(path), "PASS")
            path.write_text("Verdict: nothing useful\n", encoding="utf-8")
            self.assertEqual(dispatch.verdict_of(path), "NONE")


if __name__ == "__main__":
    unittest.main()
