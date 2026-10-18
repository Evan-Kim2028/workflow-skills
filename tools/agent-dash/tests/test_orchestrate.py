import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import orchestrate as orch
from dash.store import Store


def _body(tasks, trains=None, ws="WS9"):
    return {
        "ws": ws,
        "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": tasks}],
        "trains": trains or [],
    }


def _task(tid, status="todo", deps=None, **extra):
    row = {"id": tid, "title": f"Task {tid}", "status": status}
    if deps:
        row["deps"] = deps
    row.update(extra)
    return row


class ReadyQueueTest(unittest.TestCase):
    def test_ready_needs_all_deps_done(self):
        body = _body(
            [
                _task("WS9.1", "done"),
                _task("WS9.2", "todo", ["WS9.1"]),
                _task("WS9.3", "todo", ["WS9.2"]),
            ]
        )
        ready = orch.ready_queue(body)
        self.assertEqual([row["id"] for row in ready], ["WS9.2"])

    def test_prog_counts_as_satisfied(self):
        body = _body([_task("WS9.1", "prog"), _task("WS9.2", "todo", ["WS9.1"])])
        self.assertEqual([row["id"] for row in orch.ready_queue(body)], ["WS9.2"])

    def test_gate_blocks_like_a_dep(self):
        body = _body(
            [
                _task("WS9.0", "todo", kind="gate"),
                _task("WS9.1", "todo", ["WS9.0"]),
                _task("WS9.2", "todo"),
            ]
        )
        ready = orch.ready_queue(body)
        self.assertEqual([row["id"] for row in ready], ["WS9.0", "WS9.2"])

    def test_rank_is_chain_then_dependents(self):
        body = _body(
            [
                _task("WS9.root", "done"),
                _task("WS9.a", "todo", ["WS9.root"]),
                _task("WS9.b", "todo", ["WS9.a"]),
                _task("WS9.c", "todo", ["WS9.b"]),
                _task("WS9.side", "todo", ["WS9.root"]),
            ]
        )
        ready = orch.ready_queue(body)
        self.assertEqual([row["id"] for row in ready], ["WS9.a", "WS9.side"])
        self.assertEqual(ready[0]["chain"], 3)
        self.assertEqual(ready[0]["dependents"], 2)
        self.assertEqual(ready[0]["unblocks"], ["WS9.b"])
        self.assertEqual(ready[1]["chain"], 1)

    def test_train_and_live_pass_through(self):
        body = _body(
            [_task("WS9.1", "done"), _task("WS9.2", "todo", ["WS9.1"])],
            trains=[{"id": "T1", "members": ["WS9.2"]}],
        )
        ready = orch.ready_queue(body, {"WS9.2": "ws9-WS9.2-build"})
        self.assertEqual(ready[0]["train"], "T1")
        self.assertEqual(ready[0]["live_run"], "ws9-WS9.2-build")

    def test_stalled_lists_started_without_live(self):
        body = _body([_task("WS9.1", "wip"), _task("WS9.2", "pr"), _task("WS9.3", "todo")])
        stalled = orch.stalled_tasks(body, {"WS9.1": "ws9-WS9.1-x"})
        self.assertEqual([row["id"] for row in stalled], ["WS9.2"])


class CriticalPathTest(unittest.TestCase):
    def test_longest_chain_by_count(self):
        body = _body(
            [
                _task("WS9.1"),
                _task("WS9.2", deps=["WS9.1"]),
                _task("WS9.3", deps=["WS9.2"]),
                _task("WS9.9"),
            ]
        )
        self.assertEqual(orch.critical_path(body), ["WS9.1", "WS9.2", "WS9.3"])

    def test_longest_chain_by_duration(self):
        body = _body(
            [
                _task("WS9.1"),
                _task("WS9.2", deps=["WS9.1"]),
                _task("WS9.9"),
            ]
        )
        durations = {"WS9.1": 10.0, "WS9.2": 10.0, "WS9.9": 1000.0}
        self.assertEqual(orch.critical_path(body, durations), ["WS9.9"])
        self.assertEqual(orch.critical_path(body), ["WS9.1", "WS9.2"])

    def test_empty_plan(self):
        self.assertEqual(orch.critical_path({"groups": []}), [])
        self.assertEqual(orch.plan_extra({"groups": []}), {"ready": [], "critical_path": [], "max_parallel": 0})


