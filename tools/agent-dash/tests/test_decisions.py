"""Decisions and risks from the local hub dir: parser, endpoints, computed risks.

All hub content here is synthetic (invented decision and workstream ids):
no owner paths, no real project names, no real decision ids.
"""

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
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import decisions as dec  # noqa: E402
from dash.bundles import cmd_ws  # noqa: E402
from dash.store import Store  # noqa: E402


def _hub(files: dict) -> str:
    root = tempfile.mkdtemp()
    for name, text in files.items():
        path = Path(root) / name
        path.write_text(text, encoding="utf-8", newline="")
    return root


class ParseTest(unittest.TestCase):
    def test_missing_file_and_none_hub(self):
        self.assertEqual(dec.load_decisions(None), [])
        self.assertEqual(dec.load_decisions("/no/such/dir-xyz"), [])
        self.assertEqual(dec.load_file_risks(None), [])

    def test_real_shaped_header(self):
        hub = _hub(
            {
                "decisions.tsv": (
                    "date\tid\tdecision\twhy\tstatus\n"
                    "2026-10-15\tD-14\tone public id\tcheap keying\taccepted\n"
                    "2026-10-16\tD-25\tfreeze the route list\tlive already\tproposed\n"
                )
            }
        )
        rows = dec.load_decisions(hub)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["id"], "D-14")
        self.assertEqual(first["title"], "one public id")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["date"], "2026-10-15")
        # Unknown columns survive as short extras.
        self.assertEqual(first["why"], "cheap keying")

    def test_alias_headers(self):
        hub = _hub(
            {"decisions.tsv": "id\ttext\tstate\towner\tws\tarea\tlink\nD-1\tt\topen\tann\tWS41\tapi\thttps://example.com/a/12\n"}
        )
        (row,) = dec.load_decisions(hub)
        self.assertEqual(
            row,
            {
                "id": "D-1",
                "title": "t",
                "status": "open",
                "owner": "ann",
                "workstream": "WS41",
                "area": "api",
                "link": "https://example.com/a/12",
            },
        )

    def test_crlf_and_blank_lines(self):
        hub = _hub(
            {"decisions.tsv": "id\tdecision\r\n\r\nD-1\tfirst\r\n\r\nD-2\tsecond\r\n"}
        )
        rows = dec.load_decisions(hub)
        self.assertEqual([row["id"] for row in rows], ["D-1", "D-2"])

    def test_overlong_cells_dropped_row_kept(self):
        long_text = "x" * 500
        hub = _hub(
            {"decisions.tsv": f"id\tdecision\twhy\tstatus\nD-1\tshort title\t{long_text}\topen\n"}
        )
        (row,) = dec.load_decisions(hub)
        self.assertEqual(row["title"], "short title")
        self.assertNotIn("why", row)
        self.assertEqual(row["status"], "open")

    def test_paths_dropped_urls_and_prose_kept(self):
        hub = _hub(
            {
                "decisions.tsv": (
                    "id\tdecision\tlink\n"
                    "D-1\tserve /v1 from one place\tnotes/2026-10-18-closing.md\n"
                    "D-2\tsecond choice\thttps://example.com/notes/2026-10-18\n"
                )
            }
        )
        rows = dec.load_decisions(hub)
        self.assertEqual(rows[0]["title"], "serve /v1 from one place")
        self.assertNotIn("link", rows[0])
        self.assertEqual(rows[1]["link"], "https://example.com/notes/2026-10-18")

    def test_short_rows_padded_surplus_cells_ignored(self):
        hub = _hub({"decisions.tsv": "id\tdecision\tstatus\nD-1\tonly title\nD-2\tt\topen\textra\n"})
        rows = dec.load_decisions(hub)
        self.assertEqual(rows[0], {"id": "D-1", "title": "only title"})
        self.assertEqual(rows[1]["status"], "open")

    def test_mtime_cache_refreshes(self):
        hub = Path(tempfile.mkdtemp())
        target = hub / "decisions.tsv"
        target.write_text("id\tdecision\nD-1\tfirst\n", encoding="utf-8")
        first = dec.load_decisions(str(hub))
        second = dec.load_decisions(str(hub))
        self.assertIs(first, second)
        target.write_text("id\tdecision\nD-1\tsecond\n", encoding="utf-8")
        stamp = time.time_ns() + 10_000_000_000
        os.utime(target, ns=(stamp, stamp))
        third = dec.load_decisions(str(hub))
        self.assertIsNot(first, third)
        self.assertEqual(third[0]["title"], "second")


