import unittest

from sports_qualification_v2 import (
    CONFIG_HASH,
    QUALIFICATION_VERSION,
    apply_candidate,
    empty_state,
    evaluate_candidate,
    update_promotion_state,
)


def strong_candidate(skip_reasons=None):
    return {
        "sport_key": "baseball_mlb",
        "market_type": "moneyline",
        "kalshi_ticker": "KXMLBGAME-TEST-HOME",
        "order_side": "yes",
        "entry_price": 54.0,
        "estimated_fee_edge_pp": 0.5,
        "edge": 5.0,
        "confidence_score": 78.0,
        "independent_book_family_count": 3,
        "skip_reasons": list(skip_reasons or []),
        "pricing_v2": {
            "ok": True,
            "ask_cents": 54.0,
            "net_conservative_edge_pp": 5.0,
            "fair_probability": 61.0,
            "consensus": {"independent_family_count": 3},
            "probability_distribution": {
                "median_probability": 61.0,
                "robust_probability": 61.0,
                "all_in_price_cents": 55.0,
                "posterior_p_edge_positive": 0.90,
            },
        },
        "bet_intelligence": {
            "version": "sports-bet-intelligence-v1",
            "source_synchronization": {"score": 80.0, "status": "usable"},
            "market_microstructure": {
                "quality_score": 90.0,
                "orderbook_valid": True,
                "quote_fresh": True,
            },
            "model_disagreement": {
                "probabilities": {
                    "independent_books": 61.0,
                    "ensemble_uncalibrated": 60.0,
                    "kalshi_market": 54.0,
                },
            },
            "cross_market_consistency": {"status": "coherent"},
            "sportsbook_market": {"sharp_signal": "sharp_higher"},
        },
    }


def resolved_row(index, result="WIN", recommended_units=1):
    return {
        "generated_at": f"2026-01-{index + 1:02d}T12:00:00-06:00",
        "settled_at": f"2026-01-{index + 1:02d}T18:00:00-06:00",
        "game_key": f"game-{index}",
        "result": result,
        "hypothetical_profit_units": 0.965 if result == "WIN" else -1.035,
        "shadow_clv_5m_cents": 0.5,
        "qualification_v2": {
            "version": QUALIFICATION_VERSION,
            "config_hash": CONFIG_HASH,
            "segment": "baseball_mlb|moneyline",
            "qualifies": True,
            "recommended_units": recommended_units,
            "evidence_score": 75.0,
            "robust_price_room_cents": 2.0,
            "decision_probability": 70.0,
            "rescue_candidate": True,
        },
    }


