"""Capacity boundary checks; no exchange or order submission calls."""
from unittest import TestCase
from unittest.mock import patch

import sports_paper_bettor as sports


class SportsCapacityTests(TestCase):
    def test_all_normal_sports_capacity_defaults_are_twelve(self):
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_MAX_OPEN, 12)
        self.assertEqual(sports.SPORTS_GAME_ODDS_MAX_OPEN, 12)
        self.assertEqual(sports.SPORTS_PREGAME_MAX_OPEN, 12)

    def test_configuration_accepts_twelve_for_all_enabled_lanes(self):
        with patch.multiple(sports, EXECUTION_MODE="paper", SPORTS_PREGAME_ENABLED=True,
                            SPORTS_GAME_ODDS_ENABLED=True, SPORTS_GAME_ODDS_API_KEY="unit-test-placeholder",
                            SPORTS_LIVE_CAMPAIGN_ENABLED=True, SPORTS_PREGAME_MAX_OPEN=12,
                            SPORTS_GAME_ODDS_MAX_OPEN=12, SPORTS_LIVE_CAMPAIGN_MAX_OPEN=12):
            sports.validate_config()

    def test_configuration_rejects_thirteen(self):
        for key in ("SPORTS_LIVE_CAMPAIGN_MAX_OPEN", "SPORTS_GAME_ODDS_MAX_OPEN", "SPORTS_PREGAME_MAX_OPEN"):
            with self.subTest(key=key), patch.multiple(
                    sports, EXECUTION_MODE="paper", SPORTS_PREGAME_ENABLED=True,
                    SPORTS_GAME_ODDS_ENABLED=True, SPORTS_GAME_ODDS_API_KEY="unit-test-placeholder",
                    SPORTS_LIVE_CAMPAIGN_ENABLED=True), patch.object(sports, key, 13):
                with self.assertRaisesRegex(RuntimeError, key + " must be between 1 and 12"):
                    sports.validate_config()

    def test_twelfth_pick_reaches_quality_review_but_thirteenth_is_blocked(self):
        portfolio = {"bets": [{"status": "open", "mode": "live", "strategy_owner": "live_campaign"}
                              for _ in range(11)], "history": []}
        candidate = {"game_started": True}
        with patch.object(sports, "configured_live_campaign_bot_count", return_value=1), \
                patch.object(sports, "sports_live_campaign_lane_review", return_value={"eligible": True}) as quality, \
                patch.object(sports, "apply_sports_live_campaign_same_game_guard", side_effect=lambda p, c, r: r):
            self.assertTrue(sports.sports_live_campaign_review(portfolio, candidate, bot_number=1)["eligible"])
            quality.assert_called_once()
            portfolio["bets"].append({"status": "open", "mode": "live", "strategy_owner": "live_campaign"})
            review = sports.sports_live_campaign_review(portfolio, candidate, bot_number=1)
            self.assertFalse(review["eligible"])
            self.assertEqual(review["reason"], "campaign_open_slots_full")
            quality.assert_called_once()

    def test_new_capacity_keeps_existing_aibetpicks_shared_count(self):
        portfolio = {"bets": [{"status": "open", "mode": "live", "strategy_owner": "live_campaign"}
                              for _ in range(11)] + [{"status": "open", "mode": "live",
                                  "source": "aibetpicks", "strategy_owner": "aibetpicks"}]}
        review = sports.sports_live_campaign_review(portfolio, {"game_started": True}, bot_number=1)
        self.assertEqual(review["reason"], "campaign_open_slots_full")
        self.assertEqual(review["bot_live_active_open_count"], 12)
