"""``agentq gc``: plan, dry run, apply. Offline; every test uses a temp AGENTQ_HOME."""

import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq import cli, db
from agentq import gc as G

NOW = 1_800_000_000.0
#: A job id shaped like the ones `agentq submit` generates.
JOB = "0a1b2c3d4e5f"
WEEK = 7 * 86400.0


def finished_job(job_id: str, *, state: str = "done", finished_at: float = NOW - 2 * WEEK) -> dict:
    return {"id": job_id, "state": state, "finished_at": finished_at}


def make_stream(home: Path, job_id: str, name: str = "stream-label.ndjson", *, size: int = 100,
                mtime: float = NOW - 2 * WEEK, run: str = "run1") -> Path:
    folder = Path(home) / "jobs" / job_id / "runs" / run / "pi"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("x" * size)
    os.utime(path, (mtime, mtime))
    return path


class DurationTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(G.parse_duration("30m"), 1800.0)
        self.assertEqual(G.parse_duration("12h"), 43200.0)
        self.assertEqual(G.parse_duration("7d"), WEEK)
        self.assertEqual(G.parse_duration("2w"), 2 * WEEK)
        self.assertEqual(G.parse_duration("45s"), 45.0)

    def test_a_bare_number_is_days(self) -> None:
        self.assertEqual(G.parse_duration("3"), 3 * 86400.0)

    def test_garbage_is_rejected_with_a_readable_message(self) -> None:
        for text in ("", "week", "7x", "-1d", "1d2h"):
            with self.assertRaises(G.DurationError, msg=text) as caught:
                G.parse_duration(text)
            self.assertIn("duration like 30m, 12h or 7d", str(caught.exception))


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def test_an_old_finished_job_plans_its_streams_only(self) -> None:
        stream = make_stream(self.home, JOB)
        report = stream.parent.parent / "report.md"
        report.write_text("PASS\n")
        out = stream.parent.parent / "out-j1.md"
        out.write_text("PASS\n")
        items, skipped = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual([item["path"] for item in items], [str(stream)])
        self.assertEqual(items[0]["size"], 100)
        self.assertEqual(items[0]["job_id"], JOB)
        self.assertEqual(skipped, [])
        self.assertTrue(report.exists() and out.exists())

    def test_a_jsonl_stream_is_a_candidate_too(self) -> None:
        stream = make_stream(self.home, JOB, "stream-label.jsonl")
        items, _ = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual([item["path"] for item in items], [str(stream)])

    def test_unfinished_jobs_are_skipped(self) -> None:
        make_stream(self.home, JOB)
        for state in ("queued", "running", "starting", None):
            items, skipped = G.plan(self.home, [finished_job(JOB, state=state)], older_than_s=WEEK, now=NOW)
            self.assertEqual(items, [], state)
            self.assertEqual(skipped[0]["reason"], f"state={state}")

    def test_a_recently_finished_job_is_skipped(self) -> None:
        make_stream(self.home, JOB)
        items, skipped = G.plan(
            self.home, [finished_job(JOB, finished_at=NOW - 86400)], older_than_s=WEEK, now=NOW
        )
        self.assertEqual(items, [])
        self.assertEqual(skipped[0]["reason"], "finished recently")

    def test_a_job_without_finished_at_is_skipped(self) -> None:
        make_stream(self.home, JOB)
        items, skipped = G.plan(self.home, [finished_job(JOB, finished_at=None)], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertEqual(skipped[0]["reason"], "finished recently")

    def test_a_live_run_folder_is_never_planned(self) -> None:
        stream = make_stream(self.home, JOB)
        running = stream.parent.parent / "running"
        running.mkdir()
        (running / "label").write_text(f"{os.getpid()}\tmodel\n")
        items, skipped = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertTrue(skipped[0]["reason"].startswith("live:"))
        self.assertTrue(stream.exists())

    def test_a_dead_pid_file_does_not_keep_the_run(self) -> None:
        stream = make_stream(self.home, JOB)
        running = stream.parent.parent / "running"
        running.mkdir()
        (running / "label").write_text("999999999\tmodel\n")
        items, _ = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual([item["path"] for item in items], [str(stream)])

    def test_a_stream_modified_after_the_cutoff_is_skipped(self) -> None:
        make_stream(self.home, JOB, mtime=NOW - 3600)
        items, skipped = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertTrue(skipped[0]["reason"].startswith("modified recently:"))

    def test_other_files_and_symlinks_are_ignored(self) -> None:
        folder = self.home / "jobs" / JOB / "runs" / "run1" / "pi"
        folder.mkdir(parents=True)
        keep = folder / "transcript.txt"
        keep.write_text("keep")
        outside = Path(self._tmp.name) / "outside.ndjson"
        outside.write_text("x")
        os.utime(outside, (NOW - 2 * WEEK, NOW - 2 * WEEK))
        (folder / "stream-link.ndjson").symlink_to(outside)
        linked_dir = self.home / "jobs" / JOB / "runs" / "linked"
        linked_dir.symlink_to(outside.parent, target_is_directory=True)
        items, _ = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertTrue(keep.exists() and outside.exists())

    def test_a_job_without_a_runs_folder_is_quietly_passed_over(self) -> None:
        items, skipped = G.plan(self.home, [finished_job("deadbeefcafe")], older_than_s=WEEK, now=NOW)
        self.assertEqual((items, skipped), ([], []))

    def test_an_explicit_live_predicate_is_used(self) -> None:
        make_stream(self.home, JOB)
        items, skipped = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW, live=lambda _: True)
        self.assertEqual(items, [])
        self.assertTrue(skipped)


