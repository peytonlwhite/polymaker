import unittest
from unittest.mock import patch

from sports_live_feeds import (
    PublicScoreboardFeed,
    SportradarFeed,
    enrich_games_with_live_feed,
    extract_espn_state,
)


class SportsLiveFeedTests(unittest.TestCase):
    def test_public_baseball_state_uses_explicit_inning_and_count(self):
        event = {"id": "event", "status": {"period": 7, "type": {"state": "in", "detail": "Top 7th"}}}
        competition = {
            "id": "competition",
            "status": event["status"],
            "situation": {"outs": 2, "balls": 3, "strikes": 2},
        }
        home = {"score": "4", "team": {"displayName": "New York Yankees"}}
        away = {"score": "3", "team": {"displayName": "Atlanta Braves"}}
        state = extract_espn_state(
            event,
            competition,
            {"home": home, "away": away},
            "baseball_mlb",
            fetched_at="2026-08-09T20:00:00+00:00",
        )
        self.assertEqual(state["inning"], 7)
        self.assertEqual(state["inning_half"], "top")
        self.assertEqual(state["outs"], 2)
        self.assertEqual(state["balls"], 3)
        self.assertEqual(state["home_score"], 4)

    def test_public_tennis_state_extracts_server_and_set_games(self):
        event = {"id": "tournament"}
        competition = {
            "id": "match",
            "status": {"period": 2, "type": {"state": "in", "detail": "2nd Set"}},
        }
        home = {
            "athlete": {"displayName": "Player One"},
            "possession": True,
            "linescores": [{"value": 6}, {"value": 2}],
        }
        away = {
            "athlete": {"displayName": "Player Two"},
            "linescores": [{"value": 4}, {"value": 3}],
        }
        state = extract_espn_state(
            event,
            competition,
            {"home": home, "away": away},
            "tennis_atp",
        )
        self.assertEqual(state["server"], "Player One")
        self.assertEqual(state["current_set"], 2)
        self.assertEqual(state["home_sets_won"], 1)
        self.assertEqual(state["home_current_games"], 2)
        self.assertEqual(state["away_current_games"], 3)

    def test_public_scoreboard_is_cached_by_sport_and_day(self):
        feed = PublicScoreboardFeed(cache_seconds=60)
        payload = {"events": []}
        with patch("sports_live_feeds.get_json", return_value=(payload, {})) as request:
            day = __import__("datetime").date(2026, 8, 9)
            self.assertEqual(feed._scoreboard("baseball_mlb", day), payload)
            self.assertEqual(feed._scoreboard("baseball_mlb", day), payload)
        self.assertEqual(request.call_count, 1)

    def test_league_specific_versions_and_play_by_play_paths(self):
        feed = SportradarFeed("test", access_level="production", version="v8")
        captured = []

        def request(url):
            captured.append(url)
            return {"game": {"id": "provider-game"}}, {}

        feed._event_ids = {"mlb-local": "mlb-game", "nba-local": "nba-game"}
        feed._get = request
        feed.play_by_play({"id": "mlb-local", "sport_key": "baseball_mlb"})
        feed.play_by_play({"id": "nba-local", "sport_key": "basketball_nba"})
        self.assertIn("/mlb/production/v8/en/games/mlb-game/play_by_play.json", captured[0])
        self.assertIn("/nba/production/v7/en/games/nba-game/pbp.json", captured[1])

    def test_doubleheader_maps_to_nearest_provider_start(self):
        feed = SportradarFeed("test")
        rows = [
            {
                "id": "game-one",
                "scheduled": "2026-07-19T17:00:00Z",
                "home": {"market": "New York", "name": "Yankees"},
                "away": {"market": "Boston", "name": "Red Sox"},
            },
            {
                "id": "game-two",
                "scheduled": "2026-07-19T23:00:00Z",
                "home": {"market": "New York", "name": "Yankees"},
                "away": {"market": "Boston", "name": "Red Sox"},
            },
        ]
        feed._schedule = lambda _league, _day: rows
        event_id = feed.event_id(
            {
                "id": "odds-game-two",
                "sport_key": "baseball_mlb",
                "home_team": "New York Yankees",
                "away_team": "Boston Red Sox",
                "commence_time": "2026-07-19T22:55:00Z",
            }
        )
        self.assertEqual(event_id, "game-two")

    def test_circuit_breaker_stops_repeated_provider_failures(self):
        feed = SportradarFeed(
            "test",
            min_request_interval_seconds=0,
            failure_threshold=2,
            circuit_cooldown_seconds=60,
        )
        with patch(
            "sports_live_feeds.get_json",
            side_effect=RuntimeError("HTTP 403 trial package unavailable"),
        ) as request:
            with self.assertRaises(RuntimeError):
                feed._get("https://example.test/one")
            with self.assertRaises(RuntimeError):
                feed._get("https://example.test/two")
            with self.assertRaisesRegex(RuntimeError, "circuit_open"):
                feed._get("https://example.test/three")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(feed.status()["circuit_open"])

    def test_enrichment_uses_fallback_without_requests_while_circuit_open(self):
        feed = SportradarFeed(
            "test",
            min_request_interval_seconds=0,
            failure_threshold=1,
            circuit_cooldown_seconds=60,
        )
        with patch(
            "sports_live_feeds.get_json",
            side_effect=RuntimeError("HTTP 401 invalid package"),
        ):
            with self.assertRaises(RuntimeError):
                feed._get("https://example.test/fail")
        games, status = enrich_games_with_live_feed(
            [{"id": "game", "sport_key": "baseball_mlb"}],
            feed,
        )
        self.assertEqual(len(games), 1)
        self.assertEqual(status["requested_games"], 0)
        self.assertEqual(status["fallback_reason"], "sportradar_circuit_open")


if __name__ == "__main__":
    unittest.main()
