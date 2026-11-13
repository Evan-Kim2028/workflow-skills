"""Filler labels: every label the filler builds passes the launcher's rule and dash's ``label check``; the generator
refuses one the launcher would reject; ``filler repair-failed`` makes the targets of bad-label failures due again; and the
launcher and the label check share one length limit.

Offline. ``dash/labels.py`` is loaded by file path: importing the ``dash`` package would collide with ``agentq/dash.py``
in a run that already loaded the dashboard module.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402
from agentq import cli, db  # noqa: E402
from agentq import filler as F  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DASH_BEFORE = {name for name in sys.modules if name == "dash" or name.startswith("dash.")}
LABELS = load("dash_labels_under_test", ROOT / "dash" / "labels.py")
RUN_WORKER = load("run_worker_under_test", ROOT / "scripts" / "run_worker.py")
for _name in [n for n in sys.modules if (n == "dash" or n.startswith("dash.")) and n not in _DASH_BEFORE]:
    del sys.modules[_name]

REAL_LONG_MODULE = "pipelines/scraper-x/tests/test_prod_eval.py"
REAL_FAILED_LABEL = ("hub-exec-backlog-filler-docs-repo-a-0607c101-pipelines-scraper-x-longerzz-tests-test-prod-eval-py"
                     "-6b44d192")


def target(module, repo="repo-a"):
    return F.Target(repo=repo, module=module, workspace="/ws", report_dir="/rep")


def launch_label(slug):
    return "hub-exec-" + slug


class LabelBudgetTests(unittest.TestCase):
    def test_the_label_that_failed_in_production_is_over_the_limit_and_the_builder_never_makes_one(self):
        self.assertEqual(len(REAL_FAILED_LABEL), 106)
        self.assertFalse(F.label_fits(REAL_FAILED_LABEL))
        for kind in ("bugs", "tests", "docs", "risk"):
            label = launch_label(F.label_slug(target(REAL_LONG_MODULE), kind))
            self.assertTrue(F.label_fits(label), label)
            self.assertLessEqual(len(label), db.LAUNCH_LABEL_MAX)

    def test_the_worst_case_module_path_still_fits(self):
        deep = "/".join(f"segment_{i:03d}_with_a_long_name" for i in range(40))
        for kind in ("bugs", "tests", "docs", "risk"):
            label = launch_label(F.label_slug(target(deep, repo="a-very-long-repository-name-indeed"), kind))
            self.assertTrue(F.label_fits(label), label)

    def test_two_modules_with_the_same_readable_text_keep_different_labels(self):
        left = F.label_slug(target("x" * 200 + "/alpha"), "tests")
        right = F.label_slug(target("x" * 200 + "/beta"), "tests")
        self.assertNotEqual(left, right)
        self.assertNotEqual(F.label_slug(target("pkg/a"), "tests"), F.label_slug(target("pkg/a"), "bugs"))

    def test_every_pr_review_label_fits_for_realistic_numbers(self):
        for number in (1, 9615, 123456789):
            candidate = {"number": number, "title": "t", "url": "u", "repo": "repo-a", "slug": "o/r",
                         "workspace": "/ws", "report_dir": "/rep", "merge_commit": "c"}
            self.assertTrue(F.label_fits(launch_label(F.pr_work(candidate).slug)))

    def test_the_builder_labels_pass_the_label_check_as_well_as_the_launcher_rule(self):
        for module in (REAL_LONG_MODULE, "x" * 200 + "/y.py", "tools/agent-dash/agentq"):
            for kind in ("bugs", "tests", "docs", "risk"):
                label = launch_label(F.label_slug(target(module), kind))
                self.assertTrue(RUN_WORKER.LABEL_OK.fullmatch(label), label)
                self.assertTrue(LABELS.check(label)["ok"], (label, LABELS.check(label)["fix"]))
        candidate = {"number": 9615, "title": "t", "url": "u", "repo": "repo-a", "slug": "o/r",
                     "workspace": "/ws", "report_dir": "/rep", "merge_commit": "c"}
        label = launch_label(F.pr_work(candidate).slug)
        self.assertTrue(LABELS.check(label)["ok"], label)


class OneSourceOfTruthTests(unittest.TestCase):
    def test_the_launcher_uses_the_shared_rule(self):
        self.assertIs(RUN_WORKER.LABEL_OK, db.LAUNCH_LABEL_RE)

    def test_the_label_check_refuses_what_the_launcher_refuses(self):
        self.assertTrue(LABELS.check("hub-exec-" + "a" * 71)["ok"])
        self.assertFalse(LABELS.check("hub-exec-" + "a" * 72)["ok"])
        self.assertFalse(LABELS.check("hub-exec-has space")["ok"])
        self.assertFalse(RUN_WORKER.LABEL_OK.fullmatch("hub-exec-has space"))

    def test_the_length_limit_is_eighty_everywhere(self):
        self.assertEqual(db.LAUNCH_LABEL_MAX, 80)
        self.assertEqual(LABELS._MAX_LABEL, 80)


class StubWork:
    def __init__(self, settings, prs=()):
        self.settings, self.prs = settings, list(prs)

    def snapshot(self):
        return self.settings, self.prs


class GeneratorGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for module in ("pkg/a", "pkg/b"):
            (self.tmp / "repo" / module).mkdir(parents=True)
        self.settings = F.Settings(
            targets=[F.Target("r", "pkg/a", str(self.tmp / "repo"), str(self.tmp / "rep")),
                     F.Target("r", "pkg/b", str(self.tmp / "repo"), str(self.tmp / "rep"))],
            max_runtime=900.0, kinds=("bugs",), report_dir=str(self.tmp / "rep"), max_target_lines=4000,
            min_target_lines=300,
        )
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)

    def broker(self, labels):
        broker = B.Broker(self.home, launcher=mock.Mock(), now=lambda: 1000.0, proxy=lambda: None,
                          gauges=mock.Mock(read=lambda ttl: (90.0, 0.1)), launch_pool=mock.Mock(),
                          labeler=lambda kind, slug: next(labels))
        broker._work = StubWork(self.settings)
        return broker

    def test_a_label_run_worker_would_refuse_is_not_submitted_and_the_rest_of_the_batch_is_(self):
        labels = iter([launch_label("x" * 100), launch_label(F.label_slug(self.settings.targets[1], "bugs"))])
        with mock.patch.object(F, "module_head", return_value="h1"):
            submitted = self.broker(labels)._submit_filler(self.conn, 1000.0, 2)
        self.assertEqual(len(submitted), 1)
        job = db.get_job(self.conn, submitted[0])
        self.assertEqual(job["label"], launch_label(F.label_slug(self.settings.targets[1], "bugs")))

    def test_the_refusal_is_logged_once(self):
        broker = self.broker(iter([launch_label("x" * 100)] * 3))
        with mock.patch.object(F, "module_head", return_value="h1"):
            broker._submit_filler(self.conn, 1000.0, 1)
        broker._submit_filler(self.conn, 1000.0, 1)
        log = (self.home / "logs" / "broker.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("would be refused by run_worker"), 1)


class RepairTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)
        self.work = F.Work(kind="bugs", repo="r", workspace="/ws", module="m", commit="c", report_path="",
                           slug="s", key="r/m#bugs", report_dir="/rep")

    def failed_filler(self, job_id, detail, state="failed", audit=True, work=None):
        db.insert_job(self.conn, {
            "id": job_id, "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": f"hub-exec-{job_id}", "workspace": "/ws", "session": "backlog", "harness": "backlog",
            "prompt_path": "/p",
        }, 1.0)
        self.conn.execute("UPDATE jobs SET state = ?, detail = ? WHERE id = ?", (state, detail, job_id))
        with db.tx(self.conn):
            F.record_filler_job(self.conn, job_id, work or self.work)
            if audit:
                F.record_work(self.conn, work or self.work, job_id, 1.0)

    def test_the_dry_run_counts_and_changes_nothing(self):
        self.failed_filler("bad", "launch failed: STATUS: failed reason=bad-label; 3 attempts")
        result = F.repair_failed(self.conn, apply=False)
        self.assertEqual(result, {"failed_jobs": 1, "audit_rows": 1, "pr_rows": 0, "applied": False})
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM filler_audits WHERE last_job = 'bad'").fetchone())

    def test_apply_clears_only_the_rows_the_bad_label_jobs_wrote(self):
        self.failed_filler("bad", "launch failed: STATUS: failed reason=bad-label; 3 attempts")
        other_target = F.Work(kind="bugs", repo="r", workspace="/ws", module="n", commit="c", report_path="",
                              slug="s", key="r/n#bugs", report_dir="/rep")
        self.failed_filler("timeout", "max runtime 2700s exceeded", work=other_target)
        self.failed_filler("done", None, state="done", work=F.Work(kind="docs", repo="r", workspace="/ws", module="o",
                                                                   commit="c", report_path="", slug="s",
                                                                   key="r/o#docs", report_dir="/rep"))
        other = F.Work(kind="tests", repo="r", workspace="/ws", module="n", commit="c", report_path="", slug="s",
                       key="r/n#tests", report_dir="/rep")
        db.insert_job(self.conn, {
            "id": "pr-bad", "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "hub-exec-pr-bad", "workspace": "/ws", "session": "backlog", "harness": "backlog", "prompt_path": "/p",
        }, 1.0)
        self.conn.execute("UPDATE jobs SET state = 'failed', detail = 'launch failed: reason=bad-label' WHERE id = 'pr-bad'")
        with db.tx(self.conn):
            F.record_filler_job(self.conn, "pr-bad", other)
            F.record_work(self.conn, F.pr_work({"number": 5, "title": "t", "url": "u", "repo": "r", "slug": "o/r",
                                                "workspace": "/ws", "report_dir": "/rep", "merge_commit": "c"}),
                          "pr-bad", 1.0)
        result = F.repair_failed(self.conn, apply=True)
        self.assertEqual(result["audit_rows"], 1)
        self.assertEqual(result["pr_rows"], 1)
        self.assertTrue(result["applied"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM filler_audits WHERE last_job = 'bad'").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM filler_prs WHERE last_job = 'pr-bad'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM filler_audits WHERE last_job = 'timeout'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM filler_audits WHERE last_job = 'done'").fetchone())

    def test_the_repaired_target_is_due_again(self):
        self.failed_filler("bad", "launch failed: STATUS: failed reason=bad-label; 3 attempts")
        target = F.Target("r", "m", "/ws", "/rep")
        self.assertEqual(F.due_kinds(self.conn, target, ("bugs",), "c"), [])
        F.repair_failed(self.conn, apply=True)
        self.assertEqual(F.due_kinds(self.conn, target, ("bugs",), "c"), ["bugs"])

    def test_nothing_to_repair_says_so(self):
        self.assertEqual(F.repair_failed(self.conn, apply=True)["failed_jobs"], 0)

    def test_the_cli_is_a_dry_run_unless_apply_is_given(self):
        self.failed_filler("bad", "launch failed: STATUS: failed reason=bad-label; 3 attempts")
        self.conn.commit()
        env = {"AGENTQ_HOME": str(self.home), "PATH": os.environ["PATH"]}
        out = io.StringIO()
        cli.main(["filler", "repair-failed"], env=env, out=out, now=lambda: 1000.0)
        self.assertIn("would clear 1 audit row(s)", out.getvalue())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM filler_audits WHERE last_job = 'bad'").fetchone())
        out = io.StringIO()
        cli.main(["filler", "repair-failed", "--apply"], env=env, out=out, now=lambda: 1000.0)
        self.assertIn("cleared 1 audit row(s)", out.getvalue())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM filler_audits WHERE last_job = 'bad'").fetchone())


if __name__ == "__main__":
    unittest.main()
