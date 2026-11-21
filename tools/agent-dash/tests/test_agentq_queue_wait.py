"""Queue wait from the first ``started`` event, not ``jobs.started_at``.

``jobs.started_at`` is overwritten when a job is requeued after a death or a stall, so a job that ran for an hour
and was requeued counted its second start as queue wait. The event log keeps every start; the first one is when
the job actually left the queue. Offline: a temp agentq home.
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import cli  # noqa: E402
from agentq import db  # noqa: E402

NOW = 1_800_000_000.0


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)

    def job(self, job_id, submitted_at, starts, started_at=None):
        """A job submitted at ``submitted_at`` with one ``started`` event per entry in ``starts``."""
        db.insert_job(self.conn, {
            "id": job_id, "lane": "normal", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": job_id, "workspace": str(self.home), "session": "s", "harness": "pi", "prompt_path": "/p",
        }, submitted_at)
        for ts in starts:
            db.add_event(self.conn, job_id, "started", None, ts)
        if started_at is not None:
            self.conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (started_at, job_id))
        self.conn.commit()


class FirstStartTests(Temp):
    def test_the_first_start_is_the_earliest_started_event(self):
        self.job("j1", NOW, [NOW + 10, NOW + 500])
        self.assertEqual(db.first_started_at(self.conn, "j1"), NOW + 10)

    def test_a_job_that_never_started_has_no_first_start(self):
        self.job("j1", NOW, [])
        self.assertIsNone(db.first_started_at(self.conn, "j1"))


class QueueWaitTests(Temp):
    def test_wait_is_measured_from_the_first_start_not_the_last(self):
        # Submitted at NOW, started at +10, died, requeued, started again at +3600. The wait is 10 s, not 3600 s.
        self.job("j1", NOW, [NOW + 10, NOW + 3600], started_at=NOW + 3600)
        stats = db.queue_wait_stats(self.conn, NOW - 1)
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["p50"], 10.0)
        self.assertEqual(stats["max"], 10.0)

    def test_a_job_that_never_started_is_left_out(self):
        self.job("j1", NOW, [])
        self.assertEqual(db.queue_wait_stats(self.conn, NOW - 1)["count"], 0)

    def test_jobs_outside_the_window_are_left_out(self):
        self.job("old", NOW - 100, [NOW - 90])
        self.job("new", NOW, [NOW + 5])
        stats = db.queue_wait_stats(self.conn, NOW - 1)
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["p50"], 5.0)

    def test_the_percentiles_are_nearest_rank(self):
        for n in range(1, 101):
            self.job(f"j{n}", NOW, [NOW + n])
        stats = db.queue_wait_stats(self.conn, NOW - 1)
        self.assertEqual(stats["count"], 100)
        self.assertEqual(stats["p50"], 50.0)
        self.assertEqual(stats["p90"], 90.0)
        self.assertEqual(stats["p99"], 99.0)
        self.assertEqual(stats["max"], 100.0)

    def test_an_empty_window_has_no_percentiles(self):
        stats = db.queue_wait_stats(self.conn, NOW)
        self.assertEqual(stats, {"count": 0, "p50": None, "p90": None, "p99": None, "max": None})


class StatusLineTests(Temp):
    def test_status_prints_the_queue_wait_from_the_first_start(self):
        self.job("j1", NOW - 100, [NOW - 40, NOW - 5], started_at=NOW - 5)
        out = io.StringIO()
        # Exit 1 is the daemon-not-running answer; the wait line is what this test is about.
        cli.main(["status"], env={"AGENTQ_HOME": str(self.home)}, out=out, now=lambda: NOW)
        self.assertIn("queue wait 48h (first start): p50 60s", out.getvalue())


if __name__ == "__main__":
    unittest.main()
