"""The one verdict parser: work words, review words, and a report with none.

The two review phrasings are the real ones from jobs 5982cc3bc519 and
7e3812b955ab (``~/.local/state/agentq/jobs/<id>/report.md``).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import verdict as V  # noqa: E402

#: The first line of job 5982cc3bc519's report, verbatim.
REQUEST_CHANGES_REPORT = (
    "# WS47 identity spine — adversarial review\n"
    "\n"
    "**Verdict: REQUEST_CHANGES.** The direction is sound, and `identity.alias` is the best\n"
    "existing base for decisive external-key mappings, but it is not yet fit to be the sole\n"
    "authority.\n"
)
#: The first line of job 7e3812b955ab's report, verbatim.
APPROVE_WITH_CHANGES_REPORT = (
    "**VERDICT: APPROVE_WITH_CHANGES**\n"
    "\n"
    "Reviewed commit `0005ce5` (`git diff dcc79e0..HEAD`), read-only. One real\n"
    "data-correctness defect in the new card-image denominators, two nits.\n"
)


class ReviewVerdictTests(unittest.TestCase):
    def test_the_two_real_review_phrasings(self):
        self.assertEqual(V.verdict_of(REQUEST_CHANGES_REPORT), "REQUEST_CHANGES")
        self.assertEqual(V.verdict_of(APPROVE_WITH_CHANGES_REPORT), "APPROVE_WITH_CHANGES")

    def test_every_review_word_in_its_common_shapes(self):
        cases = {
            "APPROVE": "APPROVE",
            "**APPROVE**": "APPROVE",
            "Verdict: APPROVE": "APPROVE",
            "**Verdict: APPROVE.**": "APPROVE",
            "## Verdict: APPROVE": "APPROVE",
            "verdict - approve": "APPROVE",
            "APPROVE_WITH_CHANGES": "APPROVE_WITH_CHANGES",
            "**VERDICT: APPROVE_WITH_CHANGES**": "APPROVE_WITH_CHANGES",
            "Verdict: APPROVE_WITH_NITS": "APPROVE_WITH_NITS",
            "REQUEST_CHANGES": "REQUEST_CHANGES",
            "**Verdict: REQUEST_CHANGES.**": "REQUEST_CHANGES",
            "REJECT: the migration drops the index": "REJECT",
        }
        for text, expected in cases.items():
            self.assertEqual(V.verdict_of(text), expected, text)

    def test_review_words_are_recognised_as_review(self):
        for word in ("APPROVE", "APPROVE_WITH_CHANGES", "APPROVE_WITH_NITS", "REQUEST_CHANGES", "REJECT"):
            self.assertTrue(V.is_review(word), word)
        for word in ("PASS", "PARTIAL", "BLOCKED", "none", None):
            self.assertFalse(V.is_review(word), word)


class WorkVerdictTests(unittest.TestCase):
    def test_pass_partial_blocked(self):
        cases = {
            "PASS": "PASS",
            "PASS (no BLOCKED items)": "PASS",
            "**PASS**": "PASS",
            "PARTIAL: two files not compared": "PARTIAL",
            "verdict: BLOCKED": "BLOCKED",
            "BLOCKED: no key": "BLOCKED",
        }
        for text, expected in cases.items():
            self.assertEqual(V.verdict_of(text), expected, text)

    def test_a_report_with_no_verdict_line_has_none(self):
        cases = (
            "",
            "I'll start by reading the module and finding its documentation.\n",
            "Report written to `/tmp/report.md`.\n",
            "no verdict here PASS\n",
            "PASSED the tests, but no verdict line\n",
            "The run was BLOCKED by the slot guard.\n",
        )
        for text in cases:
            self.assertIsNone(V.verdict_of(text), text)

    def test_the_first_verdict_line_wins(self):
        self.assertEqual(V.verdict_of("PASS\n\n- BLOCKED: the pyright slot was full\n"), "PASS")
        self.assertEqual(V.verdict_of("Summary\nPARTIAL\n\nverdict: BLOCKED"), "PARTIAL")
        self.assertEqual(V.verdict_of("BLOCKED by upstream\n\nPASS"), "BLOCKED")


class BucketTests(unittest.TestCase):
    def test_review_words_fold_into_the_three_totals(self):
        self.assertEqual(V.bucket("APPROVE"), "PASS")
        self.assertEqual(V.bucket("APPROVE_WITH_CHANGES"), "PASS")
        self.assertEqual(V.bucket("APPROVE_WITH_NITS"), "PASS")
        self.assertEqual(V.bucket("REQUEST_CHANGES"), "PARTIAL")
        self.assertEqual(V.bucket("REJECT"), "PARTIAL")
        self.assertEqual(V.bucket("PASS"), "PASS")
        self.assertEqual(V.bucket("PARTIAL"), "PARTIAL")
        self.assertEqual(V.bucket("FAIL"), "FAIL")

    def test_dead_and_none_are_not_verdicts(self):
        for value in ("DEAD", "none", "", None, "QUOTA"):
            self.assertIsNone(V.bucket(value), value)
            self.assertFalse(V.has_verdict(value), value)
        for value in ("PASS", "PARTIAL", "BLOCKED", "APPROVE", "REQUEST_CHANGES"):
            self.assertTrue(V.has_verdict(value), value)


if __name__ == "__main__":
    unittest.main()
