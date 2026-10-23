"""Outcome classes: one derivation, every status/verdict/close -> class.

Table-driven against dash/outcomes.py, with three invariants:

1. Every (status, verdict) pair seen in live data maps to exactly one
   class, and died (session ended without a verdict) never shares a
   bucket with blocked (the agent reported work must come back).
2. Every class has a distinct token+glyph+word: died and blocked differ
   in colour, glyph, and word; cancelled and unknown share grey but
   differ in glyph and word; stalled shares amber with partial but
   differs in glyph and word.
3. The client mirror (ADM.outcomeOf in static/models.js) agrees with
   the server on every row of the table.
"""
import json
import os
import subprocess
import unittest

from dash.outcomes import OUTCOME, outcome_class, outcome_reason

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (status, verdict, ended?, stalled?) -> class. Covers every pair in the
# live data plus every close path the sources can write.
CASES = [
    # Still open.
    (("running", None, False, False), "run"),
    (("queued", None, False, False), "unk"),
    (("unknown", None, False, False), "unk"),
    # Approved finishes.
    (("done", "PASS", True, False), "ok"),
    (("done", "APPROVE", True, False), "ok"),
    (("done", "APPROVE_WITH_NITS", True, False), "ok"),
    (("done", "APPROVEThe", True, False), "ok"),
    (("done", "SHIP", True, False), "ok"),
    (("done", None, True, False), "ok"),
    (("finished", "SUCCESS", True, False), "ok"),
    # Partial verdicts.
    (("done", "PARTIAL", True, False), "part"),
    (("done", "APPROVE_WITH_CHANGES", True, False), "part"),
    # Reported verdicts that send work back.
    (("done", "BLOCKED", True, False), "blocked"),
    (("done", "REQUEST_CHANGES", True, False), "blocked"),
    (("done", "FIX", True, False), "blocked"),
    # Deaths: status first, whatever the verdict field holds.
    (("killed", None, True, False), "died"),
    (("killed", "PARTIAL", True, False), "died"),
    (("killed", "PASS", True, False), "died"),
    (("killed", "BLOCKED", True, False), "died"),
    (("died_early", None, True, False), "died"),
    (("died_early", "QUOTA", True, False), "died"),
    (("died_early", "IDLE", True, False), "died"),
    (("died_early", "DEAD", True, False), "died"),
    # Deaths by verdict.
    (("finished", "FAILURE", True, False), "died"),
    (("done", "FAIL", True, False), "died"),
    (("done", "UNRESOLVED", True, False), "died"),
    # Cancelled from outside, never crashed.
    (("finished", "CANCELED", True, False), "cancelled"),
    # Stalled beats running, never an ended row.
    (("running", None, False, 3600.0), "stalled"),
    (("queued", None, False, 3600.0), "stalled"),
    (("done", "PASS", True, 3600.0), "ok"),
    # Unknowns: ended unconfirmed, nothing reported.
    (("unknown", None, True, False), "unk"),
    (("unknown", "PASS", True, False), "unk"),
    (("unknown", "PARTIAL", True, False), "unk"),
    (("unknown", None, False, False), "unk"),
    (("queued", None, True, False), "unk"),
    (("done", None, False, False), "unk"),
]

REASONS = [
    ({"status": "killed", "verdict": None, "ended_at": 1.0}, "died", "SIGTERM"),
    ({"status": "died_early", "verdict": "QUOTA", "ended_at": 1.0}, "died", "quota"),
    ({"status": "died_early", "verdict": "IDLE", "ended_at": 1.0}, "died", "idle"),
    ({"status": "died_early", "verdict": "DEAD", "ended_at": 1.0}, "died", "process gone"),
    ({"status": "died_early", "verdict": None, "ended_at": 1.0}, "died", "without a verdict"),
    ({"status": "finished", "verdict": "FAILURE", "ended_at": 1.0}, "died", "FAILURE"),
    ({"status": "running", "verdict": None, "ended_at": None, "stalled": 7200.0}, "stalled", "no output for 2.0h"),
    ({"status": "unknown", "verdict": None, "ended_at": 1.0}, "unk", "no verdict"),
    ({"status": "done", "verdict": "BLOCKED", "ended_at": 1.0}, "blocked", None),
    ({"status": "done", "verdict": "PASS", "ended_at": 1.0}, "ok", None),
    ({"status": "done", "verdict": "PARTIAL", "ended_at": 1.0}, "part", None),
    ({"status": "finished", "verdict": "CANCELED", "ended_at": 1.0}, "cancelled", None),
    ({"status": "running", "verdict": None, "ended_at": None}, "run", None),
]


