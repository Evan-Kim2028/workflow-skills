"""Control plane end to end: lease, launch, PASS, probe, receipt, pr, flow.

One test walks the whole chain a contract task takes through the dispatcher:
the file lease is claimed, the fake worker reports PASS, the acceptance probe
runs against the committed tree, a matching receipt is written, the task moves
to ``pr`` and the flow projection shows it there. The rest prove the refusals
that keep a claim from becoming a merge: a second task on the same file, a
failed probe, a dirty tree and an amended tree.

Every test uses a temp state dir, a temp bundle and a temp git repo. The
launcher is a fake that writes a report file; no worker, server or live state
is touched.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash import dispatch, evidence, flowview, plans  # noqa: E402
from dash.admission import LeaseStore  # noqa: E402

WS = "WS35"
LABEL_A = "ws35-exec-a"
LABEL_B = "ws35-exec-b"
PROBE = f"{sys.executable} -c 'pass'"
MARKER = "probe-output-marker"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeLauncher:
    """Stands in for run_worker.py: records argv, writes the report on observation."""

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
            self.next_pid += 1
            pid = self.next_pid
            self.pids[label] = pid
            self.alive_pids.add(pid)
            tier_file.write_text("muse\n", encoding="utf-8")
            (run_dir / f"{label}.pid").write_text(f"{pid}\n", encoding="utf-8")
            return f"STATUS: running out={out} err={err} tier=muse"
        pid = int((run_dir / f"{label}.pid").read_text(encoding="utf-8").strip())
        if pid in self.alive_pids:
            return f"STATUS: running out={out} err={err} tier=muse"
        report = self.reports.get(label, "PASS\nok\n")
        out.write_text(report, encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return f"STATUS: done out={out} err={err} tier=muse"

    def finish(self, label, report=None):
        if report is not None:
            self.reports[label] = report
        self.alive_pids.discard(self.pids[label])

    def launches_for(self, label):
        return [argv for argv in self.argvs if argv[3] == label]


def task(tid, status="todo", **extra):
    row = {"id": tid, "title": f"Task {tid}", "status": status, "deps": []}
    row.update(extra)
    return row


def contract(files, probe=PROBE, cwd=None):
    """The version-1 contract fields for one task."""
    acceptance = {"metric": "tests", "threshold": "pass", "tolerance": "0", "probe": probe}
    if cwd:
        acceptance["cwd"] = cwd
    return {"contract_version": 1, "acceptance": acceptance, "files": files}


class ControlPlaneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundles" / WS
        self.bundle.mkdir(parents=True)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.state_dir = self.root / "state"
        self.state_env = os.environ.get("AGENT_DASH_STATE_DIR")
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state_dir)
        self.clock = Clock()
        self.worker = FakeLauncher()
        self.git_workspace()
        self.write_config(slots=2)
        for tid in ("WS35.A", "WS35.B"):
            self.write_prompt(tid)

    def tearDown(self):
        if self.state_env is None:
            os.environ.pop("AGENT_DASH_STATE_DIR", None)
        else:
            os.environ["AGENT_DASH_STATE_DIR"] = self.state_env
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.workspace), *args], capture_output=True, text=True, check=True
        )

    def git_workspace(self):
        """A clean git repo with one commit, so a probe has a tree to bind to."""
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "mod.py").write_text("one\n", encoding="utf-8")
        self.git("add", "src/mod.py")
        self.git("commit", "-q", "-m", "one")

    def write_plan(self, tasks):
        body = {"dag": {"groups": [{"id": "G1", "tasks": tasks}]}}
        (self.bundle / "plan.json").write_text(json.dumps(body), encoding="utf-8")

    def read_plan(self):
        return json.loads((self.bundle / "plan.json").read_text(encoding="utf-8"))

    def plan_task(self, tid):
        for row in self.read_plan()["dag"]["groups"][0]["tasks"]:
            if row["id"] == tid:
                return row
        raise AssertionError(f"no task {tid}")

    def status_of(self, tid):
        return self.plan_task(tid)["status"]

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
        for row in plan["dag"]["groups"][0]["tasks"]:
            if row["id"] != tid:
                continue
            if expect is not None and row["status"] not in expect:
                return False
            row["status"] = status
        (self.bundle / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        return True

    def dispatcher(self, verify_timeout_s=300):
        return dispatch.Dispatcher(
            WS,
            self.bundle,
            launcher=self.worker,
            alive=lambda pid: pid in self.worker.alive_pids,
            runs=lambda: [],
            set_status=self.set_status,
            load_body=self.read_plan,
            now=self.clock,
            verify_timeout_s=verify_timeout_s,
        )

    def lease_store(self):
        return LeaseStore(self.state_dir)

    def events(self, after=0):
        return dispatch.read_events(self.bundle, after)

    def types(self):
        return [event["type"] for event in self.events()]

    def receipt(self, tid):
        return evidence.load_receipt({"id": tid})

    def flow(self):
        """The /api/flow payload for the plan as it stands now."""
        progress = plans.plan_progress(self.read_plan()["dag"]["groups"])
        state = {"workstreams": [{"id": WS, "title": "Control plane", "progress": progress}]}
        return flowview.build_flow(state, None)


class ContractFlowTests(ControlPlaneCase):
    def test_contract_task_claims_lease_verifies_and_moves_to_pr(self):
        self.write_plan([task("WS35.A", **contract(["src/mod.py"], probe=f"{sys.executable} -c \"print('{MARKER}')\""))])
        disp = self.dispatcher()

        self.assertEqual(disp.tick()["launched"], 1)
        self.assertEqual(self.status_of("WS35.A"), "wip")
        active = self.lease_store().active(now=self.clock())
        self.assertEqual([(row["task"], row["files"]) for row in active], [("WS35.A", ["src/mod.py"])])

        self.worker.finish(LABEL_A, "PASS\nbuilt it\n")
        disp.tick()

        self.assertEqual(self.status_of("WS35.A"), "pr")
        self.assertEqual(self.lease_store().active(now=self.clock()), [], "the lease is released on the move")
        self.assertEqual([e["verdict"] for e in self.events() if e["type"] == "finished"], ["PASS"])
        self.assertNotIn("needs-lead", self.types())

        plan_task = self.plan_task("WS35.A")
        receipt = self.receipt("WS35.A")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["reason"], "ok")
        self.assertEqual(receipt["contract_hash"], evidence.contract_hash(plan_task))
        self.assertEqual(receipt["probe_hash"], evidence.probe_hash(plan_task))
        self.assertEqual(receipt["tree"], evidence.git_tree(self.workspace))
        self.assertEqual(receipt["exit_code"], 0)
        self.assertTrue(evidence.receipt_valid(receipt, plan_task, self.workspace))

        raw = evidence.receipt_path({"id": "WS35.A"}).read_text(encoding="utf-8")
        self.assertNotIn(MARKER, raw, "a receipt carries hashes, never probe output")
        self.assertEqual(receipt["stdout_sha256"], hashlib.sha256(f"{MARKER}\n".encode()).hexdigest())

    def test_second_task_on_the_same_file_is_admission_blocked_while_the_lease_is_live(self):
        self.write_plan([
            task("WS35.A", **contract(["src/mod.py"])),
            task("WS35.B", **contract(["src/mod.py"])),
        ])
        disp = self.dispatcher()

        self.assertEqual(disp.tick()["launched"], 1)
        self.assertEqual(self.status_of("WS35.A"), "wip")
        self.assertEqual(self.status_of("WS35.B"), "todo")
        blocked = [e for e in self.events() if e["type"] == "admission-blocked"]
        self.assertEqual([(e["task"], e["reason"]) for e in blocked], [("WS35.B", "conflict")])
        self.assertEqual(blocked[0]["conflicts"][0]["task"], "WS35.A")
        self.assertEqual(blocked[0]["conflicts"][0]["files"], ["src/mod.py"])
        self.assertEqual(self.worker.launches_for(LABEL_B), [])

        disp.tick()
        self.assertEqual(len([e for e in self.events() if e["type"] == "admission-blocked"]), 1,
                         "one event per held file, not one per tick")

        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()
        self.assertEqual(self.status_of("WS35.A"), "pr")
        self.assertEqual(self.status_of("WS35.B"), "wip", "the released file admits the waiting task")
        self.assertEqual(len(self.worker.launches_for(LABEL_B)), 1)

    def test_failed_probe_does_not_advance(self):
        self.write_plan([task("WS35.A", **contract(["src/mod.py"], probe=f"{sys.executable} -c 'raise SystemExit(3)'"))])
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()

        self.assertEqual(self.status_of("WS35.A"), "wip")
        self.assertIsNone(self.receipt("WS35.A"))
        self.assertEqual(self.lease_store().active(now=self.clock()), [])
        lead = [e for e in self.events() if e["type"] == "needs-lead"]
        self.assertEqual([(e["task"], e["reason"]) for e in lead], [("WS35.A", "verification-failed")])
        verification = [e for e in self.events() if e["type"] == "verification"]
        self.assertEqual(verification[0]["reason"], "probe-failed")
        self.assertEqual(verification[0]["exit_code"], 3)

    def test_dirty_tree_does_not_advance(self):
        self.write_plan([task("WS35.A", **contract(["src/mod.py"]))])
        disp = self.dispatcher()
        disp.tick()
        (self.workspace / "untracked.txt").write_text("x\n", encoding="utf-8")
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()

        self.assertEqual(self.status_of("WS35.A"), "wip")
        self.assertIsNone(self.receipt("WS35.A"))
        verification = [e for e in self.events() if e["type"] == "verification"]
        self.assertEqual(verification[0]["reason"], "dirty-tree")

    def test_amended_tree_does_not_advance(self):
        self.write_plan([task("WS35.A", **contract(["src/mod.py"], probe=[
            f"{sys.executable} -c \"open('amend.txt','w').write('x')\"",
            "git add amend.txt",
            "git commit --amend --no-edit",
        ]))])
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()

        self.assertEqual(self.status_of("WS35.A"), "wip")
        self.assertIsNone(self.receipt("WS35.A"))
        verification = [e for e in self.events() if e["type"] == "verification"]
        self.assertEqual(verification[0]["reason"], "stale-receipt")

    def test_legacy_task_still_advances_on_pass(self):
        self.write_plan([task("WS35.A")])
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()

        self.assertEqual(self.status_of("WS35.A"), "pr")
        self.assertIsNone(self.receipt("WS35.A"))
        self.assertNotIn("verification", self.types())

    def test_flow_projection_sees_the_resulting_queue_state(self):
        self.write_plan([
            task("WS35.A", **contract(["src/mod.py"])),
            task("WS35.B"),
        ])
        disp = self.dispatcher()
        disp.tick()
        self.worker.finish(LABEL_A, "PASS\nok\n")
        disp.tick()

        flow = self.flow()
        queues = {row["key"]: row["count"] for row in flow["queues"]}
        self.assertEqual(queues["pr"], 1, "the verified task is waiting to merge")
        self.assertEqual(queues["wip"], 1, "the legacy task is still running")
        self.assertEqual(queues["todo"], 0)
        self.assertEqual(flow["totals"]["tasks"], 2)
        self.assertEqual(flow["totals"]["done"], 0)
        self.assertEqual(flow["workstreams"][0]["id"], WS)
        self.assertFalse(flow["empty"])


if __name__ == "__main__":
    unittest.main()