class SportsQualificationV2Tests(unittest.TestCase):
    def test_relaxed_half_unit_entry_stays_shadow_until_promoted(self):
        candidate = strong_candidate(["low_confidence"])
        distribution = candidate["pricing_v2"]["probability_distribution"]
        distribution["posterior_p_edge_positive"] = 0.70
        distribution["robust_probability"] = 56.0
        review = evaluate_candidate(candidate)
        self.assertTrue(review["qualifies"])
        self.assertEqual(0.5, review["recommended_units"])
        self.assertEqual(0, review["execution_units"])
        self.assertFalse(review["affects_execution"])

        state = empty_state()
        state["segments"] = {
            "baseball_mlb|moneyline": {
                "status": "promoted",
                "promoted_max_units": 0.5,
                "unit_gates": {},
            },
        }
        promoted_candidate = strong_candidate(["low_confidence"])
        promoted_distribution = promoted_candidate["pricing_v2"]["probability_distribution"]
        promoted_distribution["posterior_p_edge_positive"] = 0.70
        promoted_distribution["robust_probability"] = 56.0
        promoted = apply_candidate(promoted_candidate, state)
        self.assertEqual(0.5, promoted["execution_units"])
        self.assertTrue(promoted["execution_active"])
        self.assertNotIn("low_confidence", promoted_candidate["skip_reasons"])

    def test_strong_direct_evidence_can_qualify_despite_legacy_proxy(self):
        review = evaluate_candidate(strong_candidate(["low_confidence"]))
        self.assertTrue(review["qualifies"])
        self.assertGreaterEqual(review["recommended_units"], 1)
        self.assertTrue(review["rescue_candidate"])
        self.assertFalse(review["affects_execution"])

    def test_promoted_segment_removes_only_proxy_reasons(self):
        state = empty_state()
        state["segments"] = {
            "baseball_mlb|moneyline": {
                "status": "promoted",
                "promoted_max_units": 1,
                "unit_gates": {},
            }
        }
        candidate = strong_candidate(["low_confidence", "edge_not_confirmed_distinct_update"])
        review = apply_candidate(candidate, state)
        self.assertTrue(review["execution_active"])
        self.assertNotIn("low_confidence", candidate["skip_reasons"])
        self.assertIn("edge_not_confirmed_distinct_update", candidate["skip_reasons"])

    def test_non_positive_all_in_edge_is_never_rescued(self):
        candidate = strong_candidate(["below_edge"])
        candidate["pricing_v2"]["probability_distribution"]["robust_probability"] = 54.0
        review = evaluate_candidate(candidate)
        self.assertFalse(review["qualifies"])
        self.assertIn("qualification_v2_non_positive_all_in_edge", review["hard_blockers"])

    def test_legacy_unit_quality_failure_is_tracked_as_rescue(self):
        candidate = strong_candidate([])
        candidate["sports_units"] = {
            "raw_target_units": 0,
            "legacy_raw_target_units": 0,
            "reason": "unit_quality_filter",
        }
        review = evaluate_candidate(candidate)
        self.assertTrue(review["qualifies"])
        self.assertTrue(review["rescue_candidate"])
        self.assertIn("legacy_unit_quality_filter", review["legacy_proxy_blockers"])

    def test_independent_walk_forward_results_promote_one_unit(self):
        rows = [
            resolved_row(index, "LOSS" if index in {2, 7, 12, 19, 24, 28} else "WIN")
            for index in range(30)
        ]
        state = update_promotion_state(
            rows,
            empty_state(),
            min_train_events=18,
            min_validation_events=12,
        )
        segment = state["segments"]["baseball_mlb|moneyline"]
        self.assertEqual(1, segment["promoted_max_units"])
        self.assertTrue(segment["unit_gates"]["1"]["passed"])
        self.assertEqual(0, segment["eligible_max_units"] - 1)

    def test_half_unit_rescue_promotes_before_one_unit(self):
        rows = [
            resolved_row(
                index,
                "LOSS" if index in {2, 7, 12, 19, 24, 28} else "WIN",
                recommended_units=0.5,
            )
            for index in range(30)
        ]
        state = update_promotion_state(
            rows,
            empty_state(),
            min_train_events=18,
            min_validation_events=12,
        )
        segment = state["segments"]["baseball_mlb|moneyline"]
        self.assertEqual(0.5, segment["promoted_max_units"])
        self.assertTrue(segment["unit_gates"]["0.5"]["passed"])
        self.assertFalse(segment["unit_gates"]["1"]["passed"])

    def test_promoted_segment_demotes_after_rolling_deterioration(self):
        prior = empty_state()
        prior["segments"] = {
            "baseball_mlb|moneyline": {
                "status": "promoted",
                "promoted_max_units": 1,
                "unit_gates": {},
            }
        }
        rows = [resolved_row(index, "LOSS") for index in range(20)]
        state = update_promotion_state(
            rows,
            prior,
            min_train_events=18,
            min_validation_events=12,
        )
        segment = state["segments"]["baseball_mlb|moneyline"]
        self.assertEqual(0, segment["promoted_max_units"])
        self.assertEqual("rolling_profit_deterioration", segment["demotion_reason"])

    def test_changed_tier_config_cannot_reuse_stale_promotion(self):
        prior = empty_state()
        prior["config_hash"] = "stale-tier-config"
        prior["segments"] = {
            "baseball_mlb|moneyline": {
                "status": "promoted",
                "promoted_max_units": 5,
                "unit_gates": {},
            }
        }
        state = update_promotion_state([], prior)
        self.assertEqual(CONFIG_HASH, state["config_hash"])
        self.assertEqual({}, state["segments"])


if __name__ == "__main__":
    unittest.main()
