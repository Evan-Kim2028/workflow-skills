import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import plans
from dash.bundles import bundle_plan_view, plan_frame_warnings, plan_frame_warnings_for_plan, sanitized_frame, validate_bundle
from dash.planrender import build, render

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bundles"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def frame_plan(**overrides):
    plan = {
        "workstream": "WS9",
        "frame": {
            "before": "Three readers answer the same question, and p95 read latency sits at 900 ms.",
            "after": "Clients call one route with p95 latency under 200 ms.",
            "focus": "Optimise for one read contract, trading away per-client shortcuts.",
            "scope_in": ["The shared read route"],
            "scope_out": ["Write latency (owned by the storage team)"],
            "objectives": [
                {"id": "O1", "title": "One read route", "before": "3 readers", "after": "1 route",
                 "measure": "The sample set loads.", "sections": ["g1"]},
                {"id": "O2", "title": "No legacy readers", "before": "2 live", "after": "0 live",
                 "measure": "The old routes return 410 Gone.", "sections": ["g2"]},
            ],
            "path": {
                "choice": "Keep one route and retire the other two.",
                "why": ["One contract is easier to test."],
                "tradeoffs": ["Clients migrate on their own schedule."],
                "alternatives": [
                    {"option": "Do nothing", "verdict": "rejected", "why": "Drift grows.", "revisit_when": "Never."},
                    {"option": "Keep one route", "verdict": "chosen", "why": "The sample set proves it.", "revisit_when": ""},
                    {"option": "Shim first", "verdict": "later", "why": "Doubles the surface.", "revisit_when": "When a client cannot migrate."},
                ],
            },
        },
        "dag": {
            "cols": ["Now"],
            "groups": [
                {"id": "g1", "col": 0, "title": "Agree the contract",
                 "tasks": [{"id": "WS9.1", "title": "Ship the route", "status": "todo"}]},
                {"id": "g2", "col": 0, "title": "Retire the old readers",
                 "tasks": [{"id": "WS9.2", "title": "Retire the readers", "status": "done"}]},
            ],
            "edges": [],
        },
        "narrative": {
            "thesis": "A single contract replaces three readers.",
            "end_state": "Clients read one route.",
            "phases": [{"name": "Land", "goal": "Ship the route.", "gate": "The route is on main."}],
        },
    }
    plan.update(overrides)
    return plan


