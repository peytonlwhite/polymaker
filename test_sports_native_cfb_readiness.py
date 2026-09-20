"""Autonomous CFB qualification checks; no parser or live order calls."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import sports_paper_bettor as sports


def cfb_spread():
    return {
        "sport_key": "americanfootball_ncaaf", "market_type": "spread",
        "selected_team": "Boston College Eagles", "home_team": "Cincinnati Bearcats",
        "away_team": "Boston College Eagles", "game_started": False,
        "pregame_eligible": True,
        "commence_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "order_side": "no", "market_line": 7.5, "contract_market_line": -7.5,
        "book_line": 7.5, "book_line_model": "exact_line",
        "pricing_v2": {"ok": True, "consensus": {
            "independent_family_count": 3, "line_ladder_interpolated_family_count": 0,
            "observations": [{"family": family, "age_minutes": 1.0}
                             for family in ("a", "b", "c")],
        }},
    }


class NativeCfbReadinessTests(unittest.TestCase):
    def test_late_cfb_state_refresh_deduplicates_events_and_preserves_other_sports(self):
        game = {"id": "cfb-live", "sport_key": "americanfootball_ncaaf",
                "premium_live_state": {"fetched_at": "old", "quarter": 2}}
        tennis = {"id": "cfb-live", "sport_key": "tennis_atp_us_open"}
        pairs = [(game, {"ticker": "YES"}, {"game_started": True}),
                 (game, {"ticker": "NO"}, {"game_started": True}),
                 (tennis, {}, {"game_started": True})]
        fresh = {**game, "premium_live_state": {"fetched_at": "new", "quarter": 3}}
        with patch.object(sports, "game_completed", return_value=False), patch.object(
            sports, "enrich_games_with_configured_live_data",
            return_value=([fresh], {"requested_games": 1, "enriched_games": 1, "errors": []}),
        ) as fetch:
            rows, status = sports.refresh_native_cfb_candidate_state(pairs)
        fetch.assert_called_once_with([game])
        self.assertEqual(1, status["eligible_events"])
        self.assertEqual(1, status["enriched_games"])
        self.assertEqual(["new", "new"], [r[0]["premium_live_state"]["fetched_at"] for r in rows[:2]])
        self.assertIs(tennis, rows[2][0])
        self.assertEqual("old", game["premium_live_state"]["fetched_at"])

    def test_late_cfb_state_failure_does_not_claim_fresh_state(self):
        game = {"id": "cfb-live", "sport_key": "americanfootball_ncaaf",
                "premium_live_state": {"fetched_at": "old"}}
        pairs = [(game, {}, {"game_started": True})]
        with patch.object(sports, "game_completed", return_value=False), patch.object(
            sports, "enrich_games_with_configured_live_data", side_effect=RuntimeError("unavailable"),
        ):
            rows, status = sports.refresh_native_cfb_candidate_state(pairs)
        self.assertEqual(1, status["error_count"])
        self.assertEqual(0, status["enriched_games"])
        self.assertIs(game, rows[0][0])
        self.assertEqual("old", rows[0][0]["premium_live_state"]["fetched_at"])

    def test_native_no_spread_uses_selected_line_for_exact_book_check(self):
        candidate = cfb_spread()
        review = sports.native_pregame_pricing_review(candidate)
        self.assertTrue(review["eligible"], review["failures"])
        self.assertEqual(-7.5, review["contract_line"])
        self.assertEqual(7.5, review["selected_line"])

    def test_native_inverse_spread_still_rejects_wrong_or_nonbinary_lines(self):
        for changes in [{"book_line": -7.5}, {"book_line": 6.5}, {"market_line": 6.5},
                        {"contract_market_line": -7, "market_line": 7, "book_line": 7}]:
            with self.subTest(changes=changes):
                result = sports.native_pregame_pricing_review({**cfb_spread(), **changes})
                self.assertIn("native_pregame_line_mismatch", result["failures"])
        stale = cfb_spread()
        stale["pricing_v2"]["consensus"]["observations"][0]["age_minutes"] = 7.0
        self.assertIn("native_pregame_books_stale", sports.native_pregame_pricing_review(stale)["failures"])
        thin = cfb_spread()
        thin["pricing_v2"]["consensus"]["independent_family_count"] = 2
        self.assertIn("native_pregame_independent_books_below_minimum", sports.native_pregame_pricing_review(thin)["failures"])

    def test_native_direct_spread_and_no_total_keep_original_line_sign(self):
        direct = {**cfb_spread(), "order_side": "yes", "market_line": -7.5, "book_line": -7.5}
        total = {**cfb_spread(), "market_type": "total", "market_line": 51.5,
                 "contract_market_line": 51.5, "book_line": 51.5}
        self.assertTrue(sports.native_pregame_pricing_review(direct)["eligible"])
        self.assertTrue(sports.native_pregame_pricing_review(total)["eligible"])

    def test_cfb_unit_qualification_is_active_without_capper_or_v2_promotion(self):
        candidate = {**cfb_spread(), "kalshi_ticker": "CFB-UNIT-CHECK", "edge": 8.0,
                     "entry_price": 50, "confidence_score": 95, "pro_review": {"score": 110},
                     "final_bet_score": 98, "independent_book_family_count": 4,
                     "qualification_v2": {"execution_active": False, "execution_units": 0}}
        review = sports.sports_unit_candidate_review({}, candidate)
        self.assertTrue(review["eligible"], review["reason"])
        self.assertGreaterEqual(review["additional_units"], 0.5)
        self.assertLessEqual(review["additional_units"], 5)
        self.assertEqual(1.0, review["unit_size_pct"])
        self.assertEqual("legacy_unit_tiers", review["qualification_source"])


if __name__ == "__main__":
    unittest.main()
