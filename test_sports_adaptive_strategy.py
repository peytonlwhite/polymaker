import unittest

from sports_adaptive_strategy import (
    candidate_review,
    edge_bucket,
    empty_state,
    evaluate_state,
    price_bucket,
    segment_keys,
)


def executed_row(index, profit, price=35, clv=-2, edge=3, market_type="moneyline"):
    return {
        "source": "edge_scanner",
        "result": "WIN" if profit > 0 else "LOSS",
        "profit": profit,
        "stake": 10,
        "unit_size": 10,
        "entry_price": price,
        "edge": edge,
        "game_key": f"game-{index}",
        "kalshi_ticker": f"ticker-{index}",
        "sport_key": "baseball_mlb",
        "market_type": market_type,
        "bet_timing_bucket": "mid_live",
        "pricing_v2": {"ask_cents": price},
        "fixed_horizon_clv": {"5m": {"clv_vs_entry_ask_cents": clv}},
        "placed_at": f"2026-07-{(index % 28) + 1:02d}T12:00:00+00:00",
    }


class SportsAdaptiveStrategyTests(unittest.TestCase):
    def test_price_bucket(self):
        self.assertEqual("30-39c", price_bucket(35))
        self.assertEqual("90-99c", price_bucket(99))

    def test_edge_bucket_and_broad_segment_keys(self):
        self.assertEqual("negative", edge_bucket(-0.1))
        self.assertEqual("0-2pp", edge_bucket(1.99))
        self.assertEqual("2-4pp", edge_bucket(2))
        self.assertEqual("4-6pp", edge_bucket(5.99))
        self.assertEqual("6pp-plus", edge_bucket(6))
        keys = segment_keys(executed_row(1, 5, edge=6.1, market_type="spread"))
        self.assertIn("sport_market:baseball_mlb|spread", keys)
        self.assertIn("edge:6pp-plus", keys)
        self.assertIn("sport_market_edge:baseball_mlb|spread|6pp-plus", keys)

    def test_confirmed_price_drawdown_enters_shadow(self):
        rows = [executed_row(index, -10) for index in range(30)]
        state, transitions = evaluate_state(
            {"history": rows},
            {},
            empty_state(),
            generated_at="2026-08-13T12:00:00+00:00",
        )
        self.assertEqual("shadow", state["segments"]["price:30-39c"]["state"])
        self.assertTrue(any(row["to"] == "shadow" for row in transitions))

    def test_confirmed_sport_market_drawdown_enters_shadow_across_price_buckets(self):
        rows = [
            executed_row(
                index,
                -10,
                price=25 + (index % 4) * 10,
                market_type="spread",
            )
            for index in range(30)
        ]
        state, transitions = evaluate_state(
            {"history": rows},
            {},
            empty_state(),
            generated_at="2026-08-13T12:00:00+00:00",
        )
        segment = "sport_market:baseball_mlb|spread"
        self.assertEqual("shadow", state["segments"][segment]["state"])
        self.assertTrue(any(row["segment"] == segment for row in transitions))

    def test_broad_shadow_blocks_candidate_regardless_of_price_bucket(self):
        state = empty_state()
        state["segments"] = {
            "sport_market:baseball_mlb|spread": {"state": "shadow", "reason": "test"}
        }
        candidate = {
            "entry_price": 67,
            "edge": 2.5,
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "bet_timing_bucket": "early_live",
        }
        review = candidate_review(candidate, state)
        self.assertFalse(review["ok"])
        self.assertEqual("shadow", review["state"])

    def test_forced_active_segment_reactivates_without_cooldown(self):
        state = empty_state()
        state["policy"]["forced_active_segments"] = [
            "sport_market:baseball_mlb|spread"
        ]
        state["segments"] = {
            "sport_market:baseball_mlb|spread": {
                "state": "shadow",
                "state_changed_at": "2026-09-04T10:00:00+00:00",
            }
        }
        rows = [executed_row(index, -10, market_type="spread") for index in range(30)]

        updated, transitions = evaluate_state(
            {"history": rows},
            {},
            state,
            generated_at="2026-09-04T11:00:00+00:00",
        )

        segment = updated["segments"]["sport_market:baseball_mlb|spread"]
        self.assertEqual("active", segment["state"])
        self.assertTrue(segment["forced_active"])
        self.assertEqual("forced_active_policy", segment["reason"])
        transition = next(
            row for row in transitions
            if row["segment"] == "sport_market:baseball_mlb|spread"
        )
        self.assertEqual("active", transition["to"])

    def test_forced_active_broad_segment_does_not_override_other_shadow_segments(self):
        state = empty_state()
        state["policy"]["forced_active_segments"] = [
            "sport_market:baseball_mlb|spread"
        ]
        state["segments"] = {
            "sport_market:baseball_mlb|spread": {"state": "shadow"},
            "price:30-39c": {"state": "shadow", "reason": "price_drawdown"},
        }
        candidate = {
            "entry_price": 35,
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "bet_timing_bucket": "early_live",
        }

        review = candidate_review(candidate, state)

        self.assertFalse(review["ok"])
        self.assertEqual("shadow", review["state"])
        forced = next(
            row for row in review["matching_segments"]
            if row["segment"] == "sport_market:baseball_mlb|spread"
        )
        self.assertEqual("active", forced["state"])
        self.assertTrue(forced["forced_active"])

    def test_small_sample_does_not_change_state(self):
        rows = [executed_row(index, -10) for index in range(10)]
        state, transitions = evaluate_state(
            {"history": rows}, {}, empty_state(), generated_at="2026-08-13T12:00:00+00:00"
        )
        self.assertEqual("active", state["segments"]["price:30-39c"]["state"])
        self.assertEqual([], transitions)

    def test_shadow_candidate_is_blocked_and_probation_is_capped(self):
        shadow = empty_state()
        shadow["segments"] = {"price:30-39c": {"state": "shadow", "reason": "test"}}
        candidate = {
            "entry_price": 35,
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "bet_timing_bucket": "mid_live",
        }
        self.assertFalse(candidate_review(candidate, shadow)["ok"])
        shadow["segments"]["price:30-39c"]["state"] = "probation"
        review = candidate_review(candidate, shadow)
        self.assertTrue(review["ok"])
        self.assertEqual(1, review["max_units"])

    def test_shadow_requalifies_to_probation_after_counterfactual_recovery(self):
        state = empty_state()
        state["segments"] = {
            "price:30-39c": {
                "state": "shadow",
                "state_changed_at": "2026-07-01T00:00:00+00:00",
            }
        }
        observations = []
        for index in range(30):
            observations.append({
                "source": "candidate_observation",
                "result": "WIN",
                "would_execute_without_adaptive": True,
                "adaptive_state_at_decision": "shadow",
                "hypothetical_profit_units": 0.2,
                "shadow_clv_5m_cents": 1.0,
                "entry_price": 35,
                "game_key": f"shadow-{index}",
                "kalshi_ticker": f"shadow-ticker-{index}",
                "sport_key": "baseball_mlb",
                "market_type": "moneyline",
                "bet_timing_bucket": "mid_live",
                "pricing_v2": {"ask_cents": 35},
                "settled_at": f"2026-07-{(index % 28) + 1:02d}T12:00:00+00:00",
            })
        updated, transitions = evaluate_state(
            {},
            {"resolved_observations": observations},
            state,
            generated_at="2026-08-13T12:00:00+00:00",
        )
        self.assertEqual("probation", updated["segments"]["price:30-39c"]["state"])
        self.assertEqual("shadow_requalified", transitions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
