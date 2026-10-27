import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.bundles import plan_scope_warnings, plan_scope_warnings_for_plan, validate_bundle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bundles"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def scope_plan(probe="uv run pytest api/tests/test_events_contract.py", gate="uv run pytest api/tests/test_x.py"):
    return {
        "workstream": "WS9",
        "dag": {
            "cols": ["Now"],
            "groups": [
                {"id": "g1", "col": 0, "title": "Ship the route",
                 "tasks": [{"id": "WS9.1", "title": "Ship the route", "status": "todo",
                            "acceptance": {"metric": "m", "threshold": "t", "tolerance": "0",
                                           "probe": probe}}]},
            ],
            "edges": [],
        },
        "narrative": {
            "thesis": "A single contract replaces three readers.",
            "end_state": "Clients read one route.",
            "phases": [{"name": "Land", "goal": "Ship the route.", "gate": "The route is on main."}],
        },
        "trains": [{"id": "T1", "title": "read path", "members": ["WS9.1"],
                    "status": "planned", "gate": gate}],
    }


def write_bundle(root: Path, plan: dict) -> None:
    write(root / "bundle.json", json.dumps({"id": "WS9", "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}))
    write(root / "plan.json", json.dumps(plan))


class PlanScopeWarningTests(unittest.TestCase):
    def codes(self, plan):
        return [warn["code"] for warn in plan_scope_warnings_for_plan(plan, "WS9")]

    def test_scoped_probe_and_gate_are_clean(self):
        self.assertEqual(plan_scope_warnings_for_plan(scope_plan(), "WS9"), [])

    def test_directory_argument_is_scoped(self):
        plan = scope_plan(probe="uv run pytest packages/auction_live/tests/")
        self.assertEqual(self.codes(plan), [])

    def test_bare_pytest_probes_are_plan207(self):
        for probe in ("pytest",
                      "uv run pytest",
                      "python3 -m pytest",
                      "python -m pytest",
                      "pytest -x -q",
                      "pytest and confirm green"):
            with self.subTest(probe=probe):
                self.assertIn("PLAN207", self.codes(scope_plan(probe=probe)))

    def test_string_acceptance_is_scanned(self):
        plan = scope_plan()
        plan["dag"]["groups"][0]["tasks"][0]["acceptance"] = "pytest"
        self.assertIn("PLAN207", self.codes(plan))

    def test_full_suite_tokens_are_plan207(self):
        for probe in ("pytest --full",
                      "scripts/lor-ci-local.sh",
                      "lor-ci-local.sh",
                      "ci-local.sh",
                      "python3 scripts/validate.sh",
                      "pre-commit run --all-files",
                      "gh workflow run ci",
                      "gh run rerun 123456"):
            with self.subTest(probe=probe):
                self.assertIn("PLAN207", self.codes(scope_plan(probe=probe)))

    def test_train_gate_is_scanned(self):
        plan = scope_plan(gate="pytest")
        codes = self.codes(plan)
        self.assertIn("PLAN207", codes)
        titles = [warn["title"] for warn in plan_scope_warnings_for_plan(plan, "WS9")]
        self.assertTrue(any("T1" in title for title in titles))

    def test_negated_mention_stays_quiet(self):
        plan = scope_plan(
            probe="reviewer diffs runbook against rpk topics",
            gate="union `pytest <member test files>` in the train worktree "
                 "(no lor-ci-local.sh, never the machine lock) plus pre-commit on "
                 "changed files plus scoped pyright",
        )
        self.assertEqual(plan_scope_warnings_for_plan(plan, "WS9"), [])

    def test_scoped_invocation_with_args_stays_quiet(self):
        plan = scope_plan(gate="scripts/lor-ci-local.sh --train-files <list> (pytest runs only the list)")
        self.assertEqual(self.codes(plan), [])

    def test_citation_and_dbt_full_refresh_stay_quiet(self):
        plan = scope_plan(probe="see scripts/lor-ci-local.sh:206-227 for the gate shape")
        self.assertEqual(self.codes(plan), [])
        plan = scope_plan(probe="one --full-refresh of traded_listings_pg per its model header")
        self.assertEqual(self.codes(plan), [])

    def test_warning_shape_matches_plan20x(self):
        warnings = plan_scope_warnings_for_plan(scope_plan(probe="pytest"), "WS9")
        self.assertEqual(len(warnings), 1)
        warn = warnings[0]
        self.assertEqual(set(warn), {"code", "severity", "title", "detail", "fix"})
        self.assertEqual(warn["code"], "PLAN207")
        self.assertIn("WS9.1", warn["title"])
        self.assertIn("pytest", warn["detail"])

    def test_scope_problems_are_warnings_not_issues(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "WS9"
        write_bundle(root, scope_plan(probe="pytest"))
        self.assertEqual(validate_bundle("WS9", root.parent), [])
        warnings = plan_scope_warnings("WS9", root.parent)
        self.assertTrue(any(warn["code"] == "PLAN207" for warn in warnings))

    def test_fixture_bundle_is_clean(self):
        plan = json.loads((FIXTURES / "WS1" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan_scope_warnings_for_plan(plan, "WS1"), [])


if __name__ == "__main__":
    unittest.main()
