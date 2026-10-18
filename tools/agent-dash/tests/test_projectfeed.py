"""Pure-builder tests for dash/projectfeed.py: bulk collapse, ordering,
limit, untimed omissions, and no doctor content. Synthetic bundles only."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import projectfeed  # noqa: E402

NOW = 1_761_999_960.0

BUNDLES = [
    {"id": "WS3", "title": "Gateway", "state": "active"},
    {"id": "WS5", "title": "Clients", "state": "active"},
    {"id": "WS7", "title": "Launch", "state": "active"},
    {"id": "WS8", "title": "Harbor", "state": "active"},
    {"id": "WS6", "title": "Cleanup", "state": "active"},
]


def task_line(ts, task, to, frm="wip"):
    row = {"ts": ts, "task": task, "to": to}
    if frm is not None:
        row["from"] = frm
    return row


def state_line(ts, ws, frm, to):
    return {"kind": "state", "ts": ts, "ws": ws, "from": frm, "to": to}


def bulk_histories():
    """117 done-moves inside one minute across five workstreams."""
    base = NOW - 600
    minute = int(base // 60) * 60
    counts = {"WS3": 37, "WS5": 14, "WS7": 8, "WS8": 57, "WS6": 1}
    histories = {}
    seq = 0
    for ws, count in counts.items():
        rows = []
        for _ in range(count):
            rows.append(task_line(minute + (seq % 50), "%s.%d" % (ws, seq), "done"))
            seq += 1
        histories[ws] = rows
    return histories


class BuildFeedTest(unittest.TestCase):
    def test_bulk_moves_collapse_into_one_line(self):
        payload = projectfeed.build_feed(BUNDLES, bulk_histories(), [], [], NOW)
        self.assertEqual(payload["total"], 1)
        line = payload["feed"][0]
        self.assertEqual(line["kind"], "tasks")
        self.assertIsNone(line["target"])
        self.assertEqual(
            line["text"],
            "117 tasks moved to done across Harbor (WS8) (57), "
            "Gateway (WS3) (37), Clients (WS5) (14), Launch (WS7) (8), "
            "Cleanup (WS6) (1)",
        )

    def test_single_moves_stay_individual_and_newest_first(self):
        histories = {
            "WS3": [task_line(NOW - 300, "WS3.7", "done"), task_line(NOW - 100, "WS3.8", "pr")],
            "WS5": [task_line(NOW - 200, "WS5.2", "done")],
        }
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW)
        self.assertEqual(payload["total"], 3)
        stamps = [line["ts"] for line in payload["feed"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        texts = [line["text"] for line in payload["feed"]]
        self.assertIn("Gateway (WS3): task WS3.8 moved to pr", texts)
        self.assertIn("Clients (WS5): task WS5.2 moved to done", texts)
        for line in payload["feed"]:
            self.assertEqual(line["target"], line["ws"])

    def test_reopenings_read_as_reopened(self):
        histories = {"WS3": [dict(task_line(NOW - 50, "WS3.7", "wip"), **{"from": "done"})]}
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["feed"][0]["kind"], "task")
        self.assertIn("reopened", payload["feed"][0]["text"])

    def test_state_archive_and_restore_wording(self):
        histories = {
            "WS3": [state_line(NOW - 30, "WS3", "active", "paused")],
            "WS5": [state_line(NOW - 20, "WS5", "done", "archived")],
            "WS7": [state_line(NOW - 10, "WS7", "archived", "done")],
        }
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW)
        by_kind = {line["kind"]: line["text"] for line in payload["feed"]}
        self.assertEqual(by_kind["state"], "Gateway (WS3) moved from active to paused")
        self.assertEqual(by_kind["archive"], "Clients (WS5) was archived")
        self.assertEqual(by_kind["restore"], "Launch (WS7) was restored to done")

    def test_untimed_lines_are_omitted(self):
        histories = {
            "WS3": [
                {"task": "WS3.7", "to": "done"},
                {"task": "WS3.8", "to": "done", "ts": "not-a-time"},
                state_line(NOW - 10, "WS3", "active", "paused"),
                {"kind": "state", "ws": "WS3", "from": "paused", "to": "done"},
                task_line(NOW - 5, "WS3.9", "done"),
            ]
        }
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW)
        self.assertEqual(payload["total"], 2)

    def test_approvals_need_a_date_and_a_decision(self):
        approvals = [
            {"ws": "WS7", "decided_at": "2026-04-02T15:10:00Z", "decided_by": "owner",
             "items": [
                 {"id": "hold-note", "step": "Hold the launch note", "status": "denied"},
                 {"id": "go", "step": "Ship it", "status": "pending"},
             ]},
            {"ws": "WS3", "decided_at": None, "decided_by": None,
             "items": [{"id": "x", "step": "Undated denial", "status": "denied"}]},
        ]
        payload = projectfeed.build_feed(BUNDLES, {}, approvals, [], NOW)
        self.assertEqual(payload["total"], 1)
        line = payload["feed"][0]
        self.assertEqual(line["kind"], "approval")
        self.assertEqual(line["text"], "Launch (WS7): Hold the launch note denied")

    def test_run_finishes_summarize_per_workstream_per_hour(self):
        hour = int((NOW - 5000) // 3600) * 3600
        runs = [
            {"source": "muse", "tier": "muse", "workstream": "WS8",
             "status": "done", "verdict": "PASS",
             "started_at": hour + 10, "ended_at": hour + 100},
            {"source": "muse", "tier": "muse", "workstream": "WS8",
             "status": "done", "verdict": "PASS",
             "started_at": hour + 20, "ended_at": hour + 200},
            {"source": "muse", "tier": "muse", "workstream": "WS8",
             "status": "done", "verdict": "PARTIAL",
             "started_at": hour + 30, "ended_at": hour + 300},
            {"source": "dagster", "tier": "", "workstream": "WS8",
             "status": "finished", "verdict": None,
             "started_at": hour + 40, "ended_at": hour + 400},
            {"source": "muse", "tier": "muse", "workstream": "WS8",
             "status": "running", "verdict": None,
             "started_at": hour + 50, "ended_at": None},
        ]
        payload = projectfeed.build_feed(BUNDLES, {}, [], runs, NOW)
        kinds = [line["kind"] for line in payload["feed"]]
        self.assertIn("run", kinds)
        self.assertIn("first-run", kinds)
        summary = [line for line in payload["feed"] if line["kind"] == "run"][0]
        self.assertEqual(
            summary["text"], "3 agents finished in Harbor (WS8); 2 pass, 1 partial"
        )
        first = [line for line in payload["feed"] if line["kind"] == "first-run"][0]
        self.assertEqual(first["text"], "first agent run in Harbor (WS8)")

    def test_limit_bounds(self):
        histories = {
            "WS3": [task_line(NOW - num * 61, "WS3.%d" % num, "done") for num in range(1, 300)]
        }
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW, limit=7)
        self.assertEqual(payload["limit"], 7)
        self.assertEqual(len(payload["feed"]), 7)
        self.assertEqual(payload["total"], 299)
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW)
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(len(payload["feed"]), 50)
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW, limit=500)
        self.assertEqual(payload["limit"], 200)
        self.assertEqual(len(payload["feed"]), 200)

    def test_earliest_ts_names_the_gap(self):
        histories = {
            "WS3": [task_line(NOW - 9000, "WS3.7", "done")],
            "WS5": [task_line(NOW - 100, "WS5.2", "done")],
        }
        payload = projectfeed.build_feed(BUNDLES, histories, [], [], NOW)
        self.assertEqual(payload["earliest_ts"], NOW - 9000)
        empty = projectfeed.build_feed([], {}, [], [], NOW)
        self.assertEqual(empty["feed"], [])
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["earliest_ts"])

    def test_no_doctor_items(self):
        histories = {
            "WS3": [task_line(NOW - 10, "WS3.7", "done"),
                    state_line(NOW - 5, "WS3", "active", "done")],
        }
        approvals = [
            {"ws": "WS3", "decided_at": "2026-04-02T15:10:00Z", "decided_by": "owner",
             "items": [{"id": "x", "step": "Ship it", "status": "approved"}]},
        ]
        runs = [
            {"source": "muse", "tier": "muse", "workstream": "WS3",
             "status": "done", "verdict": "PASS",
             "started_at": NOW - 90, "ended_at": NOW - 60},
        ]
        payload = projectfeed.build_feed(BUNDLES, histories, approvals, runs, NOW)
        self.assertTrue(payload["feed"])
        for line in payload["feed"]:
            self.assertNotIn(line["kind"], ("doctor", "health", "problem", "warning"))
            self.assertNotIn("doctor", line["text"].lower())
            self.assertNotIn("health", line["text"].lower())

    def test_grouped_days_use_utc(self):
        self.assertEqual(projectfeed.day_key(1_761_999_960.0), "2025-11-01")
        self.assertIsNone(projectfeed.parse_ts(None))
        self.assertIsNone(projectfeed.parse_ts("whenever"))


if __name__ == "__main__":
    unittest.main()