class ContainmentTest(unittest.TestCase):
    """A candidate must stay inside ``$AGENTQ_HOME/jobs/<id>/runs``, symlinks resolved."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()

    def outside_stream(self, name: str = "stream-victim.ndjson") -> Path:
        folder = self.base / "victim" / "runs" / "run1" / "pi"
        folder.mkdir(parents=True)
        path = folder / name
        path.write_text("x" * 100)
        os.utime(path, (NOW - 2 * WEEK, NOW - 2 * WEEK))
        return path

    def test_a_traversal_id_plans_and_removes_nothing_outside(self) -> None:
        victim = self.outside_stream()
        items, skipped = G.plan(self.home, [finished_job("../../victim")], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertEqual(skipped[0]["reason"], "job id is not plain hex")
        removed, freed, errors = G.apply(items)
        self.assertEqual((removed, freed, errors), (0, 0, []))
        self.assertTrue(victim.exists())

    def test_a_id_with_uppercase_or_short_parts_is_rejected(self) -> None:
        for job_id in ("ABCDEF", "abc", "a/b", "0a1b-2c3d", "0a1b2c3d\n"):
            items, skipped = G.plan(self.home, [finished_job(job_id)], older_than_s=WEEK, now=NOW)
            self.assertEqual(items, [], job_id)
            self.assertEqual(skipped[0]["reason"], "job id is not plain hex", job_id)

    def test_a_symlinked_runs_folder_is_never_followed(self) -> None:
        victim = self.outside_stream()
        (self.home / "jobs" / JOB).mkdir(parents=True)
        (self.home / "jobs" / JOB / "runs").symlink_to(victim.parent.parent.parent, target_is_directory=True)
        items, skipped = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertEqual(skipped[0]["reason"], "runs folder is a symlink")
        removed, _, errors = G.apply(items)
        self.assertEqual((removed, errors), (0, []))
        self.assertTrue(victim.exists())

    def test_a_symlinked_job_folder_is_rejected_by_resolution(self) -> None:
        victim = self.outside_stream()
        outside_job = victim.parent.parent.parent.parent
        (self.home / "jobs").mkdir(parents=True)
        (self.home / "jobs" / JOB).symlink_to(outside_job, target_is_directory=True)
        items, skipped = G.plan(self.home, [finished_job(JOB)], older_than_s=WEEK, now=NOW)
        self.assertEqual(items, [])
        self.assertEqual(skipped[0]["reason"], "runs folder resolves outside the jobs tree")
        removed, _, _ = G.apply(items)
        self.assertEqual(removed, 0)
        self.assertTrue(victim.exists())


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def item(self, name: str, size: int = 10) -> dict:
        path = self.root / name
        path.write_text("x" * size)
        return {
            "job_id": JOB,
            "root": str(self.root),
            "path": str(path),
            "size": size,
            "finished_at": NOW - 2 * WEEK,
        }

    def test_apply_removes_planned_files_and_counts_bytes(self) -> None:
        items = [self.item("stream-a.ndjson", 10), self.item("stream-b.ndjson", 20)]
        removed, freed, errors = G.apply(items)
        self.assertEqual((removed, freed, errors), (2, 30, []))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_vanished_file_is_not_an_error(self) -> None:
        item = self.item("stream-a.ndjson")
        Path(item["path"]).unlink()
        removed, freed, errors = G.apply([item])
        self.assertEqual((removed, freed, errors), (0, 0, []))

    def test_an_item_outside_its_planned_root_is_refused(self) -> None:
        victim = Path(self._tmp.name) / "victim"
        victim.mkdir()
        stream = victim / "stream-victim.ndjson"
        stream.write_text("x")
        root = Path(self._tmp.name) / "job" / "runs"
        root.mkdir(parents=True)
        item = {"job_id": JOB, "root": str(root), "path": str(stream), "size": 1, "finished_at": NOW}
        removed, freed, errors = G.apply([item])
        self.assertEqual((removed, freed), (0, 0))
        self.assertEqual(errors[0]["error"], "outside the job runs tree")
        self.assertTrue(stream.exists())

    def test_an_item_without_a_root_is_refused(self) -> None:
        path = self.root / "stream-a.ndjson"
        path.write_text("x")
        removed, _, errors = G.apply([{"job_id": JOB, "path": str(path), "size": 1, "finished_at": NOW}])
        self.assertEqual(removed, 0)
        self.assertEqual(errors[0]["error"], "outside the job runs tree")
        self.assertTrue(path.exists())

    def test_an_unlink_error_is_reported_not_raised(self) -> None:
        items = [self.item("stream-a.ndjson"), self.item("stream-b.ndjson")]

        def unlink(path):
            if path.endswith("stream-a.ndjson"):
                raise PermissionError("nope")
            os.unlink(path)

        removed, freed, errors = G.apply(items, unlink=unlink)
        self.assertEqual((removed, freed), (1, 10))
        self.assertEqual(errors[0]["error"], "PermissionError")


class SummaryTest(unittest.TestCase):
    def test_human_bytes(self) -> None:
        self.assertEqual(G.human_bytes(0), "0 B")
        self.assertEqual(G.human_bytes(999), "999 B")
        self.assertEqual(G.human_bytes(1024), "1.0 KB")
        self.assertEqual(G.human_bytes(10 * 1024 * 1024), "10.0 MB")

    def test_summarize_ranks_the_largest_jobs(self) -> None:
        items = [
            {"job_id": "small", "size": 100, "finished_at": NOW},
            {"job_id": "big", "size": 5000, "finished_at": NOW},
            {"job_id": "big", "size": 5000, "finished_at": NOW},
        ]
        lines = G.summarize(items)
        self.assertIn("3 stream files", lines[0])
        self.assertIn("9.9 KB", lines[0])
        self.assertIn("2 finished jobs", lines[0])
        self.assertIn("big", lines[1])
        self.assertIn("small", lines[2])

    def test_summarize_caps_the_listing(self) -> None:
        items = [{"job_id": f"j{i}", "size": i, "finished_at": NOW} for i in range(5)]
        lines = G.summarize(items, limit=2)
        self.assertEqual(len(lines), 4)
        self.assertIn("3 more jobs", lines[-1])


class GcCommandTest(unittest.TestCase):
    """End to end through ``cli.main``: dry run leaves the files, --apply removes them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.env = {"AGENTQ_HOME": str(self.home), "PATH": os.environ["PATH"]}

    def insert_job(self, job_id: str, state: str, finished_at) -> None:
        conn = db.connect(self.home)
        conn.execute(
            "INSERT INTO jobs (id, queue_seq, lane, state, kind, res, tier, label, workspace, session,"
            " harness, prompt_path, submitted_at, priority_age_at, finished_at) VALUES"
            " (?, 0, 'normal', ?, 'exec', 'normal', 'claude', ?, 'ws', 'sess', 'pi', 'p.md', ?, 0, ?)",
            (job_id, state, job_id, NOW - 3 * WEEK, finished_at),
        )
        conn.commit()
        conn.close()

    def run_cli(self, argv):
        out = io.StringIO()
        code = cli.main(argv, env=self.env, out=out, now=lambda: NOW)
        return code, out.getvalue()

    def test_dry_run_reports_and_removes_nothing(self) -> None:
        self.insert_job("a1b2c3d4e5f0", "done", NOW - 2 * WEEK)
        self.insert_job("a1b2c3d4e5f1", "done", NOW - 3600)
        old = make_stream(self.home, "a1b2c3d4e5f0", size=230)
        new = make_stream(self.home, "a1b2c3d4e5f1", mtime=NOW - 3600)
        code, text = self.run_cli(["gc", "--runs-older-than", "7d"])
        self.assertEqual(code, 0, text)
        self.assertIn("1 stream files", text)
        self.assertIn("would remove 1 stream file(s)", text)
        self.assertIn("dry run: nothing removed", text)
        self.assertTrue(old.exists() and new.exists())

    def test_apply_removes_only_the_old_stream(self) -> None:
        self.insert_job("a1b2c3d4e5f0", "done", NOW - 2 * WEEK)
        self.insert_job("a1b2c3d4e5f1", "done", NOW - 3600)
        old = make_stream(self.home, "a1b2c3d4e5f0", size=230)
        new = make_stream(self.home, "a1b2c3d4e5f1", mtime=NOW - 3600)
        report = old.parent.parent / "report.md"
        report.write_text("PASS\n")
        code, text = self.run_cli(["gc", "--runs-older-than", "7d", "--apply"])
        self.assertEqual(code, 0, text)
        self.assertIn("removed 1 stream file(s)", text)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(report.exists())

    def test_a_queued_job_is_untouched(self) -> None:
        self.insert_job("a1b2c3d4e5f2", "queued", None)
        stream = make_stream(self.home, "a1b2c3d4e5f2")
        code, text = self.run_cli(["gc", "--apply"])
        self.assertEqual(code, 0, text)
        self.assertIn("removed 0 stream file(s)", text)
        self.assertTrue(stream.exists())

    def test_a_bad_duration_exits_two(self) -> None:
        code, text = self.run_cli(["gc", "--runs-older-than", "soon"])
        self.assertEqual(code, 2)
        self.assertIn("duration like 30m, 12h or 7d", text)

    def test_a_traversal_id_in_the_db_removes_nothing_outside(self) -> None:
        self.insert_job("../../victim", "done", NOW - 2 * WEEK)
        folder = Path(self._tmp.name) / "victim" / "runs" / "run1" / "pi"
        folder.mkdir(parents=True)
        victim = folder / "stream-victim.ndjson"
        victim.write_text("x" * 130)
        os.utime(victim, (NOW - 2 * WEEK, NOW - 2 * WEEK))
        code, text = self.run_cli(["gc", "--runs-older-than", "7d", "--apply"])
        self.assertEqual(code, 0, text)
        self.assertIn("removed 0 stream file(s)", text)
        self.assertTrue(victim.exists())

    def test_the_default_window_is_seven_days(self) -> None:
        self.insert_job("a1b2c3d4e5f6", "done", NOW - 6 * 86400)
        stream = make_stream(self.home, "a1b2c3d4e5f6", mtime=NOW - 6 * 86400)
        code, text = self.run_cli(["gc"])
        self.assertEqual(code, 0, text)
        self.assertIn("would remove 0 stream file(s)", text)
        self.assertTrue(stream.exists())


if __name__ == "__main__":
    unittest.main()
