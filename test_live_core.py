import unittest

from live_core import quality_core_review


class LiveCoreSizingTests(unittest.TestCase):
    def review(self, **overrides):
        values = {
            "bankroll": 712.0,
            "metrics": {"edge": 16, "confidence": 76},
            "minimums": {"edge": 15, "confidence": 74},
            "elite_minimums": {"edge": 20, "confidence": 80},
            "base_stake_pct": 0.005,
            "base_stake_cap": 4.0,
            "elite_multiplier": 2.0,
            "elite_stake_pct": 0.01,
            "elite_stake_cap": 7.5,
            "min_stake": 1.0,
        }
        values.update(overrides)
        return quality_core_review(**values)

    def test_base_stake_uses_bankroll_not_profit_target(self):
        review = self.review()
        self.assertTrue(review["eligible"])
        self.assertEqual(review["tier"], "base")
        self.assertEqual(review["applied_stake"], 3.56)
        self.assertEqual(review["sizing_basis"], "bankroll_and_quality_only")

    def test_elite_quality_can_double_base_with_hard_caps(self):
        review = self.review(metrics={"edge": 22, "confidence": 84})
        self.assertEqual(review["tier"], "elite")
        self.assertEqual(review["base_stake"], 3.56)
        self.assertEqual(review["applied_stake"], 7.12)

    def test_quality_failure_rejects_bet_instead_of_reducing_stake(self):
        review = self.review(metrics={"edge": 14.99, "confidence": 90})
        self.assertFalse(review["eligible"])
        self.assertEqual(review["applied_stake"], 0.0)
        self.assertEqual(review["failed_metrics"], ["edge"])

    def test_elite_stake_respects_flat_cap(self):
        review = self.review(
            bankroll=5000,
            metrics={"edge": 25, "confidence": 90},
        )
        self.assertEqual(review["base_stake"], 4.0)
        self.assertEqual(review["applied_stake"], 7.5)


if __name__ == "__main__":
    unittest.main()
