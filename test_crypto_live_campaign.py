import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_paper_bettor as crypto
from crypto_live_campaign import build_campaign_context, review_candidate


TODAY = "2026-07-18"


def row_date(_value=None):
    return TODAY


def qualified_learned_model():
    tier_policy = {
        str(units): {
            "markets": 300,
            "profit_per_contract_low_90": 0.02,
            "roi": 0.08,
            "calibration_gap": 0.01,
        }
        for units in range(1, 6)
    }
    return {
        "available": True,
        "active": True,
        "qualified_for_activation": True,
        "interval_qualified": True,
        "unique_markets": 500,
        "evaluation": {
            "market_weighted_market_baseline": {"brier": 0.20},
            "market_weighted_calibrated_ensemble": {"brier": 0.16},
            "after_fee_policy": {"roi": 0.08},
            "probability_tier_policy": tier_policy,
        },
    }


def candidate(price=50.0, minutes=7.0, edge=23.0, confidence=84.0, asset="BTC"):
    return {
        "ticker": f"KX{asset}15M-WINDOW",
        "event_ticker": f"KX{asset}15M-WINDOW",
        "series_ticker": f"KX{asset}15M",
        "asset": asset,
        "market_kind": "above",
        "is_15m_market": True,
        "minutes_to_close": minutes,
        "close_time": "2026-07-18T15:15:00Z",
        "entry_price": price,
        "edge": edge,
        "confidence": confidence,
        "side": "yes",
        "selected_side_probability": 95.0,
        "selected_side_probability_low": 90.0,
        "selected_side_probability_high": 99.0,
        "probability": {
            "prob": 95.0,
            "p_yes": 95.0,
            "p_low": 90.0,
            "p_high": 99.0,
            "market_implied_yes": 95.0,
            "interval_level": 0.90,
        },
        # Most tests in this module isolate ladder/recovery mechanics.  Give
        # those fixtures fully qualified model evidence; dedicated maturity
        # tests below exercise the production 1u cap separately.
        "learned_15m_model": qualified_learned_model(),
    }