class DecisionsPayloadTest(unittest.TestCase):
    def test_newest_first_counts_and_source(self):
        hub = _hub(
            {
                "decisions.tsv": (
                    "date\tid\tdecision\tstatus\n"
                    "2026-10-14\tD-1\told\taccepted\n"
                    "2026-10-16\tD-2\tnew\tproposed\n"
                    "2026-10-15\tD-3\tmid\taccepted\n"
                )
            }
        )
        payload = dec.decisions_payload(hub)
        self.assertEqual([row["id"] for row in payload["decisions"]], ["D-2", "D-3", "D-1"])
        self.assertEqual(payload["counts"], {"proposed": 1, "accepted": 2})
        self.assertEqual(payload["source"], {"present": True})

    def test_missing_hub_is_empty_and_absent(self):
        payload = dec.decisions_payload("/no/such/dir-xyz")
        self.assertEqual(payload, {"decisions": [], "counts": {}, "source": {"present": False}})

    def test_summary_open_counts(self):
        hub = _hub(
            {
                "decisions.tsv": (
                    "id\tdecision\tstatus\n"
                    "D-1\ta\taccepted\n"
                    "D-2\tb\tproposed\n"
                    "D-3\tc\t\n"
                )
            }
        )
        self.assertEqual(dec.decisions_summary(hub), {"total": 3, "open": 2})
        self.assertEqual(dec.decisions_summary("/no/such/dir-xyz"), {"total": 0, "open": 0})


class RisksFileTest(unittest.TestCase):
    def test_tsv_and_json(self):
        hub = _hub(
            {
                "risks.tsv": "id\ttitle\tseverity\towner\tws\tstatus\nR-1\tlane waits\tWARN\tann\tWS41\topen\n",
                "risks.json": json.dumps(
                    [
                        {"id": "R-2", "title": "slow lane", "severity": "CRITICAL", "since": 1700000000},
                        {"id": "R-3", "title": "mild", "severity": "bogus"},
                    ]
                ),
            }
        )
        rows = dec.load_file_risks(hub)
        self.assertEqual([row["id"] for row in rows], ["R-1", "R-2", "R-3"])
        self.assertEqual(rows[0]["severity"], "warn")
        self.assertEqual(rows[0]["workstream"], "WS41")
        self.assertEqual(rows[0]["source"], "file")
        self.assertEqual(rows[0]["ws"], "WS41")
        self.assertIsNone(rows[2]["ws"])
        self.assertIsNone(rows[2]["since"])
        self.assertEqual(rows[1]["severity"], "critical")
        self.assertEqual(rows[1]["since"], 1700000000.0)
        self.assertEqual(rows[2]["severity"], "info")

    def test_json_object_wrapper_and_missing_files(self):
        hub = _hub({"risks.json": json.dumps({"risks": [{"id": "R-9", "title": "wrapped"}]})})
        (row,) = dec.load_file_risks(hub)
        self.assertEqual(row["id"], "R-9")
        self.assertEqual(row["severity"], "info")
        self.assertEqual(dec.load_file_risks("/no/such/dir-xyz"), [])


