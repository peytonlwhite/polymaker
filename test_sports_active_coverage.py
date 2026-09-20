import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import sports_paper_bettor as sports
from sports_probability import build_consensus


def game(sport="americanfootball_nfl"):
    now = datetime.now(timezone.utc)
    return {"id": "test-event", "sport_key": sport, "home_team": "Home", "away_team": "Away",
            "commence_time": (now + timedelta(minutes=60)).isoformat(),
            "bookmakers": [{"key": key, "last_update": now.isoformat(), "markets": [
                {"key": "h2h", "outcomes": [{"name": "Home", "price": -200},
                                              {"name": "Away", "price": 180}]}]}
                for key in ["pinnacle", "draftkings", "fanduel"]]}


class ActiveSportsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"SPORTS_ALL_ACTIVE_ENABLED": True, "SPORTS_ALL_ACTIVE_INCLUDE_OUTRIGHTS": False,
                            "SPORTS_ODDS_SKIP_INACTIVE": True, "SPORTS_ACTIVE_CATALOG_REFRESH_MINUTES": 15}.items():
            self.stack.enter_context(patch.object(sports, name, value))
        for name in ["log_line", "append_jsonl"]:
            self.stack.enter_context(patch.object(sports, name))
        self.stack.enter_context(patch.object(sports, "trusted_capper_active_sports", return_value=set()))
        self.stack.enter_context(patch.object(sports, "trusted_capper_requested_disabled_sports", return_value=set()))

    def test_catalog_refresh_detects_new_tournaments_within_fifteen_minutes(self):
        cache = {"generated_at": (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(),
                 "sports": [{"key": "tennis_wta_old"}]}
        with patch.object(sports, "load_json_file", return_value=cache):
            self.assertEqual([], sports.load_cached_active_sports())

    def test_catalog_accepts_utc_and_legacy_chicago_timestamps(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        for stamp in [recent.isoformat(), recent.astimezone(sports.LOCAL_TZ).replace(tzinfo=None).isoformat()]:
            with self.subTest(stamp=stamp), patch.object(sports, "load_json_file", return_value={
                    "generated_at": stamp, "sports": [{"key": "americanfootball_nfl"}]}):
                self.assertEqual(1, len(sports.load_cached_active_sports()))

    def test_catalog_rejects_future_timestamp(self):
        with patch.object(sports, "load_json_file", return_value={
                "generated_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "sports": [{"key": "americanfootball_nfl"}]}):
            self.assertEqual([], sports.load_cached_active_sports())

    def test_tennis_expansion_never_reintroduces_inactive_or_futures_rows(self):
        rows = [{"key": "tennis_atp_live", "active": True},
                {"key": "tennis_atp_old", "active": False},
                {"key": "tennis_atp_champion", "active": True, "has_outrights": True},
                {"key": "americanfootball_nfl", "active": True},
                {"key": "americanfootball_nfl_preseason", "active": False}]
        with patch.object(sports, "fetch_active_sports", return_value=rows):
            result = sports.expand_sport_keys(["tennis_atp", "americanfootball_nfl_preseason"])
        self.assertEqual({"tennis_atp_live", "americanfootball_nfl"}, set(result))

    def test_coverage_does_not_call_shadow_or_missing_tennis_ready(self):
        active = [{"key": "cricket_odi", "active": True}, {"key": "americanfootball_nfl", "active": True}]
        candidates = [{"sport_key": "cricket_odi", "skip_reasons": ["cricket_shadow_only"]},
                      {"sport_key": "americanfootball_nfl", "skip_reasons": ["below_edge"]}]
        with patch.object(sports, "load_json_file", return_value={"sports": active}), patch.object(
                sports, "load_schedule_cache", return_value={"sports": {}}):
            result = sports.sport_coverage_report(["tennis_atp"], [r["key"] for r in active], {}, [], candidates)
        rows = {row["sport_key"]: row for row in result["sports"]}
        self.assertEqual("shadow_only", rows["cricket_odi"]["execution_policy"])
        self.assertEqual("waiting_provider_listing", rows["tennis_atp"]["scan_status"])
        self.assertEqual("candidates_evaluated", rows["americanfootball_nfl"]["scan_status"])
        self.assertEqual(0, rows["americanfootball_nfl"]["entry_qualified"])

    def test_soccer_full_game_route_works_without_cappers(self):
        market = {"ticker": "KXEPLGAME-26SEP20FULMUN-MUN",
                  "rules_primary": "Manchester United wins after 90 minutes plus stoppage time (does not include extra time or penalties)."}
        meta = {"KXEPLGAME": {"contract_key": "SOCCERGAMEWIN"}}
        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value={"KXEPLGAME"}), patch.object(
                sports, "_KALSHI_SPORTS_SERIES_META", meta), patch.object(sports, "SPORTS_CAPPER_ENABLED", False):
            self.assertTrue(sports.is_supported_kalshi_series(market))
            self.assertFalse(sports.is_supported_kalshi_series({**market, "rules_primary": "Manchester United wins including extra time"}))

    def test_direct_soccer_series_match_active_league_and_full_event_contract(self):
        catalog = [{"key": "soccer_epl", "title": "EPL", "active": True},
                   {"key": "soccer_usa_mls", "title": "MLS", "active": False}]
        meta = {"KXEPLGAME": {"contract_key": "SOCCERGAMEWIN"},
                "KXEPLSPREAD": {"contract_key": "SOCCERSPREADS"},
                "KXEPLTOTAL": {"contract_key": "SOCCERTOTALS"},
                "KXEPL1HGAME": {"contract_key": "SOCCERGAMEWIN"},
                "KXEPLWINNER": {"contract_key": "SOCCER"},
                "KXMLSGAME": {"contract_key": "SOCCERGAMEWIN"}}
        with patch.object(sports, "load_dynamic_actionable_kalshi_series"), patch.object(
                sports, "fetch_active_sports", return_value=catalog), patch.object(
                sports, "_KALSHI_SPORTS_SERIES_META", meta):
            self.assertEqual(("KXEPLGAME", "KXEPLSPREAD", "KXEPLTOTAL"), sports.active_soccer_market_series())
            self.assertEqual((), sports.active_soccer_market_series(["tennis_atp"]))

    def test_soccer_abbreviations_do_not_match_unrelated_league_suffix(self):
        catalog = [{"key": "soccer_uefa_europa_league", "title": "UEFA Europa League", "active": True},
                   {"key": "soccer_sweden_superettan", "title": "Superettan - Sweden", "active": True}]
        meta = {key: {"contract_key": "SOCCERGAMEWIN"} for key in ["KXUELGAME", "KXALEAGUEGAME", "KXETTANGAME"]}
        with patch.object(sports, "load_dynamic_actionable_kalshi_series"), patch.object(
                sports, "fetch_active_sports", return_value=catalog), patch.object(
                sports, "_KALSHI_SPORTS_SERIES_META", meta):
            self.assertEqual(("KXUELGAME",), sports.active_soccer_market_series())


class SportSettlementPricingTests(unittest.TestCase):
    def test_soccer_two_way_books_cannot_price_regulation_win(self):
        result = build_consensus(game("soccer_epl"), market_type="moneyline", selected_team="Home")
        self.assertFalse(result["ok"])

    def test_soccer_three_way_consensus_preserves_draw_probability(self):
        fixture = game("soccer_epl")
        for book in fixture["bookmakers"]:
            book["markets"][0]["outcomes"].append({"name": "Draw", "price": 240})
        result = build_consensus(fixture, market_type="moneyline", selected_team="Home")
        self.assertTrue(result["ok"])
        self.assertEqual(3, result["independent_family_count"])
        self.assertLess(result["probability"], 60)

    def test_duplicate_soccer_team_outcome_is_not_a_draw(self):
        fixture = game("soccer_epl")
        for book in fixture["bookmakers"]:
            book["markets"][0]["outcomes"].append({"name": "Home", "price": 240})
        self.assertFalse(build_consensus(fixture, market_type="moneyline", selected_team="Home")["ok"])

    def test_soccer_scope_requires_explicit_regulation_settlement(self):
        fixture = game("soccer_epl")
        self.assertTrue(sports.soccer_regulation_market_supported(fixture, {
            "rules_primary": "Home wins after 90 minutes plus stoppage time (does not include extra time or penalties)."}))
        for rules in ["Home wins including extra time and penalties", "Home wins the full match", "Home wins after 90 minutes including extra time"]:
            with self.subTest(rules=rules):
                self.assertFalse(sports.soccer_regulation_market_supported(fixture, {"rules_primary": rules}))

    def test_nfl_tie_bound_caps_favorite_edge_including_fees(self):
        pricing = {"ok": True, "lower_probability": 66, "ask_cents": 57,
                   "estimated_fee_edge_pp": 1.7, "net_edge_pp": 12, "net_conservative_edge_pp": 7}
        sports.apply_football_tie_payout_bound(pricing, {"sport_key": "americanfootball_nfl", "market_type": "moneyline"})
        with patch.object(sports, "SPORTS_PRICING_V2_REQUIRE_CONSERVATIVE_EDGE", False):
            self.assertAlmostEqual(-8.7, sports.pricing_v2_execution_edge(pricing))

    def test_nfl_underdog_bound_does_not_invent_extra_edge(self):
        pricing = {"ok": True, "lower_probability": 42, "ask_cents": 35,
                   "estimated_fee_edge_pp": 1.7, "net_edge_pp": 10, "net_conservative_edge_pp": 5.3}
        sports.apply_football_tie_payout_bound(pricing, {"sport_key": "americanfootball_nfl_preseason", "market_type": "moneyline", "order_side": "no"})
        with patch.object(sports, "SPORTS_PRICING_V2_REQUIRE_CONSERVATIVE_EDGE", False):
            self.assertAlmostEqual(5.3, sports.pricing_v2_execution_edge(pricing))

    def test_tie_bound_does_not_change_spreads_totals_or_tennis(self):
        pricing = {"ok": True, "lower_probability": 66, "ask_cents": 57}
        for sport, market in [("americanfootball_nfl", "spread"), ("americanfootball_nfl", "total"), ("tennis_wta", "moneyline")]:
            result = copy.deepcopy(pricing)
            sports.apply_football_tie_payout_bound(result, {"sport_key": sport, "market_type": market})
            self.assertEqual(pricing, result)

    def test_nfl_order_guard_recomputes_bound_after_quote_moves(self):
        fixture = game()
        candidate = {**fixture, "market_type": "moneyline", "selected_team": "Home", "entry_bid": 43,
                     "entry_price": 44, "kalshi_ticker": "KXNFLGAME-TEST-HOME", "game_started": False}
        with patch.object(sports, "SPORTS_PRICING_V2_ENABLED", True), patch.object(
                sports, "SPORTS_PRICING_V2_ENFORCEMENT", "active"), patch.object(
                sports, "SPORTS_PRICING_V2_REQUIRE_CONSERVATIVE_EDGE", False), patch.object(
                sports, "sports_stream", return_value=None), patch.object(
                sports, "fetch_kalshi_market_by_ticker", return_value={"yes_bid": 53, "yes_ask": 54}):
            candidate["pricing_v2"] = sports.pricing_v2_candidate_review(fixture, candidate, {})
            result = sports.live_order_pricing_guard(candidate)
        self.assertFalse(result["ok"])
        self.assertEqual("order_quote_edge_evaporated", result["error"])
        bound = result["candidate_updates"]["pricing_v2"]["football_tie_payout"]
        self.assertLess(bound["net_edge_bound_pp"], -4)


if __name__ == "__main__":
    unittest.main()
