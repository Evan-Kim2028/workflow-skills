import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.bundles import validate_bundle
from dash.planrender import render

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bundles"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PlanRenderTest(unittest.TestCase):
    def test_section_order_and_progress_placeholder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "WS9"
        write(root / "bundle.json", json.dumps({"id": "WS9", "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}))
        write(
            root / "plan.json",
            json.dumps(
                {
                    "workstream": "WS9",
                    "motivation": {"verdict": "WORTH_IT", "verdict_reason": "Because the measure moved.", "today": [], "after": [], "costs": [], "not_better": []},
                    "decisions": [{"id": "D1", "question": "Ship?", "decision": "Yes.", "why": "The measure moved.", "reversible": True, "alternatives": [], "evidence": [], "risk": "Low."}],
                    "agent_friendly": {"litmus": "One command proves it.", "litmus_check": "run the command", "requirements": []},
                    "narrative": {
                        "thesis": "A single contract replaces three readers.",
                        "end_state": "Clients read one route.",
                        "phases": [{"name": "Land", "goal": "Ship the route.", "gate": "The route is on main."}],
                    },
                    "dag": {"cols": ["Now"], "groups": [{"id": "g1", "col": 0, "title": "Land", "tasks": [{"id": "WS9.1", "title": "Ship the route", "status": "todo"}]}], "edges": []},
                }
            ),
        )
        write(
            root / "layout.json",
            json.dumps(
                {
                    "why_title": "Three readers become one",
                    "why_visual": ["Before and after"],
                    "end_title": "One route",
                    "end_body": [],
                    "plan_title": "Two steps",
                    "plan_extra": [],
                    "agent_title": "Adding a route should be boring",
                    "agent_visual": [],
                    "review_note": "Checked from the repo.",
                    "ref_sections": [],
                }
            ),
        )
        write(
            root / "visuals.html",
            '<header><p class="thesis">old thesis</p><div class="figs"><div class="fig">9</div></div></header>\n'
            '<section class="pl-sec lay"><h2>Before and after</h2><p>UNIQUE-BEFORE-AFTER</p></section>\n',
        )
        page = render(root)
        self.assertIn("UNIQUE-BEFORE-AFTER", page)
        self.assertIn("A single contract replaces three readers.", page)
        self.assertNotIn(">old thesis<", page)
        self.assertNotIn('class="figs"', page)
        self.assertIn('data-plan-progress', page)
        self.assertNotIn('data-plan-approvals', page)
        self.assertNotIn("wsp-figs", page)
        self.assertNotIn("<script", page)
        order = [page.index(f'id="ws9-{anchor}"') for anchor in ("why", "end", "plan", "progress", "agent")]
        self.assertNotIn('id="ws9-decided"', page)
        self.assertNotIn("Decisions, made by review", page)
        self.assertNotIn("dec-grid", page)
        self.assertNotIn('id="ws9-ref"', page)
        self.assertEqual(order, sorted(order))
        self.assertLess(page.index("Why"), page.index("End state"))
        self.assertLess(page.index("End state"), page.index("Progress"))
        self.assertTrue((root / "page.html").is_file())
        self.assertFalse((root / "page.html.tmp").exists())

    def test_synthetic_bundles_render(self):
        self.assertTrue((FIXTURES / "OVERVIEW" / "bundle.json").is_file())
        self.assertTrue((FIXTURES / "WS1" / "plan.json").is_file())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in ("OVERVIEW", "WS1"):
            dest = root / name
            dest.mkdir()
            for item in (FIXTURES / name).iterdir():
                if item.is_file():
                    (dest / item.name).write_bytes(item.read_bytes())
        self.assertEqual(validate_bundle("OVERVIEW", root), [])
        self.assertEqual(validate_bundle("WS1", root), [])
        page = render(root / "WS1")
        self.assertIn("One read path replaces three readers.", page)
        self.assertIn("Three readers become one route.", page)
        self.assertIn('data-plan-progress', page)
        overview = render(root / "OVERVIEW")
        self.assertIn("One program, one read path.", overview)


if __name__ == "__main__":
    unittest.main()