def _plan_with_gate(path: Path) -> None:
    plan = {
        "dag": {
            "cols": ["Build"],
            "groups": [
                {
                    "id": "build",
                    "col": 0,
                    "title": "Build",
                    "tasks": [
                        {
                            "id": "WS41.1",
                            "title": "Wait for the freeze to lift",
                            "status": "todo",
                            "deps": [],
                            "kind": "gate",
                            "signal": "freeze lifts",
                            "fresh_hours": 1,
                        }
                    ],
                }
            ],
            "edges": [],
        },
        "narrative": {"thesis": "t", "end_state": "e", "phases": []},
    }
    (path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _approvals(path: Path, mtime: float) -> None:
    record = {
        "ws": path.name,
        "decided_at": None,
        "decided_by": None,
        "items": [
            {
                "id": "ship-it",
                "step": "Publish the lane",
                "status": "pending",
                "reversible": False,
                "conditions": "Lane exists.",
            }
        ],
    }
    target = path / "approvals.json"
    target.write_text(json.dumps(record), encoding="utf-8")
    os.utime(target, (mtime, mtime))


class ComputedRisksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "dash.sqlite")
        self.now = time.time()
        self.hub = tempfile.mkdtemp()

    def tearDown(self):
        self.store.close()
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def _cfg(self, **extra):
        cfg = {"hub_dir": self.hub, "doctor": {"approval_days": 3.0}}
        cfg.update(extra)
        return cfg

    def test_stale_gate_and_blocked_planned(self):
        self.assertEqual(cmd_ws(["new", "WS41", "Sample lane"]), 0)
        _plan_with_gate(self.root / "WS41")
        self.assertEqual(cmd_ws(["new", "WS42", "Waiting lane", "--state", "planned"]), 0)
        meta_path = self.root / "WS42" / "bundle.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["depends_on"] = ["WS41"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        payload = dec.risks_payload(self.store, self._cfg(), self.now)
        by_id = {risk["id"]: risk for risk in payload["risks"]}
        gate = by_id.get("gates:WS41:WS41.1")
        self.assertIsNotNone(gate)
        self.assertEqual(gate["severity"], "warn")
        self.assertEqual(gate["source"], "gates")
        self.assertTrue(gate["title"].startswith("WS41"))
        self.assertLess(gate["title"].index("WS41"), gate["title"].index("WS41.1"))
        dep = by_id.get("dependency:WS42")
        self.assertIsNotNone(dep)
        self.assertEqual(dep["severity"], "info")
        self.assertEqual(dep["source"], "dependency")
        self.assertIn("WS41", dep["title"])
        self.assertTrue(dep["title"].startswith("WS42"))

    def test_stale_approvals(self):
        self.assertEqual(cmd_ws(["new", "WS41", "Sample lane"]), 0)
        _approvals(self.root / "WS41", self.now - 10 * 86400)
        payload = dec.risks_payload(self.store, self._cfg(), self.now)
        by_id = {risk["id"]: risk for risk in payload["risks"]}
        risk = by_id.get("approvals:WS41")
        self.assertIsNotNone(risk)
        self.assertEqual(risk["severity"], "warn")
        self.assertEqual(risk["source"], "approvals")
        self.assertEqual(risk["ws"], "WS41")
        self.assertTrue(risk["title"].startswith("WS41"))

    def test_fresh_approvals_are_quiet(self):
        self.assertEqual(cmd_ws(["new", "WS41", "Sample lane"]), 0)
        _approvals(self.root / "WS41", self.now - 100)
        payload = dec.risks_payload(self.store, self._cfg(), self.now)
        self.assertNotIn("approvals:WS41", {risk["id"] for risk in payload["risks"]})

    def test_doctor_group_on_active_workstream(self):
        self.assertEqual(cmd_ws(["new", "WS41", "Sample lane"]), 0)
        self.store.doctor_snapshot(
            [
                {
                    "id": "stuck-task:ws:WS41:WS41.1",
                    "code": "stuck-task",
                    "severity": "warn",
                    "scope": "ws:WS41",
                    "ws": "WS41",
                    "title": "WS41 task is stuck",
                    "detail": "d",
                    "fix_command": "python3 agent_dash.py check",
                    "safe_auto": False,
                }
            ],
            self.now,
        )
        payload = dec.risks_payload(self.store, self._cfg(), self.now)
        by_id = {risk["id"]: risk for risk in payload["risks"]}
        risk = by_id.get("stuck-task:ws:WS41:WS41.1")
        self.assertIsNotNone(risk)
        self.assertEqual(risk["severity"], "warn")
        self.assertEqual(risk["source"], "doctor")
        self.assertTrue(risk["title"].startswith("WS41"))

    def test_archived_workstream_emits_no_computed_risk(self):
        self.assertEqual(cmd_ws(["new", "WS43", "Old lane"]), 0)
        _plan_with_gate(self.root / "WS43")
        plan_text = (self.root / "WS43" / "plan.json").read_text(encoding="utf-8")
        plan_text = plan_text.replace("WS41.1", "WS43.1")
        (self.root / "WS43" / "plan.json").write_text(plan_text, encoding="utf-8")
        meta_path = self.root / "WS43" / "bundle.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["state"] = "archived"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        payload = dec.risks_payload(self.store, self._cfg(), self.now)
        self.assertEqual(
            [risk for risk in payload["risks"] if risk.get("ws") == "WS43"], []
        )

    def test_computed_come_before_file_rows(self):
        self.assertEqual(cmd_ws(["new", "WS41", "Sample lane"]), 0)
        _plan_with_gate(self.root / "WS41")
        Path(self.hub, "risks.tsv").write_text(
            "id\ttitle\tseverity\nR-1\tfiled risk\tinfo\n", encoding="utf-8"
        )
        payload = dec.risks_payload(self.store, self._cfg(), self.now)
        ids = [risk["id"] for risk in payload["risks"]]
        self.assertIn("gates:WS41:WS41.1", ids)
        self.assertEqual(ids[-1], "R-1")

    def test_summaries(self):
        self.assertEqual(cmd_ws(["new", "WS41", "Sample lane"]), 0)
        _plan_with_gate(self.root / "WS41")
        summary = dec.risks_summary(self.store, self._cfg(), self.now)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["by_severity"], {"warn": 1})


if __name__ == "__main__":
    unittest.main()
