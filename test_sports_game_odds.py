import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sports_game_odds import SportsGameOddsFeed, event_matches_candidate, fair_anchor_for_candidate, normalize_event


def sample_event():
    return {
        "eventID": "event-1",
        "sportID": "BASEBALL",
        "leagueID": "MLB",
        "teams": {
            "home": {"names": {"long": "Toronto Blue Jays"}, "score": 2},
            "away": {"names": {"long": "Boston Red Sox"}, "score": 1},
        },
        "status": {
            "startsAt": "2026-08-10T23:07:00Z",
            "started": True,
            "live": True,
            "completed": False,
            "currentPeriodID": "9i",
            "displayLong": "9th Inning",
        },
        "odds": {
            "points-home-game-ml-home": {
                "oddID": "points-home-game-ml-home",
                "statID": "points",
                "periodID": "game",
                "betTypeID": "ml",
                "sideID": "home",
                "fairOddsAvailable": True,
                "fairOdds": "-150",
                "openFairOdds": "-125",
                "closeFairOdds": "-140",
                "byBookmaker": {
                    "fanduel": {"odds": "-155", "available": True, "lastUpdatedAt": "2026-08-11T01:54:49Z"},
                    "closedbook": {"odds": "+120", "available": False, "lastUpdatedAt": "2026-08-10T10:00:00Z"},
                },
            },
            "points-away-game-ml-away": {
                "oddID": "points-away-game-ml-away",
                "statID": "points",
                "periodID": "game",
                "betTypeID": "ml",
                "sideID": "away",
                "fairOddsAvailable": True,
                "fairOdds": "+150",
                "byBookmaker": {
                    "fanduel": {"odds": "+140", "available": True, "lastUpdatedAt": "2026-08-11T01:54:49Z"},
                },
            },
        },
    }


class SportsGameOddsTests(unittest.TestCase):
    def test_normalize_event_produces_existing_game_shape_and_state(self):
        game = normalize_event(sample_event(), fetched_at="2026-08-11T01:55:00Z", requested_sport_key="baseball_mlb", tier="amateur")
        self.assertEqual("baseball_mlb", game["sport_key"])
        self.assertEqual("Toronto Blue Jays", game["home_team"])
        self.assertEqual(1, len(game["bookmakers"]))
        outcomes = game["bookmakers"][0]["markets"][0]["outcomes"]
        self.assertEqual({"Boston Red Sox", "Toronto Blue Jays"}, {row["name"] for row in outcomes})
        self.assertEqual(9, game["live_score_context"]["inning"])
        self.assertEqual("sports_game_odds", game["_odds_source"])
        self.assertEqual(-150, game["_sports_game_odds"]["fair_anchors"]["points-home-game-ml-home"]["fair_odds"])

    def test_fair_anchor_maps_moneyline_selection(self):
        game = normalize_event(sample_event(), fetched_at="2026-08-11T01:55:00Z", requested_sport_key="baseball_mlb")
        anchor = fair_anchor_for_candidate(
            game,
            {"market_type": "moneyline", "selected_team": "Toronto Blue Jays"},
        )
        self.assertTrue(anchor["ok"])
        self.assertEqual(-150, anchor["fair_odds"])
        self.assertAlmostEqual(60.0, anchor["fair_probability"])
        self.assertAlmostEqual(55.556, anchor["open_fair_probability"], places=3)
        self.assertAlmostEqual(58.333, anchor["close_fair_probability"], places=3)
        self.assertAlmostEqual(4.444, anchor["fair_probability_move_from_open_pp"], places=3)

    def test_event_matching_checks_teams_and_start(self):
        candidate = {
            "home_team": "Toronto Blue Jays",
            "away_team": "Boston Red Sox",
            "commence_time": "2026-08-10T23:07:00Z",
        }
        self.assertTrue(event_matches_candidate(sample_event(), candidate))
        self.assertFalse(event_matches_candidate(sample_event(), {**candidate, "home_team": "New York Yankees"}))
        self.assertFalse(event_matches_candidate(sample_event(), {**candidate, "commence_time": "2026-08-11T06:30:00Z"}))

    def test_free_tier_budget_uses_provider_usage_and_daily_pacing(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = SportsGameOddsFeed("key", cache_file=Path(directory) / "cache.json")
            feed.state["usage"] = {
                "fetched_at": "2099-01-01T00:00:00Z",
                "tier": "amateur",
                "monthly_max_entities": 2500,
                "monthly_current_entities": 0,
            }
            with patch.object(feed, "refresh_usage", return_value=feed.state["usage"]):
                budget = feed._budget_status(1)
            self.assertTrue(budget["allowed"])
            self.assertEqual(2300, budget["monthly_target_entities"])

    def test_games_for_candidates_uses_cached_matching_event(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = SportsGameOddsFeed("key", cache_file=Path(directory) / "cache.json", live_cache_minutes=999999)
            feed.state["usage"] = {"fetched_at": "2099-01-01T00:00:00Z", "tier": "amateur"}
            feed.state["events"] = {
                "event-1": {
                    "fetched_at": "2099-01-01T00:00:00Z",
                    "requested_sport_key": "baseball_mlb",
                    "event": sample_event(),
                }
            }
            candidate = {
                "game_key": "game-1",
                "sport_key": "baseball_mlb",
                "home_team": "Toronto Blue Jays",
                "away_team": "Boston Red Sox",
                "commence_time": "2026-08-10T23:07:00Z",
                "game_started": True,
                "edge": 4,
            }
            with patch.object(feed, "refresh_usage", return_value=feed.state["usage"]):
                games, status = feed.games_for_candidates([candidate])
            self.assertEqual(1, len(games))
            self.assertEqual(1, status["cache_hits"])


if __name__ == "__main__":
    unittest.main()
