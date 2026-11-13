"""Filler work supply: the module splitter, per-kind due and rotation, the merged-PR source (gh stubbed), the
findings index, the store migration to per-kind audits, the settings checks and the dry-run report.

Offline. AGENTQ_HOME and the repos are temp folders; gh is a fake that returns canned JSON.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402
from agentq import db  # noqa: E402
from agentq import filler as F  # noqa: E402
from agentq import pr_source as P  # noqa: E402

NOW = 1_800_000_000.0


def write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"x = {i}\n" for i in range(count)), encoding="utf-8")


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ws = self.tmp / "repo"
        self.ws.mkdir()
        self.home = self.tmp / "home"


class SplitterTests(Temp):
    def test_a_module_within_the_limit_is_one_target(self):
        write_lines(self.ws / "pkg" / "a.py", 50)
        self.assertEqual(F.split_module(str(self.ws), "pkg", 4000, 300), ["pkg"])

    def test_an_oversized_module_splits_into_its_child_packages(self):
        for child in ("alpha", "beta"):
            write_lines(self.ws / "big" / child / "m.py", 1500)
        write_lines(self.ws / "big" / "tiny" / "t.py", 10)
        self.assertEqual(F.split_module(str(self.ws), "big", 2000, 300), ["big/alpha", "big/beta", "big/tiny"])

    def test_a_child_still_over_the_limit_splits_into_files_of_at_least_min_lines(self):
        write_lines(self.ws / "big" / "core" / "long.py", 1200)
        write_lines(self.ws / "big" / "core" / "short.py", 100)
        write_lines(self.ws / "big" / "core" / "sub" / "deep.py", 900)
        write_lines(self.ws / "big" / "other" / "x.py", 1500)
        got = F.split_module(str(self.ws), "big", 1000, 300)
        self.assertEqual(got, ["big/core/long.py", "big/core/sub/deep.py", "big/other/x.py"])

    def test_files_directly_in_an_oversized_module_count_when_large_enough(self):
        write_lines(self.ws / "flat" / "huge.py", 1200)
        write_lines(self.ws / "flat" / "small.py", 20)
        write_lines(self.ws / "flat" / "child" / "c.py", 1200)
        self.assertEqual(F.split_module(str(self.ws), "flat", 1000, 300), ["flat/child/c.py", "flat/huge.py"])

    def test_a_module_with_no_code_is_kept_whole_for_its_docs(self):
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "README.md").write_text("docs only\n", encoding="utf-8")
        self.assertEqual(F.split_module(str(self.ws), "docs", 4000, 300), ["docs"])

    def test_caches_and_hidden_folders_are_not_counted(self):
        write_lines(self.ws / "pkg" / "__pycache__" / "x.py", 5000)
        write_lines(self.ws / "pkg" / ".venv" / "y.py", 5000)
        write_lines(self.ws / "pkg" / "real.py", 10)
        self.assertEqual(F.split_module(str(self.ws), "pkg", 100, 5), ["pkg"])

    def test_a_missing_folder_yields_no_target_and_a_file_is_its_own_target(self):
        self.assertEqual(F.split_module(str(self.ws), "nope", 100, 5), [])
        write_lines(self.ws / "one.py", 3)
        self.assertEqual(F.split_module(str(self.ws), "one.py", 100, 5), ["one.py"])

    def test_the_settings_split_each_repo_module_and_keep_github_slugs(self):
        write_lines(self.ws / "big" / "a" / "m.py", 1500)
        write_lines(self.ws / "big" / "b" / "m.py", 1500)
        config = {"filler": {"report_dir": str(self.tmp / "rep"), "max_target_lines": 2000, "min_target_lines": 300,
                             "repos": {"r": {"workspace": str(self.ws), "modules": ["big"], "github": "o/r"}}}}
        settings = F.load_settings(config)
        self.assertEqual([t.module for t in settings.targets], ["big/a", "big/b"])
        self.assertEqual([(p.repo, p.slug) for p in settings.pr_repos], [("r", "o/r")])
        self.assertEqual(settings.kinds, F.DEFAULT_KINDS)
        self.assertEqual(settings.pr_window_h, 48.0)


class SettingsValidationTests(Temp):
    def config(self, **filler):
        base = {"report_dir": str(self.tmp / "rep"), "repos": {"r": {"workspace": str(self.ws), "modules": ["x"]}}}
        base.update(filler)
        return {"filler": base}

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown kinds: speed"):
            F.load_settings(self.config(kinds=["bugs", "speed"]))

    def test_the_limits_must_be_positive_whole_numbers(self):
        with self.assertRaisesRegex(ValueError, "max_target_lines"):
            F.load_settings(self.config(max_target_lines=0))
        with self.assertRaisesRegex(ValueError, "min_target_lines"):
            F.load_settings(self.config(min_target_lines="300"))

    def test_the_pr_window_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "pr_window_h"):
            F.load_settings(self.config(pr_window_h=0))


class KindDueTests(Temp):
    def setUp(self):
        super().setUp()
        write_lines(self.ws / "pkg" / "a.py", 10)
        write_lines(self.ws / "pkg" / "b.py", 10)
        self.target = F.Target(repo="r", module="pkg", workspace=str(self.ws), report_dir=str(self.tmp / "rep"))
        self.other = F.Target(repo="r", module="pkg2", workspace=str(self.ws), report_dir=str(self.tmp / "rep"))
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)

    def submit(self, work, job_id, now=NOW):
        with db.tx(self.conn):
            F.record_work(self.conn, work, job_id, now)

    def insert_job(self, job_id, state="queued"):
        db.insert_job(self.conn, {
            "id": job_id, "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "x", "workspace": str(self.ws), "session": "backlog", "harness": "backlog", "prompt_path": "/p",
        }, NOW)
        self.conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job_id))

    def test_a_never_run_target_has_every_kind_due_in_configured_order(self):
        self.assertEqual(F.due_kinds(self.conn, self.target, F.DEFAULT_KINDS, "h1"),
                         ["bugs", "tests", "docs", "risk"])

    def test_a_kind_is_not_due_again_until_the_target_changes(self):
        self.insert_job("j1", "done")
        self.submit(F.audit_work(self.target, "bugs", "h1"), "j1")
        self.assertNotIn("bugs", F.due_kinds(self.conn, self.target, F.DEFAULT_KINDS, "h1"))
        self.assertIn("bugs", F.due_kinds(self.conn, self.target, F.DEFAULT_KINDS, "h2"))

    def test_other_kinds_stay_due_after_one_kind_runs(self):
        self.insert_job("j1", "done")
        self.submit(F.audit_work(self.target, "bugs", "h1"), "j1")
        self.assertEqual(F.due_kinds(self.conn, self.target, F.DEFAULT_KINDS, "h1"), ["tests", "docs", "risk"])

    def test_a_kind_with_an_open_job_is_not_due(self):
        self.insert_job("open", "queued")
        self.submit(F.audit_work(self.target, "tests", "h1"), "open")
        self.assertNotIn("tests", F.due_kinds(self.conn, self.target, F.DEFAULT_KINDS, "h2"))

    def test_next_work_rotates_targets_and_kinds_without_repeating_a_pair(self):
        seen = []
        for n in range(5):
            work = F.next_work(self.conn, [self.target, self.other], F.DEFAULT_KINDS, head_of=lambda t: "h1")
            if work is None:
                break
            seen.append((work.module, work.kind))
            self.insert_job(f"job{n}", "done")
            self.submit(work, f"job{n}")
        self.assertEqual(seen[:4], [("pkg", "bugs"), ("pkg2", "bugs"), ("pkg", "tests"), ("pkg2", "tests")])
        self.assertEqual(len(set(seen)), len(seen))

    def test_a_target_without_a_head_commit_is_skipped(self):
        self.assertIsNone(F.next_work(self.conn, [self.target], F.DEFAULT_KINDS, head_of=lambda t: None))

    def test_the_report_path_and_label_carry_the_kind(self):
        work = F.audit_work(self.target, "docs", "h1")
        self.assertEqual(work.report_path, str(self.tmp / "rep" / "filler" / "r" / "docs" / "pkg.md"))
        self.assertEqual(work.slug, "backlog-filler-docs-r-pkg")
        self.assertTrue(F.is_filler_label("hub-exec-" + work.slug))

    def test_the_prompt_for_each_kind_is_its_own_brief(self):
        texts = {}
        for kind in ("bugs", "tests", "docs", "risk"):
            texts[kind] = F.prompt_for(F.audit_work(self.target, kind, "h1"))
            self.assertIn(f"kind: {kind}", texts[kind])
            self.assertNotIn("{", texts[kind].split("Scope:")[0])
        self.assertEqual(len(set(texts.values())), 4)


class PrSourceTests(Temp):
    def fake(self, rows=None, returncode=0, stdout=None, stderr=""):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            text = stdout if stdout is not None else json.dumps(rows or [])
            return subprocess.CompletedProcess(cmd, returncode, text, stderr)
        run.calls = calls
        return run

    def test_merged_prs_builds_one_bounded_gh_call_and_parses_rows(self):
        rows = [
            {"number": 7, "title": "old", "url": "u7", "mergedAt": "2026-11-11T10:00:00Z",
             "mergeCommit": {"oid": "aaa"}},
            {"number": 9, "title": "new", "url": "u9", "mergedAt": "2026-11-12T10:00:00Z",
             "mergeCommit": {"oid": "bbb"}},
        ]
        run = self.fake(rows)
        got = P.merged_prs("o/r", "2026-11-10", run=run)
        cmd = run.calls[0]
        self.assertEqual(cmd[:4], ["gh", "pr", "list", "--repo"])
        self.assertIn("merged:>=2026-11-10", cmd)
        self.assertIn("--limit", cmd)
        self.assertEqual([r["number"] for r in got], [9, 7])
        self.assertEqual(got[0]["merge_commit"], "bbb")

    def test_a_failed_or_unreadable_gh_call_raises_so_the_repo_is_skipped(self):
        with self.assertRaises(P.PRSourceError):
            P.merged_prs("o/r", "2026-11-10", run=self.fake(returncode=1, stderr="rate limited"))
        with self.assertRaises(P.PRSourceError):
            P.merged_prs("o/r", "2026-11-10", run=self.fake(stdout="not json"))
        with self.assertRaises(P.PRSourceError):
            P.merged_prs("o/r", "2026-11-10", run=mock.Mock(side_effect=OSError("no gh")))

    def test_the_window_is_taken_in_whole_days(self):
        self.assertEqual(P.since_date(NOW, 48), "2027-01-13")

    def test_a_merged_pr_is_reviewed_once_and_the_next_target_follows(self):
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        target = F.Target(repo="r", module="pkg", workspace=str(self.ws), report_dir=str(self.tmp / "rep"))
        write_lines(self.ws / "pkg" / "a.py", 5)
        candidate = {"number": 9, "title": "t", "url": "u", "repo": "r", "slug": "o/r", "workspace": str(self.ws),
                     "report_dir": str(self.tmp / "rep"), "merge_commit": "bbb", "mergedAt": ""}
        work = F.next_work(conn, [target], prs=[candidate], head_of=lambda t: "h1")
        self.assertEqual((work.kind, work.key), ("pr", "pr:r#9"))
        self.assertEqual(work.report_path, str(self.tmp / "rep" / "filler" / "r" / "pr" / "9.md"))
        with db.tx(conn):
            F.record_work(conn, work, "jobpr", NOW)
        nxt = F.next_work(conn, [target], prs=[candidate], head_of=lambda t: "h1")
        self.assertEqual(nxt.kind, "bugs")

    def test_the_pr_prompt_names_the_pull_request_and_its_slug(self):
        candidate = {"number": 9, "title": "Fix x", "url": "https://x/9", "repo": "r", "slug": "o/r",
                     "workspace": str(self.ws), "report_dir": str(self.tmp / "rep"), "merge_commit": "bbb"}
        text = F.prompt_for(F.pr_work(candidate))
        self.assertIn("#9", text)
        self.assertIn("--repo o/r", text)
        self.assertIn("Fix x", text)


class FindingsIndexTests(Temp):
    REPORT_A = (
        "# report\n\n- **low** `a.py:3` — stale comment\n- **high** `b.py:10` — unchecked error\n"
        "- **medium** `c.py:7` — race on cache\n\nVERDICT: PASS\n"
    )

    def test_findings_are_parsed_and_the_top_ones_are_the_most_severe(self):
        self.assertEqual(F.parse_findings(self.REPORT_A)[1], ("high", "b.py:10", "unchecked error"))
        top = F.top_findings(self.REPORT_A, 2)
        self.assertEqual([f[0] for f in top], ["high", "medium"])

    def test_the_index_is_created_newest_first_and_deduplicated_by_location_and_title(self):
        rep = str(self.tmp / "rep")
        added = F.append_index(rep, "job one", F.top_findings(self.REPORT_A, 5), NOW)
        self.assertEqual(added, 3)
        again = F.append_index(rep, "job two", F.top_findings(self.REPORT_A, 5), NOW + 60)
        self.assertEqual(again, 0)
        newer = "- **high** `d.py:1` — new defect\n"
        self.assertEqual(F.append_index(rep, "job three", F.parse_findings(newer), NOW + 120), 1)
        text = (self.tmp / "rep" / "filler" / "INDEX.md").read_text(encoding="utf-8")
        self.assertLess(text.index("job three"), text.index("job one"))
        self.assertEqual(text.count("`b.py:10`"), 1)
        self.assertTrue(text.startswith("# Filler findings"))

    def test_the_same_location_with_a_new_title_is_a_new_finding(self):
        rep = str(self.tmp / "rep")
        F.append_index(rep, "a", [("high", "x.py:1", "first")], NOW)
        self.assertEqual(F.append_index(rep, "b", [("high", "x.py:1", "second")], NOW), 1)

    def test_a_block_that_repeats_a_finding_in_one_report_adds_it_once(self):
        rep = str(self.tmp / "rep")
        self.assertEqual(F.append_index(rep, "a", [("low", "y.py:2", "t"), ("low", "y.py:2", "t")], NOW), 1)


class MigrationTests(Temp):
    def test_a_version_four_store_gets_its_audit_rows_as_bugs_kind(self):
        conn = db.connect(self.home)
        conn.execute("DROP TABLE filler_audits")
        conn.execute("DROP TABLE filler_prs")
        conn.execute("INSERT INTO filler_targets VALUES ('r/pkg', 'c9', 5.0, NULL)")
        conn.execute("PRAGMA user_version = 4")
        conn.close()
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
        row = conn.execute("SELECT last_commit FROM filler_audits WHERE target = 'r/pkg' AND kind = 'bugs'").fetchone()
        self.assertEqual(row[0], "c9")
        self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'filler_prs'").fetchone())


class DryRunTests(Temp):
    def test_the_dry_run_counts_due_pairs_and_unreviewed_prs_without_launching(self):
        write_lines(self.ws / "pkg" / "a.py", 10)
        self.home.mkdir()
        (self.home / "backlog-seed.json").write_text(json.dumps({"filler": {
            "report_dir": str(self.tmp / "rep"), "pr_window_h": 48,
            "repos": {"r": {"workspace": str(self.ws), "modules": ["pkg"], "github": "o/r"}},
        }}), encoding="utf-8")
        recent = "2026-11-13T00:00:00Z"
        pr_rows = [{"number": 5, "title": "t", "url": "u", "mergedAt": recent, "merge_commit": "c"}]
        out = io.StringIO()
        code = F.main(["--home", str(self.home)], out=out, run_pr=lambda slug, since: pr_rows,
                      now=P.merged_epoch({"mergedAt": recent}) + 3600, head_of=lambda t: "h1")
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("due target+kind pairs: 4", text)
        self.assertIn("merged PRs due for review in the last 48 h: 1", text)
        self.assertIn("ANSWER: 4 target+kind pair(s) and 1 merged PR(s) due now", text)


class BrokerFindingsTest(Temp):
    def test_a_finished_filler_job_adds_its_top_findings_to_the_index(self):
        (self.home).mkdir()
        (self.home / "backlog-seed.json").write_text(json.dumps({"filler": {
            "report_dir": str(self.tmp / "rep"), "repos": {"r": {"workspace": str(self.ws), "modules": ["x"]}},
        }}), encoding="utf-8")
        write_lines(self.ws / "x" / "a.py", 3)
        broker = B.Broker(self.home, launcher=mock.Mock(), now=lambda: NOW, proxy=lambda: None,
                          gauges=mock.Mock(read=lambda ttl: (90.0, 0.1)), launch_pool=mock.Mock(),
                          labeler=lambda kind, slug: f"hub-exec-{slug}")
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        run_dir = self.home / "run"
        run_dir.mkdir()
        db.insert_job(conn, {
            "id": "fj", "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "hub-exec-backlog-filler-bugs-r-x", "workspace": str(self.ws), "session": "backlog",
            "harness": "backlog", "prompt_path": "/p",
        }, NOW)
        conn.execute("UPDATE jobs SET state = 'running', run_dir = ?, started_at = ? WHERE id = 'fj'",
                     (str(run_dir), NOW))
        job = db.get_job(conn, "fj")
        broker._finish(conn, job, "- **high** `x/a.py:2` — bad thing\nVERDICT: PASS\n", NOW + 5)
        index = (self.tmp / "rep" / "filler" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("hub-exec-backlog-filler-bugs-r-x", index)
        self.assertIn("`x/a.py:2` — bad thing", index)


class PrSourceUnitTests(unittest.TestCase):
    def test_merged_epoch_reads_gh_timestamps_and_tolerates_bad_rows(self):
        self.assertEqual(P.merged_epoch({"mergedAt": "1970-01-01T00:01:00Z"}), 60.0)
        self.assertEqual(P.merged_epoch({"mergedAt": "nope"}), 0.0)


if __name__ == "__main__":
    unittest.main()