class WavesTest(unittest.TestCase):
    def test_levels_without_files(self):
        body = _body(
            [
                _task("WS9.1"),
                _task("WS9.2", deps=["WS9.1"]),
                _task("WS9.3", deps=["WS9.1"]),
                _task("WS9.4", deps=["WS9.2", "WS9.3"]),
            ]
        )
        self.assertEqual(orch.waves(body), [["WS9.1"], ["WS9.2", "WS9.3"], ["WS9.4"]])
        self.assertEqual(orch.max_parallel(body), 2)

    def test_level_splits_on_shared_files(self):
        body = _body(
            [
                _task("WS9.1", files=["api/x.py"]),
                _task("WS9.2", files=["api/x.py"]),
                _task("WS9.3", files=["api/y.py"]),
            ]
        )
        wave_sets = orch.waves(body)
        self.assertEqual(len(wave_sets), 2)
        flat = [tid for wave in wave_sets for tid in wave]
        self.assertEqual(sorted(flat), ["WS9.1", "WS9.2", "WS9.3"])
        for wave in wave_sets:
            self.assertFalse("WS9.1" in wave and "WS9.2" in wave)

    def test_glob_collision(self):
        body = _body(
            [
                _task("WS9.1", files=["api/x.py"]),
                _task("WS9.2", files=["api/*.py"]),
            ]
        )
        self.assertEqual(len(orch.waves(body)), 2)

    def test_disjoint_wildcards_share_a_wave(self):
        self.assertFalse(orch.globs_overlap("a/*.py", "b/*.py"))
        self.assertTrue(orch.globs_overlap("api/*.py", "api/*.py"))
        self.assertTrue(orch.globs_overlap("api/x.py", "api/*.py"))


class OverlapsTest(unittest.TestCase):
    def test_declared_pairs(self):
        body = _body(
            [
                _task("WS9.1", "wip", files=["api/x.py"]),
                _task("WS9.2", "wip", files=["api/*.py"]),
                _task("WS9.3", "wip", files=["web/y.py"]),
            ]
        )
        pairs = orch.overlaps(body, ["WS9.1", "WS9.2", "WS9.3"])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["a"], "WS9.1")
        self.assertEqual(pairs[0]["b"], "WS9.2")
        self.assertEqual(pairs[0]["source"], "declared")
        self.assertTrue(pairs[0]["files"])

    def test_tasks_without_files_never_collide(self):
        body = _body([_task("WS9.1", "wip"), _task("WS9.2", "wip")])
        self.assertEqual(orch.overlaps(body), [])


class StaleGatesTest(unittest.TestCase):
    def test_stale_and_fresh(self):
        now = time.time()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 48 * 3600))
        new = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        body = _body(
            [
                _task("WS9.0", "todo", kind="gate", signal="the freeze lifts", confirmed_at=old, fresh_hours=24),
                _task("WS9.1", "todo", deps=["WS9.0"]),
                _task("WS9.2", "todo", kind="gate", signal="nightly", confirmed_at=new, fresh_hours=24),
            ]
        )
        gates = orch.stale_gates(body, now)
        by_id = {row["id"]: row for row in gates}
        self.assertTrue(by_id["WS9.0"]["stale"])
        self.assertFalse(by_id["WS9.2"]["stale"])
        self.assertEqual(by_id["WS9.0"]["blocked"], ["WS9.1"])
        self.assertEqual(by_id["WS9.0"]["signal"], "the freeze lifts")

    def test_missing_confirmation_is_stale(self):
        body = _body([_task("WS9.0", "todo", kind="gate", signal="the freeze lifts", fresh_hours=24)])
        self.assertTrue(orch.stale_gates(body, time.time())[0]["stale"])

    def test_no_bound_never_stale(self):
        body = _body([_task("WS9.0", "todo", kind="gate")])
        self.assertFalse(orch.stale_gates(body, time.time())[0]["stale"])


