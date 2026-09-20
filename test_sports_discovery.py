import json
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sports_paper_bettor as sports
import sports_live_campaign as live_campaign


class SportsDiscoveryTests(unittest.TestCase):
    def test_live_timing_buckets_are_sport_specific(self):
        self.assertEqual(sports.candidate_timing_bucket({
            "game_started": True,
            "minutes_since_start": 45,
            "sport_key": "basketball_nba",
        }), "mid_live")
        self.assertEqual(sports.candidate_timing_bucket({
            "game_started": True,
            "minutes_since_start": 45,
            "sport_key": "baseball_mlb",
        }), "early_live")
        self.assertEqual(sports.candidate_timing_bucket({
            "game_started": True,
            "minutes_since_start": 100,
            "sport_key": "tennis_atp",
        }), "mid_live")

    def test_negative_low_edge_clv_restores_sport_floor(self):
        history = []
        for index in range(12):
            history.append({
                "strategy_owner": "live_campaign",
                "result": "WIN" if index % 2 else "LOSS",
                "sport_key": "tennis_atp",
                "edge": 1.5,
                "fixed_horizon_clv": {
                    "5m": {"on_time": True, "clv_vs_entry_ask_cents": -1.5}
                },
            })
        candidate = {
            "kalshi_ticker": "CLV-TEST",
            "sport_key": "tennis_atp",
            "market_type": "moneyline",
            "order_side": "yes",
            "edge": 1.5,
            "confidence_score": 95,
            "pro_review": {"score": 110},
            "final_bet_score": 100,
            "independent_book_family_count": 3,
        }
        with (
            patch.object(sports, "SPORTS_LOW_EDGE_CLV_AUTO_TIGHTEN_ENABLED", True),
            patch.object(sports, "SPORTS_LOW_EDGE_CLV_MIN_SAMPLES", 12),
            patch.object(sports, "SPORTS_LOW_EDGE_CLV_THRESHOLD_CENTS", -1),
            patch.object(sports, "SPORTS_LOW_EDGE_CLV_RESTORED_MIN_EDGE", 2),
        ):
            review = sports.sports_unit_candidate_review(
                {"balance": 3000, "bets": [], "history": history}, candidate, bot_number=1
            )
        self.assertTrue(review["low_edge_clv_review"]["tightened"])
        self.assertFalse(review["eligible"])

    def test_hot_candidate_queue_keeps_only_near_actionable_rows(self):
        candidate = {
            "kalshi_ticker": "HOT-TEST",
            "event_id": "event",
            "sport_key": "baseball_mlb",
            "market_type": "total",
            "selected_team": "Over 8.5",
            "edge": -0.5,
            "entry_price": 45,
            "confidence_score": 90,
            "pro_review": {"score": 95},
            "final_bet_score": 95,
            "independent_book_family_count": 2,
            "game_started": True,
            "game_completed": False,
            "skip_reasons": ["below_edge", "live_edge_too_low"],
        }
        with TemporaryDirectory() as tmp, patch.object(
            sports, "SPORTS_HOT_CANDIDATES_FILE", str(Path(tmp) / "hot.json")
        ), patch.object(sports, "SPORTS_HOT_RECHECK_ENABLED", True):
            summary = sports.build_hot_recheck_summary([candidate])
        self.assertEqual(1, summary["count"])
        self.assertEqual("HOT-TEST", summary["candidates"][0]["kalshi_ticker"])
        route = sports.hot_recheck_route({"hot_recheck": summary})
        self.assertTrue(route["enabled"])
        self.assertEqual(["HOT-TEST"], route["tickers"])
        self.assertEqual(["baseball_mlb"], route["sport_keys"])

    def test_focused_kalshi_fetch_is_bounded_to_exact_tickers(self):
        with patch.object(
            sports,
            "fetch_kalshi_market_by_ticker",
            side_effect=lambda ticker: {"ticker": ticker, "yes_bid": 49, "yes_ask": 50},
        ) as fetch, patch.object(
            sports,
            "market_with_stream_snapshot",
            side_effect=lambda market: (market, {}),
        ):
            markets = sports.fetch_focused_kalshi_markets(["A", "B", "A"])
        self.assertEqual(["A", "B"], [row["ticker"] for row in markets])
        self.assertEqual(2, fetch.call_count)

    def test_adaptive_live_tennis_poll_uses_saved_exact_ticker_route(self):
        route = sports.adaptive_live_tennis_recheck_route({
            "adaptive_live_tennis_recheck": {
                "sport_keys": ["tennis_atp_us_open"],
                "tickers": ["TENNIS-A", "TENNIS-A", "TENNIS-B"],
            }
        })
        self.assertTrue(route["enabled"])
        self.assertEqual(["tennis_atp_us_open"], route["sport_keys"])
        self.assertEqual(["TENNIS-A", "TENNIS-B"], route["tickers"])

    def test_legacy_strategy_owner_backfill_is_explicitly_marked_inferred(self):
        portfolio = {
            "bets": [],
            "history": [
                {"source": "edge_scanner"},
                {"source": "bot_pick"},
                {"source": "manual_live_test"},
                {"source": "unknown"},
            ],
        }
        corrected, counts = sports.backfill_legacy_strategy_owners(portfolio)
        self.assertEqual(3, corrected)
        self.assertEqual("live_campaign", portfolio["history"][0]["strategy_owner"])
        self.assertEqual("bot_pick", portfolio["history"][1]["strategy_owner"])
        self.assertEqual("user_bet", portfolio["history"][2]["strategy_owner"])
        self.assertTrue(portfolio["history"][0]["strategy_owner_inferred"])
        self.assertNotIn("strategy_owner", portfolio["history"][3])
        self.assertEqual(1, counts["live_campaign"])

    def test_football_series_and_provider_keys_are_preseason_ready(self):
        self.assertEqual(
            sports.canonical_sport_key("americanfootball_nfl_preseason"),
            "americanfootball_nfl",
        )
        self.assertIn("KXNCAAFGAME", sports.KALSHI_MARKET_SERIES)
        self.assertIn("KXNCAAFSPREAD", sports.KALSHI_MARKET_SERIES)
        self.assertIn("KXNCAAFTOTAL", sports.KALSHI_MARKET_SERIES)
        self.assertIn("KXNFLGAME", sports.KALSHI_MARKET_SERIES)
        self.assertIn("KXNFLSPREAD", sports.KALSHI_MARKET_SERIES)
        self.assertIn("KXNFLTOTAL", sports.KALSHI_MARKET_SERIES)
        self.assertIn("americanfootball_nfl_preseason", sports.SPORT_KEYS)
        self.assertIn("americanfootball_nfl", sports.SPORT_KEYS)

        launcher = Path("start_sports_loop.ps1").read_text(encoding="utf-8")
        for legacy_series in (
            "KXCFBGAME", "KXCFBSPREAD", "KXCFBTOTAL",
            "KXNCAABGAME", "KXNCAABSPREAD", "KXNCAABTOTAL",
        ):
            self.assertIn(legacy_series, launcher)

    def test_college_series_fetch_uses_bounded_deep_priority_window(self):
        calls = []

        def fetch(*_args, **kwargs):
            calls.append(kwargs)
            return []

        with patch.object(sports, "fetch_kalshi_markets", side_effect=fetch), patch.object(
            sports, "kalshi_market_series_for_sports", return_value=("KXNCAAFTOTAL",)
        ), patch.object(sports, "list_capper_tickets", return_value=[]):
            sports.fetch_kalshi_sports_markets(["americanfootball_ncaaf"])

        self.assertEqual(2, len(calls))
        series_call = calls[1]
        self.assertEqual(1000, series_call["limit"])
        self.assertEqual(5, series_call["max_pages"])
        params = series_call["extra_params"]
        self.assertEqual("KXNCAAFTOTAL", params["series_ticker"])
        self.assertGreater(params["max_close_ts"] - params["min_close_ts"], 72 * 60 * 60)

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_unit_tiers_accept_strictly_positive_net_edge_and_scale_to_five(self):
        base = {
            "kalshi_ticker": "UNIT-TEST",
            "order_side": "yes",
            "confidence_score": 70,
            "pro_review": {"score": 85},
            "final_bet_score": 80,
            "independent_book_family_count": 2,
        }
        no_edge = sports.sports_unit_candidate_review({}, {**base, "edge": 0.0}, bot_number=1)
        one_unit = sports.sports_unit_candidate_review({}, {**base, "edge": 0.01}, bot_number=1)
        five_units = sports.sports_unit_candidate_review({}, {
            **base,
            "edge": 6.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 95,
            "independent_book_family_count": 3,
        }, bot_number=1)
        self.assertFalse(no_edge["eligible"])
        self.assertEqual(no_edge["reason"], "unit_quality_filter")
        self.assertEqual(one_unit["additional_units"], 1)
        self.assertEqual(one_unit["requested_stake"], sports.SPORTS_UNIT_SIZE)
        self.assertEqual(five_units["additional_units"], 5)
        self.assertEqual(five_units["requested_stake"], 5 * sports.SPORTS_UNIT_SIZE)
        self.assertEqual(
            [sports.SPORTS_UNIT_TIERS[units]["min_final_score"] for units in range(1, 6)],
            [80.0, 85.0, 89.0, 92.0, 95.0],
        )
        self.assertEqual(
            [sports.SPORTS_UNIT_TIERS[units]["min_final_score"] for units in (1.5, 2.5, 3.5, 4.5)],
            [82.5, 87.0, 90.5, 93.5],
        )

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_half_unit_quality_tiers_interpolate_between_whole_units(self):
        candidate = {
            "kalshi_ticker": "HALF-UNIT-QUALITY",
            "order_side": "yes",
            "edge": 0.6,
            "confidence_score": 73,
            "pro_review": {"score": 87.5},
            "final_bet_score": 82.5,
            "independent_book_family_count": 2,
        }
        review = sports.sports_unit_candidate_review({}, candidate, bot_number=1)
        self.assertEqual(1.5, review["raw_target_units"])
        self.assertEqual(1.5, review["target_units"])
        self.assertEqual(1.5, review["additional_units"])
        self.assertEqual(1.5 * sports.SPORTS_UNIT_SIZE, review["requested_stake"])
        self.assertEqual([1, 2], review["requirements"]["1.5"]["derived_from_units"])

    def test_probability_sizing_can_promote_high_probability_low_edge_quality_tier(self):
        candidate = {
            "kalshi_ticker": "FAVORITE-PROBABILITY",
            "order_side": "yes",
            "entry_price": 60,
            "estimated_fee_edge_pp": 0,
            "edge": 0.5,
            "confidence_score": 95,
            "pro_review": {"score": 110},
            "final_bet_score": 100,
            "independent_book_family_count": 4,
            "pricing_v2": {
                "ok": True,
                "fair_probability": 75,
                "uncertainty_pp": 8,
                "empirical_uncertainty_pp": 8,
                "p_edge_positive": 0.90,
                "calibration_source": "sport_market:baseball mlb|moneyline",
                "calibration_diagnostics": {
                    "effective_event_count": 100,
                    "calibration_bias_pp": 0,
                    "walk_forward_validation": {"promotion_validated": True, "validation_version": "settlement-purged-hierarchy-v1", "promotion_enabled": True},
                },
            },
        }
        review = sports.sports_unit_candidate_review(
            {"balance": 1000, "bets": [], "history": []}, candidate, bot_number=1
        )
        self.assertEqual(1, review["raw_target_units"])
        self.assertEqual(5, review["non_edge_quality_cap_units"])
        self.assertEqual(3, review["target_units"])
        self.assertEqual("promoted_by_probability_kelly", review["probability_sizing"]["reason"])
        self.assertEqual(3, review["additional_units"])

    def test_probability_sizing_trims_five_unit_longshot_without_skipping_it(self):
        candidate = {
            "kalshi_ticker": "LONGSHOT-PROBABILITY",
            "order_side": "yes",
            "entry_price": 25,
            "estimated_fee_edge_pp": 0,
            "edge": 8,
            "confidence_score": 95,
            "pro_review": {"score": 110},
            "final_bet_score": 100,
            "independent_book_family_count": 4,
            "pricing_v2": {
                "ok": True,
                "fair_probability": 30,
                "uncertainty_pp": 2,
                "empirical_uncertainty_pp": 2,
                "p_edge_positive": 0.99,
                "calibration_source": "sport_market:baseball mlb|moneyline",
                "calibration_diagnostics": {
                    "effective_event_count": 100,
                    "calibration_bias_pp": 0,
                    "walk_forward_validation": {"promotion_validated": True, "validation_version": "settlement-purged-hierarchy-v1", "promotion_enabled": True},
                },
            },
        }
        review = sports.sports_unit_candidate_review(
            {"balance": 1000, "bets": [], "history": []}, candidate, bot_number=1
        )
        self.assertEqual(5, review["raw_target_units"])
        self.assertTrue(review["eligible"])
        self.assertEqual(0.5, review["target_units"])
        self.assertEqual("reduced_by_probability_kelly", review["probability_sizing"]["reason"])

    def test_probability_tail_cap_limits_combined_open_longshot_units(self):
        portfolio = {
            "balance": 920,
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "kalshi_ticker": "OPEN-LONGSHOT",
                "order_side": "yes",
                "stake": 80,
                "sports_units": {"placed_units": 4, "unit_size": 20},
                "probability_sizing": {"calibrated_win_probability": 30},
            }],
            "history": [],
        }
        candidate = {
            "kalshi_ticker": "NEXT-LONGSHOT",
            "order_side": "yes",
            "entry_price": 25,
            "estimated_fee_edge_pp": 0,
            "edge": 1.5,
            "confidence_score": 80,
            "pro_review": {"score": 92},
            "final_bet_score": 87,
            "independent_book_family_count": 3,
            "pricing_v2": {
                "ok": True,
                "fair_probability": 30,
                "uncertainty_pp": 2,
                "empirical_uncertainty_pp": 2,
                "p_edge_positive": 0.99,
                "calibration_source": "global",
                "calibration_diagnostics": {
                    "effective_event_count": 100,
                    "calibration_bias_pp": 0,
                },
            },
        }
        review = sports.sports_unit_candidate_review(portfolio, candidate, bot_number=1)
        self.assertFalse(review["eligible"])
        self.assertEqual("probability_tail_portfolio_cap", review["reason"])
        self.assertEqual(0, review["probability_sizing"]["tail_portfolio_cap"]["remaining_tail_units"])

    def test_probability_calibration_combines_candidate_observations_without_row_count_leakage(self):
        portfolio = {
            "history": [{"result": "WIN", "game_key": "executed", "pricing_v2": {}}],
            "bets": [],
        }
        registry = {
            "resolved_observations": [
                {
                    "result": "WIN",
                    "source": "candidate_observation",
                    "game_key": "same-candidate-game",
                    "pricing_v2": {"book_probability": 55, "kalshi_mid_probability": 50},
                }
                for _index in range(120)
            ]
        }
        with patch.object(sports, "build_calibration_state", return_value={}) as build, patch.object(
            sports, "write_json"
        ):
            state = sports.refresh_probability_calibration(portfolio, registry)
        self.assertEqual(121, len(build.call_args.args[0]))
        self.assertEqual(1, state["resolved_candidate_event_count"])
        self.assertFalse(state["candidate_population_ready"])
        self.assertEqual("executed_plus_resolved_candidate_observations", state["training_population"])

    def test_unit_size_is_one_percent_of_equity_and_open_stake_does_not_shrink_it(self):
        cash_portfolio = {"balance": 3000, "bets": []}
        invested_portfolio = {
            "balance": 2940,
            "bets": [{"status": "open", "mode": "live", "stake": 60}],
        }
        self.assertEqual(sports.sports_unit_bankroll_base(cash_portfolio), 3000)
        self.assertEqual(sports.effective_sports_unit_size(cash_portfolio), 30)
        self.assertEqual(sports.sports_unit_bankroll_base(invested_portfolio), 3000)
        self.assertEqual(sports.effective_sports_unit_size(invested_portfolio), 30)

    def test_unit_bankroll_snapshot_remains_fixed_after_intraday_settlement(self):
        portfolio = {"balance": 3000, "bets": []}
        self.assertEqual(sports.effective_sports_unit_size(portfolio), 30)
        snapshot = dict(portfolio["sports_unit_daily_snapshot"])

        portfolio["balance"] = 3150
        self.assertEqual(sports.sports_unit_bankroll_base(portfolio), 3000)
        self.assertEqual(sports.effective_sports_unit_size(portfolio), 30)
        self.assertEqual(portfolio["sports_unit_daily_snapshot"], snapshot)

    def test_near_zero_unit_snapshot_rebases_after_material_reconciliation(self):
        portfolio = {
            "balance": 1176.75,
            "bets": [],
            "sports_unit_daily_snapshot": {
                "session_key": "2026-08-26|daily_open",
                "local_date": "2026-08-26",
                "bankroll_base": 0.75,
            },
        }
        with patch.object(
            sports,
            "sports_unit_session_identity",
            return_value=("2026-08-26|daily_open", "2026-08-26", None),
        ):
            snapshot, changed = sports.ensure_sports_unit_daily_snapshot(portfolio)
        self.assertTrue(changed)
        self.assertEqual(1176.75, snapshot["bankroll_base"])
        self.assertEqual(11.77, snapshot["unit_size_at_capture"])
        self.assertEqual(
            "material_upward_reconciliation_after_stale_snapshot",
            snapshot["rebase_reason"],
        )

    def test_normal_intraday_equity_increase_does_not_rebase_unit_snapshot(self):
        portfolio = {
            "balance": 1500,
            "bets": [],
            "sports_unit_daily_snapshot": {
                "session_key": "2026-08-26|daily_open",
                "local_date": "2026-08-26",
                "bankroll_base": 1000,
            },
        }
        with patch.object(
            sports,
            "sports_unit_session_identity",
            return_value=("2026-08-26|daily_open", "2026-08-26", None),
        ):
            snapshot, changed = sports.ensure_sports_unit_daily_snapshot(portfolio)
        self.assertFalse(changed)
        self.assertEqual(1000, snapshot["bankroll_base"])

    def test_unit_percentage_change_refreshes_size_without_rebasing_bankroll(self):
        portfolio = {
            "balance": 3500,
            "bets": [],
            "sports_unit_daily_snapshot": {
                "session_key": "2026-08-26|daily_open",
                "local_date": "2026-08-26",
                "bankroll_base": 3000,
                "unit_size_pct_at_capture": 2.0,
                "unit_size_at_capture": 60.0,
            },
        }
        with patch.object(
            sports,
            "sports_unit_session_identity",
            return_value=("2026-08-26|daily_open", "2026-08-26", None),
        ):
            snapshot, changed = sports.ensure_sports_unit_daily_snapshot(portfolio)
        self.assertTrue(changed)
        self.assertEqual(3000, snapshot["bankroll_base"])
        self.assertEqual(1.0, snapshot["unit_size_pct_at_capture"])
        self.assertEqual(30.0, snapshot["unit_size_at_capture"])
        self.assertEqual("unit_size_percentage_changed", snapshot["rebase_reason"])

    def test_stale_unit_snapshot_rolls_to_the_current_betting_day(self):
        portfolio = {
            "balance": 3000,
            "bets": [],
            "sports_unit_daily_snapshot": {
                "session_key": "1999-01-01|daily_open",
                "local_date": "1999-01-01",
                "bankroll_base": 1000,
            },
        }
        self.assertEqual(sports.sports_unit_bankroll_base(portfolio), 3000)
        self.assertNotEqual(
            portfolio["sports_unit_daily_snapshot"]["session_key"],
            "1999-01-01|daily_open",
        )

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_unit_add_on_only_funds_the_improved_target_difference(self):
        portfolio = {
            "balance": sports.STARTING_BALANCE - 45,
            "bets": [{
                "mode": "live",
                "status": "open",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "kalshi_ticker": "ADD-ON",
                "order_side": "yes",
                "stake": 45,
                "sports_units": {"placed_units": 3},
            }]
        }
        candidate = {
            "kalshi_ticker": "ADD-ON",
            "order_side": "yes",
            "edge": 8.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 4,
        }
        review = sports.sports_unit_candidate_review(portfolio, candidate, bot_number=1)
        self.assertTrue(review["eligible"])
        self.assertTrue(review["is_add_on"])
        self.assertEqual(review["existing_units"], 3)
        self.assertEqual(review["additional_units"], 2)
        self.assertEqual(review["requested_stake"], 2 * sports.SPORTS_UNIT_SIZE)

        unchanged = sports.sports_unit_candidate_review(
            portfolio,
            {
                **candidate,
                "edge": 4.1,
                "confidence_score": 82,
                "pro_review": {"score": 95},
                "final_bet_score": 94,
                "independent_book_family_count": 3,
            },
            bot_number=1,
        )
        self.assertFalse(unchanged["eligible"])
        self.assertEqual(unchanged["reason"], "unit_target_already_funded")

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_fractional_existing_units_only_fund_half_unit_shortfall(self):
        portfolio = {
            "balance": sports.STARTING_BALANCE - 22.5,
            "bets": [{
                "mode": "live",
                "status": "open",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "kalshi_ticker": "HALF-ADD-ON",
                "order_side": "yes",
                "stake": 22.5,
                "unit_count": 1.5,
                "sports_units": {"placed_units": 1.5},
            }],
        }
        candidate = {
            "kalshi_ticker": "HALF-ADD-ON",
            "order_side": "yes",
            "edge": 1.6,
            "confidence_score": 79,
            "pro_review": {"score": 92.5},
            "final_bet_score": 87,
            "independent_book_family_count": 2,
        }
        review = sports.sports_unit_candidate_review(portfolio, candidate, bot_number=1)
        self.assertEqual(1.5, review["existing_units"])
        self.assertEqual(2.5, review["target_units"])
        self.assertEqual(1.0, review["additional_units"])

    def test_exposure_capacity_steps_down_in_half_units(self):
        unit_review = {
            "eligible": True,
            "additional_units": 3.0,
            "placed_units": 3.0,
            "requested_stake": 30.0,
            "unit_size": 10.0,
        }
        group = {
            "limits": {"game": 25.0, "team": 100.0, "sport": 100.0},
            "existing": {"game": 0.0, "team": 0.0, "sport": 0.0},
        }
        with (
            patch.object(sports, "effective_live_max_stake", return_value=1000),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "live_exposure", return_value=0),
            patch.object(sports, "trusted_capper_active_sports", return_value=set()),
            patch.object(sports, "live_group_exposure_review", return_value=group),
        ):
            review = sports.apply_unit_exposure_step_down(
                {"balance": 1000, "bets": []},
                {"sport_key": "baseball_mlb"},
                unit_review,
            )
        self.assertEqual(2.5, review["additional_units"])
        self.assertEqual(25.0, review["requested_stake"])

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_mlb_moneyline_cap_does_not_change_raw_quality_tier(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "kalshi_ticker": "MLB-CAP",
            "order_side": "yes",
            "edge": 8.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 3,
        }
        with patch.object(sports, "SPORTS_MLB_MONEYLINE_MAX_UNITS", 1):
            review = sports.sports_unit_candidate_review({}, candidate, bot_number=1)
        self.assertEqual(review["raw_target_units"], 5)
        self.assertEqual(review["target_units"], 1)
        self.assertEqual(review["additional_units"], 1)
        self.assertIn("mlb_moneyline_sample_cap", [row["reason"] for row in review["unit_caps"]])

    def test_mlb_total_guard_rejects_recent_weak_price_and_edge_profiles(self):
        base = {
            "sport_key": "baseball_mlb",
            "market_type": "total",
            "entry_price": 41,
            "edge": 2.49,
            "confidence_score": 98,
            "pro_review": {"score": 107},
            "final_bet_score": 94,
            "independent_book_family_count": 4,
            "sharp_book_count": 2,
        }
        weak_edge = sports.sports_mlb_total_guard(base)
        self.assertFalse(weak_edge["ok"])
        self.assertIn("mlb_total_min_edge", weak_edge["failures"])

        high_price = sports.sports_mlb_total_guard({**base, "entry_price": 61, "edge": 6})
        self.assertFalse(high_price["ok"])
        self.assertIn("mlb_total_price_range", high_price["failures"])

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_mlb_total_guard_accepts_consensus_quality_and_allows_five_units(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "total",
            "kalshi_ticker": "MLB-TOTAL-CAP",
            "order_side": "yes",
            "entry_price": 46,
            "edge": 6.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 5,
            "sharp_book_count": 2,
        }
        guard = sports.sports_mlb_total_guard(candidate)
        self.assertTrue(guard["ok"])
        unit_review = sports.sports_unit_candidate_review({}, candidate, bot_number=1)
        self.assertEqual(5, unit_review["raw_target_units"])
        self.assertEqual(5, unit_review["target_units"])
        self.assertIn("mlb_total_variance_cap", [row["reason"] for row in unit_review["unit_caps"]])

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_active_mlb_moneyline_can_reach_five_units(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "kalshi_ticker": "MLB-FIVE-UNIT",
            "order_side": "yes",
            "edge": 8.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 3,
            "adaptive_strategy": {"enabled": True, "state": "active"},
        }
        review = sports.sports_unit_candidate_review({}, candidate, bot_number=1)
        self.assertEqual(5, review["raw_target_units"])
        self.assertEqual(5, review["target_units"])
        self.assertEqual(5, review["additional_units"])

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_live_missing_state_and_ai_medium_risk_cap_units(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "market_type": "total",
            "game_started": True,
            "game_state_features": {"state_quality": "score_only", "authoritative_progress": False},
            "ai_risk": {
                "enabled": True,
                "effective_provider": "openai",
                "summary": {"risk_level": "medium"},
            },
            "kalshi_ticker": "STATE-CAP",
            "order_side": "yes",
            "edge": 8.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 3,
        }
        with patch.object(sports, "SPORTS_LIVE_MISSING_STATE_MAX_UNITS", 2), patch.object(
            sports, "SPORTS_AI_MEDIUM_RISK_MAX_UNITS", 2
        ):
            review = sports.sports_unit_candidate_review({}, candidate, bot_number=1)
        self.assertEqual(review["raw_target_units"], 5)
        self.assertEqual(review["target_units"], 1)
        self.assertEqual(
            {row["reason"] for row in review["unit_caps"]},
            {"authoritative_game_progress_unavailable", "ai_medium_risk"},
        )

    def test_post_ai_revalidation_recomputes_edge_retention(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "event_id": "event-1",
            "market_type": "total",
            "kalshi_ticker": "REVALIDATE",
            "edge": 8.0,
            "sports_units": {"raw_target_units": 2},
            "pricing_v2": {"ok": True, "consensus": {"ok": True}},
        }

        def apply_pricing(_game, row, _state):
            row["pricing_v2"] = {"ok": True, "consensus": {"ok": True, "independent_family_count": 3}}
            row["independent_book_family_count"] = 3
            row["edge"] = 6.0
            return row

        with patch.object(sports, "SPORTS_POST_AI_ODDS_REVALIDATION_ENABLED", True), patch.object(
            sports, "SPORTS_POST_AI_ODDS_REVALIDATION_MIN_RAW_UNITS", 2
        ), patch.object(sports, "SPORTS_POST_AI_EDGE_STABILITY_SECONDS", 0), patch.object(
            sports, "fetch_post_ai_event_odds", return_value=({"_odds_fetched_at": "now", "_odds_source": "test"}, {"status": "fetched"})
        ), patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=apply_pricing), patch.object(
            sports,
            "live_order_pricing_guard",
            return_value={"ok": True, "source": "test", "candidate_updates": {"edge": 6.0}},
        ), patch.object(sports, "append_jsonl"):
            review = sports.post_ai_revalidate_candidate(candidate, {}, stability_started_at=None)
        self.assertTrue(review["ok"])
        self.assertEqual(review["status"], "confirmed")
        self.assertEqual(review["edge_retention_ratio"], 0.75)

    def test_post_ai_same_tick_keeps_units_after_prior_distinct_confirmation(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "event_id": "event-1",
            "market_type": "moneyline",
            "kalshi_ticker": "REVALIDATE-DISTINCT",
            "edge": 8.0,
            "edge_confirmation": {"ok": True, "distinct_update_count": 2},
            "sports_units": {"raw_target_units": 2},
            "pricing_v2": {
                "ok": True,
                "consensus": {"ok": True, "update_token": "tick-2"},
            },
        }

        def apply_pricing(_game, row, _state):
            row["pricing_v2"] = {
                "ok": True,
                "consensus": {
                    "ok": True,
                    "independent_family_count": 3,
                    "update_token": "tick-2",
                },
            }
            row["independent_book_family_count"] = 3
            row["edge"] = 7.0
            return row

        with patch.object(sports, "SPORTS_POST_AI_REQUIRE_DISTINCT_UPDATE", True), patch.object(
            sports, "SPORTS_POST_AI_EDGE_STABILITY_SECONDS", 0
        ), patch.object(
            sports,
            "fetch_post_ai_event_odds",
            return_value=({"_odds_fetched_at": "now", "_odds_source": "test"}, {"status": "fetched"}),
        ), patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=apply_pricing), patch.object(
            sports,
            "live_order_pricing_guard",
            return_value={"ok": True, "source": "test", "candidate_updates": {"edge": 7.0}},
        ), patch.object(sports, "append_jsonl"):
            review = sports.post_ai_revalidate_candidate(candidate, {}, stability_started_at=None)
        self.assertTrue(review["distinct_update_confirmed"])
        self.assertFalse(review["new_provider_update_confirmed"])
        self.assertTrue(review["prior_distinct_updates_confirmed"])
        self.assertEqual(2, review["distinct_update_count"])
        self.assertEqual(review["status"], "confirmed")

    def test_large_live_bet_uses_longer_stability_window(self):
        with patch.object(sports, "SPORTS_POST_AI_EDGE_STABILITY_SECONDS", 15), patch.object(
            sports, "SPORTS_POST_AI_LARGE_BET_STABILITY_SECONDS", 45
        ):
            self.assertEqual(
                15,
                sports.post_ai_revalidation_stability_seconds({
                    "game_started": True,
                    "sports_units": {"additional_units": 0.5},
                }),
            )
            self.assertEqual(
                45,
                sports.post_ai_revalidation_stability_seconds({
                    "game_started": True,
                    "sports_units": {"additional_units": 1.5},
                }),
            )

    def test_large_live_consensus_reversal_caps_size_at_half_unit(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "event_id": "event-1",
            "market_type": "spread",
            "game_started": True,
            "kalshi_ticker": "LARGE-REVERSAL",
            "entry_price": 40.0,
            "edge": 8.0,
            "edge_confirmation": {"ok": True, "distinct_update_count": 2},
            "sports_units": {"additional_units": 1.5, "raw_target_units": 4},
            "pricing_v2": {
                "ok": True,
                "independent_outcome_probability": 55.0,
                "consensus": {"ok": True, "update_token": "tick-1"},
            },
            "game_state_features": {"authoritative_progress": True},
        }

        def apply_pricing(_game, row, _state):
            row["pricing_v2"] = {
                "ok": True,
                "independent_outcome_probability": 52.5,
                "consensus": {
                    "ok": True,
                    "independent_family_count": 3,
                    "update_token": "tick-2",
                },
            }
            row["independent_book_family_count"] = 3
            row["edge"] = 5.5
            return row

        with patch.object(sports, "SPORTS_POST_AI_EDGE_STABILITY_SECONDS", 0), patch.object(
            sports, "SPORTS_POST_AI_LARGE_BET_STABILITY_SECONDS", 0
        ), patch.object(
            sports, "SPORTS_POST_AI_LARGE_BET_MAX_ADVERSE_CONSENSUS_MOVE_PP", 2
        ), patch.object(
            sports, "SPORTS_POST_AI_LARGE_BET_FALLBACK_MAX_UNITS", 0.5
        ), patch.object(
            sports,
            "fetch_post_ai_event_odds",
            return_value=({"_odds_fetched_at": "now", "_odds_source": "test"}, {"status": "fetched"}),
        ), patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=apply_pricing), patch.object(
            sports,
            "live_order_pricing_guard",
            return_value={"ok": True, "source": "test", "candidate_updates": {"edge": 5.5}},
        ), patch.object(sports, "append_jsonl"):
            review = sports.post_ai_revalidate_candidate(candidate, {}, stability_started_at=None)
        self.assertTrue(review["ok"])
        self.assertEqual("confirmed_large_bet_0_5u_only", review["status"])
        self.assertEqual(-2.5, review["book_consensus_move_pp"])
        self.assertEqual(0.5, review["large_bet_size_cap_units"])
        cap = next(
            row
            for row in sports.sports_unit_risk_caps(candidate)
            if row["reason"] == "post_ai_large_bet_stability_probation"
        )
        self.assertEqual(0.5, cap["max_units"])

    def test_watchlist_confirmation_requires_distinct_provider_updates(self):
        base = {
            "kalshi_ticker": "DISTINCT",
            "order_side": "yes",
            "market_type": "moneyline",
            "selected_team": "Home",
            "pricing_v2": {"consensus": {"update_token": "tick-1"}},
        }
        state = {}
        with patch.object(sports, "load_watchlist", side_effect=lambda: state), patch.object(
            sports, "save_watchlist", side_effect=lambda value: state.update(value)
        ), patch.object(sports, "SPORTS_WATCHLIST_CONFIRM_SCANS", 2), patch.object(
            sports, "SPORTS_WATCHLIST_REQUIRE_DISTINCT_UPDATES", True
        ):
            state = sports.update_watchlist([base])
            self.assertFalse(sports.has_watchlist_confirmation(base, state))
            state = sports.update_watchlist([base])
            self.assertFalse(sports.has_watchlist_confirmation(base, state))
            changed = {**base, "pricing_v2": {"consensus": {"update_token": "tick-2"}}}
            state = sports.update_watchlist([changed])
            self.assertTrue(sports.has_watchlist_confirmation(changed, state))

    def test_probability_of_positive_edge_caps_units_without_blocking_one_unit(self):
        candidate = {"pricing_v2": {"p_edge_positive": 0.52}}
        with patch.object(sports, "SPORTS_P_EDGE_POSITIVE_MIN", 0.55), patch.object(
            sports, "SPORTS_P_EDGE_POSITIVE_MAX_UNITS", 1
        ):
            caps = sports.sports_unit_risk_caps(candidate)
        cap = next(row for row in caps if row["reason"] == "edge_probability_probation_cap")
        self.assertEqual(1, cap["max_units"])

    def test_adaptive_probation_caps_units_without_disabling_volume(self):
        candidate = {
            "adaptive_strategy": {
                "enabled": True,
                "state": "probation",
                "max_units": 1,
                "matching_segments": [{"segment": "price:30-39c", "state": "probation"}],
            }
        }
        caps = sports.sports_unit_risk_caps(candidate)
        cap = next(row for row in caps if row["reason"] == "adaptive_strategy_probation")
        self.assertEqual(1, cap["max_units"])

    def test_every_live_unit_requires_post_ai_revalidation(self):
        live = {"game_started": True, "sports_units": {"raw_target_units": 1}}
        pregame = {"game_started": False, "sports_units": {"raw_target_units": 1}}
        with patch.object(sports, "SPORTS_POST_AI_ODDS_REVALIDATION_ENABLED", True), patch.object(
            sports, "SPORTS_POST_AI_LIVE_MIN_RAW_UNITS", 1
        ), patch.object(sports, "SPORTS_POST_AI_ODDS_REVALIDATION_MIN_RAW_UNITS", 2):
            self.assertTrue(sports.post_ai_revalidation_required(live))
            self.assertFalse(sports.post_ai_revalidation_required(pregame))

    def test_order_guard_uses_raw_unit_tier_edge_floor(self):
        candidate = {
            "sports_units": {
                "raw_target_units": 3,
                "requirements": {3: {"min_edge": 4.0}},
            }
        }
        self.assertEqual(sports.candidate_order_unit_required_edge(candidate), 4.0)

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_close_post_ai_edge_uses_tolerance_without_losing_unit_eligibility(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "event_id": "event-1",
            "market_type": "moneyline",
            "game_started": True,
            "kalshi_ticker": "EDGE-RANGE",
            "order_side": "yes",
            "entry_price": 40.0,
            "edge": 2.1,
            "edge_confirmation": {"ok": True, "distinct_update_count": 2},
            "sports_units": {"raw_target_units": 1},
            "confidence_score": 70,
            "pro_review": {"score": 85},
            "final_bet_score": 85,
            "independent_book_family_count": 2,
            "pricing_v2": {"ok": True, "consensus": {"ok": True}},
        }

        def apply_pricing(_game, row, _state):
            row["pricing_v2"] = {"ok": True, "consensus": {"ok": True, "independent_family_count": 2}}
            row["independent_book_family_count"] = 2
            row["edge"] = 1.7
            return row

        guard = {
            "ok": False,
            "error": "order_quote_edge_evaporated",
            "source": "test",
            "edge": 1.7,
            "required_edge": 2.0,
            "standard_required_edge": 2.0,
            "candidate_updates": {"edge": 1.7, "entry_price": 42.0},
        }
        with patch.object(sports, "SPORTS_POST_AI_EDGE_STABILITY_SECONDS", 0), patch.object(
            sports, "SPORTS_POST_AI_LARGE_BET_STABILITY_SECONDS", 0
        ), patch.object(
            sports, "fetch_post_ai_event_odds", return_value=({"_odds_fetched_at": "now", "_odds_source": "test"}, {"status": "fetched"})
        ), patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=apply_pricing), patch.object(
            sports, "live_order_pricing_guard", return_value=guard
        ), patch.object(sports, "append_jsonl"):
            review = sports.post_ai_revalidate_candidate(candidate, {})
        self.assertTrue(review["ok"])
        self.assertTrue(review["tolerance_approved"])
        self.assertEqual(review["status"], "confirmed_with_tolerance")
        unit_review = sports.sports_unit_candidate_review({}, candidate, bot_number=1)
        self.assertEqual(unit_review["target_units"], 1)
        self.assertEqual(unit_review["metrics"]["edge_qualification_basis"], "post_ai_tolerance")

    def test_live_derivatives_require_authoritative_progress_but_moneyline_does_not(self):
        base = {
            "game_started": True,
            "game_state_features": {"authoritative_progress": False, "state_quality": "score_only"},
        }
        with patch.object(sports, "SPORTS_REQUIRE_AUTHORITATIVE_LIVE_DERIVATIVE_STATE", True), patch.object(
            sports, "SPORTS_ALLOW_SCORE_ONLY_LIVE_DERIVATIVES_1U", False
        ):
            self.assertTrue(sports.live_derivative_state_blocked({**base, "market_type": "total"}))
            self.assertTrue(sports.live_derivative_state_blocked({**base, "market_type": "spread"}))
            self.assertFalse(sports.live_derivative_state_blocked({**base, "market_type": "moneyline"}))

    def test_score_only_live_derivative_fail_soft_requires_fresh_confirmed_market(self):
        candidate = {
            "game_started": True,
            "market_type": "total",
            "has_live_score_context": True,
            "live_odds_age_minutes": 0.5,
            "independent_book_family_count": 2,
            "game_state_features": {"authoritative_progress": False, "state_quality": "score_only"},
        }
        with patch.object(sports, "SPORTS_REQUIRE_AUTHORITATIVE_LIVE_DERIVATIVE_STATE", True), patch.object(
            sports, "SPORTS_ALLOW_SCORE_ONLY_LIVE_DERIVATIVES_1U", True
        ):
            self.assertFalse(sports.live_derivative_state_blocked(candidate))
            self.assertTrue(sports.live_derivative_state_blocked({**candidate, "independent_book_family_count": 1}))
            self.assertTrue(sports.live_derivative_state_blocked({**candidate, "has_live_score_context": False}))

    def test_score_only_mlb_derivative_is_capped_at_half_unit(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "game_started": True,
            "game_state_features": {
                "authoritative_progress": False,
                "state_quality": "score_only",
            },
        }
        with patch.object(sports, "SPORTS_LIVE_MISSING_STATE_MAX_UNITS", 1), patch.object(
            sports, "SPORTS_SCORE_ONLY_MLB_DERIVATIVE_MAX_UNITS", 0.5
        ):
            caps = sports.sports_unit_risk_caps(candidate)
        cap = next(row for row in caps if row["reason"] == "mlb_score_only_derivative_probation")
        self.assertEqual(0.5, cap["max_units"])

    def test_remote_audit_includes_every_nonzero_account_position(self):
        account = {
            "positions_sample": [
                {"ticker": "KXSOL15M-TEST", "position_fp": "-4.00"},
                {"ticker": "KXMLBGAME-TEST", "position_fp": "2.00"},
                {"ticker": "CLOSED-TEST", "position_fp": "0.00"},
            ]
        }
        self.assertEqual(
            sports.remote_position_tickers(account),
            {"KXSOL15M-TEST", "KXMLBGAME-TEST"},
        )

    def test_only_sports_specific_mve_tickers_are_importable(self):
        parlay = "KXMVESPORTSMULTIGAMEEXTENDED-S2026095AFB734BE-5A4AB63D1B8"
        self.assertTrue(sports.sports_ticker_for_manual_import(parlay))
        self.assertEqual(sports.infer_manual_market_type(parlay), "parlay")
        self.assertFalse(sports.sports_ticker_for_manual_import("KXMVEOTHER-TEST"))
        self.assertFalse(sports.sports_ticker_for_manual_import("KXSOL15M-TEST"))

    def test_exact_dynamic_soccer_derivative_is_importable_but_crypto_is_not(self):
        ticker = "KXEFLCHAMPIONSHIPBTTS-26SEP01WHUWOL-BTTS"
        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value=set()), patch.object(
            sports,
            "_KALSHI_SPORTS_SERIES_META",
            {"KXEFLCHAMPIONSHIPBTTS": {"contract_key": "SOCCERBTTS"}},
        ), patch.object(
            sports,
            "trusted_capper_requested_soccer_derivative_series",
            return_value=(),
        ):
            self.assertTrue(sports.sports_ticker_for_manual_import(ticker))
            self.assertFalse(sports.sports_ticker_for_manual_import("KXSOL15M-TEST"))

    def test_unresolved_soccer_ticket_does_not_prioritize_every_provider_league(self):
        with patch.object(sports, "trusted_capper_active_sports", return_value={"soccer"}):
            self.assertTrue(sports.trusted_capper_watches_sport("soccer_epl"))
            self.assertFalse(sports.trusted_capper_watches_exact_sport("soccer_epl"))
        with patch.object(sports, "trusted_capper_active_sports", return_value={"soccer_epl"}):
            self.assertTrue(sports.trusted_capper_watches_exact_sport("soccer_epl"))

    def test_unresolved_soccer_discovery_reuses_broad_live_schedule_cache(self):
        cache = {
            "sports": {
                "soccer_epl": {
                    "generated_at": (datetime.now(sports.LOCAL_TZ) - timedelta(minutes=10)).isoformat(),
                    "markets": "h2h",
                    "games": [{
                        "id": "epl-live",
                        "sport_key": "soccer_epl",
                        "commence_time": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                    }],
                },
            },
        }
        with patch.object(sports, "trusted_capper_active_sports", return_value={"soccer"}), patch.object(
            sports, "SPORTS_CAPPER_BROAD_DISCOVERY_CACHE_MINUTES", 30.0
        ):
            review = sports.cached_odds_entry(cache, "soccer_epl", "h2h")
        self.assertTrue(review["valid"])
        self.assertTrue(review["broad_capper_discovery_lane"])

    def test_live_campaign_uses_unit_sizing_with_one_twelve_slot_bot_and_no_money_caps(self):
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_CYCLE_GOALS, (2.0,) * 5)
        self.assertEqual(sports.configured_live_campaign_bot_count(), 1)
        self.assertEqual(sports.configured_live_campaign_cycle_profit(1), 2)
        self.assertEqual(sports.configured_live_campaign_cycle_count(1), 5)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_MAX_OPEN, 12)
        self.assertEqual(sports.SPORTS_GAME_ODDS_MAX_OPEN, 12)
        self.assertEqual(sports.SPORTS_PREGAME_MAX_OPEN, 12)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP, 0)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP_PCT, 0)
        self.assertFalse(sports.SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_ENABLED)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_PRICE_CENTS, 20)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_CHECK_SECONDS, 30)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_MIN_PRO_SCORE, 85)
        self.assertEqual(sports.SPORTS_SPREAD_MIN_PRO_SCORE, 85)
        self.assertTrue(all(cap == 0 for cap in sports.SPORTS_LIVE_CAMPAIGN_FIRST_LOSS_CAPS.values()))
        self.assertTrue(all(cap == 0 for cap in sports.SPORTS_LIVE_CAMPAIGN_LATER_LOSS_CAPS.values()))
        self.assertFalse(sports.SPORTS_LIVE_CAMPAIGN_PARTIAL_RECOVERY_ENABLED)
        self.assertTrue(sports.SPORTS_UNIT_STAKING_ENABLED)
        self.assertEqual(sports.SPORTS_UNIT_SIZE_PCT, 1)
        self.assertEqual(sports.SPORTS_PROBABILITY_SIZING_KELLY_FRACTION, 0.125)
        self.assertGreater(sports.SPORTS_UNIT_SIZE, 0)
        self.assertEqual(sports.SPORTS_UNIT_MAX_PER_MARKET, 5)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_FULL_RECOVERY_LOSSES, 2)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_PARTIAL_RECOVERY_FRACTION, 0.5)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SAME_GAME_MAX_POSITIONS, 2)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SAME_DIRECTION_MAX_POSITIONS, 1)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_CAP, 0)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_PCT, 0)
        self.assertEqual(sports.SPORTS_MAX_GAME_EXPOSURE_PCT, 0)
        self.assertEqual(sports.SPORTS_MAX_TEAM_EXPOSURE_PCT, 0)
        self.assertEqual(sports.SPORTS_MAX_SPORT_EXPOSURE_PCT, 0)
        self.assertEqual(sports.SPORTS_CAPPER_RESERVED_OPEN_UNITS, 0)
        self.assertEqual(sports.effective_live_max_stake(), 0)
        self.assertEqual(sports.effective_live_daily_loss_cap(), 0)
        self.assertEqual(sports.effective_live_open_exposure_cap(), 0)
        self.assertEqual(sports.SPORTS_MLB_TOTAL_MIN_EDGE, 2.5)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_CONSERVATIVE_EDGE, 3)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_CONFIDENCE, 85)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_PRO_SCORE, 95)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_FINAL_SCORE, 95)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_BOOK_FAMILIES, 2)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_SHARP_BOOKS, 1)
        self.assertTrue(sports.SPORTS_LIVE_CAMPAIGN_OPPOSITE_HEDGE_ENABLED)

    def test_autonomous_open_slots_exclude_capper_user_and_assumed_loss_positions(self):
        base = {"status": "open", "mode": "live", "stake": 10}
        portfolio = {
            "bets": [
                {**base, "strategy_owner": "live_campaign"},
                {**base, "strategy_owner": "sports_game_odds"},
                {**base, "strategy_owner": "trusted_capper", "source": "trusted_capper"},
                {**base, "strategy_owner": "user_bet", "source": "user_manual"},
                {
                    **base,
                    "strategy_owner": "live_campaign",
                    "live_campaign": {"assumed_loss": True},
                },
            ]
        }

        rows = sports.sports_bot_open_positions(portfolio)

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["strategy_owner"] for row in rows},
            {"live_campaign", "sports_game_odds"},
        )

    def test_zero_live_stake_cap_preserves_the_strategy_requested_stake(self):
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "effective_live_max_stake", return_value=0):
            self.assertEqual(sports.candidate_execution_stake(123.45), 123.45)

    def test_on_demand_market_request_includes_actionable_total(self):
        with (
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ENABLED", True),
            patch.object(sports, "SPORTS_ODDS_SPREADS_ON_DEMAND_ENABLED", True),
        ):
            markets = sports.markets_for_sport(
                "baseball_mlb",
                {"baseball_mlb": ["total"]},
            )
        self.assertEqual(markets, "h2h,totals")

    def test_live_campaign_loss_allowance_is_global_across_lanes(self):
        now = datetime.now(sports.LOCAL_TZ).isoformat(timespec="seconds")
        portfolio = {
            "balance": 60,
            "history": [],
            "bets": [
                {
                    "status": "open",
                    "mode": "live",
                    "strategy_owner": "live_campaign",
                    "bot_number": 1,
                    "stake": 40,
                    "fee": 0,
                    "placed_at": now,
                    "live_campaign": {"bot_number": 1, "role": "primary"},
                }
            ],
        }
        with (
            patch.object(sports, "configured_live_campaign_bot_count", return_value=2),
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=75),
        ):
            lane_two = sports.sports_live_campaign_lane_context(portfolio, 2)
            aggregate = sports.sports_live_campaign_context(portfolio)
        self.assertEqual(lane_two["global_daily_open_risk"], 40)
        self.assertEqual(lane_two["global_daily_loss_remaining"], 35)
        self.assertEqual(lane_two["daily_loss_remaining"], 35)
        self.assertEqual(aggregate["daily_loss_cap"], 75)
        self.assertEqual(aggregate["daily_loss_remaining"], 35)

    def test_global_campaign_cap_uses_lower_of_fixed_and_bankroll_percentage(self):
        portfolio = {"balance": 431.99, "history": [], "bets": []}
        with (
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP", 75),
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP_PCT", 0.10),
            patch.object(sports, "effective_live_daily_loss_cap", return_value=300),
        ):
            cap = sports.effective_live_campaign_daily_loss_cap(portfolio)
        self.assertEqual(cap, 43.20)

    def test_provider_exhaustion_does_not_expire_with_local_timer(self):
        with TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "key-state.json")
            tomorrow = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
            Path(state_file).write_text(
                json.dumps(
                    {
                        "version": 3,
                        "active_index": 0,
                        "accounts": {
                            "0": {
                                "provider_status": "exhausted",
                                "exhausted": True,
                                "provider_remaining": 0,
                                "next_probe_at": tomorrow,
                            }
                        },
                        "blocked": {
                            "0": {
                                "pause_until": (
                                    datetime.now() - timedelta(hours=1)
                                ).isoformat(timespec="seconds")
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(sports, "ODDS_KEY_STATE_FILE", state_file),
                patch.object(sports, "ODDS_API_KEYS", ("test-key",)),
                patch.object(sports, "ODDS_API_KEY", "test-key"),
            ):
                self.assertEqual(sports.active_odds_api_key(), "")

    def test_provider_headers_are_accounted_per_key(self):
        with TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "key-state.json")
            with (
                patch.object(sports, "ODDS_KEY_STATE_FILE", state_file),
                patch.object(sports, "ODDS_API_KEYS", ("key-a", "key-b")),
            ):
                sports.record_odds_provider_response(
                    1,
                    {
                        "x-requests-remaining": "497",
                        "x-requests-used": "3",
                        "x-requests-last": "2",
                    },
                    url="https://api.the-odds-api.com/v4/sports/test/odds",
                )
                summary = sports.odds_provider_account_summary()
        account = summary["accounts"][1]
        self.assertEqual(account["provider_remaining"], 497)
        self.assertEqual(account["provider_used"], 3)
        self.assertEqual(account["provider_last_cost"], 2)
        self.assertFalse(account["exhausted"])
        self.assertIn("projected_consumption_to_reset", account["projection"])

    def test_same_game_second_same_team_position_requires_stronger_evidence(self):
        existing = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "game_key": "mlb|today|a|b",
            "market_type": "moneyline",
            "selected_team": "Team A",
            "entry_price": 35,
            "contracts": 4,
            "stake": 1.4,
            "kalshi_ticker": "A-ML",
        }
        portfolio = {"balance": 98.6, "bets": [existing], "history": []}
        candidate = {
            "game_key": "mlb|today|a|b",
            "market_type": "spread",
            "selected_team": "Team A",
            "confidence_score": 90,
            "pro_review": {"score": 100},
            "final_bet_score": 97,
            "independent_book_family_count": 3,
            "sharp_book_count": 1,
            "pricing_v2": {"net_conservative_edge_pp": 3.5},
        }
        review = {"planned_contracts": 4, "applied_stake": 1.4, "estimated_fee": 0.05, "worst_case_cost": 1.45}
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_SAME_DIRECTION_MAX_POSITIONS", 1):
            capped = sports.sports_live_campaign_same_game_review(portfolio, candidate, review)
        self.assertFalse(capped["ok"])
        self.assertEqual(capped["reason"], "campaign_same_direction_position_cap")

        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_SAME_DIRECTION_MAX_POSITIONS", 2):
            allowed = sports.sports_live_campaign_same_game_review(portfolio, candidate, review)
            self.assertTrue(allowed["ok"])
            self.assertEqual(allowed["reason"], "same_direction_second_position_allowed")

        candidate["pricing_v2"]["net_conservative_edge_pp"] = 2.99
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_SAME_DIRECTION_MAX_POSITIONS", 2):
            blocked = sports.sports_live_campaign_same_game_review(portfolio, candidate, review)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "campaign_second_position_quality")
        self.assertIn("conservative_edge", blocked["failures"])

    def test_same_game_opposite_position_requires_nonnegative_after_fee_hedge(self):
        existing = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "game_key": "mlb|today|a|b",
            "market_type": "moneyline",
            "selected_team": "Team A",
            "entry_price": 35,
            "contracts": 4,
            "stake": 1.4,
            "kalshi_ticker": "A-ML",
        }
        portfolio = {"balance": 98.6, "bets": [existing], "history": []}
        candidate = {
            "game_key": "mlb|today|a|b",
            "market_type": "moneyline",
            "selected_team": "Team B",
        }
        losing_hedge = {"planned_contracts": 5, "applied_stake": 2.6, "estimated_fee": 0.07, "worst_case_cost": 2.67}
        blocked = sports.sports_live_campaign_same_game_review(portfolio, candidate, losing_hedge)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "campaign_hedge_worst_case_below_minimum")
        self.assertLess(blocked["worst_case_profit_after_fees"], 0)

        cheap_existing = {**existing, "entry_price": 20, "contracts": 5, "stake": 1.0}
        portfolio["bets"] = [cheap_existing]
        valid_hedge = {
            "planned_contracts": 5,
            "applied_stake": 1.0,
            "estimated_fee": sports.contract_fee(5, 20, sports.SPORTS_SHADOW_FEE_RATE),
            "worst_case_cost": 1.1,
        }
        allowed = sports.sports_live_campaign_same_game_review(portfolio, candidate, valid_hedge)
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["reason"], "verified_after_fee_hedge")
        self.assertGreaterEqual(allowed["worst_case_profit_after_fees"], 0)

    def test_moneyline_and_total_are_distinct_same_game_positions(self):
        existing = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "bot_number": 1,
            "game_key": "baseball_mlb|bos|lad",
            "market_type": "moneyline",
            "selected_team": "Los Angeles Dodgers",
            "entry_price": 51,
            "contracts": 88,
            "stake": 44.88,
            "fee": 1.54,
            "kalshi_ticker": "KXMLBGAME-LAD",
        }
        portfolio = {"balance": 4300.0, "bets": [existing], "history": []}
        candidate = {
            "game_key": "baseball_mlb|bos|lad",
            "market_type": "total",
            "selected_team": "Over 12.5",
            "kalshi_ticker": "KXMLBTOTAL-13",
        }
        review = {
            "planned_contracts": 63,
            "applied_stake": 29.61,
            "estimated_fee": 1.1,
            "worst_case_cost": 30.71,
        }
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_CAP", 90):
            allowed = sports.sports_live_campaign_same_game_review(
                portfolio,
                candidate,
                review,
            )
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["reason"], "distinct_market_type_position_allowed")
        self.assertEqual(allowed["relationship"], "distinct_market_type")
        self.assertEqual(allowed["combined_worst_case_cost"], 77.13)

    def test_campaign_duplicate_check_matches_contract_not_entire_game(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "game_key": "baseball_mlb|bos|lad",
                "event_ticker": "EVENT",
                "kalshi_ticker": "KXMLBGAME-LAD",
                "market_type": "moneyline",
                "order_side": "yes",
            }]
        }
        total = {
            "game_key": "baseball_mlb|bos|lad",
            "event_ticker": "EVENT",
            "kalshi_ticker": "KXMLBTOTAL-13",
            "market_type": "total",
            "market_line": 12.5,
            "order_side": "yes",
        }
        duplicate = {**total, "kalshi_ticker": "KXMLBGAME-LAD", "market_type": "moneyline"}
        self.assertFalse(live_campaign._same_open_market(portfolio, total, 1))
        self.assertTrue(live_campaign._same_open_market(portfolio, duplicate, 1))

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_two_and_half_unit_total_can_join_three_unit_moneyline_under_six_unit_cap(self):
        today = datetime.now(sports.LOCAL_TZ).date().isoformat()
        portfolio = {
            "balance": 4300.0,
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "placed_at": f"{today}T18:30:00-05:00",
                "game_key": "baseball_mlb|bos|lad",
                "event_ticker": "KXMLBGAME-EVENT",
                "kalshi_ticker": "KXMLBGAME-LAD",
                "market_type": "moneyline",
                "selected_team": "Los Angeles Dodgers",
                "order_side": "yes",
                "entry_price": 51,
                "contracts": 88,
                "stake": 44.88,
                "fee": 1.54,
                "sports_units": {"placed_units": 3},
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "bot_number": 1,
                    "campaign_date": today,
                },
            }],
            "history": [],
        }
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "game_state_features": {"authoritative_progress": True, "state_quality": "authoritative_progress"},
            "game_completed": False,
            "market_type": "total",
            "total_side": "over",
            "market_line": 12.5,
            "book_line": 12.5,
            "entry_price": 47,
            "edge": 7.218,
            "confidence_score": 98,
            "pro_review": {"score": 107},
            "final_bet_score": 88.0,
            "independent_book_family_count": 5,
            "sharp_book_count": 2,
            "pricing_v2": {"net_conservative_edge_pp": 5.474},
            "game_key": "baseball_mlb|bos|lad",
            "event_ticker": "KXMLBTOTAL-EVENT",
            "kalshi_ticker": "KXMLBTOTAL-13",
            "order_side": "yes",
        }
        with (
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_CAP", 300),
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=1000),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "effective_live_max_stake", return_value=1000),
            patch.object(sports, "trusted_capper_active_sports", return_value=False),
        ):
            review = sports.sports_live_campaign_review(portfolio, candidate, bot_number=1)
        self.assertTrue(review["eligible"])
        self.assertEqual(review["sports_units"]["additional_units"], 2.5)
        self.assertEqual(review["same_game_guard"]["reason"], "distinct_market_type_position_allowed")

    def test_terminal_settlement_keeps_outcome_separate_from_clv(self):
        bet = {
            "entry_price": 35,
            "order_side": "yes",
            "current_yes_ask": 1,
            "current_yes_bid": 1,
            "current_no_ask": 99,
            "current_no_bid": 99,
            "current_side_ask": 1,
            "current_side_bid": 1,
            "current_clv_vs_side_ask": -34,
            "current_mark_to_side_bid": -34,
            "current_price_side": "yes",
            "current_price_snapshot_at": "2026-07-21T23:14:00",
        }
        market = {
            "status": "settled",
            "result": "no",
            "yes_ask": 100,
            "yes_bid": 0,
            "no_ask": 100,
            "no_bid": 0,
        }
        self.assertTrue(sports.apply_market_snapshot_to_bet(bet, market, "final"))
        self.assertEqual(bet["final_side_ask"], 1)
        self.assertEqual(bet["final_clv_vs_side_ask"], -34)
        self.assertEqual(bet["final_price_snapshot_at"], "2026-07-21T23:14:00")
        self.assertEqual(bet["settlement_yes_value"], 0)
        self.assertEqual(bet["settlement_side_value"], 0)
        self.assertTrue(bet["terminal_quote_excluded_from_clv"])
        self.assertEqual(bet["final_snapshot_source"], "last_tradable_current_quote")

    def test_terminal_clv_backfill_repairs_existing_settled_rows(self):
        portfolio = {"history": [{
            "status": "settled",
            "result": "LOSS",
            "kalshi_result": "no",
            "order_side": "yes",
            "entry_price": 35,
            "current_side_ask": 1,
            "current_clv_vs_side_ask": -34,
            "current_price_snapshot_at": "2026-07-21T23:14:31",
            "final_side_ask": 100,
            "final_clv_vs_side_ask": 65,
            "final_price_snapshot_at": "2026-07-21T23:17:51",
        }]}
        self.assertEqual(sports.backfill_terminal_clv_snapshots(portfolio), 1)
        row = portfolio["history"][0]
        self.assertEqual(row["final_side_ask"], 1)
        self.assertEqual(row["final_clv_vs_side_ask"], -34)
        self.assertEqual(row["settlement_side_value"], 0)
        self.assertEqual(sports.backfill_terminal_clv_snapshots(portfolio), 0)

    def test_consensus_clv_matches_exact_side_and_line(self):
        bet = {
            "status": "open",
            "kalshi_ticker": "TOTAL-9",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {"book_probability": 55.0},
            "fixed_horizon_clv": {"5m": {"on_time": True, "captured_at": datetime.now().astimezone().isoformat()}},
        }
        candidate = {
            "kalshi_ticker": "TOTAL-9",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {
                "ok": True,
                "book_probability": 58.0,
                "consensus": {"independent_family_count": 5, "observations": [{"last_update": datetime.now().astimezone().isoformat()}]},
            },
        }
        self.assertTrue(sports.update_open_bet_consensus_clv({"bets": [bet]}, [candidate]))
        mark = bet["fixed_horizon_clv"]["5m"]
        self.assertTrue(mark["book_identity_valid"])
        self.assertEqual(mark["book_probability_side"], "no")
        self.assertEqual(mark["book_move_pp"], 3.0)
        self.assertEqual(mark["book_identity"]["side_transform"], "identity")

    def test_consensus_clv_complements_opposite_side_of_same_binary_total(self):
        bet = {
            "status": "open",
            "kalshi_ticker": "TOTAL-9",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {"book_probability": 55.0},
            "fixed_horizon_clv": {"5m": {"on_time": True, "captured_at": datetime.now().astimezone().isoformat()}},
        }
        candidate = {
            "kalshi_ticker": "TOTAL-9",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "yes",
            "total_side": "over",
            "pricing_v2": {
                "ok": True,
                "book_probability": 42.0,
                "consensus": {"independent_family_count": 4, "observations": [{"last_update": datetime.now().astimezone().isoformat()}]},
            },
        }
        sports.update_open_bet_consensus_clv({"bets": [bet]}, [candidate])
        mark = bet["fixed_horizon_clv"]["5m"]
        self.assertTrue(mark["book_identity_valid"])
        self.assertEqual(mark["book_probability"], 58.0)
        self.assertEqual(mark["book_move_pp"], 3.0)
        self.assertEqual(mark["book_identity"]["side_transform"], "binary_complement")

    def test_consensus_clv_rejects_line_mismatch_and_never_backfills_later(self):
        bet = {
            "status": "open",
            "kalshi_ticker": "TOTAL-9",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {"book_probability": 55.0},
            "fixed_horizon_clv": {"5m": {"on_time": True}},
        }
        wrong_line = {
            "kalshi_ticker": "TOTAL-9",
            "market_type": "total",
            "market_line": 9.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {"ok": True, "book_probability": 58.0},
        }
        sports.update_open_bet_consensus_clv({"bets": [bet]}, [wrong_line])
        mark = bet["fixed_horizon_clv"]["5m"]
        self.assertFalse(mark["book_identity_valid"])
        self.assertEqual(mark["book_identity_reason"], "market_line_mismatch")
        self.assertNotIn("book_move_pp", mark)

        correct_line = {**wrong_line, "market_line": 8.5}
        self.assertTrue(sports.update_open_bet_consensus_clv({"bets": [bet]}, [correct_line]))
        self.assertNotIn("book_move_pp", mark)

    def test_each_sports_bot_uses_and_locks_its_own_daily_cycle_schedule(self):
        portfolio = {"bets": [], "history": []}
        date_key = datetime.now(sports.LOCAL_TZ).date().isoformat()
        configured = {
            "SPORTS_LIVE_CAMPAIGN_BOT_1_CYCLE_PROFIT": "3",
            "SPORTS_LIVE_CAMPAIGN_BOT_1_CYCLE_COUNT": "3",
            "SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_PROFIT": "7",
            "SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_COUNT": "7",
        }

        with patch.object(
            sports,
            "settings_file_value",
            side_effect=lambda name, default="": configured.get(name, default),
        ):
            bot_one = sports.sports_live_campaign_strategy(portfolio, 1, date_key)
            bot_two = sports.sports_live_campaign_strategy(portfolio, 2, date_key)

        self.assertEqual(bot_one["goals"], (3.0,) * 3)
        self.assertEqual(bot_two["goals"], (7.0,) * 7)
        self.assertEqual(bot_one["configured_cycle_count"], 3)
        self.assertEqual(bot_two["configured_cycle_count"], 7)
        self.assertFalse(bot_one["goal_schedule_locked"])
        self.assertFalse(bot_two["goal_schedule_locked"])

        portfolio["bets"].append({
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "bot_number": 2,
            "placed_at": datetime.now(sports.LOCAL_TZ).isoformat(),
            "live_campaign": {"campaign_date": date_key, "bot_number": 2},
        })
        configured["SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_PROFIT"] = "9"
        configured["SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_COUNT"] = "9"
        with patch.object(
            sports,
            "settings_file_value",
            side_effect=lambda name, default="": configured.get(name, default),
        ):
            locked_bot_two = sports.sports_live_campaign_strategy(portfolio, 2, date_key)

        self.assertEqual(locked_bot_two["goals"], (7.0,) * 7)
        self.assertEqual(locked_bot_two["configured_cycle_profit"], 9.0)
        self.assertEqual(locked_bot_two["configured_cycle_count"], 9)
        self.assertTrue(locked_bot_two["goal_schedule_locked"])

        configured["SPORTS_LIVE_CAMPAIGN_BOT_2_CAMPAIGN_ID"] = "reset-bot-2"
        with patch.object(
            sports,
            "settings_file_value",
            side_effect=lambda name, default="": configured.get(name, default),
        ):
            reset_bot_two = sports.sports_live_campaign_strategy(portfolio, 2, date_key)

        self.assertEqual(reset_bot_two["campaign_id"], "reset-bot-2")
        self.assertEqual(reset_bot_two["goals"], (9.0,) * 9)
        self.assertFalse(reset_bot_two["goal_schedule_locked"])

    def test_sports_cycle_count_control_clamps_invalid_values(self):
        with patch.object(
            sports,
            "settings_file_value",
            side_effect=lambda name, default="": {
                "SPORTS_LIVE_CAMPAIGN_BOT_1_CYCLE_COUNT": "0",
                "SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_COUNT": "250",
            }.get(name, default),
        ):
            self.assertEqual(sports.configured_live_campaign_cycle_count(1), 1)
            self.assertEqual(sports.configured_live_campaign_cycle_count(2), 100)

    def test_shared_scan_enforces_one_global_autonomous_open_limit_across_campaign_bots(self):
        portfolio = {
            "balance": 100.0,
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "game_key": "first::game",
                "stake": 2.0,
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "bot_number": 1,
                    "campaign_date": datetime.now(sports.LOCAL_TZ).date().isoformat(),
                },
            }],
            "history": [],
        }
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "market_type": "moneyline",
            "entry_price": 35,
            "edge": 13,
            "confidence_score": 95,
            "pro_review": {"score": 120},
            "final_bet_score": 100,
            "game_key": "second::game",
            "event_ticker": "EVENT-2",
            "kalshi_ticker": "EVENT-2-MARKET",
            "order_side": "yes",
            "independent_book_family_count": 3,
        }
        with (
            patch.object(sports, "configured_live_campaign_bot_count", return_value=2),
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_MAX_OPEN", 1),
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=1000),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "effective_live_max_stake", return_value=1000),
            patch.object(sports, "trusted_capper_active_sports", return_value=False),
        ):
            aggregate = sports.sports_live_campaign_context(portfolio)
            review = sports.sports_live_campaign_review(portfolio, candidate)
        self.assertEqual(len(aggregate["bots"]), 2)
        self.assertEqual(aggregate["available_bot_numbers"], [])
        self.assertEqual(aggregate["bot_live_active_open_count"], 1)
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_open_slots_full")

    def test_lowering_configured_bot_count_disables_new_lane_two_orders(self):
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "market_type": "moneyline",
            "entry_price": 35,
            "edge": 13,
            "confidence_score": 95,
            "pro_review": {"score": 120},
            "final_bet_score": 100,
        }
        with patch.object(sports, "configured_live_campaign_bot_count", return_value=1):
            review = sports.sports_live_campaign_review(
                {"balance": 100.0, "bets": [], "history": []},
                candidate,
                bot_number=2,
            )
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_bot_disabled")

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_campaign_allows_same_market_unit_add_on_up_to_five_total_units(self):
        today = datetime.now(sports.LOCAL_TZ).date().isoformat()
        portfolio = {
            "balance": 1000.0,
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "placed_at": f"{today}T10:00:00-05:00",
                "game_key": "away::home",
                "event_ticker": "EVENT",
                "kalshi_ticker": "ADD-ON",
                "order_side": "yes",
                "entry_price": 50,
                "stake": 45,
                "sports_units": {"placed_units": 3},
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "bot_number": 1,
                    "campaign_date": today,
                },
            }],
            "history": [],
        }
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "game_state_features": {"authoritative_progress": True, "state_quality": "authoritative_progress"},
            "market_type": "moneyline",
            "entry_price": 50,
            "edge": 8.1,
            "confidence_score": 92,
            "pro_review": {"score": 105},
            "final_bet_score": 100,
            "independent_book_family_count": 4,
            "game_key": "away::home",
            "event_ticker": "EVENT",
            "kalshi_ticker": "ADD-ON",
            "order_side": "yes",
        }
        with (
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=1000),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "effective_live_max_stake", return_value=1000),
            patch.object(sports, "trusted_capper_active_sports", return_value=False),
        ):
            review = sports.sports_live_campaign_review(portfolio, candidate, bot_number=1)
        self.assertTrue(review["eligible"])
        self.assertEqual(review["sports_units"]["additional_units"], 2)
        self.assertEqual(review["recovery_mode"], "unit_flat")
        self.assertEqual(review["same_game_guard"]["reason"], "unit_same_market_add_on")

    def test_plus_four_hundred_quote_marks_newest_campaign_bet_assumed_loss(self):
        older = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "placed_at": "2026-07-21T12:00:00-05:00",
            "stake": 2.0,
            "fee": 0.04,
            "kalshi_ticker": "OLDER",
            "kalshi_order_side": "yes",
            "selected_team": "Older Team",
            "live_campaign": {"active": True, "role": "primary"},
        }
        newest = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "placed_at": "2026-07-21T12:30:00-05:00",
            "stake": 5.0,
            "fee": 0.1,
            "kalshi_ticker": "NEWEST",
            "kalshi_order_side": "yes",
            "selected_team": "Newest Team",
            "live_campaign": {"active": True, "role": "primary"},
        }
        portfolio = {"balance": 100, "bets": [older, newest], "history": []}
        markets = {
            "NEWEST": {
                "ticker": "NEWEST",
                "yes_bid": 19,
                "yes_ask": 20,
                "no_bid": 80,
                "no_ask": 81,
            }
        }
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_ENABLED", True), patch.object(
            sports, "sports_stream", return_value=None
        ), patch.object(
            sports, "log_line"
        ), patch.object(sports, "append_jsonl"):
            result = sports.mark_live_campaign_assumed_loss(
                portfolio,
                markets,
                allow_rest=False,
            )
        self.assertTrue(result["marked"])
        self.assertEqual(result["american_odds"], 400)
        self.assertEqual(result["assumed_loss_amount"], 5.1)
        self.assertTrue(newest["live_campaign"]["assumed_loss"])
        self.assertFalse(older["live_campaign"].get("assumed_loss", False))
        self.assertEqual(newest["status"], "open")

    def test_assumed_loss_signal_uses_selected_no_side_and_waits_above_threshold(self):
        bet = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "live_campaign",
            "placed_at": "2026-07-21T12:30:00-05:00",
            "stake": 3.0,
            "fee": 0.05,
            "kalshi_ticker": "NO-SIDE",
            "kalshi_order_side": "no",
            "selected_team": "No Team",
            "live_campaign": {"active": True, "role": "primary"},
        }
        portfolio = {"balance": 100, "bets": [bet], "history": []}
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_ENABLED", True), patch.object(
            sports, "sports_stream", return_value=None
        ), patch.object(
            sports, "log_line"
        ), patch.object(sports, "append_jsonl"):
            waiting = sports.mark_live_campaign_assumed_loss(
                portfolio,
                {"NO-SIDE": {"yes_bid": 79, "yes_ask": 80, "no_bid": 20, "no_ask": 21}},
                allow_rest=False,
            )
            self.assertFalse(waiting["marked"])
            marked = sports.mark_live_campaign_assumed_loss(
                portfolio,
                {"NO-SIDE": {"yes_bid": 80, "yes_ask": 81, "no_bid": 19, "no_ask": 20}},
                allow_rest=False,
            )
        self.assertTrue(marked["marked"])
        self.assertEqual(marked["side"], "no")
        self.assertEqual(marked["american_odds"], 400)

    def test_assumed_loss_monitor_checks_newest_position_in_each_bot_lane(self):
        bets = [
            {
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "bot_number": number,
                "placed_at": f"2026-07-21T12:0{number}:00-05:00",
                "stake": 2.0,
                "fee": 0.05,
                "kalshi_ticker": f"BOT-{number}",
                "kalshi_order_side": "yes",
                "selected_team": f"Bot {number} Team",
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "bot_number": number,
                },
            }
            for number in (1, 2)
        ]
        markets = {
            f"BOT-{number}": {"yes_bid": 19, "yes_ask": 20, "no_bid": 80, "no_ask": 81}
            for number in (1, 2)
        }
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_ENABLED", True), patch.object(
            sports, "configured_live_campaign_bot_count", return_value=2
        ), patch.object(
            sports, "sports_stream", return_value=None
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            result = sports.mark_live_campaign_assumed_losses(
                {"balance": 100.0, "bets": bets, "history": []},
                markets,
                allow_rest=False,
            )
        self.assertTrue(result["marked"])
        self.assertEqual(result["marked_count"], 2)
        self.assertEqual(
            [row["bot_number"] for row in result["marked_results"]],
            [1, 2],
        )
        self.assertTrue(all(row["live_campaign"]["assumed_loss"] for row in bets))

    def test_campaign_orders_force_fill_or_kill(self):
        self.assertEqual(
            sports.sports_live_order_time_in_force(
                {"live_campaign": {"active": True}}
            ),
            "fill_or_kill",
        )
        with patch.object(sports, "SPORTS_LIVE_TIME_IN_FORCE", "immediate_or_cancel"):
            self.assertEqual(
                sports.sports_live_order_time_in_force(
                    {"live_campaign": {"active": False}}
                ),
                "immediate_or_cancel",
            )

    def test_configured_net_edge_floor_is_consistent_across_active_gates(self):
        self.assertEqual(sports.SPORTS_EDGE_THRESHOLD, 0)
        self.assertEqual(sports.SPORTS_PRO_MIN_EDGE, 0)
        self.assertEqual(sports.SPORTS_LIVE_GAME_MIN_EDGE, 0)
        self.assertEqual(sports.SPORTS_LIVE_CAMPAIGN_MIN_EDGE, 0)

    def test_guarded_nominal_edge_requires_fresh_independent_sharp_consensus(self):
        pricing = {
            "net_edge_pp": 2.1,
            "net_conservative_edge_pp": 1.5,
            "spread_cents": 1.0,
            "uncertainty_pp": 1.8,
            "consensus": {
                "independent_family_count": 4,
                "sharp_book_count": 2,
                "average_age_minutes": 0.5,
            },
        }
        self.assertEqual(sports.pricing_v2_execution_edge(pricing), 2.1)
        self.assertEqual(pricing["edge_basis"], "guarded_nominal_net")
        self.assertTrue(pricing["guarded_nominal_review"]["eligible"])

    def test_guarded_nominal_edge_falls_back_when_consensus_is_stale(self):
        pricing = {
            "net_edge_pp": 2.1,
            "net_conservative_edge_pp": 0.3,
            "spread_cents": 1.0,
            "uncertainty_pp": 1.8,
            "consensus": {
                "independent_family_count": 4,
                "sharp_book_count": 2,
                "average_age_minutes": 3.0,
            },
        }
        self.assertEqual(sports.pricing_v2_execution_edge(pricing), 0.3)
        self.assertEqual(pricing["edge_basis"], "conservative_net")
        self.assertIn(
            "sportsbook_consensus_stale",
            pricing["guarded_nominal_review"]["failures"],
        )

    def test_fill_or_kill_fee_guard_uses_complete_order_fee(self):
        self.assertEqual(
            sports.order_fee_guard_per_contract(10, 50, fill_or_kill=True),
            0.018,
        )
        self.assertEqual(
            sports.order_fee_guard_per_contract(10, 50, fill_or_kill=False),
            0.02,
        )

    def test_candidate_edge_log_details_explains_negative_net_edge(self):
        candidate = {
            "pricing_v2": {
                "ok": True,
                "book_probability": 59.247,
                "fair_probability": 58.56,
                "lower_probability": 56.965,
                "ask_cents": 57,
                "raw_edge_pp": 1.56,
                "uncertainty_pp": 1.595,
                "estimated_fee_edge_pp": 1.716,
                "consensus": {
                    "raw_book_count": 2,
                    "independent_family_count": 2,
                    "stale_book_count": 6,
                    "missing_timestamp_book_count": 0,
                },
            }
        }
        details = sports.candidate_edge_log_details(candidate)
        self.assertIn("edge_basis=conservative_net", details)
        self.assertIn("fair=58.560%", details)
        self.assertIn("lower=56.965%", details)
        self.assertIn("raw=1.560%", details)
        self.assertIn("uncertainty=1.595pp", details)
        self.assertIn("fee=1.716pp", details)
        self.assertIn("raw_books=8", details)
        self.assertIn("families=2", details)
        self.assertIn("stale=6", details)

    def test_secondary_signals_cannot_remove_configured_quality_floors(self):
        candidate = {
            "edge": 2.9,
            "confidence_score": 69.0,
            "game_started": True,
            "skip_reasons": [],
        }
        with patch.object(sports, "SPORTS_EDGE_THRESHOLD", 3.0), patch.object(
            sports, "SPORTS_MIN_CONFIDENCE", 70.0
        ), patch.object(sports, "SPORTS_LIVE_GAME_MIN_EDGE", 3.0), patch.object(
            sports, "SPORTS_LIVE_GAME_MIN_CONFIDENCE", 70.0
        ), patch.object(sports, "SPORTS_LIVE_GAME_BETTING_ENABLED", True):
            reasons = sports.enforce_candidate_quality_floors(candidate)

        self.assertEqual(
            reasons,
            [
                "below_edge",
                "low_confidence",
                "live_edge_too_low",
                "live_confidence_too_low",
            ],
        )

    @patch.object(sports, "SPORTS_FAVORITE_WATCH_ENABLED", True)
    def test_favorite_rebound_cannot_bypass_edge_floor(self):
        candidate = {
            "event_id": "event-1",
            "sport_key": "basketball_wnba",
            "selected_team": "Phoenix Mercury",
            "market_type": "spread",
            "game_started": True,
            "minutes_since_start": 20,
            "has_live_score_context": True,
            "live_odds_age_minutes": 0.2,
            "entry_price": 49,
            "confidence_score": 98,
            "edge": -1.7,
        }
        watchlist = {
            sports.favorite_watch_key(candidate): {
                "first_book_price": -175,
                "latest_book_price": -195,
                "best_model_prob": 63,
            }
        }
        with patch.object(sports, "SPORTS_EDGE_THRESHOLD", 3.0), patch.object(
            sports, "SPORTS_LIVE_GAME_MIN_EDGE", 3.0
        ):
            review = sports.favorite_rebound_review(
                candidate,
                watchlist,
                {"score": 95},
            )

        self.assertFalse(review["ok"])
        self.assertEqual(review["reason"], "favorite_watch_below_edge")
        self.assertEqual(review["required_edge"], 3.0)

    def test_live_fee_guard_covers_one_contract_partial_fill_rounding(self):
        self.assertEqual(sports.rounded_taker_fee(1, 50, 0.07), 0.02)
        self.assertEqual(sports.rounded_taker_fee(10, 50, 0.07), 0.18)

    def test_dynamic_exposure_cap_uses_thirty_percent_without_dollar_ceiling(self):
        with patch.object(
            sports, "fetch_live_account_snapshot", return_value={"cash_balance": 1000.0}
        ), patch.object(sports, "KALSHI_API_KEY", "key"), patch.object(
            sports, "KALSHI_PRIVATE_KEY_PATH", "key.pem"
        ):
            self.assertEqual(sports.effective_live_cash_cap(0, 0.30, 0), 300.0)

    def test_premium_live_feed_counts_as_score_context(self):
        game = {
            "away_team": "Connecticut Sun",
            "home_team": "Phoenix Mercury",
            "premium_live_state": {
                "provider": "sportradar",
                "away_score": 18,
                "home_score": 21,
                "period": 2,
                "clock": "08:10",
            },
        }
        self.assertTrue(sports.has_live_score_context(game))
        summary = sports.score_context_text(game)
        self.assertIn("Connecticut Sun 18", summary)
        self.assertIn("Phoenix Mercury 21", summary)
        self.assertIn("source=sportradar", summary)

    def test_score_feed_metadata_without_a_score_is_not_live_confirmation(self):
        game = {
            "live_score_context": {
                "scores": [{"completed": False, "source": "api"}],
            },
        }
        self.assertFalse(sports.has_live_score_context(game))

    def test_real_score_is_not_hidden_by_metadata_only_context(self):
        game = {
            "live_score_context": {"scores": [{"completed": False, "source": "api"}]},
            "scores": [{"name": "Home", "score": "0"}],
        }
        self.assertTrue(sports.has_live_score_context(game))

    def test_live_odds_age_uses_book_quote_time_before_cache_age(self):
        game = {
            "_odds_cache_age_minutes": 0,
            "bookmakers": [
                {
                    "last_update": (
                        datetime.now(timezone.utc) - timedelta(minutes=8)
                    ).isoformat(),
                    "markets": [],
                }
            ],
        }
        self.assertGreaterEqual(sports.live_odds_age_minutes(game), 7.9)

    def test_live_settlement_never_double_credits_reconciled_cash(self):
        live = {"mode": "live"}
        paper = {"mode": "paper"}
        portfolio = {"balance": 100.0}
        sports.credit_settlement_balance(portfolio, live, 25)
        self.assertEqual(portfolio["balance"], 100.0)
        sports.credit_settlement_balance(portfolio, paper, 25)
        self.assertEqual(portfolio["balance"], 125.0)

    def test_live_settlement_waits_until_remote_position_disappears(self):
        bet = {
            "status": "open",
            "mode": "live",
            "stake": 5.0,
            "contracts": 10,
            "kalshi_ticker": "TEST-MARKET",
            "order_side": "yes",
        }
        portfolio = {"balance": 100.0, "bets": [bet], "history": []}
        market = {"ticker": "TEST-MARKET", "status": "finalized", "result": "yes"}
        with patch.object(
            sports,
            "recently_reconciled_remote_open_tickers",
            return_value={"TEST-MARKET"},
        ), patch.object(sports, "log_line"):
            changed, settled = sports.settle_open_sports_bets(
                portfolio,
                {"TEST-MARKET": market},
            )
        self.assertTrue(changed)
        self.assertEqual(settled, [])
        self.assertEqual(portfolio["bets"], [bet])
        self.assertEqual(portfolio["history"], [])
        self.assertEqual(portfolio["balance"], 100.0)
        self.assertIn("settlement_pending_remote_release_at", bet)

    def test_live_audit_suppresses_expected_settlement_release_lag(self):
        ticker = "TEST-SETTLEMENT-GRACE"
        bet = {
            "status": "open",
            "mode": "live",
            "kalshi_ticker": ticker,
            "settlement_pending_remote_release_at": datetime.now().astimezone().isoformat(),
        }
        portfolio = {"balance": 100.0, "bets": [bet], "history": []}
        reconciliation = {
            "account": {"ok": True, "cash_balance": 100.0},
            "local_live_open": {ticker: bet},
            "remote_sports_tickers": [],
            "position_mismatches": [],
        }
        with (
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_AUDIT_ENABLED", True),
            patch.object(sports, "SPORTS_LIVE_AUDIT_SETTLEMENT_GRACE_MINUTES", 5),
            patch.object(sports, "reconcile_live_account", return_value=reconciliation),
            patch.object(sports, "write_json"),
            patch.object(sports, "append_jsonl"),
            patch.object(sports, "log_line"),
        ):
            audit = sports.live_audit(portfolio, force=True)
        self.assertTrue(audit["ok"])
        self.assertEqual([], audit["warnings"])
        self.assertEqual([ticker], audit["settlement_grace_local_tickers"])
        self.assertEqual([], audit["actionable_unmatched_local_tickers"])

    def test_reconciliation_detects_contract_and_side_mismatch(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "kalshi_ticker": "TEST-MARKET",
                "order_side": "yes",
                "contracts": 10,
                "stake": 5,
            }]
        }
        mismatch = sports.live_position_mismatches(
            portfolio,
            [{"ticker": "TEST-MARKET", "position_fp": "-8.00"}],
        )
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["local_side"], "yes")
        self.assertEqual(mismatch[0]["remote_side"], "no")
        self.assertEqual(mismatch[0]["local_contracts"], 10.0)
        self.assertEqual(mismatch[0]["remote_contracts"], 8.0)

    def test_live_account_snapshot_fetches_every_position_page(self):
        calls = []

        def request(path, **_kwargs):
            calls.append(path)
            if path == "/portfolio/balance":
                return {"balance": 10000}, {}
            if "cursor=next-page" in path:
                return {
                    "market_positions": [{"ticker": "SECOND", "position_fp": "1.00"}],
                    "cursor": "",
                }, {}
            return {
                "market_positions": [{"ticker": "FIRST", "position_fp": "1.00"}],
                "cursor": "next-page",
            }, {}

        with patch.object(sports, "KALSHI_API_KEY", "key"), patch.object(
            sports, "KALSHI_API_SECRET", "secret"
        ), patch.object(sports, "kalshi_private_request", side_effect=request):
            snapshot = sports.fetch_live_account_snapshot()

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["position_count"], 2)
        self.assertEqual(snapshot["position_pages"], 2)
        self.assertEqual(
            [row["ticker"] for row in snapshot["positions_sample"]],
            ["FIRST", "SECOND"],
        )
        self.assertTrue(any("limit=1000" in path for path in calls))
        self.assertTrue(any("cursor=next-page" in path for path in calls))

    def test_ambiguous_order_intent_recovers_as_owned_live_bet(self):
        with TemporaryDirectory() as tmp:
            intents_path = Path(tmp) / "intents.json"
            sports.write_json(
                intents_path,
                {
                    "intents": [{
                        "client_order_id": "sports-intent-1",
                        "status": "ambiguous",
                        "created_at": "2026-07-19T17:00:00-05:00",
                        "ticker": "TEST-MARKET",
                        "order_side": "no",
                        "request": {"client_order_id": "sports-intent-1"},
                        "candidate": {
                            "kalshi_ticker": "TEST-MARKET",
                            "order_side": "no",
                            "entry_price": 40,
                            "live_campaign": {"active": True, "role": "primary", "cycle_number": 1},
                        },
                        "requested_stake": 0.8,
                        "reservation_id": "reservation-1",
                    }],
                },
            )
            recovered_order = {
                "client_order_id": "sports-intent-1",
                "fill_count_fp": "2.00",
                "taker_fill_cost_dollars": "0.8000",
                "taker_fees_dollars": "0.0400",
                "no_price_dollars": "0.4000",
            }
            portfolio = {"balance": 100, "bets": [], "history": []}
            with patch.object(
                sports, "SPORTS_LIVE_ORDER_INTENTS_FILE", str(intents_path)
            ), patch.object(
                sports,
                "lookup_order_by_client_id",
                return_value={"found": True, "order": recovered_order, "source": "/portfolio/orders"},
            ), patch.object(sports, "save_portfolio"), patch.object(
                sports, "finalize_reservation"
            ) as finalize, patch.object(sports, "log_line"), patch.object(
                sports, "append_jsonl"
            ):
                recovered = sports.recover_pending_live_order_intents(portfolio)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["strategy_owner"], "live_campaign")
        self.assertEqual(recovered[0]["stake"], 0.8)
        self.assertEqual(recovered[0]["entry_price"], 40.0)
        self.assertEqual(recovered[0]["fee"], 0.04)
        finalize.assert_called_with("reservation-1", "filled", 0.8)

    def test_submit_timeout_recovers_fill_by_client_order_id(self):
        candidate = {
            "kalshi_ticker": "TEST-MARKET",
            "order_side": "yes",
            "entry_price": 50,
            "edge": 5,
            "model_prob": 55,
            "model_prob_lower": 55,
            "market_type": "moneyline",
        }
        pricing = {
            "ok": True,
            "required_edge": 3,
            "candidate_updates": {
                "entry_price": 50,
                "model_prob": 55,
                "model_prob_lower": 55,
                "edge": 5,
            },
        }
        recovered_order = {
            "client_order_id": "server-copy",
            "fill_count_fp": "10.00",
            "taker_fill_cost_dollars": "5.0000",
            "taker_fees_dollars": "0.1800",
            "yes_price_dollars": "0.5000",
        }
        with TemporaryDirectory() as tmp, patch.object(
            sports, "SPORTS_LIVE_ORDER_INTENTS_FILE", str(Path(tmp) / "intents.json")
        ), patch.object(sports, "live_order_start_guard", return_value={"ok": True}), patch.object(
            sports, "live_order_pricing_guard", return_value=pricing
        ), patch.object(sports, "effective_live_max_stake", return_value=10), patch.object(
            sports,
            "reserve_live_order",
            return_value={"ok": True, "approved_stake": 5, "reservation_id": "reservation-1"},
        ) as reserve, patch.object(sports, "kalshi_private_request", side_effect=TimeoutError("response lost")), patch.object(
            sports,
            "lookup_order_by_client_id",
            return_value={"found": True, "order": recovered_order, "source": "/portfolio/orders"},
        ), patch.object(sports, "finalize_reservation") as finalize:
            result = sports.place_live_kalshi_order(candidate, 5, portfolio={})

        self.assertTrue(result["ok"])
        self.assertEqual(result["actual_stake"], 5.0)
        self.assertEqual(result["contracts"], 10.0)
        self.assertTrue(result["response"]["recovered_from_order_history"])
        reserve.assert_called_once()
        finalize.assert_called_once_with(
            "reservation-1", "filled_pending_persist", 5.0
        )

    def test_early_result_shortcut_never_closes_live_position_locally(self):
        live_bet = {
            "status": "open",
            "mode": "live",
            "stake": 50,
            "strategy_owner": "live_campaign",
            "kalshi_ticker": "TEST-LIVE",
        }
        portfolio = {"balance": 100, "bets": [live_bet], "history": []}

        with patch.object(sports, "SPORTS_EARLY_RESULT_CHECK_ENABLED", True), patch.object(
            sports, "early_dead_loss_review", return_value={"reason": "would_close_paper"}
        ) as review:
            changed, closed = sports.early_close_dead_open_bets(portfolio)

        self.assertFalse(changed)
        self.assertEqual(closed, [])
        self.assertEqual(portfolio["bets"], [live_bet])
        self.assertEqual(portfolio["history"], [])
        review.assert_not_called()

    def test_campaign_revalidation_rejects_final_quote_above_price_cap(self):
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "market_type": "moneyline",
            "entry_price": 68,
            "edge": 4,
            "confidence_score": 90,
            "pro_review": {"score": 100},
            "final_bet_score": 90,
            "independent_book_family_count": 2,
            "game_key": "away::home",
            "live_campaign": {"active": True},
        }
        result = sports.revalidate_live_campaign_order({"bets": [], "history": []}, candidate)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "campaign_revalidation_campaign_price_range")

    def test_campaign_revalidation_accepts_practical_minus_200_endpoint(self):
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "market_type": "moneyline",
            "entry_price": 67,
            "edge": 4,
            "confidence_score": 90,
            "pro_review": {"score": 100},
            "final_bet_score": 90,
            "independent_book_family_count": 2,
            "game_key": "away::home",
            "live_campaign": {"active": True},
        }
        with (
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=1000),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "effective_live_max_stake", return_value=1000),
            patch.object(sports, "trusted_capper_active_sports", return_value=False),
        ):
            result = sports.revalidate_live_campaign_order({"bets": [], "history": []}, candidate)
        self.assertTrue(result["ok"])

    @patch.object(sports, "SPORTS_PROBABILITY_SIZING_REQUIRE_WALK_FORWARD", False)
    def test_campaign_revalidation_downgrades_tier_and_resizes(self):
        candidate = {
            "game_started": True,
            "has_live_score_context": True,
            "market_type": "moneyline",
            "entry_price": 50,
            "edge": 4,
            "confidence_score": 90,
            "pro_review": {"score": 100},
            "final_bet_score": 90,
            "independent_book_family_count": 2,
            "game_state_features": {"authoritative_progress": True, "state_quality": "authoritative_progress"},
            "game_key": "away::home",
            "live_campaign": {"active": True, "quality_tier": "strong"},
        }
        with (
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=1000),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "effective_live_max_stake", return_value=1000),
            patch.object(sports, "trusted_capper_active_sports", return_value=False),
        ):
            result = sports.revalidate_live_campaign_order({"bets": [], "history": []}, candidate)
        self.assertTrue(result["ok"])
        self.assertEqual(result["campaign"]["quality_tier"], "qualified")
        self.assertEqual(result["stake"], result["campaign"]["applied_stake"])
        self.assertTrue(result["campaign"]["planned_target_met"])
        self.assertEqual(result["campaign"]["sports_units"]["additional_units"], 3)

    def test_average_fill_price_is_used_as_cost_basis(self):
        self.assertEqual(
            sports.response_average_fill_price_cents({"average_fill_price": "0.4200"}, 45),
            42.0,
        )
        self.assertEqual(
            sports.response_average_fill_price_cents(
                {"average_fill_price": "0.5800"},
                45,
                order_side="no",
            ),
            42.0,
        )
        self.assertEqual(sports.response_average_fill_price_cents({}, 45), 45.0)
        partial = sports.live_fill_financials(
            {"fill_count": "3.00", "average_fill_price": "0.4200"},
            45,
        )
        self.assertEqual(partial["fill_count"], 3.0)
        self.assertEqual(partial["average_fill_price"], 42.0)
        self.assertEqual(partial["actual_stake"], 1.26)
        no_fill = sports.live_fill_financials(
            {"fill_count": "3.00", "average_fill_price": "0.5800"},
            45,
            order_side="no",
        )
        self.assertEqual(no_fill["average_fill_price"], 42.0)
        self.assertEqual(no_fill["actual_stake"], 1.26)

    def test_fixed_point_order_history_shape_is_valid_fill_and_fee_data(self):
        history_fill = {
            "fill_count_fp": "3.00",
            "taker_fill_cost_dollars": "1.2600",
            "taker_fees_dollars": "0.0600",
            "yes_price_dollars": "0.4200",
        }
        fill = sports.live_fill_financials(history_fill, 45, order_side="yes")
        self.assertEqual(fill["fill_count"], 3.0)
        self.assertEqual(fill["average_fill_price"], 42.0)
        self.assertEqual(fill["actual_stake"], 1.26)
        self.assertEqual(sports.order_response_fee(history_fill), 0.06)

        no_history_fill = {
            "fill_count_fp": "3.00",
            "taker_fill_cost_dollars": "1.2600",
            "no_price_dollars": "0.4200",
        }
        no_fill = sports.live_fill_financials(no_history_fill, 45, order_side="no")
        self.assertEqual(no_fill["average_fill_price"], 42.0)
        self.assertEqual(no_fill["actual_stake"], 1.26)

    def test_fixed_point_count_works_with_average_fee_response(self):
        self.assertEqual(
            sports.order_response_fee(
                {"fill_count_fp": "3.00", "average_fee_paid": "0.0200"}
            ),
            0.06,
        )

    def test_odds_credit_cost_counts_markets_and_regions(self):
        self.assertEqual(
            sports.estimated_credit_cost("h2h,spreads", "us,us2"),
            4,
        )

    def test_campaign_daily_loss_is_clamped_to_account_allowance(self):
        with (
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP", 300),
            patch.object(sports, "SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP_PCT", 0),
            patch.object(sports, "effective_live_daily_loss_cap", return_value=35),
        ):
            self.assertEqual(sports.effective_live_campaign_daily_loss_cap(), 35)

    def test_campaign_preflight_enforces_normal_open_exposure(self):
        campaign = {
            "complete": False,
            "daily_loss_cap_hit": False,
            "bot_live_open_count": 0,
            "daily_loss_remaining": 20,
        }
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(
            sports, "SPORTS_LIVE_FALLBACK_TO_PAPER", False
        ), patch.object(sports, "sports_live_campaign_context", return_value=campaign), patch.object(
            sports, "live_exposure", return_value=24
        ), patch.object(sports, "effective_live_max_stake", return_value=10), patch.object(
            sports, "effective_live_open_exposure_cap", return_value=25
        ), patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""
        ), patch.object(sports, "load_live_order_intents", return_value={"intents": []}):
            reason = sports.live_execution_preflight_skip_reason(
                {},
                {
                    "game_started": True,
                    "has_live_score_context": True,
                    "game_completed": False,
                    "live_campaign": {"active": True},
                },
                2,
            )
        self.assertEqual(reason, "live_open_exposure_cap")

    def test_campaign_preflight_treats_zero_open_exposure_cap_as_disabled(self):
        campaign = {
            "complete": False,
            "daily_loss_cap_hit": False,
            "bot_live_open_count": 0,
            "daily_loss_remaining": 500,
        }
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(
            sports, "SPORTS_LIVE_FALLBACK_TO_PAPER", False
        ), patch.object(sports, "sports_live_campaign_context", return_value=campaign), patch.object(
            sports, "live_exposure", return_value=5000
        ), patch.object(sports, "effective_live_max_stake", return_value=75), patch.object(
            sports, "effective_live_open_exposure_cap", return_value=0
        ), patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""), patch.object(
            sports, "load_live_order_intents", return_value={"intents": []}
        ), patch.object(
            sports,
            "live_order_review",
            return_value={"ok": True, "approved_stake": 75},
        ):
            reason = sports.live_execution_preflight_skip_reason(
                {},
                {
                    "game_started": True,
                    "has_live_score_context": True,
                    "game_completed": False,
                    "live_campaign": {"active": True},
                },
                75,
            )
        self.assertEqual(reason, "")

    def test_campaign_source_filter_keeps_only_active_score_confirmed_games(self):
        games = [
            {"id": "scheduled", "commence_time": "2999-01-01T00:00:00Z"},
            {
                "id": "live-no-score",
                "commence_time": "2020-01-01T00:00:00Z",
            },
            {
                "id": "completed",
                "commence_time": "2020-01-01T00:00:00Z",
                "scores": [{"name": "A", "score": "1"}],
                "completed": True,
            },
            {
                "id": "live",
                "commence_time": "2020-01-01T00:00:00Z",
                "scores": [{"name": "A", "score": "1"}],
                "completed": False,
            },
        ]
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ENABLED", True), patch.object(
            sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", False
        ):
            filtered, stats = sports.campaign_live_game_filter(games)
        self.assertEqual([game["id"] for game in filtered], ["live"])
        self.assertEqual(stats["scheduled_excluded"], 1)
        self.assertEqual(stats["missing_score_excluded"], 1)
        self.assertEqual(stats["completed_excluded"], 1)

    def test_campaign_source_filter_adds_only_near_start_pregame_games(self):
        now = datetime.now(timezone.utc)
        games = [
            {"id": "near", "commence_time": (now + timedelta(minutes=30)).isoformat()},
            {"id": "too-early", "commence_time": (now + timedelta(minutes=46)).isoformat()},
        ]
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ENABLED", True), patch.object(
            sports, "SPORTS_PREGAME_ENABLED", True
        ), patch.object(sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", False), patch.object(
            sports, "SPORTS_PREGAME_MAX_MINUTES_BEFORE_START", 45
        ):
            filtered, stats = sports.campaign_live_game_filter(games)
        self.assertEqual([game["id"] for game in filtered], ["near"])
        self.assertTrue(filtered[0]["pregame_eligible"])
        self.assertEqual(stats["pregame_games"], 1)
        self.assertEqual(stats["scheduled_excluded"], 1)

    def test_all_open_pregame_mode_accepts_distant_supported_events(self):
        game = {
            "id": "future",
            "commence_time": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
        }
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ENABLED", True), patch.object(
            sports, "SPORTS_PREGAME_ENABLED", True
        ), patch.object(sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", True):
            filtered, stats = sports.campaign_live_game_filter([game])
        self.assertEqual([row["id"] for row in filtered], ["future"])
        self.assertTrue(filtered[0]["pregame_eligible"])
        self.assertEqual(stats["pregame_games"], 1)

    def test_native_pregame_entry_window_runs_from_three_hours_to_ten_minutes(self):
        now = datetime.now(timezone.utc)
        with patch.object(sports, "SPORTS_PREGAME_ENABLED", True), patch.object(
            sports, "SPORTS_NATIVE_PREGAME_MIN_MINUTES_BEFORE_START", 10
        ), patch.object(
            sports, "SPORTS_NATIVE_PREGAME_MAX_MINUTES_BEFORE_START", 180
        ):
            three_hours = sports.native_pregame_window_review({
                "commence_time": (now + timedelta(minutes=180)).isoformat(),
            }, now=now)
            ten_minutes = sports.native_pregame_window_review({
                "commence_time": (now + timedelta(minutes=10)).isoformat(),
            }, now=now)
            too_early = sports.native_pregame_window_review({
                "commence_time": (now + timedelta(minutes=181)).isoformat(),
            }, now=now)
            final_window = sports.native_pregame_window_review({
                "commence_time": (now + timedelta(minutes=9)).isoformat(),
            }, now=now)
        self.assertTrue(three_hours["eligible"])
        self.assertTrue(ten_minutes["eligible"])
        self.assertEqual("native_pregame_too_early", too_early["reason"])
        self.assertEqual("native_pregame_final_window_closed", final_window["reason"])

    def test_native_pregame_requires_fresh_exact_line_independent_pricing(self):
        candidate = {
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
            "game_started": False,
            "market_type": "spread",
            "market_line": -2.5,
            "contract_market_line": -2.5,
            "book_line": -2.5,
            "book_line_model": "exact_line",
            "skip_reasons": [],
            "pricing_v2": {
                "ok": True,
                "consensus": {
                    "independent_family_count": 3,
                    "sharp_book_count": 1,
                    "line_ladder_interpolated_family_count": 0,
                    "observations": [
                        {"family": "a", "age_minutes": 1.0, "line_model": "exact_line"},
                        {"family": "b", "age_minutes": 1.5, "line_model": "exact_line"},
                        {"family": "c", "age_minutes": 2.0, "line_model": "exact_line"},
                    ],
                },
            },
        }
        with patch.object(sports, "SPORTS_PREGAME_ENABLED", True), patch.object(
            sports, "SPORTS_NATIVE_PREGAME_MIN_BOOK_FAMILIES", 3
        ), patch.object(sports, "SPORTS_NATIVE_PREGAME_REQUIRE_EXACT_LINE", True):
            approved = sports.native_pregame_pricing_review(candidate)
            interpolated = sports.native_pregame_pricing_review({
                **candidate,
                "book_line_model": "monotone_interpolation",
                "pricing_v2": {
                    **candidate["pricing_v2"],
                    "consensus": {
                        **candidate["pricing_v2"]["consensus"],
                        "line_ladder_interpolated_family_count": 1,
                    },
                },
            })
            stale = sports.native_pregame_pricing_review({
                **candidate,
                "pricing_v2": {
                    **candidate["pricing_v2"],
                    "consensus": {
                        **candidate["pricing_v2"]["consensus"],
                        "observations": [
                            {"family": "a", "age_minutes": 7.0},
                            {"family": "b", "age_minutes": 1.0},
                            {"family": "c", "age_minutes": 1.0},
                        ],
                    },
                },
            })
        self.assertTrue(approved["eligible"])
        self.assertIn("native_pregame_line_not_exact", interpolated["failures"])
        self.assertIn("native_pregame_books_stale", stale["failures"])

    def test_missing_live_score_sports_only_requests_uncovered_live_sports(self):
        games = [
            {
                "sport_key": "baseball_mlb",
                "commence_time": "2020-01-01T00:00:00Z",
                "premium_live_state": {"home_score": 2, "away_score": 1},
            },
            {
                "sport_key": "basketball_wnba",
                "commence_time": "2020-01-01T00:00:00Z",
            },
            {
                "sport_key": "basketball_nba",
                "commence_time": "2999-01-01T00:00:00Z",
            },
            {
                "sport_key": "icehockey_nhl",
                "commence_time": "2020-01-01T00:00:00Z",
                "completed": True,
            },
        ]
        self.assertEqual(
            sports.missing_live_score_sports(games),
            ["basketball_wnba"],
        )

    def test_missing_live_score_fallback_ignores_unmatched_provider_events(self):
        games = [
            {
                "id": "actionable",
                "sport_key": "basketball_wnba",
                "commence_time": "2020-01-01T00:00:00Z",
                "premium_live_state": {"home_score": 10, "away_score": 8},
            },
            {
                "id": "unmatched",
                "sport_key": "basketball_wnba",
                "commence_time": "2020-01-01T00:00:00Z",
            },
        ]
        self.assertEqual(
            sports.missing_live_score_sports(
                games,
                {"basketball_wnba": ["actionable"]},
            ),
            [],
        )

    def test_actionable_event_filter_preserves_only_kalshi_matched_games(self):
        games = [
            {"id": "keep", "sport_key": "baseball_mlb"},
            {"id": "drop", "sport_key": "baseball_mlb"},
            {"id": "fallback", "sport_key": "basketball_wnba"},
        ]
        with patch.object(sports, "SPORTS_ODDS_ACTIONABLE_EVENT_FILTER_ENABLED", True):
            filtered, status = sports.filter_games_to_actionable_events(
                games,
                {"baseball_mlb": ["keep"]},
            )
        self.assertEqual([row["id"] for row in filtered], ["keep", "fallback"])
        self.assertEqual(status["filtered_games"], 1)

    def test_score_enrichment_fetches_only_requested_fallback_sports(self):
        games = [
            {
                "id": "mlb",
                "sport_key": "baseball_mlb",
                "commence_time": "2020-01-01T00:00:00Z",
            },
            {
                "id": "wnba",
                "sport_key": "basketball_wnba",
                "commence_time": "2020-01-01T00:00:00Z",
            },
        ]
        with patch.object(
            sports,
            "fetch_scores_for_sports",
            return_value={},
        ) as fetch_scores, patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            sports.enrich_games_with_score_context(
                games,
                ["basketball_wnba"],
                max_cache_minutes=3,
            )
        fetch_scores.assert_called_once_with(
            ["basketball_wnba"],
            max_cache_minutes=3,
        )

    def test_campaign_preflight_rejects_non_live_and_completed_candidates(self):
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(
            sports, "SPORTS_LIVE_FALLBACK_TO_PAPER", False
        ), patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""), patch.object(
            sports, "load_live_order_intents", return_value={"intents": []}
        ):
            scheduled = sports.live_execution_preflight_skip_reason(
                {},
                {"game_started": False, "live_campaign": {"active": True}},
                2,
            )
            missing_score = sports.live_execution_preflight_skip_reason(
                {},
                {
                    "game_started": True,
                    "has_live_score_context": False,
                    "live_campaign": {"active": True},
                },
                2,
            )
            completed = sports.live_execution_preflight_skip_reason(
                {},
                {
                    "game_started": True,
                    "has_live_score_context": True,
                    "game_completed": True,
                    "live_campaign": {"active": True},
                },
                2,
            )
        self.assertEqual(scheduled, "campaign_live_only")
        self.assertEqual(missing_score, "campaign_live_score_required")
        self.assertEqual(completed, "campaign_game_completed")

    def test_campaign_preflight_allows_a_qualified_pregame_candidate(self):
        candidate = {
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "game_started": False,
            "pregame_eligible": True,
            "live_campaign": {"active": True},
        }
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(
            sports, "SPORTS_LIVE_FALLBACK_TO_PAPER", False
        ), patch.object(sports, "SPORTS_PREGAME_ENABLED", True), patch.object(
            sports, "live_reconciliation_preflight_skip_reason", return_value=""
        ), patch.object(sports, "sports_live_campaign_context", return_value={
            "complete": False,
            "daily_loss_cap_hit": False,
            "bot_live_active_open_count": 0,
            "daily_loss_remaining": 75,
        }), patch.object(sports, "effective_live_max_stake", return_value=50), patch.object(
            sports, "live_exposure", return_value=0
        ), patch.object(sports, "effective_live_open_exposure_cap", return_value=300), patch.object(
            sports, "load_live_order_intents", return_value={"intents": []}
        ), patch.object(
            sports,
            "live_order_review",
            return_value={"ok": True, "approved_stake": 5},
        ):
            reason = sports.live_execution_preflight_skip_reason({}, candidate, 5)
        self.assertEqual(reason, "")

    def test_pregame_position_cap_counts_open_pending_and_resting_campaign_rows(self):
        portfolio = {
            "bets": [
                {
                    "status": status,
                    "mode": "live",
                    "strategy_owner": "live_campaign",
                    "bet_timing_bucket": "pregame",
                }
                for status in ("open", "pending", "resting")
            ]
        }
        self.assertEqual(sports.campaign_pregame_open_count(portfolio), 3)

    def test_live_order_guard_falls_back_to_rest_when_stream_is_stale(self):
        stream = type(
            "Stream",
            (),
            {"snapshot": lambda self, ticker, max_age_seconds: {"fresh": False}},
        )()
        candidate = {
            "kalshi_ticker": "TEST",
            "pricing_v2": {
                "ok": True,
                "consensus": {"ok": True},
            },
        }
        with patch.object(sports, "SPORTS_PRICING_V2_ENABLED", True), patch.object(
            sports, "SPORTS_PRICING_V2_ENFORCEMENT", "active"
        ), patch.object(sports, "SPORTS_KALSHI_STREAM_REQUIRE_FOR_LIVE", True), patch.object(
            sports, "sports_stream", return_value=stream
        ), patch.object(sports, "fetch_kalshi_market_by_ticker", return_value={}
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"
        ):
            result = sports.live_order_pricing_guard(candidate)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_executable_quote_at_order")
        self.assertEqual(result["source"], "kalshi_rest_order_guard")

    def test_live_order_guard_blocks_when_edge_evaporates(self):
        stream = type(
            "Stream",
            (),
            {
                "snapshot": lambda self, ticker, max_age_seconds: {
                    "fresh": True,
                    "yes_bid": 58,
                    "yes_ask": 60,
                }
            },
        )()
        candidate = {
            "kalshi_ticker": "TEST",
            "order_side": "yes",
            "game_started": True,
            "live_campaign": {"active": True},
            "pricing_v2": {
                "ok": True,
                "requested_book_weight": 0.25,
                "consensus": {
                    "ok": True,
                    "probability": 62,
                    "uncertainty_pp": 2,
                    "independent_family_count": 4,
                },
            },
        }
        with patch.object(sports, "SPORTS_PRICING_V2_ENABLED", True), patch.object(
            sports, "SPORTS_PRICING_V2_ENFORCEMENT", "active"
        ), patch.object(sports, "SPORTS_KALSHI_STREAM_REQUIRE_FOR_LIVE", False), patch.object(
            sports, "sports_stream", return_value=stream
        ):
            result = sports.live_order_pricing_guard(candidate)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "order_quote_edge_evaporated")
        self.assertEqual(result["candidate_updates"]["entry_price"], 60.0)
        self.assertEqual(
            result["candidate_updates"]["edge"],
            result["edge"],
        )
        self.assertEqual(
            result["candidate_updates"]["pricing_v2"]["order_guard_source"],
            "kalshi_websocket",
        )

    def test_live_order_guard_carries_executable_top_depth(self):
        stream = type(
            "Stream",
            (),
            {"snapshot": lambda self, ticker, max_age_seconds: {
                "fresh": True,
                "orderbook_valid": True,
                "yes_bid": 39,
                "yes_ask": 40,
                "yes_ask_size": 123,
            }},
        )()
        candidate = {
            "kalshi_ticker": "DEPTH",
            "order_side": "yes",
            "pricing_v2": {
                "ok": True,
                "requested_book_weight": 1.0,
                "consensus": {
                    "ok": True,
                    "probability": 60,
                    "uncertainty_pp": 2,
                    "independent_family_count": 3,
                },
            },
        }
        with patch.object(sports, "sports_stream", return_value=stream), patch.object(
            sports, "SPORTS_EDGE_THRESHOLD", 0
        ), patch.object(sports, "SPORTS_PRO_MIN_EDGE", 0):
            review = sports.live_order_pricing_guard(candidate)
        self.assertTrue(review["ok"])
        self.assertEqual(123.0, review["candidate_updates"]["executable_contracts_at_ask"])
        self.assertTrue(review["candidate_updates"]["orderbook_depth_valid"])

    def test_kalshi_first_gate_selects_only_usable_sports_and_spreads(self):
        markets = [
            {
                "series_ticker": "KXMLBGAME",
                "ticker": "KXMLBGAME-TEST-NYY",
                "title": "Will the Yankees win this baseball game?",
                "yes_bid": 40,
                "yes_ask": 45,
                "volume": 100,
            },
            {
                "series_ticker": "KXWNBASPREAD",
                "ticker": "KXWNBASPREAD-TEST-NYL3",
                "title": "Liberty wins by over 2.5 points in this basketball game",
                "yes_bid": 35,
                "yes_ask": 42,
                "volume": 80,
            },
            {
                "series_ticker": "KXNBAGAME",
                "ticker": "KXNBAGAME-TEST-BOS",
                "title": "Will Boston win this basketball game?",
                "yes_bid": 20,
                "yes_ask": 75,
                "volume": 100,
            },
        ]
        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value=set()):
            result = sports.build_kalshi_first_odds_gate(
                markets,
                ["baseball_mlb", "basketball_wnba", "basketball_nba"],
            )
        self.assertEqual(result["sport_keys"], ["baseball_mlb", "basketball_wnba"])
        self.assertEqual(result["market_types_by_sport"]["baseball_mlb"], ["moneyline"])
        self.assertEqual(result["market_types_by_sport"]["basketball_wnba"], ["spread"])
        self.assertNotIn("basketball_nba", result["market_types_by_sport"])

    def test_kalshi_first_gate_keeps_open_bet_sport_for_monitoring(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "sport_key": "baseball_mlb",
                "market_type": "spread",
            }]
        }
        result = sports.build_kalshi_first_odds_gate(
            [],
            ["baseball_mlb", "basketball_wnba"],
            portfolio=portfolio,
        )
        self.assertEqual(result["sport_keys"], ["baseball_mlb"])
        self.assertEqual(result["market_types_by_sport"]["baseball_mlb"], ["spread"])
        self.assertEqual(result["open_bet_forced_sports"], ["baseball_mlb"])

    def test_kalshi_first_gate_maps_actionable_market_to_free_schedule_event(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=30)
        ticker_start = start.astimezone(sports.LOCAL_TZ).strftime("%y%b%d%H%M").upper()
        market = {
            "series_ticker": "KXMLBGAME",
            "ticker": f"KXMLBGAME-{ticker_start}NYYBOS-NYY",
            "title": "Will the New York Yankees win this baseball game?",
            "yes_bid": 40,
            "yes_ask": 45,
            "volume": 100,
        }
        schedule_cache = {
            "sports": {
                "baseball_mlb": {
                    "games": [{
                        "id": "odds-event-1",
                        "sport_key": "baseball_mlb",
                        "home_team": "Boston Red Sox",
                        "away_team": "New York Yankees",
                        "commence_time": start.isoformat(),
                    }]
                }
            }
        }
        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value=set()), patch.object(
            sports, "market_matches_provider_sport", return_value=True
        ), patch.object(sports, "market_date_matches_game", return_value=True), patch.object(
            sports, "doubleheader_start_matches_game", return_value=True
        ):
            result = sports.build_kalshi_first_odds_gate(
                [market],
                ["baseball_mlb"],
                now=now,
                schedule_cache=schedule_cache,
            )
        self.assertEqual(
            result["actionable_event_ids_by_sport"]["baseball_mlb"],
            ["odds-event-1"],
        )

    @patch.object(sports, "SPORTS_PREGAME_ENABLED", True)
    @patch.object(sports, "SPORTS_MIN_KALSHI_VOLUME", 100)
    def test_nfl_gate_keeps_preseason_and_regular_event_ids_separate(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=30)
        market = {
            "series_ticker": "KXNFLGAME",
            "ticker": "KXNFLGAME-TEST-KC",
            "title": "Will Kansas City win this football game?",
            "yes_bid": 40,
            "yes_ask": 45,
            "volume": 100,
        }
        schedule_cache = {
            "sports": {
                "americanfootball_nfl_preseason": {
                    "games": [{
                        "id": "preseason-event",
                        "sport_key": "americanfootball_nfl_preseason",
                        "home_team": "Kansas City Chiefs",
                        "away_team": "Arizona Cardinals",
                        "commence_time": start.isoformat(),
                    }]
                },
                "americanfootball_nfl": {
                    "games": [{
                        "id": "regular-event",
                        "sport_key": "americanfootball_nfl",
                        "home_team": "Buffalo Bills",
                        "away_team": "Miami Dolphins",
                        "commence_time": start.isoformat(),
                    }]
                },
            }
        }

        def match_game(game, _market):
            return (2, {}) if game.get("id") == "preseason-event" else (0, {})

        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value=set()), patch.object(
            sports, "kalshi_market_start_datetime", return_value=start
        ), patch.object(sports, "market_matches_provider_sport", return_value=True), patch.object(
            sports, "market_date_matches_game", return_value=True
        ), patch.object(sports, "doubleheader_start_matches_game", return_value=True), patch.object(
            sports, "match_game_to_market", side_effect=match_game
        ):
            result = sports.build_kalshi_first_odds_gate(
                [market],
                ["americanfootball_nfl_preseason", "americanfootball_nfl"],
                now=now,
                schedule_cache=schedule_cache,
            )
        self.assertEqual(
            result["actionable_event_ids_by_sport"]["americanfootball_nfl_preseason"],
            ["preseason-event"],
        )
        self.assertNotIn("americanfootball_nfl", result["actionable_event_ids_by_sport"])

    def test_unit_analytics_group_by_sport_market_and_units_with_fixed_clv(self):
        portfolio = {
            "history": [
                {
                    "strategy_owner": "live_campaign",
                    "result": "WIN",
                    "sport_key": "baseball_mlb",
                    "market_type": "total",
                    "unit_count": 2,
                    "stake": 30,
                    "profit": 24,
                    "edge": 5.5,
                    "fixed_horizon_clv": {
                        "5m": {"on_time": True, "clv_vs_entry_ask_cents": 2.0}
                    },
                },
                {
                    "strategy_owner": "live_campaign",
                    "result": "LOSS",
                    "sport_key": "baseball_mlb",
                    "market_type": "total",
                    "unit_count": 2,
                    "stake": 30,
                    "profit": -30,
                    "edge": 4.5,
                    "fixed_horizon_clv": {
                        "5m": {"on_time": True, "clv_vs_entry_ask_cents": -1.0}
                    },
                },
            ],
            "bets": [],
        }
        analytics = sports.sports_unit_analytics(portfolio)
        row = analytics["by_sport_market_units"][0]
        self.assertEqual((row["sport_key"], row["market_type"], row["units"]), ("baseball_mlb", "total", 2))
        self.assertEqual((row["bets"], row["wins"], row["losses"]), (2, 1, 1))
        self.assertEqual(row["profit"], -6)
        self.assertEqual(row["roi"], -10)
        self.assertEqual(row["average_edge"], 5)
        self.assertEqual(row["average_clv_5m_cents"], 0.5)
        self.assertEqual(row["clv_5m_count"], 2)

    def test_unit_analytics_preserves_half_unit_cohort(self):
        analytics = sports.sports_unit_analytics({
            "history": [{
                "strategy_owner": "live_campaign",
                "result": "WIN",
                "sport_key": "tennis_atp",
                "market_type": "moneyline",
                "unit_count": 0.5,
                "unit_size": 20,
                "stake": 10,
                "profit": 8,
            }],
            "bets": [],
        })
        self.assertEqual(0.5, analytics["settled_units_risked"])
        self.assertEqual("0.5u", analytics["by_units"][0]["label"])
        self.assertEqual(0.5, analytics["by_sport_market_units"][0]["units"])

    def test_daily_summary_does_not_report_a_winner_as_biggest_loss(self):
        summary = sports.sports_daily_rows_summary(
            [{
                "result": "WIN",
                "stake": 20,
                "profit": 12,
                "game": "Away at Home",
                "selected_team": "Home",
            }],
            "settlement_date",
        )
        self.assertEqual(summary["biggest_win"]["profit"], 12)
        self.assertEqual(summary["biggest_loss"], {})
        self.assertEqual(summary["basis"], "settlement_date")

    def test_execution_analytics_tracks_ai_latency_and_post_ai_retention(self):
        analytics = sports.sports_execution_quality_analytics([{
            "game_state_features": {"state_quality": "authoritative_progress", "authoritative_progress": True},
            "ai_risk": {"timings_ms": {"openai": 1200, "grok": 800}},
            "post_ai_revalidation": {"requested": True, "ok": True, "edge_retention_ratio": 0.75},
        }])
        self.assertEqual(analytics["authoritative_state_pct"], 100)
        self.assertEqual(analytics["average_ai_latency_ms"], 2000)
        self.assertEqual(analytics["post_ai_confirmation_pct"], 100)
        self.assertEqual(analytics["average_edge_retention_pct"], 75)

    def test_kalshi_first_gate_does_not_count_future_timestamped_market(self):
        now = datetime.now(timezone.utc)
        market = {
            "series_ticker": "KXMLBGAME",
            "ticker": "KXMLBGAME-FUTURE-NYY",
            "title": "Will the Yankees win this baseball game?",
            "yes_bid": 40,
            "yes_ask": 45,
            "volume": 100,
        }
        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value=set()), patch.object(
            sports, "kalshi_market_start_datetime", return_value=now + timedelta(hours=1)
        ), patch.object(sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", False), patch.object(
            sports, "SPORTS_PREGAME_MAX_MINUTES_BEFORE_START", 45
        ):
            result = sports.build_kalshi_first_odds_gate(
                [market],
                ["baseball_mlb"],
                now=now,
            )
        self.assertEqual(result["sport_keys"], [])
        self.assertEqual(result["diagnostics"]["not_live_yet"], 1)

    def test_kalshi_first_gate_requires_untimestamped_market_to_match_live_schedule(self):
        now = datetime.now(timezone.utc)
        market = {
            "series_ticker": "KXWNBAGAME",
            "ticker": "KXWNBAGAME-NOTIME-NYL",
            "title": "Will the Liberty win this basketball game?",
            "yes_bid": 40,
            "yes_ask": 45,
            "volume": 100,
        }
        schedule_cache = {
            "sports": {
                "basketball_wnba": {
                    "games": [{
                        "sport_key": "basketball_wnba",
                        "home_team": "New York Liberty",
                        "away_team": "Chicago Sky",
                        "commence_time": (now + timedelta(hours=2)).isoformat(),
                    }]
                }
            }
        }
        with patch.object(sports, "load_dynamic_actionable_kalshi_series", return_value=set()):
            result = sports.build_kalshi_first_odds_gate(
                [market],
                ["basketball_wnba"],
                now=now,
                schedule_cache=schedule_cache,
            )
        self.assertEqual(result["sport_keys"], [])
        self.assertEqual(result["diagnostics"]["untimestamped_not_matched_live"], 1)

    def test_generic_soccer_gate_pays_only_for_the_schedule_matched_league(self):
        now = datetime.now(timezone.utc)
        market = {
            "series_ticker": "KXSOCCERMATCH",
            "ticker": "KXSOCCERMATCH-26AUG20BODSTU-BOD",
            "title": "Will Bodo/Glimt beat Sturm Graz?",
            "yes_bid": 44,
            "yes_ask": 46,
            "volume": 1000,
            "liquidity": 1000,
        }
        schedule_cache = {
            "sports": {
                "soccer_uefa_champs_league": {"games": [{
                    "id": "bodo-sturm",
                    "sport_key": "soccer_uefa_champs_league",
                    "home_team": "Bodo/Glimt",
                    "away_team": "Sturm Graz",
                    "commence_time": (now + timedelta(minutes=30)).isoformat(),
                }]},
                "soccer_epl": {"games": [{
                    "id": "arsenal-chelsea",
                    "sport_key": "soccer_epl",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "commence_time": (now + timedelta(minutes=30)).isoformat(),
                }]},
            }
        }

        def match_score(game, _market):
            return (100 if game.get("id") == "bodo-sturm" else 0, {})

        with patch.object(sports, "is_sports_market", return_value=True), patch.object(
            sports, "is_supported_kalshi_series", return_value=True
        ), patch.object(sports, "is_simple_market", return_value=True), patch.object(
            sports, "kalshi_market_canonical_sport", return_value="soccer"
        ), patch.object(sports, "classify_kalshi_market", return_value={"type": "moneyline"}), patch.object(
            sports, "kalshi_market_start_datetime", return_value=now - timedelta(minutes=5)
        ), patch.object(sports, "market_matches_provider_sport", return_value=True), patch.object(
            sports, "market_date_matches_game", return_value=True
        ), patch.object(sports, "doubleheader_start_matches_game", return_value=True), patch.object(
            sports, "match_game_to_market", side_effect=match_score
        ):
            result = sports.build_kalshi_first_odds_gate(
                [market],
                ["soccer_uefa_champs_league", "soccer_epl"],
                now=now,
                schedule_cache=schedule_cache,
            )

        self.assertEqual(["soccer_uefa_champs_league"], result["sport_keys"])
        self.assertEqual(
            ["bodo-sturm"],
            result["actionable_event_ids_by_sport"]["soccer_uefa_champs_league"],
        )

    def test_on_demand_market_selection_requests_spreads_only_when_needed(self):
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ENABLED", True), patch.object(
            sports, "SPORTS_ODDS_SPREADS_ON_DEMAND_ENABLED", True
        ):
            self.assertEqual(
                sports.markets_for_sport(
                    "baseball_mlb",
                    requested_market_types={"baseball_mlb": ["moneyline"]},
                ),
                "h2h",
            )
            self.assertEqual(
                sports.markets_for_sport(
                    "baseball_mlb",
                    requested_market_types={"baseball_mlb": ["spread"]},
                ),
                "h2h,spreads",
            )

    def test_selective_overnight_window_uses_chicago_hours(self):
        with patch.object(sports, "SPORTS_OVERNIGHT_ODDS_ROUTING_ENABLED", True), patch.object(
            sports, "SPORTS_OVERNIGHT_ODDS_START_HOUR", 0
        ), patch.object(sports, "SPORTS_OVERNIGHT_ODDS_END_HOUR", 9):
            self.assertTrue(sports.overnight_odds_routing_active(datetime(2026, 8, 20, 0, 0)))
            self.assertTrue(sports.overnight_odds_routing_active(datetime(2026, 8, 20, 8, 59)))
            self.assertFalse(sports.overnight_odds_routing_active(datetime(2026, 8, 20, 9, 0)))
            self.assertFalse(sports.overnight_odds_routing_active(datetime(2026, 8, 20, 23, 59)))

    def test_overseas_derivative_markets_pause_under_pressure_but_priority_survives(self):
        requested = {"baseball_kbo": ["moneyline", "total"]}
        with patch.object(sports, "SPORTS_LIVE_CAMPAIGN_ENABLED", True), patch.object(
            sports, "SPORTS_ODDS_SPREADS_ON_DEMAND_ENABLED", True
        ), patch.object(sports, "SPORTS_OVERSEAS_DERIVATIVE_SHADOW_BUDGET_GUARD_ENABLED", True):
            self.assertEqual(
                "h2h",
                sports.markets_for_sport(
                    "baseball_kbo",
                    requested,
                    shadow_paid_calls_allowed=False,
                    priority_lane=False,
                ),
            )
            self.assertEqual(
                "h2h,totals",
                sports.markets_for_sport(
                    "baseball_kbo",
                    requested,
                    shadow_paid_calls_allowed=False,
                    priority_lane=True,
                ),
            )

    def test_overnight_shadow_policy_pauses_only_nonpriority_cricket(self):
        with patch.object(
            sports, "SPORTS_OVERNIGHT_PAUSE_CRICKET_SHADOW_ON_BUDGET_PRESSURE", True
        ):
            self.assertFalse(sports.overnight_paid_shadow_sport_allowed(
                "cricket_ipl",
                overnight_active=True,
                shadow_paid_calls_allowed=False,
                priority_lane=False,
            ))
            self.assertTrue(sports.overnight_paid_shadow_sport_allowed(
                "cricket_ipl",
                overnight_active=True,
                shadow_paid_calls_allowed=False,
                priority_lane=True,
            ))
            self.assertTrue(sports.overnight_paid_shadow_sport_allowed(
                "cricket_ipl",
                overnight_active=False,
                shadow_paid_calls_allowed=False,
                priority_lane=False,
            ))
            self.assertTrue(sports.overnight_paid_shadow_sport_allowed(
                "baseball_npb",
                overnight_active=True,
                shadow_paid_calls_allowed=False,
                priority_lane=False,
            ))

    def test_on_demand_alternate_repair_leaves_moneyline_to_live_refresh(self):
        game = {
            "id": "live-moneyline-repair",
            "sport_key": "basketball_wnba",
            "commence_time": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "moneyline",
            "edge": 8.0,
            "game_completed": False,
            "pricing_v2": {"reason": "same_book_consensus_unavailable"},
        }
        with (
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED", True),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN", 8),
            patch.object(sports, "SPORTS_CAPPER_ENABLED", False),
            patch.object(sports, "odds_api_get_json") as get_mock,
        ):
            merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game],
                [(game, {}, candidate)],
            )

        self.assertEqual(0, status["requested_events"])
        self.assertEqual(1, status["unsupported_market_skips"])
        self.assertEqual([game], merged)
        get_mock.assert_not_called()

    def test_on_demand_alternate_repair_skips_low_quality_candidate(self):
        game = {
            "id": "low-quality-spread",
            "sport_key": "basketball_wnba",
            "commence_time": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        }
        candidate = {
            "market_type": "spread",
            "edge": 8.0,
            "entry_price": 45,
            "confidence_score": 65,
            "skip_reasons": ["pricing_v2_insufficient_independent_books", "low_confidence"],
            "pricing_v2": {"reason": "pricing_v2_insufficient_independent_books"},
        }
        with patch.object(sports, "SPORTS_CAPPER_ENABLED", False), patch.object(
            sports, "odds_api_get_json"
        ) as get_mock:
            _merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual(1, status["candidate_quality_skips"])
        get_mock.assert_not_called()

    def test_far_pregame_alternate_repair_reuses_hour_cache(self):
        game = {
            "id": "far-spread",
            "sport_key": "basketball_wnba",
            "commence_time": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "spread",
            "edge": 8.0,
            "entry_price": 45,
            "confidence_score": 80,
            "skip_reasons": ["pricing_v2_insufficient_independent_books"],
            "pricing_v2": {"reason": "pricing_v2_insufficient_independent_books"},
        }
        cached_game = {**game, "bookmakers": [{"key": "cached", "markets": []}]}
        cache_key = ("basketball_wnba", "far-spread", "alternate_spreads", "us")
        with (
            patch.object(sports, "SPORTS_CAPPER_ENABLED", False),
            patch.object(sports, "SPORTS_ODDS_PRIMARY_REGION", "us"),
            patch.object(sports, "SPORTS_ODDS_SECONDARY_REGIONS", ""),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_FAR_PREGAME_CACHE_MINUTES", 60),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_NEAR_START_MINUTES", 90),
            patch.object(sports, "SPORTS_ALTERNATE_ODDS_CACHE", {
                cache_key: {"fetched_epoch": sports.time.time() - 30 * 60, "game": cached_game}
            }),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}),
            patch.object(sports, "odds_api_get_json") as get_mock,
        ):
            merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual(1, status["cache_hits"])
        self.assertEqual(0, status["requested_events"])
        self.assertEqual("cached", merged[0]["bookmakers"][0]["key"])
        get_mock.assert_not_called()

    def test_overseas_alternate_shadow_call_pauses_under_budget_pressure(self):
        game = {
            "id": "npb-total-shadow",
            "sport_key": "baseball_npb",
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "total",
            "edge": 5.0,
            "entry_price": 45,
            "confidence_score": 80,
            "skip_reasons": ["pricing_v2_insufficient_independent_books"],
            "pricing_v2": {"reason": "pricing_v2_insufficient_independent_books"},
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_ENABLED", False),
            patch.object(sports, "SPORTS_OVERSEAS_DERIVATIVE_SHADOW_BUDGET_GUARD_ENABLED", True),
            patch.object(sports, "SPORTS_ALTERNATE_ODDS_CACHE", {}),
            patch.object(sports, "sports_odds_priority_sports", return_value=set()),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={
                "paid_refresh_multiplier": 6.0,
                "shadow_paid_calls_allowed": False,
                "odds_usage_pressure_mode": "critical",
            }),
            patch.object(sports, "append_jsonl"),
            patch.object(sports, "odds_api_get_json") as get_mock,
        ):
            _merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual(1, status["overseas_derivative_budget_skips"])
        self.assertEqual(0, status["requested_events"])
        get_mock.assert_not_called()

    def test_alternate_repair_uses_primary_region_only_when_coverage_is_sufficient(self):
        game = {
            "id": "spread-event",
            "sport_key": "basketball_wnba",
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "spread", "edge": 5.0, "entry_price": 45,
            "confidence_score": 80,
            "skip_reasons": ["pricing_v2_insufficient_independent_books"],
            "pricing_v2": {"reason": "pricing_v2_insufficient_independent_books"},
        }
        refreshed = {
            **game,
            "bookmakers": [{"key": key, "markets": []} for key in ("a", "b", "c")],
        }
        with patch.object(sports, "SPORTS_CAPPER_ENABLED", False), patch.object(
            sports, "SPORTS_ALTERNATE_ODDS_CACHE", {}
        ), patch.object(sports, "SPORTS_ODDS_PRIMARY_REGION", "us"), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_REGIONS", "us2,uk,eu"
        ), patch.object(sports, "SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS", 3), patch.object(
            sports, "can_spend_odds_credits", return_value=(True, {}, 0)
        ), patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}), patch.object(
            sports, "odds_api_get_json", return_value=(refreshed, {})
        ) as get_mock, patch.object(sports, "record_odds_spend"), patch.object(
            sports, "append_jsonl"
        ):
            _merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual(1, get_mock.call_count)
        self.assertEqual("us", get_mock.call_args.kwargs["params"]["regions"])
        self.assertEqual(0, status["secondary_region_requests"])

    def test_alternate_repair_expands_secondary_regions_only_after_weak_primary(self):
        game = {
            "id": "spread-event",
            "sport_key": "basketball_wnba",
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "spread", "edge": 5.0, "entry_price": 45,
            "confidence_score": 80,
            "skip_reasons": ["pricing_v2_insufficient_independent_books"],
            "pricing_v2": {"reason": "pricing_v2_insufficient_independent_books"},
        }
        primary = {**game, "bookmakers": [{"key": "a", "markets": []}]}
        supplemental = {
            **game,
            "bookmakers": [{"key": key, "markets": []} for key in ("b", "c")],
        }
        with patch.object(sports, "SPORTS_CAPPER_ENABLED", False), patch.object(
            sports, "SPORTS_ALTERNATE_ODDS_CACHE", {}
        ), patch.object(sports, "SPORTS_ODDS_PRIMARY_REGION", "us"), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_REGIONS", "us2,uk,eu"
        ), patch.object(sports, "SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS", 3), patch.object(
            sports, "can_spend_odds_credits", return_value=(True, {}, 0)
        ), patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}), patch.object(
            sports, "odds_api_get_json", side_effect=[(primary, {}), (supplemental, {})]
        ) as get_mock, patch.object(sports, "record_odds_spend"), patch.object(
            sports, "append_jsonl"
        ):
            merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual(2, get_mock.call_count)
        self.assertEqual("us", get_mock.call_args_list[0].kwargs["params"]["regions"])
        self.assertEqual("us2,uk,eu", get_mock.call_args_list[1].kwargs["params"]["regions"])
        self.assertEqual(1, status["secondary_region_requests"])
        self.assertEqual({"a", "b", "c"}, {row["key"] for row in merged[0]["bookmakers"]})

    def test_late_live_consensus_refresh_requests_primary_market(self):
        game = {
            "id": "late-live-wnba",
            "sport_key": "basketball_wnba",
            "commence_time": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "moneyline",
            "edge": 3.02,
            "confidence_score": 72.1,
            "kalshi_volume": 1_500_000,
            "game_started": True,
            "game_completed": False,
            "pricing_v2": {"reason": "same_book_consensus_unavailable"},
        }
        refreshed = {
            **game,
            "bookmakers": [{"key": "fresh-book", "markets": []}],
        }
        headers = {
            "x-requests-remaining": "998",
            "x-requests-last": "1",
            "_polymaker_odds_key_index": 0,
        }
        with (
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_ENABLED", True),
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MAX_EVENTS", 3),
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MIN_EDGE", 0),
            patch.object(sports, "SPORTS_ODDS_PRIMARY_REGION", "us"),
            patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)),
            patch.object(sports, "odds_api_get_json", return_value=(refreshed, headers)) as get_mock,
            patch.object(sports, "record_odds_spend") as spend_mock,
            patch.object(sports, "append_jsonl"),
        ):
            merged, status = sports.enrich_with_late_live_consensus_refresh(
                [game],
                [(game, {}, candidate)],
            )

        self.assertEqual(1, status["requested_events"])
        self.assertEqual(["late-live-wnba"], status["refreshed_event_ids"])
        self.assertEqual(["h2h"], status["markets"])
        self.assertEqual("h2h", get_mock.call_args.kwargs["params"]["markets"])
        self.assertEqual("us", get_mock.call_args.kwargs["params"]["regions"])
        self.assertEqual("fresh-book", merged[0]["bookmakers"][0]["key"])
        spend_mock.assert_called_once()

    def test_late_live_consensus_refresh_keeps_nonpositive_and_pregame_candidates(self):
        live_game = {"id": "live", "sport_key": "basketball_wnba", "bookmakers": []}
        pregame = {"id": "pregame", "sport_key": "basketball_wnba", "bookmakers": []}
        base = {
            "market_type": "moneyline",
            "confidence_score": 90,
            "game_completed": False,
            "pricing_v2": {"reason": "same_book_consensus_unavailable"},
        }
        pairs = [
            (live_game, {}, {**base, "edge": -0.01, "game_started": True}),
            (pregame, {}, {**base, "edge": 8.0, "game_started": False}),
        ]
        with (
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_ENABLED", True),
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MAX_EVENTS", 3),
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MIN_EDGE", 0),
            patch.object(sports, "odds_api_get_json") as get_mock,
        ):
            merged, status = sports.enrich_with_late_live_consensus_refresh(
                [live_game, pregame],
                pairs,
            )

        self.assertEqual(0, status["eligible_events"])
        self.assertEqual([live_game, pregame], merged)
        get_mock.assert_not_called()

    def test_late_live_refresh_does_not_bypass_overnight_shadow_pressure(self):
        game = {
            "id": "late-live-npb",
            "sport_key": "baseball_npb",
            "commence_time": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "bookmakers": [],
        }
        candidate = {
            "market_type": "moneyline",
            "edge": 5.0,
            "confidence_score": 85,
            "game_started": True,
            "game_completed": False,
            "pricing_v2": {"reason": "same_book_consensus_unavailable"},
        }
        with (
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_ENABLED", True),
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MAX_EVENTS", 3),
            patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MIN_EDGE", 0),
            patch.object(sports, "overnight_odds_routing_active", return_value=True),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={
                "paid_refresh_multiplier": 6.0,
                "shadow_paid_calls_allowed": False,
            }),
            patch.object(sports, "sports_odds_priority_sports", return_value=set()),
            patch.object(sports, "odds_api_get_json") as get_mock,
        ):
            merged, status = sports.enrich_with_late_live_consensus_refresh(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual([game], merged)
        self.assertEqual(1, status["overnight_shadow_skips"])
        self.assertEqual(0, status["requested_events"])
        get_mock.assert_not_called()

    def test_refreshed_event_replaces_only_its_candidate_pairs(self):
        games = [{"id": "keep"}, {"id": "refresh"}]
        old_pairs = [
            (games[0], {"ticker": "keep"}, {"marker": "old-keep"}),
            (games[1], {"ticker": "refresh"}, {"marker": "old-refresh"}),
        ]
        diagnostics = {"keep": sports.Counter({"keep": 1}), "refresh": sports.Counter({"old": 1})}
        with patch.object(
            sports,
            "evaluate_candidate",
            side_effect=lambda game, market, event_diagnostics, previous_lines, calibration_state=None: {
                "marker": f"new-{game['id']}-{market['ticker']}"
            },
        ) as evaluate_mock:
            pairs, updated_diagnostics = sports.replace_refreshed_candidate_pairs(
                old_pairs,
                diagnostics,
                games,
                [{"ticker": "m1"}, {"ticker": "m2"}],
                {},
                {},
                ["refresh"],
            )

        self.assertEqual(2, evaluate_mock.call_count)
        self.assertEqual(
            ["old-keep", "new-refresh-m1", "new-refresh-m2"],
            [candidate["marker"] for _game, _market, candidate in pairs],
        )
        self.assertEqual({"keep", "refresh"}, set(updated_diagnostics))

    def test_candidate_pair_index_avoids_cross_sport_and_date_evaluations(self):
        today = datetime.now(sports.LOCAL_TZ).date()
        game = {
            "id": "indexed-game",
            "sport_key": "baseball_mlb",
            "commence_time": datetime.now(timezone.utc).isoformat(),
        }
        markets = [
            {"ticker": "matching"},
            {"ticker": "wrong-sport"},
            {"ticker": "wrong-date"},
            {"ticker": "unknown"},
        ]
        identities = {
            "matching": "baseball_mlb",
            "wrong-sport": "basketball_wnba",
            "wrong-date": "baseball_mlb",
            "unknown": "",
        }
        dates = {
            "matching": today,
            "wrong-sport": today,
            "wrong-date": today + timedelta(days=5),
            "unknown": None,
        }
        with (
            patch.object(sports, "kalshi_market_canonical_sport", side_effect=lambda row: identities[row["ticker"]]),
            patch.object(sports, "kalshi_market_date", side_effect=lambda row: dates[row["ticker"]]),
            patch.object(sports, "evaluate_candidate", return_value=None) as evaluate_mock,
        ):
            _pairs, diagnostics = sports.evaluate_candidate_pairs_for_games(
                [game], markets, {}, {}
            )
        evaluated = {call.args[1]["ticker"] for call in evaluate_mock.call_args_list}
        self.assertEqual({"matching", "unknown"}, evaluated)
        self.assertEqual(1, diagnostics["indexed-game"]["cross_sport_series"])
        self.assertEqual(1, diagnostics["indexed-game"]["date_mismatch"])

    def test_odds_cache_refreshes_when_spread_coverage_is_missing(self):
        cache = {
            "version": 2,
            "sports": {
                "baseball_mlb": {
                    "generated_at": datetime.now(sports.LOCAL_TZ).isoformat(),
                    "markets": "h2h",
                    "games": [],
                }
            },
        }
        status = sports.cached_odds_entry(
            cache,
            "baseball_mlb",
            required_markets="h2h,spreads",
        )
        self.assertFalse(status["valid"])
        self.assertEqual(status["reason"], "market_coverage")
        self.assertEqual(status["missing_markets"], ["spreads"])

    @patch.object(sports, "SPORTS_PREGAME_ENABLED", True)
    @patch.object(sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", True)
    @patch.object(sports, "SPORTS_LIVE_GAME_BETTING_ENABLED", True)
    def test_unpromoted_overseas_research_reuses_hourly_cache_overnight(self):
        now = datetime.now(timezone.utc)
        cache = {
            "version": 2,
            "sports": {
                "baseball_npb": {
                    "generated_at": (datetime.now(sports.LOCAL_TZ) - timedelta(minutes=45)).isoformat(),
                    "markets": "h2h",
                    "games": [{
                        "id": "npb-research",
                        "sport_key": "baseball_npb",
                        "commence_time": (now + timedelta(minutes=30)).isoformat(),
                    }],
                },
            },
        }
        with (
            patch.object(sports, "SPORTS_ODDS_CACHE_MAX_HOURS", 1),
            patch.object(sports, "SPORTS_OVERNIGHT_OVERSEAS_RESEARCH_CACHE_MINUTES", 60),
            patch.object(sports, "SPORTS_PREGAME_FAST_POLL_MINUTES_BEFORE_START", 60),
            patch.object(sports, "SPORTS_PREGAME_FAST_ODDS_CACHE_MAX_MINUTES", 3),
            patch.object(sports, "trusted_capper_watches_exact_sport", return_value=False),
            patch.object(sports, "trusted_capper_broad_discovery_sport", return_value=False),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={
                "paid_refresh_multiplier": 1.0,
            }),
        ):
            research = sports.cached_odds_entry(
                cache,
                "baseball_npb",
                overnight_active=True,
                overseas_rollout_state={"segments": {}},
            )
            priority = sports.cached_odds_entry(
                cache,
                "baseball_npb",
                priority_lane=True,
                overnight_active=True,
                overseas_rollout_state={"segments": {}},
            )
        self.assertTrue(research["valid"])
        self.assertTrue(research["overnight_overseas_research_lane"])
        self.assertEqual(60, research["adaptive_pregame_cache_max_minutes"])
        self.assertFalse(priority["valid"])
        self.assertEqual("pregame_stale", priority["reason"])

    def test_pregame_cache_polls_slowly_then_accelerates_near_start(self):
        now = datetime.now(timezone.utc)
        generated_at = (datetime.now(sports.LOCAL_TZ) - timedelta(minutes=5)).isoformat()
        base = {"version": 2, "sports": {"baseball_mlb": {"generated_at": generated_at}}}
        with patch.object(sports, "SPORTS_PREGAME_ENABLED", True), patch.object(
            sports, "SPORTS_PREGAME_MAX_MINUTES_BEFORE_START", 45
        ), patch.object(sports, "SPORTS_PREGAME_FAST_POLL_MINUTES_BEFORE_START", 15), patch.object(
            sports, "SPORTS_PREGAME_ODDS_CACHE_MAX_MINUTES", 10
        ), patch.object(sports, "SPORTS_PREGAME_FAST_ODDS_CACHE_MAX_MINUTES", 3), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}
        ), patch.object(
            sports, "trusted_capper_watches_exact_sport", return_value=False
        ), patch.object(
            sports, "trusted_capper_broad_discovery_sport", return_value=False
        ):
            early = json.loads(json.dumps(base))
            early["sports"]["baseball_mlb"]["games"] = [
                {"commence_time": (now + timedelta(minutes=30)).isoformat()}
            ]
            near = json.loads(json.dumps(base))
            near["sports"]["baseball_mlb"]["games"] = [
                {"commence_time": (now + timedelta(minutes=10)).isoformat()}
            ]
            early_status = sports.cached_odds_entry(early, "baseball_mlb")
            near_status = sports.cached_odds_entry(near, "baseball_mlb")
        self.assertTrue(early_status["valid"])
        self.assertEqual(early_status["adaptive_pregame_cache_max_minutes"], 10)
        self.assertFalse(near_status["valid"])
        self.assertEqual(near_status["reason"], "pregame_stale")
        self.assertEqual(near_status["adaptive_pregame_cache_max_minutes"], 3)

    @patch.object(sports, "SPORTS_PREGAME_ENABLED", True)
    @patch.object(sports, "SPORTS_LIVE_GAME_BETTING_ENABLED", True)
    def test_actionable_cache_timing_ignores_unmatched_live_games(self):
        now = datetime.now(timezone.utc)
        cache = {
            "version": 2,
            "sports": {
                "baseball_mlb": {
                    "generated_at": (datetime.now(sports.LOCAL_TZ) - timedelta(minutes=5)).isoformat(),
                    "games": [
                        {
                            "id": "unmatched-live",
                            "commence_time": (now - timedelta(hours=1)).isoformat(),
                        },
                        {
                            "id": "actionable-pregame",
                            "commence_time": (now + timedelta(minutes=30)).isoformat(),
                        },
                    ],
                }
            },
        }
        with patch.object(sports, "SPORTS_LIVE_ODDS_CACHE_MAX_MINUTES", 1.5), patch.object(
            sports, "SPORTS_PREGAME_ODDS_CACHE_MAX_MINUTES", 10
        ), patch.object(sports, "SPORTS_PREGAME_FAST_POLL_MINUTES_BEFORE_START", 15), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}
        ), patch.object(
            sports, "trusted_capper_watches_exact_sport", return_value=False
        ), patch.object(
            sports, "trusted_capper_broad_discovery_sport", return_value=False
        ):
            status = sports.cached_odds_entry(
                cache,
                "baseball_mlb",
                required_event_ids=["actionable-pregame"],
            )
        self.assertTrue(status["valid"])
        self.assertEqual(status["adaptive_pregame_cache_max_minutes"], 10)

    def test_actionable_cache_refreshes_immediately_for_new_event(self):
        cache = {
            "version": 2,
            "sports": {
                "baseball_mlb": {
                    "generated_at": datetime.now(sports.LOCAL_TZ).isoformat(),
                    "games": [{"id": "old-event"}],
                }
            },
        }
        status = sports.cached_odds_entry(
            cache,
            "baseball_mlb",
            required_event_ids=["new-event"],
        )
        self.assertFalse(status["valid"])
        self.assertEqual(status["reason"], "event_coverage")
        self.assertEqual(status["missing_event_ids"], ["new-event"])

    @patch.object(sports, "SPORTS_PREGAME_ENABLED", True)
    @patch.object(sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", True)
    @patch.object(sports, "SPORTS_ODDS_CACHE_MAX_HOURS", 1)
    def test_distant_pregame_cache_uses_hourly_refresh(self):
        now = datetime.now(timezone.utc)
        cache = {
            "version": 2,
            "sports": {
                "baseball_mlb": {
                    "generated_at": (datetime.now(sports.LOCAL_TZ) - timedelta(minutes=30)).isoformat(),
                    "games": [{"commence_time": (now + timedelta(days=1)).isoformat()}],
                }
            },
        }
        with patch.object(sports, "SPORTS_PREGAME_ENABLED", True), patch.object(
            sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", True
        ), patch.object(sports, "SPORTS_PREGAME_FAR_POLL_MINUTES_BEFORE_START", 180), patch.object(
            sports, "SPORTS_PREGAME_FAR_ODDS_CACHE_MAX_MINUTES", 60
        ), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}
        ), patch.object(
            sports, "trusted_capper_watches_exact_sport", return_value=False
        ), patch.object(
            sports, "trusted_capper_broad_discovery_sport", return_value=False
        ):
            status = sports.cached_odds_entry(cache, "baseball_mlb")
        self.assertTrue(status["valid"])
        self.assertEqual(status["adaptive_pregame_cache_max_minutes"], 60)

    def test_fast_live_sports_use_stricter_odds_freshness(self):
        with patch.object(sports, "SPORTS_LIVE_TENNIS_MAX_ODDS_AGE_MINUTES", 1.5), patch.object(
            sports, "SPORTS_LIVE_BASKETBALL_MAX_ODDS_AGE_MINUTES", 2.0
        ), patch.object(sports, "SPORTS_LIVE_GAME_MAX_ODDS_AGE_MINUTES", 3.0):
            self.assertEqual(sports.live_odds_max_age_minutes({"sport_key": "tennis_itf_men"}), 1.5)
            self.assertEqual(sports.live_odds_max_age_minutes({"sport_key": "tennis_wta"}), 1.5)
            self.assertEqual(sports.live_odds_max_age_minutes({"sport_key": "basketball_nba"}), 2.0)
            self.assertEqual(sports.live_odds_max_age_minutes({"sport_key": "baseball_mlb"}), 3.0)

    @patch.object(sports, "SPORTS_LIVE_GAME_BETTING_ENABLED", True)
    def test_actionable_live_tennis_cache_refreshes_inside_one_minute(self):
        now = datetime.now(timezone.utc)
        cache = {
            "version": 2,
            "sports": {
                "tennis_wta_us_open": {
                    "generated_at": (datetime.now(sports.LOCAL_TZ) - timedelta(seconds=55)).isoformat(),
                    "games": [{
                        "id": "live-tennis",
                        "sport_key": "tennis_wta_us_open",
                        "commence_time": (now - timedelta(minutes=20)).isoformat(),
                    }],
                }
            },
        }
        with patch.object(sports, "SPORTS_LIVE_ODDS_CACHE_MAX_MINUTES", 1.5), patch.object(
            sports, "SPORTS_LIVE_TENNIS_REFRESH_SECONDS", 50
        ), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}
        ):
            status = sports.cached_odds_entry(
                cache,
                "tennis_wta_us_open",
                required_event_ids=["live-tennis"],
            )
        self.assertFalse(status["valid"])
        self.assertEqual(status["reason"], "live_stale")
        self.assertAlmostEqual(status["adaptive_live_cache_max_minutes"], 50 / 60)

    @patch.object(sports, "ODDS_API_KEYS", ["fixture-key"])
    def test_exhausted_daily_budget_is_summarized_before_per_sport_loop(self):
        with patch.object(sports, "ODDS_API_KEY", "test-key"), patch.object(
            sports, "SPORTS_ODDS_DAILY_CREDIT_LIMIT", 700
        ), patch.object(sports, "expand_sport_keys", return_value=["baseball_mlb", "basketball_wnba"]), patch.object(
            sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}
        ), patch.object(
            sports,
            "can_spend_odds_credits",
            return_value=(False, {"budget_block_reason": "daily_emergency_cap", "rolling_credits_used": 3200}, 700),
        ), patch.object(sports, "log_line") as log_mock, patch.object(sports, "append_jsonl") as event_mock, patch.object(
            sports, "schedule_gate_decision"
        ) as schedule_mock:
            result = sports.fetch_odds_games(["baseball_mlb"])

        self.assertEqual(result, [])
        self.assertEqual(log_mock.call_count, 1)
        self.assertIn("sports=2", log_mock.call_args.args[1])
        self.assertEqual(event_mock.call_args.args[1]["type"], "odds_api_budget_exhausted")
        schedule_mock.assert_not_called()

    def test_kalshi_gate_shortlist_is_not_reexpanded_before_paid_fetch(self):
        with patch.object(sports, "ODDS_API_KEYS", ("test-key",)), patch.object(
            sports, "ODDS_API_KEY", "test-key"
        ), patch.object(
            sports, "expand_sport_keys", return_value=["soccer_epl", "soccer_france_ligue_one"]
        ) as expand_mock, patch.object(
            sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}
        ), patch.object(
            sports,
            "can_spend_odds_credits",
            return_value=(False, {"budget_block_reason": "test", "rolling_credits_used": 0}, 0),
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            sports.fetch_odds_games(
                ["soccer_epl"],
                include_scores=False,
                sport_keys_already_expanded=True,
            )

        expand_mock.assert_not_called()

    def test_paid_odds_request_is_scoped_to_actionable_event_ids(self):
        event = {
            "id": "event-1",
            "sport_key": "baseball_mlb",
            "home_team": "Boston Red Sox",
            "away_team": "New York Yankees",
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "bookmakers": [],
        }
        with patch.object(sports, "ODDS_API_KEYS", ("test-key",)), patch.object(
            sports, "ODDS_API_KEY", "test-key"
        ), patch.object(sports, "SPORTS_ODDS_ACTIONABLE_EVENT_FILTER_ENABLED", True), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED", False
        ), patch.object(
            sports, "SPORTS_SCHEDULE_GATE_ENABLED", False
        ), patch.object(sports, "expand_sport_keys", return_value=["baseball_mlb"]), patch.object(
            sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}
        ), patch.object(
            sports,
            "can_spend_odds_credits",
            return_value=(True, {"monthly_credits_used": 0}, 0),
        ), patch.object(sports, "odds_pause_until", return_value={}), patch.object(
            sports,
            "odds_api_get_json",
            return_value=([event], {"x-requests-remaining": "999", "x-requests-last": "1", "_polymaker_odds_key_index": 0}),
        ) as get_mock, patch.object(
            sports,
            "record_odds_spend",
            return_value={"daily_credits_used": 1, "rolling_credits_used": 1, "monthly_credits_used": 1},
        ), patch.object(sports, "save_odds_cache_state"), patch.object(
            sports, "update_schedule_cache_for_sport"
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            games = sports.fetch_odds_games(
                ["baseball_mlb"],
                requested_market_types={"baseball_mlb": ["moneyline"]},
                requested_event_ids_by_sport={"baseball_mlb": ["event-1"]},
                include_scores=False,
            )
        self.assertEqual([row["id"] for row in games], ["event-1"])
        self.assertEqual(get_mock.call_args.kwargs["params"]["eventIds"], "event-1")
        self.assertEqual(get_mock.call_args.kwargs["params"]["regions"], "us")

    def test_monthly_budget_allows_spend_below_monthly_cap(self):
        now = datetime.now()
        with TemporaryDirectory() as temp_dir, patch.object(
            sports, "ODDS_BUDGET_FILE", str(Path(temp_dir) / "budget.json")
        ), patch.object(sports, "LOG_FILE", str(Path(temp_dir) / "missing.log")), patch.object(
            sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 10000
        ), patch.object(
            sports, "SPORTS_ODDS_HARD_CREDIT_LIMIT", 10000
        ), patch.object(
            sports, "odds_usage", return_value={"date": now.date().isoformat(), "estimated_credits_used": 0, "calls": []}
        ), patch.object(
            sports,
            "odds_provider_account_summary",
            return_value={
                "accounts": [
                    {
                        "request_eligible": True,
                        "provider_remaining": 500,
                    }
                ],
                "request_eligible_accounts": 1,
                "provider_credits_remaining_authoritative": 500,
            },
        ):
            sports.save_odds_budget_state(
                {
                    "version": 1,
                    "entries": [{"at": now.isoformat(), "actual_cost": 800}],
                }
            )
            allowed, usage, daily_used = sports.can_spend_odds_credits(3)
        self.assertTrue(allowed)
        self.assertEqual(daily_used, 800)
        self.assertEqual(usage["monthly_credits_used"], 800)

    def test_monthly_budget_blocks_at_monthly_cap(self):
        now = datetime.now()
        with TemporaryDirectory() as temp_dir, patch.object(
            sports, "ODDS_BUDGET_FILE", str(Path(temp_dir) / "budget.json")
        ), patch.object(sports, "LOG_FILE", str(Path(temp_dir) / "missing.log")), patch.object(
            sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 4500
        ), patch.object(
            sports, "SPORTS_ODDS_HARD_CREDIT_LIMIT", 4500
        ), patch.object(
            sports, "odds_usage", return_value={"date": now.date().isoformat(), "estimated_credits_used": 0, "calls": []}
        ), patch.object(
            sports,
            "odds_provider_account_summary",
            return_value={
                "accounts": [
                    {
                        "request_eligible": True,
                        "provider_remaining": 500,
                    }
                ],
                "request_eligible_accounts": 1,
                "provider_credits_remaining_authoritative": 500,
            },
        ):
            sports.save_odds_budget_state(
                {
                    "version": 1,
                    # Keep the fixture inside the current calendar month even
                    # when the suite runs on the first day of a new month.
                    "entries": [{"at": (now - timedelta(hours=1)).isoformat(), "actual_cost": 4499}],
                }
            )
            allowed, usage, daily_used = sports.can_spend_odds_credits(3)
        self.assertFalse(allowed)
        self.assertEqual(daily_used, 4499)
        self.assertEqual(usage["monthly_credits_used"], 4499)

    def test_provider_cycle_usage_is_authoritative_after_plan_upgrade(self):
        budget = {"monthly_credits_used": 8066}
        provider = {
            "accounts": [
                {
                    # The retired key is eligible only for an occasional
                    # provider probe and must not poison active-cycle usage.
                    "request_eligible": True,
                    "provider_status": "exhausted",
                    "provider_used": None,
                    "provider_remaining": 0,
                },
                {
                    "request_eligible": True,
                    "provider_used": 10,
                    "provider_remaining": 99990,
                },
            ]
        }
        with patch.object(sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 70000):
            usage = sports.odds_operating_credit_usage(budget, provider)
        self.assertEqual(usage["operating_credits_used"], 10)
        self.assertEqual(usage["operating_credits_remaining"], 69990)
        self.assertEqual(
            usage["operating_usage_source"],
            "provider_reported_active_cycle",
        )

    def test_provider_operating_limit_blocks_without_daily_cap(self):
        with patch.object(
            sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 70000
        ), patch.object(
            sports, "SPORTS_ODDS_HARD_CREDIT_LIMIT", 70000
        ), patch.object(
            sports,
            "odds_usage",
            return_value={"date": "2026-08-06", "calls": []},
        ), patch.object(
            sports,
            "odds_budget_usage",
            return_value={
                "state": {},
                "monthly_credits_used": 12000,
                "daily_credits_used": 5000,
            },
        ), patch.object(
            sports,
            "odds_provider_account_summary",
            return_value={
                "accounts": [{
                    "request_eligible": True,
                    "provider_used": 69999,
                    "provider_remaining": 30001,
                }],
                "request_eligible_accounts": 1,
            },
        ):
            allowed, usage, daily_used = sports.can_spend_odds_credits(2)
        self.assertFalse(allowed)
        self.assertEqual(usage["budget_block_reason"], "provider_cycle_hard_cap")
        self.assertEqual(usage["operating_credits_used"], 69999)
        self.assertEqual(daily_used, 5000)
        self.assertEqual(usage["budget_block_reason"], "provider_cycle_hard_cap")

    def test_adaptive_provider_pacing_stretches_refreshes_without_daily_cap(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        budget = {
            "monthly_credits_used": 9000,
            "state": {
                "entries": [
                    {"at": (now - timedelta(hours=1)).isoformat(), "actual_cost": 420}
                ]
            },
        }
        provider = {
            "accounts": [{
                "request_eligible": True,
                "provider_status": "available",
                "provider_used": 42,
                "provider_remaining": 99958,
            }]
        }
        with patch.object(
            sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 70000
        ), patch.object(
            sports, "SPORTS_ODDS_PACING_CREDIT_TARGET", 70000
        ), patch.object(
            sports, "SPORTS_ODDS_ADAPTIVE_PACING_ENABLED", True
        ), patch.object(
            sports, "SPORTS_ODDS_PACING_WINDOW_HOURS", 3.0
        ), patch.object(
            sports, "SPORTS_ODDS_PACING_TRIGGER_RATIO", 1.05
        ), patch.object(
            sports, "SPORTS_ODDS_PACING_MAX_MULTIPLIER", 1.75
        ):
            pacing = sports.odds_paid_refresh_pacing(now, budget, provider)
        self.assertGreater(pacing["paid_refresh_multiplier"], 1.0)
        self.assertLessEqual(pacing["paid_refresh_multiplier"], 1.75)
        self.assertEqual(pacing["pacing_recent_credits"], 420)

    def test_adaptive_provider_pacing_does_not_slow_an_on_pace_profile(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        budget = {
            "monthly_credits_used": 9000,
            "state": {
                "entries": [
                    {"at": (now - timedelta(hours=1)).isoformat(), "actual_cost": 30}
                ]
            },
        }
        provider = {
            "accounts": [{
                "request_eligible": True,
                "provider_status": "available",
                "provider_used": 42,
                "provider_remaining": 99958,
            }]
        }
        with patch.object(
            sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 70000
        ), patch.object(
            sports, "SPORTS_ODDS_PACING_CREDIT_TARGET", 70000
        ), patch.object(
            sports, "SPORTS_ODDS_ADAPTIVE_PACING_ENABLED", True
        ), patch.object(
            sports, "SPORTS_ODDS_PACING_WINDOW_HOURS", 3.0
        ):
            pacing = sports.odds_paid_refresh_pacing(now, budget, provider)
        self.assertEqual(pacing["paid_refresh_multiplier"], 1.0)

    def test_monitoring_target_does_not_block_reserve_usage(self):
        with patch.object(
            sports, "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT", 70000
        ), patch.object(
            sports, "SPORTS_ODDS_HARD_CREDIT_LIMIT", 100000
        ), patch.object(
            sports,
            "odds_usage",
            return_value={"date": "2026-08-06", "calls": []},
        ), patch.object(
            sports,
            "odds_budget_usage",
            return_value={
                "state": {},
                "monthly_credits_used": 75000,
                "daily_credits_used": 5000,
            },
        ), patch.object(
            sports,
            "odds_provider_account_summary",
            return_value={
                "accounts": [{
                    "request_eligible": True,
                    "provider_status": "available",
                    "provider_used": 75000,
                    "provider_remaining": 25000,
                }],
                "request_eligible_accounts": 1,
            },
        ):
            allowed, usage, _daily_used = sports.can_spend_odds_credits(3)
        self.assertTrue(allowed)
        self.assertEqual(usage["operating_credits_remaining"], 0)
        self.assertEqual(usage["hard_credits_remaining"], 25000)

    def test_live_game_only_expires_its_own_sport_cache(self):
        now = datetime.now(timezone.utc)
        generated_at = (datetime.now(sports.LOCAL_TZ) - timedelta(minutes=20)).isoformat()
        cache = {
            "version": 2,
            "sports": {
                "baseball_mlb": {
                    "generated_at": generated_at,
                    "games": [{"sport_key": "baseball_mlb", "commence_time": (now - timedelta(hours=1)).isoformat()}],
                },
                "basketball_wnba": {
                    "generated_at": generated_at,
                    "games": [{"sport_key": "basketball_wnba", "commence_time": (now + timedelta(hours=1)).isoformat()}],
                },
            },
        }
        with patch.object(sports, "SPORTS_LIVE_GAME_BETTING_ENABLED", True), patch.object(
            sports, "SPORTS_LIVE_ODDS_CACHE_MAX_MINUTES", 10
        ), patch.object(sports, "SPORTS_ODDS_CACHE_MAX_HOURS", 0.75), patch.object(
            sports, "SPORTS_PREGAME_ENABLED", False
        ):
            games, fresh, stale, live_stale = sports.select_cached_odds(
                cache, ["baseball_mlb", "basketball_wnba"]
            )
        self.assertEqual(fresh, ["basketball_wnba"])
        self.assertEqual(stale, ["baseball_mlb"])
        self.assertEqual(live_stale, ["baseball_mlb"])
        self.assertEqual([game["sport_key"] for game in games], ["basketball_wnba"])

    def test_empty_per_sport_cache_entry_is_valid_until_expiry(self):
        cache = {
            "version": 2,
            "sports": {
                "soccer_epl": {
                    "generated_at": datetime.now(sports.LOCAL_TZ).isoformat(),
                    "games": [],
                }
            },
        }
        status = sports.cached_odds_entry(cache, "soccer_epl")
        self.assertTrue(status["valid"])
        self.assertEqual(status["games"], [])

    def test_schedule_refresh_uses_zero_credit_events_endpoint(self):
        event = {
            "id": "game-1",
            "sport_key": "baseball_mlb",
            "home_team": "Home",
            "away_team": "Away",
            "commence_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        with patch.object(sports, "ODDS_API_KEY", "test-key"), patch.object(
            sports, "SPORTS_SCHEDULE_GATE_ENABLED", True
        ), patch.object(sports, "load_schedule_cache", return_value={"sports": {}}), patch.object(
            sports, "get_json", return_value=([event], {"x-requests-last": "0"})
        ) as get_mock, patch.object(sports, "save_schedule_cache") as save_mock, patch.object(
            sports, "record_odds_spend"
        ) as spend_mock, patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            cache = sports.refresh_schedule_cache_from_events(["baseball_mlb"])
        self.assertIn("/baseball_mlb/events", get_mock.call_args.args[0])
        self.assertEqual(cache["sports"]["baseball_mlb"]["game_count"], 1)
        save_mock.assert_called_once()
        spend_mock.assert_not_called()

    def test_odds_api_quota_failure_rotates_to_fallback_without_persisting_keys(self):
        with TemporaryDirectory() as temp_dir, patch.object(
            sports, "ODDS_API_KEYS", ("primary-secret", "fallback-secret")
        ), patch.object(sports, "ODDS_API_KEY", "primary-secret"), patch.object(
            sports, "ODDS_KEY_STATE_FILE", str(Path(temp_dir) / "key_state.json")
        ), patch.object(
            sports, "ODDS_PAUSE_FILE", str(Path(temp_dir) / "pause.json")
        ), patch.object(
            sports, "LOG_FILE", str(Path(temp_dir) / "sports.log")
        ), patch.object(
            sports, "EVENTS_FILE", str(Path(temp_dir) / "events.jsonl")
        ), patch.object(
            sports,
            "get_json",
            side_effect=[
                RuntimeError("OUT_OF_USAGE_CREDITS"),
                ([{"id": "game-1"}], {"x-requests-remaining": "499"}),
            ],
        ) as get_mock:
            data, headers = sports.odds_api_get_json(
                "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
                params={"markets": "h2h"},
            )
            state = json.loads((Path(temp_dir) / "key_state.json").read_text())
            event_text = (Path(temp_dir) / "events.jsonl").read_text()

        self.assertEqual(data, [{"id": "game-1"}])
        self.assertEqual(headers["x-requests-remaining"], "499")
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(get_mock.call_args_list[0].kwargs["params"]["apiKey"], "primary-secret")
        self.assertEqual(get_mock.call_args_list[1].kwargs["params"]["apiKey"], "fallback-secret")
        self.assertEqual(state["active_index"], 1)
        self.assertNotIn("primary-secret", json.dumps(state))
        self.assertNotIn("fallback-secret", json.dumps(state))
        self.assertNotIn("primary-secret", event_text)
        self.assertNotIn("fallback-secret", event_text)

    @patch.object(sports, "ODDS_API_KEYS", ["fixture-key"])
    def test_routine_scores_cost_one_and_completed_history_costs_two(self):
        usage = {"estimated_credits_used": 1, "daily_credits_used": 1, "rolling_credits_used": 100}
        with patch.object(sports, "ODDS_API_KEY", "test-key"), patch.object(
            sports, "SPORTS_SCORES_ENABLED", True
        ), patch.object(sports, "SPORTS_SCORES_DAYS_FROM", 1), patch.object(
            sports, "can_spend_odds_credits", return_value=(True, usage, 0)
        ), patch.object(sports, "get_json", return_value=([], {"x-requests-last": "1"})) as get_mock, patch.object(
            sports, "record_odds_spend", return_value=usage
        ) as spend_mock, patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            sports.fetch_scores_for_sport("baseball_mlb", include_completed=False)
        self.assertNotIn("daysFrom", get_mock.call_args.kwargs["params"])
        self.assertEqual(spend_mock.call_args.args[2], 1)

        usage = {"estimated_credits_used": 2, "daily_credits_used": 2, "rolling_credits_used": 101}
        with patch.object(sports, "ODDS_API_KEY", "test-key"), patch.object(
            sports, "SPORTS_SCORES_ENABLED", True
        ), patch.object(sports, "SPORTS_SCORES_DAYS_FROM", 1), patch.object(
            sports, "can_spend_odds_credits", return_value=(True, usage, 0)
        ), patch.object(sports, "get_json", return_value=([], {"x-requests-last": "2"})) as get_mock, patch.object(
            sports, "record_odds_spend", return_value=usage
        ) as spend_mock, patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            sports.fetch_scores_for_sport("baseball_mlb", include_completed=True)
        self.assertEqual(get_mock.call_args.kwargs["params"]["daysFrom"], 1)
        self.assertEqual(spend_mock.call_args.args[2], 2)

    def test_all_active_discovers_event_sports_and_excludes_outrights_and_politics(self):
        rows = [
            {"key": "soccer_epl", "active": True, "group": "Soccer", "has_outrights": False},
            {"key": "basketball_nba_summer_league", "active": True, "group": "Basketball", "has_outrights": False},
            {"key": "golf_major_winner", "active": True, "group": "Golf", "has_outrights": True},
            {"key": "politics_test", "active": True, "group": "Politics", "has_outrights": False},
            {"key": "mma_mixed_martial_arts", "active": True, "group": "Combat Sports", "has_outrights": False},
            {"key": "boxing_boxing", "active": True, "group": "Boxing", "has_outrights": False},
        ]
        with patch.object(sports, "fetch_active_sports", return_value=rows), patch.object(
            sports, "SPORTS_ALL_ACTIVE_ENABLED", True
        ), patch.object(sports, "SPORTS_ALL_ACTIVE_INCLUDE_OUTRIGHTS", False), patch.object(
            sports, "SPORTS_ALL_ACTIVE_EXCLUDED_GROUPS", {"politics"}
        ), patch.object(
            sports, "trusted_capper_active_sports", return_value=set()
        ), patch.object(
            sports, "trusted_capper_requested_disabled_sports", return_value=set()
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            keys = sports.expand_sport_keys([])
        self.assertEqual(keys, ["basketball_nba_summer_league", "soccer_epl"])

    def test_capper_generic_sport_resolves_to_schedule_snapshot_league(self):
        soccer_ticket = {
            "sport_key": "soccer",
            "schedule_snapshot": {"games": [{
                "sport_key": "soccer_efl_champ",
                "home_team": "West Ham",
                "away_team": "Wolves",
            }]},
        }
        tennis_ticket = {
            "sport_key": "tennis",
            "schedule_snapshot": {"games": [{
                "sport_key": "tennis_atp_us_open",
                "home_team": "Player One",
                "away_team": "Player Two",
            }]},
        }
        self.assertEqual(
            {"soccer_efl_champ"},
            sports.trusted_capper_ticket_provider_sports(soccer_ticket),
        )
        self.assertEqual(
            {"tennis_atp_us_open"},
            sports.trusted_capper_ticket_provider_sports(tennis_ticket),
        )
        self.assertEqual(
            {"soccer"},
            sports.trusted_capper_ticket_provider_sports({"sport_key": "soccer"}),
        )

    def test_exact_capper_league_does_not_reexpand_to_all_soccer(self):
        rows = [
            {"key": "baseball_mlb", "active": True},
            {"key": "soccer_efl_champ", "active": True},
            {"key": "soccer_epl", "active": True},
            {"key": "soccer_spain_la_liga", "active": True},
        ]
        with patch.object(sports, "fetch_active_sports", return_value=rows), patch.object(
            sports, "SPORTS_ALL_ACTIVE_ENABLED", False
        ), patch.object(
            sports, "trusted_capper_active_sports", return_value={"soccer_efl_champ"}
        ), patch.object(
            sports, "trusted_capper_requested_disabled_sports", return_value=set()
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            keys = sports.expand_sport_keys(["baseball_mlb", "soccer_efl_champ"])
        self.assertEqual(["baseball_mlb", "soccer_efl_champ"], keys)

    def test_unresolved_generic_capper_soccer_still_discovers_active_leagues(self):
        rows = [
            {"key": "soccer_efl_champ", "active": True},
            {"key": "soccer_epl", "active": True},
        ]
        with patch.object(sports, "fetch_active_sports", return_value=rows), patch.object(
            sports, "SPORTS_ALL_ACTIVE_ENABLED", False
        ), patch.object(
            sports, "trusted_capper_active_sports", return_value={"soccer"}
        ), patch.object(
            sports, "trusted_capper_requested_disabled_sports", return_value=set()
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            keys = sports.expand_sport_keys([])
        self.assertEqual(["soccer_efl_champ", "soccer_epl"], keys)

    def test_configured_mma_and_boxing_are_always_removed(self):
        with patch.object(sports, "SPORTS_ALL_ACTIVE_ENABLED", False), patch.object(
            sports, "SPORTS_ODDS_SKIP_INACTIVE", False
        ), patch.object(
            sports, "trusted_capper_active_sports", return_value=set()
        ), patch.object(
            sports, "trusted_capper_requested_disabled_sports", return_value=set()
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            keys = sports.expand_sport_keys([
                "baseball_mlb",
                "mma_mixed_martial_arts",
                "boxing_boxing",
            ])
        self.assertEqual(keys, ["baseball_mlb"])

    def test_kalshi_combat_series_are_blocked(self):
        with patch.object(
            sports,
            "trusted_capper_requested_disabled_sports",
            return_value=set(),
        ):
            self.assertFalse(sports.is_supported_kalshi_series({
                "ticker": "KXUFCFIGHT-26JUL18-FIGHTER",
                "series_ticker": "KXUFCFIGHT",
            }))
            self.assertFalse(sports.is_supported_kalshi_series({
                "ticker": "KXBOXING-26JUL18-FIGHTER",
                "series_ticker": "KXBOXING",
            }))

    def test_soccer_uses_h2h_to_control_provider_cost(self):
        self.assertEqual(sports.markets_for_sport("soccer_epl"), "h2h")
        self.assertEqual(sports.markets_for_sport("tennis_itf_women"), "h2h")
        expected = "h2h,spreads" if sports.SPORTS_LIVE_CAMPAIGN_ENABLED else "h2h,spreads,totals"
        self.assertEqual(sports.markets_for_sport("basketball_euroleague"), expected)

    def test_atp_and_wta_expand_but_itf_is_disabled(self):
        rows = [
            {"key": "tennis_atp_wimbledon", "active": True},
            {"key": "tennis_wta_wimbledon", "active": True},
            {"key": "tennis_itf_m25_tulsa", "active": True},
            {"key": "tennis_itf_w15_dallas", "active": True},
        ]
        with patch.object(sports, "fetch_active_sports", return_value=rows), patch.object(
            sports, "SPORTS_ALL_ACTIVE_ENABLED", False
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            keys = sports.expand_sport_keys(["tennis_atp", "tennis_wta", "tennis_itf"])
        self.assertEqual(keys, [row["key"] for row in rows[:2]])

    def test_adaptive_live_tennis_poll_only_activates_for_live_atp_wta(self):
        with patch.object(sports, "SPORTS_LIVE_TENNIS_REFRESH_SECONDS", 50), patch.object(
            sports, "SPORTS_SCAN_INTERVAL_MINUTES", 3
        ), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}
        ):
            live_report = {
                "odds_api_gate": {"live_actionable_sports": ["tennis_wta"]}
            }
            pregame_report = {
                "odds_api_gate": {"live_actionable_sports": []}
            }
            itf_report = {
                "odds_api_gate": {"live_actionable_sports": ["tennis_itf"]}
            }
            self.assertEqual(sports.next_sports_scan_sleep_seconds(live_report), 50)
            self.assertEqual(sports.next_sports_scan_sleep_seconds(pregame_report), 180)
            self.assertEqual(sports.next_sports_scan_sleep_seconds(itf_report), 180)

    def test_budget_pressure_keeps_hot_and_live_tennis_scan_cadence(self):
        with patch.object(sports, "SPORTS_HOT_RECHECK_BURST_COUNT", 0), patch.object(
            sports, "SPORTS_HOT_RECHECK_INTERVAL_SECONDS", 15
        ), patch.object(sports, "SPORTS_HOT_RECHECK_MAX_BURST_SCANS", 3), patch.object(
            sports, "SPORTS_LIVE_TENNIS_REFRESH_SECONDS", 45
        ), patch.object(sports, "SPORTS_SCAN_INTERVAL_MINUTES", 2.5), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 6.0}
        ):
            self.assertEqual(
                15,
                sports.next_sports_scan_sleep_seconds({"hot_recheck": {"count": 1}}),
            )
            self.assertEqual(
                45,
                sports.next_sports_scan_sleep_seconds({
                    "hot_recheck": {"count": 0},
                    "odds_api_gate": {"live_actionable_sports": ["tennis_atp"]},
                }),
            )
            self.assertEqual(
                900,
                sports.next_sports_scan_sleep_seconds({"hot_recheck": {"count": 0}}),
            )

    def test_tennis_direct_series_cover_singles_challengers_itf_and_doubles(self):
        series = set(sports.kalshi_market_series_for_sports(["tennis_atp", "tennis_wta", "tennis_itf"]))
        self.assertTrue(
            {
                "KXATPMATCH",
                "KXATPGAME",
                "KXATPCHALLENGERMATCH",
                "KXCHALLENGERMATCH",
                "KXATPDOUBLES",
                "KXWTAMATCH",
                "KXWTAGAME",
                "KXWTACHALLENGERMATCH",
                "KXWTADOUBLES",
                "KXITFMATCH",
                "KXITFWMATCH",
                "KXITFDOUBLES",
                "KXITFWDOUBLES",
            }.issubset(series)
        )
        self.assertFalse(any("SETWINNER" in item or "GWINNER" in item for item in series))

    def test_three_way_moneyline_removes_draw_vig(self):
        features = {
            "best_h2h": {
                "Home": {"price": 100},
                "Away": {"price": 100},
                "Draw": {"price": 200},
            }
        }
        probs = sports.no_vig_moneyline_probs(features, "Home", "Away")
        self.assertAlmostEqual(probs["home"]["prob"], 37.5, places=1)
        self.assertAlmostEqual(probs["away"]["prob"], 37.5, places=1)

    def test_dynamic_series_keeps_full_match_and_rejects_set_market(self):
        self.assertTrue(
            sports.is_actionable_full_event_series(
                {"title": "ITF Match", "contract_terms_url": "https://example/ITFMATCH.pdf"}
            )
        )
        self.assertFalse(
            sports.is_actionable_full_event_series(
                {"title": "ATP Set Winner", "contract_terms_url": "https://example/TENNISSETWINNER.pdf"}
            )
        )

    def test_generic_model_excludes_full_and_half_soccer_btts(self):
        # Full-game BTTS is fetched only for its exact trusted-capper lane; it
        # is not priceable as an ordinary h2h/spread/total model candidate.
        self.assertFalse(sports.is_actionable_full_event_series({
            "title": "BTTS",
            "contract_terms_url": "https://example/SOCCERBTTS.pdf",
        }))
        self.assertFalse(sports.is_actionable_full_event_series({
            "title": "First Half BTTS",
            "contract_terms_url": "https://example/SOCCERHBTTS.pdf",
        }))

    @patch.object(sports, "SPORTS_CAPPER_ENABLED", True)
    def test_active_capper_btts_ticket_resolves_exact_league_series(self):
        ticket = {
            "status": "unmatched",
            "executable": True,
            "market_type": "btts",
            "target_event_date": (datetime.now(sports.LOCAL_TZ).date() + timedelta(days=1)).isoformat(),
            "components": [{"market_type": "btts"}],
            "schedule_snapshot": {
                "games": [{"sport_key": "soccer_efl_champ"}],
            },
        }
        meta = {
            "KXEFLCHAMPIONSHIPBTTS": {
                "tags": {"soccer"},
                "title": "btts",
                "contract_key": "SOCCERBTTS",
            },
            "KXEFLCHAMPIONSHIP1HBTTS": {
                "tags": {"soccer"},
                "title": "first half btts",
                "contract_key": "SOCCERHBTTS",
            },
            "KXEPLBTTS": {
                "tags": {"soccer"},
                "title": "epl btts",
                "contract_key": "SOCCERBTTS",
            },
        }
        with patch.object(sports, "list_capper_tickets", return_value=[ticket]), patch.object(
            sports,
            "load_cached_active_sports",
            return_value=[{
                "key": "soccer_efl_champ",
                "title": "Championship",
                "description": "EFL Championship",
            }],
        ), patch.object(
            sports, "load_dynamic_actionable_kalshi_series", return_value=set(meta)
        ), patch.object(sports, "_KALSHI_SPORTS_SERIES_META", meta):
            self.assertEqual(
                ("KXEFLCHAMPIONSHIPBTTS",),
                sports.trusted_capper_requested_btts_series(),
            )

    def test_market_series_tag_blocks_cross_sport_team_collision(self):
        meta = {
            "KXNPBSPREAD": {
                "tags": {"baseball"},
                "title": "nippon baseball spread",
                "contract_key": "BASEBALLSPREAD",
            }
        }
        with patch.object(sports, "_DYNAMIC_ACTIONABLE_KALSHI_SERIES", {"KXNPBSPREAD"}), patch.object(
            sports, "_KALSHI_SPORTS_SERIES_META", meta
        ):
            self.assertFalse(
                sports.market_matches_provider_sport(
                    {"sport_key": "aussierules_afl"},
                    {"event_ticker": "KXNPBSPREAD-26JUL18ABC"},
                )
            )
            self.assertTrue(
                sports.market_matches_provider_sport(
                    {"sport_key": "baseball_npb"},
                    {"event_ticker": "KXNPBSPREAD-26JUL18ABC"},
                )
            )

    def test_dynamic_spread_series_cannot_be_mispriced_as_moneyline(self):
        meta = {
            "KXMLSSPREAD": {
                "tags": {"soccer"},
                "title": "mls spread",
                "contract_key": "SOCCERSPREAD",
            }
        }
        with patch.object(sports, "_DYNAMIC_ACTIONABLE_KALSHI_SERIES", {"KXMLSSPREAD"}), patch.object(
            sports, "_KALSHI_SPORTS_SERIES_META", meta
        ):
            result = sports.classify_kalshi_market(
                {
                    "event_ticker": "KXMLSSPREAD-26JUL22NSHMTL",
                    "ticker": "KXMLSSPREAD-26JUL22NSHMTL-NSH3",
                    "title": "Will Nashville SC win by 3 or more goals?",
                }
            )
        self.assertEqual(result["type"], "spread")
        self.assertEqual(result["point"], -2.5)

    def test_broad_league_prefix_requires_full_event_catalog_approval(self):
        dangerous = (
            {
                "series_ticker": "KXNBASERIESROADWIN",
                "ticker": "KXNBASERIESROADWIN-26JUL19BOSNY-NY",
                "title": "Will New York get its first road win in the series?",
            },
            {
                "series_ticker": "KXMLBWINS-NYY",
                "ticker": "KXMLBWINS-NYY-26-NYY90",
                "title": "Will the New York Yankees win 90 games?",
            },
            {
                "series_ticker": "KXNHLMVP",
                "ticker": "KXNHLMVP-26-MCDAVID",
                "title": "Will Connor McDavid win the Hart Trophy?",
            },
        )
        with patch.object(sports, "KALSHI_SERIES_PREFIXES", ("KXNBA", "KXMLB", "KXNHL")), patch.object(
            sports, "KALSHI_MARKET_SERIES", ("KXNBAGAME", "KXMLBGAME", "KXNHLGAME")
        ), patch.object(sports, "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED", True), patch.object(
            sports, "_DYNAMIC_ACTIONABLE_KALSHI_SERIES", set()
        ):
            for market in dangerous:
                with self.subTest(series=market["series_ticker"]):
                    self.assertFalse(sports.is_supported_kalshi_series(market))

        with patch.object(sports, "KALSHI_SERIES_PREFIXES", ("KXATP",)), patch.object(
            sports, "KALSHI_MARKET_SERIES", ("KXATPGAME",)
        ), patch.object(sports, "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED", True), patch.object(
            sports, "_DYNAMIC_ACTIONABLE_KALSHI_SERIES", set()
        ):
            self.assertTrue(
                sports.is_supported_kalshi_series(
                    {
                        "series_ticker": "KXATPGAME",
                        "ticker": "KXATPGAME-26JUL19PLAYERA-PLAYERA",
                        "title": "Will Player A win the match?",
                    }
                )
            )

    def test_broad_static_prefix_does_not_admit_partial_game_markets(self):
        with patch.object(sports, "KALSHI_SERIES_PREFIXES", ("KXNBA",)), patch.object(
            sports, "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED", False
        ):
            self.assertFalse(
                sports.is_supported_kalshi_series(
                    {
                        "event_ticker": "KXNBASUMMER1HWINNER-26JUL16PORDEN",
                        "ticker": "KXNBASUMMER1HWINNER-26JUL16PORDEN-DEN",
                        "title": "First half winner",
                    }
                )
            )
            self.assertFalse(
                sports.is_supported_kalshi_series(
                    {
                        "event_ticker": "KXNBASUMMER1HSPREAD-26JUL16PORDEN",
                        "ticker": "KXNBASUMMER1HSPREAD-26JUL16PORDEN-DEN3",
                        "title": "Denver wins the first half by over 2.5 points",
                    }
                )
            )
            self.assertTrue(
                sports.is_supported_kalshi_series(
                    {
                        "event_ticker": "KXNBASUMMERTOTAL-26JUL16PORDEN",
                        "ticker": "KXNBASUMMERTOTAL-26JUL16PORDEN-201",
                        "title": "Full Game: Over 200.5 points scored",
                    }
                )
            )

        with patch.object(sports, "KALSHI_SERIES_PREFIXES", ("KXWNBA", "KXNBA", "KXNHL", "KXAFCON")), patch.object(
            sports, "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED", False
        ):
            partial_markets = (
                {
                    "event_ticker": "KXWNBA3QSPREAD-26JUL19CONNPHX",
                    "ticker": "KXWNBA3QSPREAD-26JUL19CONNPHX-PHX4",
                    "title": "Will Phoenix win the third quarter by 4 or more points?",
                },
                {
                    "event_ticker": "KXWNBA4QSPREAD-26JUL19CONNPHX",
                    "ticker": "KXWNBA4QSPREAD-26JUL19CONNPHX-PHX2",
                    "title": "Will Phoenix win the fourth quarter by 2 or more points?",
                },
                {
                    "event_ticker": "KXNBA1QTOTAL-26JUL19LALBOS",
                    "ticker": "KXNBA1QTOTAL-26JUL19LALBOS-55",
                    "title": "Will 54.5 or more points be scored in the first quarter?",
                },
                {
                    "event_ticker": "KXNHL2PSPREAD-26JUL19CHIDET",
                    "ticker": "KXNHL2PSPREAD-26JUL19CHIDET-DET2",
                    "title": "Will Detroit win the second period by 2 or more goals?",
                },
            )
            for market in partial_markets:
                with self.subTest(series=market["event_ticker"]):
                    self.assertTrue(sports.is_partial_event_market(market))
                    self.assertFalse(sports.is_supported_kalshi_series(market))
                    self.assertFalse(sports.is_simple_market(market))
                    self.assertEqual(
                        sports.classify_kalshi_market(market),
                        {"type": "unsupported", "reason": "partial_event_market"},
                    )
            self.assertTrue(
                sports.is_supported_kalshi_series(
                    {
                        "event_ticker": "KXAFCONGAME-26JUL19NGACMR",
                        "ticker": "KXAFCONGAME-26JUL19NGACMR-NGA",
                        "title": "AFCON Game Winner",
                    }
                )
            )

        with patch.object(sports, "KALSHI_SERIES_PREFIXES", ("KXMLB", "KXWBC")), patch.object(
            sports, "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED", False
        ):
            for series in ("KXMLBF3", "KXMLBF5", "KXMLBF5SPREAD", "KXMLBF5TOTAL", "KXMLBF7", "KXWBCF5"):
                with self.subTest(series=series):
                    self.assertFalse(
                        sports.is_supported_kalshi_series(
                            {
                                "event_ticker": f"{series}-26JUL17TBBOS",
                                "ticker": f"{series}-26JUL17TBBOS-BOS",
                                "title": "Boston wins the partial-game market",
                            }
                        )
                    )

    def test_ufc_betting_blocks_full_fight_winners_and_props(self):
        with patch.object(sports, "KALSHI_SERIES_PREFIXES", ("KXUFC",)), patch.object(
            sports, "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED", False
        ), patch.object(
            sports, "trusted_capper_requested_disabled_sports", return_value=set()
        ):
            self.assertFalse(
                sports.is_supported_kalshi_series(
                    {
                        "series_ticker": "KXUFCFIGHT",
                        "ticker": "KXUFCFIGHT-26JUL18CORNIC-COR",
                        "title": "Will Alden Coria win the fight?",
                    }
                )
            )
            for series in ("KXUFCMOV", "KXUFCMOF", "KXUFCVICROUND", "KXUFCROUNDS", "KXUFCDISTANCE"):
                with self.subTest(series=series):
                    self.assertFalse(
                        sports.is_supported_kalshi_series(
                            {
                                "series_ticker": series,
                                "ticker": f"{series}-26JUL18CORNIC-COR",
                                "title": "Will Alden Coria win by KO/TKO/DQ?",
                            }
                        )
                    )

    def test_ufc_method_and_round_props_are_not_classified_as_moneylines(self):
        markets = (
            {
                "series_ticker": "KXUFCMOV",
                "ticker": "KXUFCMOV-26JUL18CORNIC-CORKOTKODQ",
                "title": "Will Alden Coria win the fight by KO/TKO/DQ?",
            },
            {
                "series_ticker": "KXUFCVICROUND",
                "ticker": "KXUFCVICROUND-26JUL18CORNIC-COR2",
                "title": "Will Alden Coria win the fight in Round 2?",
            },
        )
        for market in markets:
            with self.subTest(ticker=market["ticker"]):
                result = sports.classify_kalshi_market(market)
                self.assertEqual(result["type"], "unsupported")
                self.assertEqual(result["reason"], "unsupported_prop_type")

    def test_ufc_full_fight_date_comma_and_provider_spelling_variant_are_safe(self):
        game = {
            "sport_key": "mma_mixed_martial_arts",
            "home_team": "Sergey Spivak",
            "away_team": "Vitor Petrino",
            "commence_time": "2026-08-23T02:15:00Z",
        }
        market = {
            "series_ticker": "KXUFCFIGHT",
            "event_ticker": "KXUFCFIGHT-26AUG22SPIPET",
            "ticker": "KXUFCFIGHT-26AUG22SPIPET-PET",
            "title": "Will Vitor Petrino win the Spivac vs Petrino professional MMA fight scheduled for Aug 22, 2026?",
            "yes_sub_title": "Vitor Petrino",
        }
        self.assertTrue(sports.is_simple_market(market))
        score, debug = sports.match_game_to_market(game, market)
        self.assertEqual(sports.SPORTS_MIN_MATCH_SCORE, score)
        self.assertEqual("Vitor Petrino", debug["combat_exact_selected_fighter"])
        self.assertEqual("vitor petrino", sports.infer_market_side(game, market))

        multi_outcome = {**market, "title": "Vitor Petrino, Sergey Spivak, or draw"}
        self.assertFalse(sports.is_simple_market(multi_outcome))

    def test_generic_two_letter_soccer_tokens_are_not_ticker_aliases(self):
        aliases = sports.team_aliases("AC Oulu")
        self.assertNotIn("ac", aliases)
        self.assertEqual(sports.alias_hits(aliases, "racing louisville vs houston dash", "kxnwslgame26julrachda"), [])

    def test_college_market_identity_rejects_generic_tennessee_tech_collision(self):
        market = {
            "ticker": "KXNCAAFGAME-26SEP12SAMTNTC-TNTC",
            "rules_primary": (
                "If Tennessee Tech wins the Samford vs Tennessee Tech college football "
                "game originally scheduled for Sep 12, 2026, then the market resolves to Yes."
            ),
        }
        wrong_game = {
            "sport_key": "americanfootball_ncaaf",
            "home_team": "Georgia Tech Yellow Jackets",
            "away_team": "Tennessee Volunteers",
        }
        correct_game = {
            "sport_key": "americanfootball_ncaaf",
            "home_team": "Tennessee Tech Golden Eagles",
            "away_team": "Samford Bulldogs",
        }

        wrong_score, wrong_debug = sports.match_game_to_market(wrong_game, market)
        correct_score, correct_debug = sports.match_game_to_market(correct_game, market)

        self.assertEqual(0, wrong_score)
        self.assertFalse(wrong_debug["college_event_identity"]["matched"])
        self.assertEqual(sports.SPORTS_MIN_MATCH_SCORE, correct_score)
        self.assertTrue(correct_debug["college_event_identity"]["matched"])

    def test_college_market_side_uses_campus_aware_yes_identity(self):
        game = {
            "sport_key": "americanfootball_ncaaf",
            "home_team": "Texas Longhorns",
            "away_team": "Texas State Bobcats",
        }
        market = {
            "ticker": "KXNCAAFGAME-26SEP05TXSTTEX-TXST",
            "title": "Texas St. wins",
            "yes_sub_title": "Texas St.",
            "rules_primary": (
                "If Texas St. wins the Texas St. vs Texas college football game, "
                "then the market resolves to Yes."
            ),
        }

        self.assertEqual("texas state bobcats", sports.infer_market_side(game, market))

    def test_college_market_side_does_not_collapse_miami_campuses(self):
        game = {
            "sport_key": "americanfootball_ncaaf",
            "home_team": "Miami Hurricanes",
            "away_team": "Miami (OH) RedHawks",
        }
        market = {
            "ticker": "KXNCAAFGAME-26SEP05MOHMIA-MOH",
            "title": "Miami (OH) wins",
            "yes_sub_title": "Miami (OH)",
        }

        self.assertEqual("miami oh redhawks", sports.infer_market_side(game, market))

    def test_college_side_identity_requires_candidate_to_match_contract_side(self):
        candidate = {
            "sport_key": "americanfootball_ncaaf",
            "market_type": "moneyline",
            "home_team": "Texas Longhorns",
            "away_team": "Texas State Bobcats",
            "selected_team": "Texas Longhorns",
            "order_side": "yes",
        }
        market = {
            "title": "Texas St. wins",
            "yes_sub_title": "Texas St.",
        }

        wrong = sports.college_market_side_identity_review(candidate, market)
        right = sports.college_market_side_identity_review(
            {**candidate, "selected_team": "Texas State Bobcats"},
            market,
        )
        inverse = sports.college_market_side_identity_review(
            {**candidate, "order_side": "no"},
            market,
        )

        self.assertFalse(wrong["matched"])
        self.assertTrue(right["matched"])
        self.assertTrue(inverse["matched"])

    def test_college_basketball_identity_rejects_state_and_tech_collision(self):
        market = {
            "ticker": "KXNCAAMBGAME-26NOV10KANTXT-TXT",
            "rules_primary": (
                "If Texas Tech wins in the Kansas vs Texas Tech men's college basketball "
                "game, then the market resolves to Yes."
            ),
        }
        wrong_game = {
            "sport_key": "basketball_ncaab",
            "home_team": "Kansas State Wildcats",
            "away_team": "Texas Longhorns",
        }
        correct_game = {
            "sport_key": "basketball_ncaab",
            "home_team": "Texas Tech Red Raiders",
            "away_team": "Kansas Jayhawks",
        }

        wrong_score, wrong_debug = sports.match_game_to_market(wrong_game, market)
        correct_score, _correct_debug = sports.match_game_to_market(correct_game, market)

        self.assertEqual(0, wrong_score)
        self.assertFalse(wrong_debug["college_event_identity"]["matched"])
        self.assertEqual(sports.SPORTS_MIN_MATCH_SCORE, correct_score)

    def test_college_identity_rejects_same_schools_in_wrong_sport(self):
        football_game = {
            "sport_key": "americanfootball_ncaaf",
            "home_team": "Duke Blue Devils",
            "away_team": "Kansas Jayhawks",
        }
        basketball_market = {
            "series_ticker": "KXNCAAMBGAME",
            "ticker": "KXNCAAMBGAME-26NOV07KANDKE-DKE",
            "rules_primary": (
                "If Duke wins the Kansas vs Duke men's college basketball game, "
                "then the market resolves to Yes."
            ),
        }

        score, debug = sports.match_game_to_market(football_game, basketball_market)

        self.assertEqual(0, score)
        identity = debug["college_event_identity"]
        self.assertTrue(identity["detected"])
        self.assertFalse(identity["sport_matched"])
        self.assertEqual("basketball", identity["contract_sport"])
        self.assertEqual("football", identity["expected_contract_sport"])

    def test_college_school_alias_replacement_preserves_provider_mascot(self):
        self.assertEqual(
            "louisiana monroe warhawks",
            sports.capper_cfb_school_text("UL Monroe Warhawks"),
        )
        self.assertGreater(
            sports.capper_cfb_name_match_score("Louisiana-Monroe", "UL Monroe Warhawks"),
            0,
        )

    def test_doubleheader_contract_matches_only_corresponding_provider_game(self):
        game_one = {
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-17T17:36:00Z",
        }
        game_two = {
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-17T23:11:00Z",
        }
        market_one = {
            "event_ticker": "KXMLBGAME-26JUL171335TBBOSG1",
            "ticker": "KXMLBGAME-26JUL171335TBBOSG1-BOS",
        }
        market_two = {
            "event_ticker": "KXMLBGAME-26JUL171910TBBOSG2",
            "ticker": "KXMLBGAME-26JUL171910TBBOSG2-BOS",
        }

        self.assertTrue(sports.doubleheader_start_matches_game(game_one, market_one))
        self.assertFalse(sports.doubleheader_start_matches_game(game_one, market_two))
        self.assertFalse(sports.doubleheader_start_matches_game(game_two, market_one))
        self.assertTrue(sports.doubleheader_start_matches_game(game_two, market_two))

    def test_baseball_start_guard_rejects_mismatch_without_explicit_game_marker(self):
        game = {
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-17T17:36:00Z",
        }
        market = {
            "event_ticker": "KXMLBGAME-26JUL171910TBBOS",
            "ticker": "KXMLBGAME-26JUL171910TBBOS-BOS",
        }
        self.assertFalse(sports.doubleheader_start_matches_game(game, market))

    def test_pirates_later_contract_cannot_match_live_earlier_game(self):
        live_game = {
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-18T17:11:00Z",
        }
        later_game = {
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-18T20:10:00Z",
        }
        later_market = {
            "ticker": "KXMLBGAME-26JUL181610PITCLE-PIT",
            "event_ticker": "KXMLBGAME-26JUL181610PITCLE",
        }
        self.assertFalse(sports.doubleheader_start_matches_game(live_game, later_market))
        self.assertTrue(sports.doubleheader_start_matches_game(later_game, later_market))

    def test_final_live_order_guard_rejects_pirates_cross_game_match(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-18T17:11:00Z",
            "game_started": True,
            "kalshi_ticker": "KXMLBGAME-26JUL181610PITCLE-PIT",
        }
        review = sports.live_order_start_guard(
            candidate,
            now=datetime(2026, 7, 18, 18, 34, tzinfo=timezone.utc),
        )
        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "sports_game_start_time_mismatch")
        self.assertEqual(review["difference_minutes"], 179.0)

    def test_final_live_order_guard_rejects_provider_game_not_live(self):
        review = sports.live_order_start_guard({
            "sport_key": "basketball_wnba",
            "commence_time": "2026-07-18T23:00:00Z",
            "game_started": False,
            "kalshi_ticker": "KXWNBAGAME-26JUL181900TEAMTEAM-TEAM",
        })
        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "sports_provider_game_not_live")

    def test_final_live_order_guard_rejects_college_contract_identity_mismatch(self):
        candidate = {
            "sport_key": "americanfootball_ncaaf",
            "home_team": "Georgia Tech Yellow Jackets",
            "away_team": "Tennessee Volunteers",
            "commence_time": "2026-09-12T23:00:00Z",
            "game_started": True,
            "kalshi_ticker": "KXNCAAFGAME-26SEP12SAMTNTC-TNTC",
        }
        current_market = {
            "ticker": candidate["kalshi_ticker"],
            "rules_primary": (
                "If Tennessee Tech wins the Samford vs Tennessee Tech college football "
                "game, then the market resolves to Yes."
            ),
        }

        with patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=current_market):
            review = sports.live_order_start_guard(candidate)

        self.assertFalse(review["ok"])
        self.assertEqual("sports_college_event_identity_mismatch", review["error"])

    def test_final_live_order_guard_rejects_college_contract_side_mismatch(self):
        candidate = {
            "sport_key": "americanfootball_ncaaf",
            "market_type": "moneyline",
            "home_team": "Texas Longhorns",
            "away_team": "Texas State Bobcats",
            "selected_team": "Texas Longhorns",
            "order_side": "yes",
            "commence_time": "2026-09-05T19:30:00Z",
            "game_started": True,
            "kalshi_ticker": "KXNCAAFGAME-26SEP05TXSTTEX-TXST",
        }
        current_market = {
            "ticker": candidate["kalshi_ticker"],
            "title": "Texas St. wins",
            "yes_sub_title": "Texas St.",
            "rules_primary": (
                "If Texas St. wins the Texas St. vs Texas college football game, "
                "then the market resolves to Yes."
            ),
        }

        with patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=current_market):
            review = sports.live_order_start_guard(candidate)

        self.assertFalse(review["ok"])
        self.assertEqual("sports_college_side_identity_mismatch", review["error"])

    def test_final_live_order_guard_allows_tagged_near_start_pregame(self):
        now = datetime.now(timezone.utc)
        with patch.object(sports, "SPORTS_PREGAME_ENABLED", True), patch.object(
            sports, "SPORTS_PREGAME_MAX_MINUTES_BEFORE_START", 45
        ):
            review = sports.live_order_start_guard({
                "sport_key": "basketball_wnba",
                "commence_time": (now + timedelta(minutes=20)).isoformat(),
                "game_started": False,
                "pregame_eligible": True,
                "kalshi_ticker": "TEST",
            }, now=now)
        self.assertTrue(review["ok"])
        self.assertTrue(review["pregame"])

    def test_phase_two_late_start_does_not_block_independent_edge_bet(self):
        with patch.object(sports, "SPORTS_PHASE_TWO_CONTROL_EDGE_BETS", False), patch.object(
            sports, "SPORTS_PHASE_TWO_PAUSE_NORMAL_AFTER_TARGET", False
        ):
            self.assertFalse(sports.phase_two_skip_blocks_normal_bet("phase_two_reserved_late_start"))
        with patch.object(sports, "SPORTS_PHASE_TWO_CONTROL_EDGE_BETS", True):
            self.assertTrue(sports.phase_two_skip_blocks_normal_bet("phase_two_reserved_late_start"))

    def test_live_exposure_preflight_blocks_before_paid_ai_review(self):
        portfolio = {"bets": []}
        candidate = {"recovery_staking": {}}
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(
            sports, "live_daily_loss", return_value=0.0
        ), patch.object(sports, "effective_live_daily_loss_cap", return_value=100.0), patch.object(
            sports, "live_exposure", return_value=120.0
        ), patch.object(sports, "effective_live_max_stake", return_value=5.0), patch.object(
            sports, "effective_live_open_exposure_cap", return_value=100.0
        ), patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""
        ), patch.object(sports, "load_live_order_intents", return_value={"intents": []}):
            reason = sports.live_execution_preflight_skip_reason(portfolio, candidate, 5.0)
        self.assertEqual(reason, "live_open_exposure_cap")

    def test_team_concentration_cap_applies_across_different_markets(self):
        portfolio = {
            "balance": 900,
            "bets": [{
                "mode": "live",
                "status": "open",
                "stake": 90,
                "sport_key": "baseball_mlb",
                "game_key": "game-1",
                "home_team": "New York Yankees",
                "away_team": "Boston Red Sox",
            }],
        }
        candidate = {
            "sport_key": "baseball_mlb",
            "game_key": "game-2",
            "selected_team": "New York Yankees",
            "home_team": "New York Yankees",
            "away_team": "Toronto Blue Jays",
        }
        with patch.object(sports, "SPORTS_MAX_GAME_EXPOSURE_PCT", 1), patch.object(
            sports, "SPORTS_MAX_TEAM_EXPOSURE_PCT", 0.10
        ), patch.object(sports, "SPORTS_MAX_SPORT_EXPOSURE_PCT", 1):
            review = sports.live_group_exposure_review(portfolio, candidate, 20)
        self.assertFalse(review["ok"])
        self.assertIn("team", review["failures"])

    def test_all_available_recovery_cannot_bypass_global_exposure_preflight(self):
        portfolio = {"bets": []}
        candidate = {"recovery_staking": {"all_available_cash": True}}
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(
            sports, "live_daily_loss", return_value=0.0
        ), patch.object(sports, "effective_live_daily_loss_cap", return_value=100.0), patch.object(
            sports, "live_exposure", return_value=120.0
        ), patch.object(sports, "effective_live_max_stake", return_value=5.0), patch.object(
            sports, "effective_live_open_exposure_cap", return_value=100.0
        ), patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""
        ), patch.object(sports, "load_live_order_intents", return_value={"intents": []}):
            reason = sports.live_execution_preflight_skip_reason(portfolio, candidate, 5.0)
        self.assertEqual(reason, "live_open_exposure_cap")

    def test_reconciliation_preflight_blocks_unverified_or_unmatched_positions(self):
        now = datetime.now(timezone.utc).isoformat()
        with patch.object(
            sports,
            "load_json_file",
            return_value={
                "generated_at": now,
                "account": {"ok": False, "positions_error": "timeout"},
            },
        ):
            self.assertEqual(
                sports.live_reconciliation_preflight_skip_reason(),
                "live_account_reconciliation_unavailable",
            )
        with patch.object(
            sports,
            "load_json_file",
            return_value={
                "generated_at": now,
                "account": {"ok": True},
                "unmatched_remote_tickers": ["REMOTE-ONLY"],
            },
        ):
            self.assertEqual(
                sports.live_reconciliation_preflight_skip_reason(),
                "live_position_reconciliation_unmatched",
            )

    def test_manual_import_records_only_same_ticker_position_delta_once(self):
        ticker = "KXMLBTOTAL-26JUL171910TBBOSG2-9"
        portfolio = {
            "bets": [{
                "mode": "live",
                "status": "open",
                "kalshi_ticker": ticker,
                "order_side": "no",
                "stake": 12.0,
                "contracts": 25.0,
                "source": "edge_scanner",
            }]
        }
        account = {
            "positions_sample": [{
                "ticker": ticker,
                "position_fp": "-108.78",
                "total_traded_dollars": "50.5388",
                "fees_paid_dollars": "1.8937",
                "last_updated_ts": "2026-07-17T20:29:52Z",
            }]
        }
        with patch.object(sports, "fetch_kalshi_market_by_ticker", return_value={"title": "Rays vs Red Sox total"}), patch.object(
            sports, "log_line"
        ), patch.object(sports, "append_jsonl"):
            imported = sports.import_manual_live_positions(portfolio, account)
            imported_again = sports.import_manual_live_positions(portfolio, account)
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["strategy_owner"], "user_bet")
        self.assertEqual(imported[0]["contracts"], 83.78)
        self.assertEqual(imported[0]["stake"], 38.54)
        self.assertTrue(imported[0]["remote_delta_import"])
        self.assertEqual(imported[0]["remote_position"]["total_traded_dollars"], "38.5400")
        self.assertEqual(imported[0]["remote_position"]["fees_paid_dollars"], "1.893700")
        self.assertEqual(imported_again, [])

    def test_manual_import_adds_sports_parlay_to_user_exposure(self):
        ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026095AFB734BE-5A4AB63D1B8"
        portfolio = {"bets": []}
        account = {
            "positions_sample": [{
                "ticker": ticker,
                "position_fp": "488.75",
                "market_exposure_dollars": "241.4425",
                "fees_paid_dollars": "8.5519",
                "last_updated_ts": "2026-07-31T15:20:52Z",
            }]
        }
        with patch.object(
            sports,
            "fetch_kalshi_market_by_ticker",
            return_value={"title": "Sports multigame parlay"},
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            imported = sports.import_manual_live_positions(portfolio, account)

        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["market_type"], "parlay")
        self.assertEqual(imported[0]["strategy_owner"], "user_bet")
        self.assertEqual(imported[0]["stake"], 241.44)
        self.assertEqual(sports.live_exposure(portfolio), 241.44)

    def test_live_exposure_includes_pending_and_resting_orders(self):
        portfolio = {"bets": [
            {"mode": "live", "status": "open", "stake": 10},
            {"mode": "live", "status": "pending", "stake": 20},
            {"mode": "live", "status": "resting", "stake": 30},
            {"mode": "paper", "status": "open", "stake": 40},
            {"mode": "live", "status": "settled", "stake": 50},
        ]}
        self.assertEqual(sports.live_exposure(portfolio), 60)

    def test_user_cashout_reconciliation_records_realized_profit_and_removes_exposure(self):
        ticker = "KXWTAMATCH-26AUG02BIRGOL-BIR"
        bet = {
            "mode": "live",
            "status": "open",
            "strategy_owner": "user_bet",
            "source": "user_manual",
            "user_bet": True,
            "kalshi_ticker": ticker,
            "order_side": "yes",
            "stake": 290.05,
            "contracts": 568.72,
            "entry_price": 51.0,
            "remote_position": {
                "realized_pnl_dollars": "0.000000",
                "fees_paid_dollars": "9.948700",
            },
        }
        portfolio = {"balance": 1000.0, "bets": [bet], "history": []}
        account = {"ok": True, "positions_sample": []}
        closed_position = {
            "ticker": ticker,
            "position_fp": "0.00",
            "realized_pnl_dollars": "40.000000",
            "fees_paid_dollars": "12.000000",
            "last_updated_ts": "2026-08-02T20:00:00Z",
        }
        with patch.object(
            sports,
            "fetch_total_traded_position",
            return_value={"ok": True, "position": closed_position},
        ), patch.object(
            sports,
            "fetch_position_fills",
            return_value={
                "ok": True,
                "fills": [{
                    "fill_id": "cashout-fill",
                    "ticker": ticker,
                    "action": "sell",
                    "side": "yes",
                    "count_fp": "568.72",
                    "yes_price_dollars": "0.5800",
                    "fee_cost": "2.051300",
                }],
            },
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            closed = sports.reconcile_user_cashed_out_positions(portfolio, account)

        self.assertEqual(len(closed), 1)
        self.assertEqual(portfolio["bets"], [])
        self.assertEqual(len(portfolio["history"]), 1)
        self.assertTrue(closed[0]["cashed_out"])
        self.assertEqual(closed[0]["close_reason"], "user_cashout_detected")
        self.assertEqual(closed[0]["result"], "WIN")
        self.assertEqual(closed[0]["gross_profit"], 39.81)
        self.assertEqual(closed[0]["fee"], 12.0)
        self.assertEqual(closed[0]["profit"], 27.81)
        self.assertEqual(closed[0]["cashout_proceeds"], 329.86)
        self.assertEqual(portfolio["balance"], 1000.0)

    def test_missing_bot_position_without_sell_fill_is_not_marked_cashed_out(self):
        portfolio = {
            "bets": [{
                "mode": "live",
                "status": "open",
                "strategy_owner": "live_campaign",
                "kalshi_ticker": "KXWTAMATCH-BOT",
                "stake": 15.0,
                "contracts": 30.0,
            }],
            "history": [],
        }
        with patch.object(
            sports,
            "fetch_total_traded_position",
            return_value={"ok": True, "position": {"ticker": "KXWTAMATCH-BOT", "position_fp": "0"}},
        ), patch.object(
            sports,
            "fetch_position_fills",
            return_value={"ok": True, "fills": []},
        ):
            closed = sports.reconcile_user_cashed_out_positions(
                portfolio,
                {"ok": True, "positions_sample": []},
            )
        self.assertEqual(closed, [])
        self.assertEqual(len(portfolio["bets"]), 1)

    def test_user_manual_sell_of_bot_bet_is_reconciled_but_never_submitted_by_bot(self):
        ticker = "KXWTAMATCH-26AUG02BIRGOL-GOL"
        portfolio = {
            "bets": [{
                "mode": "live",
                "status": "open",
                "strategy_owner": "live_campaign",
                "kalshi_ticker": ticker,
                "order_side": "yes",
                "stake": 29.76,
                "contracts": 93.0,
                "entry_price": 32.0,
            }],
            "history": [],
        }
        with patch.object(
            sports,
            "fetch_total_traded_position",
            return_value={"ok": True, "position": {"ticker": ticker, "position_fp": "0", "last_updated_ts": "2026-08-02T20:00:00Z"}},
        ), patch.object(
            sports,
            "fetch_position_fills",
            return_value={"ok": True, "fills": [{
                "fill_id": "manual-sell",
                "action": "sell",
                # Kalshi V2 may encode a YES-position cashout sell with the
                # opposite side while yes_price_dollars remains authoritative.
                "side": "no",
                "count_fp": "93.00",
                "yes_price_dollars": "0.4500",
                "fee_cost": "1.0000",
            }]},
        ), patch.object(sports, "kalshi_private_request") as submit, patch.object(
            sports, "log_line"
        ), patch.object(sports, "append_jsonl"):
            closed = sports.reconcile_user_cashed_out_positions(
                portfolio,
                {"ok": True, "positions_sample": []},
            )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["strategy_owner"], "live_campaign")
        self.assertEqual(closed[0]["cashout_initiator"], "user")
        self.assertEqual(closed[0]["result"], "WIN")
        self.assertEqual(closed[0]["profit"], 11.09)
        self.assertEqual(portfolio["bets"], [])
        submit.assert_not_called()

    @patch.object(sports, "EXECUTION_MODE", "live")
    @patch.object(sports, "KALSHI_API_KEY", "fixture-key")
    @patch.object(sports, "KALSHI_PRIVATE_KEY_PATH", "fixture-only.pem")
    @patch.object(sports, "SPORTS_RECONCILE_LIVE_ON_SCAN", True)
    def test_reconciliation_excludes_non_sports_remote_position(self):
        account = {
            "ok": True,
            "cash_balance": 100.0,
            "positions_sample": [{
                "ticker": "KXMVEOTHER-TEST",
                "position_fp": "10.00",
                "market_exposure_dollars": "4.25",
            }],
        }
        portfolio = {"balance": 100.0, "bets": []}
        with patch.object(sports, "fetch_live_account_snapshot", return_value=account), patch.object(
            sports, "live_readiness_report", return_value={}
        ), patch.object(
            sports, "import_manual_live_positions", return_value=[]
        ), patch.object(sports, "write_json"), patch.object(sports, "log_line"):
            report = sports.reconcile_live_account(portfolio)

        self.assertEqual(report["unmatched_remote_tickers"], [])
        self.assertEqual(report["remote_open_count"], 0)
        self.assertEqual(report["remote_account_open_count"], 1)
        self.assertEqual(report["remote_market_exposure"], 0)
        self.assertEqual(report["remote_excluded_non_sports_count"], 1)
        self.assertEqual(report["remote_excluded_non_sports_tickers"], ["KXMVEOTHER-TEST"])

    @patch.object(sports, "EXECUTION_MODE", "live")
    @patch.object(sports, "KALSHI_API_KEY", "fixture-key")
    @patch.object(sports, "KALSHI_PRIVATE_KEY_PATH", "fixture-only.pem")
    @patch.object(sports, "SPORTS_RECONCILE_LIVE_ON_SCAN", True)
    def test_reconciliation_only_surfaces_unmatched_sports_positions(self):
        account = {
            "ok": True,
            "cash_balance": 100.0,
            "positions_sample": [
                {
                    "ticker": "KXMLBGAME-TEST",
                    "position_fp": "10.00",
                    "market_exposure_dollars": "4.25",
                },
                {
                    "ticker": "KXHIGHNY-TEST",
                    "position_fp": "8.00",
                    "market_exposure_dollars": "3.00",
                },
            ],
        }
        portfolio = {"balance": 100.0, "bets": []}
        with patch.object(sports, "fetch_live_account_snapshot", return_value=account), patch.object(
            sports, "live_readiness_report", return_value={}
        ), patch.object(
            sports, "import_manual_live_positions", return_value=[]
        ), patch.object(sports, "write_json"), patch.object(sports, "log_line"):
            report = sports.reconcile_live_account(portfolio)

        self.assertEqual(report["unmatched_remote_tickers"], ["KXMLBGAME-TEST"])
        self.assertEqual(report["remote_sports_tickers"], ["KXMLBGAME-TEST"])
        self.assertEqual(report["remote_open_count"], 1)
        self.assertEqual(report["remote_account_open_count"], 2)
        self.assertEqual(report["remote_market_exposure"], 4.25)
        self.assertEqual(report["remote_excluded_non_sports_tickers"], ["KXHIGHNY-TEST"])

    @patch.object(sports, "EXECUTION_MODE", "live")
    @patch.object(sports, "KALSHI_API_KEY", "fixture-key")
    @patch.object(sports, "KALSHI_PRIVATE_KEY_PATH", "fixture-only.pem")
    @patch.object(sports, "SPORTS_RECONCILE_LIVE_ON_SCAN", True)
    def test_reconciliation_keeps_locally_owned_dynamic_sports_ticker(self):
        ticker = "KXDYNAMICSPORT-TEST"
        account = {
            "ok": True,
            "cash_balance": 95.0,
            "positions_sample": [{"ticker": ticker, "position_fp": "5.00"}],
        }
        portfolio = {
            "balance": 95.0,
            "bets": [{
                "mode": "live",
                "status": "open",
                "kalshi_ticker": ticker,
                "contracts": 5,
                "stake": 5.0,
            }],
        }
        with patch.object(sports, "fetch_live_account_snapshot", return_value=account), patch.object(
            sports, "live_readiness_report", return_value={}
        ), patch.object(sports, "write_json"), patch.object(sports, "log_line"):
            report = sports.reconcile_live_account(portfolio)

        self.assertEqual(report["unmatched_local_tickers"], [])
        self.assertEqual(report["unmatched_remote_tickers"], [])
        self.assertEqual(report["remote_sports_tickers"], [ticker])

    def test_manual_results_are_excluded_from_strategy_controls(self):
        now = datetime.now(sports.LOCAL_TZ).isoformat()
        portfolio = {
            "starting_balance": 100.0,
            "balance": 50.0,
            "history": [
                {
                    "mode": "live",
                    "status": "settled",
                    "settled_at": now,
                    "result": "LOSS",
                    "strategy_owner": "small_edge",
                    "stake": 5.0,
                    "profit": -5.0,
                },
                {
                    "mode": "live",
                    "status": "settled",
                    "settled_at": now,
                    "result": "LOSS",
                    "strategy_owner": "user_bet",
                    "source": "user_manual",
                    "stake": 200.0,
                    "profit": -200.0,
                },
            ],
        }
        self.assertEqual(sports.daily_realized_profit(portfolio), -5.0)
        self.assertEqual(sports.rolling_recovery_profit(portfolio), -5.0)
        self.assertEqual(sports.live_daily_loss(portfolio), 5.0)
        self.assertEqual(sports.daily_loss_streak(portfolio), 1)
        self.assertEqual(sports.realized_bankroll_stats(portfolio)["realized_balance"], 95.0)

    def test_forced_risk_closes_do_not_create_a_strategy_loss_streak(self):
        now = datetime.now(sports.LOCAL_TZ).isoformat()
        portfolio = {
            "history": [
                {
                    "status": "settled",
                    "result": "LOSS",
                    "stake": 2.0,
                    "profit": -2.0,
                    "settled_at": now,
                },
                {
                    "status": "settled",
                    "result": "LOSS",
                    "stake": 10.0,
                    "profit": -1.0,
                    "settled_at": now,
                    "closed_early": True,
                    "close_reason": "invalid_market_model_mismatch",
                },
            ]
        }
        self.assertEqual(sports.daily_loss_streak(portfolio), 1)
        self.assertEqual(sports.all_time_loss_streak(portfolio), 1)

    def test_fok_recheck_resubmits_full_size_only_at_same_or_better_price(self):
        class Stream:
            def snapshot(self, _ticker, max_age_seconds=None):
                return {
                    "fresh": True,
                    "yes_ask": 31,
                    "yes_ask_size": 233,
                }

        first = {
            "ok": False,
            "error": "live_order_not_filled",
            "price": 31,
            "request": {"count": 233},
        }
        filled = {"ok": True, "price": 31, "actual_stake": 72.23}
        candidate = {
            "kalshi_ticker": "TEST-FOK",
            "market_type": "moneyline",
            "selected_team": "Test Team",
            "order_side": "yes",
            "entry_price": 31,
            "live_campaign": {"active": True},
        }
        with (
            patch.object(sports, "SPORTS_FOK_RECHECK_ENABLED", True),
            patch.object(sports, "SPORTS_FOK_RECHECK_ATTEMPTS", 1),
            patch.object(sports, "SPORTS_FOK_RECHECK_INTERVAL_SECONDS", 0),
            patch.object(sports, "SPORTS_FOK_RECHECK_MAX_WAIT_SECONDS", 30),
            patch.object(sports, "sports_stream", return_value=Stream()),
            patch.object(sports, "live_execution_preflight_skip_reason", return_value=""),
            patch.object(sports, "place_live_kalshi_order", side_effect=[first, filled]) as submit,
            patch.object(sports, "log_line"),
        ):
            result = sports.place_live_kalshi_order_with_retry(candidate, 72.23, portfolio={"bets": []})

        self.assertTrue(result["ok"])
        self.assertEqual(2, submit.call_count)
        retry_candidate = submit.call_args_list[1].args[0]
        self.assertEqual(31, retry_candidate["fok_recheck_max_price_cents"])
        self.assertEqual(233, retry_candidate["fok_recheck_required_contracts"])
        self.assertEqual("same_or_better_price_full_depth", result["fok_recheck"]["mode"])

    def test_fok_recheck_does_not_submit_when_full_depth_is_missing(self):
        class Stream:
            def snapshot(self, _ticker, max_age_seconds=None):
                return {"fresh": True, "yes_ask": 30, "yes_ask_size": 232}

        first = {
            "ok": False,
            "error": "live_order_not_filled",
            "price": 31,
            "request": {"count": 233},
        }
        candidate = {
            "kalshi_ticker": "TEST-FOK",
            "market_type": "moneyline",
            "selected_team": "Test Team",
            "order_side": "yes",
            "entry_price": 31,
            "live_campaign": {"active": True},
        }
        with (
            patch.object(sports, "SPORTS_FOK_RECHECK_ENABLED", True),
            patch.object(sports, "SPORTS_FOK_RECHECK_ATTEMPTS", 1),
            patch.object(sports, "SPORTS_FOK_RECHECK_INTERVAL_SECONDS", 0),
            patch.object(sports, "SPORTS_FOK_RECHECK_MAX_WAIT_SECONDS", 30),
            patch.object(sports, "sports_stream", return_value=Stream()),
            patch.object(sports, "place_live_kalshi_order", return_value=first) as submit,
            patch.object(sports, "log_line"),
        ):
            result = sports.place_live_kalshi_order_with_retry(candidate, 72.23, portfolio={"bets": []})

        self.assertFalse(result["ok"])
        self.assertEqual(1, submit.call_count)
        self.assertTrue(result["fok_recheck"]["exhausted"])

    def test_transient_clv_identity_retries_with_stored_entry_identity(self):
        bet = {
            "status": "open",
            "kalshi_ticker": "TOTAL-RETRY",
            "market_type": "total",
            "market_line": 9.5,
            "order_side": "no",
            "total_side": "under",
            "clv_identity": {
                "ticker": "TOTAL-RETRY",
                "market_type": "total",
                "market_line": 8.5,
                "order_side": "no",
                "total_side": "under",
            },
            "pricing_v2": {"book_probability": 55.0},
            "fixed_horizon_clv": {"5m": {
                "on_time": True,
                "captured_at": datetime.now().astimezone().isoformat(),
            }},
        }
        portfolio = {"bets": [bet]}
        self.assertTrue(sports.update_open_bet_consensus_clv(portfolio, []))
        mark = bet["fixed_horizon_clv"]["5m"]
        self.assertTrue(mark["book_identity_pending"])
        self.assertIsNone(mark["book_identity_valid"])

        candidate = {
            "kalshi_ticker": "TOTAL-RETRY",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {
                "ok": True,
                "book_probability": 58.0,
                "consensus": {"independent_family_count": 3, "observations": [{"last_update": datetime.now().astimezone().isoformat()}]},
            },
        }
        self.assertTrue(sports.update_open_bet_consensus_clv(portfolio, [candidate]))
        self.assertTrue(mark["book_identity_valid"])
        self.assertFalse(mark["book_identity_pending"])
        self.assertEqual(3.0, mark["book_move_pp"])
        self.assertEqual(8.5, mark["market_identity"]["market_line"])

    def test_consensus_clv_uses_recent_verified_snapshot_when_candidate_temporarily_missing(self):
        captured_at = datetime.now().astimezone().isoformat()
        identity_review = {
            "provider_updates": [captured_at],
            "ok": True,
            "reason": "matched",
            "normalized_book_probability": 58.0,
            "independent_book_families": 3,
        }
        bet = {
            "status": "open",
            "kalshi_ticker": "TOTAL-CACHED",
            "market_type": "total",
            "market_line": 8.5,
            "order_side": "no",
            "total_side": "under",
            "pricing_v2": {"book_probability": 55.0},
            "latest_verified_book_consensus": {
                "captured_at": captured_at,
                "book_identity": identity_review,
                "book_probability": 58.0,
                "independent_book_families": 3,
            },
            "fixed_horizon_clv": {"5m": {"captured_at": captured_at}},
        }
        with patch.object(sports, "SPORTS_CLV_BOOK_SNAPSHOT_MAX_AGE_MINUTES", 3):
            self.assertTrue(sports.update_open_bet_consensus_clv({"bets": [bet]}, []))
        mark = bet["fixed_horizon_clv"]["5m"]
        self.assertTrue(mark["book_identity_valid"])
        self.assertEqual("matched_recent_verified_snapshot", mark["book_identity_reason"])
        self.assertEqual(3.0, mark["book_move_pp"])

    def test_spread_quality_uses_eighty_point_final_score_floor(self):
        candidate = {"market_type": "spread", "confidence_score": 90.0}
        pro_review = {"score": 100.0}

        below = sports.spread_quality_review(
            candidate,
            pro_review=pro_review,
            final_review={"score": 79.9},
        )
        at_floor = sports.spread_quality_review(
            candidate,
            pro_review=pro_review,
            final_review={"score": 80.0},
        )

        self.assertEqual(80.0, sports.SPORTS_SPREAD_MIN_FINAL_SCORE)
        self.assertIn("spread_final_score_too_low", below["failures"])
        self.assertTrue(at_floor["ok"])

    def test_tennis_identity_normalization_handles_diacritics(self):
        self.assertEqual(sports.normalize_text("Iva Jović"), "iva jovic")
        game = {
            "home_team": "Iva Jović",
            "away_team": "Marie Bouzkova",
        }
        market = {
            "ticker": "KXWTAMATCH-26AUG18BOUJOV-BOU",
            "title": "Iva Jovic vs Marie Bouzkova: will Marie Bouzkova win?",
            "yes_sub_title": "Marie Bouzkova",
        }
        score, debug = sports.match_game_to_market(game, market)
        self.assertEqual(2, score)
        self.assertTrue(debug["home_hits"])
        self.assertEqual("marie bouzkova", sports.infer_market_side(game, market))

    def test_matchup_local_city_aliases_resolve_wnba_contract_sides(self):
        game = {
            "home_team": "Las Vegas Aces",
            "away_team": "Atlanta Dream",
        }
        las_vegas_market = {
            "ticker": "KXWNBASPREAD-26AUG18ATLLV-LV4",
            "title": "Atlanta at Las Vegas: Las Vegas wins by over 3.5 points?",
            "yes_sub_title": "Las Vegas",
        }
        atlanta_market = {
            "ticker": "KXWNBAGAME-26AUG18ATLLV-ATL",
            "title": "Atlanta at Las Vegas: Atlanta wins?",
            "yes_sub_title": "Atlanta",
        }

        score, debug = sports.match_game_to_market(game, las_vegas_market)
        self.assertEqual(2, score)
        self.assertTrue(debug["home_hits"])
        self.assertTrue(debug["away_hits"])
        self.assertEqual("las vegas aces", sports.infer_market_side(game, las_vegas_market))
        self.assertEqual("atlanta dream", sports.infer_market_side(game, atlanta_market))

    def test_wnba_spread_materializes_direct_and_inverse_no_exposures(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        game = {
            "id": "atl-las-vegas",
            "sport_key": "basketball_wnba",
            "sport_title": "WNBA",
            "home_team": "Las Vegas Aces",
            "away_team": "Atlanta Dream",
            "commence_time": start,
            "bookmakers": [{
                "key": "pinnacle",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "markets": [{
                    "key": "spreads",
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "outcomes": [
                        {"name": "Las Vegas Aces", "point": -3.5, "price": -108},
                        {"name": "Atlanta Dream", "point": 3.5, "price": -102},
                    ],
                }],
            }],
        }
        market = {
            "ticker": "KXWNBASPREAD-26AUG18ATLLV-LV4",
            "event_ticker": "KXWNBASPREAD-26AUG18ATLLV",
            "series_ticker": "KXWNBASPREAD",
            "title": "Atlanta at Las Vegas: Las Vegas wins by over 3.5 points?",
            "yes_sub_title": "Las Vegas",
            "yes_bid": 50,
            "yes_ask": 52,
            "no_bid": 47,
            "no_ask": 49,
            "volume": 500,
            "liquidity": 500,
        }

        def pricing_unavailable(_game, candidate, _state):
            candidate["pricing_v2"] = {"ok": False, "reason": "test_pricing_disabled"}
            return candidate

        with patch.object(sports, "live_game_allowed", return_value=True), patch.object(
            sports, "is_supported_kalshi_series", return_value=True
        ), patch.object(sports, "market_matches_provider_sport", return_value=True), patch.object(
            sports, "market_date_matches_game", return_value=True
        ), patch.object(sports, "sports_stream", return_value=None), patch.object(
            sports, "apply_pricing_v2_to_candidate", side_effect=pricing_unavailable
        ):
            candidates = sports.evaluate_market_candidate_variants(
                game, market, calibration_state={}
            )

        self.assertEqual(2, len(candidates))
        by_side = {candidate["order_side"]: candidate for candidate in candidates}
        self.assertEqual("Las Vegas Aces", by_side["yes"]["selected_team"])
        self.assertEqual(-3.5, by_side["yes"]["market_line"])
        self.assertEqual(52, by_side["yes"]["entry_price"])
        self.assertEqual("Atlanta Dream", by_side["no"]["selected_team"])
        self.assertEqual(3.5, by_side["no"]["market_line"])
        self.assertEqual(49, by_side["no"]["entry_price"])
        self.assertEqual(
            "opponent_spread_via_no",
            by_side["no"]["equivalent_contract_review"]["equivalence"],
        )

    def test_cfb_exact_moneyline_spread_and_total_side_mapping(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        updated = datetime.now(timezone.utc).isoformat()
        game = {
            "id": "clemson-lsu",
            "sport_key": "americanfootball_ncaaf",
            "sport_title": "NCAAF",
            "home_team": "LSU Tigers",
            "away_team": "Clemson Tigers",
            "commence_time": start,
            "bookmakers": [{
                "key": "pinnacle",
                "last_update": updated,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": updated,
                        "outcomes": [
                            {"name": "LSU Tigers", "price": -380},
                            {"name": "Clemson Tigers", "price": 300},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": updated,
                        "outcomes": [
                            {"name": "LSU Tigers", "point": -10.5, "price": -110},
                            {"name": "Clemson Tigers", "point": 10.5, "price": -110},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": updated,
                        "outcomes": [
                            {"name": "Over", "point": 50.5, "price": -108},
                            {"name": "Under", "point": 50.5, "price": -112},
                        ],
                    },
                ],
            }],
        }
        moneyline = {
            "ticker": "KXNCAAFGAME-26SEP05CLELSU-LSU",
            "series_ticker": "KXNCAAFGAME",
            "title": "Clemson Tigers at LSU Tigers: LSU Tigers win?",
            "yes_sub_title": "LSU Tigers",
            "yes_bid": 76,
            "yes_ask": 78,
            "no_bid": 21,
            "no_ask": 23,
            "volume": 500,
            "liquidity": 500,
        }
        spread = {
            "ticker": "KXNCAAFSPREAD-26SEP05CLELSU-LSU11",
            "series_ticker": "KXNCAAFSPREAD",
            "title": "Clemson Tigers at LSU Tigers: LSU Tigers wins by over 10.5 points?",
            "yes_sub_title": "LSU Tigers",
            "yes_bid": 48,
            "yes_ask": 50,
            "no_bid": 49,
            "no_ask": 51,
            "volume": 500,
            "liquidity": 500,
        }
        total = {
            "ticker": "KXNCAAFTOTAL-26SEP05CLELSU-51",
            "series_ticker": "KXNCAAFTOTAL",
            "title": "Clemson Tigers at LSU Tigers: Over 50.5 total points?",
            "yes_sub_title": "Over 50.5",
            "yes_bid": 47,
            "yes_ask": 49,
            "no_bid": 50,
            "no_ask": 52,
            "volume": 500,
            "liquidity": 500,
        }

        def pricing_unavailable(_game, candidate, _state):
            candidate["pricing_v2"] = {"ok": False, "reason": "test_pricing_disabled"}
            return candidate

        with patch.object(sports, "live_game_allowed", return_value=True), patch.object(
            sports, "is_supported_kalshi_series", return_value=True
        ), patch.object(sports, "market_matches_provider_sport", return_value=True), patch.object(
            sports, "market_date_matches_game", return_value=True
        ), patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=pricing_unavailable):
            ml_yes = sports.evaluate_candidate(game, moneyline, order_side_override="yes")
            spread_yes = sports.evaluate_candidate(game, spread, order_side_override="yes")
            spread_no = sports.evaluate_candidate(game, spread, order_side_override="no")
            total_yes = sports.evaluate_candidate(game, total, order_side_override="yes")
            total_no = sports.evaluate_candidate(game, total, order_side_override="no")

        self.assertEqual(("LSU Tigers", "yes"), (ml_yes["selected_team"], ml_yes["order_side"]))
        self.assertEqual(("LSU Tigers", -10.5, "yes"), (
            spread_yes["selected_team"], spread_yes["market_line"], spread_yes["order_side"],
        ))
        self.assertEqual(("Clemson Tigers", 10.5, "no"), (
            spread_no["selected_team"], spread_no["market_line"], spread_no["order_side"],
        ))
        self.assertEqual(("over", 50.5, "yes"), (
            total_yes["total_side"], total_yes["market_line"], total_yes["order_side"],
        ))
        self.assertEqual(("under", 50.5, "no"), (
            total_no["total_side"], total_no["market_line"], total_no["order_side"],
        ))

    def test_opposite_capper_total_tickets_materialize_independent_routes(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        target_date = sports.parse_iso(start).astimezone(sports.LOCAL_TZ).date().isoformat()
        game = {
            "id": "clemson-lsu",
            "sport_key": "americanfootball_ncaaf",
            "home_team": "LSU Tigers",
            "away_team": "Clemson Tigers",
            "commence_time": start,
        }
        market = {
            "ticker": "KXNCAAFTOTAL-26SEP05CLELSU-51",
            "series_ticker": "KXNCAAFTOTAL",
            "title": "Clemson Tigers at LSU Tigers: Over 50.5 total points?",
        }

        def ticket(ticket_id, side):
            return {
                "ticket_id": ticket_id,
                "sport_key": "americanfootball_ncaaf",
                "pick_type": "straight",
                "posted_odds": -110,
                "target_event_date": target_date,
                "components": [{
                    "component_id": f"{ticket_id}-component",
                    "market_type": "total",
                    "selection": f"{side.title()} 50.5",
                    "total_side": side,
                    "market_line": 50.5,
                    "event_hint": "Clemson/LSU",
                }],
            }

        def evaluate(_game, _market, *_args, **kwargs):
            order_side = kwargs.get("order_side_override")
            total_side = "over" if order_side == "yes" else "under"
            return {
                "event_id": game["id"],
                "game_key": game["id"],
                "sport_key": game["sport_key"],
                "commence_time": start,
                "home_team": game["home_team"],
                "away_team": game["away_team"],
                "kalshi_ticker": market["ticker"],
                "market_type": "total",
                "market_line": 50.5,
                "selected_team": f"{total_side.title()} 50.5",
                "total_side": total_side,
                "order_side": order_side,
                "entry_price": 50,
                "edge": 3.0,
                "estimated_fee_edge_pp": 0.0,
                "kalshi_volume": 500,
            }

        with patch.object(sports, "build_candidate_market_index", return_value={}), patch.object(
            sports, "indexed_markets_for_game", return_value=[market]
        ), patch.object(sports, "evaluate_candidate", side_effect=evaluate), patch.object(
            sports, "assign_candidate_confidence"
        ):
            requested = sports.build_trusted_capper_requested_side_candidates(
                [ticket("over-ticket", "over"), ticket("under-ticket", "under")],
                [market],
                [game],
                calibration_state={},
            )

        self.assertEqual(
            {("over", "yes"), ("under", "no")},
            {(row["total_side"], row["order_side"]) for row in requested},
        )

    @patch.object(sports, "SPORTS_CAPPER_ENABLED", True)
    def test_saudi_capper_derivatives_resolve_exact_series_not_generic_soccer(self):
        target = (datetime.now(sports.LOCAL_TZ).date() + timedelta(days=1)).isoformat()
        tickets = [
            {
                "status": "unmatched", "executable": True, "sport_key": "soccer",
                "target_event_date": target,
                "components": [{"market_type": "btts"}],
                "schedule_snapshot": {"games": [{"sport_key": "soccer_saudi_arabia_pro_league"}]},
            },
            {
                "status": "unmatched", "executable": True, "sport_key": "soccer",
                "target_event_date": target,
                "components": [{"market_type": "total"}],
                "schedule_snapshot": {"games": [{"sport_key": "soccer_saudi_arabia_pro_league"}]},
            },
        ]
        meta = {
            "KXSOCCERBTTS": {"contract_key": "SOCCERBTTS"},
            "KXSAUDIPLBTTS": {"contract_key": "SOCCERBTTS"},
            "KXSAUDIPLTOTAL": {"contract_key": "SOCCERTOTAL"},
            "KXEPLBTTS": {"contract_key": "SOCCERBTTS"},
        }
        with patch.object(sports, "list_capper_tickets", return_value=tickets), patch.object(
            sports,
            "load_cached_active_sports",
            return_value=[{
                "key": "soccer_saudi_arabia_pro_league",
                "title": "Saudi Arabia - Pro League",
                "description": "Soccer",
            }],
        ), patch.object(
            sports, "load_dynamic_actionable_kalshi_series", return_value=set(meta)
        ), patch.object(sports, "_KALSHI_SPORTS_SERIES_META", meta):
            self.assertEqual(
                ("KXSAUDIPLBTTS",),
                sports.trusted_capper_requested_btts_series(),
            )
            self.assertEqual(
                ("KXSAUDIPLBTTS", "KXSAUDIPLTOTAL"),
                sports.trusted_capper_requested_soccer_derivative_series(),
            )

    def test_postponed_tennis_market_date_requires_exact_pair_and_delay_rule(self):
        game = {
            "sport_key": "tennis_atp_us_open",
            "home_team": "Francisco Comesana",
            "away_team": "Flavio Cobolli",
            "commence_time": "2026-09-01T15:00:00Z",
        }
        market = {
            "ticker": "KXATPMATCH-26AUG30COMCOB-COB",
            "title": "Flavio Cobolli wins",
            "rules_primary": "If Flavio Cobolli wins the Comesana vs Cobolli tennis match, this resolves Yes.",
            "rules_secondary": "If this match is postponed or delayed, it remains open for the rescheduled match.",
        }
        self.assertTrue(sports.market_date_matches_game(game, market))
        self.assertFalse(sports.market_date_matches_game({**game, "home_team": "Arthur Fery"}, market))
        self.assertFalse(sports.market_date_matches_game(game, {**market, "rules_secondary": "Standard rules apply."}))
        self.assertFalse(sports.market_date_matches_game({**game, "sport_key": "baseball_mlb"}, market))

    def test_exact_capper_soccer_derivative_does_not_expand_normal_allowlist(self):
        game = {"sport_key": "soccer_saudi_arabia_pro_league"}
        market = {"ticker": "KXSAUDIPLTOTAL-26SEP01HILAAS-3"}
        normal_diagnostics = Counter()
        capper_diagnostics = Counter()
        with patch.object(sports, "live_game_allowed", return_value=True), patch.object(
            sports, "is_supported_kalshi_series", return_value=False
        ), patch.object(
            sports, "inferred_market_series_ticker", return_value="KXSAUDIPLTOTAL"
        ), patch.object(
            sports,
            "trusted_capper_requested_soccer_derivative_series",
            return_value=("KXSAUDIPLTOTAL",),
        ), patch.object(sports, "market_matches_provider_sport", return_value=False):
            normal = sports.evaluate_candidate(game, market, diagnostics=normal_diagnostics)
            requested = sports.evaluate_candidate(
                game,
                market,
                diagnostics=capper_diagnostics,
                trusted_capper_requested_series=True,
            )

        self.assertIsNone(normal)
        self.assertIsNone(requested)
        self.assertEqual(1, normal_diagnostics["unsupported_kalshi_series"])
        self.assertEqual(0, capper_diagnostics["unsupported_kalshi_series"])
        self.assertEqual(1, capper_diagnostics["cross_sport_series"])

    def test_candidate_index_allows_exact_postponed_tennis_market(self):
        game = {
            "id": "postponed-cobolli",
            "sport_key": "tennis_atp_us_open",
            "home_team": "Francisco Comesana",
            "away_team": "Flavio Cobolli",
            "commence_time": "2026-09-01T15:00:00Z",
        }
        market = {
            "ticker": "KXATPMATCH-26AUG30COMCOB-COB",
            "title": "Flavio Cobolli wins",
            "rules_primary": "If Flavio Cobolli wins the Comesana vs Cobolli tennis match, this resolves Yes.",
            "rules_secondary": "If this match is postponed or delayed, it remains open for the rescheduled match.",
        }
        market_index = {
            "known": {
                "tennis_atp": {
                    "undated": [],
                    "dated": {date(2026, 8, 30): [market]},
                },
            },
            "unknown": [],
        }
        diagnostics = Counter()

        selected = sports.indexed_markets_for_game(market_index, game, diagnostics)

        self.assertEqual([market], selected)
        self.assertEqual(0, diagnostics["date_mismatch"])
        sports.indexed_markets_for_game(market_index, game, diagnostics)
        self.assertEqual(["tennis_atp"], market_index["provider_matches"]["tennis_atp_us_open"])

    def test_tennis_set_spread_is_not_priced_from_game_handicap_feed(self):
        game = {
            "id": "mensik-tirante",
            "sport_key": "tennis_atp",
            "sport_title": "ATP",
            "home_team": "Thiago Agustin Tirante",
            "away_team": "Jakub Mensik",
            "commence_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "bookmakers": [{
                "key": "pinnacle",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Jakub Mensik", "point": 1.5, "price": -110},
                        {"name": "Thiago Agustin Tirante", "point": -1.5, "price": -110},
                    ],
                }],
            }],
        }
        market = {
            "ticker": "KXATPSETSPREAD-TEST-TIR2",
            "series_ticker": "KXATPSETSPREAD",
            "title": "Tirante wins by over 1.5 sets?",
            "yes_sub_title": "Thiago Agustin Tirante",
            "yes_bid": 48,
            "yes_ask": 50,
            "no_bid": 49,
            "no_ask": 51,
        }
        diagnostics = Counter()

        with patch.object(sports, "live_game_allowed", return_value=True), patch.object(
            sports, "is_supported_kalshi_series", return_value=True
        ), patch.object(sports, "market_matches_provider_sport", return_value=True), patch.object(
            sports, "market_date_matches_game", return_value=True
        ):
            candidate = sports.evaluate_candidate(game, market, diagnostics=diagnostics)

        self.assertIsNone(candidate)
        self.assertEqual(1, diagnostics["set_spread_requires_same_unit_pricing"])

    def test_tennis_moneyline_selects_better_no_side_exposure(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        game = {
            "id": "tennis-event",
            "sport_key": "tennis_wta",
            "sport_title": "WTA",
            "home_team": "Iva Jović",
            "away_team": "Marie Bouzkova",
            "commence_time": start,
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "markets": [{
                        "key": "h2h",
                        "last_update": datetime.now(timezone.utc).isoformat(),
                        "outcomes": [
                            {"name": "Iva Jović", "price": 170},
                            {"name": "Marie Bouzkova", "price": -200},
                        ],
                    }],
                }
            ],
        }
        market = {
            "ticker": "KXWTAMATCH-26AUG18BOUJOV-BOU",
            "event_ticker": "KXWTAMATCH-26AUG18BOUJOV",
            "series_ticker": "KXWTAMATCH",
            "title": "Iva Jovic vs Marie Bouzkova: will Marie Bouzkova win?",
            "yes_sub_title": "Marie Bouzkova",
            "yes_bid": 68,
            "yes_ask": 70,
            "no_bid": 30,
            "no_ask": 32,
            "volume": 500,
            "liquidity": 500,
        }

        def pricing_unavailable(_game, candidate, _state):
            candidate["pricing_v2"] = {"ok": False, "reason": "test_pricing_disabled"}
            return candidate

        with patch.object(sports, "live_game_allowed", return_value=True), patch.object(
            sports, "is_supported_kalshi_series", return_value=True
        ), patch.object(sports, "market_matches_provider_sport", return_value=True), patch.object(
            sports, "market_date_matches_game", return_value=True
        ), patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=pricing_unavailable):
            candidate = sports.evaluate_candidate(game, market, calibration_state={})
            forced_yes = sports.evaluate_candidate(
                game,
                market,
                calibration_state={},
                order_side_override="yes",
            )
            ticket = {
                "ticket_id": "requested-side",
                "sport_key": "tennis",
                "pick_type": "straight",
                "posted_odds": -150,
                "target_event_date": sports.parse_iso(start).astimezone(sports.LOCAL_TZ).date().isoformat(),
                "components": [{
                    "component_id": "bouzkova-ml",
                    "market_type": "moneyline",
                    "selection": "Marie Bouzkova",
                }],
            }
            with patch.object(sports, "build_candidate_market_index", return_value={}), patch.object(
                sports, "indexed_markets_for_game", return_value=[market]
            ):
                requested = sports.build_trusted_capper_requested_side_candidates(
                    [ticket], [market], [game], calibration_state={}
                )
        self.assertIsNotNone(candidate)
        self.assertEqual("no", candidate["order_side"])
        self.assertEqual("Iva Jović", candidate["selected_team"])
        self.assertEqual(32, candidate["entry_price"])
        self.assertEqual("no", candidate["equivalent_contract_review"]["selected_order_side"])
        self.assertIsNotNone(forced_yes)
        self.assertEqual("yes", forced_yes["order_side"])
        self.assertEqual("Marie Bouzkova", forced_yes["selected_team"])
        self.assertEqual(70, forced_yes["entry_price"])
        self.assertEqual(1, len(requested))
        self.assertEqual("Marie Bouzkova", requested[0]["selected_team"])
        self.assertEqual("yes", requested[0]["order_side"])
        self.assertTrue(requested[0]["capper_requested_side_materialized"])
        self.assertIn("confidence_score", requested[0])
        quality = sports.capper_ticket_quality(
            ticket,
            [(ticket["components"][0], requested[0])],
            {},
        )
        self.assertEqual(requested[0]["confidence_score"], quality["confidence"])
        self.assertIsNotNone(quality["pro_score"])
        self.assertIsNotNone(quality["final_score"])

    def test_equivalent_tennis_contracts_are_deduplicated_to_best_route(self):
        game = {"id": "event-1", "sport_key": "tennis_atp"}
        weaker = {
            "event_id": "event-1",
            "sport_key": "tennis_atp",
            "market_type": "moneyline",
            "selected_team": "Player One",
            "kalshi_ticker": "YES-ONE",
            "order_side": "yes",
            "entry_price": 45,
            "edge": 2,
            "pricing_v2": {"ok": True},
        }
        better = {
            **weaker,
            "kalshi_ticker": "NO-TWO",
            "order_side": "no",
            "entry_price": 42,
            "edge": 5,
        }
        pairs = sports.dedupe_equivalent_candidate_pairs([
            (game, {}, weaker),
            (game, {}, better),
        ])
        self.assertEqual(1, len(pairs))
        self.assertEqual("NO-TWO", pairs[0][2]["kalshi_ticker"])
        self.assertEqual(2, pairs[0][2]["equivalent_contract_review"]["equivalent_contract_count"])

    def test_full_event_schedule_is_not_replaced_by_partial_odds_rows(self):
        existing = {
            "sports": {
                "tennis_atp": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": "events",
                    "games": [
                        {"id": "event-1", "commence_time": "2026-08-18T18:00:00Z"},
                        {"id": "event-2", "commence_time": "2026-08-18T19:00:00Z"},
                    ],
                }
            }
        }
        with patch.object(sports, "SPORTS_SCHEDULE_GATE_ENABLED", True), patch.object(
            sports, "load_schedule_cache", return_value=existing
        ), patch.object(sports, "save_schedule_cache") as save_mock:
            sports.update_schedule_cache_for_sport("tennis_atp", [{
                "id": "event-1",
                "sport_key": "tennis_atp",
                "home_team": "One",
                "away_team": "Two",
                "commence_time": "2026-08-18T18:00:00Z",
            }])
        saved = save_mock.call_args.args[0]["sports"]["tennis_atp"]
        self.assertEqual("events", saved["source"])
        self.assertEqual({"event-1", "event-2"}, {row["id"] for row in saved["games"]})

    def test_tennis_sharp_discovery_unions_primary_missing_events(self):
        start = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        primary = {
            "id": "event-1", "sport_key": "tennis_atp", "home_team": "A",
            "away_team": "B", "commence_time": start, "bookmakers": [],
        }
        discovered = {
            "id": "event-2", "sport_key": "tennis_atp", "home_team": "C",
            "away_team": "D", "commence_time": start, "bookmakers": [],
        }
        headers = {"x-requests-remaining": "999", "x-requests-last": "1", "_polymaker_odds_key_index": 0}
        with patch.object(sports, "ODDS_API_KEYS", ("test-key",)), patch.object(
            sports, "ODDS_API_KEY", "test-key"
        ), patch.object(sports, "SPORTS_SCHEDULE_GATE_ENABLED", False), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED", False
        ), patch.object(sports, "SPORTS_TENNIS_SHARP_DISCOVERY_ENABLED", True), patch.object(
            sports, "SPORTS_TENNIS_SHARP_BOOKMAKERS", ("pinnacle", "betfair_ex_eu")
        ), patch.object(sports, "expand_sport_keys", return_value=["tennis_atp"]), patch.object(
            sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}
        ), patch.object(sports, "can_spend_odds_credits", return_value=(True, {"monthly_credits_used": 0}, 0)), patch.object(
            sports, "odds_pause_until", return_value={}
        ), patch.object(sports, "odds_api_get_json", side_effect=[([primary], headers), ([discovered], headers)]) as get_mock, patch.object(
            sports, "record_odds_spend", return_value={"daily_credits_used": 1, "rolling_credits_used": 1, "monthly_credits_used": 1}
        ), patch.object(sports, "save_odds_cache_state"), patch.object(
            sports, "update_schedule_cache_for_sport"
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            games = sports.fetch_odds_games(
                ["tennis_atp"],
                requested_market_types={"tennis_atp": ["moneyline"]},
                requested_event_ids_by_sport={"tennis_atp": ["event-1", "event-2"]},
                include_scores=False,
            )
        self.assertEqual({"event-1", "event-2"}, {row["id"] for row in games})
        self.assertEqual("event-1,event-2", get_mock.call_args_list[1].kwargs["params"]["eventIds"])
        self.assertNotIn("regions", get_mock.call_args_list[1].kwargs["params"])

    def test_tennis_sharp_discovery_skips_when_primary_coverage_is_sufficient(self):
        start = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        primary = {
            "id": "event-1", "sport_key": "tennis_atp", "home_team": "A",
            "away_team": "B", "commence_time": start,
            "bookmakers": [{"key": key, "markets": []} for key in ("a", "b", "c")],
        }
        headers = {
            "x-requests-remaining": "999", "x-requests-last": "1",
            "_polymaker_odds_key_index": 0,
        }
        with patch.object(sports, "ODDS_API_KEYS", ("test-key",)), patch.object(
            sports, "ODDS_API_KEY", "test-key"
        ), patch.object(sports, "SPORTS_SCHEDULE_GATE_ENABLED", False), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED", False
        ), patch.object(sports, "SPORTS_TENNIS_SHARP_DISCOVERY_ENABLED", True), patch.object(
            sports, "SPORTS_TENNIS_SHARP_MIN_PRIMARY_BOOKMAKERS", 3
        ), patch.object(sports, "SPORTS_TENNIS_SHARP_BOOKMAKERS", ("pinnacle",)), patch.object(
            sports, "expand_sport_keys", return_value=["tennis_atp"]
        ), patch.object(sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}), patch.object(
            sports, "can_spend_odds_credits", return_value=(True, {"monthly_credits_used": 0}, 0)
        ), patch.object(sports, "odds_pause_until", return_value={}), patch.object(
            sports, "odds_api_get_json", return_value=([primary], headers)
        ) as get_mock, patch.object(
            sports, "record_odds_spend",
            return_value={"daily_credits_used": 1, "rolling_credits_used": 1, "monthly_credits_used": 1},
        ), patch.object(sports, "save_odds_cache_state"), patch.object(
            sports, "update_schedule_cache_for_sport"
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            games = sports.fetch_odds_games(
                ["tennis_atp"],
                requested_event_ids_by_sport={"tennis_atp": ["event-1"]},
                include_scores=False,
            )
        self.assertEqual(["event-1"], [row["id"] for row in games])
        self.assertEqual(1, get_mock.call_count)

    def test_sharp_discovery_repair_prevents_redundant_secondary_region_call(self):
        start = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        primary = {
            "id": "npb-event", "sport_key": "baseball_npb", "home_team": "A",
            "away_team": "B", "commence_time": start, "bookmakers": [],
        }
        discovered = {
            **primary,
            "bookmakers": [{"key": key, "markets": []} for key in ("pinnacle", "betfair", "matchbook")],
        }
        headers = {
            "x-requests-remaining": "999", "x-requests-last": "1",
            "_polymaker_odds_key_index": 0,
        }
        with patch.object(sports, "ODDS_API_KEYS", ("test-key",)), patch.object(
            sports, "ODDS_API_KEY", "test-key"
        ), patch.object(sports, "SPORTS_SCHEDULE_GATE_ENABLED", False), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED", True
        ), patch.object(sports, "SPORTS_ODDS_SECONDARY_REGIONS", "us2,uk,eu"), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS", 3
        ), patch.object(sports, "SPORTS_OVERSEAS_SHARP_DISCOVERY_ENABLED", True), patch.object(
            sports, "SPORTS_OVERSEAS_SHARP_BOOKMAKERS", ("pinnacle", "betfair", "matchbook")
        ), patch.object(sports, "expand_sport_keys", return_value=["baseball_npb"]), patch.object(
            sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}
        ), patch.object(sports, "can_spend_odds_credits", return_value=(True, {"monthly_credits_used": 0}, 0)), patch.object(
            sports, "odds_pause_until", return_value={}
        ), patch.object(sports, "odds_api_get_json", side_effect=[([primary], headers), ([discovered], headers)]) as get_mock, patch.object(
            sports, "record_odds_spend",
            return_value={"daily_credits_used": 1, "rolling_credits_used": 1, "monthly_credits_used": 1},
        ), patch.object(sports, "save_odds_cache_state"), patch.object(
            sports, "update_schedule_cache_for_sport"
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            sports.fetch_odds_games(
                ["baseball_npb"],
                requested_event_ids_by_sport={"baseball_npb": ["npb-event"]},
                include_scores=False,
            )
        self.assertEqual(2, get_mock.call_count)

    def test_overseas_sharp_discovery_is_scoped_to_actionable_events(self):
        start = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        event = {
            "id": "npb-event", "sport_key": "baseball_npb",
            "home_team": "Hanshin Tigers", "away_team": "Yomiuri Giants",
            "commence_time": start, "bookmakers": [],
        }
        headers = {
            "x-requests-remaining": "999", "x-requests-last": "1",
            "_polymaker_odds_key_index": 0,
        }
        with patch.object(sports, "ODDS_API_KEYS", ("test-key",)), patch.object(
            sports, "ODDS_API_KEY", "test-key"
        ), patch.object(sports, "SPORTS_SCHEDULE_GATE_ENABLED", False), patch.object(
            sports, "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED", False
        ), patch.object(sports, "SPORTS_OVERSEAS_SHARP_DISCOVERY_ENABLED", True), patch.object(
            sports, "SPORTS_OVERSEAS_SHARP_BOOKMAKERS", ("pinnacle", "betfair_ex_eu")
        ), patch.object(sports, "expand_sport_keys", return_value=["baseball_npb"]), patch.object(
            sports, "load_odds_cache_state", return_value={"version": 2, "sports": {}}
        ), patch.object(
            sports, "can_spend_odds_credits", return_value=(True, {"monthly_credits_used": 0}, 0)
        ), patch.object(sports, "odds_pause_until", return_value={}), patch.object(
            sports, "odds_api_get_json", side_effect=[([event], headers), ([event], headers)]
        ) as get_mock, patch.object(
            sports, "record_odds_spend",
            return_value={"daily_credits_used": 1, "rolling_credits_used": 1, "monthly_credits_used": 1},
        ), patch.object(sports, "save_odds_cache_state"), patch.object(
            sports, "update_schedule_cache_for_sport"
        ), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"):
            games = sports.fetch_odds_games(
                ["baseball_npb"],
                requested_market_types={"baseball_npb": ["moneyline", "total"]},
                requested_event_ids_by_sport={"baseball_npb": ["npb-event"]},
                include_scores=False,
            )
        self.assertEqual(["npb-event"], [row["id"] for row in games])
        discovery_params = get_mock.call_args_list[1].kwargs["params"]
        self.assertEqual("npb-event", discovery_params["eventIds"])
        self.assertEqual("pinnacle,betfair_ex_eu", discovery_params["bookmakers"])
        self.assertEqual("h2h", discovery_params["markets"])
        self.assertNotIn("regions", discovery_params)

    def test_prematch_anchor_is_reused_for_live_tennis_shadow(self):
        with TemporaryDirectory() as tmp, patch.object(
            sports, "SPORTS_PREMATCH_ANCHORS_FILE", str(Path(tmp) / "anchors.json")
        ), patch.object(sports, "SPORTS_PREMATCH_ANCHORS_STATE", None), patch.object(
            sports, "SPORTS_PREMATCH_ANCHORS_DIRTY", False
        ):
            pregame = {
                "event_id": "event-1", "game_key": "game-1", "sport_key": "tennis_wta",
                "market_type": "moneyline", "selected_team": "Iva Jović", "game_started": False,
                "commence_time": "2026-08-18T18:00:00Z", "independent_book_family_count": 3,
                "pricing_v2": {"ok": True, "independent_outcome_probability": 57.25},
            }
            sports.update_candidate_prematch_anchor(pregame)
            self.assertTrue(sports.save_prematch_anchors_if_dirty())
            live = {**pregame, "game_started": True, "pricing_v2": {"ok": False}}
            sports.update_candidate_prematch_anchor(live)
        self.assertEqual(57.25, live["pregame_probability"])
        self.assertTrue(live["prematch_anchor"]["available"])

    def test_external_consensus_edge_is_separate_and_skip_reason_is_precise(self):
        candidate = {
            "entry_price": 52,
            "edge": -0.2,
            "estimated_fee_edge_pp": 0.2,
            "skip_reasons": ["below_edge"],
            "pricing_v2": {
                "ok": True,
                "independent_outcome_probability": 53,
                "estimated_fee_edge_pp": 0.2,
                "consensus": {"uncertainty_pp": 2, "independent_family_count": 3, "raw_book_count": 5},
            },
        }
        shadow = sports.apply_external_consensus_shadow(candidate)
        sports.apply_precise_skip_reasons(candidate)
        self.assertEqual(1, shadow["raw_edge_pp"])
        self.assertFalse(shadow["affects_execution"])
        self.assertIn("external_edge_not_confirmed_by_execution_blend", candidate["display_skip_reasons"])

    def test_tennis_derivative_shadow_fetches_only_near_actionable_events(self):
        game = {"id": "event-1", "sport_key": "tennis_atp", "home_team": "One", "away_team": "Two"}
        candidate = {
            "sport_key": "tennis_atp", "market_type": "moneyline", "selected_team": "One",
            "event_id": "event-1", "edge": 0, "game_started": False, "game_completed": False,
            "pricing_v2": {"ok": True}, "external_consensus_shadow": {"raw_edge_pp": 0.2},
        }
        derivative = {
            **game,
            "bookmakers": [{
                "key": "pinnacle",
                "markets": [
                    {"key": "spreads", "outcomes": [{"name": "One", "point": -2.5, "price": -110}]},
                    {"key": "totals", "outcomes": [{"name": "Over", "point": 22.5, "price": -110}, {"name": "Under", "point": 22.5, "price": -110}]},
                ],
            }],
        }
        headers = {"x-requests-last": "2", "x-requests-remaining": "900"}
        with patch.object(sports, "SPORTS_TENNIS_DERIVATIVE_ODDS_CACHE", {}), patch.object(
            sports, "SPORTS_TENNIS_DERIVATIVE_CACHE_LOADED", True
        ), patch.object(
            sports, "SPORTS_TENNIS_DERIVATIVE_SHADOW_ENABLED", True
        ), patch.object(sports, "SPORTS_TENNIS_DERIVATIVE_MAX_EVENTS_PER_SCAN", 2), patch.object(
            sports, "SPORTS_TENNIS_SHARP_BOOKMAKERS", ("pinnacle",)
        ), patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}
        ), patch.object(sports, "odds_api_get_json", return_value=(derivative, headers)), patch.object(
            sports, "record_odds_spend"
        ), patch.object(sports, "write_json"), patch.object(
            sports, "append_jsonl"
        ), patch.object(sports, "log_line"):
            merged, status = sports.enrich_with_on_demand_tennis_derivatives(
                [game], [(game, {}, candidate)]
            )
        features = sports.tennis_derivative_shadow_features(merged[0], candidate)
        self.assertEqual(1, status["requested_events"])
        self.assertTrue(features["available"])
        self.assertEqual(-2.5, features["selected_player_spread"])
        self.assertEqual(22.5, features["match_total_games"])
        self.assertFalse(features["affects_execution"])

    def test_tennis_derivative_shadow_pauses_during_budget_pressure(self):
        game = {"id": "event-1", "sport_key": "tennis_atp"}
        candidate = {
            "sport_key": "tennis_atp", "market_type": "moneyline",
            "game_completed": False, "pricing_v2": {"ok": True},
            "external_consensus_shadow": {"raw_edge_pp": 1.0},
        }
        with patch.object(sports, "SPORTS_TENNIS_DERIVATIVE_SHADOW_ENABLED", True), patch.object(
            sports, "SPORTS_TENNIS_DERIVATIVE_MAX_EVENTS_PER_SCAN", 2
        ), patch.object(sports, "SPORTS_TENNIS_SHARP_BOOKMAKERS", ("pinnacle",)), patch.object(
            sports, "odds_paid_refresh_pacing", return_value={
                "paid_refresh_multiplier": 6.0,
                "odds_usage_pressure_mode": "critical",
                "shadow_paid_calls_allowed": False,
            }
        ), patch.object(sports, "odds_api_get_json") as get_mock, patch.object(
            sports, "append_jsonl"
        ):
            merged, status = sports.enrich_with_on_demand_tennis_derivatives(
                [game], [(game, {}, candidate)]
            )
        self.assertEqual([game], merged)
        self.assertTrue(status["budget_pressure_skip"])
        get_mock.assert_not_called()

    def test_live_tennis_game_total_series_is_supported_and_classified_exactly(self):
        market = {
            "event_ticker": "KXATPGTOTAL-26AUG20PAUCOB",
            "ticker": "KXATPGTOTAL-26AUG20PAUCOB-23",
            "title": "Over 22.5 games",
            "rules_primary": (
                "If the number of completed games in the full match is above 22.5 "
                "in the Tommy Paul vs Flavio Cobolli professional tennis match."
            ),
        }
        self.assertTrue(sports.is_supported_kalshi_series(market))
        self.assertTrue(sports.is_simple_market(market))
        self.assertEqual(
            {"type": "total", "side": "Over", "point": 22.5},
            sports.classify_kalshi_market(market),
        )
        self.assertEqual("game", sports.capper_line_unit(market))

    def test_tennis_total_catalog_contract_is_actionable_but_total_sets_is_not(self):
        self.assertTrue(sports.is_actionable_full_event_series({
            "title": "ATP Total Games",
            "contract_terms_url": "https://example/TENNISTOTALGAMES.pdf",
        }))
        self.assertFalse(sports.is_actionable_full_event_series({
            "title": "ATP Total Sets",
            "contract_terms_url": "https://example/TENNISTOTALSETS.pdf",
        }))

    def test_tennis_game_total_market_triggers_exact_alternate_total_scan(self):
        commence = datetime.now(timezone.utc) + timedelta(minutes=20)
        kalshi_day = commence.astimezone(sports.LOCAL_TZ).strftime("%y%b%d").upper()
        game = {
            "id": "paul-cobolli",
            "sport_key": "tennis_atp_cincinnati_open",
            "home_team": "Tommy Paul",
            "away_team": "Flavio Cobolli",
            "commence_time": commence.isoformat(),
            "bookmakers": [],
        }
        market = {
            "event_ticker": f"KXATPGTOTAL-{kalshi_day}PAUCOB",
            "ticker": f"KXATPGTOTAL-{kalshi_day}PAUCOB-23",
            "title": "Over 22.5 games",
            "rules_primary": "Tommy Paul vs Flavio Cobolli professional tennis match",
            "yes_bid": 49,
            "yes_ask": 50,
            "no_bid": 50,
            "no_ask": 51,
            "volume": 1000,
        }
        refreshed = {
            **game,
            "bookmakers": [{
                "key": "pinnacle",
                "markets": [{
                    "key": "alternate_totals",
                    "outcomes": [
                        {"name": "Over", "point": 22.5, "price": -110},
                        {"name": "Under", "point": 22.5, "price": -110},
                    ],
                }],
            }],
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_ENABLED", False),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED", True),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN", 8),
            patch.object(sports, "SPORTS_TENNIS_DERIVATIVE_MAX_EVENTS_PER_SCAN", 2),
            patch.object(sports, "SPORTS_ALTERNATE_ODDS_CACHE", {}),
            patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)),
            patch.object(sports, "odds_api_get_json", return_value=(refreshed, {})) as get_odds,
            patch.object(sports, "record_odds_spend") as spend_mock,
            patch.object(sports, "append_jsonl"),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}),
        ):
            merged, status = sports.enrich_with_on_demand_alternate_odds(
                [game], [], [market]
            )
        self.assertEqual(1, status["tennis_total_market_events"])
        self.assertEqual(1, status["tennis_total_selected_events"])
        self.assertEqual(1, status["requested_events"])
        self.assertEqual("alternate_totals", get_odds.call_args.kwargs["params"]["markets"])
        self.assertEqual("pinnacle", merged[0]["bookmakers"][0]["key"])

    def test_soccer_capper_derivatives_request_btts_and_standard_total(self):
        commence = datetime.now(timezone.utc) + timedelta(hours=1)
        target_date = commence.astimezone(sports.LOCAL_TZ).date().isoformat()
        game = {
            "id": "hilal-ahli",
            "sport_key": "soccer_saudi_arabia_pro_league",
            "home_team": "Al-Hilal",
            "away_team": "Al-Ahli",
            "commence_time": commence.isoformat(),
            "bookmakers": [],
        }
        tickets = [
            {
                "ticket_id": "btts",
                "sport_key": "soccer",
                "market_type": "btts",
                "target_event_date": target_date,
                "components": [{
                    "component_id": "btts-leg",
                    "market_type": "btts",
                    "selection": "Al Hilal/Al Ahli",
                    "btts_side": "yes",
                }],
            },
            {
                "ticket_id": "total",
                "sport_key": "soccer",
                "market_type": "total",
                "target_event_date": target_date,
                "components": [{
                    "component_id": "total-leg",
                    "market_type": "total",
                    "selection": "Over 2.5",
                    "event_hint": "Al Hilal/Al Ahli",
                    "total_side": "over",
                    "market_line": 2.5,
                }],
            },
        ]
        refreshed = {
            **game,
            "bookmakers": [{
                "key": "pinnacle",
                "markets": [
                    {"key": "btts", "outcomes": []},
                    {"key": "totals", "outcomes": []},
                ],
            }],
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_ENABLED", True),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED", True),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN", 8),
            patch.object(sports, "SPORTS_ALTERNATE_ODDS_CACHE", {}),
            patch.object(sports, "list_capper_tickets", return_value=tickets),
            patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)),
            patch.object(sports, "odds_api_get_json", return_value=(refreshed, {})) as get_odds,
            patch.object(sports, "record_odds_spend") as spend_mock,
            patch.object(sports, "append_jsonl"),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1.0}),
        ):
            _merged, status = sports.enrich_with_on_demand_alternate_odds([game], [], [])

        self.assertEqual(1, status["capper_priority_events"])
        requested_markets = [
            call.kwargs["params"]["markets"]
            for call in get_odds.call_args_list
        ]
        self.assertEqual("alternate_totals,btts", requested_markets[0])
        self.assertEqual("totals", requested_markets[-1])
        self.assertEqual(1, requested_markets.count("totals"))
        self.assertEqual(1, status["standard_total_fallback_requests"])
        self.assertTrue(spend_mock.call_args_list)
        self.assertTrue(all(
            call.kwargs.get("event_id") == "hilal-ahli"
            for call in spend_mock.call_args_list
        ))

    def test_alternate_tennis_totals_feed_the_exact_total_candidate_model(self):
        game = {
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "bookmakers": [{
                "key": "pinnacle",
                "markets": [{
                    "key": "alternate_totals",
                    "outcomes": [
                        {"name": "Over", "point": 22.5, "price": -110},
                        {"name": "Under", "point": 22.5, "price": -110},
                    ],
                }],
            }],
        }
        features = sports.get_game_market_features(game)
        row = sports.total_probability_model_row(game, features, "over", 22.5)
        self.assertEqual(22.5, features["best_totals"]["Over|22.5"]["point"])
        self.assertEqual("exact_line", row["line_model"])
        self.assertAlmostEqual(50.0, row["prob"], places=2)

    def test_tennis_total_line_ladder_interpolates_but_never_extrapolates(self):
        bookmakers = []
        for key in ("pinnacle", "draftkings", "fanduel"):
            bookmakers.append({
                "key": key,
                "markets": [{
                    "key": "alternate_totals",
                    "outcomes": [
                        {"name": "Over", "point": 21.5, "price": -150},
                        {"name": "Under", "point": 21.5, "price": 120},
                        {"name": "Over", "point": 23.5, "price": 120},
                        {"name": "Under", "point": 23.5, "price": -150},
                    ],
                }],
            })
        game = {
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "bookmakers": bookmakers,
        }
        features = sports.get_game_market_features(game)
        interpolated = sports.total_probability_model_row(game, features, "over", 22.5)
        extrapolated = sports.total_probability_model_row(game, features, "over", 24.5)
        self.assertEqual("same_book_monotone_interpolation", interpolated["line_model"])
        self.assertEqual(3, interpolated["line_ladder"]["interpolated_family_count"])
        self.assertIsNone(extrapolated)

    def test_mlb_live_spread_guard_accepts_fresh_exact_moderate_gap(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "game_started": True,
            "market_line": -2.5,
            "skip_reasons": [],
            "pricing_v2": {
                "ok": True,
                "independent_outcome_probability": 52.0,
                "kalshi_market_probability": 42.0,
                "consensus": {
                    "average_age_minutes": 0.8,
                    "observations": [
                        {"family": "sharp-a", "line_model": "exact_line", "interpolated": False, "last_update": datetime.now().astimezone().isoformat()},
                        {"family": "sharp-b", "line_model": "exact_line", "interpolated": False, "last_update": datetime.now().astimezone().isoformat()},
                    ],
                },
            },
        }

        review = sports.apply_mlb_live_spread_guard(candidate)

        self.assertTrue(review["ok"])
        self.assertEqual(2, review["exact_book_families"])
        self.assertEqual([], candidate["skip_reasons"])

    def test_mlb_live_spread_guard_rejects_stale_synthetic_extreme_gap_and_line(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "game_started": True,
            "market_line": -4.5,
            "skip_reasons": [],
            "pricing_v2": {
                "ok": True,
                "independent_outcome_probability": 60.0,
                "kalshi_market_probability": 40.0,
                "consensus": {
                    "average_age_minutes": 2.0,
                    "observations": [
                        {"family": "book-a", "line_model": "monotone_interpolation", "interpolated": True},
                        {"family": "book-b", "line_model": "exact_line", "interpolated": False},
                    ],
                },
            },
        }

        review = sports.apply_mlb_live_spread_guard(candidate)

        self.assertFalse(review["ok"])
        self.assertEqual(
            {
                "mlb_live_spread_exact_book_support_too_low",
                "mlb_live_spread_book_consensus_stale",
                "mlb_live_spread_extreme_book_kalshi_disagreement",
                "mlb_live_spread_line_too_extreme",
            },
            set(candidate["skip_reasons"]),
        )

    def test_mlb_live_spread_risk_caps_keep_standard_and_late_lines_active_at_smaller_size(self):
        standard = {
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "game_started": True,
            "market_line": -1.5,
            "bet_timing_bucket": "early_live",
            "game_state_features": {"authoritative_progress": True, "inning": 2},
        }
        late = {
            **standard,
            "market_line": -2.5,
            "bet_timing_bucket": "late_live",
            "game_state_features": {"authoritative_progress": True, "inning": 8},
        }

        standard_caps = sports.sports_unit_risk_caps(standard)
        late_caps = sports.sports_unit_risk_caps(late)

        self.assertEqual(
            sports.SPORTS_MLB_LIVE_SPREAD_STANDARD_LINE_MAX_UNITS,
            next(row for row in standard_caps if row["reason"] == "mlb_standard_run_line_variance_cap")["max_units"],
        )
        self.assertEqual(
            sports.SPORTS_MLB_LIVE_SPREAD_LATE_MAX_UNITS,
            next(row for row in late_caps if row["reason"] == "mlb_late_live_spread_variance_cap")["max_units"],
        )

    def test_every_mlb_live_spread_unit_requires_final_event_refresh(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "market_type": "spread",
            "game_started": True,
            "sports_units": {"raw_target_units": 0.5},
        }
        with patch.object(sports, "SPORTS_POST_AI_ODDS_REVALIDATION_ENABLED", True), patch.object(
            sports, "SPORTS_MLB_LIVE_SPREAD_FORCE_REVALIDATION", True
        ):
            self.assertTrue(sports.post_ai_revalidation_required(candidate))


if __name__ == "__main__":
    unittest.main()
