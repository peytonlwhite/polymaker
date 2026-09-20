import unittest

from sports_candidate_tracking import compact_registry, empty_registry, register_candidates, resolve_candidates


class SportsCandidateTrackingTests(unittest.TestCase):
    def test_registry_compaction_removes_unused_decision_cards(self):
        registry = empty_registry()
        registry["resolved_observations"] = [{
            "pricing_v2": {
                "book_probability": 55,
                "kalshi_mid_probability": 50,
                "probability_distribution": {"draws": list(range(10))},
                "probability_decomposition": {"components": [1, 2, 3]},
            },
            "probability_sizing": {"large": list(range(10))},
            "outcome_probability_challengers": {"large": list(range(10))},
            "game_state_shadow": {"features": {"large": list(range(10))}},
            "bet_intelligence": {"large": list(range(10))},
            "qualification_v2": {"version": "qualification-v2"},
        }]
        changed = compact_registry(registry)
        row = registry["resolved_observations"][0]
        self.assertEqual(6, changed)
        self.assertEqual(55, row["pricing_v2"]["book_probability"])
        self.assertNotIn("probability_distribution", row["pricing_v2"])
        self.assertNotIn("probability_sizing", row)
        self.assertIn("qualification_v2", row)

    def test_rejected_candidate_is_resolved_for_calibration(self):
        candidate = {
            "event_id": "event",
            "game_key": "game",
            "kalshi_ticker": "TICKER",
            "order_side": "no",
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "market_line": None,
            "market_close_time": "2026-08-16T20:00:00Z",
            "skip_reasons": ["edge_not_confirmed_distinct_update"],
            "pricing_v2": {
                "ok": True,
                "version": "same-book-consensus-v2",
                "book_probability": 60,
                "kalshi_mid_probability": 50,
                "fair_probability": 55,
                "calibrated_ensemble_probability": 54,
                "independent_outcome_probability": 60,
                "posterior_p_edge_positive": 0.61,
                "settlement_time_bucket": "30_to_120m",
                "probability_price_band": "40_to_60",
                "probability_distribution": {"median_probability": 54, "lower_probability_90": 48},
                "ask_cents": 52,
                "consensus": {"update_token": "tick-1"},
            },
        }
        registry, added = register_candidates(empty_registry(), [candidate])
        self.assertEqual(1, added)
        registry, resolved = resolve_candidates(registry, {"TICKER": {"result": "no"}})
        self.assertEqual(1, resolved)
        self.assertEqual("WIN", registry["resolved_observations"][0]["result"])
        self.assertFalse(registry["resolved_observations"][0]["would_execute"])
        self.assertFalse(registry["resolved_observations"][0]["would_execute_without_adaptive"])
        self.assertEqual("candidate-outcomes-v5", registry["version"])
        observation = registry["resolved_observations"][0]
        self.assertEqual(54, observation["pricing_v2"]["calibrated_ensemble_probability"])
        self.assertEqual("30_to_120m", observation["settlement_time_bucket"])
        self.assertEqual(0.61, observation["posterior_p_edge_positive"])

    def test_same_provider_update_is_not_duplicated(self):
        candidate = {
            "kalshi_ticker": "TICKER",
            "order_side": "yes",
            "pricing_v2": {
                "ok": True,
                "ask_cents": 40,
                "consensus": {"update_token": "same"},
            },
        }
        registry, first = register_candidates(empty_registry(), [candidate])
        registry, second = register_candidates(registry, [candidate])
        self.assertEqual(1, first)
        self.assertEqual(0, second)

    def test_shadow_observation_tracks_counterfactual_profit_and_clv(self):
        first = {
            "kalshi_ticker": "SHADOW",
            "order_side": "yes",
            "entry_price": 35,
            "skip_reasons": ["adaptive_strategy_shadow"],
            "adaptive_strategy": {"state": "shadow"},
            "pricing_v2": {
                "ok": True,
                "ask_cents": 35,
                "consensus": {"update_token": "tick-1"},
            },
        }
        second = {
            **first,
            "entry_price": 40,
            "pricing_v2": {
                "ok": True,
                "ask_cents": 40,
                "consensus": {"update_token": "tick-2"},
            },
        }
        registry, _ = register_candidates(empty_registry(), [first])
        registry["pending"]["SHADOW|yes"]["observations"][0]["generated_at"] = "2026-08-13T12:00:00+00:00"
        registry, _ = register_candidates(registry, [second])
        registry["pending"]["SHADOW|yes"]["observations"][1]["generated_at"] = "2026-08-13T12:06:00+00:00"
        registry, _ = resolve_candidates(registry, {"SHADOW": {"result": "yes"}})
        resolved = registry["resolved_observations"][0]
        self.assertTrue(resolved["would_execute_without_adaptive"])
        self.assertEqual("shadow", resolved["adaptive_state_at_decision"])
        self.assertEqual(5.0, resolved["shadow_clv_5m_cents"])
        self.assertGreater(resolved["hypothetical_profit_units"], 1.0)

    def test_overseas_shadow_reason_preserves_counterfactual_eligibility(self):
        candidate = {
            "kalshi_ticker": "NPB-SHADOW",
            "order_side": "yes",
            "sport_key": "baseball_npb",
            "market_type": "moneyline",
            "skip_reasons": ["overseas_moneyline_shadow_validation"],
            "pricing_v2": {
                "ok": True,
                "ask_cents": 48,
                "consensus": {"update_token": "npb-1"},
            },
        }
        registry, added = register_candidates(empty_registry(), [candidate])
        self.assertEqual(1, added)
        registry, resolved = resolve_candidates(
            registry,
            {"NPB-SHADOW": {"result": "yes"}},
        )
        self.assertEqual(1, resolved)
        observation = registry["resolved_observations"][0]
        self.assertFalse(observation["would_execute"])
        self.assertTrue(observation["would_execute_without_overseas_rollout"])


if __name__ == "__main__":
    unittest.main()