def write_bundle(root: Path, plan: dict) -> None:
    write(root / "bundle.json", json.dumps({"id": "WS9", "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}))
    write(root / "plan.json", json.dumps(plan))
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
        '<header><p class="thesis">old thesis</p></header>\n'
        '<section class="pl-sec lay"><h2>Before and after</h2><p>UNIQUE-BEFORE-AFTER</p></section>\n',
    )


class SanitizeFrameTests(unittest.TestCase):
    def test_unknown_keys_dropped(self):
        frame = sanitized_frame({"before": "3 readers today.", "bogus": 1, "objectives": [{"id": "O1", "nope": 2}]})
        self.assertEqual(frame, {"before": "3 readers today.", "objectives": [{"id": "O1"}]})

    def test_missing_frame_is_none(self):
        self.assertIsNone(sanitized_frame(None))
        self.assertIsNone(sanitized_frame("nope"))
        self.assertEqual(sanitized_frame({"before": "3 readers today."}), {"before": "3 readers today."})

    def test_objective_carried_through_plan_apis(self):
        plan = frame_plan()
        plan["dag"]["groups"][0]["objective"] = "O1"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write(root / "maps.json", json.dumps({"WS9": "plan.json"}))
        write(root / "plan.json", json.dumps({"dag": plan["dag"], "narrative": plan["narrative"],
                                              "frame": plan["frame"], "trains": []}))
        body = plans.load("WS9", root / "maps.json")
        self.assertEqual(body["groups"][0]["objective"], "O1")
        self.assertEqual(body["frame"]["before"], plan["frame"]["before"])
        self.assertEqual(body["frame"]["objectives"][0]["sections"], ["g1"])

        dest = root / "WS9"
        write_bundle(dest, plan)
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(root)
        self.addCleanup(lambda: os.environ.__setitem__("AGENT_DASH_WORKSTREAMS", str(FIXTURES)))
        try:
            view = bundle_plan_view("WS9")
        finally:
            os.environ["AGENT_DASH_WORKSTREAMS"] = str(FIXTURES)
        self.assertEqual(view["groups"][0]["objective"], "O1")
        self.assertEqual(view["frame"]["path"]["alternatives"][1]["verdict"], "chosen")


class PlanFrameWarningTests(unittest.TestCase):
    def codes(self, plan):
        return [warn["code"] for warn in plan_frame_warnings_for_plan(plan, "WS9")]

    def test_clean_frame_has_no_warnings(self):
        self.assertEqual(plan_frame_warnings_for_plan(frame_plan(), "WS9"), [])

    def test_template_skeleton_is_flagged(self):
        from dash.bundles import _template_plan

        codes = self.codes(_template_plan("WS9"))
        self.assertIn("PLAN202", codes)  # TODO sentences carry no digit
        self.assertIn("PLAN204", codes)  # one objective with no section
        self.assertIn("PLAN203", codes)  # template section maps to no objective
        self.assertIn("PLAN206", codes)  # two alternatives, none chosen

    def test_missing_frame_is_plan201(self):
        self.assertEqual(self.codes({"dag": {"groups": []}}), ["PLAN201"])

    def test_missing_sentence_is_plan201(self):
        plan = frame_plan()
        del plan["frame"]["after"]
        self.assertIn("PLAN201", self.codes(plan))

    def test_long_or_numberless_sentence_is_plan202(self):
        plan = frame_plan()
        plan["frame"]["before"] = "word " * 41
        self.assertIn("PLAN202", self.codes(plan))
        plan = frame_plan()
        plan["frame"]["before"] = "Three readers answer the same question today."
        self.assertIn("PLAN202", self.codes(plan))

    def test_orphan_section_is_plan203(self):
        plan = frame_plan()
        plan["frame"]["objectives"][1]["sections"] = ["g2"]
        plan["frame"]["objectives"][0]["sections"] = []
        plan["dag"]["groups"][0].pop("objective", None)
        self.assertIn("PLAN203", self.codes(plan))

    def test_objective_with_no_section_is_plan204(self):
        plan = frame_plan()
        plan["frame"]["objectives"] = [
            {"id": "O1", "title": "One", "before": "3", "after": "1", "measure": "m", "sections": ["g1", "g2"]},
            {"id": "O2", "title": "Two", "before": "2", "after": "0", "measure": "m", "sections": []},
        ]
        self.assertIn("PLAN204", self.codes(plan))

    def test_unknown_section_id_is_plan205(self):
        plan = frame_plan()
        plan["frame"]["objectives"][0]["sections"] = ["nope"]
        codes = self.codes(plan)
        self.assertIn("PLAN205", codes)
        self.assertIn("PLAN203", codes)

    def test_thin_or_choiceless_alternatives_are_plan206(self):
        plan = frame_plan()
        plan["frame"]["path"]["alternatives"] = plan["frame"]["path"]["alternatives"][:2]
        self.assertIn("PLAN206", self.codes(plan))
        plan = frame_plan()
        for alt in plan["frame"]["path"]["alternatives"]:
            alt["verdict"] = "rejected"
        self.assertIn("PLAN206", self.codes(plan))
        plan = frame_plan()
        for alt in plan["frame"]["path"]["alternatives"]:
            alt["option"] = "Something else"
        self.assertIn("PLAN206", self.codes(plan))

    def test_frame_problems_are_warnings_not_issues(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "WS9"
        plan = frame_plan()
        plan["frame"]["path"]["alternatives"] = []
        write_bundle(root, plan)
        self.assertEqual(validate_bundle("WS9", root.parent), [])
        warnings = plan_frame_warnings("WS9", root.parent)
        self.assertTrue(any(warn["code"] == "PLAN206" for warn in warnings))
        for warn in warnings:
            self.assertEqual(set(warn), {"code", "severity", "title", "detail", "fix"})

    def test_fixture_frame_is_clean(self):
        plan = json.loads((FIXTURES / "WS1" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan_frame_warnings_for_plan(plan, "WS1"), [])


class PlanFrameRenderTests(unittest.TestCase):
    def render_frame(self, plan):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "WS9"
        write_bundle(root, plan)
        return render(root)

    def test_frame_section_order(self):
        plan = frame_plan()
        plan["decisions"] = [
            {"id": "D1", "question": "Which read path do clients keep?", "decision": "Keep one route.",
             "alternatives": [], "evidence": [], "why": "One contract.", "risk": "Low.",
             "reversible": True, "review_needed": "The sample set loads."}
        ]
        page = self.render_frame(plan)
        anchors = ["ws9-arch", "ws9-objectives", "ws9-path", "ws9-end", "ws9-plan", "ws9-progress", "ws9-agent", "ws9-ref"]
        self.assertTrue(all(f'id="{anchor}"' in page for anchor in anchors))
        order = [page.index(f'id="{anchor}"') for anchor in anchors]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('id="ws9-why"', page)
        band = page.index("data-frame-band")
        self.assertIn("Three readers answer the same question", page[band:band + 2000])
        self.assertIn("Clients call one route", page[band:band + 2000])
        self.assertIn("In scope", page)
        self.assertIn("Out of scope", page)
        self.assertEqual(page.count('data-objective='), 2)
        for gid in ("g1", "g2"):
            self.assertIn(f"view=exec#sec-{gid}", page)
        self.assertEqual(page.count('<tr class="chosen">'), 1)
        self.assertIn("Keep one route", page)
        self.assertIn('<details class="more"><summary>Decisions', page)

    def test_no_frame_fallback_renders_as_today(self):
        plan = frame_plan()
        del plan["frame"]
        page = self.render_frame(plan)
        self.assertNotIn("data-frame-band", page)
        self.assertIn("This plan has no before/after frame yet", page)
        order = [page.index(f'id="ws9-{anchor}"') for anchor in ("why", "end", "plan", "progress", "agent")]
        self.assertEqual(order, sorted(order))
        self.assertLess(page.index("Why"), page.index("End state"))

    def test_fixture_page_order(self):
        page = render(FIXTURES / "WS1")
        order = [page.index(f'id="ws1-{anchor}"') for anchor in ("arch", "objectives", "path", "end", "plan", "progress", "agent", "ref")]
        self.assertEqual(order, sorted(order))
        self.assertIn("data-frame-band", page)
        self.assertEqual(page.count('<tr class="chosen">'), 1)


if __name__ == "__main__":
    unittest.main()
