import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import sports_paper_bettor as bot


class SportsGameOddsStrategyTests(unittest.TestCase):
    def test_strategy_has_independent_open_position_count(self):
        portfolio = {
            "bets": [
                {"status": "open", "strategy_owner": "live_campaign", "kalshi_ticker": "MAIN"},
                {"status": "open", "strategy_owner": "trusted_capper", "kalshi_ticker": "CAPPER"},
                {"status": "open", "strategy_owner": "sports_game_odds", "kalshi_ticker": "SGO1"},
                {"status": "settled", "strategy_owner": "sports_game_odds", "kalshi_ticker": "SGO2"},
            ]
        }
        self.assertEqual(1, len(bot.sports_game_odds_open_positions(portfolio)))

    def test_strategy_owner_and_orders_are_fill_or_kill(self):
        candidate = {"sports_game_odds_strategy": {"active": True}}
        self.assertEqual("sports_game_odds", bot.strategy_owner_for_candidate(candidate))
        self.assertEqual("fill_or_kill", bot.sports_live_order_time_in_force(candidate))

    @patch.object(bot, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_quality_ladder_scales_twenty_dollar_unit_to_five_units(self):
        candidate = {
            "game_started": False,
            "edge": 8,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 3,
            "pricing_v2": {"consensus": {"independent_family_count": 3}},
        }
        review = bot.sports_game_odds_unit_review(candidate)
        self.assertEqual(5, review["target_units"])
        self.assertEqual(5 * bot.SPORTS_GAME_ODDS_UNIT_SIZE, review["requested_stake"])

    def test_analytics_are_separate_and_use_fixed_units(self):
        portfolio = {
            "bets": [{"status": "open", "strategy_owner": "sports_game_odds", "stake": 19.8}],
            "history": [
                {
                    "strategy_owner": "sports_game_odds",
                    "result": "WIN",
                    "stake": 20,
                    "profit": 12,
                    "sport_key": "baseball_mlb",
                    "market_type": "moneyline",
                },
                {
                    "strategy_owner": "live_campaign",
                    "result": "LOSS",
                    "stake": 50,
                    "profit": -50,
                },
            ],
        }
        analytics = bot.sports_game_odds_analytics(portfolio)
        self.assertEqual(1, analytics["settled_bets"])
        self.assertEqual(1, analytics["open_bets"])
        self.assertEqual(12, analytics["profit"])
        self.assertAlmostEqual(12 / bot.SPORTS_GAME_ODDS_UNIT_SIZE, analytics["profit_units"])

    @patch.object(bot, "EXECUTION_MODE", "live")
    @patch.object(bot, "SPORTS_PREGAME_ENABLED", True)
    @patch.object(bot, "SPORTS_PREGAME_ALL_OPEN_ENABLED", True)
    def test_review_requires_no_duplicate_but_uses_separate_slot_pool(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        candidate = {
            "kalshi_ticker": "SGO-TICKER",
            "order_side": "yes",
            "market_type": "moneyline",
            "selected_team": "Home Team",
            "home_team": "Home Team",
            "away_team": "Away Team",
            "commence_time": start,
            "game_started": False,
            "game_completed": False,
            "entry_price": 50,
            "edge": 4,
            "estimated_fee_edge_pp": 1,
            "model_prob": 55,
            "confidence_score": 90,
            "kalshi_spread": 2,
            "pricing_v2": {"ok": True, "consensus": {"independent_family_count": 3}},
            "independent_book_family_count": 3,
            "skip_reasons": [],
        }
        main = {
            **candidate,
            "edge": 1,
            "pricing_v2": {"ok": True, "consensus": {"independent_family_count": 3}},
        }
        provider_game = {"home_team": "Home Team", "away_team": "Away Team", "_sports_game_odds": {}}
        main_only_portfolio = {"bets": [{"status": "open", "strategy_owner": "live_campaign", "kalshi_ticker": "OTHER"}]}
        with patch.object(bot, "live_trading_ready", return_value=True), patch.object(
            bot, "pro_decision_score", return_value={"ok": True, "score": 90}
        ), patch.object(bot, "final_bet_score", return_value={"ok": True, "score": 90}):
            review = bot.sports_game_odds_candidate_review(
                main_only_portfolio, dict(candidate), main, provider_game
            )
            duplicate = bot.sports_game_odds_candidate_review(
                {"bets": [{"status": "open", "strategy_owner": "live_campaign", "kalshi_ticker": "SGO-TICKER"}]},
                dict(candidate),
                main,
                provider_game,
            )
        self.assertTrue(review["eligible"])
        self.assertFalse(duplicate["eligible"])
        self.assertIn("sports_game_odds_duplicate_position", duplicate["reasons"])


if __name__ == "__main__":
    unittest.main()
