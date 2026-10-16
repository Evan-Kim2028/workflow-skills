import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.pricing import cost, family, load


class PricingTest(unittest.TestCase):
    def test_contract_rates(self):
        table = load()
        self.assertEqual(table["opus"], {"input": 15, "output": 75, "cache_read": 1.5, "cache_write": 18.75})
        self.assertEqual(table["sonnet"], {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75})
        self.assertEqual(table["haiku"], {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25})

    def test_family_substring(self):
        self.assertEqual(family("claude-opus-5-5"), "opus")
        self.assertEqual(family("claude-sonnet-4-5"), "sonnet")
        self.assertEqual(family("claude-haiku-4-5"), "haiku")
        self.assertIsNone(family("glm-5.3"))
        self.assertIsNone(family("muse-1"))
        self.assertIsNone(family("grok-4.7"))
        self.assertIsNone(family(None))

    def test_opus_mix(self):
        priced = cost("claude-opus-4", 1000, 2000, 4000, 8000)
        self.assertEqual(round(priced, 3), 0.321)

    def test_custom_table_changes_the_number(self):
        tracker = {
            "opus": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 8.0},
        }
        priced = cost("claude-opus-4", 1000, 2000, 4000, 8000, table=tracker)
        self.assertEqual(round(priced, 4), 0.1088)

    def test_unit_rates(self):
        million = (1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost("opus", *million), 15 + 75 + 1.5 + 18.75)
        self.assertAlmostEqual(cost("sonnet", *million), 3 + 15 + 0.3 + 3.75)
        self.assertAlmostEqual(cost("haiku", *million), 1 + 5 + 0.1 + 1.25)
        self.assertEqual(cost("haiku", 0, 0, 0, 0), 0.0)

    def test_unpriced_and_missing(self):
        self.assertIsNone(cost("glm-5.3", 10, 10, 10, 10))
        self.assertIsNone(cost("muse-1", 10, 1, 1, 1))
        self.assertIsNone(cost("grok-4", 10, 1, 0, 0))
        self.assertIsNone(cost("claude-opus-5-5", None, 1, 1, 1))
        self.assertIsNone(cost("claude-opus-5-5", 1, 1, None, 1))
        self.assertIsNone(cost("", 1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