class DeferredLedgerTest(unittest.TestCase):
    def test_received_as_match(self):
        bodies = {
            "WS9": _body([_task("WS9.1", "deferred", moved_to="WS10", received_as="WS10.3")], ws="WS9"),
            "WS10": _body([_task("WS10.3", "todo")], ws="WS10"),
        }
        (row,) = orch.deferred_ledger(bodies)
        self.assertEqual(row["received"], "WS10.3")
        self.assertFalse(row["orphan"])

    def test_title_similarity_match(self):
        bodies = {
            "WS9": _body([_task("WS9.1", "deferred", moved_to="WS10")], ws="WS9"),
            "WS10": _body([_task("WS10.7", "todo")], ws="WS10"),
        }
        bodies["WS9"]["groups"][0]["tasks"][0]["title"] = "Retire the old readers"
        bodies["WS10"]["groups"][0]["tasks"][0]["title"] = "Retire the old readers"
        (row,) = orch.deferred_ledger(bodies)
        self.assertEqual(row["received"], "WS10.7")
        self.assertFalse(row["orphan"])

    def test_orphans_flagged(self):
        bodies = {
            "WS9": _body(
                [
                    _task("WS9.1", "deferred", moved_to="WS10"),
                    _task("WS9.2", "deferred"),
                ],
                ws="WS9",
            ),
            "WS10": _body([_task("WS10.9", "todo")], ws="WS10"),
        }
        bodies["WS9"]["groups"][0]["tasks"][0]["title"] = "Migrate the thing"
        rows = {row["id"]: row for row in orch.deferred_ledger(bodies)}
        self.assertTrue(rows["WS9.1"]["orphan"])
        self.assertIsNone(rows["WS9.1"]["received"])
        self.assertTrue(rows["WS9.2"]["orphan"])
        self.assertIsNone(rows["WS9.2"]["moved_to"])


class DepJumpTest(unittest.TestCase):
    def test_unfinished_dep_is_a_jump(self):
        body = _body([_task("WS9.1", "todo"), _task("WS9.2", "wip", ["WS9.1"])])
        jumps = orch.dep_jumps(body, {"WS9.2": 1000.0}, {})
        self.assertEqual(jumps, [{"task": "WS9.2", "dep": "WS9.1", "started_at": 1000.0, "dep_done_at": None}])

    def test_start_before_done_moment_is_a_jump(self):
        body = _body([_task("WS9.1", "done"), _task("WS9.2", "done", ["WS9.1"])])
        jumps = orch.dep_jumps(body, {"WS9.2": 1000.0}, {"WS9.1": 2000.0})
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0]["dep_done_at"], 2000.0)

    def test_done_dep_without_moment_is_skipped(self):
        body = _body([_task("WS9.1", "done"), _task("WS9.2", "wip", ["WS9.1"])])
        self.assertEqual(orch.dep_jumps(body, {"WS9.2": 1000.0}, {}), [])

    def test_healthy_order_is_clean(self):
        body = _body([_task("WS9.1", "done"), _task("WS9.2", "done", ["WS9.1"])])
        self.assertEqual(orch.dep_jumps(body, {"WS9.2": 2000.0}, {"WS9.1": 1000.0}), [])


class LabelRoundsTest(unittest.TestCase):
    def test_fix_and_rev_markers(self):
        self.assertEqual(orch.label_rounds("ws4-WS4.1-slug-fix2"), [("fix", 2)])
        self.assertEqual(orch.label_rounds("ws4-WS4.1-slug-rev1"), [("rev", 1)])
        self.assertEqual(orch.label_rounds("ws4-WS4.1-x-fix1-rev2"), [("fix", 1), ("rev", 2)])

    def test_plain_label_has_no_rounds(self):
        self.assertEqual(orch.label_rounds("ws4-WS4.1-notes-index"), [])
        self.assertEqual(orch.label_rounds(None), [])
        self.assertEqual(orch.label_rounds(""), [])

    def test_slug_words_without_numbers_do_not_count(self):
        self.assertEqual(orch.label_rounds("ws4-WS4.1-prefix-notes"), [])
        self.assertEqual(orch.label_rounds("ws4-WS4.1-revamp-fix"), [])

    def test_multi_digit_rounds(self):
        self.assertEqual(orch.label_rounds("ws4-WS4.1-x-rev10"), [("rev", 10)])

    def test_case_insensitive(self):
        self.assertEqual(orch.label_rounds("ws4-WS4.1-x-FIX1"), [("fix", 1)])


