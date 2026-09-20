import unittest

from shadow_pricing import analyze_shadow_candidate, apply_final_quality_filters, kelly_metrics


class ShadowPricingTests(unittest.TestCase):
    def test_fee_reduces_edge_and_kelly_stake(self):
        metrics = kelly_metrics(60, 50, 1000, 0.25, 0.10, fee_rate=0.07)
        self.assertAlmostEqual(metrics["raw_edge_pp"], 10.0)
        self.assertAlmostEqual(metrics["estimated_fee_edge_pp"], 1.75)
        self.assertAlmostEqual(metrics["net_edge_pp"], 8.25)
        self.assertLess(metrics["fee_aware_kelly_stake"], metrics["raw_kelly_stake"])

    def test_same_event_position_blocks_shadow_recommendation(self):
        result = analyze_shadow_candidate(
            probability_pct=70,
            price_cents=50,
            bankroll=100,
            capital_base=120,
            open_positions=[{"status": "open", "event": "event-1", "group": "BTC", "stake": 5}],
            event_key="event-1",
            group_key="BTC",
            event_field="event",
            group_field="group",
            min_net_edge_pp=4,
            kelly_fraction=0.25,
            max_stake_pct=0.10,
            min_stake=1,
            max_event_positions=1,
        )
        self.assertEqual(result["decision"], "shadow_skip")
        self.assertEqual(result["fee_aware_correlated_stake"], 0)
        self.assertIn("correlated_event_position_cap", result["reasons"])

    def test_uncorrelated_positive_candidate_gets_recommendation(self):
        result = analyze_shadow_candidate(
            probability_pct=70,
            price_cents=50,
            bankroll=100,
            capital_base=100,
            open_positions=[],
            event_key="event-2",
            group_key="ETH",
            event_field="event",
            group_field="group",
            min_net_edge_pp=4,
            kelly_fraction=0.25,
            max_stake_pct=0.10,
            min_stake=0.50,
            max_event_positions=1,
        )
        self.assertEqual(result["decision"], "shadow_approved")
        self.assertGreater(result["fee_aware_correlated_stake"], 0)

    def test_current_quality_filter_prevents_shadow_approval(self):
        result = analyze_shadow_candidate(
            probability_pct=70,
            price_cents=50,
            bankroll=100,
            capital_base=100,
            open_positions=[],
            event_key="event-3",
            group_key="ETH",
            event_field="event",
            group_field="group",
            min_net_edge_pp=4,
            kelly_fraction=0.25,
            max_stake_pct=0.10,
            min_stake=0.50,
            current_filter_reasons=["low_confidence"],
        )
        self.assertFalse(result["current_filter_pass"])
        self.assertEqual(result["fee_aware_correlated_stake"], 0)
        self.assertIn("current_quality_filter", result["reasons"])

    def test_final_bot_filter_overlays_shadow_decision(self):
        candidate = {
            "skip_reasons": ["grok_veto"],
            "shadow": {
                "decision": "shadow_approved",
                "fee_aware_correlated_stake": 4.25,
                "estimated_contracts": 8.5,
                "reasons": [],
            },
        }
        apply_final_quality_filters([candidate])
        self.assertEqual(candidate["shadow"]["decision"], "shadow_skip")
        self.assertEqual(candidate["shadow"]["fee_aware_correlated_stake"], 0)
        self.assertEqual(candidate["shadow"]["current_filter_reasons"], ["grok_veto"])


if __name__ == "__main__":
    unittest.main()