class CryptoLiveCampaignTests(unittest.TestCase):
    def pilot_settings(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_SIMPLE_ACTION_GATE_ENABLED": "false",
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "2",
            "CRYPTO_15M_LOW_EDGE_PILOT_ENABLED": "true",
            "CRYPTO_15M_LOW_EDGE_PILOT_MIN_EDGE": "1",
            "CRYPTO_15M_LOW_EDGE_PILOT_MAX_EDGE": "3",
            "CRYPTO_15M_LOW_EDGE_PILOT_MIN_CONFIDENCE": "70",
            "CRYPTO_15M_LOW_EDGE_PILOT_MIN_PRICE_CENTS": "55",
            "CRYPTO_15M_LOW_EDGE_PILOT_MAX_PRICE_CENTS": "70",
            "CRYPTO_15M_LOW_EDGE_PILOT_MAX_OPEN": "1",
            "CRYPTO_15M_LOW_EDGE_PILOT_MAX_SETTLED": "50",
        })
        return settings

    def test_network_outage_backoff_starts_after_three_failed_scans(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_NETWORK_OUTAGE_FAILURE_THRESHOLD": "3",
            "CRYPTO_NETWORK_OUTAGE_BACKOFF_MINUTES": "5",
            "CRYPTO_NETWORK_OUTAGE_MAX_BACKOFF_MINUTES": "10",
        })
        self.assertEqual(
            crypto.crypto_network_outage_backoff_seconds(settings, 2),
            0.0,
        )
        self.assertEqual(
            crypto.crypto_network_outage_backoff_seconds(settings, 3),
            300.0,
        )
        self.assertEqual(
            crypto.crypto_network_outage_backoff_seconds(settings, 6),
            600.0,
        )

    def test_scan_capacity_tracks_bot_count_up_to_configured_limit(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_MAX_BETS_PER_SCAN": "5",
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
        })
        self.assertEqual(crypto.effective_scan_bet_limit(settings), 3)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "5"
        self.assertEqual(crypto.effective_scan_bet_limit(settings), 5)

    def test_simple_action_gate_is_one_uniform_quality_rule(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        portfolio = {"balance": 1000.0, "bets": []}

        accepted = candidate(price=40, edge=2, confidence=67, minutes=12)
        accepted_review = crypto.crypto_15m_campaign_lane_review(
            settings,
            portfolio,
            accepted,
            1,
        )
        self.assertTrue(accepted_review["eligible"])
        self.assertEqual(accepted_review["qualification_mode"], "simple_action_gate")
        self.assertEqual(accepted_review["action_gate"]["minimum_edge"], 2)
        self.assertEqual(accepted_review["action_gate"]["minimum_confidence"], 67)
        self.assertEqual(accepted_review["action_gate"]["minimum_price_cents"], 40)
        self.assertEqual(accepted_review["action_gate"]["maximum_price_cents"], 70)
        self.assertEqual(accepted_review["action_gate"]["minimum_minutes_remaining"], 2)
        self.assertEqual(accepted_review["action_gate"]["maximum_minutes_remaining"], 12)
        self.assertEqual(
            accepted["low_edge_pilot"]["reason"],
            "disabled_by_simple_action_gate",
        )

        for rejected, reason in (
            (candidate(price=39.99, edge=9, confidence=80), "campaign_price_range"),
            (candidate(price=50, edge=1.99, confidence=80), "campaign_quality_filter"),
            (candidate(price=50, edge=9, confidence=66.99), "campaign_quality_filter"),
            (candidate(price=50, edge=2, confidence=67, minutes=1.99), "campaign_entry_window"),
            (candidate(price=50, edge=2, confidence=67, minutes=12.01), "campaign_entry_window"),
        ):
            with self.subTest(reason=reason, candidate=rejected):
                review = crypto.crypto_15m_campaign_lane_review(
                    settings,
                    portfolio,
                    rejected,
                    1,
                )
                self.assertFalse(review["eligible"])
                self.assertEqual(review["reason"], reason)
                self.assertTrue(review["failed_checks"])
                self.assertIn("observed", review)
                self.assertIn("requirements", review)

        combined_failure = crypto.crypto_15m_campaign_lane_review(
            settings,
            portfolio,
            candidate(price=31, edge=3.9, confidence=60.8),
            1,
        )
        self.assertEqual(combined_failure["reason"], "campaign_price_range")
        self.assertEqual(
            {row["metric"] for row in combined_failure["failed_checks"]},
            {"price_cents", "confidence"},
        )

    def test_evidence_tiered_gate_applies_price_specific_time_and_confidence(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_TIERED_ACTION_GATE_ENABLED": "true",
            "CRYPTO_15M_SIMPLE_ACTION_GATE_ENABLED": "true",
        })
        portfolio = {"balance": 1000.0, "bets": []}

        low = candidate(price=40, edge=4, confidence=70, minutes=12)
        low_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, low, 1
        )
        self.assertTrue(low_review["eligible"])
        self.assertEqual(low_review["qualification_mode"], "evidence_tiered_gate")
        self.assertEqual(
            low_review["selected_price_tier"]["name"],
            "low_price_35_44",
        )
        self.assertEqual(low_review["recovery_mode"], "full")
        self.assertEqual(
            low["low_edge_pilot"]["reason"],
            "disabled_by_evidence_tiered_gate",
        )

        boundary = candidate(price=40, edge=4, confidence=70, minutes=2)
        boundary_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, boundary, 1
        )
        self.assertTrue(boundary_review["eligible"])

        too_early = candidate(price=40, edge=8, confidence=80, minutes=1.99)
        too_early_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, too_early, 1
        )
        self.assertFalse(too_early_review["eligible"])
        self.assertEqual(too_early_review["reason"], "campaign_entry_window")

        late_low = candidate(price=40, edge=8, confidence=80, minutes=12.01)
        late_low_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, late_low, 1
        )
        self.assertFalse(late_low_review["eligible"])
        self.assertEqual(late_low_review["reason"], "campaign_entry_window")

        mid = candidate(price=50, edge=4, confidence=70, minutes=12)
        mid_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, mid, 1
        )
        self.assertTrue(mid_review["eligible"])
        self.assertEqual(
            mid_review["selected_price_tier"]["name"],
            "mid_price_45_62",
        )

        weak_high = candidate(price=63, edge=4, confidence=71.99, minutes=7)
        weak_high_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, weak_high, 1
        )
        self.assertFalse(weak_high_review["eligible"])
        self.assertEqual(weak_high_review["reason"], "campaign_quality_filter")
        strong_high = candidate(price=63, edge=4, confidence=72, minutes=7)
        strong_high_review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, strong_high, 1
        )
        self.assertTrue(strong_high_review["eligible"])
        self.assertEqual(
            strong_high_review["selected_price_tier"]["name"],
            "high_price_63_70",
        )

    def test_evidence_tiered_gate_hard_blocks_model_disagreement_above_fifteen(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_TIERED_ACTION_GATE_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        accepted = candidate(price=50, edge=4, confidence=70, minutes=7)
        accepted["probability"] = {"model_disagreement": 15.0}
        self.assertTrue(
            crypto.crypto_15m_campaign_lane_review(
                settings, portfolio, accepted, 1
            )["eligible"]
        )

        blocked = candidate(price=50, edge=20, confidence=90, minutes=7)
        blocked["probability"] = {"model_disagreement": 15.01}
        review = crypto.crypto_15m_campaign_lane_review(
            settings, portfolio, blocked, 1
        )
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_model_disagreement_limit")
        self.assertEqual(review["applied_stake"], 0.0)

    def test_confidence_unit_ladder_sizes_one_through_five_units(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_UNIT_SIZE_PCT": "0.30",
        })
        portfolio = {"balance": 1000.0, "bets": []}
        cases = (
            (75, 2, -3, 1),
            (80, 3, -2, 2),
            (88, 4, -1, 3),
            (95, 5, 0.0001, 4),
            (98, 7, 2, 5),
        )
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            for confidence, edge, edge_low, expected_units in cases:
                row = candidate(edge=edge, confidence=confidence)
                row.update({
                    "expected_edge": edge,
                    "edge_low": edge_low,
                    "probability_net_edge_positive": confidence,
                    "data_quality": {"score": 0.99},
                })
                with self.subTest(units=expected_units):
                    review = crypto.crypto_15m_unit_candidate_review(
                        settings, portfolio, row
                    )
                    self.assertTrue(review["eligible"])
                    self.assertEqual(review["target_units"], expected_units)
                    self.assertEqual(review["unit_size"], 3.0)
                    self.assertEqual(review["requested_stake"], expected_units * 3.0)

    def test_unqualified_model_is_capped_at_one_unit(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(price=50, edge=12, confidence=99)
        row.update({
            "expected_edge": 12,
            "edge_low": 6,
            "probability_net_edge_positive": 99,
            "data_quality": {"score": 0.99},
            "learned_15m_model": {
                "available": True,
                "active": False,
                "interval_qualified": False,
                "unique_markets": 200,
            },
        })
        review = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["model_target_units"], 5)
        self.assertEqual(review["model_maturity_unit_cap"], 1)
        self.assertEqual(review["target_units"], 1)
        self.assertEqual(
            review["model_maturity"]["reason"],
            "model_not_walk_forward_qualified",
        )

    def test_two_unit_unlock_requires_independent_market_threshold(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(price=50, edge=12, confidence=99)
        row.update({
            "expected_edge": 12,
            "edge_low": 6,
            "probability_net_edge_positive": 99,
            "data_quality": {"score": 0.99},
        })
        row["learned_15m_model"]["evaluation"]["probability_tier_policy"]["2"]["markets"] = 49
        blocked = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertEqual(blocked["model_maturity_unit_cap"], 1)
        row["learned_15m_model"]["evaluation"]["probability_tier_policy"]["2"]["markets"] = 50
        unlocked = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertGreaterEqual(unlocked["model_maturity_unit_cap"], 2)
        self.assertGreaterEqual(unlocked["target_units"], 2)

    def test_live_probability_ladder_caps_high_edge_underdog_at_half_unit(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_UNIT_SIZE_PCT": "0.75",
            "CRYPTO_15M_UNIT_HALF_MIN_CONFIDENCE": "85",
            "CRYPTO_15M_UNIT_HALF_MIN_EDGE": "4",
            "CRYPTO_15M_UNIT_HALF_MIN_EDGE_LOW": "0.0001",
            "CRYPTO_15M_UNIT_1_MIN_CONFIDENCE": "80",
            "CRYPTO_15M_UNIT_1_MIN_EDGE_LOW": "-1",
        })
        row = candidate(price=40, edge=5, confidence=90)
        row.update({
            "expected_edge": 5,
            "edge_low": 1,
            "probability_net_edge_positive": 90,
            "selected_side_probability": 55,
            "selected_side_probability_low": 52,
            "selected_side_probability_high": 58,
            "probability": {
                "prob": 55,
                "p_yes": 55,
                "p_low": 52,
                "p_high": 58,
                "market_implied_yes": 40,
            },
            "data_quality": {"score": 0.99},
        })
        review = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["target_units"], 0.5)
        self.assertEqual(review["win_probability_unit_cap"], 0.5)
        self.assertEqual(review["requested_stake"], 3.75)
        self.assertAlmostEqual(review["metrics"]["win_probability"], 45.25)
        self.assertAlmostEqual(
            review["metrics"]["calibrated_win_probability"],
            45.25,
        )
        self.assertEqual(
            review["metrics"]["win_probability_interval"],
            {"low": 44.2, "high": 46.3, "level": 0.9},
        )
        self.assertFalse(
            review["probability_calibration"]["empirical_calibrator_active"]
        )
        self.assertEqual(
            review["probability_calibration"]["interval_status"],
            "estimated_model_interval_not_coverage_qualified",
        )

    def test_live_probability_ladder_rejects_high_edge_below_probability_floor(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(price=40, edge=8, confidence=99)
        row.update({
            "expected_edge": 8,
            "edge_low": 4,
            "probability_net_edge_positive": 99,
            "selected_side_probability": 50,
            "selected_side_probability_low": 48,
            "selected_side_probability_high": 52,
            "probability": {
                "prob": 50,
                "p_low": 48,
                "p_high": 52,
                "market_implied_yes": 40,
            },
            "data_quality": {"score": 0.99},
        })
        review = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "crypto_unit_win_probability_filter")
        self.assertEqual(review["target_units"], 0)

    def test_five_units_require_probability_edge_and_kelly_capacity(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_UNIT_5_MIN_EDGE_LOW": "3",
        })
        row = candidate(price=50, edge=8, confidence=99)
        row.update({
            "expected_edge": 8,
            "edge_low": 4,
            "probability_net_edge_positive": 99,
            "selected_side_probability": 85,
            "selected_side_probability_low": 78,
            "selected_side_probability_high": 90,
            "probability": {
                "prob": 85,
                "p_low": 78,
                "p_high": 90,
                "market_implied_yes": 80,
            },
            "data_quality": {"score": 0.99},
        })
        review = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertEqual(review["target_units"], 5)
        self.assertGreaterEqual(review["kelly_unit_cap"], 5)
        self.assertEqual(review["portfolio_unit_cap"], 5)

    def test_transient_penny_bankroll_snapshot_self_heals(self):
        portfolio = {
            "balance": 735.02,
            "bets": [],
            "crypto_15m_unit_bankroll": {
                "date": TODAY,
                "balance": 0.02,
                "locked": True,
            },
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            snapshot = crypto.crypto_15m_unit_bankroll_base(portfolio)
        self.assertEqual(snapshot["balance"], 735.02)
        self.assertTrue(snapshot["locked"])

    def test_default_crypto_unit_is_three_quarters_percent_of_locked_bankroll(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 3800.10, "bets": []}
        row = candidate(edge=2, confidence=75)
        row.update({
            "expected_edge": 2,
            "edge_low": -3,
            "probability_net_edge_positive": 75,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_unit_candidate_review(
                settings, portfolio, row
            )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["unit_size_pct"], 0.75)
        self.assertEqual(review["unit_size"], 28.5)
        self.assertEqual(review["requested_stake"], 28.5)

    def test_live_execution_cap_downshifts_without_weakening_model_tier(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        settings["CRYPTO_15M_UNIT_SIZE_PCT"] = "1.00"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(edge=7, confidence=98)
        row.update({
            "expected_edge": 7,
            "edge_low": 2,
            "probability_net_edge_positive": 98,
            "data_quality": {"score": 0.99},
            "execution_unit_cap": {
                "maximum_units": 2,
                "reason": "fresh_executable_depth",
            },
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_unit_candidate_review(
                settings, portfolio, row
            )

        self.assertTrue(review["eligible"])
        self.assertEqual(review["model_target_units"], 5)
        self.assertEqual(review["target_units"], 2)
        self.assertEqual(review["requested_stake"], 20.0)
        self.assertTrue(review["execution_downshifted"])
        self.assertEqual(review["passed_tiers"], [0.5, 1, 2, 3, 4, 5])

    def test_confidence_unit_bankroll_base_is_locked_for_the_day(self):
        portfolio = {"balance": 1000.0, "bets": []}
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            first = crypto.crypto_15m_unit_bankroll_base(portfolio)
            portfolio["balance"] = 1250.0
            second = crypto.crypto_15m_unit_bankroll_base(portfolio)
        self.assertEqual(first["balance"], 1000.0)
        self.assertEqual(second["balance"], 1000.0)
        self.assertTrue(second["locked"])

    def test_confidence_unit_mode_requires_fresh_complete_data(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(edge=7, confidence=99)
        row.update({
            "expected_edge": 7,
            "edge_low": 2,
            "probability_net_edge_positive": 99,
            "data_quality": {"score": 0.94},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_unit_candidate_review(
                settings, {"balance": 1000.0, "bets": []}, row
            )
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "crypto_unit_data_quality")
        self.assertEqual(review["requested_stake"], 0.0)

    def test_confidence_unit_mode_fails_closed_without_edge_interval(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(edge=7, confidence=99)
        row.update({
            "expected_edge": 7,
            "probability_net_edge_positive": 99,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_unit_candidate_review(
                settings, {"balance": 1000.0, "bets": []}, row
            )
        self.assertFalse(review["eligible"])
        self.assertFalse(review["metrics_available"])
        self.assertEqual(review["reason"], "crypto_unit_metrics_unavailable")

    def test_fixed_unit_stake_bypasses_completed_cycle_but_keeps_risk_limits(self):
        history = []
        for cycle, goal in enumerate((5.0, 2.5, 1.25), start=1):
            history.append({
                "status": "settled",
                "result": "WIN",
                "profit": goal,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": f"p-{cycle}",
                "settled_at": f"s-{cycle}",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            })
        review = review_candidate(
            {"balance": 1000.0, "history": history},
            candidate(price=50, edge=7, confidence=98),
            TODAY,
            row_date,
            fixed_stake=12.0,
            fixed_stake_metadata={"quality_tier": "4u"},
            cycle_completion_blocks=False,
            min_edge=2.0,
            min_confidence=75.0,
            mid_price_min_edge=2.0,
            mid_price_min_confidence=75.0,
        )
        self.assertTrue(review["eligible"])
        self.assertTrue(review["cycle_was_complete"])
        self.assertTrue(review["cycle_tracking_only"])
        self.assertEqual(review["sizing_mode"], "probability_edge_kelly_units")
        self.assertEqual(review["applied_stake"], 12.0)

    def test_fixed_unit_stake_rejects_sub_unit_daily_capacity(self):
        review = review_candidate(
            {"balance": 1000.0, "history": []},
            candidate(price=50, edge=23, confidence=84),
            TODAY,
            row_date,
            fixed_stake=20.0,
            fixed_stake_metadata={"quality_tier": "2u", "unit_size": 10.0},
            daily_loss_remaining_override=4.0,
        )

        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_unit_capacity")
        self.assertEqual(review["applied_contracts"], 0)
        self.assertFalse(review["full_unit_capacity"])
        self.assertEqual(review["minimum_unit_contracts"], 20)

    def test_bot_capacity_reset_excludes_only_pre_reset_rows(self):
        portfolio = {
            "crypto_15m_daily_capacity_resets": {
                "3": {"reset_at": "2026-08-15T09:00:00-05:00"},
            },
        }
        self.assertFalse(
            crypto.crypto_15m_daily_capacity_includes(
                portfolio,
                3,
                "2026-08-15T08:59:59-05:00",
            )
        )
        self.assertTrue(
            crypto.crypto_15m_daily_capacity_includes(
                portfolio,
                3,
                "2026-08-15T09:00:01-05:00",
            )
        )
        self.assertTrue(
            crypto.crypto_15m_daily_capacity_includes(
                portfolio,
                2,
                "2026-08-15T08:59:59-05:00",
            )
        )

    def test_continuous_high_water_ledger_carries_drawdown_across_settlements(self):
        migration_time = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        portfolio = {"balance": 1000.0, "bets": [], "history": []}
        initial = crypto.crypto_15m_high_water_drawdown_ledger(
            portfolio,
            1,
            initial_drawdown=5.0,
            reference_time=migration_time,
        )
        self.assertEqual(initial["current_profit"], -5.0)
        self.assertEqual(initial["outstanding_drawdown"], 5.0)

        def settled(row_id, minutes, result, profit):
            return {
                "id": row_id,
                "status": "settled",
                "result": result,
                "profit": profit,
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": 1,
                "settled_at": (migration_time + timedelta(minutes=minutes)).isoformat(),
            }

        portfolio["history"] = [
            settled("win-1", 1, "WIN", 3.0),
            settled("win-2", 2, "WIN", 4.0),
            settled("loss-1", 3, "LOSS", -1.0),
        ]
        ledger = crypto.crypto_15m_high_water_drawdown_ledger(
            portfolio,
            1,
            reference_time=migration_time + timedelta(minutes=4),
        )
        self.assertEqual(ledger["current_profit"], 1.0)
        self.assertEqual(ledger["high_water_profit"], 2.0)
        self.assertEqual(ledger["outstanding_drawdown"], 1.0)
        self.assertEqual(ledger["settled_since_migration"], 3)
        self.assertEqual(ledger["wins_since_migration"], 2)
        self.assertEqual(ledger["losses_since_migration"], 1)

    def test_pooled_high_water_ledger_offsets_results_across_bots(self):
        migration_time = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        portfolio = {"balance": 1000.0, "bets": [], "history": []}
        initial = crypto.crypto_15m_pooled_high_water_drawdown_ledger(
            portfolio,
            initial_drawdown=100.0,
            reference_time=migration_time,
        )
        self.assertEqual(initial["current_profit"], -100.0)
        self.assertEqual(initial["outstanding_drawdown"], 100.0)

        def settled(row_id, bot_number, minutes, result, profit):
            return {
                "id": row_id,
                "status": "settled",
                "result": result,
                "profit": profit,
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": bot_number,
                "settled_at": (migration_time + timedelta(minutes=minutes)).isoformat(),
            }

        portfolio["history"] = [
            settled("bot3-win", 3, 1, "WIN", 60.0),
            settled("bot2-loss", 2, 2, "LOSS", -10.0),
        ]
        ledger = crypto.crypto_15m_pooled_high_water_drawdown_ledger(
            portfolio,
            reference_time=migration_time + timedelta(minutes=3),
        )
        self.assertEqual(ledger["current_profit"], -50.0)
        self.assertEqual(ledger["high_water_profit"], 0.0)
        self.assertEqual(ledger["outstanding_drawdown"], 50.0)
        self.assertEqual(ledger["contributing_bot_numbers"], [2, 3])

    def test_continuous_lane_migrates_sum_of_bot_drawdowns_to_pool_once(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_CONTINUOUS_OPPORTUNITY_MODE": "true",
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
            "CRYPTO_15M_POOLED_RECOVERY_ENABLED": "true",
        })
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        portfolio = {
            "balance": 1000.0,
            "bets": [],
            "history": [],
            "crypto_15m_high_water_ledgers": {
                "1": {
                    "migration_cutoff": cutoff.isoformat(),
                    "baseline_drawdown": 100,
                },
                "2": {
                    "migration_cutoff": cutoff.isoformat(),
                    "baseline_drawdown": 0,
                },
                "3": {
                    "migration_cutoff": cutoff.isoformat(),
                    "baseline_drawdown": 50,
                },
            },
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            lane = crypto.crypto_15m_campaign_lane_context(settings, portfolio, 2)
            aggregate = crypto.crypto_15m_campaign_context(settings, portfolio)
        pooled = portfolio["crypto_15m_pooled_high_water_ledger"]
        self.assertEqual(pooled["baseline_drawdown"], 150.0)
        self.assertEqual(pooled["outstanding_drawdown"], 150.0)
        self.assertEqual(lane["outstanding_drawdown"], 0.0)
        self.assertEqual(lane["pooled_outstanding_drawdown"], 150.0)
        self.assertEqual(aggregate["outstanding_drawdown"], 150.0)
        self.assertEqual(aggregate["per_bot_outstanding_drawdown"], 150.0)

    def test_continuous_lane_removes_cycle_completion_and_schedule_mutation(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_CONTINUOUS_OPPORTUNITY_MODE": "true",
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
        })
        portfolio = {"balance": 1000.0, "bets": [], "history": []}
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            lane = crypto.crypto_15m_campaign_lane_context(settings, portfolio, 1)
            aggregate = crypto.crypto_15m_campaign_context(settings, portfolio)
        self.assertTrue(lane["continuous_opportunity_mode"])
        self.assertFalse(lane["cycle_workflow_enabled"])
        self.assertFalse(lane["cycle_completion_blocks"])
        self.assertFalse(lane["cycle_tracking_only"])
        self.assertEqual(lane["cycle_number"], 0)
        self.assertEqual(lane["cycle_goals"], [])
        self.assertNotIn("crypto_15m_campaign_schedules", portfolio)
        self.assertEqual(aggregate["configured_bot_count"], 3)
        self.assertEqual(aggregate["available_bot_count"], 3)
        self.assertEqual(aggregate["target_remaining"], 0.0)

    def test_bounded_recovery_prefers_high_water_drawdown_over_cycle_fields(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {
                    "cycle_realized_profit": 100.0,
                    "outstanding_drawdown": 100.0,
                    "high_water_drawdown": {"outstanding_drawdown": 100.0},
                },
            )
        self.assertEqual(recovery["drawdown_source"], "continuous_high_water")
        self.assertEqual(recovery["target_recovery_profit"], 33.0)
        self.assertTrue(recovery["active"])

    def test_flat_bot_can_use_pooled_campaign_drawdown(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
        recovery = crypto.crypto_15m_bounded_unit_recovery_review(
            settings,
            row,
            units,
            {
                "outstanding_drawdown": 0.0,
                "high_water_drawdown": {"outstanding_drawdown": 0.0},
                "pooled_recovery_enabled": True,
                "pooled_outstanding_drawdown": 100.0,
                "pooled_high_water_drawdown": {"outstanding_drawdown": 100.0},
                "recovery_reservations": {},
            },
        )
        self.assertTrue(recovery["active"])
        self.assertEqual(recovery["drawdown_source"], "pooled_continuous_high_water")
        self.assertEqual(recovery["outstanding_drawdown"], 100.0)
        self.assertEqual(recovery["base_units"], 3)
        self.assertGreater(recovery["bonus_units"], 0)

    def test_same_expiry_open_recovery_keeps_next_bet_at_base_size(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
        expiry_key = crypto.crypto_15m_recovery_expiry_key(row)
        recovery = crypto.crypto_15m_bounded_unit_recovery_review(
            settings,
            row,
            units,
            {
                "pooled_recovery_enabled": True,
                "pooled_outstanding_drawdown": 100.0,
                "pooled_high_water_drawdown": {"outstanding_drawdown": 100.0},
                "recovery_reservations": {
                    "open_overlay_count": 1,
                    "reserved_recovery_profit": 10.0,
                    "by_expiry": {expiry_key: {"count": 1}},
                },
            },
        )
        self.assertFalse(recovery["active"])
        self.assertEqual(recovery["reason"], "pooled_recovery_expiry_reserved")
        self.assertEqual(recovery["bonus_units"], 0)
        self.assertEqual(recovery["requested_stake"], units["requested_stake"])

    def test_other_expiry_reservation_reduces_available_bonus(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
        recovery = crypto.crypto_15m_bounded_unit_recovery_review(
            settings,
            row,
            units,
            {
                "pooled_recovery_enabled": True,
                "pooled_outstanding_drawdown": 100.0,
                "pooled_high_water_drawdown": {"outstanding_drawdown": 100.0},
                "recovery_reservations": {
                    "open_overlay_count": 1,
                    "reserved_recovery_profit": 20.0,
                    "by_expiry": {"different-expiry": {"count": 1}},
                },
            },
        )
        self.assertTrue(recovery["active"])
        self.assertEqual(recovery["target_recovery_profit"], 33.0)
        self.assertEqual(recovery["available_target_recovery_profit"], 13.0)
        self.assertGreater(recovery["bonus_units"], 0)
        self.assertLess(recovery["bonus_units"], 2)
        self.assertLessEqual(
            recovery["allocated_recovery_profit"],
            recovery["available_target_recovery_profit"],
        )

    def test_recovery_overlay_stays_active_in_confidence_unit_mode(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_TIERED_ACTION_GATE_ENABLED": "true",
            "CRYPTO_15M_PARTIAL_RECOVERY_ENABLED": "true",
        })
        portfolio = {
            "balance": 1000.0,
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -5.0,
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": 1,
                "placed_at": "p-1",
                "settled_at": "s-1",
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 1,
                },
            }],
            "bets": [],
        }
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
            "probability": {
                "model_disagreement": 0,
                "market_implied_yes": 95.0,
            },
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_campaign_lane_review(
                settings, portfolio, row, 1
            )
        self.assertTrue(review["eligible"])
        self.assertTrue(review["crypto_units"]["recovery_overlay_active"])
        self.assertEqual(review["sizing_mode"], "bounded_unit_recovery")
        self.assertEqual(review["recovery_mode"], "bounded_unit_recovery")
        bounded = review["bounded_unit_recovery"]
        self.assertTrue(bounded["active"])
        self.assertEqual(bounded["base_units"], 3)
        self.assertGreater(bounded["bonus_units"], 0)
        self.assertLessEqual(bounded["bonus_units"], 2)
        self.assertLessEqual(bounded["total_units"], 5)

    def test_bounded_unit_recovery_does_not_boost_below_three_unit_base(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=3, confidence=80)
        row.update({
            "expected_edge": 3,
            "edge_low": -2,
            "probability_net_edge_positive": 80,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -50.0},
            )
        self.assertEqual(units["target_units"], 2)
        self.assertFalse(recovery["active"])
        self.assertEqual(recovery["reason"], "quality_below_recovery_floor")
        self.assertEqual(recovery["bonus_units"], 0)
        self.assertEqual(recovery["requested_stake"], units["requested_stake"])

    def test_bounded_unit_recovery_does_not_boost_one_unit_signal(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=2, confidence=75)
        row.update({
            "expected_edge": 2,
            "edge_low": -3,
            "probability_net_edge_positive": 75,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -50.0},
            )
        self.assertEqual(units["target_units"], 1)
        self.assertFalse(recovery["active"])
        self.assertEqual(recovery["reason"], "quality_below_recovery_floor")
        self.assertEqual(recovery["bonus_units"], 0)
        self.assertEqual(recovery["requested_stake"], units["requested_stake"])

    def test_bounded_recovery_requires_normal_three_unit_probability_tier(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        units = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        self.assertEqual(units["target_units"], 3)
        units["win_probability_unit_cap"] = 2
        recovery = crypto.crypto_15m_bounded_unit_recovery_review(
            settings,
            row,
            units,
            {"cycle_realized_profit": -100.0},
        )
        self.assertFalse(recovery["active"])
        self.assertEqual(recovery["reason"], "probability_below_recovery_floor")
        self.assertEqual(recovery["bonus_units"], 0)

    def test_bounded_recovery_combined_size_obeys_all_non_edge_caps(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        units = crypto.crypto_15m_unit_candidate_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
        )
        units.update({
            "win_probability_unit_cap": 5,
            "kelly_unit_cap": 4,
            "liquidity_unit_cap": 5,
            "portfolio_unit_cap": 4,
            "execution_unit_cap": 5,
        })
        recovery = crypto.crypto_15m_bounded_unit_recovery_review(
            settings,
            row,
            units,
            {"cycle_realized_profit": -1000.0},
        )
        self.assertTrue(recovery["active"])
        self.assertEqual(recovery["maximum_total_units"], 4)
        self.assertEqual(recovery["bonus_units"], 1)
        self.assertEqual(recovery["total_units"], 4)
        self.assertTrue(recovery["combined_cap_applied"])

    def test_slippage_reconfirmed_signal_cannot_receive_recovery_bonus(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
            "live_slippage_reconfirmation": {
                "confirmed": True,
                "base_units_only": True,
            },
        })
        units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
        recovery = crypto.crypto_15m_bounded_unit_recovery_review(
            settings,
            row,
            units,
            {"cycle_realized_profit": -100.0},
        )

        self.assertEqual(units["target_units"], 3)
        self.assertFalse(recovery["active"])
        self.assertEqual(
            recovery["reason"],
            "slippage_reconfirmation_base_units_only",
        )
        self.assertEqual(recovery["bonus_units"], 0)
        self.assertEqual(recovery["requested_stake"], units["requested_stake"])

    def test_bounded_unit_recovery_targets_third_drawdown_with_two_unit_cap(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        settings["CRYPTO_15M_UNIT_SIZE_PCT"] = "1.00"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
            "exact_fee_cents": 1.75,
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -100.0},
            )
        self.assertEqual(recovery["target_recovery_profit"], 33.0)
        self.assertEqual(recovery["base_units"], 3)
        self.assertEqual(recovery["bonus_units"], 2)
        self.assertEqual(recovery["total_units"], 5)
        self.assertEqual(recovery["requested_stake"], 50.0)

    def test_recovery_counterfactual_isolates_bonus_profit_and_drawdown(self):
        cutoff = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)

        def settled(row_id, hour, result, profit):
            return {
                "id": row_id,
                "status": "settled",
                "mode": "live",
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": 1,
                "asset": "BTC",
                "ticker": f"KXBTC15M-{row_id}",
                "side": "yes",
                "placed_at": (cutoff + timedelta(minutes=45)).isoformat(),
                "settled_at": (cutoff + timedelta(hours=hour)).isoformat(),
                "result": result,
                "contracts": 100,
                "entry_price": 40,
                "stake": 40,
                "fee": 1,
                "profit": profit,
                "crypto_units": {
                    "bounded_recovery": {
                        "base_units": 3,
                        "bonus_units": 1,
                        "total_units": 4,
                    },
                },
            }

        portfolio = {
            "bets": [
                settled("loss", 1, "LOSS", -41),
                settled("win", 2, "WIN", 59),
            ],
            "crypto_15m_high_water_ledgers": {
                "1": {
                    "migration_cutoff": cutoff.isoformat(),
                    "baseline_drawdown": 10,
                },
            },
        }
        analytics = crypto.recovery_counterfactual_analytics(
            portfolio,
            reference_time=cutoff + timedelta(hours=3),
        )
        summary = analytics["summary"]
        self.assertEqual(summary["status"], "active")
        self.assertEqual(summary["settled"], 2)
        self.assertEqual((summary["wins"], summary["losses"]), (1, 1))
        self.assertEqual(summary["actual_profit"], 18.0)
        self.assertEqual(summary["counterfactual_base_profit"], 13.5)
        self.assertEqual(summary["incremental_profit"], 4.5)
        self.assertEqual(summary["bonus_risk"], 20.5)
        self.assertAlmostEqual(summary["incremental_roi"], 21.95, places=2)
        self.assertEqual(summary["maximum_incremental_loss_streak"], 1)
        self.assertEqual(summary["actual_max_drawdown"], 51.0)
        self.assertEqual(summary["counterfactual_base_max_drawdown"], 40.75)
        bot = analytics["by_bot"][0]
        self.assertEqual(bot["overlay_bets"], 2)
        self.assertEqual(bot["actual"]["completed_recoveries"], 1)
        self.assertEqual(bot["actual"]["average_recovery_hours"], 2.0)
        self.assertEqual(analytics["recent"][0]["ticker"], "KXBTC15M-win")

    def test_recovery_counterfactual_collects_until_bonus_bet_settles(self):
        analytics = crypto.recovery_counterfactual_analytics(
            {
                "bets": [],
                "crypto_15m_high_water_ledgers": {
                    "1": {
                        "migration_cutoff": "2026-08-14T00:00:00+00:00",
                        "baseline_drawdown": 0,
                    },
                },
            },
            reference_time=datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(analytics["summary"]["status"], "collecting")
        self.assertEqual(analytics["summary"]["settled"], 0)
        self.assertEqual(analytics["by_bot"][0]["overlay_bets"], 0)

    def test_two_unit_recovery_cap_still_respects_five_unit_total(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        settings["CRYPTO_15M_UNIT_SIZE_PCT"] = "1.00"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=5, confidence=95)
        row.update({
            "expected_edge": 5,
            "edge_low": 0.0001,
            "probability_net_edge_positive": 95,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -1000.0},
            )
        self.assertEqual(units["target_units"], 4)
        self.assertEqual(recovery["bonus_units"], 1)
        self.assertEqual(recovery["total_units"], 5)
        self.assertEqual(recovery["requested_stake"], 50.0)

    def test_bounded_unit_recovery_never_exceeds_normal_five_unit_cap(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        settings["CRYPTO_15M_UNIT_SIZE_PCT"] = "1.00"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=7, confidence=98)
        row.update({
            "expected_edge": 7,
            "edge_low": 2,
            "probability_net_edge_positive": 98,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -1000.0},
            )
        self.assertEqual(units["target_units"], 5)
        self.assertEqual(recovery["reason"], "base_at_recovery_cap")
        self.assertEqual(recovery["bonus_units"], 0)
        self.assertEqual(recovery["total_units"], 5)
        self.assertEqual(recovery["requested_stake"], 50.0)

    def test_bounded_recovery_cannot_exceed_live_execution_unit_cap(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        settings["CRYPTO_15M_UNIT_SIZE_PCT"] = "1.00"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
            "execution_unit_cap": {"maximum_units": 2},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -100.0},
            )

        self.assertEqual(units["model_target_units"], 3)
        self.assertEqual(units["target_units"], 2)
        self.assertEqual(recovery["maximum_total_units"], 2)
        self.assertEqual(recovery["bonus_units"], 0)
        self.assertEqual(recovery["total_units"], 2)
        self.assertEqual(recovery["requested_stake"], 20.0)

    def test_directional_cooldown_suppresses_bounded_recovery_bonus(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_UNIT_STAKING_ENABLED"] = "true"
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50, edge=4, confidence=88)
        row.update({
            "expected_edge": 4,
            "edge_low": -1,
            "probability_net_edge_positive": 88,
            "data_quality": {"score": 0.99},
        })
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            units = crypto.crypto_15m_unit_candidate_review(settings, portfolio, row)
            recovery = crypto.crypto_15m_bounded_unit_recovery_review(
                settings,
                row,
                units,
                {"cycle_realized_profit": -50.0},
                {"active": True},
            )
        self.assertFalse(recovery["active"])
        self.assertEqual(recovery["reason"], "directional_cooldown_base_units_only")
        self.assertEqual(recovery["requested_stake"], units["requested_stake"])

    def test_evidence_tiered_gate_allows_second_expiry_bet_at_four_percent(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_TIERED_ACTION_GATE_ENABLED"] = "true"
        open_row = candidate(price=50, edge=4, confidence=70, asset="SOL")
        open_row.update({
            "status": "open",
            "mode": "live",
            "strategy_owner": "crypto_15m_campaign",
            "bot_number": 2,
            "crypto_live_campaign": {
                "role": "primary",
                "bot_number": 2,
            },
        })
        portfolio = {"balance": 1000.0, "bets": [open_row]}
        second = candidate(price=50, edge=4, confidence=70, asset="BTC")
        review = crypto.crypto_15m_campaign_lane_review(
            settings,
            portfolio,
            second,
            1,
        )
        self.assertTrue(review["eligible"])
        self.assertFalse(review["same_expiry_elite_exception"])
        self.assertEqual(review["same_expiry_normal_limit"], 2)

    def test_commodity_15m_uses_campaign_bot_gate_and_recovery_sizing(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_EXECUTION_MODE": "live",
            "MULTI_MARKET_ENABLED": "true",
            "MULTI_MARKET_LIVE_ENABLED": "true",
            "MULTI_MARKET_REQUIRE_PYTH_HISTORY": "true",
        })
        row = candidate(price=50, edge=3, confidence=72, minutes=7, asset="GOLD")
        row.update({
            "market_lane": "commodity_15m",
            "side": "yes",
            "model": {
                "data_source": "pyth",
                "settlement_source": "Pyth",
                "history_points": 120,
            },
            "flow_review": {
                "yes_source_count": 2,
                "source_count": 2,
                "direction": "yes",
                "strength": 0.20,
            },
            "skip_reasons": [],
        })

        self.assertTrue(crypto.candidate_passes(settings, {"balance": 1000, "bets": []}, row))
        self.assertEqual(row["strategy_owner"], crypto.CRYPTO_15M_CAMPAIGN_OWNER)
        self.assertIn(row["bot_number"], {1, 2, 3})
        self.assertEqual(row["crypto_live_campaign"]["qualification_mode"], "simple_action_gate")
        self.assertGreater(row["crypto_live_campaign"]["applied_stake"], 0)
        self.assertNotIn("multi_market", row)

        stake = crypto.candidate_stake(
            settings,
            row,
            balance=1000,
            portfolio={"balance": 1000, "bets": []},
        )
        self.assertEqual(stake, round(row["crypto_live_campaign"]["applied_stake"], 2))

    def test_commodity_live_pause_keeps_only_qualified_candidate_in_shadow(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_EXECUTION_MODE": "live",
            "MULTI_MARKET_ENABLED": "true",
            "MULTI_MARKET_LIVE_ENABLED": "true",
            "MULTI_MARKET_COMMODITY_LIVE_ENABLED": "false",
            "MULTI_MARKET_REQUIRE_PYTH_HISTORY": "true",
        })
        row = candidate(price=50, edge=3, confidence=72, minutes=7, asset="GOLD")
        row.update({
            "market_lane": "commodity_15m",
            "side": "yes",
            "model": {
                "data_source": "pyth",
                "settlement_source": "Pyth",
                "history_points": 120,
            },
            "flow_review": {
                "yes_source_count": 2,
                "source_count": 2,
                "direction": "yes",
                "strength": 0.20,
            },
            "skip_reasons": [],
        })

        self.assertFalse(
            crypto.candidate_passes(settings, {"balance": 1000, "bets": []}, row)
        )
        self.assertEqual(row["skip_reasons"], ["commodity_live_shadow"])
        self.assertTrue(row["directional_confirmation"]["ok"])

        weak = dict(row)
        weak["ticker"] = "KXGOLD15M-WEAK"
        weak["skip_reasons"] = []
        weak["flow_review"] = {
            "yes_source_count": 0,
            "source_count": 1,
            "direction": "no",
            "strength": 0.05,
        }
        self.assertFalse(
            crypto.candidate_passes(settings, {"balance": 1000, "bets": []}, weak)
        )
        self.assertIn("commodity_live_shadow", weak["skip_reasons"])
        self.assertIn("directional_flow_too_weak", weak["skip_reasons"])

    def test_commodity_shadow_uses_evidence_tiered_two_to_twelve_minute_gate(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_15M_TIERED_ACTION_GATE_ENABLED": "true",
            "MULTI_MARKET_ENABLED": "true",
            "MULTI_MARKET_LIVE_ENABLED": "true",
            "MULTI_MARKET_COMMODITY_LIVE_ENABLED": "false",
            "MULTI_MARKET_REQUIRE_PYTH_HISTORY": "true",
        })
        portfolio = {"balance": 1000, "bets": []}

        def commodity(minutes):
            row = candidate(
                price=50,
                edge=4,
                confidence=70,
                minutes=minutes,
                asset="GOLD",
            )
            row.update({
                "market_lane": "commodity_15m",
                "side": "yes",
                "model": {
                    "data_source": "pyth",
                    "settlement_source": "Pyth",
                    "history_points": 120,
                },
                "flow_review": {
                    "yes_source_count": 2,
                    "source_count": 2,
                    "direction": "yes",
                    "strength": 0.20,
                },
                "skip_reasons": [],
            })
            return row

        boundary = commodity(2)
        self.assertFalse(crypto.candidate_passes(settings, portfolio, boundary))
        self.assertEqual(boundary["skip_reasons"], ["commodity_live_shadow"])
        self.assertEqual(
            boundary["crypto_live_campaign"]["qualification_mode"],
            "evidence_tiered_gate",
        )

        too_early = commodity(1.99)
        self.assertFalse(crypto.candidate_passes(settings, portfolio, too_early))
        self.assertIn("commodity_live_shadow", too_early["skip_reasons"])
        self.assertIn("campaign_entry_window", too_early["skip_reasons"])

    def test_simple_action_gate_removes_mid_price_early_entry_and_edge_gap(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        row = candidate(price=50, edge=3.5, confidence=70, minutes=10.5)
        review = crypto.crypto_15m_campaign_lane_review(
            settings,
            {"balance": 1000.0, "bets": []},
            row,
            1,
        )

        self.assertTrue(review["eligible"])
        self.assertEqual(review["reason"], "eligible")
        self.assertEqual(review["qualification_mode"], "simple_action_gate")
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "6"
        self.assertEqual(crypto.effective_scan_bet_limit(settings), 5)

    def test_directional_loss_cluster_caps_two_recovery_attempts_per_bot(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_ENABLED": "true",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_LOSS_COUNT": "3",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_MIN_BOTS": "2",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_LOOKBACK_MINUTES": "90",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_COOLDOWN_BETS": "2",
            "CRYPTO_15M_BOT_1_CYCLE_PROFIT": "5",
        })
        now = crypto.utc_now()
        bot_one_campaign_id = crypto.crypto_15m_campaign_strategy(
            settings,
            date_key=TODAY,
            bot_number=1,
        )["campaign_id"]

        def loss(bot_number, minutes_ago, *, cooldown=False):
            settled_at = now - timedelta(minutes=minutes_ago)
            return {
                "id": f"loss-{bot_number}-{minutes_ago}",
                "status": "settled",
                "result": "LOSS",
                "side": "no",
                "profit": -1.0,
                "placed_at": settled_at.isoformat(),
                "settled_at": settled_at.isoformat(),
                "strategy_owner": crypto.CRYPTO_15M_CAMPAIGN_OWNER,
                "bot_number": bot_number,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "campaign_id": bot_one_campaign_id if bot_number == 1 else "other-bot",
                    "bot_number": bot_number,
                    "directional_recovery_cooldown": {"active": cooldown},
                },
            }

        portfolio = {
            "balance": 1000.0,
            "bets": [loss(1, 30), loss(2, 20), loss(3, 10)],
        }
        row = candidate(price=50, edge=5, confidence=75, minutes=7)
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_campaign_lane_review(
                settings,
                portfolio,
                row,
                1,
            )

        self.assertTrue(review["directional_recovery_cooldown"]["active"])
        self.assertEqual(
            review["recovery_mode"],
            "directional_cooldown",
            msg=review,
        )
        self.assertEqual(review["target_profit"], review["cycle_goal"])
        self.assertGreater(
            review["full_recovery_target_profit"],
            review["cycle_goal"],
        )

        portfolio["bets"].extend([loss(1, 5, cooldown=True), loss(1, 2, cooldown=True)])
        cooldown = crypto.crypto_directional_recovery_cooldown(
            settings,
            portfolio,
            1,
            now=now,
        )
        self.assertFalse(cooldown["active"])
        self.assertEqual(cooldown["attempts_used"], 2)
        self.assertEqual(cooldown["reason"], "cooldown_attempts_complete")

    def test_mid_price_exception_requires_nine_edge_and_74_confidence(self):
        common = {
            "min_edge": 6,
            "min_confidence": 74,
            "min_price": 35,
            "max_price": 70,
            "mid_price_min": 35,
            "mid_price_max": 54,
            "mid_price_min_edge": 9,
            "mid_price_min_confidence": 74,
        }
        rejected = review_candidate(
            {"bets": []}, candidate(price=40, edge=8.99, confidence=80),
            TODAY, row_date, **common,
        )
        accepted = review_candidate(
            {"bets": []}, candidate(price=40, edge=9, confidence=74),
            TODAY, row_date, **common,
        )
        self.assertEqual(rejected["reason"], "campaign_mid_price_quality_filter")
        self.assertTrue(accepted["eligible"])

    def test_market_fetch_marks_a_complete_dns_outage(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_SERIES": "KXBTC15M,KXETH15M",
            "CRYPTO_FETCH_MAX_WORKERS": "2",
        })
        with patch.object(
            crypto,
            "http_json",
            side_effect=ConnectionError("Failed to resolve external-api.kalshi.com"),
        ):
            markets = crypto.fetch_kalshi_markets(settings)

        self.assertEqual(markets, [])
        self.assertTrue(crypto.LAST_KALSHI_MARKET_FETCH_HEALTH["outage"])
        self.assertTrue(
            crypto.LAST_KALSHI_MARKET_FETCH_HEALTH["network_failure"]
        )
        self.assertEqual(
            crypto.LAST_KALSHI_MARKET_FETCH_HEALTH["failed_series"],
            2,
        )

    def test_low_edge_pilot_is_isolated_and_uses_base_cycle_sizing(self):
        settings = self.pilot_settings()
        row = candidate(price=60.0, edge=2.0, confidence=72.0)

        review = crypto.crypto_15m_campaign_review(
            settings,
            {"bets": []},
            row,
            bot_number=1,
        )

        self.assertTrue(review["eligible"])
        self.assertTrue(review["low_edge_pilot"]["active"])
        self.assertFalse(review["low_edge_pilot"]["base_cycle_only"])
        self.assertFalse(review["low_edge_pilot"]["affects_global_edge_floor"])
        self.assertEqual(review["target_profit"], 2.0)

    def test_low_edge_pilot_allows_recovery_but_rejects_a_second_open_pilot(self):
        settings = self.pilot_settings()
        row = candidate(price=60.0, edge=2.0, confidence=72.0)
        recovery = crypto.crypto_low_edge_pilot_review(
            settings, {"bets": []}, row, 1
        )
        self.assertTrue(recovery["eligible"])
        self.assertFalse(recovery["base_cycle_only"])

        portfolio = {
            "bets": [{
                "status": "open",
                "strategy_owner": crypto.CRYPTO_15M_CAMPAIGN_OWNER,
                "low_edge_pilot": {"active": True},
            }]
        }
        with patch.object(
            crypto,
            "crypto_15m_campaign_lane_context",
            return_value={
                "cycle_number": 1,
                "cycle_loss_count": 0,
                "realized_drawdown": 0.0,
            },
        ):
            occupied = crypto.crypto_low_edge_pilot_review(
                settings, portfolio, row, 2
            )
        self.assertEqual(
            occupied["reason"],
            "campaign_low_edge_pilot_open_limit",
        )

    def test_low_edge_pilot_stops_at_review_sample(self):
        settings = self.pilot_settings()
        portfolio = {
            "bets": [{
                "status": "settled",
                "strategy_owner": crypto.CRYPTO_15M_CAMPAIGN_OWNER,
                "low_edge_pilot": {"active": True},
            } for _ in range(50)]
        }
        row = candidate(price=60.0, edge=2.0, confidence=72.0)
        with patch.object(
            crypto,
            "crypto_15m_campaign_lane_context",
            return_value={
                "cycle_number": 1,
                "cycle_loss_count": 0,
                "realized_drawdown": 0.0,
            },
        ):
            review = crypto.crypto_low_edge_pilot_review(
                settings, portfolio, row, 1
            )
        self.assertEqual(
            review["reason"],
            "campaign_low_edge_pilot_sample_complete",
        )

    def test_official_low_edge_rotation_has_no_settlement_sample_cap(self):
        settings = self.pilot_settings()
        settings.update({
            "CRYPTO_15M_LOW_EDGE_OFFICIAL_ROTATION": "true",
            "CRYPTO_15M_LOW_EDGE_PILOT_MIN_CONFIDENCE": "74",
            "CRYPTO_15M_LOW_EDGE_PILOT_MAX_PRICE_CENTS": "65",
        })
        portfolio = {
            "bets": [{
                "status": "settled",
                "strategy_owner": crypto.CRYPTO_15M_CAMPAIGN_OWNER,
                "low_edge_pilot": {"active": True},
            } for _ in range(100)]
        }
        row = candidate(price=60.0, edge=2.0, confidence=75.0)
        review = crypto.crypto_low_edge_pilot_review(
            settings, portfolio, row, 1
        )
        self.assertTrue(review["eligible"])
        self.assertTrue(review["official_rotation"])
        self.assertEqual(review["max_settled"], 0)
        self.assertEqual(review["reason"], "low_edge_official_candidate")
        summary = crypto.summarize_low_edge_live_pilot(portfolio, settings)
        self.assertEqual(summary["mode"], "official_rotation")
        self.assertFalse(summary["sample_complete"])

    def test_candidate_quality_edge_is_net_of_estimated_fee(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_ALLOW_RANGE_MARKETS": "false",
            "CRYPTO_ALLOW_ABOVE_BELOW_MARKETS": "true",
            "CRYPTO_SHADOW_FEE_RATE": "0.07",
        })
        market = {
            "ticker": "KXBTC15M-WINDOW",
            "event_ticker": "KXBTC15M-WINDOW",
            "series_ticker": "KXBTC15M",
            "title": "Bitcoin above target",
            "yes_ask": 50,
            "no_ask": 55,
            "close_time": "2026-07-18T15:15:00Z",
        }
        with patch.object(crypto, "is_15m_target_market", return_value=True), patch.object(
            crypto, "infer_asset", return_value="BTC"
        ), patch.object(crypto, "market_kind", return_value="above"), patch.object(
            crypto, "estimate_15m_asset_model", return_value={"prob_up": 70}
        ), patch.object(
            crypto, "probability_for_market", return_value={"prob": 70.0, "minutes_to_close": 7.0}
        ), patch.object(crypto, "confidence_for_candidate", return_value=85.0):
            rows = crypto.build_candidates(settings, [market], {"BTC": {}}, {})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_edge"], 20.0)
        self.assertEqual(rows[0]["estimated_fee_edge_pp"], 1.75)
        self.assertEqual(rows[0]["edge"], 17.5)
        self.assertEqual(rows[0]["edge"], rows[0]["net_edge"])

    def test_initial_candidate_prefers_executable_orderbook_price(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_ALLOW_RANGE_MARKETS": "false",
            "CRYPTO_ALLOW_ABOVE_BELOW_MARKETS": "true",
            "CRYPTO_SHADOW_FEE_RATE": "0.07",
        })
        market = {
            "ticker": "KXBTC15M-WINDOW",
            "event_ticker": "KXBTC15M-WINDOW",
            "series_ticker": "KXBTC15M",
            "title": "Bitcoin above target",
            "yes_ask": 50,
            "no_ask": 55,
            "close_time": "2026-07-18T15:15:00Z",
        }
        microstructure = {
            market["ticker"]: {
                "best_yes_entry_price_cents": 43.0,
                "best_no_entry_price_cents": 58.0,
                "orderbook_fetched_at": "2026-07-18T15:07:00Z",
            }
        }
        with patch.object(crypto, "is_15m_target_market", return_value=True), patch.object(
            crypto, "infer_asset", return_value="BTC"
        ), patch.object(crypto, "market_kind", return_value="above"), patch.object(
            crypto, "estimate_15m_asset_model", return_value={"prob_up": 70}
        ), patch.object(
            crypto, "probability_for_market", return_value={"prob": 70.0, "minutes_to_close": 7.0}
        ), patch.object(crypto, "confidence_for_candidate", return_value=85.0):
            rows = crypto.build_candidates(
                settings,
                [market],
                {"BTC": {}},
                {},
                kalshi_microstructure_by_ticker=microstructure,
            )

        self.assertEqual(rows[0]["entry_price"], 43.0)
        self.assertEqual(
            rows[0]["initial_price_source"],
            "kalshi_executable_orderbook",
        )
        self.assertEqual(rows[0]["market_yes_ask"], 50.0)

        # Source attribution must follow the selected data source, even when a
        # fresh executable price happens to equal the older market summary.
        microstructure[market["ticker"]].update({
            "best_yes_entry_price_cents": 50.0,
            "best_no_entry_price_cents": 55.0,
        })
        with patch.object(crypto, "is_15m_target_market", return_value=True), patch.object(
            crypto, "infer_asset", return_value="BTC"
        ), patch.object(crypto, "market_kind", return_value="above"), patch.object(
            crypto, "estimate_15m_asset_model", return_value={"prob_up": 70}
        ), patch.object(
            crypto, "probability_for_market", return_value={"prob": 70.0, "minutes_to_close": 7.0}
        ), patch.object(crypto, "confidence_for_candidate", return_value=85.0):
            equal_price_rows = crypto.build_candidates(
                settings,
                [market],
                {"BTC": {}},
                {},
                kalshi_microstructure_by_ticker=microstructure,
            )
        self.assertEqual(
            equal_price_rows[0]["initial_price_source"],
            "kalshi_executable_orderbook",
        )
        self.assertEqual(
            equal_price_rows[0]["market_quote_fetched_at"],
            "2026-07-18T15:07:00Z",
        )

    def test_first_primary_is_sized_to_net_five_profit(self):
        review = review_candidate({}, candidate(), TODAY, row_date)
        self.assertTrue(review["eligible"])
        self.assertEqual(review["role"], "primary")
        self.assertEqual(review["target_profit"], 5.0)
        self.assertEqual(review["applied_stake"], 5.5)
        self.assertEqual(review["required_contracts"], 11)

    def test_configured_twenty_to_seventy_cent_price_band(self):
        kwargs = {"min_price": 20.0, "max_price": 70.0}
        self.assertEqual(review_candidate({}, candidate(price=19.99), TODAY, row_date, **kwargs)["reason"], "campaign_price_range")
        self.assertTrue(review_candidate({}, candidate(price=20.0, edge=15, confidence=75), TODAY, row_date, **kwargs)["eligible"])
        self.assertTrue(review_candidate({}, candidate(price=70.0), TODAY, row_date, **kwargs)["eligible"])
        self.assertEqual(review_candidate({}, candidate(price=70.01), TODAY, row_date, **kwargs)["reason"], "campaign_price_range")

    def test_twenty_cent_longshot_requires_fifteen_edge_and_seventy_five_confidence(self):
        kwargs = {
            "min_price": 20.0,
            "max_price": 70.0,
            "min_edge": 12.0,
            "min_confidence": 70.0,
            "longshot_max_price": 20.0,
            "longshot_min_edge": 15.0,
            "longshot_min_confidence": 75.0,
        }
        self.assertEqual(
            review_candidate({}, candidate(price=20, edge=14.99, confidence=80), TODAY, row_date, **kwargs)["reason"],
            "campaign_longshot_quality_filter",
        )
        self.assertEqual(
            review_candidate({}, candidate(price=20, edge=16, confidence=74.99), TODAY, row_date, **kwargs)["reason"],
            "campaign_longshot_quality_filter",
        )
        self.assertTrue(
            review_candidate({}, candidate(price=20, edge=15, confidence=75), TODAY, row_date, **kwargs)["eligible"]
        )

    def test_entry_window_is_two_to_twelve_minutes(self):
        self.assertEqual(review_candidate({}, candidate(minutes=1.99), TODAY, row_date)["reason"], "campaign_entry_window")
        self.assertTrue(review_candidate({}, candidate(minutes=2.0), TODAY, row_date)["eligible"])
        self.assertTrue(review_candidate({}, candidate(minutes=12.0), TODAY, row_date)["eligible"])
        self.assertEqual(review_candidate({}, candidate(minutes=12.01), TODAY, row_date)["reason"], "campaign_entry_window")

    def test_mid_price_band_requires_six_edge_and_sixty_five_confidence(self):
        kwargs = {
            "min_edge": 4.0,
            "min_confidence": 60.0,
            "mid_price_min": 45.0,
            "mid_price_max": 54.0,
            "mid_price_min_edge": 6.0,
            "mid_price_min_confidence": 65.0,
        }
        self.assertEqual(
            review_candidate(
                {},
                candidate(price=50, edge=5.99, confidence=80),
                TODAY,
                row_date,
                **kwargs,
            )["reason"],
            "campaign_mid_price_quality_filter",
        )
        self.assertEqual(
            review_candidate(
                {},
                candidate(price=50, edge=7, confidence=64.99),
                TODAY,
                row_date,
                **kwargs,
            )["reason"],
            "campaign_mid_price_quality_filter",
        )
        self.assertTrue(
            review_candidate(
                {},
                candidate(price=50, edge=6, confidence=65),
                TODAY,
                row_date,
                **kwargs,
            )["eligible"]
        )
        self.assertTrue(
            review_candidate(
                {},
                candidate(price=44, edge=4, confidence=60),
                TODAY,
                row_date,
                **kwargs,
            )["eligible"]
        )

    def test_more_than_ten_minutes_requires_stronger_quality(self):
        kwargs = {
            "min_edge": 4.0,
            "min_confidence": 60.0,
            "early_entry_min_minutes": 10.0,
            "early_entry_min_edge": 6.0,
            "early_entry_min_confidence": 70.0,
        }
        self.assertTrue(
            review_candidate(
                {},
                candidate(price=44, minutes=10.0, edge=4, confidence=60),
                TODAY,
                row_date,
                **kwargs,
            )["eligible"]
        )
        self.assertEqual(
            review_candidate(
                {},
                candidate(price=44, minutes=10.01, edge=5.99, confidence=80),
                TODAY,
                row_date,
                **kwargs,
            )["reason"],
            "campaign_early_entry_quality_filter",
        )
        self.assertTrue(
            review_candidate(
                {},
                candidate(price=44, minutes=15, edge=6, confidence=70),
                TODAY,
                row_date,
                max_minutes_remaining=15,
                **kwargs,
            )["eligible"]
        )

    def test_second_same_expiry_position_requires_elite_quality(self):
        open_rows = []
        for bot_number, asset in ((2, "SOL"),):
            open_rows.append({
                "status": "open",
                "mode": "live",
                "strategy_owner": "crypto_15m_campaign",
                "ticker": f"KX{asset}15M-WINDOW",
                "event_ticker": f"KX{asset}15M-WINDOW",
                "asset": asset,
                "close_time": "2026-07-18T15:15:00Z",
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": bot_number,
                },
                "bot_number": bot_number,
            })
        portfolio = {"bets": open_rows}
        quality_kwargs = {
            "min_edge": 4,
            "min_confidence": 65,
        }
        blocked = review_candidate(
            portfolio,
            candidate(price=44, edge=5.99, confidence=90),
            TODAY,
            row_date,
            bot_number=1,
            **quality_kwargs,
        )
        self.assertEqual(blocked["reason"], "campaign_expiry_window_requires_elite")
        low_confidence = review_candidate(
            portfolio,
            candidate(price=44, edge=6, confidence=69.99),
            TODAY,
            row_date,
            bot_number=1,
            **quality_kwargs,
        )
        self.assertEqual(
            low_confidence["reason"],
            "campaign_expiry_window_requires_elite",
        )
        elite = review_candidate(
            portfolio,
            candidate(price=44, edge=6, confidence=70),
            TODAY,
            row_date,
            bot_number=1,
            **quality_kwargs,
        )
        self.assertTrue(elite["eligible"])
        self.assertTrue(elite["same_expiry_elite_exception"])

    def test_third_same_expiry_position_is_blocked_even_when_elite(self):
        open_rows = []
        for bot_number, asset in ((2, "SOL"), (3, "ETH")):
            open_rows.append({
                "status": "open",
                "mode": "live",
                "strategy_owner": "crypto_15m_campaign",
                "ticker": f"KX{asset}15M-WINDOW",
                "event_ticker": f"KX{asset}15M-WINDOW",
                "asset": asset,
                "close_time": "2026-07-18T15:15:00Z",
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": bot_number,
                },
                "bot_number": bot_number,
            })
        review = review_candidate(
            {"bets": open_rows},
            candidate(price=50, edge=30, confidence=95),
            TODAY,
            row_date,
            bot_number=1,
        )
        self.assertEqual(review["reason"], "campaign_expiry_window_concentration")

    def test_only_15m_up_down_target_markets_are_allowed(self):
        daily = candidate()
        daily["is_15m_market"] = False
        self.assertEqual(review_candidate({}, daily, TODAY, row_date)["reason"], "campaign_15m_only")
        range_market = candidate()
        range_market["market_kind"] = "range"
        self.assertEqual(review_candidate({}, range_market, TODAY, row_date)["reason"], "campaign_target_market_only")

    def test_primary_cycles_five_two_fifty_one_twenty_five_then_stop(self):
        history = []
        for cycle, (goal, expected) in enumerate(zip((5.0, 2.5, 1.25), (5.5, 3.0, 1.5)), start=1):
            review = review_candidate({"history": history}, candidate(), TODAY, row_date)
            self.assertEqual(review["applied_stake"], expected)
            history.append(
                {
                    "status": "settled",
                    "result": "WIN",
                    "profit": goal,
                    "strategy_owner": "crypto_15m_campaign",
                    "placed_at": f"p-{cycle}",
                    "settled_at": f"s-{cycle}",
                    "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
                }
            )
        self.assertTrue(build_campaign_context({"history": history}, TODAY, row_date)["complete"])
        self.assertEqual(review_candidate({"history": history}, candidate(), TODAY, row_date)["reason"], "campaign_complete")

    def test_one_large_win_advances_only_one_cycle(self):
        portfolio = {
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 9.73,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "p",
                "settled_at": "s",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            }]
        }
        context = build_campaign_context(portfolio, TODAY, row_date, goal_tolerance=0.05)
        self.assertEqual(context["cycle_number"], 2)
        self.assertEqual(context["cycle_goal"], 2.5)
        self.assertEqual(context["cycle_start_profit"], 9.73)
        self.assertEqual(context["target_remaining"], 2.5)
        self.assertFalse(context["complete"])
        self.assertEqual(context["progression_mode"], "separate_cycle_profit_targets")

    def test_cycle_two_recovers_only_losses_after_cycle_one_win(self):
        portfolio = {
            "history": [
                {
                    "status": "settled",
                    "result": "WIN",
                    "profit": 9.73,
                    "strategy_owner": "crypto_15m_campaign",
                    "placed_at": "p-1",
                    "settled_at": "s-1",
                    "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
                },
                {
                    "status": "settled",
                    "result": "LOSS",
                    "profit": -1.0,
                    "strategy_owner": "crypto_15m_campaign",
                    "placed_at": "p-2",
                    "settled_at": "s-2",
                    "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
                },
            ]
        }
        context = build_campaign_context(portfolio, TODAY, row_date)
        self.assertEqual(context["cycle_number"], 2)
        self.assertEqual(context["cycle_start_profit"], 9.73)
        self.assertEqual(context["cycle_realized_profit"], -1.0)
        self.assertEqual(context["target_remaining"], 3.5)

    def test_five_two_dollar_cycles_require_five_separate_profitable_sequences(self):
        goals = (2.0, 2.0, 2.0, 2.0, 2.0)
        history = []
        for cycle in range(1, 6):
            review = review_candidate(
                {"history": history},
                candidate(price=50),
                TODAY,
                row_date,
                cycle_goals=goals,
            )
            self.assertEqual(review["cycle_number"], cycle)
            self.assertEqual(review["target_profit"], 2.0)
            history.append({
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": f"p-{cycle}",
                "settled_at": f"s-{cycle}",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            })
        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=goals,
        )
        self.assertTrue(context["complete"])
        self.assertEqual(context["cycle_index"], 5)
        self.assertEqual(context["realized_profit"], 10.0)

    def test_extending_completed_five_cycle_campaign_resumes_at_first_one_dollar_cycle(self):
        history = []
        for cycle in range(1, 6):
            history.append({
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": f"p-{cycle}",
                "settled_at": f"s-{cycle}",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            })

        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=(2.0, 2.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        )

        self.assertFalse(context["complete"])
        self.assertEqual(context["cycle_index"], 5)
        self.assertEqual(context["cycle_number"], 6)
        self.assertEqual(context["cycle_goal"], 1.0)
        self.assertEqual(context["cycle_start_profit"], 10.0)
        self.assertEqual(context["target_remaining"], 1.0)

    def test_campaign_id_restarts_progress_without_removing_prior_bets(self):
        history = [
            {
                "status": "settled",
                "result": "WIN",
                "profit": 10.0,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "old-p",
                "settled_at": "old-s",
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "campaign_id": "old-run",
                },
            },
            {
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "new-p",
                "settled_at": "new-s",
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "campaign_id": "new-run",
                },
            },
        ]
        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            campaign_id="new-run",
        )
        self.assertEqual(context["campaign_id"], "new-run")
        self.assertEqual(context["settled_count"], 1)
        self.assertEqual(context["realized_profit"], 2.0)
        self.assertEqual(context["cycle_number"], 2)
        self.assertFalse(context["complete"])

    def test_hybrid_recovery_switches_to_half_drawdown_after_two_losses(self):
        history = [
            {
                "status": "settled",
                "result": "LOSS",
                "profit": -2.5,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "p-1",
                "settled_at": "s-1",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            },
            {
                "status": "settled",
                "result": "LOSS",
                "profit": -2.5,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "p-2",
                "settled_at": "s-2",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            },
        ]
        review = review_candidate(
            {"history": history},
            candidate(price=50),
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertTrue(review["partial_recovery_active"])
        self.assertEqual(review["cycle_loss_count"], 2)
        self.assertEqual(review["recovery_mode"], "partial")
        self.assertEqual(review["full_recovery_target_profit"], 7.0)
        self.assertEqual(review["target_profit"], 4.5)
        self.assertEqual(review["applied_stake"], 5.0)

    def test_partial_win_stays_in_same_cycle_until_full_two_dollar_goal(self):
        history = [
            {
                "status": "settled",
                "result": result,
                "profit": profit,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": f"p-{index}",
                "settled_at": f"s-{index}",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            }
            for index, (result, profit) in enumerate(
                (("LOSS", -2.5), ("LOSS", -2.5), ("WIN", 4.5)),
                start=1,
            )
        ]
        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertEqual(context["cycle_number"], 1)
        self.assertEqual(context["cycle_realized_profit"], -0.5)
        self.assertTrue(context["partial_recovery_active"])
        review = review_candidate(
            {"history": history},
            candidate(price=50),
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertEqual(review["target_profit"], 2.25)

    def test_hybrid_plan_waits_for_next_daily_campaign(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CYCLE_GOALS": "2,2,2,2,2",
            "CRYPTO_15M_LEGACY_CYCLE_GOALS": "5,2.5,1.25",
            "CRYPTO_15M_HYBRID_EFFECTIVE_DATE": "2026-07-21",
            "CRYPTO_15M_PARTIAL_RECOVERY_ENABLED": "true",
        })
        legacy = crypto.crypto_15m_campaign_strategy(settings, "2026-07-20")
        hybrid = crypto.crypto_15m_campaign_strategy(settings, "2026-07-21")
        self.assertEqual(legacy["goals"], [5.0, 2.5, 1.25])
        self.assertFalse(legacy["partial_recovery_enabled"])
        self.assertEqual(hybrid["goals"], [2.0] * 5)
        self.assertTrue(hybrid["partial_recovery_enabled"])

    def test_dated_goal_override_grandfathers_completed_cycle_then_expires(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CYCLE_GOALS": "2,2,2,2,2,2,2,2,2,2",
            "CRYPTO_15M_CYCLE_GOALS_OVERRIDE_DATE": "2026-07-21",
            "CRYPTO_15M_CYCLE_GOALS_OVERRIDE": "2,2,2,2,2,1,2,2,2,2",
            "CRYPTO_15M_HYBRID_EFFECTIVE_DATE": "2026-07-20",
        })

        today = crypto.crypto_15m_campaign_strategy(settings, "2026-07-21")
        tomorrow = crypto.crypto_15m_campaign_strategy(settings, "2026-07-22")

        self.assertEqual(today["goals"], [2.0, 2.0, 2.0, 2.0, 2.0, 1.0, 2.0, 2.0, 2.0, 2.0])
        self.assertEqual(today["goal_schedule_source"], "dated_override")
        self.assertEqual(tomorrow["goals"], [2.0] * 10)
        self.assertEqual(tomorrow["goal_schedule_source"], "CRYPTO_15M_CYCLE_GOALS")

    def test_each_fresh_bot_uses_its_own_configured_profit_per_cycle(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CYCLE_GOALS": "2,2,2",
            "CRYPTO_15M_BOT_1_CYCLE_PROFIT": "1.50",
            "CRYPTO_15M_BOT_2_CYCLE_PROFIT": "3.25",
            "CRYPTO_15M_BOT_1_CYCLE_COUNT": "3",
            "CRYPTO_15M_BOT_2_CYCLE_COUNT": "5",
        })
        portfolio = {}

        bot_one = crypto.crypto_15m_campaign_strategy(
            settings,
            TODAY,
            bot_number=1,
            portfolio=portfolio,
        )
        bot_two = crypto.crypto_15m_campaign_strategy(
            settings,
            TODAY,
            bot_number=2,
            portfolio=portfolio,
        )

        self.assertEqual(bot_one["goals"], [1.5, 1.5, 1.5])
        self.assertEqual(bot_two["goals"], [3.25] * 5)
        self.assertFalse(bot_one["goal_schedule_locked"])
        self.assertFalse(bot_two["goal_schedule_locked"])
        self.assertEqual(bot_one["configured_cycle_profit_key"], "CRYPTO_15M_BOT_1_CYCLE_PROFIT")
        self.assertEqual(bot_two["configured_cycle_profit_key"], "CRYPTO_15M_BOT_2_CYCLE_PROFIT")
        self.assertEqual(bot_one["configured_cycle_count"], 3)
        self.assertEqual(bot_two["configured_cycle_count"], 5)
        self.assertEqual(bot_one["configured_cycle_count_key"], "CRYPTO_15M_BOT_1_CYCLE_COUNT")
        self.assertEqual(bot_two["configured_cycle_count_key"], "CRYPTO_15M_BOT_2_CYCLE_COUNT")

    def test_cycle_profit_locks_after_first_lane_bet_and_changes_next_day(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CYCLE_GOALS": "2,2,2",
            "CRYPTO_15M_BOT_1_CYCLE_PROFIT": "2",
            "CRYPTO_15M_BOT_1_CYCLE_COUNT": "3",
        })
        portfolio = {}
        initial = crypto.crypto_15m_campaign_strategy(
            settings,
            TODAY,
            bot_number=1,
            portfolio=portfolio,
        )
        portfolio["bets"] = [{
            "status": "open",
            "mode": "live",
            "strategy_owner": "crypto_15m_campaign",
            "placed_at": "2026-07-18T10:00:00Z",
            "bot_number": 1,
            "crypto_live_campaign": {
                "role": "primary",
                "campaign_date": TODAY,
                "campaign_id": "",
                "bot_number": 1,
            },
        }]
        settings["CRYPTO_15M_BOT_1_CYCLE_PROFIT"] = "5"
        settings["CRYPTO_15M_BOT_1_CYCLE_COUNT"] = "6"
        locked = crypto.crypto_15m_campaign_strategy(
            settings,
            TODAY,
            bot_number=1,
            portfolio=portfolio,
        )
        tomorrow = crypto.crypto_15m_campaign_strategy(
            settings,
            "2026-07-19",
            bot_number=1,
            portfolio=portfolio,
        )

        self.assertEqual(initial["goals"], [2.0, 2.0, 2.0])
        self.assertEqual(locked["goals"], [2.0, 2.0, 2.0])
        self.assertTrue(locked["goal_schedule_locked"])
        self.assertEqual(locked["configured_cycle_profit"], 5.0)
        self.assertEqual(locked["configured_cycle_count"], 6)
        self.assertEqual(locked["effective_cycle_count"], 3)
        self.assertEqual(tomorrow["goals"], [5.0] * 6)
        self.assertEqual(tomorrow["effective_cycle_count"], 6)
        self.assertFalse(tomorrow["goal_schedule_locked"])

    def test_per_bot_campaign_id_restarts_only_that_bot_at_cycle_one(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_BOT_1_CYCLE_PROFIT": "2",
            "CRYPTO_15M_BOT_1_CYCLE_COUNT": "3",
        })
        portfolio = {
            "balance": 100,
            "history": [{
                "status": "settled",
                "mode": "live",
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "2026-07-18T10:00:00Z",
                "settled_at": "2026-07-18T10:10:00Z",
                "result": "WIN",
                "profit": 2.0,
                "bot_number": 1,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "campaign_id": "",
                    "bot_number": 1,
                },
            }],
        }

        with patch.object(crypto, "local_date_key", return_value=TODAY):
            before = crypto.crypto_15m_campaign_lane_context(settings, portfolio, 1)
            settings["CRYPTO_15M_BOT_1_CAMPAIGN_ID"] = "manual-reset-1"
            after = crypto.crypto_15m_campaign_lane_context(settings, portfolio, 1)

        self.assertEqual(before["cycle_number"], 2)
        self.assertEqual(after["cycle_number"], 1)
        self.assertEqual(after["campaign_id"], "manual-reset-1")
        self.assertEqual(after["realized_profit"], 0.0)
        self.assertEqual(after["bot_daily_realized_profit"], 2.0)
        self.assertEqual(after["daily_loss_remaining"], 402.0)

    def test_crypto_campaign_bot_count_is_capped_at_five(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "99"
        self.assertEqual(crypto.configured_crypto_15m_campaign_bot_count(settings), 5)

    def test_recovery_is_payout_aware_without_legacy_stake_cap(self):
        portfolio = {
            "history": [
                {
                    "status": "settled",
                    "result": "LOSS",
                    "profit": -5.0,
                    "strategy_owner": "crypto_15m_campaign",
                    "placed_at": "p",
                    "settled_at": "s",
                    "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
                }
            ]
        }
        review = review_candidate(portfolio, candidate(price=50), TODAY, row_date)
        self.assertEqual(review["target_profit"], 10.0)
        self.assertEqual(review["applied_stake"], 10.5)
        plus_money = review_candidate(portfolio, candidate(price=40), TODAY, row_date)
        self.assertEqual(plus_money["applied_stake"], 7.2)

    def test_small_target_rounds_up_to_a_whole_contract_that_can_reach_goal(self):
        review = review_candidate({}, candidate(price=22), TODAY, row_date, cycle_goals=(2.53,))
        self.assertEqual(review["target_profit"], 2.53)
        self.assertEqual(review["required_contracts"], 4)
        self.assertEqual(review["applied_stake"], 0.88)
        self.assertGreaterEqual(
            review["applied_stake"] * review["profit_per_staked_dollar_after_fee"],
            review["target_profit"],
        )

    def test_one_open_bot_position_fills_crypto_slots(self):
        open_rows = []
        for index, asset in enumerate(("BTC", "ETH", "SOL")):
            open_rows.append(
                {
                    "status": "open",
                    "mode": "live",
                    "strategy_owner": "crypto_15m_campaign",
                    "asset": asset,
                    "event_ticker": f"EVENT-{index}",
                    "close_time": f"CLOSE-{index}",
                    "crypto_live_campaign": {
                        "role": "primary" if index == 0 else "support",
                        "campaign_date": TODAY,
                    },
                }
            )
        full = review_candidate({"bets": open_rows[:1]}, candidate(asset="DOGE"), TODAY, row_date)
        self.assertEqual(full["reason"], "campaign_open_slots_full")

    def test_market_helpers_identify_live_15m_window(self):
        now = datetime(2026, 7, 18, 15, 5, tzinfo=timezone.utc)
        market = {
            "event_ticker": "KXBTC15M-26JUL181115",
            "floor_strike": 65000,
            "close_time": (now + timedelta(minutes=10)).isoformat(),
        }
        self.assertTrue(crypto.is_15m_target_market(market))
        self.assertAlmostEqual(crypto.market_minutes_to_close(market, now=now), 10.0)

    def test_live_order_layer_rechecks_integer_price_band(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        row = candidate(price=81.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {"enabled": True}
        result = crypto.place_live_kalshi_order(settings, {"bets": []}, row, 5.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "crypto_15m_order_price_range")
        self.assertEqual((result["minimum_price"], result["maximum_price"]), (40, 70))

    def test_campaign_live_order_requires_full_recovery_fill(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_DRY_RUN": "true",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_TIME_IN_FORCE": "immediate_or_cancel",
        })
        row = candidate(price=50.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {"enabled": True}

        result = crypto.place_live_kalshi_order(settings, {"bets": []}, row, 20.0)

        self.assertEqual(result["error"], "live_dry_run")
        self.assertEqual(result["request"]["count"], "40.00")
        self.assertEqual(result["request"]["time_in_force"], "fill_or_kill")

    def test_non_campaign_live_order_keeps_configured_time_in_force(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "false",
            "CRYPTO_LIVE_DRY_RUN": "true",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_TIME_IN_FORCE": "immediate_or_cancel",
        })
        row = candidate(price=21.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {"enabled": False}

        result = crypto.place_live_kalshi_order(settings, {"bets": []}, row, 5.0)

        self.assertEqual(result["request"]["time_in_force"], "immediate_or_cancel")

    def test_fresh_depth_counts_opposite_side_volume_at_the_limit(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        yes_orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.3400", "2.00"], ["0.3500", "4.00"], ["0.4000", "3.00"]],
                "no_dollars": [["0.3000", "20.00"], ["0.3500", "5.00"], ["0.4100", "4.00"]],
            }
        }
        with patch.object(crypto, "http_json", return_value=yes_orderbook):
            review = crypto.fresh_kalshi_executable_depth(settings, "TEST", "yes", 65, 8)

        self.assertTrue(review["ok"])
        self.assertEqual(review["contra_side"], "no")
        self.assertEqual(review["available_contracts"], 9.0)
        self.assertEqual(review["required_contracts"], 8)

    @patch.object(crypto, "kalshi_credentials", new=lambda *_: {"api_key_id": "offline-fixture", "private_key_path": "unused-offline-fixture", "private_key_pem": ""})
    def test_fresh_depth_rejects_before_fok_when_full_size_is_unavailable(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
        })
        row = candidate(price=65.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {"enabled": True}
        depth = {
            "ok": False,
            "error": "live_order_insufficient_fresh_depth",
            "required_contracts": 6,
            "available_contracts": 2.0,
        }
        with patch.object(crypto, "allow_live_trading", return_value=True), patch.object(
            crypto, "fresh_kalshi_executable_depth", return_value=depth
        ), patch.object(crypto, "kalshi_private_request") as submit:
            result = crypto.place_live_kalshi_order(settings, {"bets": []}, row, 3.90)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "live_order_insufficient_fresh_depth")
        self.assertEqual(result["liquidity_preflight"]["available_contracts"], 2.0)
        submit.assert_not_called()

    def test_immediate_depth_check_retries_largest_revalidated_lower_unit(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED": "true",
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
        })
        row = candidate(price=50.0, edge=4, confidence=88)
        row.update({
            "side": "yes",
            "market_lane": "crypto_15m",
            "crypto_units": {
                "eligible": True,
                "target_units": 3,
                "applied_total_units": 3,
                "unit_size": 10.0,
                "applied_stake": 30.0,
            },
            "crypto_live_campaign": {
                "enabled": True,
                "eligible": True,
                "bot_number": 1,
                "applied_stake": 30.0,
                "applied_contracts": 60,
            },
        })
        first_depth = {
            "ok": False,
            "error": "live_order_insufficient_fresh_depth",
            "required_contracts": 60,
            "available_contracts": 45.0,
        }
        second_depth = {
            "ok": True,
            "error": None,
            "required_contracts": 40,
            "available_contracts": 45.0,
            "full_size_entry_price_cents": 50,
            "limit_price_cents": 50,
        }
        downshift = {
            "enabled": True,
            "attempted": True,
            "active": True,
            "trigger": "immediate_pre_submit_depth",
            "original_units": 3,
            "applied_units": 2,
            "refreshed_price_cents": 50,
        }

        def apply_downshift(_settings, _portfolio, mutable, *_args, **_kwargs):
            mutable["crypto_units"].update({
                "target_units": 2,
                "applied_total_units": 2,
                "applied_stake": 20.0,
            })
            mutable["crypto_live_campaign"].update({
                "applied_stake": 20.0,
                "applied_contracts": 40,
            })
            mutable["live_unit_depth_downshift"] = downshift
            return {**second_depth, "unit_depth_downshift": downshift}

        with patch.object(
            crypto, "crypto_live_readiness", return_value={"ok": True}
        ), patch.object(
            crypto,
            "fresh_kalshi_executable_depth",
            side_effect=[first_depth, second_depth],
        ) as fresh_depth, patch.object(
            crypto,
            "try_live_unit_depth_downshift_refresh",
            side_effect=apply_downshift,
        ) as downshift_review, patch.object(
            crypto,
            "kalshi_private_request",
            return_value=({"order": {"fill_count": "40"}}, {}),
        ) as submit:
            result = crypto.place_live_kalshi_order(
                settings, {"bets": []}, row, 30.0
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["contracts"], 40)
        self.assertEqual(result["requested_stake"], 20.0)
        self.assertEqual(result["unit_depth_downshift"]["applied_units"], 2)
        self.assertEqual(fresh_depth.call_count, 2)
        downshift_review.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["body"]["count"], "40.00")

    def test_shared_cap_fill_reconciles_units_and_removes_unexecuted_recovery(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_MIN_ENTRY_PRICE_CENTS": "35",
            "CRYPTO_15M_SIMPLE_MIN_PRICE_CENTS": "35",
            "CRYPTO_15M_TIERED_MIN_PRICE_CENTS": "35",
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "true",
            "CRYPTO_LIVE_DEPTH_PREFLIGHT_ENABLED": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
        })
        row = candidate(price=38.0, edge=8, confidence=90)
        bounded = {
            "enabled": True,
            "active": True,
            "reason": "bounded_recovery_bonus",
            "base_units": 2,
            "bonus_units": 1.5,
            "total_units": 3.5,
            "unit_size": 10.0,
            "requested_stake": 35.0,
            "estimated_profit_per_unit_after_fee": 15.0,
            "allocated_recovery_profit": 22.5,
        }
        row.update({
            "side": "yes",
            "market_lane": "crypto_15m",
            "crypto_units": {
                "eligible": True,
                "target_units": 2,
                "base_units": 2,
                "recovery_bonus_units": 1.5,
                "applied_total_units": 3.5,
                "unit_size": 10.0,
                "applied_stake": 35.0,
                "bounded_recovery": bounded,
            },
            "crypto_live_campaign": {
                "enabled": True,
                "eligible": True,
                "bot_number": 1,
                "applied_stake": 35.0,
                "applied_contracts": 92,
                "bounded_unit_recovery": bounded,
            },
        })
        reservation = {
            "ok": True,
            "reservation_id": "cap-test",
            "approved_stake": 15.2,
        }
        with patch.object(
            crypto, "reserve_live_order", return_value=reservation
        ), patch.object(
            crypto, "finalize_reservation"
        ) as finalize, patch.object(
            crypto, "crypto_live_readiness", return_value={"ok": True}
        ), patch.object(
            crypto,
            "kalshi_private_request",
            return_value=({"order": {"fill_count": "40"}}, {}),
        ):
            result = crypto.place_live_kalshi_order(
                settings, {"bets": []}, row, 35.0
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["actual_stake"], 15.2)
        self.assertEqual(result["contracts"], 40)
        self.assertEqual(row["crypto_units"]["model_selected_units"], 3.5)
        self.assertEqual(row["crypto_units"]["applied_total_units"], 1.52)
        self.assertEqual(row["crypto_units"]["recovery_bonus_units"], 0.0)
        self.assertFalse(
            row["crypto_units"]["bounded_recovery"]["active"]
        )
        self.assertEqual(
            row["crypto_units"]["bounded_recovery"]["reason"],
            "post_fill_recovery_not_executed",
        )
        self.assertEqual(row["crypto_live_campaign"]["applied_stake"], 15.2)
        self.assertEqual(row["crypto_live_campaign"]["applied_contracts"], 40)
        self.assertEqual(result["unit_accounting"]["executed_units"], 1.52)
        finalize.assert_called_once_with("cap-test", "filled", 15.2)

        settled = {
            **row,
            "status": "settled",
            "mode": "live",
            "contracts": 40,
            "stake": 15.2,
            "fee": 0.5,
            "profit": -15.7,
            "result": "LOSS",
        }
        self.assertIsNone(crypto._recovery_counterfactual_row(settled))

    def test_reconciled_recovery_counterfactual_uses_exact_base_contracts(self):
        row = candidate(price=38.0)
        bounded = {
            "active": True,
            "base_units": 2,
            "bonus_units": 1.5,
            "total_units": 3.5,
            "unit_size": 10.0,
            "requested_stake": 35.0,
            "estimated_profit_per_unit_after_fee": 15.0,
        }
        row.update({
            "side": "yes",
            "crypto_units": {
                "target_units": 2,
                "base_units": 2,
                "recovery_bonus_units": 1.5,
                "applied_total_units": 3.5,
                "unit_size": 10.0,
                "bounded_recovery": bounded,
            },
            "crypto_live_campaign": {
                "enabled": True,
                "bounded_unit_recovery": bounded,
            },
        })
        live_order = {
            "actual_stake": 22.8,
            "contracts": 60,
            "executed_price": 38,
        }

        accounting = crypto.reconcile_live_unit_accounting(row, live_order)

        self.assertEqual(accounting["executed_base_contracts"], 52)
        self.assertEqual(accounting["executed_recovery_bonus_contracts"], 8)
        self.assertEqual(row["crypto_units"]["recovery_bonus_units"], 0.304)
        self.assertTrue(row["crypto_units"]["bounded_recovery"]["active"])
        settled = {
            **row,
            "status": "settled",
            "mode": "live",
            "contracts": 60,
            "stake": 22.8,
            "fee": 1.0,
            "profit": -23.8,
            "result": "LOSS",
        }
        comparison = crypto._recovery_counterfactual_row(settled)
        self.assertEqual(comparison["counterfactual_base_contracts"], 52.0)
        self.assertEqual(comparison["actual_contracts"], 60.0)

    def test_portfolio_unit_accounting_repair_is_one_time_and_preserves_rows(self):
        bounded = {
            "active": True,
            "base_units": 2,
            "bonus_units": 1.5,
            "total_units": 3.5,
            "unit_size": 10.0,
        }
        row = candidate(price=38.0)
        row.update({
            "id": "historical-fill",
            "mode": "live",
            "status": "settled",
            "strategy_owner": "crypto_15m_campaign",
            "entry_price": 39.0,
            "stake": 15.2,
            "contracts": 40,
            "crypto_units": {
                "target_units": 2,
                "base_units": 2,
                "recovery_bonus_units": 1.5,
                "applied_total_units": 3.5,
                "unit_size": 10.0,
                "bounded_recovery": bounded,
            },
            "crypto_live_campaign": {
                "enabled": True,
                "bounded_unit_recovery": bounded,
            },
        })
        portfolio = {"bets": [row]}

        repaired = crypto.reconcile_portfolio_live_unit_accounting(portfolio)
        repaired_again = crypto.reconcile_portfolio_live_unit_accounting(portfolio)

        self.assertEqual([item["id"] for item in repaired], ["historical-fill"])
        self.assertEqual(repaired_again, [])
        self.assertEqual(len(portfolio["bets"]), 1)
        self.assertEqual(row["reported_entry_price"], 39.0)
        self.assertEqual(row["entry_price"], 38.0)
        self.assertEqual(row["crypto_units"]["applied_total_units"], 1.52)
        self.assertEqual(row["crypto_units"]["recovery_bonus_units"], 0.0)

    def test_depth_downshift_tries_whole_units_largest_first(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED": "true",
            "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_MIN_UNITS": "1",
        })
        row = candidate(price=50.0, edge=5, confidence=95)
        row.update({
            "side": "yes",
            "market_lane": "crypto_15m",
            "crypto_units": {
                "eligible": True,
                "target_units": 4,
                "applied_total_units": 4,
                "unit_size": 10.0,
                "applied_stake": 40.0,
            },
            "crypto_live_campaign": {
                "enabled": True,
                "eligible": True,
                "bot_number": 1,
                "applied_stake": 40.0,
                "applied_contracts": 80,
            },
        })

        def campaign_review(_settings, _portfolio, mutable, bot_number=None):
            cap = crypto.crypto_execution_unit_cap(mutable)
            mutable["crypto_units"] = {
                "eligible": True,
                "model_target_units": 4,
                "target_units": cap,
                "applied_total_units": cap,
                "unit_size": 10.0,
                "applied_stake": cap * 10.0,
            }
            review = {
                "enabled": True,
                "eligible": True,
                "reason": "eligible",
                "bot_number": bot_number,
                "applied_stake": cap * 10.0,
                "applied_contracts": cap * 20,
            }
            mutable["crypto_live_campaign"] = review
            return review

        def refreshed_quote(_settings, _portfolio, mutable, **_kwargs):
            cap = crypto.crypto_execution_unit_cap(mutable)
            if cap == 3:
                return {
                    "ok": False,
                    "error": "live_quote_no_full_size_depth",
                    "required_contracts": 60,
                    "available_contracts": 50,
                }
            return {
                "ok": True,
                "error": None,
                "refreshed_price_cents": 50,
                "required_contracts": 40,
                "available_contracts": 50,
            }

        with patch.object(
            crypto, "apply_candidate_pricing", return_value={}
        ), patch.object(
            crypto, "crypto_15m_campaign_review", side_effect=campaign_review
        ), patch.object(
            crypto,
            "crypto_15m_directional_confirmation_review",
            return_value={"enabled": False, "ok": True},
        ), patch.object(
            crypto,
            "refresh_live_campaign_candidate_quote",
            side_effect=refreshed_quote,
        ):
            result = crypto.try_live_unit_depth_downshift_refresh(
                settings,
                {"balance": 1000.0, "bets": []},
                row,
                "live_quote_no_full_size_depth",
                reference_price_cents=50,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["unit_depth_downshift"]["applied_units"], 2)
        self.assertEqual(
            [attempt["maximum_units"] for attempt in result["unit_depth_downshift"]["attempts"]],
            [3, 2],
        )
        self.assertEqual(row["crypto_live_campaign"]["applied_stake"], 20.0)
        self.assertEqual(crypto.crypto_execution_unit_cap(row), 2)

    def test_disabled_depth_downshift_preserves_full_size_rejection(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED": "false",
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
        })
        row = candidate(price=50.0)
        row.update({
            "side": "yes",
            "market_lane": "crypto_15m",
            "crypto_units": {
                "eligible": True,
                "target_units": 2,
                "applied_total_units": 2,
                "unit_size": 10.0,
            },
            "crypto_live_campaign": {"enabled": True, "eligible": True},
        })
        depth = {
            "ok": False,
            "error": "live_order_insufficient_fresh_depth",
            "required_contracts": 40,
            "available_contracts": 20.0,
        }
        with patch.object(
            crypto, "crypto_live_readiness", return_value={"ok": True}
        ), patch.object(
            crypto, "fresh_kalshi_executable_depth", return_value=depth
        ), patch.object(
            crypto, "try_live_unit_depth_downshift_refresh"
        ) as downshift_review, patch.object(
            crypto, "kalshi_private_request"
        ) as submit:
            result = crypto.place_live_kalshi_order(
                settings, {"bets": []}, row, 20.0
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "live_order_insufficient_fresh_depth")
        downshift_review.assert_not_called()
        submit.assert_not_called()

    @patch.object(crypto, "kalshi_credentials", new=lambda *_: {"api_key_id": "offline-fixture", "private_key_path": "unused-offline-fixture", "private_key_pem": ""})
    def test_fok_insufficient_volume_retries_once_after_fresh_safe_depth(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_LIVE_FOK_SAFE_RETRY_ENABLED": "true",
        })
        row = candidate(price=65.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {
            "enabled": True,
            "eligible": True,
            "bot_number": 1,
            "applied_contracts": 6,
        }
        depth = {
            "ok": True,
            "required_contracts": 6,
            "available_contracts": 8,
            "full_size_entry_price_cents": 65,
            "limit_price_cents": 65,
        }
        first_error = RuntimeError(
            "HTTP 409 fill_or_kill_insufficient_resting_volume"
        )
        filled = ({"order": {"fill_count": "6"}}, {})
        with patch.object(crypto, "allow_live_trading", return_value=True), patch.object(
            crypto,
            "fresh_kalshi_executable_depth",
            return_value=depth,
        ), patch.object(
            crypto,
            "refresh_live_campaign_candidate_quote",
            return_value={
                **depth,
                "enabled": True,
                "max_adverse_move_cents": 3,
            },
        ), patch.object(
            crypto,
            "kalshi_private_request",
            side_effect=[first_error, filled],
        ) as submit:
            result = crypto.place_live_kalshi_order(
                settings,
                {"bets": []},
                row,
                3.90,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["fok_retry"]["attempted"])
        self.assertTrue(result["fok_retry"]["submitted"])
        self.assertEqual(submit.call_count, 2)
        first_body = submit.call_args_list[0].kwargs["body"]
        second_body = submit.call_args_list[1].kwargs["body"]
        self.assertNotEqual(
            first_body["client_order_id"],
            second_body["client_order_id"],
        )
        self.assertEqual(second_body["time_in_force"], "fill_or_kill")
        self.assertEqual(second_body["price"], first_body["price"])
        self.assertEqual(second_body["count"], first_body["count"])
        self.assertTrue(result["fok_retry"]["fully_revalidated"])

    @patch.object(crypto, "kalshi_credentials", new=lambda *_: {"api_key_id": "offline-fixture", "private_key_path": "unused-offline-fixture", "private_key_pem": ""})
    def test_fok_retry_can_reprice_one_cent_after_full_revalidation(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_LIVE_FOK_SAFE_RETRY_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "3",
        })
        row = candidate(price=65.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {
            "enabled": True,
            "eligible": True,
            "bot_number": 1,
            "applied_contracts": 6,
        }
        first_depth = {
            "ok": True,
            "required_contracts": 6,
            "available_contracts": 8,
            "full_size_entry_price_cents": 65,
            "limit_price_cents": 65,
        }

        def reprice(_settings, _portfolio, target):
            target["entry_price"] = 66.0
            target["edge"] = 7.0
            target["crypto_live_campaign"] = {
                **target["crypto_live_campaign"],
                "eligible": True,
                "applied_contracts": 7,
                "applied_stake": 4.62,
            }
            return {
                "ok": True,
                "enabled": True,
                "required_contracts": 7,
                "available_contracts": 9,
                "refreshed_price_cents": 66,
                "max_adverse_move_cents": 3,
            }

        first_error = RuntimeError(
            "HTTP 409 fill_or_kill_insufficient_resting_volume"
        )
        with patch.object(crypto, "allow_live_trading", return_value=True), patch.object(
            crypto,
            "fresh_kalshi_executable_depth",
            return_value=first_depth,
        ), patch.object(
            crypto,
            "refresh_live_campaign_candidate_quote",
            side_effect=reprice,
        ), patch.object(
            crypto,
            "kalshi_private_request",
            side_effect=[first_error, ({"order": {"fill_count": "7"}}, {})],
        ) as submit:
            result = crypto.place_live_kalshi_order(
                settings,
                {"bets": []},
                row,
                3.90,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["price"], 66)
        self.assertEqual(result["contracts"], 7.0)
        self.assertEqual(submit.call_args_list[1].kwargs["body"]["price"], "0.6600")
        self.assertEqual(submit.call_args_list[1].kwargs["body"]["count"], "7.00")
        self.assertEqual(result["fok_retry"]["adverse_move_cents"], 1.0)

    @patch.object(crypto, "kalshi_credentials", new=lambda *_: {"api_key_id": "offline-fixture", "private_key_path": "unused-offline-fixture", "private_key_pem": ""})
    def test_live_order_exception_preserves_sanitized_exchange_diagnostics(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
        })
        row = candidate(price=65.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {"enabled": True, "bot_number": 1}
        depth = {
            "ok": True,
            "required_contracts": 6,
            "available_contracts": 8,
            "full_size_entry_price_cents": 65,
            "limit_price_cents": 65,
        }
        exchange_error = RuntimeError(
            'HTTP 422: {"error":{"code":"invalid_order",'
            '"message":"exchange rejected exact payload"}}?token=secret'
        )
        with patch.object(crypto, "allow_live_trading", return_value=True), patch.object(
            crypto, "fresh_kalshi_executable_depth", return_value=depth
        ), patch.object(
            crypto, "kalshi_private_request", side_effect=exchange_error
        ):
            result = crypto.place_live_kalshi_order(
                settings, {"bets": []}, row, 3.90
            )

        self.assertEqual(result["error"], "live_order_exception")
        self.assertEqual(
            result["exception_details"]["exception_type"],
            "RuntimeError",
        )
        self.assertIn(
            "exchange rejected exact payload",
            result["exception_details"]["exchange_error"],
        )
        self.assertNotIn("token=secret", result["exception_details"]["exchange_error"])

    @patch.object(crypto, "kalshi_credentials", new=lambda *_: {"api_key_id": "offline-fixture", "private_key_path": "unused-offline-fixture", "private_key_pem": ""})
    def test_final_depth_preflight_never_reuses_an_earlier_quote(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_LIVE_QUOTE_REUSE_SECONDS": "3",
        })
        row = candidate(price=65.0)
        row["side"] = "yes"
        row["crypto_live_campaign"] = {
            "enabled": True,
            "bot_number": 1,
            "applied_contracts": 6,
        }
        row["live_quote_refresh"] = {
            "ok": True,
            "enabled": True,
            "fetched_at": crypto.iso_now(),
            "refreshed_price_cents": 65,
            "required_contracts": 6,
            "available_contracts": 10,
            "full_size_entry_price_cents": 65,
            "campaign_review": {"bot_number": 1},
        }
        immediate_depth = {
            "ok": True,
            "required_contracts": 6,
            "available_contracts": 10,
            "full_size_entry_price_cents": 65,
            "limit_price_cents": 65,
        }
        with patch.object(crypto, "allow_live_trading", return_value=True), patch.object(
            crypto,
            "fresh_kalshi_executable_depth",
            return_value=immediate_depth,
        ) as fresh_depth, patch.object(
            crypto,
            "kalshi_private_request",
            return_value=({"order": {"fill_count": "6"}}, {}),
        ):
            result = crypto.place_live_kalshi_order(
                settings,
                {"bets": []},
                row,
                3.90,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["liquidity_preflight"]["source"],
            "immediate_pre_submit_depth",
        )
        fresh_depth.assert_called_once()

    def test_fresh_quote_batch_reranks_by_refreshed_edge(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_PRIORITY_ASSET": "",
            "CRYPTO_LIVE_RERANK_MAX_CANDIDATES": "5",
            "CRYPTO_FETCH_MAX_WORKERS": "2",
        })
        first = candidate(edge=12.0, confidence=80.0, asset="BTC")
        first["ticker"] = "FIRST"
        first["skip_reasons"] = []
        second = candidate(edge=8.0, confidence=80.0, asset="ETH")
        second["ticker"] = "SECOND"
        second["skip_reasons"] = []

        def refresh(_settings, _portfolio, row):
            row["edge"] = 4.0 if row["ticker"] == "FIRST" else 10.0
            return {
                "ok": True,
                "enabled": True,
                "fetched_at": crypto.iso_now(),
                "refreshed_price_cents": row["entry_price"],
                "required_contracts": 1,
                "campaign_review": {"bot_number": 1},
            }

        with patch.object(
            crypto,
            "refresh_live_campaign_candidate_quote",
            side_effect=refresh,
        ):
            summary = crypto.refresh_and_rerank_live_candidates(
                settings,
                {"bets": []},
                [first, second],
            )

        self.assertEqual(summary["refreshed"], 2)
        rows = [first, second]
        rows.sort(
            key=lambda row: crypto.crypto_candidate_sort_key(settings, row),
            reverse=True,
        )
        self.assertEqual(rows[0]["ticker"], "SECOND")

    def test_fresh_quote_batch_selects_best_candidate_before_refresh(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_PRIORITY_ASSET": "",
            "CRYPTO_LIVE_RERANK_MAX_CANDIDATES": "1",
            "CRYPTO_FETCH_MAX_WORKERS": "1",
        })
        lower = candidate(edge=5.0, confidence=70.0, asset="ETH")
        lower["ticker"] = "LOWER"
        lower["skip_reasons"] = []
        higher = candidate(edge=10.0, confidence=70.0, asset="SOL")
        higher["ticker"] = "HIGHER"
        higher["skip_reasons"] = []

        def refresh(_settings, _portfolio, row):
            return {
                "ok": True,
                "enabled": True,
                "fetched_at": crypto.iso_now(),
                "refreshed_price_cents": row["entry_price"],
                "required_contracts": 1,
                "campaign_review": {"bot_number": 1},
            }

        with patch.object(
            crypto,
            "refresh_live_campaign_candidate_quote",
            side_effect=refresh,
        ) as refresh_mock:
            summary = crypto.refresh_and_rerank_live_candidates(
                settings,
                {"bets": []},
                [lower, higher],
            )

        self.assertEqual(summary["requested"], 1)
        self.assertEqual(refresh_mock.call_args.args[2]["ticker"], "HIGHER")

    def test_fresh_depth_reports_nearby_price_when_old_limit_is_gone(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.2800", "10.00"]],
                "no_dollars": [["0.6000", "25.00"]],
            }
        }
        with patch.object(crypto, "http_json", return_value=orderbook):
            review = crypto.fresh_kalshi_executable_depth(
                settings,
                "TEST",
                "yes",
                39,
                4,
            )

        self.assertFalse(review["ok"])
        self.assertEqual(review["available_contracts"], 0.0)
        self.assertEqual(review["best_contra_bid_cents"], 60.0)
        self.assertEqual(review["best_entry_price_cents"], 40.0)
        self.assertEqual(review["full_size_entry_price_cents"], 40.0)
        self.assertEqual(review["price_move_beyond_limit_cents"], 1.0)

    def test_live_quote_refresh_reprices_and_revalidates_campaign(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_MIN_EDGE": "4",
            "CRYPTO_15M_MIN_CONFIDENCE": "60",
            "CRYPTO_15M_MIN_ENTRY_PRICE_CENTS": "35",
            "CRYPTO_15M_MAX_ENTRY_PRICE_CENTS": "70",
            "CRYPTO_LIVE_QUOTE_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "2",
            "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "3",
            "CRYPTO_15M_PRIORITY_ASSET": "BTC",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "false",
        })
        portfolio = {"balance": 100.0, "bets": []}
        row = candidate(price=50.0, edge=8.25, confidence=84.0, asset="BTC")
        row.update({"side": "yes", "model_prob_yes": 60.0})
        crypto.crypto_15m_campaign_review(settings, portfolio, row)
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.4000", "100.00"]],
                "no_dollars": [["0.4800", "100.00"]],
            }
        }

        with patch.object(crypto, "http_json", return_value=orderbook):
            review = crypto.refresh_live_campaign_candidate_quote(
                settings,
                portfolio,
                row,
            )

        self.assertTrue(review["ok"])
        self.assertTrue(review["priority_asset"])
        self.assertEqual(review["original_price_cents"], 50.0)
        self.assertEqual(review["refreshed_price_cents"], 52.0)
        self.assertEqual(row["entry_price"], 52.0)
        self.assertGreaterEqual(row["edge"], 4.0)
        self.assertTrue(row["crypto_live_campaign"]["eligible"])
        self.assertGreater(review["available_contracts"], review["required_contracts"])

    def test_live_quote_refresh_recomputes_and_enforces_disagreement_guard(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_MAX_UNCONFIRMED_MARKET_GAP": "8",
            "CRYPTO_15M_FLOW_CONFIRMATION_MIN_STRENGTH": "0.22",
            "CRYPTO_15M_FLOW_CONFIRMATION_MIN_SOURCES": "3",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "false",
            "CRYPTO_LIVE_QUOTE_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "6",
            "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "6",
        })
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50.0, edge=20.0, confidence=95.0, asset="BTC")
        row.update({
            "market_lane": "crypto_15m",
            "side": "yes",
            "model_prob_yes": 62.0,
            "probability": {
                "prob": 62.0,
                "raw_market_gap": 4.0,
                "model_disagreement": 0.0,
            },
            "flow_review": {
                "direction": "yes",
                "strength": 0.30,
                "source_count": 3,
                "confirming_source_count": 2,
                "yes_source_count": 2,
                "cross_exchange_dispersion_bps": 2.0,
            },
            "data_quality": {"score": 0.99},
        })
        row["disagreement_guard"] = crypto.crypto_15m_disagreement_review(settings, row)
        self.assertTrue(row["disagreement_guard"]["ok"])
        crypto.crypto_15m_campaign_review(settings, portfolio, row)
        orderbook = {"orderbook_fp": {"no_dollars": [["0.5000", "100.00"]]}}

        def refresh_probability(_settings, refreshed, _orderbook):
            refreshed["probability"] = {
                "prob": 70.0,
                "raw_market_gap": 12.0,
                "model_disagreement": 0.0,
            }
            refreshed["model_prob_yes"] = 70.0
            return {"ok": True, "enabled": True, "fresh_confidence": 95.0}

        with patch.object(crypto, "http_json", return_value=orderbook), patch.object(
            crypto,
            "refresh_live_underlying_probability",
            side_effect=refresh_probability,
        ):
            review = crypto.refresh_live_campaign_candidate_quote(
                settings,
                portfolio,
                row,
                allow_unit_downshift=False,
            )

        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "live_quote_revalidation_failed")
        self.assertEqual(review["revalidation_reason"], "model_market_disagreement_unconfirmed")
        self.assertEqual(review["refreshed_disagreement_guard"]["raw_market_gap"], 12.0)
        self.assertFalse(row["disagreement_guard"]["ok"])

    def test_live_quote_refresh_rejects_excessive_slippage(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "2",
            "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "3",
            "CRYPTO_15M_PRIORITY_ASSET": "BTC",
        })
        portfolio = {"balance": 100.0, "bets": []}
        row = candidate(price=50.0, edge=20.0, confidence=84.0, asset="BTC")
        row.update({"side": "yes", "model_prob_yes": 75.0})
        crypto.crypto_15m_campaign_review(settings, portfolio, row)
        orderbook = {
            "orderbook_fp": {
                "no_dollars": [["0.4500", "100.00"]],
            }
        }

        with patch.object(crypto, "http_json", return_value=orderbook):
            review = crypto.refresh_live_campaign_candidate_quote(
                settings,
                portfolio,
                row,
            )

        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "live_quote_slippage_limit")
        self.assertEqual(review["refreshed_price_cents"], 55.0)
        self.assertEqual(review["adverse_move_cents"], 5.0)
        self.assertEqual(row["entry_price"], 55.0)
        self.assertEqual(review["refreshed_net_edge"], row["edge"])
        self.assertEqual(review["refreshed_edge_low"], row["edge_low"])
        self.assertEqual(review["refreshed_confidence"], row["confidence"])
        self.assertIsNotNone(review["refreshed_probability"]["p_yes"])
        self.assertIsNotNone(review["refreshed_probability"]["p_low"])
        self.assertIsNotNone(review["refreshed_probability"]["p_high"])
        row["live_quote_refresh"] = review
        quality = crypto.build_execution_quality_record(
            row,
            "quote_rejected",
            reason=review["error"],
            settings=settings,
        )
        self.assertEqual(quality["refreshed_net_edge"], row["edge"])

    def test_live_quote_refresh_accepts_six_cent_boundary(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "6",
            "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "6",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "false",
        })
        portfolio = {"balance": 100.0, "bets": []}
        row = candidate(price=50.0, edge=20.0, confidence=90.0, asset="SOL")
        row.update({"side": "yes", "model_prob_yes": 75.0})
        crypto.crypto_15m_campaign_review(settings, portfolio, row)
        orderbook = {"orderbook_fp": {"no_dollars": [["0.4400", "100.00"]]}}

        with patch.object(crypto, "http_json", return_value=orderbook):
            review = crypto.refresh_live_campaign_candidate_quote(
                settings,
                portfolio,
                row,
            )

        self.assertTrue(review["ok"])
        self.assertEqual(review["refreshed_price_cents"], 56.0)
        self.assertEqual(review["adverse_move_cents"], 6.0)

    def test_oversized_slippage_uses_stable_second_quote_without_depth_cooldown(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "2",
            "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "3",
            "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS": "15",
            "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS": "2",
            "CRYPTO_15M_PRIORITY_ASSET": "BTC",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "false",
        })
        portfolio = {"balance": 100.0, "bets": []}
        first = candidate(price=50.0, edge=20.0, confidence=90.0, asset="BTC")
        first.update({"side": "yes", "model_prob_yes": 75.0})
        crypto.crypto_15m_campaign_review(settings, portfolio, first)
        first_book = {"orderbook_fp": {"no_dollars": [["0.4500", "100.00"]]}}
        crypto.LIVE_DEPTH_RETRY_UNTIL.clear()
        crypto.LIVE_SLIPPAGE_RECONFIRMATIONS.clear()
        try:
            with patch.object(crypto, "http_json", return_value=first_book):
                first_review = crypto.refresh_live_campaign_candidate_quote(
                    settings,
                    portfolio,
                    first,
                )
            self.assertEqual(first_review["error"], "live_quote_slippage_limit")
            state = crypto.handle_live_quote_refresh_failure(
                settings,
                first,
                first_review,
                now=time.time() - 16,
            )
            self.assertEqual(state["status"], "waiting")
            self.assertNotIn(first["ticker"], crypto.LIVE_DEPTH_RETRY_UNTIL)

            second = candidate(price=50.0, edge=20.0, confidence=90.0, asset="BTC")
            second.update({"side": "yes", "model_prob_yes": 75.0})
            crypto.crypto_15m_campaign_review(settings, portfolio, second)
            second_book = {"orderbook_fp": {"no_dollars": [["0.4400", "100.00"]]}}
            with patch.object(crypto, "http_json", return_value=second_book):
                second_review = crypto.refresh_live_campaign_candidate_quote(
                    settings,
                    portfolio,
                    second,
                )

            self.assertTrue(second_review["ok"])
            self.assertTrue(second_review["reconfirmation"]["confirmed"])
            self.assertEqual(second_review["reconfirmation"]["confirmation_move_cents"], 1.0)
            self.assertTrue(
                second["live_slippage_reconfirmation"]["base_units_only"]
            )
            self.assertFalse(crypto.LIVE_SLIPPAGE_RECONFIRMATIONS)
        finally:
            crypto.LIVE_DEPTH_RETRY_UNTIL.clear()
            crypto.LIVE_SLIPPAGE_RECONFIRMATIONS.clear()

    def test_unstable_second_slippage_quote_restarts_wait(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "2",
            "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "3",
            "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS": "15",
            "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS": "2",
            "CRYPTO_15M_PRIORITY_ASSET": "BTC",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "false",
        })
        portfolio = {"balance": 100.0, "bets": []}
        first = candidate(price=50.0, asset="BTC")
        first.update({"side": "yes", "model_prob_yes": 80.0})
        crypto.LIVE_SLIPPAGE_RECONFIRMATIONS.clear()
        try:
            crypto.start_live_slippage_reconfirmation(
                settings,
                first,
                {
                    "error": "live_quote_slippage_limit",
                    "original_price_cents": 50.0,
                    "refreshed_price_cents": 55.0,
                    "adverse_move_cents": 5.0,
                },
                now=time.time() - 16,
            )
            second = candidate(price=50.0, asset="BTC")
            second.update({"side": "yes", "model_prob_yes": 80.0})
            crypto.crypto_15m_campaign_review(settings, portfolio, second)
            unstable_book = {
                "orderbook_fp": {"no_dollars": [["0.4200", "100.00"]]}
            }
            with patch.object(crypto, "http_json", return_value=unstable_book):
                review = crypto.refresh_live_campaign_candidate_quote(
                    settings,
                    portfolio,
                    second,
                    allow_unit_downshift=False,
                )
            self.assertFalse(review["ok"])
            self.assertEqual(
                review["error"],
                "live_quote_slippage_reconfirm_unstable",
            )
            restarted = crypto.handle_live_quote_refresh_failure(
                settings,
                second,
                review,
            )
            self.assertEqual(restarted["reference_price_cents"], 58.0)
            self.assertEqual(restarted["status"], "waiting")
        finally:
            crypto.LIVE_SLIPPAGE_RECONFIRMATIONS.clear()

    def test_live_underlying_refresh_reprices_probability_from_fresh_ws_mid(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_UNDERLYING_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_UNDERLYING_REQUIRE_FRESH": "true",
            "CRYPTO_LIVE_UNDERLYING_MAX_AGE_SECONDS": "3",
        })
        row = candidate(price=50.0, edge=10.0, confidence=80.0, asset="BTC")
        row.update({
            "market_lane": "crypto_15m",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "floor_strike": 100.0,
            "cap_strike": None,
            "volume": 1000,
            "side": "yes",
            "model_prob_yes": 50.0,
            "model": {
                "model_horizon": "15m",
                "spot": 100.0,
                "coinbase_spot": 100.0,
                "minute_vol": 0.001,
                "trend_drift_per_minute": 0.0,
                "reversion_drift_per_minute": 0.0,
                "drift_per_minute": 0.0,
                "fakeout_risk": 0.0,
                "underlying_flow_score": 0.0,
                "cross_exchange_dispersion_bps": 0.0,
            },
            "probability": {"prob": 50.0},
            "microstructure": {
                "coinbase": {"mid": 100.0},
                "settlement_proxy_spot": 100.0,
            },
            "learned_15m_model": {"active": False},
        })
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.5000", "100.00"]],
                "no_dollars": [["0.5000", "100.00"]],
            }
        }

        class FreshStream:
            @staticmethod
            def snapshot(_asset):
                return {
                    "source": "coinbase_ws",
                    "mid": 101.0,
                    "book_age_seconds": 0.2,
                }

        with patch.object(crypto, "COINBASE_MICROSTRUCTURE_STREAM", FreshStream()):
            review = crypto.refresh_live_underlying_probability(
                settings,
                row,
                orderbook,
            )

        self.assertTrue(review["ok"])
        self.assertEqual(review["fresh_proxy_spot"], 101.0)
        self.assertEqual(row["model"]["spot"], 101.0)
        self.assertGreater(row["model_prob_yes"], 50.0)
        self.assertEqual(review["book_age_seconds"], 0.2)
        quality = crypto.build_execution_quality_record(
            row,
            "quote_rejected",
            reason="test",
            settings=settings,
        )
        self.assertEqual(quality["underlying_book_age_seconds"], 0.2)
        self.assertEqual(quality["underlying_move_bps"], 100.0)
        self.assertGreater(quality["underlying_probability_move_pp"], 0.0)

    def test_live_underlying_refresh_fails_closed_on_stale_ws_book(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_LIVE_UNDERLYING_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_UNDERLYING_REQUIRE_FRESH": "true",
            "CRYPTO_LIVE_UNDERLYING_MAX_AGE_SECONDS": "3",
        })
        row = candidate(asset="BTC")
        row["market_lane"] = "crypto_15m"

        class StaleStream:
            @staticmethod
            def snapshot(_asset):
                return {
                    "source": "coinbase_ws",
                    "mid": 100.0,
                    "book_age_seconds": 8.0,
                }

        with patch.object(crypto, "COINBASE_MICROSTRUCTURE_STREAM", StaleStream()):
            review = crypto.refresh_live_underlying_probability(settings, row, {})

        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "live_underlying_refresh_unavailable")

    def test_bitcoin_is_prioritized_without_excluding_other_assets(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_PRIORITY_ASSET"] = "BTC"
        btc = candidate(edge=5.0, confidence=70.0, asset="BTC")
        sol = candidate(edge=20.0, confidence=90.0, asset="SOL")
        rows = sorted(
            [sol, btc],
            key=lambda row: crypto.crypto_candidate_sort_key(settings, row),
            reverse=True,
        )

        self.assertEqual(rows[0]["asset"], "BTC")
        self.assertEqual(rows[1]["asset"], "SOL")

    def test_live_bet_preserves_directional_confirmation_for_analytics(self):
        row = candidate(price=50.0)
        row.update({
            "side": "yes",
            "model_prob_yes": 65.0,
            "strategy_owner": "crypto_15m_campaign",
            "directional_confirmation": {
                "enabled": True,
                "ok": True,
                "selected_side": "yes",
                "selected_side_source_count": 2,
                "minimum_sources": 2,
            },
        })
        portfolio = {"bets": []}
        live_order = {
            "ok": True,
            "actual_stake": 2.5,
            "price": 50,
            "contracts": 5,
        }
        with patch.object(crypto, "log_line"), patch.object(
            crypto, "event_line"
        ), patch.object(crypto, "record_crypto_phase_two_placement"):
            bet = crypto.record_live_bet(portfolio, row, live_order, persist=False)

        self.assertEqual(
            bet["directional_confirmation"]["selected_side_source_count"],
            2,
        )
        self.assertEqual(
            portfolio["bets"][0]["directional_confirmation"]["minimum_sources"],
            2,
        )
        self.assertEqual(bet["bot_number"], 1)

    def test_execution_quality_tracks_initial_refresh_and_actual_fill(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        row = candidate(price=52.0, edge=11.0, confidence=74.0)
        row.update({
            "side": "yes",
            "model_prob_yes": 65.0,
            "execution_quote_origin": {
                "price_cents": 50.0,
                "net_edge": 13.0,
                "raw_edge": 15.0,
                "confidence": 74.0,
            },
            "live_quote_refresh": {
                "enabled": True,
                "ok": True,
                "original_price_cents": 50.0,
                "refreshed_price_cents": 52.0,
                "refreshed_net_edge": 11.0,
                "quote_age_seconds": 1.25,
                "fetched_at": crypto.iso_now(),
                "required_contracts": 10,
                "available_contracts": 25,
            },
        })
        live_order = {
            "ok": True,
            "price": 52.0,
            "executed_price": 51.0,
            "contracts": 10,
            "requested_stake": 5.2,
            "actual_stake": 5.1,
            "liquidity_preflight": {
                "required_contracts": 10,
                "available_contracts": 25,
            },
        }

        quality = crypto.build_execution_quality_record(
            row,
            "filled",
            live_order=live_order,
            settings=settings,
        )

        self.assertEqual(quality["initial_to_refresh_slippage_cents"], 2.0)
        self.assertEqual(quality["refresh_to_fill_slippage_cents"], -1.0)
        self.assertEqual(quality["total_slippage_cents"], 1.0)
        self.assertEqual(quality["price_improvement_cents"], 1.0)
        self.assertEqual(quality["fill_ratio"], 1.0)
        self.assertGreater(quality["post_fill_net_edge"], 2.0)

    def test_execution_quality_analytics_separates_fills_and_blocks(self):
        now = datetime.now(timezone.utc)
        records = [
            {
                "captured_at": now.isoformat(),
                "outcome": "filled",
                "asset": "BTC",
                "initial_quote_cents": 50,
                "total_slippage_cents": 1,
                "price_improvement_cents": 0.5,
                "post_fill_net_edge": 4,
                "quote_age_seconds": 1,
            },
            {
                "captured_at": now.isoformat(),
                "outcome": "quote_rejected",
                "reason": "live_quote_slippage_limit",
                "asset": "BTC",
                "initial_quote_cents": 50,
                "quote_age_seconds": 2,
            },
            {
                "captured_at": now.isoformat(),
                "outcome": "order_failed",
                "reason": "live_order_insufficient_fresh_depth",
                "asset": "ETH",
                "initial_quote_cents": 60,
                "quote_age_seconds": 3,
            },
        ]

        analytics = crypto.execution_quality_analytics(records=records, now=now)

        self.assertEqual(analytics["summary"]["attempts"], 3)
        self.assertEqual(analytics["summary"]["filled"], 1)
        self.assertEqual(analytics["summary"]["depth_blocks"], 1)
        self.assertEqual(analytics["summary"]["slippage_blocks"], 1)
        self.assertEqual(analytics["summary"]["price_improved_fills"], 1)
        self.assertEqual(analytics["failure_reasons"][0]["count"], 1)

    def test_shared_scan_assigns_next_candidate_to_available_second_bot(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "2"
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "crypto_15m_campaign",
                "asset": "BTC",
                "event_ticker": "BTC-OPEN",
                "close_time": "BTC-CLOSE",
                "bot_number": 1,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 1,
                },
            }],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_campaign_review(
                settings,
                portfolio,
                candidate(asset="DOGE"),
            )
            context = crypto.crypto_15m_campaign_context(settings, portfolio)

        self.assertTrue(review["eligible"])
        self.assertEqual(review["bot_number"], 2)
        self.assertEqual(review["max_open"], 1)
        self.assertEqual(context["configured_bot_count"], 2)
        self.assertEqual(context["available_bot_numbers"], [2])
        self.assertEqual(context["bot_live_open_count"], 1)
        self.assertEqual(context["max_open"], 2)

    def test_other_bot_cannot_take_same_exact_contract_on_opposite_side(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "2"
        row = candidate(asset="BTC")
        row["side"] = "yes"
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "ticker": row["ticker"],
                "side": "no",
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": 1,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 1,
                },
            }],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            review = crypto.crypto_15m_campaign_review(
                settings,
                portfolio,
                row,
                bot_number=2,
            )

        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_exact_contract_conflict")

    def test_first_choice_rotates_after_successful_campaign_placement(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "2"
        portfolio = {"crypto_15m_campaign_next_bot": 2}
        self.assertEqual(crypto.crypto_15m_campaign_bot_order(settings, portfolio), [2, 1])
        crypto.advance_crypto_15m_campaign_bot_order(portfolio, 2, 2)
        self.assertEqual(portfolio["crypto_15m_campaign_next_bot"], 1)
        self.assertEqual(crypto.crypto_15m_campaign_bot_order(settings, portfolio), [1, 2])

    def test_rotation_bootstraps_after_most_recent_campaign_bot(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "2"
        portfolio = {"bets": [{
            "placed_at": "2026-07-22T10:00:00Z",
            "strategy_owner": "crypto_15m_campaign",
            "bot_number": 1,
            "crypto_live_campaign": {"bot_number": 1},
        }]}
        self.assertEqual(crypto.crypto_15m_campaign_bot_order(settings, portfolio), [2, 1])

    def test_depth_failure_cooldown_is_per_ticker_and_expires(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_LIVE_DEPTH_RETRY_COOLDOWN_SECONDS"] = "60"
        row = candidate(asset="SOL")
        crypto.LIVE_DEPTH_RETRY_UNTIL.clear()
        try:
            self.assertEqual(crypto.start_live_depth_retry_cooldown(settings, row, now=100), 60.0)
            self.assertEqual(crypto.live_depth_retry_cooldown_remaining(settings, row, now=130), 30.0)
            other = candidate(asset="ETH")
            self.assertEqual(crypto.live_depth_retry_cooldown_remaining(settings, other, now=130), 0.0)
            self.assertEqual(crypto.live_depth_retry_cooldown_remaining(settings, row, now=161), 0.0)
        finally:
            crypto.LIVE_DEPTH_RETRY_UNTIL.clear()

    def test_pending_slippage_reconfirmation_defers_candidate_for_fifteen_seconds(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "false",
            "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS": "15",
        })
        portfolio = {"balance": 1000.0, "bets": []}
        row = candidate(price=50.0, edge=20.0, confidence=90.0)
        row.update({"side": "yes", "model_prob_yes": 75.0})
        crypto.LIVE_SLIPPAGE_RECONFIRMATIONS.clear()
        try:
            crypto.start_live_slippage_reconfirmation(
                settings,
                row,
                {
                    "error": "live_quote_slippage_limit",
                    "original_price_cents": 50.0,
                    "refreshed_price_cents": 58.0,
                    "adverse_move_cents": 8.0,
                },
            )
            self.assertFalse(crypto.candidate_passes(settings, portfolio, row))
            self.assertIn(
                "live_quote_slippage_reconfirm_wait",
                row["skip_reasons"],
            )
            self.assertNotIn("live_depth_retry_cooldown", row["skip_reasons"])
        finally:
            crypto.LIVE_SLIPPAGE_RECONFIRMATIONS.clear()

    def test_correlated_window_analytics_tracks_different_assets(self):
        rows = []
        for bot_number, asset, side, result, profit in (
            (1, "BTC", "no", "WIN", 2.0),
            (2, "ETH", "no", "LOSS", -1.0),
        ):
            rows.append({
                "status": "settled",
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": bot_number,
                "asset": asset,
                "ticker": f"{asset}-WINDOW",
                "close_time": "2026-07-18T15:15:00Z",
                "side": side,
                "result": result,
                "stake": 2.0,
                "profit": profit,
                "crypto_live_campaign": {"campaign_date": TODAY, "bot_number": bot_number},
            })
        analytics = crypto.correlated_campaign_window_analytics(rows)
        summary = analytics["summary"]
        self.assertEqual(summary["overlap_windows"], 1)
        self.assertEqual(summary["same_direction_windows"], 1)
        self.assertEqual(summary["exact_contract_overlap_windows"], 0)
        self.assertEqual(summary["overlap_profit"], 1.0)

    def test_four_loss_recovery_switches_to_partial_after_fourth_loss(self):
        self.assertEqual(crypto.DEFAULT_SETTINGS["CRYPTO_15M_FULL_RECOVERY_LOSSES"], "4")
        history = []
        for index in range(4):
            history.append({
                "status": "settled",
                "result": "LOSS",
                "profit": -1.0,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": f"p-{index}",
                "settled_at": f"s-{index}",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            })
        review = review_candidate(
            {"history": history},
            candidate(),
            TODAY,
            row_date,
            cycle_goals=(2.0,),
            partial_recovery_enabled=True,
            full_recovery_losses=4,
            partial_recovery_fraction=0.5,
        )
        self.assertEqual(review["recovery_mode"], "partial")
        self.assertTrue(review["partial_recovery_active"])

    def test_campaign_ledgers_are_isolated_by_bot_number(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "2"
        settings["CRYPTO_15M_CYCLE_GOALS"] = "2,2"
        portfolio = {
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
                "settled_at": "2026-07-18T10:00:00Z",
                "strategy_owner": "crypto_15m_campaign",
                "bot_number": 1,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 1,
                },
            }],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            bot_one = crypto.crypto_15m_campaign_context(settings, portfolio, bot_number=1)
            bot_two = crypto.crypto_15m_campaign_context(settings, portfolio, bot_number=2)
            aggregate = crypto.crypto_15m_campaign_context(settings, portfolio)

        self.assertEqual(bot_one["cycle_number"], 2)
        self.assertEqual(bot_one["realized_profit"], 2.0)
        self.assertEqual(bot_two["cycle_number"], 1)
        self.assertEqual(bot_two["realized_profit"], 0.0)
        self.assertEqual(aggregate["realized_profit"], 2.0)
        self.assertEqual([row["bot_number"] for row in aggregate["bots"]], [1, 2])

    def test_campaign_aggregate_sums_settlements_and_excludes_sub_unit_lane(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_CONTINUOUS_OPPORTUNITY_MODE": "true",
            "CRYPTO_15M_UNIT_SIZE_PCT": "1",
            "CRYPTO_15M_DAILY_LOSS_CAP": "400",
            "CRYPTO_15M_CROSS_BOT_CAP_RECOVERY_ENABLED": "false",
        })

        def settled(row_id, bot_number, result, profit):
            return {
                "id": row_id,
                "status": "settled",
                "mode": "live",
                "result": result,
                "profit": profit,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "2026-07-18T10:00:00Z",
                "settled_at": "2026-07-18T10:10:00Z",
                "bot_number": bot_number,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": bot_number,
                },
            }

        portfolio = {
            "balance": 1000.0,
            "bets": [
                settled("bot-1-win", 1, "WIN", 25),
                settled("bot-1-loss", 1, "LOSS", -20),
                settled("bot-2-win", 2, "WIN", 15),
                settled("bot-3-loss", 3, "LOSS", -396),
            ],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY), patch.object(
            crypto, "is_after_daily_reset", return_value=True
        ):
            aggregate = crypto.crypto_15m_campaign_context(settings, portfolio)

        self.assertEqual(aggregate["settled_count"], 4)
        self.assertEqual(aggregate["primary_wins"], 2)
        self.assertEqual(aggregate["primary_losses"], 2)
        self.assertEqual(aggregate["available_bot_numbers"], [1, 2])
        self.assertEqual(aggregate["unit_capacity_limited_bot_numbers"], [3])
        self.assertEqual(aggregate["bots"][2]["daily_loss_remaining"], 4.0)
        self.assertFalse(aggregate["bots"][2]["unit_capacity_available"])

    def test_current_strategy_performance_is_fingerprint_scoped(self):
        current = {"fingerprint": "current-v1"}

        def bet(row_id, fingerprint, result, profit, stake):
            return {
                "id": row_id,
                "ticker": row_id,
                "status": "settled",
                "mode": "live",
                "market_lane": "crypto_15m",
                "strategy_owner": "crypto_15m_campaign",
                "strategy_identity": {"fingerprint": fingerprint},
                "entry_price": 50,
                "confidence": 80,
                "result": result,
                "profit": profit,
                "stake": stake,
                "settled_at": "2026-07-18T10:10:00Z",
            }

        portfolio = {
            "bets": [
                bet("current-win", "current-v1", "WIN", 10, 20),
                bet("current-loss", "current-v1", "LOSS", -5, 10),
                bet("old-loss", "old-v1", "LOSS", -100, 100),
            ]
        }
        performance = crypto.current_strategy_performance(
            portfolio,
            current,
            crypto.DEFAULT_SETTINGS,
        )

        self.assertEqual(performance["settled_count"], 2)
        self.assertEqual(performance["wins"], 1)
        self.assertEqual(performance["losses"], 1)
        self.assertEqual(performance["profit"], 5.0)
        self.assertEqual(performance["roi_pct"], 16.6667)

    def test_reducing_bot_count_disables_new_lane_two_entries_but_tracks_open_bet(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_CAMPAIGN_BOT_COUNT"] = "1"
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "crypto_15m_campaign",
                "asset": "ETH",
                "event_ticker": "ETH-OPEN",
                "close_time": "ETH-CLOSE",
                "bot_number": 2,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 2,
                },
            }],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY):
            disabled = crypto.crypto_15m_campaign_review(
                settings,
                portfolio,
                candidate(asset="DOGE"),
                bot_number=2,
            )
            aggregate = crypto.crypto_15m_campaign_context(settings, portfolio)

        self.assertFalse(disabled["eligible"])
        self.assertEqual(disabled["reason"], "campaign_bot_disabled")
        self.assertEqual(aggregate["configured_bot_count"], 1)
        self.assertEqual(aggregate["bot_live_open_count"], 1)
        self.assertEqual(aggregate["disabled_bots"][0]["bot_number"], 2)

    def test_legacy_campaign_rows_are_migrated_to_bot_one(self):
        row = {
            "strategy_owner": "crypto_15m_campaign",
            "crypto_live_campaign": {"role": "primary"},
        }
        portfolio = {"bets": [row]}
        self.assertTrue(crypto.ensure_crypto_15m_campaign_bot_numbers(portfolio))
        self.assertEqual(row["bot_number"], 1)
        self.assertEqual(row["crypto_live_campaign"]["bot_number"], 1)
        self.assertFalse(crypto.ensure_crypto_15m_campaign_bot_numbers(portfolio))

    def test_daily_loss_cap_blocks_after_three_hundred_net_loss(self):
        portfolio = {
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -300,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "now",
                "settled_at": "now",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            }]
        }
        review = review_candidate(portfolio, candidate(), TODAY, row_date)
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_daily_loss_cap")

    def test_cross_bot_profit_reopens_a_locally_capped_continuous_lane(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
            "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
            "CRYPTO_15M_CONTINUOUS_OPPORTUNITY_MODE": "true",
            "CRYPTO_15M_DAILY_LOSS_CAP": "400",
            "CRYPTO_15M_CROSS_BOT_CAP_RECOVERY_ENABLED": "true",
        })

        def settled(row_id, bot_number, profit):
            return {
                "id": row_id,
                "status": "settled",
                "mode": "live",
                "result": "WIN" if profit > 0 else "LOSS",
                "profit": profit,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "2026-07-18T10:00:00Z",
                "settled_at": "2026-07-18T10:10:00Z",
                "bot_number": bot_number,
                "crypto_live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": bot_number,
                },
            }

        portfolio = {
            "balance": 3000,
            "bets": [
                settled("bot-1-loss", 1, -450),
                settled("bot-2-win", 2, 100),
            ],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY), patch.object(
            crypto, "is_after_daily_reset", return_value=True
        ):
            context = crypto.crypto_15m_campaign_lane_context(
                settings,
                portfolio,
                1,
            )

        self.assertEqual(context["bot_daily_realized_profit"], -450)
        self.assertEqual(context["cross_bot_recovery_credit"], 100)
        self.assertEqual(context["effective_bot_daily_realized_profit"], -350)
        self.assertEqual(context["daily_loss_remaining"], 50)
        self.assertFalse(context["daily_loss_cap_hit"])
        self.assertEqual(context["status"], "active")

    def test_cross_bot_recovery_credit_is_allocated_once_across_losing_lanes(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
            "CRYPTO_15M_CROSS_BOT_CAP_RECOVERY_ENABLED": "true",
        })

        def settled(row_id, bot_number, profit):
            return {
                "id": row_id,
                "status": "settled",
                "profit": profit,
                "strategy_owner": "crypto_15m_campaign",
                "settled_at": "2026-07-18T10:10:00Z",
                "bot_number": bot_number,
            }

        portfolio = {
            "bets": [
                settled("bot-1-loss", 1, -450),
                settled("bot-2-loss", 2, -300),
                settled("bot-3-win", 3, 100),
            ],
        }
        with patch.object(crypto, "local_date_key", return_value=TODAY), patch.object(
            crypto, "is_after_daily_reset", return_value=True
        ):
            recovery = crypto.crypto_15m_cross_bot_cap_recovery(
                settings,
                portfolio,
            )

        self.assertEqual(recovery["positive_profit_pool"], 100)
        self.assertEqual(recovery["allocations"]["1"], 60)
        self.assertEqual(recovery["allocations"]["2"], 40)
        self.assertEqual(
            sum(recovery["allocations"].values()),
            100,
        )

    def test_daily_loss_capacity_uses_forty_percent_of_bankroll(self):
        portfolio = {
            "balance": 400,
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -75,
                "strategy_owner": "crypto_15m_campaign",
                "placed_at": "now",
                "settled_at": "now",
                "crypto_live_campaign": {"role": "primary", "campaign_date": TODAY},
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            daily_loss_cap=300,
            daily_loss_cap_pct=0.40,
        )
        self.assertEqual(context["daily_loss_cap"], 160)
        self.assertEqual(context["daily_loss_cap_pct"], 0.40)
        self.assertEqual(context["daily_loss_cap_source"], "bankroll_pct")
        self.assertEqual(context["daily_loss_remaining"], 85)

    def test_old_open_bot_position_still_consumes_daily_loss_capacity(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "stake": 100,
                "entry_price": 50,
                "strategy_owner": "small_edge",
                "placed_at": "2026-07-17T23:59:00Z",
            }]
        }
        context = build_campaign_context(portfolio, TODAY, lambda value=None: "2026-07-17")
        self.assertGreater(context["bot_daily_open_risk"], 100)
        self.assertLess(context["daily_loss_remaining"], 200)

    def test_loop_slows_to_one_minute_only_after_campaign_completion(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "true",
            "CRYPTO_SCAN_INTERVAL_MINUTES": "0.5",
            "CRYPTO_CAMPAIGN_COMPLETE_SCAN_INTERVAL_MINUTES": "1",
        })
        self.assertEqual(
            crypto.crypto_loop_interval_seconds(
                settings,
                {"crypto_15m_campaign": {"complete": False}},
            ),
            30.0,
        )
        self.assertEqual(
            crypto.crypto_loop_interval_seconds(
                settings,
                {"crypto_15m_campaign": {"complete": True}},
            ),
            60.0,
        )


if __name__ == "__main__":
    unittest.main()
