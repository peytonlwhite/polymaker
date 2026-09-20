import unittest

import sports_paper_bettor as sports
from sports_overseas_rollout import (
    apply_candidate,
    candidate_policy,
    empty_state,
    update_state,
)


def resolved_row(index, sport="baseball_npb", profit=0.5):
    return {
        "generated_at": f"2026-07-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
        "kalshi_ticker": f"OVERSEAS-{index}",
        "order_side": "yes",
        "sport_key": sport,
        "market_type": "moneyline",
        "game_started": False,
        "skip_reasons": ["overseas_moneyline_shadow_validation"],
        "would_execute_without_overseas_rollout": True,
        "result": "WIN",
        "hypothetical_profit_units": profit,
        "independent_book_family_count": 2,
        "shadow_clv_5m_cents": 0.25,
        "pricing_v2": {"calibrated_ensemble_probability": 65},
    }


class SportsOverseasRolloutTests(unittest.TestCase):
    def test_policies_keep_derivatives_live_and_cricket_in_shadow(self):
        self.assertEqual(
            ("validation", "overseas_moneyline_shadow_validation"),
            candidate_policy({
                "sport_key": "baseball_npb",
                "market_type": "moneyline",
                "game_started": False,
            })[:2],
        )
        self.assertEqual(
            "overseas_totals_shadow_only",
            candidate_policy({
                "sport_key": "baseball_kbo",
                "market_type": "total",
                "game_started": False,
            })[1],
        )
        self.assertEqual(
            "overseas_live_state_unavailable",
            candidate_policy({
                "sport_key": "baseball_kbo",
                "market_type": "moneyline",
                "game_started": True,
            })[1],
        )
        self.assertEqual(
            "cricket_shadow_only",
            candidate_policy({
                "sport_key": "cricket_ipl",
                "market_type": "moneyline",
            })[1],
        )
        self.assertEqual(
            "test_cricket_disabled",
            candidate_policy({
                "sport_key": "cricket_test_match",
                "market_type": "moneyline",
            })[1],
        )

    def test_moneyline_auto_promotes_only_after_walk_forward_sample(self):
        state = update_state(
            [resolved_row(index) for index in range(30)],
            empty_state(),
            min_train_events=18,
            min_validation_events=12,
        )
        segment = state["segments"]["baseball_npb|moneyline|pregame"]
        self.assertTrue(segment["execution_active"])
        self.assertEqual("promoted_1u", segment["status"])
        self.assertEqual(18, segment["train"]["event_count"])
        self.assertEqual(12, segment["validation"]["event_count"])
        candidate = {
            "sport_key": "baseball_npb",
            "market_type": "moneyline",
            "skip_reasons": [],
        }
        review = apply_candidate(candidate, state)
        self.assertTrue(review["execution_active"])
        self.assertEqual([], candidate["skip_reasons"])
        self.assertEqual(
            1,
            min(cap["max_units"] for cap in sports.sports_unit_risk_caps(candidate)),
        )
        self.assertEqual(
            1,
            sports.sports_game_odds_post_ai_unit_cap(candidate, {})["max_units"],
        )

    def test_standard_failures_do_not_count_toward_promotion(self):
        rows = [resolved_row(index) for index in range(30)]
        for row in rows:
            row["skip_reasons"].append("same_book_consensus_unavailable")
            row["would_execute_without_overseas_rollout"] = False
        state = update_state(rows, empty_state())
        segment = state["segments"]["baseball_npb|moneyline|pregame"]
        self.assertFalse(segment["execution_active"])
        self.assertEqual(0, segment["eligible_event_count"])

    def test_disabled_rollout_is_fail_closed(self):
        candidate = {
            "sport_key": "baseball_npb",
            "market_type": "moneyline",
            "skip_reasons": [],
        }
        review = apply_candidate(candidate, empty_state(), enabled=False)
        self.assertFalse(review["execution_active"])
        self.assertIn("overseas_moneyline_shadow_validation", candidate["skip_reasons"])

    def test_exact_discovery_mappings_exclude_test_cricket(self):
        self.assertEqual(
            "baseball_npb",
            sports.kalshi_market_canonical_sport({"series_ticker": "KXNPBGAME"}),
        )
        self.assertEqual(
            "baseball_kbo",
            sports.kalshi_market_canonical_sport({"series_ticker": "KXKBOGAME"}),
        )
        self.assertEqual(
            "cricket_caribbean_premier_league",
            sports.kalshi_market_canonical_sport({"series_ticker": "KXCPLMATCH"}),
        )
        generic_t20 = sports.kalshi_market_canonical_sport({
            "series_ticker": "KXCRICKETWOMENT20IMATCH"
        })
        self.assertEqual("cricket_t20", generic_t20)
        self.assertTrue(sports.kalshi_sport_identity_matches_provider(
            generic_t20,
            "cricket_t20_world_cup_womens",
        ))
        self.assertFalse(sports.kalshi_sport_identity_matches_provider(
            generic_t20,
            "cricket_test_match",
        ))
        self.assertNotIn("cricket_test_match", sports.KALSHI_MARKET_SERIES_BY_SPORT)
        self.assertEqual("h2h", sports.markets_for_sport("cricket_ipl"))


if __name__ == "__main__":
    unittest.main()
