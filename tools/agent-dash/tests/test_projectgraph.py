"""Cross-workstream dependency graph builder: edges, states, cycles, layers."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import projectgraph as pg  # noqa: E402


def bundle(ws, state="active", depends_on=None, title=None, milestone=None):
    row = {"id": ws, "title": title or ("Title %s" % ws), "state": state}
    if depends_on is not None:
        row["depends_on"] = depends_on
    if milestone is not None:
        row["milestone"] = milestone
    return row


def plan(groups):
    return {"ws": "X", "cols": ["A"], "groups": groups, "edges": [], "phases": []}


def gate(tid, signal=None, status="todo", confirmed_at=None, fresh_hours=None, title=None, dep_notes=None):
    task = {"id": tid, "title": title or tid, "status": status, "kind": "gate", "deps": []}
    if signal is not None:
        task["signal"] = signal
    if confirmed_at is not None:
        task["confirmed_at"] = confirmed_at
    if fresh_hours is not None:
        task["fresh_hours"] = fresh_hours
    if dep_notes is not None:
        task["dep_notes"] = dep_notes
    return task


def task(tid, status="todo", **extra):
    row = {"id": tid, "title": tid, "status": status, "deps": []}
    row.update(extra)
    return row


NOW = 1_761_999_960.0


class DependsOnEdges(unittest.TestCase):
    def test_dependency_met_when_source_done_or_archived(self):
        bundles = [
            bundle("WS1", state="done"),
            bundle("WS2", depends_on=["WS1"]),
            bundle("WS3", state="archived"),
            bundle("WS4", depends_on=["WS3"]),
            bundle("WS5", depends_on=["WS2"]),
        ]
        body = pg.build_graph(bundles, {}, now=NOW)
        by_pair = {(edge["from"], edge["to"]): edge for edge in body["edges"]}
        self.assertEqual(by_pair[("WS1", "WS2")]["kind"], "dependency")
        self.assertEqual(by_pair[("WS1", "WS2")]["status"], "met")
        self.assertEqual(by_pair[("WS3", "WS4")]["status"], "met")
        self.assertEqual(by_pair[("WS2", "WS5")]["status"], "waiting")
        self.assertIn("waits on", by_pair[("WS1", "WS2")]["condition"])

    def test_unknown_and_self_ids_are_dropped(self):
        bundles = [bundle("WS1", depends_on=["WS1", "WS99", "nope"])]
        body = pg.build_graph(bundles, {}, now=NOW)
        self.assertEqual(body["edges"], [])
        self.assertEqual(body["cycles"], [])

    def test_nodes_carry_state_progress_live_and_milestone(self):
        bundles = [bundle("WS1", state="paused", milestone="Part 2")]
        progress = {"WS1": {"done": 3, "total": 9, "counts": {}, "pct": 33}}
        body = pg.build_graph(bundles, {}, live={"WS1": 2}, progress=progress, now=NOW)
        node = body["nodes"][0]
        self.assertEqual(node["state"], "paused")
        self.assertEqual(node["progress"], {"done": 3, "total": 9, "counts": {}, "pct": 33})
        self.assertEqual(node["live_agents"], 2)
        self.assertEqual(node["milestone"], "Part 2")
        self.assertIsInstance(body["generated_at"], float)


class GateEdges(unittest.TestCase):
    def test_gate_signal_names_source(self):
        bundles = [bundle("WS6", state="active"), bundle("WS9", state="planned")]
        plans = {
            "WS9": plan([{"id": "wait", "col": 0, "title": "Wait", "tasks": [
                gate("WS9.G1", signal="WS6 cutover batch is merged"),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "gate"]
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["from"], edges[0]["to"]), ("WS6", "WS9"))
        self.assertEqual(edges[0]["status"], "waiting")
        self.assertEqual(edges[0]["condition"], "WS6 cutover batch is merged")
        self.assertEqual(edges[0]["gate"], "WS9.G1")

    def test_gate_dep_notes_text_names_source(self):
        bundles = [bundle("WS8", state="active"), bundle("WS10", state="planned")]
        plans = {
            "WS10": plan([{"id": "wait", "col": 0, "title": "Wait", "tasks": [
                gate("WS10.G1", signal="the launch feed is steady",
                     dep_notes={"WS10.G1": "run only after WS8 lands"}),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "gate"]
        self.assertEqual([(edge["from"], edge["to"]) for edge in edges], [("WS8", "WS10")])

    def test_stale_gate_edge_is_stale(self):
        bundles = [bundle("WS6", state="active"), bundle("WS9", state="planned")]
        plans = {
            "WS9": plan([{"id": "wait", "col": 0, "title": "Wait", "tasks": [
                gate("WS9.G1", signal="WS6 cutover batch is merged",
                     confirmed_at="2025-10-22T12:00:00Z", fresh_hours=24),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "gate"]
        self.assertEqual(edges[0]["status"], "stale")
        self.assertEqual(edges[0]["fresh_hours"], 24)

    def test_fresh_confirmed_gate_edge_is_met(self):
        bundles = [bundle("WS9", state="planned"), bundle("WS10", state="planned")]
        plans = {
            "WS10": plan([{"id": "wait", "col": 0, "title": "Wait", "tasks": [
                gate("WS10.G2", signal="WS9 relay loop is live",
                     confirmed_at="2026-10-17T20:00:00Z", fresh_hours=72),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "gate"]
        self.assertEqual(edges[0]["status"], "met")

    def test_done_gate_and_done_source_are_met(self):
        bundles = [bundle("WS1", state="done"), bundle("WS2", state="active")]
        plans = {
            "WS2": plan([{"id": "wait", "col": 0, "title": "Wait", "tasks": [
                gate("WS2.G1", signal="WS1 handoff is complete", status="done"),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "gate"]
        self.assertEqual(edges[0]["status"], "met")

    def test_gate_without_freshness_never_goes_stale(self):
        bundles = [bundle("WS6", state="active"), bundle("WS9", state="planned")]
        plans = {
            "WS9": plan([{"id": "wait", "col": 0, "title": "Wait", "tasks": [
                gate("WS9.G1", signal="WS6 says go"),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "gate"]
        self.assertEqual(edges[0]["status"], "waiting")


class MovedEdges(unittest.TestCase):
    def test_moved_edge_met_when_received(self):
        bundles = [bundle("WS4"), bundle("WS6")]
        plans = {
            "WS4": plan([{"id": "b", "col": 0, "title": "Build", "tasks": [
                task("WS4.1", status="deferred", moved_to="WS6",
                     received_as="WS6.9", note="waits on the freeze"),
            ]}]),
            "WS6": plan([{"id": "c", "col": 0, "title": "Catch", "tasks": [
                task("WS6.9", status="todo"),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "moved"]
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["from"], edges[0]["to"]), ("WS4", "WS6"))
        self.assertEqual(edges[0]["status"], "met")
        self.assertEqual(edges[0]["condition"], "waits on the freeze")

    def test_moved_edge_waiting_when_orphaned(self):
        bundles = [bundle("WS4"), bundle("WS6")]
        plans = {
            "WS4": plan([{"id": "b", "col": 0, "title": "Build", "tasks": [
                task("WS4.1", status="deferred", moved_to="WS6"),
            ]}]),
            "WS6": plan([{"id": "c", "col": 0, "title": "Catch", "tasks": [
                task("WS6.1", status="todo", title="Something entirely unrelated here"),
            ]}]),
        }
        body = pg.build_graph(bundles, plans, now=NOW)
        edges = [edge for edge in body["edges"] if edge["kind"] == "moved"]
        self.assertEqual(edges[0]["status"], "waiting")


class CyclesAndLayers(unittest.TestCase):
    def test_cycles_are_reported_not_errors(self):
        bundles = [
            bundle("WS1", depends_on=["WS3"]),
            bundle("WS2", depends_on=["WS1"]),
            bundle("WS3", depends_on=["WS2"]),
        ]
        body = pg.build_graph(bundles, {}, now=NOW)
        self.assertEqual(body["cycles"], [["WS1", "WS2", "WS3"]])
        depths = {node["id"]: node["depth"] for node in body["nodes"]}
        self.assertEqual(sorted(depths), ["WS1", "WS2", "WS3"])
        self.assertTrue(all(isinstance(depth, int) for depth in depths.values()))

    def test_layers_run_from_roots(self):
        bundles = [
            bundle("WS1"),
            bundle("WS2", depends_on=["WS1"]),
            bundle("WS3", depends_on=["WS1"]),
            bundle("WS4", depends_on=["WS2", "WS3"]),
            bundle("WS9"),
        ]
        body = pg.build_graph(bundles, {}, now=NOW)
        depths = {node["id"]: node["depth"] for node in body["nodes"]}
        self.assertEqual(depths, {"WS1": 0, "WS2": 1, "WS3": 1, "WS4": 2, "WS9": 0})
        self.assertEqual(body["cycles"], [])

    def test_edges_are_deterministic(self):
        bundles = [
            bundle("WS2", depends_on=["WS1"]),
            bundle("WS1"),
            bundle("WS10", depends_on=["WS1"]),
        ]
        first = pg.build_graph(bundles, {}, now=NOW)
        second = pg.build_graph(list(reversed(bundles)), {}, now=NOW)
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual([node["id"] for node in first["nodes"]], ["WS1", "WS10", "WS2"])

    def test_text_matching_is_case_insensitive(self):
        self.assertEqual(pg.workstream_ids_in_text("waits on ws6 and WS7"), ["WS6", "WS7"])
        self.assertEqual(pg.workstream_ids_in_text("nothing here"), [])
        self.assertEqual(pg.workstream_ids_in_text(None), [])


if __name__ == "__main__":
    unittest.main()
