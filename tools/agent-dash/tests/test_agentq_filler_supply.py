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
import threading
import time
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
        self.assertEqual(work.report_path,
                         str(self.tmp / "rep" / "filler" / "r" / "docs" / f"{F.module_slug('pkg')}.md"))
        self.assertEqual(work.slug, f"backlog-filler-docs-{F.module_slug('r')}-{F.module_slug('pkg')}")
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
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
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
    """A finished filler job is indexed from the report location recorded at submission, even when the live
    settings are not loaded (a cold start, or a restart that adopts the job)."""

    def finished_job(self, conn, report_dir):
        write_lines(self.ws / "x" / "a.py", 3)
        db.insert_job(conn, {
            "id": "fj", "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "hub-exec-backlog-filler-bugs-r-x", "workspace": str(self.ws), "session": "backlog",
            "harness": "backlog", "prompt_path": "/p",
        }, NOW)
        work = F.Work(kind="bugs", repo="r", workspace=str(self.ws), module="x", commit="c", report_path="",
                      slug="s", key="r/x#bugs", report_dir=report_dir)
        F.record_filler_job(conn, "fj", work)
        run_dir = self.tmp / "run"
        run_dir.mkdir(exist_ok=True)
        conn.execute("UPDATE jobs SET state = 'running', started_at = ?, run_dir = ? WHERE id = 'fj'",
                     (NOW, str(run_dir)))
        return db.get_job(conn, "fj")

    def broker(self):
        return B.Broker(self.home, launcher=mock.Mock(), now=lambda: NOW, proxy=lambda: None,
                        gauges=mock.Mock(read=lambda ttl: (90.0, 0.1)), launch_pool=mock.Mock(),
                        labeler=lambda kind, slug: f"hub-exec-{slug}")

    def test_a_finished_job_is_indexed_when_the_live_settings_are_not_loaded(self):
        self.home.mkdir()
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        rep = str(self.tmp / "rep")
        job = self.finished_job(conn, rep)
        broker = self.broker()
        self.assertEqual(broker._filler_settings().targets, [])
        broker._finish(conn, job, "- **high** `x/a.py:2` — bad thing\nVERDICT: PASS\n", NOW + 5)
        index = (self.tmp / "rep" / "filler" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("hub-exec-backlog-filler-bugs-r-x", index)
        self.assertIn("`x/a.py:2` — bad thing", index)
        self.assertIsNotNone(conn.execute("SELECT indexed_at FROM filler_jobs WHERE job_id = 'fj'").fetchone()[0])

    def test_a_failed_index_is_recorded_and_retried_by_a_later_tick(self):
        self.home.mkdir()
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        rep = self.tmp / "rep"
        (rep / "filler" / "INDEX.md").mkdir(parents=True)
        job = self.finished_job(conn, str(rep))
        broker = self.broker()
        broker._finish(conn, job, "- **high** `x/a.py:2` — bad thing\nVERDICT: PASS\n", NOW + 5)
        row = conn.execute("SELECT indexed_at, index_error FROM filler_jobs WHERE job_id = 'fj'").fetchone()
        self.assertIsNone(row["indexed_at"])
        self.assertIn("IsADirectoryError", row["index_error"])
        self.assertEqual(F.pending_index(conn), ["fj"])
        (rep / "filler" / "INDEX.md").rmdir()
        broker._retry_indexing(conn, NOW + 5 + B.INDEX_RETRY_S + 1)
        self.assertEqual(F.pending_index(conn), [])
        self.assertIn("bad thing", (rep / "filler" / "INDEX.md").read_text(encoding="utf-8"))


class ArchiveSafetyTests(Temp):
    def fill(self, rep, count, max_entries=2):
        for n in range(count):
            F.append_index(rep, f"job {n}", [("low", f"f{n}.py:1", f"title {n}")], NOW + n, max_entries=max_entries,
                           job_id=f"id{n}")

    def test_an_archive_failure_leaves_the_index_unchanged_and_loses_nothing(self):
        rep = str(self.tmp / "rep")
        self.fill(rep, 2)
        index = self.tmp / "rep" / "filler" / "INDEX.md"
        before = index.read_bytes()
        archive = self.tmp / "rep" / "filler" / f"INDEX-{time.strftime('%Y-%m', time.gmtime(NOW))}.md"
        archive.mkdir()
        with self.assertRaises(OSError):
            F.append_index(rep, "job 2", [("low", "f2.py:1", "title 2")], NOW + 2, max_entries=2, job_id="id2")
        self.assertEqual(index.read_bytes(), before)
        archive.rmdir()
        F.append_index(rep, "job 2", [("low", "f2.py:1", "title 2")], NOW + 2, max_entries=2, job_id="id2")
        held = (self.tmp / "rep" / "filler" / f"INDEX-{time.strftime('%Y-%m', time.gmtime(NOW))}.md").read_text(
            encoding="utf-8")
        live = index.read_text(encoding="utf-8")
        self.assertIn("title 0", held)
        self.assertIn("title 2", live)
        self.assertNotIn("title 0", live)

    def test_a_crash_between_archive_and_replace_does_not_duplicate_on_retry(self):
        rep = str(self.tmp / "rep")
        self.fill(rep, 2)
        index = self.tmp / "rep" / "filler" / "INDEX.md"
        before = index.read_text(encoding="utf-8")
        F.append_index(rep, "job 2", [("low", "f2.py:1", "title 2")], NOW + 2, max_entries=2, job_id="id2")
        index.write_text(before, encoding="utf-8")
        F.append_index(rep, "job 2", [("low", "f2.py:1", "title 2")], NOW + 2, max_entries=2, job_id="id2")
        archive = (self.tmp / "rep" / "filler" / f"INDEX-{time.strftime('%Y-%m', time.gmtime(NOW))}.md").read_text(
            encoding="utf-8")
        self.assertEqual(archive.count("title 0"), 1)
        self.assertNotIn("title 1", archive)
        self.assertNotIn("title 2", archive)


class RefreshGuardTests(Temp):
    def seed(self, **filler):
        self.home.mkdir(exist_ok=True)
        base = {"report_dir": str(self.tmp / "rep"), "repos": {"r": {"workspace": str(self.ws), "modules": ["pkg"]}}}
        base.update(filler)
        path = self.home / "backlog-seed.json"
        path.write_text(json.dumps({"filler": base}), encoding="utf-8")
        stamp = time.time() + self.bump
        self.bump += 10
        os.utime(path, (stamp, stamp))

    def setUp(self):
        super().setUp()
        self.bump = 0
        write_lines(self.ws / "pkg" / "a.py", 10)
        self.logged = []

    def cache(self, now):
        return F.WorkCache(self.home, now=lambda: now[0], synchronous=True, log=self.logged.append)

    def test_the_reviewers_one_e308_window_is_refused_logged_and_keeps_the_snapshot(self):
        self.seed()
        now = [NOW]
        cache = self.cache(now)
        settings, _ = cache.snapshot()
        self.assertEqual([t.module for t in settings.targets], ["pkg"])
        self.seed(pr_window_h=1e308)
        now[0] = NOW + 10
        settings, prs = cache.snapshot()
        self.assertEqual([t.module for t in settings.targets], ["pkg"])
        self.assertEqual(len(self.logged), 1)
        self.assertIn("filler refresh failed: ValueError", self.logged[0])
        self.assertIn("pr_window_h", self.logged[0])

    def test_an_unexpected_error_is_logged_and_does_not_escape_the_refresh(self):
        self.seed()
        now = [NOW]
        cache = self.cache(now)
        with mock.patch.object(F, "load_settings", side_effect=OverflowError("boom")):
            now[0] = NOW + 5
            cache._refresh(now[0])
        self.assertEqual(self.logged, ["filler refresh failed: OverflowError: boom"])

    def test_failures_back_off_from_sixty_seconds_doubling_to_thirty_minutes(self):
        self.seed()
        now = [NOW]
        cache = self.cache(now)
        now[0] = NOW + 2 * F.PR_REFRESH_S
        calls = []

        def failing(config):
            calls.append(now[0])
            raise RuntimeError("down")
        with mock.patch.object(F, "load_settings", failing):
            for _ in range(12):
                now[0] = cache._next_attempt_at or now[0]
                cache.snapshot()
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertEqual(gaps[:5], [60.0, 120.0, 240.0, 480.0, 960.0])
        self.assertEqual(max(gaps), F.REFRESH_BACKOFF_MAX_S)

    def test_no_retry_happens_inside_the_backoff_window_but_a_seed_change_retries_at_once(self):
        self.seed()
        now = [NOW]
        cache = self.cache(now)
        cache.snapshot()
        calls = []

        def failing(config):
            calls.append(now[0])
            raise RuntimeError("down")
        with mock.patch.object(F, "load_settings", failing):
            self.seed(scan_max_files=20000)
            now[0] = NOW + 5
            cache.snapshot()
            now[0] = NOW + 30
            cache.snapshot()
            self.assertEqual(len(calls), 1)
            self.seed(scan_max_files=19999)
            now[0] = NOW + 31
            cache.snapshot()
            self.assertEqual(len(calls), 2)

    def test_the_same_error_is_logged_once_and_a_new_one_is_logged_again(self):
        self.seed()
        now = [NOW]
        cache = self.cache(now)
        cache.snapshot()
        messages = iter(["one", "one", "two"])

        def failing(config):
            raise RuntimeError(next(messages))
        now[0] = NOW + 2 * F.PR_REFRESH_S
        with mock.patch.object(F, "load_settings", failing):
            for _ in range(3):
                now[0] = (cache._next_attempt_at or now[0]) + 1
                cache.snapshot()
        self.assertEqual(self.logged, ["filler refresh failed: RuntimeError: one",
                                       "filler refresh failed: RuntimeError: two"])


class ConfigBoundsTests(unittest.TestCase):
    def config(self, **filler):
        return {"filler": {"report_dir": "/tmp/rep", "repos": {"r": {"workspace": "/tmp", "modules": []}}, **filler}}

    def test_a_pr_window_beyond_720_hours_is_refused(self):
        with self.assertRaisesRegex(ValueError, "pr_window_h"):
            F.load_settings(self.config(pr_window_h=721))
        self.assertEqual(F.load_settings(self.config(pr_window_h=720)).pr_window_h, 720.0)

    def test_scan_budgets_must_be_positive_and_within_their_ceilings(self):
        with self.assertRaisesRegex(ValueError, "scan_max_files"):
            F.load_settings(self.config(scan_max_files=0))
        with self.assertRaisesRegex(ValueError, "scan_max_mb"):
            F.load_settings(self.config(scan_max_mb=-1))
        with self.assertRaisesRegex(ValueError, "scan_max_seconds"):
            F.load_settings(self.config(scan_max_seconds=1e9))

    def test_non_finite_numbers_are_refused(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            F.load_settings(self.config(pr_window_h=float("nan")))
        with self.assertRaisesRegex(ValueError, "finite"):
            F.load_settings(self.config(scan_max_seconds=float("inf")))

    def test_the_configured_budgets_reach_the_settings(self):
        settings = F.load_settings(self.config(scan_max_files=500, scan_max_mb=2, scan_max_seconds=1.5))
        self.assertEqual((settings.scan_max_files, settings.scan_max_bytes, settings.scan_max_seconds),
                         (500, 2 * 2**20, 1.5))


class PromptSizeTests(unittest.TestCase):
    """Every filler brief asks for the terse report, and stays short enough that its output stays lean."""

    RULES = ("at most 10 findings", "under 60 lines", "5 lines", "why:")

    def rendered(self, kind):
        target = F.Target(repo="r", module="pkg/a.py", workspace="/ws", report_dir="/rep")
        if kind == "pr":
            work = F.pr_work({"number": 7, "title": "t", "url": "u", "repo": "r", "slug": "o/r", "workspace": "/ws",
                              "report_dir": "/rep", "merge_commit": "c"})
        else:
            work = F.audit_work(target, kind, "c0ffee")
        return F.prompt_for(work)

    def test_every_kind_carries_the_terse_rules(self):
        for kind in ("bugs", "tests", "docs", "risk", "pr"):
            text = self.rendered(kind)
            for rule in self.RULES:
                self.assertIn(rule, text, f"{kind} brief is missing {rule!r}")

    def test_every_brief_is_short_enough(self):
        for kind in ("bugs", "tests", "docs", "risk", "pr"):
            text = self.rendered(kind)
            self.assertLessEqual(len(text), 2200, f"{kind} brief is {len(text)} characters")
            self.assertLessEqual(len(text.splitlines()), 30, f"{kind} brief has too many lines")

    def test_a_terse_finding_still_parses_into_the_index_with_its_why(self):
        line = "- **high** `pkg/a.py:12` — unchecked error — why: the write is never checked"
        self.assertEqual(F.parse_findings(line), [("high", "pkg/a.py:12",
                                                   "unchecked error — why: the write is never checked")])


class PrSourceUnitTests(unittest.TestCase):
    def test_merged_epoch_reads_gh_timestamps_and_tolerates_bad_rows(self):
        self.assertEqual(P.merged_epoch({"mergedAt": "1970-01-01T00:01:00Z"}), 60.0)
        self.assertEqual(P.merged_epoch({"mergedAt": "nope"}), 0.0)


class ScanSafetyTests(Temp):
    """A FIFO, a slow tree and an over-budget tree never block the caller, and only regular code files count."""

    def test_a_fifo_named_like_code_is_not_counted_or_opened(self):
        write_lines(self.ws / "pkg" / "real.py", 10)
        os.mkfifo(self.ws / "pkg" / "pipe.py")
        result = {}

        def scan():
            result["files"] = F._module_code_files(self.ws, "pkg", F.ScanBudget(), run=fake_git(set()))
        worker = threading.Thread(target=scan, daemon=True)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive(), "scan blocked on a FIFO")
        self.assertEqual([rel for rel, _ in result["files"]], ["pkg/real.py"])

    def test_a_file_budget_overrun_keeps_the_module_whole(self):
        for i in range(10):
            write_lines(self.ws / "big" / f"c{i}" / "m.py", 50)
        self.assertEqual(F.split_module(str(self.ws), "big", 100, 5, budget=F.ScanBudget(max_files=3)), ["big"])
        self.assertGreater(len(F.split_module(str(self.ws), "big", 100, 5)), 1)

    def test_a_slow_tree_over_the_time_budget_keeps_the_module_whole(self):
        ticks = iter(range(100))
        budget = F.ScanBudget(max_seconds=2.5, clock=lambda: float(next(ticks)))
        for i in range(8):
            write_lines(self.ws / "slow" / f"c{i}" / "m.py", 50)
        self.assertEqual(F.split_module(str(self.ws), "slow", 100, 5, budget=budget), ["slow"])

    def test_a_slow_file_read_is_stopped_by_the_time_budget(self):
        for i in range(30):
            write_lines(self.ws / "sleepy" / f"c{i}" / "m.py", 10)
        real_count = F._count_lines

        def slow(path):
            time.sleep(0.02)
            return real_count(path)
        started = time.monotonic()
        with mock.patch.object(F, "_count_lines", slow):
            result = F.split_module(str(self.ws), "sleepy", 100, 5, budget=F.ScanBudget(max_seconds=0.1))
        self.assertEqual(result, ["sleepy"])
        self.assertLess(time.monotonic() - started, 3.0)

    def test_the_settings_refresh_runs_off_the_caller_thread(self):
        self.home.mkdir()
        (self.home / "backlog-seed.json").write_text(json.dumps({"filler": {
            "report_dir": str(self.tmp / "rep"), "repos": {"r": {"workspace": str(self.ws), "modules": []}},
        }}), encoding="utf-8")
        real_load = F.load_settings

        def slow_load(config):
            time.sleep(1.0)
            return real_load(config)
        cache = F.WorkCache(self.home)
        started = time.monotonic()
        with mock.patch.object(F, "load_settings", slow_load):
            settings, prs = cache.snapshot()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(settings.targets, [])
            cache._thread.join(5)
        settings, prs = cache.snapshot()
        self.assertEqual(prs, [])
        self.assertEqual(len(settings.kinds), 4)


def fake_git(ignored):
    """A stand-in for ``git check-ignore --stdin``: it prints the paths in ``ignored`` that it is given."""
    def run(cmd, input="", **kwargs):
        lines = [line for line in input.splitlines() if line in ignored]
        return subprocess.CompletedProcess(cmd, 0, "\n".join(lines) + ("\n" if lines else ""), "")
    return run


class ExclusionTests(Temp):
    def test_generated_and_vendored_folders_are_pruned(self):
        write_lines(self.ws / "pkg" / "real.py", 10)
        for folder in ("target", "dbt_packages", "build", "dist", ".venv", "node_modules", "__pycache__",
                       "site-packages", "vendor"):
            write_lines(self.ws / "pkg" / folder / "generated.py", 500)
        files = F._module_code_files(self.ws, "pkg", F.ScanBudget(), run=fake_git(set()))
        self.assertEqual([rel for rel, _ in files], ["pkg/real.py"])

    def test_git_ignored_files_are_dropped_with_one_check_ignore_call(self):
        write_lines(self.ws / "pkg" / "real.py", 10)
        write_lines(self.ws / "pkg" / "gen" / "out.py", 10)
        calls = []

        def run(cmd, input="", **kwargs):
            calls.append(cmd)
            return fake_git({"pkg/gen/out.py"})(cmd, input=input)
        files = F._module_code_files(self.ws, "pkg", F.ScanBudget(), run=run)
        self.assertEqual([rel for rel, _ in files], ["pkg/real.py"])
        self.assertEqual(len([c for c in calls if "check-ignore" in c]), 1)

    def test_real_git_ignores_a_file_when_the_workspace_is_a_repo(self):
        subprocess.run(["git", "-C", str(self.ws), "init", "-q"], check=True)
        (self.ws / ".gitignore").write_text("gen/\n", encoding="utf-8")
        write_lines(self.ws / "pkg" / "real.py", 10)
        write_lines(self.ws / "pkg" / "gen" / "out.py", 10)
        files = F._module_code_files(self.ws, "pkg", F.ScanBudget())
        self.assertEqual([rel for rel, _ in files], ["pkg/real.py"])

    def test_a_symlink_leaving_the_workspace_is_rejected_and_one_inside_is_read(self):
        outside = self.tmp / "outside"
        write_lines(outside / "secret.py", 10)
        write_lines(self.ws / "pkg" / "real.py", 10)
        os.symlink(outside / "secret.py", self.ws / "pkg" / "linked.py")
        os.symlink(self.ws / "pkg" / "real.py", self.ws / "pkg" / "alias.py")
        os.symlink(outside, self.ws / "pkg" / "linkdir")
        files = F._module_code_files(self.ws, "pkg", F.ScanBudget(), run=fake_git(set()))
        self.assertEqual([rel for rel, _ in files], ["pkg/alias.py", "pkg/real.py"])

    def test_a_module_that_is_a_symlink_outside_the_workspace_yields_no_target(self):
        outside = self.tmp / "outside"
        write_lines(outside / "secret.py", 10)
        os.symlink(outside, self.ws / "linkmod")
        self.assertEqual(F.split_module(str(self.ws), "linkmod", 4000, 300), [])


class SlugCollisionTests(unittest.TestCase):
    def test_paths_that_share_a_long_prefix_get_different_slugs_and_report_paths(self):
        shared = "packages/" + "x" * 80
        a, b = F.module_slug(shared + "/alpha"), F.module_slug(shared + "/beta")
        self.assertNotEqual(a, b)
        target_a = F.Target(repo="r", module=shared + "/alpha", workspace="", report_dir="/rep")
        target_b = F.Target(repo="r", module=shared + "/beta", workspace="", report_dir="/rep")
        self.assertNotEqual(target_a.report_path("bugs"), target_b.report_path("bugs"))
        self.assertNotEqual(F.label_slug(target_a), F.label_slug(target_b))

    def test_every_slug_ends_in_an_eight_hex_hash_and_keeps_a_readable_start(self):
        for text in ("pkg/a", "root", "a" * 200):
            slug = F.module_slug(text)
            self.assertRegex(slug, r"-[0-9a-f]{8}$")
        self.assertTrue(F.module_slug("pkg/a").startswith("pkg-a-"))
        self.assertEqual(F.module_slug("pkg/a"), F.module_slug("pkg/a"))


class IndexSafetyTests(Temp):
    def test_two_concurrent_appenders_keep_both_findings(self):
        rep = str(self.tmp / "rep")
        barrier = threading.Barrier(8)
        errors = []

        def append(n):
            try:
                barrier.wait()
                F.append_index(rep, f"job {n}", [("high", f"f{n}.py:1", f"title number {n}")], NOW + n)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        text = (self.tmp / "rep" / "filler" / "INDEX.md").read_text(encoding="utf-8")
        for n in range(8):
            self.assertIn(f"title number {n}", text)
        self.assertEqual(sorted(p.name for p in (self.tmp / "rep" / "filler").iterdir() if "tmp" in p.name), [])

    def test_the_rewrite_is_atomic_and_leaves_no_temporary_file_behind(self):
        rep = str(self.tmp / "rep")
        F.append_index(rep, "one", [("low", "a.py:1", "first")], NOW)
        F.append_index(rep, "two", [("low", "b.py:1", "second")], NOW + 1)
        names = sorted(p.name for p in (self.tmp / "rep" / "filler").iterdir())
        self.assertEqual(names, ["INDEX.lock", "INDEX.md"])


class IndexRetentionTests(Temp):
    def test_the_index_keeps_only_the_newest_entries_and_archives_the_rest(self):
        rep = str(self.tmp / "rep")
        for n in range(5):
            F.append_index(rep, f"job {n}", [("low", f"f{n}.py:1", f"title {n}")], NOW + n, max_entries=3)
        text = (self.tmp / "rep" / "filler" / "INDEX.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("- **low**"), 3)
        self.assertIn("title 4", text)
        self.assertNotIn("title 0", text)
        archive = (self.tmp / "rep" / "filler" / f"INDEX-{time.strftime('%Y-%m', time.gmtime(NOW))}.md")
        archived = archive.read_text(encoding="utf-8")
        self.assertIn("title 0", archived)
        self.assertIn("title 1", archived)

    def test_entries_from_an_older_month_go_to_their_own_archive(self):
        rep = str(self.tmp / "rep")
        F.append_index(rep, "january", [("low", "a.py:1", "old one")], NOW, max_entries=1)
        F.append_index(rep, "february", [("low", "b.py:1", "new one")], NOW + 40 * 86400, max_entries=1)
        index = (self.tmp / "rep" / "filler" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("new one", index)
        self.assertNotIn("old one", index)
        self.assertIn("old one", (self.tmp / "rep" / "filler" / "INDEX-2027-01.md").read_text(encoding="utf-8"))

    def test_the_default_retention_is_two_thousand_entries(self):
        self.assertEqual(F.INDEX_MAX_ENTRIES, 2000)


if __name__ == "__main__":
    unittest.main()


class QuotaTests(Temp):
    """One priority-ordered due list feeds every filler source, and it returns exactly the count the controller asked for."""

    def targets(self, n):
        return [F.Target(repo="r", module=f"m{i:04d}", workspace=str(self.ws), report_dir=str(self.tmp / "rep"))
                for i in range(n)]

    def prs(self, n):
        return [{"number": 1000 + i, "title": f"pr {i}", "url": f"u{i}", "mergedAt": "1970-01-02T00:00:00Z",
                 "merge_commit": f"c{i}", "repo": "r", "slug": "o/r", "workspace": str(self.ws),
                 "report_dir": str(self.tmp / "rep")} for i in range(n)]

    def store(self):
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        return conn

    def test_a_hundred_due_prs_and_six_thousand_pairs_still_give_exactly_the_count_asked(self):
        conn = self.store()
        works = F.due_works(conn, self.targets(1500), prs=self.prs(100), head_of=lambda t: "h1", count=4)
        self.assertEqual(len(works), 4)
        self.assertEqual([w.kind for w in works], ["pr"] * 4)
        self.assertEqual([w.pr_number for w in works], [1000, 1001, 1002, 1003])

    def test_with_no_pr_due_the_pairs_spread_across_targets_before_repeating_one(self):
        conn = self.store()
        works = F.due_works(conn, self.targets(3), prs=(), head_of=lambda t: "h1", count=4)
        self.assertEqual(len(works), 4)
        self.assertEqual(sorted({w.module for w in works}), ["m0000", "m0001", "m0002"])
        self.assertEqual(len({(w.module, w.kind) for w in works}), 4)

    def test_a_count_of_zero_submits_nothing(self):
        self.assertEqual(F.due_works(self.store(), self.targets(5), prs=self.prs(5), head_of=lambda t: "h", count=0),
                         [])

    def test_a_pr_review_is_labelled_as_filler_so_it_counts_as_queued_filler(self):
        work = F.pr_work(self.prs(1)[0])
        self.assertTrue(F.is_filler_label("hub-exec-" + work.slug))
        self.assertIn("-backlog-filler-pr-", "hub-exec-" + work.slug)

    def test_queued_filler_of_any_kind_is_counted_through_its_record(self):
        conn = self.store()
        for job_id, label in (("pr-old", "hub-exec-pr-r-1"), ("pr-new", "hub-exec-backlog-filler-pr-r-2")):
            db.insert_job(conn, {
                "id": job_id, "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
                "label": label, "workspace": str(self.ws), "session": "backlog", "harness": "backlog",
                "prompt_path": "/p",
            }, NOW)
            with db.tx(conn):
                F.record_filler_job(conn, job_id, F.pr_work(self.prs(1)[0]))
        from agentq import fill as FL
        counts = FL.job_counts(conn, NOW)
        self.assertEqual(counts["queued_filler"], 2)
        self.assertEqual(F.queued_filler_ids(conn), {"pr-old", "pr-new"})
