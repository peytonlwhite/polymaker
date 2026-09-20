import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import sports_live_feeds
from sports_game_state import (
    game_state_features,
    revalidate_cached_game_state,
    shadow_game_state_probability,
)
from sports_live_feeds import SportradarFeed, enrich_games_with_live_feed, extract_provider_state


class SportsGameStateTests(unittest.TestCase):
    def test_baseball_total_uses_game_score_without_selected_team_difference(self):
        features = game_state_features(
            {
                "sport_key": "baseball_mlb",
                "home_team": "Cardinals",
                "away_team": "Phillies",
                "premium_live_state": {
                    "provider": "espn_scoreboard",
                    "verified_progress_source": True,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "inning": 8,
                    "inning_half": "top",
                    "outs": 1,
                    "home_score": 1,
                    "away_score": 0,
                },
            },
            {"selected_team": "Under 4.5", "market_type": "total"},
        )
        self.assertTrue(features["score_available"])
        self.assertEqual(1, features["score_total"])
        self.assertTrue(features["model_ready"])
        self.assertTrue(features["authoritative_progress"])

    def test_public_tennis_state_derives_selected_differences(self):
        game = {
            "sport_key": "tennis_atp",
            "home_team": "Player One",
            "away_team": "Player Two",
            "premium_live_state": {
                "provider": "espn_scoreboard",
                "verified_progress_source": True,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "server": "Player One",
                "current_set": 2,
                "home_sets_won": 1,
                "away_sets_won": 0,
                "home_current_games": 2,
                "away_current_games": 3,
            },
        }
        features = game_state_features(game, {"selected_team": "Player Two"})
        self.assertEqual(features["selected_sets_diff"], -1)
        self.assertEqual(features["selected_games_diff"], 1)
        self.assertTrue(features["authoritative_progress"])

    def test_stale_provider_progress_is_not_authoritative(self):
        game = {
            "sport_key": "basketball_nba",
            "home_team": "Home",
            "away_team": "Away",
            "premium_live_state": {
                "provider": "espn_scoreboard",
                "verified_progress_source": True,
                "fetched_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                "period": 4,
                "clock": "02:30",
                "home_score": 101,
                "away_score": 98,
            },
        }
        features = game_state_features(game, {"selected_team": "Home"})
        self.assertTrue(features["progress_ready"])
        self.assertFalse(features["provider_fresh"])
        self.assertFalse(features["authoritative_progress"])

    def test_fast_recheck_expires_cached_authoritative_progress(self):
        fetched_at = (datetime.now(timezone.utc) - timedelta(seconds=170)).isoformat()
        refreshed = revalidate_cached_game_state(
            {
                "provider_fetched_at": fetched_at,
                "provider_age_seconds": 170,
                "provider_fresh": True,
                "progress_ready": True,
                "authoritative_progress": True,
                "score_available": True,
                "state_quality": "authoritative_progress",
            },
            elapsed_seconds=20,
        )
        self.assertGreater(refreshed["provider_age_seconds"], 180)
        self.assertFalse(refreshed["provider_fresh"])
        self.assertFalse(refreshed["authoritative_progress"])
        self.assertFalse(refreshed["live_state_fresh"])
        self.assertEqual("score_only", refreshed["state_quality"])

    def test_missing_baseball_half_does_not_become_string_none(self):
        game = {
            "sport_key": "baseball_mlb",
            "home_team": "Home",
            "away_team": "Away",
            "premium_live_state": {
                "provider": "espn_scoreboard",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "inning": 5,
                "home_score": 2,
                "away_score": 1,
            },
        }
        features = game_state_features(game, {"selected_team": "Home"})
        self.assertNotIn("inning_half", features)
        self.assertFalse(features["progress_ready"])
        self.assertFalse(features["authoritative_progress"])

    def test_basketball_provider_state_is_compact(self):
        state = extract_provider_state(
            {
                "game": {
                    "id": "provider-game",
                    "status": "inprogress",
                    "quarter": 4,
                    "clock": "02:30",
                    "home": {"points": 101, "remaining_timeouts": 2},
                    "away": {"points": 98, "remaining_timeouts": 1},
                    "plays": [
                        {
                            "id": "play-1",
                            "clock": "02:30",
                            "event_type": "fieldgoal",
                            "possession": "home",
                        }
                    ],
                }
            },
            "basketball_nba",
        )
        self.assertEqual(state["home_score"], 101)
        self.assertEqual(state["away_score"], 98)
        self.assertEqual(state["period"], 4)
        self.assertNotIn("plays", state)

    def test_shadow_state_probability_never_affects_execution(self):
        game = {
            "sport_key": "basketball_nba",
            "home_team": "Home",
            "away_team": "Away",
            "premium_live_state": {
                "provider": "sportradar",
                "provider_game_id": "provider-game",
                "period": 4,
                "clock": "02:30",
                "home_score": 101,
                "away_score": 98,
            },
        }
        candidate = {
            "selected_team": "Home",
            "minutes_since_start": 105,
            "pregame_probability": 52,
            "model_prob": 52,
            "pricing_v2": {"book_probability": 52},
        }
        features = game_state_features(game, candidate)
        shadow = shadow_game_state_probability(candidate, features)
        self.assertTrue(features["model_ready"])
        self.assertTrue(features["authoritative_progress"])
        self.assertEqual(features["state_quality"], "authoritative_progress")
        self.assertGreater(shadow["probability"], shadow["prior_probability"])
        self.assertEqual(shadow["mode"], "shadow_only")
        self.assertFalse(shadow["affects_execution"])

    def test_shadow_state_does_not_double_count_live_book_without_prematch_prior(self):
        candidate = {"model_prob": 60, "pricing_v2": {"book_probability": 60}}
        features = {
            "model_ready": True,
            "sport_key": "baseball_mlb",
            "score_diff_selected": 3,
            "game_progress": 0.8,
        }
        shadow = shadow_game_state_probability(candidate, features)
        self.assertEqual(60.0, shadow["probability"])
        self.assertEqual("prematch_prior_unavailable_prevent_double_count", shadow["reason"])

    def test_football_uses_quarter_and_game_clock(self):
        state = extract_provider_state(
            {
                "game": {
                    "id": "nfl-game",
                    "status": "inprogress",
                    "quarter": 3,
                    "clock": "08:12",
                    "home": {"points": 21},
                    "away": {"points": 17},
                    "down": 2,
                    "distance": 7,
                }
            },
            "americanfootball_nfl",
        )
        self.assertEqual(state["quarter"], 3)
        self.assertEqual(state["clock"], "08:12")
        game = {
            "sport_key": "americanfootball_nfl",
            "home_team": "Home",
            "away_team": "Away",
            "premium_live_state": state,
        }
        features = game_state_features(game, {"selected_team": "Home"})
        self.assertEqual(features["quarter"], 3)
        self.assertAlmostEqual(features["clock_minutes"], 8.2)
        self.assertAlmostEqual(features["regulation_minutes_remaining"], 23.2)
        self.assertTrue(features["authoritative_progress"])
        self.assertNotIn("inning", features)

    def test_score_only_fallback_is_not_authoritative_progress(self):
        features = game_state_features(
            {
                "sport_key": "basketball_nba",
                "home_team": "Home",
                "away_team": "Away",
                "live_score_context": {
                    "source": "odds_api_scores",
                    "period": 3,
                    "clock": "08:00",
                    "scores": [
                        {"name": "Home", "score": 70},
                        {"name": "Away", "score": 68},
                    ],
                },
            },
            {"selected_team": "Home"},
        )
        self.assertTrue(features["progress_ready"])
        self.assertFalse(features["authoritative_progress"])
        self.assertEqual(features["state_quality"], "score_only")

    def test_hockey_and_soccer_use_their_own_progress_fields(self):
        hockey = game_state_features(
            {
                "sport_key": "icehockey_nhl",
                "home_team": "Home",
                "away_team": "Away",
                "premium_live_state": {
                    "period": 3,
                    "clock": "04:30",
                    "home_score": 3,
                    "away_score": 2,
                },
            },
            {"selected_team": "Home"},
        )
        self.assertEqual(hockey["period"], 3)
        self.assertEqual(hockey["clock_minutes"], 4.5)
        self.assertNotIn("quarter", hockey)
        self.assertNotIn("inning", hockey)

        soccer = game_state_features(
            {
                "sport_key": "soccer_epl",
                "home_team": "Home",
                "away_team": "Away",
                "premium_live_state": {
                    "half": 2,
                    "match_minute": 83,
                    "stoppage_time": 4,
                    "home_score": 2,
                    "away_score": 1,
                },
            },
            {"selected_team": "Home"},
        )
        self.assertEqual(soccer["half"], 2)
        self.assertEqual(soccer["match_minute"], 83)
        self.assertEqual(soccer["stoppage_time"], 4)
        self.assertNotIn("clock_minutes", soccer)
        self.assertNotIn("inning", soccer)

    def test_missing_premium_key_is_fail_open(self):
        games = [{"id": "game-1", "sport_key": "baseball_mlb"}]
        enriched, status = enrich_games_with_live_feed(
            games,
            SportradarFeed(""),
        )
        self.assertEqual(enriched, games)
        self.assertFalse(status["enabled"])
        self.assertTrue(status["requires_api_key"])
        self.assertEqual(status["requested_games"], 0)

    def test_sportradar_uses_current_api_key_header(self):
        feed = SportradarFeed("test-key", min_request_interval_seconds=0)
        with patch.object(
            sports_live_feeds,
            "get_json",
            return_value=({"games": []}, {}),
        ) as request:
            feed._schedule("mlb", __import__("datetime").date(2026, 7, 19))
        _url = request.call_args.args[0]
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["Accept"], "application/json")

    def test_sportradar_retries_one_rate_limit_response(self):
        feed = SportradarFeed("test-key", min_request_interval_seconds=0)
        with patch.object(
            sports_live_feeds,
            "get_json",
            side_effect=[
                RuntimeError('HTTP 429: {"message":"Too Many Requests"}'),
                ({"games": []}, {}),
            ],
        ), patch.object(sports_live_feeds.time, "sleep") as sleep:
            rows = feed._schedule("mlb", __import__("datetime").date(2026, 7, 19))
        self.assertEqual(rows, [])
        self.assertEqual(feed.status()["request_count"], 2)
        self.assertEqual(feed.status()["rate_limit_count"], 1)
        sleep.assert_called_once_with(2.0)

    def test_tennis_shadow_uses_server_points_tiebreak_surface_and_format(self):
        game = {
            "sport_key": "tennis_atp",
            "home_team": "Player One",
            "away_team": "Player Two",
            "premium_live_state": {
                "provider": "espn_scoreboard",
                "verified_progress_source": True,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "server": "Player One",
                "current_set": 2,
                "home_sets_won": 1,
                "away_sets_won": 0,
                "home_current_games": 6,
                "away_current_games": 6,
                "home_point_score": "40",
                "away_point_score": "30",
                "break_point": False,
                "tiebreak": True,
                "surface": "Grass",
                "best_of": 5,
                "match_format": "Best of 5",
            },
        }
        candidate = {
            "selected_team": "Player One",
            "pregame_probability": 55,
            "pricing_v2": {"book_probability": 60},
        }
        features = game_state_features(game, candidate)
        shadow = shadow_game_state_probability(candidate, features)
        self.assertTrue(features["selected_serving"])
        self.assertEqual(3, features["selected_point_value"])
        self.assertTrue(features["tiebreak"])
        self.assertEqual("Grass", features["surface"])
        self.assertEqual(5, features["best_of"])
        self.assertTrue(features["authoritative_progress"])
        self.assertGreater(shadow["probability"], shadow["prior_probability"])
        self.assertIn("point_score", shadow["tennis_adjustment_components"])
        self.assertFalse(shadow["affects_execution"])


if __name__ == "__main__":
    unittest.main()
