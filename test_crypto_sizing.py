import unittest
from unittest.mock import patch

import crypto_paper_bettor as crypto


class CryptoLiveReconciliationTests(unittest.TestCase):
    def test_locally_owned_legacy_series_still_matches_remote_position(self):
        local_ticker = "KXBTCD-26JUL1817-T64499.99"
        remote = [
            {"ticker": local_ticker, "position_fp": "12.40"},
            {"ticker": "KXSPORTS-EXAMPLE", "position_fp": "5.00"},
        ]
        tickers = crypto.reconciliation_remote_tickers(
            remote,
            {local_ticker},
            ("KXBTC15M", "KXETH15M"),
        )
        self.assertEqual(tickers, {local_ticker})

    def test_zero_contract_history_is_not_reported_as_remote_open(self):
        remote = [
            {"ticker": "KXBTC15M-OLD", "position_fp": "0.00"},
            {"ticker": "KXBTC15M-OPEN", "position_fp": "-2.50"},
        ]
        tickers = crypto.reconciliation_remote_tickers(
            remote,
            set(),
            ("KXBTC15M",),
        )
        self.assertEqual(tickers, {"KXBTC15M-OPEN"})


class CryptoPhaseTwoSupportSizingTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(crypto.DEFAULT_SETTINGS)
        self.settings["CRYPTO_15M_CAMPAIGN_ENABLED"] = "false"
        # Legacy sizing helpers remain testable but are disabled by default in
        # the live-core policy.
        self.settings["CRYPTO_SHARED_RECOVERY_ENABLED"] = "true"
        self.phase = {"enabled": True, "status": "pending", "current_cycle": 1}
        self.portfolio = {"bets": []}

    def support_stake(self, tier):
        candidate = {}
        if tier:
            candidate["selective_edge"] = {"tier": tier, "base_stake": 3.0}
        stake = crypto.apply_crypto_small_edge_staking(
            self.settings,
            self.portfolio,
            candidate,
            10.0,
            100.0,
            self.phase,
        )
        return stake, candidate["small_edge"]

    def test_normal_support_cap_is_three_dollars(self):
        stake, context = self.support_stake(None)
        self.assertEqual(stake, 3.0)
        self.assertEqual(context["support_cap"], 3.0)

    def test_strong_support_cap_is_five_dollars(self):
        stake, context = self.support_stake("strong_edge")
        self.assertEqual(stake, 5.0)
        self.assertEqual(context["support_tier"], "strong_edge")

    def test_elite_support_cap_is_seven_fifty(self):
        stake, context = self.support_stake("elite_edge")
        self.assertEqual(stake, 7.5)
        self.assertEqual(context["support_tier"], "elite_edge")

    def test_accidental_one_cent_bet_is_not_phase_two_coordination(self):
        accidental = {"entry_price": 1, "phase_two_attempt_id": "accidental"}
        legitimate = {"entry_price": 2, "phase_two_attempt_id": "legitimate"}
        self.assertFalse(crypto.is_crypto_phase_two_bet(accidental, self.settings))
        self.assertTrue(crypto.is_crypto_phase_two_bet(legitimate, self.settings))

    def test_full_size_phase_two_bet_is_not_also_a_recovery_slice(self):
        candidate = {"phase_two": {"active": True}}
        with patch.object(crypto, "shared_recovery_context") as recovery_context:
            stake = crypto.apply_crypto_shared_recovery_staking(self.settings, candidate, 5.0, 100.0)
        self.assertEqual(stake, 5.0)
        recovery_context.assert_not_called()

    def test_base_stake_can_cover_adaptive_recovery_without_extra_size(self):
        candidate = {
            "ticker": "RECOVERY-A",
            "event_ticker": "RECOVERY-EVENT",
            "entry_price": 50,
            "edge": 15,
            "confidence": 80,
        }
        context = {
            "enabled": True,
            "strategy": "crypto",
            "active": True,
            "reason": "shared_system_drawdown_recovery",
            "target_profit": 10.0,
            "slice_target_profit": 3.33,
            "max_recovery_stake": 20.0,
            "slots_remaining": 3,
        }
        with patch.object(crypto, "shared_recovery_context", return_value=context):
            stake = crypto.apply_crypto_shared_recovery_staking(self.settings, candidate, 5.0, 100.0)
        self.assertEqual(stake, 5.0)
        self.assertTrue(candidate["shared_recovery"]["active"])
        self.assertTrue(candidate["shared_recovery"]["covered_by_base_stake"])
        self.assertEqual(candidate["shared_recovery"]["allocated_target_profit"], 4.0)

    def test_recovery_rejects_edge_below_new_quality_floor(self):
        candidate = {
            "ticker": "RECOVERY-B",
            "event_ticker": "RECOVERY-EVENT",
            "entry_price": 50,
            "edge": 14.99,
            "confidence": 80,
        }
        context = {
            "enabled": True,
            "strategy": "crypto",
            "active": True,
            "reason": "shared_system_drawdown_recovery",
            "target_profit": 10.0,
            "slice_target_profit": 3.33,
            "max_recovery_stake": 20.0,
            "slots_remaining": 3,
        }
        with patch.object(crypto, "shared_recovery_context", return_value=context):
            stake = crypto.apply_crypto_shared_recovery_staking(self.settings, candidate, 5.0, 100.0)
        self.assertEqual(stake, 5.0)
        self.assertFalse(candidate["shared_recovery"]["active"])
        self.assertEqual(candidate["shared_recovery"]["reason"], "crypto_recovery_edge_filter")

    def event_row(self, side="yes"):
        return {
            "status": "open",
            "event_ticker": "EVENT-A",
            "market_kind": "above",
            "floor_strike": 100,
            "side": side,
            "entry_price": 50,
            "stake": 1.0,
        }

    def event_candidate(self, side="yes", edge=12, confidence=75):
        return {
            "event_ticker": "EVENT-A",
            "market_kind": "above",
            "floor_strike": 100,
            "side": side,
            "entry_price": 50,
            "edge": edge,
            "confidence": confidence,
            "skip_reasons": [],
        }

    def test_second_event_position_is_half_stake(self):
        candidate = self.event_candidate()
        stake = crypto.apply_crypto_event_diminishing(
            self.settings, {"bets": [self.event_row()]}, candidate, 4.0
        )
        self.assertEqual(stake, 2.0)
        self.assertEqual(candidate["event_diversification"]["tier"], "second_half")

    def test_third_event_position_can_improve_worst_case_coverage(self):
        candidate = self.event_candidate(side="no")
        stake = crypto.apply_crypto_event_diminishing(
            self.settings, {"bets": [self.event_row(), self.event_row()]}, candidate, 4.0
        )
        self.assertEqual(stake, 1.0)
        self.assertEqual(candidate["event_diversification"]["reason"], "crypto_event_third_coverage")

    def test_third_event_position_requires_coverage_or_elite_quality(self):
        candidate = self.event_candidate(side="yes")
        stake = crypto.apply_crypto_event_diminishing(
            self.settings, {"bets": [self.event_row(), self.event_row()]}, candidate, 4.0
        )
        self.assertEqual(stake, 0.0)
        self.assertIn("crypto_event_third_not_justified", candidate["skip_reasons"])

    def test_elite_third_position_gets_quarter_stake(self):
        candidate = self.event_candidate(side="yes", edge=20, confidence=90)
        stake = crypto.apply_crypto_event_diminishing(
            self.settings, {"bets": [self.event_row(), self.event_row()]}, candidate, 4.0
        )
        self.assertEqual(stake, 1.0)
        self.assertEqual(candidate["event_diversification"]["reason"], "crypto_event_third_elite")

    def test_fourth_event_position_is_blocked(self):
        candidate = self.event_candidate(side="no", edge=20, confidence=90)
        stake = crypto.apply_crypto_event_diminishing(
            self.settings,
            {"bets": [self.event_row(), self.event_row(), self.event_row()]},
            candidate,
            4.0,
        )
        self.assertEqual(stake, 0.0)
        self.assertIn("crypto_event_cluster_full", candidate["skip_reasons"])

    def test_six_to_nine_percent_edge_is_shadow_only(self):
        candidate = {
            "ticker": "EDGE-ONLY",
            "event_ticker": "EVENT-B",
            "side": "yes",
            "entry_price": 50,
            "edge": 9.0,
            "confidence": 80,
            "volume": 100,
            "liquidity": 100,
        }
        self.assertFalse(crypto.candidate_passes(self.settings, {"bets": []}, candidate))
        self.assertIn("edge_too_low", candidate["skip_reasons"])


if __name__ == "__main__":
    unittest.main()
