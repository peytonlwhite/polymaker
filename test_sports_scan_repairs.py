"""Regression checks for data acquisition and scheduling, never order execution."""
import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import sports_paper_bettor as sports
from sports_scan_planner import make_plan
from test_sports_odds_quality import book, fixture


def hot_candidate(**changes):
    return {"kalshi_ticker": "HOT", "event_id": "hot-event", "sport_key": "baseball_mlb",
            "market_type": "spread", "edge": 5, "entry_price": 45,
            "confidence_score": 90, "pro_review": {"score": 95}, "final_bet_score": 95,
            "independent_book_family_count": 3, "game_started": True,
            "skip_reasons": ["edge_not_confirmed_distinct_update"], **changes}


class ScanRepairTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "SPORTS_ODDS_COVERAGE_REPAIR_ENABLED": True,
            "SPORTS_ODDS_COVERAGE_LOOKAHEAD_MINUTES": 180,
            "SPORTS_PRICING_V2_MIN_BOOK_FAMILIES": 2,
            "SPORTS_NATIVE_PREGAME_MIN_BOOK_FAMILIES": 3,
            "SPORTS_MLB_TOTAL_MIN_BOOK_FAMILIES": 4,
            "SPORTS_MLB_LIVE_SPREAD_MIN_EXACT_BOOK_FAMILIES": 2,
            "SPORTS_LIVE_MAX_PRICE_CENTS": 67, "SPORTS_MIN_KALSHI_VOLUME": 10,
            "SPORTS_MAX_SPREAD_CENTS": 5, "SPORTS_CAPPER_ENABLED": False,
            "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED": True,
            "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN": 2,
            "SPORTS_ALTERNATE_ODDS_CACHE": {}, "SPORTS_ODDS_PRIMARY_REGION": "us,us2,eu",
            "SPORTS_ODDS_SECONDARY_REGIONS": "uk", "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED": True,
            "SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS": 5,
            "SPORTS_HOT_RECHECK_ENABLED": True, "SPORTS_HOT_RECHECK_BURST_COUNT": 0,
            "SPORTS_HOT_RECHECK_MAX_BURST_SCANS": 2, "SPORTS_HOT_RECHECK_INTERVAL_SECONDS": 15,
            "SPORTS_LIVE_CAMPAIGN_MIN_PRICE_CENTS": 20, "SPORTS_LIVE_CAMPAIGN_MAX_PRICE_CENTS": 67,
        }.items():
            self.stack.enter_context(patch.object(sports, name, value))
        for name, value in {
            "load_dynamic_actionable_kalshi_series": None,
            "odds_paid_refresh_pacing": {"paid_refresh_multiplier": 1, "shadow_paid_calls_allowed": True},
            "sports_odds_priority_sports": set(), "can_spend_odds_credits": (True, {}, 0),
            "record_odds_spend": {}, "append_jsonl": None, "write_json": None,
        }.items():
            self.stack.enter_context(patch.object(sports, name, return_value=value))
        self.orders = self.stack.enter_context(patch.object(sports, "place_live_kalshi_order"))
        self.addCleanup(self.orders.assert_not_called)

    def test_live_thin_data_scores_allow_acquisition_without_clearing_any_gate(self):
        game, market, candidate = fixture()
        candidate["game_started"] = True
        candidate["skip_reasons"] += ["live_edge_too_low", "live_confidence_too_low"]
        before = copy.deepcopy(candidate)
        self.assertTrue(sports.odds_coverage_repair_eligible(game, market, candidate))
        self.assertEqual(before, candidate)
        for reason in ("campaign_quality_filter", "authoritative_live_derivative_state_required",
                       "low_kalshi_liquidity", "college_market_side_identity_mismatch", "already_has_bet"):
            with self.subTest(reason=reason):
                blocked = {**candidate, "skip_reasons": candidate["skip_reasons"] + [reason]}
                self.assertFalse(sports.odds_coverage_repair_eligible(game, market, blocked))

    def test_coverage_request_fetches_featured_and_alternate_then_independent_region(self):
        game, market, candidate = fixture()
        candidate["game_started"] = True
        candidate["skip_reasons"] += ["live_edge_too_low", "live_confidence_too_low"]
        # Many listed bookmakers do not imply support for the target line.
        primary = {**game, "bookmakers": [book("draftkings", market_key="alternate_spreads")]
                   + [book("wrongline" + str(i), line=-2.5) for i in range(6)]}
        secondary = {**game, "bookmakers": [book("pinnacle")]}
        before = copy.deepcopy(candidate)
        with patch.object(sports, "odds_api_get_json", side_effect=[(primary, {}), (secondary, {})]) as get:
            merged, status = sports.enrich_with_on_demand_alternate_odds([game], [(game, market, candidate)])
        self.assertEqual(2, get.call_count)
        for call in get.call_args_list:
            self.assertEqual({"spreads", "alternate_spreads"}, set(call.kwargs["params"]["markets"].split(",")))
        self.assertEqual("uk", get.call_args.kwargs["params"]["regions"])
        self.assertEqual(1, status["coverage_targets_improved"])
        self.assertTrue(status["coverage_results"][0]["requirement_met"])
        self.assertEqual(2, sports.odds_exact_line_family_count(merged[0], candidate))
        self.assertEqual(before, candidate)

    def test_family_target_tracks_stricter_existing_lane_requirements(self):
        self.assertEqual(2, sports.odds_coverage_family_target(hot_candidate()))
        self.assertEqual(4, sports.odds_coverage_family_target(hot_candidate(market_type="total")))
        self.assertEqual(3, sports.odds_coverage_family_target(hot_candidate(
            game_started=False, native_pregame_eligible=True)))

    def test_coverage_requests_honor_budget_and_cache(self):
        game, market, candidate = fixture()
        with patch.object(sports, "can_spend_odds_credits", return_value=(False, {}, 0)), \
                patch.object(sports, "odds_api_get_json") as get:
            _, status = sports.enrich_with_on_demand_alternate_odds([game], [(game, market, candidate)])
        get.assert_not_called()
        self.assertEqual(1, status["budget_skips"])
        with patch.object(sports, "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED", False), \
                patch.object(sports, "odds_api_get_json", return_value=(game, {})) as get:
            sports.enrich_with_on_demand_alternate_odds([game], [(game, market, candidate)])
            _, cached = sports.enrich_with_on_demand_alternate_odds([game], [(game, market, candidate)])
        self.assertEqual(1, get.call_count)
        self.assertEqual(1, cached["cache_hits"])

    def test_strong_unconfirmed_edge_gets_hot_recheck_without_confirmation_override(self):
        row = hot_candidate(pricing_v2={"consensus": {"update_token": "provider-a"}})
        before = copy.deepcopy(row)
        self.assertEqual(1, sports.build_hot_recheck_summary([row])["count"])
        watchlist = {}
        with patch.object(sports, "load_watchlist", return_value=watchlist), \
                patch.object(sports, "save_watchlist"), \
                patch.object(sports, "SPORTS_WATCHLIST_CONFIRM_SCANS", 2), \
                patch.object(sports, "SPORTS_WATCHLIST_REQUIRE_DISTINCT_UPDATES", True):
            sports.update_watchlist([row])
            sports.update_watchlist([row])
            self.assertFalse(sports.edge_confirmation_review(row, watchlist)["ok"])
            self.assertEqual(before, row)
            row["pricing_v2"]["consensus"]["update_token"] = "provider-b"
            sports.update_watchlist([row])
            self.assertTrue(sports.edge_confirmation_review(row, watchlist)["ok"])

    def test_hot_recheck_preserves_quality_and_invalid_price_exclusions(self):
        for changes in ({"skip_reasons": ["campaign_quality_filter", "edge_not_confirmed_distinct_update"]},
                        {"confidence_score": 60}, {"final_bet_score": 70}, {"game_completed": True},
                        {"entry_price": 99}, {"edge": float("nan")}, {"independent_book_family_count": 0}):
            with self.subTest(changes=changes):
                self.assertEqual(0, sports.build_hot_recheck_summary([hot_candidate(**changes)])["count"])

    def test_confirmation_queue_has_priority_over_marginal_price_queue(self):
        rows = [hot_candidate(kalshi_ticker="MARGINAL", edge=-0.1, skip_reasons=["below_edge"]), hot_candidate()]
        self.assertEqual("HOT", sports.build_hot_recheck_summary(rows)["candidates"][0]["kalshi_ticker"])

    def report(self):
        now = datetime.now(timezone.utc)
        plan = make_plan([{"kalshi_ticker": "REGULAR", "event_id": "regular", "sport_key": "baseball_mlb",
                           "commence_time": (now - timedelta(minutes=20)).isoformat()}], now=now)
        return {"last_broad_discovery_at": now.isoformat(), "focused_scan_plan": plan,
                "hot_recheck": sports.build_hot_recheck_summary([hot_candidate()]), "scan_duration_seconds": 10}

    def test_hot_burst_runs_before_rotation_then_yields_to_rotation(self):
        report = self.report()
        original_cursor = report["focused_scan_plan"]["cursor"]
        for _ in range(2):
            self.assertEqual(15, sports.next_sports_scan_sleep_seconds(report))
            route = sports.next_sports_scan_route(report)
            self.assertEqual("hot_recheck", route["scan_reason"])
            self.assertEqual(["HOT"], route["tickers"])
        self.assertEqual(30, sports.next_sports_scan_sleep_seconds(report))
        route = sports.next_sports_scan_route(report)
        self.assertEqual("focused_price_refresh", route["scan_reason"])
        self.assertEqual(["REGULAR"], route["tickers"])
        self.assertEqual(original_cursor, report["focused_scan_plan"]["cursor"])

    def test_hot_queue_never_starves_broad_discovery_or_user_wakeup(self):
        report = self.report()
        sports.next_sports_scan_sleep_seconds(report)
        self.assertEqual("regular", sports.next_sports_scan_route(report, woke_early=True)["scan_reason"])
        report["last_broad_discovery_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        route = sports.next_sports_scan_route(report)
        self.assertEqual("regular", route["scan_reason"])
        self.assertIsNone(route["tickers"])


if __name__ == "__main__":
    unittest.main()