def make_run(status, verdict, ended, stalled):
    return {
        "status": status,
        "verdict": verdict,
        "ended_at": 1792340000.0 if ended else None,
        "stalled": stalled,
    }


class OutcomeClassTests(unittest.TestCase):
    def test_every_case_maps_to_its_class(self):
        for (status, verdict, ended, stalled), want in CASES:
            with self.subTest(status=status, verdict=verdict):
                self.assertEqual(outcome_class(make_run(status, verdict, ended, stalled)), want)

    def test_never_colours_two_meanings_one_bucket(self):
        self.assertEqual(outcome_class({"status": "killed", "verdict": "BLOCKED", "ended_at": 1.0}), "died")
        self.assertEqual(outcome_class({"status": "done", "verdict": "BLOCKED", "ended_at": 1.0}), "blocked")
        self.assertEqual(outcome_class({"status": "done", "verdict": "FAIL", "ended_at": 1.0}), "died")
        self.assertEqual(outcome_class({"status": "finished", "verdict": "CANCELED", "ended_at": 1.0}), "cancelled")

    def test_ragged_rows_read_unknown_never_raise(self):
        for bad in (None, {}, {"status": "done"}, {"verdict": "PASS"}, {"ended_at": "x"}):
            self.assertEqual(outcome_class(bad), "unk")

    def test_distinct_tokens_per_class(self):
        tokens = {cls: OUTCOME[cls][0] for cls in OUTCOME}
        # Died and blocked differ in colour; cancelled/unknown share grey
        # and stalled/partial share amber by design, but every class has
        # its own glyph plus its own word.
        self.assertNotEqual(tokens["died"], tokens["blocked"])
        glyphs = [OUTCOME[cls][1] for cls in OUTCOME]
        self.assertEqual(len(set(glyphs)), len(glyphs), "two classes share a glyph")
        words = [OUTCOME[cls][2] for cls in OUTCOME]
        self.assertEqual(len(set(words)), len(words), "two classes share a word")

    def test_glyph_and_word_present(self):
        expect = {
            "run": ("\u25cf", "running"),
            "ok": ("\u2713", "finished"),
            "part": ("\u25d0", "partial"),
            "blocked": ("\u2298", "blocked"),
            "died": ("\u2715", "died"),
            "cancelled": ("\u23f9", "cancelled"),
            "stalled": ("\u25d4", "stalled"),
            "unk": ("\u25cb", "no verdict"),
        }
        self.assertEqual(set(OUTCOME), set(expect))
        for cls, (glyph, word) in expect.items():
            self.assertEqual(OUTCOME[cls][1], glyph, cls)
            self.assertEqual(OUTCOME[cls][2], word, cls)

    def test_reasons_name_the_class_in_words(self):
        for run, cls, needle in REASONS:
            with self.subTest(run=run):
                self.assertEqual(outcome_class(run), cls)
                reason = outcome_reason(run, cls)
                if needle is None:
                    self.assertIsNone(reason, run)
                else:
                    self.assertIsNotNone(reason, run)
                    self.assertIn(needle, reason, run)


class ClientMirrorTests(unittest.TestCase):
    """ADM.outcomeOf agrees with dash/outcomes.py on every table row."""

    @classmethod
    def _client_classes(cls):
        rows = []
        for (status, verdict, ended, stalled), _want in CASES:
            rows.append(
                {
                    "status": status,
                    "verdict": verdict,
                    "ended_at": 1792340000.0 if ended else None,
                    "stalled": stalled,
                }
            )
        harness = (
            "const fs=require('fs');"
            "const src=fs.readFileSync(%s,'utf8');"
            "var window={};"
            "eval(src);"
            "var ROWS=%s;"
            "console.log(JSON.stringify(ROWS.map(function(r){return window.ADM.outcomeOf(r);})));"
            % (json.dumps(os.path.join(ROOT, "static", "models.js")), json.dumps(rows))
        )
        proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout)

    def test_client_mirror_matches_server(self):
        got = self._client_classes()
        self.assertEqual(len(got), len(CASES))
        for ((status, verdict, ended, stalled), want), cls in zip(CASES, got):
            with self.subTest(status=status, verdict=verdict):
                self.assertEqual(cls, want)
                self.assertEqual(cls, outcome_class(make_run(status, verdict, ended, stalled)))


if __name__ == "__main__":
    unittest.main()
