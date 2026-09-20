import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sports_paper_bettor as sports


def book(key, line=-1.5, updated=None, market_key="spreads"):
    return {"key": key, "last_update": updated or datetime.now(timezone.utc).isoformat(),
            "markets": [{"key": market_key, "outcomes": [
                {"name": "Detroit Tigers", "point": line, "price": -110},
                {"name": "Minnesota Twins", "point": -line, "price": -110},
            ]}]}


def fixture():
    start = datetime.now(timezone.utc) + timedelta(minutes=60)
    event = "KXMLBSPREAD-" + start.astimezone(ZoneInfo("America/New_York")).strftime("%y%b%d%H%M").upper() + "DETMIN"
    game = {"id": "event-1", "sport_key": "baseball_mlb", "home_team": "Minnesota Twins",
            "away_team": "Detroit Tigers", "commence_time": start.isoformat(), "bookmakers": [book("draftkings")]}
    market = {"ticker": event + "-DET2", "event_ticker": event, "series_ticker": "KXMLBSPREAD",
              "title": "Detroit at Minnesota: Detroit wins by over 1.5 runs?", "yes_sub_title": "Detroit",
              "yes_bid": 44, "yes_ask": 45, "no_bid": 55, "no_ask": 56, "volume": 1000, "status": "active"}
    candidate = {**game, "market_type": "spread", "market_line": -1.5, "selected_team": "Detroit Tigers",
                 "entry_price": 45, "kalshi_spread": 1, "kalshi_ticker": market["ticker"],
                 "edge": -1, "confidence_score": 65, "game_completed": False,
                 "skip_reasons": ["pricing_v2_insufficient_independent_books", "low_confidence", "below_edge"],
                 "pricing_v2": {"reason": "pricing_v2_insufficient_independent_books"}}
    return game, market, candidate


class OddsQualityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(sports, "load_dynamic_actionable_kalshi_series"))
        for name, value in {"SPORTS_ODDS_COVERAGE_REPAIR_ENABLED": True,
                            "SPORTS_ODDS_COVERAGE_LOOKAHEAD_MINUTES": 180,
                            "SPORTS_PRICING_V2_MIN_BOOK_FAMILIES": 2,
                            "SPORTS_LIVE_MAX_PRICE_CENTS": 67, "SPORTS_MIN_KALSHI_VOLUME": 10,
                            "SPORTS_MAX_SPREAD_CENTS": 5}.items():
            self.stack.enter_context(patch.object(sports, name, value))

    def test_merge_preserves_market_timestamps_and_does_not_mutate_inputs(self):
        now = datetime.now(timezone.utc)
        old, fresh = (now - timedelta(minutes=20)).isoformat(), now.isoformat()
        primary = {"id": "event", "bookmakers": [book("draftkings", updated=old)]}
        primary["bookmakers"][0]["markets"].append({"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 8.5}, {"name": "Under", "price": -110, "point": 8.5}]})
        supplemental = {"id": "event", "bookmakers": [book("draftkings", updated=fresh)]}
        before = copy.deepcopy([primary, supplemental])
        merged = sports.merge_odds_region_games([primary], [supplemental])[0]
        markets = {m["key"]: m for m in merged["bookmakers"][0]["markets"]}
        self.assertEqual(fresh, markets["spreads"]["last_update"])
        self.assertEqual(old, markets["totals"]["last_update"])
        self.assertEqual(before, [primary, supplemental])
        self.assertEqual(1, sports.odds_exact_line_family_count(merged, {"market_type": "spread", "market_line": -1.5, "selected_team": "Detroit Tigers"}))
        self.assertEqual(0, sports.odds_exact_line_family_count(merged, {"market_type": "total", "market_line": 8.5, "total_side": "Over"}))

    def test_merge_compares_instants_across_offsets(self):
        primary = {"id": "event", "bookmakers": [book("draftkings", updated="2026-09-14T14:00:00Z")]}
        newer = {"id": "event", "bookmakers": [book("draftkings", line=-2.5, updated="2026-09-14T09:01:00-05:00")]}
        result = sports.merge_odds_region_games([primary], [newer])[0]
        self.assertEqual(-2.5, result["bookmakers"][0]["markets"][0]["outcomes"][0]["point"])

    def test_untimestamped_market_cannot_replace_verified_snapshot(self):
        primary = {"id": "event", "bookmakers": [book("draftkings")]}
        unknown = {"id": "event", "bookmakers": [book("draftkings", line=-2.5)]}
        unknown["bookmakers"][0].pop("last_update")
        result = sports.merge_odds_region_games([primary], [unknown])[0]
        self.assertEqual(-1.5, result["bookmakers"][0]["markets"][0]["outcomes"][0]["point"])

    def test_thin_book_evidence_can_be_refreshed_before_score_gates(self):
        game, market, candidate = fixture()
        before = copy.deepcopy(candidate)
        self.assertTrue(sports.odds_coverage_repair_eligible(game, market, candidate))
        self.assertEqual(before, candidate)
        self.assertIn("below_edge", candidate["skip_reasons"])

    def test_coverage_probe_preserves_structure_liquidity_and_time_constraints(self):
        for change in ["future", "partial", "wrong_team", "wide", "low_volume", "high_price", "unknown_failure", "completed"]:
            game, market, candidate = fixture()
            if change == "future": game["commence_time"] = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
            if change == "partial": market["series_ticker"] = "KXMLBINNINGSPREAD"
            if change == "wrong_team": game["away_team"] = "New York Yankees"
            if change == "wide": candidate["kalshi_spread"] = 10
            if change == "low_volume": market["volume"] = 0
            if change == "high_price": candidate["entry_price"] = 90
            if change == "unknown_failure": candidate["skip_reasons"].append("college_identity_unverified")
            if change == "completed": candidate["game_completed"] = True
            with self.subTest(change=change):
                self.assertFalse(sports.odds_coverage_repair_eligible(game, market, candidate))

    def test_probe_pauses_under_pressure_and_skips_sufficient_coverage(self):
        game, market, candidate = fixture()
        self.assertFalse(sports.odds_coverage_repair_eligible(game, market, candidate, shadow_paid_calls_allowed=False))
        game["bookmakers"].append(book("fanduel"))
        self.assertFalse(sports.odds_coverage_repair_eligible(game, market, candidate))

    def test_exact_coverage_excludes_correlated_stale_and_other_line_books(self):
        game, _market, candidate = fixture()
        game["bookmakers"] = [book("fanatics"), book("pointsbetus"), book("draftkings", line=-2.5),
                              book("fanduel", updated=(datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat())]
        self.assertEqual(1, sports.odds_exact_line_family_count(game, candidate))

    def test_enrichment_expands_for_exact_line_even_with_many_bookmakers(self):
        game, market, candidate = fixture()
        primary = {**game, "bookmakers": [book("draftkings", market_key="alternate_spreads")] +
                   [book("different" + str(i), line=-2.5, market_key="alternate_spreads") for i in range(6)]}
        secondary = {**game, "bookmakers": [book("pinnacle", market_key="alternate_spreads")]}
        with ExitStack() as stack:
            for name, value in {"SPORTS_CAPPER_ENABLED": False, "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED": True,
                                "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN": 2, "SPORTS_ALTERNATE_ODDS_CACHE": {},
                                "SPORTS_ODDS_PRIMARY_REGION": "us,us2,eu", "SPORTS_ODDS_SECONDARY_REGIONS": "uk",
                                "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED": True, "SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS": 5}.items():
                stack.enter_context(patch.object(sports, name, value))
            for name, value in {"odds_paid_refresh_pacing": {"paid_refresh_multiplier": 1.0},
                                "sports_odds_priority_sports": set(), "can_spend_odds_credits": (True, {}, 0),
                                "record_odds_spend": {}, "append_jsonl": None}.items():
                stack.enter_context(patch.object(sports, name, return_value=value))
            get = stack.enter_context(patch.object(sports, "odds_api_get_json", side_effect=[(primary, {}), (secondary, {})]))
            orders = stack.enter_context(patch.object(sports, "place_live_kalshi_order"))
            merged, status = sports.enrich_with_on_demand_alternate_odds([game], [(game, market, candidate)])
        self.assertEqual(1, status["coverage_probe_events"])
        self.assertEqual(1, status["exact_line_secondary_requests"])
        self.assertEqual(1, status["coverage_targets_improved"])
        self.assertEqual(2, get.call_count)
        self.assertEqual(2, sports.odds_exact_line_family_count(merged[0], candidate))
        self.assertEqual(-1, candidate["edge"])
        self.assertEqual(65, candidate["confidence_score"])
        orders.assert_not_called()

    def test_recent_usage_recovers_missing_hours_without_double_counting(self):
        now = datetime(2026, 9, 14, 12)
        shared = {"at": "2026-09-14T11:30:00", "actual_cost": 10}
        budget = {"state": {"entries": [shared, {"at": "2026-09-14T10:30:00", "actual_cost": 3}]}}
        usage = {"calls": [shared, {"at": "2026-09-14T09:30:00", "actual_cost": 7},
                           {"at": "2026-09-14T11:31:00", "actual_cost": 0},
                           {"at": "2026-09-14T12:30:00", "actual_cost": 99}]}
        result = sports.odds_recent_spend(now, budget, usage)
        self.assertEqual(20, result["observed_daily_credits"])
        self.assertEqual(10, result["last_hour_credits"])
        self.assertEqual(2, result["last_hour_calls"])

    def test_budget_report_prefers_zero_provider_cycle_and_preserves_local_total(self):
        budget = {"state": {}, "monthly_credits_used": 1234, "daily_credits_used": 10}
        operating = {"operating_credits_used": 0, "operating_credits_remaining": 4500000}
        with patch.object(sports, "odds_budget_usage", return_value=budget), patch.object(sports, "odds_provider_account_summary", return_value={}), patch.object(sports, "odds_operating_credit_usage", return_value=operating), patch.object(sports, "odds_paid_refresh_pacing", return_value={"observed_daily_credits": 20}):
            result = sports.odds_budget_report()
        self.assertEqual(0, result["monthly_credits_used"])
        self.assertEqual(1234, result["local_calendar_credits_used"])
        self.assertEqual(20, result["daily_credits_used"])
        self.assertEqual(1234, budget["monthly_credits_used"])

    def test_dashboard_zero_cycle_usage_does_not_reuse_old_local_spend(self):
        import dashboard
        now = datetime.now().astimezone().isoformat()
        report = {"odds_budget": {"operating_credits_used": 0, "operating_usage_source": "provider_reported_active_cycle",
                                 "pacing_recent_credits_per_hour": 10, "last_hour_credits": 20, "last_hour_calls": 5}}
        def read(path, default=None):
            if Path(path).name == 'sports_odds_budget.json':
                return {"entries": [{"at": now, "actual_cost": 999}]}
            if Path(path).name == 'sports_paper_report.json': return report
            return {}
        with patch.object(dashboard, "read_lines", return_value=[]), patch.object(dashboard, "read_json", side_effect=read):
            result = dashboard.build_log_analytics(Path('unused.log'), 'sports')["odds_monthly_projection"]
        self.assertEqual(0, result["used"])
        self.assertEqual(999, result["local_calendar_used"])
        self.assertEqual(240, result["daily_rate"])
        self.assertEqual(20, result["last_hour_credits"])


if __name__ == "__main__":
    unittest.main()
