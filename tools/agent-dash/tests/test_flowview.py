"""Execution flow payload: queue sums, blocker grouping, commands, empty state."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import flowview

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def ws_row(ident, counts, title=""):
    """One /api/state workstream row; total excludes deferred, like plan_progress."""
    counts = dict(counts)
    total = sum(counts.get(key, 0) for key in ("todo", "wip", "pr", "prog", "done"))
    return {
        "id": ident,
        "title": title,
        "progress": {"counts": counts, "total": total, "done": counts.get("done", 0)},
    }


def problem(code, ws, severity, count, title="", **extra):
    row = {"code": code, "ws": ws, "severity": severity, "count": count, "title": title}
    row.update(extra)
    return row


class QueueSums(unittest.TestCase):
    def test_queues_sum_across_workstreams(self):
        state = {"workstreams": [
            ws_row("WS33", {"todo": 4, "wip": 2, "pr": 1, "prog": 1, "done": 6, "deferred": 1}),
            ws_row("WS34", {"todo": 3, "wip": 1, "done": 2}),
        ]}
        flow = flowview.build_flow(state, None)
        self.assertEqual([q["key"] for q in flow["queues"]], ["todo", "wip", "pr", "prog", "deferred"])
        self.assertEqual([q["count"] for q in flow["queues"]], [7, 3, 1, 1, 1])
        self.assertEqual(flow["totals"]["tasks"], 20)
        self.assertEqual(flow["totals"]["done"], 8)
        self.assertEqual(flow["totals"]["pct"], 40)
        self.assertEqual(flow["totals"]["workstreams"], 2)
        self.assertFalse(flow["empty"])

    def test_workstream_rows_carry_their_own_counts(self):
        state = {"workstreams": [
            ws_row("WS33", {"todo": 4, "wip": 2, "done": 6}, "Serving cutover"),
            ws_row("WS34", {"todo": 3, "done": 2}, "Price coverage"),
        ]}
        flow = flowview.build_flow(state, None)
        rows = {row["id"]: row for row in flow["workstreams"]}
        self.assertEqual(rows["WS33"]["counts"]["todo"], 4)
        self.assertEqual(rows["WS33"]["title"], "Serving cutover")
        self.assertEqual(rows["WS33"]["total"], 12)
        self.assertEqual(rows["WS34"]["pct"], 40)

    def test_workstreams_without_progress_are_skipped(self):
        state = {"workstreams": [
            {"id": "WS1", "title": "No plan", "progress": None},
            ws_row("WS2", {"todo": 1}),
        ]}
        flow = flowview.build_flow(state, None)
        self.assertEqual([row["id"] for row in flow["workstreams"]], ["WS2"])
        self.assertEqual(flow["totals"]["workstreams"], 1)

    def test_empty_state(self):
        for state in (None, {}, {"workstreams": []}):
            flow = flowview.build_flow(state, None)
            self.assertTrue(flow["empty"])
            self.assertEqual(flow["totals"]["workstreams"], 0)
            self.assertEqual(flow["totals"]["tasks"], 0)
            self.assertEqual(flow["totals"]["pct"], 0)
            self.assertEqual(flow["blockers"], [])
            self.assertEqual([q["count"] for q in flow["queues"]], [0, 0, 0, 0, 0])


class BlockerGrouping(unittest.TestCase):
    def test_blockers_group_by_code_and_workstream(self):
        doctor = {"problems": [
            problem("stuck-task", "WS33", "warn", 1, "Task WS33.1 is stuck"),
            problem("stuck-task", "WS33", "critical", 2, "Task WS33.2 is stuck"),
            problem("stuck-task", "WS34", "warn", 1, "Task WS34.1 is stuck"),
        ]}
        flow = flowview.build_flow(None, doctor)
        self.assertEqual(len(flow["blockers"]), 2)
        first = flow["blockers"][0]
        self.assertEqual((first["code"], first["ws"]), ("stuck-task", "WS33"))
        self.assertEqual(first["count"], 3)
        self.assertEqual(first["severity"], "critical")
        self.assertEqual(first["title"], "Task WS33.1 is stuck")
        self.assertEqual(flow["totals"]["blockers"], 2)
        self.assertEqual(flow["totals"]["blocked_tasks"], 4)

    def test_blockers_sort_worst_then_largest(self):
        doctor = {"problems": [
            problem("info-code", "WS1", "info", 9),
            problem("warn-small", "WS2", "warn", 1),
            problem("warn-big", "WS3", "warn", 5),
            problem("crit", "WS4", "critical", 1),
        ]}
        flow = flowview.build_flow(None, doctor)
        self.assertEqual([b["code"] for b in flow["blockers"]], ["crit", "warn-big", "warn-small", "info-code"])

    def test_items_total_wins_over_count(self):
        doctor = {"problems": [problem("stuck-task", "WS33", "warn", 1, items_total=7)]}
        flow = flowview.build_flow(None, doctor)
        self.assertEqual(flow["blockers"][0]["count"], 7)

    def test_unknown_severity_reads_as_warn(self):
        doctor = {"problems": [problem("odd", "WS1", "bogus", 1)]}
        flow = flowview.build_flow(None, doctor)
        self.assertEqual(flow["blockers"][0]["severity"], "warn")

    def test_generated_at_comes_from_the_doctor_payload(self):
        self.assertEqual(flowview.build_flow(None, {"generated_at": 1762300000.0})["generated_at"], 1762300000.0)
        self.assertIsNone(flowview.build_flow(None, None)["generated_at"])
        self.assertIsNone(flowview.build_flow(None, {"generated_at": "never"})["generated_at"])


class Commands(unittest.TestCase):
    def test_workstream_blocker_commands_match_the_cli(self):
        doctor = {"problems": [problem("stuck-task", "WS33", "warn", 1)]}
        blocker = flowview.build_flow(None, doctor)["blockers"][0]
        self.assertEqual(blocker["command"], "python3 agent_dash.py doctor --ws WS33")
        self.assertEqual(blocker["next"], "python3 agent_dash.py ws next WS33")

    def test_global_blocker_has_no_next_command(self):
        doctor = {"problems": [problem("unpriced-model", None, "warn", 1)]}
        blocker = flowview.build_flow(None, doctor)["blockers"][0]
        self.assertEqual(blocker["command"], "python3 agent_dash.py doctor")
        self.assertIsNone(blocker["next"])

    def test_unsafe_workstream_id_never_reaches_a_command(self):
        doctor = {"problems": [problem("stuck-task", "WS33; rm -rf /", "warn", 1)]}
        blocker = flowview.build_flow(None, doctor)["blockers"][0]
        self.assertEqual(blocker["command"], "python3 agent_dash.py doctor")
        self.assertIsNone(blocker["next"])
        self.assertNotIn("rm -rf", blocker["command"])
        self.assertNotIn("rm -rf", blocker["next"] or "")


class PayloadHygiene(unittest.TestCase):
    def test_payload_carries_no_prompt_argv_or_env(self):
        doctor = {"generated_at": 1.0, "problems": [
            problem("stuck-task", "WS33", "warn", 1, "Task WS33.1 is stuck",
                    detail="prompt: do the thing --flag secret", fix={"command": "rm -rf /"}),
        ]}
        state = {"workstreams": [ws_row("WS33", {"todo": 1})]}
        raw = json.dumps(flowview.build_flow(state, doctor))
        for word in ("prompt", "argv", "environment", "secret", "rm -rf"):
            self.assertNotIn(word, raw, f"the flow payload leaks {word!r}")
        flow = flowview.build_flow(state, doctor)
        self.assertEqual(set(flow), {"generated_at", "queues", "blockers", "workstreams", "totals", "empty"})
        self.assertEqual(set(flow["blockers"][0]), {"code", "ws", "severity", "count", "title", "command", "next"})
        self.assertEqual(set(flow["totals"]), {"workstreams", "tasks", "done", "pct", "blockers", "blocked_tasks"})


class Fixtures(unittest.TestCase):
    def test_active_fixture_matches_the_builder(self):
        fixture = json.loads((FIXTURES / "flow.json").read_text())
        state = {"workstreams": [
            {"id": row["id"], "title": row["title"],
             "progress": {"counts": row["counts"], "total": row["total"], "done": row["done"]}}
            for row in fixture["workstreams"]
        ]}
        doctor = {"generated_at": fixture["generated_at"], "problems": [
            {"code": b["code"], "ws": b["ws"], "severity": b["severity"],
             "count": b["count"], "title": b["title"]}
            for b in fixture["blockers"]
        ]}
        self.assertEqual(flowview.build_flow(state, doctor), fixture)

    def test_empty_fixture_matches_the_builder(self):
        fixture = json.loads((FIXTURES / "flow-empty.json").read_text())
        self.assertEqual(flowview.build_flow({"workstreams": []}, None), fixture)


if __name__ == "__main__":
    unittest.main()
