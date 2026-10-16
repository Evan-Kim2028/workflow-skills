"""Lane driver with an injected runner. No real cursor-agent."""
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAVE = ROOT / "workflows" / "ws-wave.js"


def load():
    path = ROOT / "scripts" / "drive_lanes.py"
    spec = importlib.util.spec_from_file_location("drive_lanes_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load()
REGISTRY = json.loads((ROOT / "workers.example.json").read_text(encoding="utf-8"))


class Fake:
    def __init__(self, report):
        self.report = report
        self.calls = []
        self.git_calls = []
        self.dash_calls = []
        self.sleeps = 0
        self.alive_left = {}
        self.lock = threading.Lock()
        self._pid = 5000

    def runner(self, argv):
        with self.lock:
            self.calls.append(list(argv))
            self._pid += 1
            pid = self._pid
            self.alive_left[pid] = 1
        label = argv[2]
        run_dir = Path(argv[5])
        tier = argv[1]
        model = argv[7]
        text = self.report(label)
        run_dir.mkdir(parents=True, exist_ok=True)
        out = run_dir / f"out-{label}.md"
        out.write_text(text, encoding="utf-8")
        (run_dir / f"{label}.pid").write_text(f"{pid}\n", encoding="utf-8")
        (run_dir / f"{label}.tier").write_text(f"{tier}\n{model}\n", encoding="utf-8")
        err = run_dir / f"err-{label}.log"
        state = "failed" if not str(text).strip() else "running"
        line = f"STATUS: {state} out={out} err={err} tier={tier}"
        if model:
            line += f" model={model}"
        if state == "failed":
            line += " reason=empty-report"
        return 0, line + "\n", ""

    def pid_alive(self, pid):
        with self.lock:
            left = self.alive_left.get(int(pid), 0)
            if left > 0:
                self.alive_left[int(pid)] = left - 1
                return True
        return False

    def sleep(self, _seconds):
        with self.lock:
            self.sleeps += 1

    def git(self, argv):
        with self.lock:
            self.git_calls.append(list(argv))
        return 0

    def dash(self, argv):
        with self.lock:
            self.dash_calls.append(list(argv))
        return 0

    def pgrep(self, _pattern):
        return 0

    def hooks(self, registry=None):
        return {
            "runner": self.runner,
            "git": self.git,
            "dash": self.dash,
            "sleep": self.sleep,
            "pid_alive": self.pid_alive,
            "pgrep": self.pgrep,
            "registry": REGISTRY if registry is None else registry,
        }

    def labels(self):
        with self.lock:
            return [call[2] for call in self.calls]


def make_spec(root, lanes, **extra):
    template = Path(root) / "template.md"
    template.write_text("COMMON RULES\n", encoding="utf-8")
    run_dir = Path(root) / "runs"
    run_dir.mkdir(exist_ok=True)
    spec = {
        "ws": "WS7",
        "repo": str(Path(root) / "repo"),
        "runDir": str(run_dir),
        "template": str(template),
        "runner": "/bin/run_worker.sh",
        "dash": "/bin/agent_dash.py",
        "maxFixRounds": 2,
        "lanes": lanes,
    }
    spec.update(extra)
    return spec


def lane(root, task="A", slug="a", **kwargs):
    row = {
        "task": task,
        "slug": slug,
        "branch": f"ws7/{slug}",
        "worktree": str(Path(root) / f"wt-{slug}"),
        "brief": f"build {task}",
        "tests": f"pytest {slug}.py",
        "review": True,
    }
    row.update(kwargs)
    return row


def statuses(fake, task):
    return [call[-1] for call in fake.dash_calls if len(call) >= 2 and call[-2] == task]


class DriveLanesTest(unittest.TestCase):
    def test_approve_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(lambda label: "VERDICT: APPROVE\n" if "-rev" in label else "PASS\nbuilt\n")
            spec = make_spec(tmp, [lane(tmp)])
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 0)
            self.assertEqual(fake.labels(), ["ws7-A-a", "ws7-A-a-rev1"])
            self.assertEqual(summary["approved"], [{"task": "A", "branch": "ws7/a", "worktree": spec["lanes"][0]["worktree"]}])
            self.assertEqual(summary["needsLead"], [])
            self.assertEqual(summary["lanes"][0]["final"], "APPROVE")
            self.assertEqual(summary["lanes"][0]["runs"][0]["role"], "build")
            self.assertEqual(summary["lanes"][0]["runs"][1]["role"], "review")
            self.assertEqual(statuses(fake, "A"), ["wip", "pr"])
            self.assertGreaterEqual(fake.sleeps, 1)
            self.assertEqual(fake.calls[0][6], "15")
            prompt = Path(fake.calls[0][4]).read_text(encoding="utf-8")
            self.assertTrue(prompt.startswith("[label: ws7-A-a]\n\nCOMMON RULES\n"))
            self.assertIn("The diff base is origin/main.", prompt)
            joined = [" ".join(cmd) for cmd in fake.git_calls]
            self.assertTrue(any("fetch -q origin main" in cmd for cmd in joined))
            self.assertTrue(any("worktree add -b ws7/a" in cmd and "origin/main" in cmd for cmd in joined))
            self.assertFalse(any(part in ("push", "merge", "rebase") for cmd in fake.git_calls for part in cmd))
            saved = json.loads((Path(spec["runDir"]) / "drive-A.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["final"], "APPROVE")

    def test_fix_loop_then_approve(self):
        def report(label):
            if label.endswith("-rev1"):
                return "VERDICT: REQUEST_CHANGES\n1. bug - a.py:1 - wrong - fix it\n"
            if "-rev" in label:
                return "VERDICT: APPROVE\n"
            return "PASS\nfixed\n"

        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(report)
            spec = make_spec(tmp, [lane(tmp, reviewFocus="the index lists every note")])
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 0)
            self.assertEqual(
                fake.labels(),
                ["ws7-A-a", "ws7-A-a-rev1", "ws7-A-a-fix1", "ws7-A-a-rev2"],
            )
            self.assertEqual(summary["lanes"][0]["final"], "APPROVE")
            roles = [row["role"] for row in summary["lanes"][0]["runs"]]
            self.assertEqual(roles, ["build", "review", "fix", "review"])
            fix_prompt = Path(fake.calls[2][4]).read_text(encoding="utf-8")
            self.assertIn("1. bug - a.py:1 - wrong - fix it", fix_prompt)
            self.assertEqual(statuses(fake, "A"), ["wip", "pr"])

    def test_max_rounds_needs_lead(self):
        def report(label):
            if "-rev" in label:
                return "VERDICT: REQUEST_CHANGES\nstill wrong\n"
            return "PASS\n"

        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(report)
            spec = make_spec(tmp, [lane(tmp)], maxFixRounds=1)
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 3)
            self.assertEqual(summary["lanes"][0]["final"], "needs-lead")
            self.assertEqual(summary["needsLead"][0]["task"], "A")
            self.assertEqual(summary["needsLead"][0]["final"], "needs-lead")
            self.assertNotIn("ws7-A-a-fix2", fake.labels())
            self.assertIn("ws7-A-a-fix1", fake.labels())
            self.assertIn("ws7-A-a-rev2", fake.labels())
            self.assertNotIn("pr", statuses(fake, "A"))

    def test_blocked_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(lambda _label: "BLOCKED\ncould not read the schema\n")
            spec = make_spec(tmp, [lane(tmp)])
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 3)
            self.assertEqual(fake.labels(), ["ws7-A-a"])
            self.assertEqual(summary["lanes"][0]["final"], "build-failed")
            self.assertEqual(summary["needsLead"][0]["final"], "build-failed")
            self.assertEqual(summary["approved"], [])
            self.assertEqual(statuses(fake, "A"), ["wip"])

    def test_empty_report_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(lambda _label: "")
            spec = make_spec(tmp, [lane(tmp)])
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 3)
            self.assertEqual(fake.labels(), ["ws7-A-a"])
            self.assertEqual(summary["lanes"][0]["final"], "build-failed")
            self.assertEqual(summary["lanes"][0]["build"]["verdict"], "none")

    def test_deps_order_and_skipped(self):
        def report(label):
            if label == "ws7-X-x":
                return "BLOCKED\nmissing file\n"
            if "-rev" in label:
                return "VERDICT: APPROVE\n"
            return "PASS\nbuilt\n"

        errors = []
        barrier = threading.Barrier(2)

        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(report)

            def runner(argv):
                label = argv[2]
                if label in ("ws7-A-a", "ws7-X-x"):
                    try:
                        barrier.wait(3)
                    except threading.BrokenBarrierError as exc:
                        errors.append(repr(exc))
                        raise
                return fake.runner(argv)

            hooks = fake.hooks()
            hooks["runner"] = runner
            lanes = [
                lane(tmp, "A", "a"),
                lane(tmp, "B", "b", deps=["A"]),
                lane(tmp, "X", "x"),
                lane(tmp, "C", "c", deps=["X"]),
                lane(tmp, "S", "s", deps=["A"], base="dep:A", review=False),
            ]
            spec = make_spec(tmp, lanes)
            summary, code = mod.drive(spec, hooks=hooks)
            self.assertEqual(errors, [])
            labels = fake.labels()
            self.assertLess(labels.index("ws7-A-a-rev1"), labels.index("ws7-B-b"))
            self.assertLess(labels.index("ws7-A-a-rev1"), labels.index("ws7-S-s"))
            self.assertFalse(any(label.startswith("ws7-C-") for label in labels))
            by_task = {row["task"]: row for row in summary["lanes"]}
            self.assertEqual(by_task["A"]["final"], "APPROVE")
            self.assertEqual(by_task["B"]["final"], "APPROVE")
            self.assertEqual(by_task["X"]["final"], "build-failed")
            self.assertEqual(by_task["C"]["final"], "skipped-dep")
            self.assertEqual(by_task["C"]["dependency"], "X")
            self.assertEqual(by_task["C"]["runs"], [])
            self.assertEqual(by_task["S"]["final"], "built")
            self.assertEqual([row["role"] for row in by_task["S"]["runs"]], ["build"])
            self.assertEqual(summary["skipped"], [{"task": "C", "dependency": "X", "branch": "ws7/c"}])
            self.assertEqual([row["task"] for row in summary["needsLead"]], ["X"])
            self.assertEqual([row["task"] for row in summary["built"]], ["S"])
            self.assertEqual(code, 3)
            self.assertEqual(statuses(fake, "S"), ["wip"])
            self.assertNotIn("pr", statuses(fake, "S"))
            self.assertNotIn("wip", statuses(fake, "C"))
            s_cmds = [" ".join(cmd) for cmd in fake.git_calls if str(Path(tmp) / "wt-s") in " ".join(cmd)]
            self.assertTrue(s_cmds)
            self.assertIn("ws7/a", s_cmds[0])
            self.assertNotIn("origin/main", s_cmds[0])
            self.assertFalse(any("fetch" in cmd for cmd in s_cmds))
            a_cmds = [" ".join(cmd) for cmd in fake.git_calls if str(Path(tmp) / "wt-a") in " ".join(cmd)]
            self.assertTrue(any("origin/main" in cmd for cmd in a_cmds))

    def test_next_free_label_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = make_spec(tmp, [lane(tmp, review=True)])
            Path(spec["lanes"][0]["worktree"]).mkdir()
            run_dir = Path(spec["runDir"])
            (run_dir / "ws7-A-a-rev1.tier").write_text("grok\n", encoding="utf-8")
            (run_dir / "ws7-A-a-rev2.tier").write_text("grok\n", encoding="utf-8")
            fake = Fake(lambda _label: "VERDICT: APPROVE\n")
            code = mod.main([spec_path(tmp, spec), "--only", "A", "--start", "rev"], hooks=fake.hooks())
            self.assertEqual(code, 0)
            self.assertEqual(fake.labels(), ["ws7-A-a-rev3"])
            self.assertEqual(statuses(fake, "A"), ["pr"])
            prompt = Path(fake.calls[0][4]).read_text(encoding="utf-8")
            self.assertIn("round 3", prompt)

    def test_review_focus_in_review_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(lambda label: "VERDICT: APPROVE\n" if "-rev" in label else "PASS\n")
            spec = make_spec(
                tmp,
                [lane(tmp, reviewFocus=["the index lists every note", "the flag is set"])],
            )
            _summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 0)
            prompt = Path(fake.calls[1][4]).read_text(encoding="utf-8")
            self.assertIn("the index lists every note", prompt)
            self.assertIn("1. the index lists every note", prompt)
            self.assertIn("2. the flag is set", prompt)
            self.assertIn(mod.MUST_CHECK, prompt)
            self.assertIn(mod.CHECK_FAILED, prompt)

    def test_tier_and_model_reach_runner(self):
        grok = "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]"

        def report(label):
            if label.endswith("-rev1"):
                return "VERDICT: REQUEST_CHANGES\n1. bug - a.py:1 - wrong - fix it\n"
            if "-rev" in label:
                return "VERDICT: APPROVE_WITH_NITS\n"
            return "PASS\n"

        with tempfile.TemporaryDirectory() as tmp:
            row = lane(
                tmp,
                tier="grok",
                model=grok,
                reviewTier="composer",
                reviewModel="composer-2.5",
                fixTier="glm",
                fixModel="glm-5.3",
                reviewFocus="the index lists every note",
            )
            bare = lane(tmp, task="B", slug="b", review=False)
            fake = Fake(report)
            spec = make_spec(tmp, [row, bare])
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 0)
            by_label = {call[2]: call for call in fake.calls}
            self.assertEqual(by_label["ws7-A-a"][1], "grok")
            self.assertEqual(by_label["ws7-A-a"][7], grok)
            self.assertEqual(by_label["ws7-A-a-rev1"][1], "composer")
            self.assertEqual(by_label["ws7-A-a-rev1"][7], "composer-2.5")
            self.assertEqual(by_label["ws7-A-a-fix1"][1], "glm")
            self.assertEqual(by_label["ws7-A-a-fix1"][7], "glm-5.3")
            self.assertEqual(by_label["ws7-B-b"][1], "default")
            self.assertEqual(by_label["ws7-B-b"][7], "")
            self.assertEqual(by_label["ws7-A-a"][6], "15")
            runs = {row["task"]: row["runs"] for row in summary["lanes"]}
            self.assertEqual(runs["A"][0]["tier"], "grok")
            self.assertEqual(runs["A"][0]["model"], grok)
            self.assertEqual(runs["A"][1]["tier"], "composer")
            self.assertEqual(runs["A"][1]["model"], "composer-2.5")
            self.assertEqual(runs["A"][2]["tier"], "glm")
            self.assertEqual(runs["A"][2]["model"], "glm-5.3")
            self.assertEqual(summary["lanes"][0]["final"], "APPROVE_WITH_NITS")
            prompt = Path(by_label["ws7-A-a-rev1"][4]).read_text(encoding="utf-8")
            self.assertIn("the index lists every note", prompt)

    def test_one_way_reviews_when_review_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake(lambda label: "VERDICT: APPROVE\n" if "-rev" in label else "PASS\n")
            spec = make_spec(
                tmp,
                [lane(tmp, review=False, oneWay=True, reviewFocus="flag is set")],
            )
            summary, code = mod.drive(spec, hooks=fake.hooks())
            self.assertEqual(code, 0)
            self.assertEqual(summary["lanes"][0]["final"], "APPROVE")
            self.assertIn("ws7-A-a-rev1", fake.labels())
            prompt = Path(fake.calls[1][4]).read_text(encoding="utf-8")
            self.assertIn("flag is set", prompt)

    def test_tier_cap(self):
        registry = json.loads(json.dumps(REGISTRY))
        registry["workers"]["muse"]["cap"] = 1
        entered = []
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        second = threading.Event()

        def runner(argv):
            with lock:
                entered.append(argv[2])
                n = len(entered)
            if n == 1:
                started.set()
                release.wait(5)
            else:
                second.set()
            run_dir = Path(argv[5])
            run_dir.mkdir(parents=True, exist_ok=True)
            out = run_dir / f"out-{argv[2]}.md"
            out.write_text("PASS\n", encoding="utf-8")
            (run_dir / f"{argv[2]}.tier").write_text(argv[1] + "\n\n", encoding="utf-8")
            err = run_dir / f"err-{argv[2]}.log"
            model = argv[7]
            line = f"STATUS: done out={out} err={err} tier={argv[1]}"
            if model:
                line += f" model={model}"
            return 0, line + "\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            lanes = [
                lane(tmp, "A", "a", tier="muse", review=False),
                lane(tmp, "B", "b", tier="muse", review=False),
            ]
            spec = make_spec(tmp, lanes)
            holder = {}

            def go():
                holder["result"] = mod.drive(
                    spec,
                    hooks={
                        "runner": runner,
                        "git": lambda _argv: 0,
                        "dash": lambda _argv: 0,
                        "sleep": lambda _seconds: None,
                        "pid_alive": lambda _pid: False,
                        "pgrep": lambda _pattern: 0,
                        "registry": registry,
                    },
                )

            thread = threading.Thread(target=go)
            thread.start()
            self.assertTrue(started.wait(3))
            self.assertFalse(second.wait(0.3), "second lane started while the tier was at cap")
            release.set()
            self.assertTrue(second.wait(3))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            summary, code = holder["result"]
            self.assertEqual(code, 0)
            self.assertEqual({row["final"] for row in summary["lanes"]}, {"built"})
            self.assertEqual(len(entered), 2)

    def test_key_sentences_match_ws_wave(self):
        js = WAVE.read_text(encoding="utf-8")
        row = {
            "task": "A",
            "slug": "a",
            "branch": "ws7/a",
            "worktree": "/wt/a",
            "brief": "build A",
            "tests": "",
            "reviewFocus": "the index lists every note",
        }
        blob = "\n".join(
            [
                mod.build_task(row, "WS7", "/repo", "origin/main", mod.DEFAULT_TRAILER),
                mod.review_task(row, 1, "WS7", "origin/main"),
                mod.fix_task(row, 1, "WS7", "1. bug", mod.DEFAULT_TRAILER),
            ]
        )
        for sentence in mod.KEY_SENTENCES:
            self.assertIn(sentence, js, sentence)
            self.assertIn(sentence, blob, sentence)
        self.assertIn("the index lists every note", blob)

    def test_cli_rejects_resume_without_only(self):
        self.assertEqual(mod.main(["missing.json", "--start", "rev"]), 2)
        self.assertEqual(mod.main(["missing.json", "--only", "A", "--start", "fix"]), 2)


def spec_path(root, spec):
    path = Path(root) / "lanes.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)