class LiveByTaskTest(unittest.TestCase):
    def test_newest_run_wins(self):
        runs = [
            {"run_id": "a", "label": "ws9-WS9.1-first", "workstream": "WS9", "status": "done", "started_at": 100.0},
            {"run_id": "b", "label": "ws9-WS9.1-second", "workstream": "WS9", "status": "running", "started_at": 200.0},
        ]
        self.assertEqual(orch.live_by_task(runs, "WS9", ["WS9.1"]), {"WS9.1": "ws9-WS9.1-second"})
        self.assertEqual(
            orch.live_by_task(runs, "WS9", ["WS9.1"], live_only=False),
            {"WS9.1": "ws9-WS9.1-second"},
        )

    def test_other_workstream_ignored(self):
        runs = [{"run_id": "a", "label": "ws9-WS9.1-x", "workstream": "WS10", "status": "running", "started_at": 1.0}]
        self.assertEqual(orch.live_by_task(runs, "WS9", ["WS9.1"]), {})

    def test_phantom_task_id_drops_to_unmapped(self):
        from dash import taskmatch

        ids = ["WS7.8"]
        self.assertEqual(taskmatch.task_of("ws7-ops-gold-rebuild", "WS7", {}, ids), "WS7.OPS")
        self.assertIsNone(taskmatch.task_of_verified("ws7-ops-gold-rebuild", "WS7", {}, ids))
        self.assertEqual(taskmatch.task_of_verified("ws7-WS7.8-x", "WS7", {}, ids), "WS7.8")
        self.assertEqual(
            taskmatch.task_of_verified("ws7-ops-gold-rebuild", "WS7", {}, []),
            "WS7.OPS",
        )
        runs = [
            {"run_id": "a", "label": "ws7-ops-gold-rebuild", "workstream": "WS7", "status": "running", "started_at": 100.0},
            {"run_id": "b", "label": "ws7-WS7.8-x", "workstream": "WS7", "status": "running", "started_at": 200.0},
        ]
        self.assertEqual(orch.live_by_task(runs, "WS7", ids), {"WS7.8": "ws7-WS7.8-x"})


def _seed_profile_db(path: Path) -> float:
    """Synthetic dashboard: one lead, a subagent child, a Bash-launched
    manifest-style worker with no parent, and a workflow wrapper child."""
    store = Store(path)
    base = 1_700_000_000.0
    store.upsert_session(
        {
            "session_id": "lead-1",
            "tool": "muse",
            "label": "ws9-lead",
            "started_at": base,
            "status": "running",
        }
    )
    store.upsert_run(
        {
            "run_id": "claude-subagent:child-1:ws9-WS9.1-build",
            "source": "claude-subagent",
            "label": "ws9-WS9.1-build",
            "session_id": "child-1",
            "parent_session_id": "lead-1",
            "status": "done",
            "started_at": base + 10,
            "ended_at": base + 70,
        }
    )
    store.upsert_run(
        {
            "run_id": "muse:bash-1:ws9-WS9.2-build",
            "source": "muse",
            "model": "muse-spark-1.3-contributor",
            "label": "ws9-WS9.2-build",
            "session_id": "bash-1",
            "status": "done",
            "started_at": base + 20,
            "ended_at": base + 50,
        }
    )
    store.upsert_run(
        {
            "run_id": "claude-workflow:wf-1:wf_abc123",
            "source": "claude-workflow",
            "label": "ws9-WS9.3-wave",
            "session_id": "wf-1",
            "parent_session_id": "lead-1",
            "status": "done",
            "started_at": base + 30,
            "ended_at": base + 90,
        }
    )
    store.upsert_run(
        {
            "run_id": "muse:late-1:ws9-WS9.9-late",
            "source": "muse",
            "label": "ws9-WS9.9-late",
            "session_id": "late-1",
            "status": "done",
            "started_at": base + 10_000,
            "ended_at": base + 10_060,
        }
    )
    store.close()
    return base


class LeadProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dash.sqlite"
        self.base = _seed_profile_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_every_worker_a_lead_launched(self):
        store = Store(self.db)
        try:
            profile = orch.lead_profile(store, session_id="lead-1", now=self.base + 500)
        finally:
            store.close()
        self.assertIsNotNone(profile)
        # parent-linked subagent + wrapper; the Bash-launched manifest row
        # joins only in --ws mode (no parent recorded there).
        self.assertEqual(profile["workers_launched"], 2)
        self.assertEqual(profile["ws"], "WS9")
        self.assertEqual(profile["peak_concurrency"], 2)
        self.assertNotIn("LOW-DELEGATION", profile["flags"])

    def test_ws_mode_adds_bash_launched_workers(self):
        store = Store(self.db)
        try:
            profile = orch.lead_profile(store, ws="WS9", now=self.base + 500)
        finally:
            store.close()
        self.assertEqual(profile["workers_launched"], 3)
        self.assertEqual(profile["by_source"].get("muse"), 1)
        self.assertEqual(profile["by_source"].get("claude-subagent"), 1)
        self.assertEqual(profile["by_source"].get("claude-workflow"), 1)

    def test_lone_lead_is_low_delegation(self):
        store = Store(self.db)
        try:
            store.upsert_session(
                {
                    "session_id": "lead-9",
                    "tool": "muse",
                    "label": "ws9-lonely",
                    "started_at": self.base,
                    "status": "running",
                }
            )
            profile = orch.lead_profile(store, session_id="lead-9", now=self.base + 500)
        finally:
            store.close()
        self.assertEqual(profile["workers_launched"], 0)
        self.assertIn("LOW-DELEGATION", profile["flags"])
        self.assertEqual(profile["mean_concurrency"], 0.0)
        self.assertEqual(profile["peak_concurrency"], 0)

    def test_unknown_session_is_none(self):
        store = Store(self.db)
        try:
            self.assertIsNone(orch.lead_profile(store, session_id="nope", now=self.base))
            self.assertIsNone(orch.lead_profile(store, ws="WS99", now=self.base))
            self.assertIsNone(orch.lead_profile(store, now=self.base))
        finally:
            store.close()


class LeadProfileCliTest(unittest.TestCase):
    def test_lead_profile_json_over_cli(self):
        import contextlib
        import io
        import json as _json

        import agent_dash

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "dash.sqlite")
            _seed_profile_db(Path(db))
            old = os.environ.get("AGENT_DASH_DB")
            os.environ["AGENT_DASH_DB"] = db
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = agent_dash.main(["lead-profile", "--ws", "WS9", "--json"])
                self.assertEqual(code, 0)
                payload = _json.loads(buf.getvalue())
                self.assertEqual(payload["ws"], "WS9")
                self.assertEqual(payload["workers_launched"], 3)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = agent_dash.main(["lead-profile", "nope", "--json"])
                self.assertEqual(code, 3)
                self.assertEqual(agent_dash.main(["lead-profile"]), 2)
            finally:
                if old is None:
                    os.environ.pop("AGENT_DASH_DB", None)
                else:
                    os.environ["AGENT_DASH_DB"] = old


class WorktreeGitTest(unittest.TestCase):
    def test_enclosing_worktree_none_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(orch.enclosing_worktree(Path(tmp) / "muse" / "out-x.md"))

    def test_branch_files_from_temp_repo(self):
        import shutil
        import subprocess

        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = lambda *args: subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
            run("init", "-b", "main")
            run("config", "user.email", "test@example.com")
            run("config", "user.name", "Test")
            (root / "a.py").write_text("x = 1\n")
            run("add", "a.py")
            run("commit", "-m", "base")
            run("checkout", "-b", "train/demo")
            (root / "b.py").write_text("y = 2\n")
            run("add", "b.py")
            run("commit", "-m", "branch work")
            files = orch.branch_files(root, "train/demo")
            self.assertEqual(files, ["b.py"])
            self.assertIsNone(orch.branch_files(root, "train/missing"))
            self.assertEqual(orch.enclosing_worktree(root / "muse" / "out-x.md"), root)
