import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_btc_15m_sprint as sprint
import crypto_paper_bettor as crypto


NOW = datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc)


def candidate(
    *,
    ticker="KXBTC15M-TEST",
    asset="BTC",
    price=40.0,
    minutes=7.0,
    edge=3.5,
    skip_reasons=None,
):
    side = "yes"
    return {
        "ticker": ticker,
        "event_ticker": ticker,
        "series_ticker": f"KX{asset}15M",
        "asset": asset,
        "market_lane": "crypto_15m",
        "is_15m_market": True,
        "side": side,
        "entry_price": price,
        "expected_edge": edge,
        "edge": edge,
        "exact_fee_cents": 1.65,
        "fee_schedule_exact": True,
        "minutes_to_close": minutes,
        "close_time": (NOW + timedelta(minutes=minutes)).isoformat(),
        "initial_price_source": "kalshi_executable_orderbook",
        "market_quote_fetched_at": NOW.isoformat(),
        "model_prob_yes": 47.5,
        "selected_side_probability": 47.5,
        "selected_side_probability_low": 44.0,
        "selected_side_probability_high": 51.0,
        "skip_reasons": list(
            skip_reasons
            if skip_reasons is not None
            else ["directional_opposition"]
        ),
        "flow_review": {
            "direction": "no",
            "strength": 0.25,
            "source_scores": {"coinbase": -0.2, "kraken": -0.3},
        },
        "directional_confirmation": {
            "ok": False,
            "reason": "directional_opposition",
        },
        "data_quality": {"score": 0.99, "reasons": []},
        "kalshi_microstructure": {
            "source": "kalshi_websocket",
            "sequence_valid": True,
            "fresh": True,
            "age_seconds": 0.1,
            "exchange_ts_ms": 123456789,
            "orderbook_fetched_at": NOW.isoformat(),
            "message_type": "orderbook_delta",
            f"best_{side}_entry_price_cents": price,
            f"top1_{side}_depth_contracts": 5.0,
        },
    }


def settled_record(captured_at, *, lane=sprint.PRIMARY_LANE, win=True):
    unreserved = 0.5835 if win else -0.4165
    return {
        "id": f"{lane}:{captured_at.isoformat()}",
        "lane": lane,
        "status": "settled",
        "settlement_verified": True,
        "capture_quality": {"ok": True},
        "captured_at": captured_at.isoformat(),
        "result": "WIN" if win else "LOSS",
        "entry_stake_dollars": 0.40,
        "fee_per_contract_dollars": 0.0165,
        "slippage_reserve_dollars": 0.01,
        "reserved_break_even_probability": 0.4265,
        "unreserved_profit": unreserved,
        "reserved_profit": unreserved - 0.01,
    }


class Btc15mSprintTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "CRYPTO_15M_SPRINT_ENABLED": "true",
            "CRYPTO_15M_SPRINT_PROSPECTIVE_HOURS": "72",
            "CRYPTO_15M_SPRINT_MIN_FORWARD_MARKETS": "60",
        }
        self.config = sprint.policy_configuration(self.settings)

    def test_policy_accepts_only_flow_veto_as_remaining_rejection(self):
        accepted = sprint.flow_veto_policy_review(candidate(), self.config)
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["reserved_edge_cents"], 2.5)

        for row, reason in (
            (
                candidate(skip_reasons=["directional_opposition", "conservative_edge_not_positive"]),
                "other_rejection_reasons_present",
            ),
            (candidate(skip_reasons=[]), "flow_veto_reason_absent"),
            (candidate(asset="ETH"), "not_primary_asset"),
            (candidate(edge=2.99), "reserved_edge_below_minimum"),
        ):
            with self.subTest(reason=reason):
                review = sprint.flow_veto_policy_review(row, self.config)
                self.assertFalse(review["ok"])
                self.assertIn(reason, review["reasons"])

    def test_price_and_time_boundaries_are_locked(self):
        for price in (35, 49):
            self.assertTrue(
                sprint.flow_veto_policy_review(
                    candidate(price=price), self.config
                )["ok"]
            )
        for minutes in (2, 12):
            self.assertTrue(
                sprint.flow_veto_policy_review(
                    candidate(minutes=minutes), self.config
                )["ok"]
            )
        self.assertFalse(
            sprint.flow_veto_policy_review(candidate(price=34.99), self.config)["ok"]
        )
        self.assertFalse(
            sprint.flow_veto_policy_review(candidate(minutes=12.01), self.config)["ok"]
        )

    def test_capture_fails_closed_for_book_sequence_freshness_and_depth(self):
        cases = (
            ("sequence_valid", False, "book_sequence_invalid"),
            ("fresh", False, "book_stale"),
            ("age_seconds", 2.01, "book_too_old"),
            ("top1_yes_depth_contracts", 0.5, "insufficient_one_contract_depth"),
            ("best_yes_entry_price_cents", 41, "entry_price_book_mismatch"),
        )
        for field, value, reason in cases:
            row = candidate()
            row["kalshi_microstructure"][field] = value
            with self.subTest(reason=reason):
                review = sprint.capture_quality_review(row, self.config)
                self.assertFalse(review["ok"])
                self.assertIn(reason, review["reasons"])

    def test_first_observation_is_deduplicated_and_baselines_are_non_promotable(self):
        state = sprint.empty_state(self.settings, now=NOW)
        added = sprint.capture_candidates(state, [candidate()], now=NOW)
        self.assertEqual(
            {row["lane"] for row in added},
            {
                sprint.PRIMARY_LANE,
                sprint.EXACT_40_BASELINE,
                sprint.BAND_40_44_BASELINE,
            },
        )
        self.assertEqual(sprint.capture_candidates(state, [candidate()], now=NOW), [])
        promotable = [row for row in state["records"] if row["eligible_for_promotion"]]
        self.assertEqual(len(promotable), 1)
        self.assertEqual(promotable[0]["lane"], sprint.PRIMARY_LANE)

    def test_non_btc_assets_are_control_only(self):
        state = sprint.empty_state(self.settings, now=NOW)
        added = sprint.capture_candidates(
            state,
            [candidate(ticker="KXETH15M-TEST", asset="ETH")],
            now=NOW,
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["lane"], sprint.CONTROL_LANE)
        self.assertFalse(added[0]["eligible_for_promotion"])

    def test_late_btc_diagnostic_is_first_observation_and_non_promotable(self):
        state = sprint.empty_state(self.settings, now=NOW)
        row = candidate(
            ticker="KXBTC15M-LATE",
            price=45,
            minutes=3,
            edge=-2,
            skip_reasons=["conservative_edge_not_positive", "campaign_quality_filter"],
        )
        added = sprint.capture_candidates(state, [row], now=NOW)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["lane"], sprint.LATE_2_4_DIAGNOSTIC)
        self.assertFalse(added[0]["eligible_for_promotion"])
        self.assertEqual(sprint.capture_candidates(state, [row], now=NOW), [])

        report = sprint.summarize(state, now=NOW)
        diagnostic = report["diagnostics"]["btc_2_4_minute"]
        self.assertEqual(diagnostic["tracked"], 1)
        self.assertFalse(diagnostic["development_selected"])
        self.assertTrue(diagnostic["preregistered_hypothesis"])
        hypothesis = next(
            row for row in report["hypotheses"]
            if row["lane"] == sprint.LATE_2_4_DIAGNOSTIC
        )
        self.assertEqual(hypothesis["role"], "primary_candidate")
        self.assertEqual(hypothesis["minimum_independent_markets"], 50)
        self.assertFalse(hypothesis["ready_for_manual_review"])

        outside = sprint.empty_state(self.settings, now=NOW)
        self.assertEqual(
            sprint.capture_candidates(
                outside,
                [candidate(ticker="KXBTC15M-NOT-LATE", price=45, minutes=4.01, edge=-2)],
                now=NOW,
            ),
            [],
        )

    def test_settlement_uses_one_contract_exact_fee_and_reserve(self):
        state = sprint.empty_state(self.settings, now=NOW)
        sprint.capture_candidates(state, [candidate(price=40)], now=NOW)
        settled = sprint.settle_records(
            state,
            lambda _ticker: {
                "status": "finalized",
                "result": "yes",
                "settlement_ts": (NOW + timedelta(minutes=10)).isoformat(),
            },
            now=NOW + timedelta(minutes=10),
        )
        self.assertEqual(len(settled), 3)
        primary = next(row for row in settled if row["lane"] == sprint.PRIMARY_LANE)
        self.assertEqual(primary["unreserved_profit"], 0.5835)
        self.assertEqual(primary["reserved_profit"], 0.5735)
        self.assertTrue(primary["settlement_verified"])

    def test_pass_requires_sixty_forward_markets_and_both_profitable_halves(self):
        state = sprint.empty_state(self.settings, now=NOW)
        start = datetime.fromisoformat(state["prospective_started_at"])
        midpoint = start + timedelta(hours=36)
        state["records"] = [
            *[
                settled_record(start + timedelta(minutes=index + 1))
                for index in range(30)
            ],
            *[
                settled_record(midpoint + timedelta(minutes=index + 1))
                for index in range(30)
            ],
        ]
        report = sprint.summarize(
            state,
            now=datetime.fromisoformat(state["evaluation_due_at"]) + timedelta(seconds=1),
        )
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["recommendation_only"])
        self.assertFalse(report["automatic_promotion"])
        self.assertTrue(all(gate["passed"] for gate in report["qualification_gates"].values()))

    def test_under_sixty_is_inconclusive_and_baselines_cannot_promote(self):
        state = sprint.empty_state(self.settings, now=NOW)
        start = datetime.fromisoformat(state["prospective_started_at"])
        state["records"] = [
            settled_record(start + timedelta(minutes=index), lane=sprint.EXACT_40_BASELINE)
            for index in range(100)
        ]
        report = sprint.summarize(
            state,
            now=datetime.fromisoformat(state["evaluation_due_at"]) + timedelta(seconds=1),
        )
        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertEqual(report["primary"]["settled"], 0)
        baseline = next(
            row for row in report["lanes"] if row["lane"] == sprint.EXACT_40_BASELINE
        )
        self.assertEqual(baseline["settled"], 100)
        self.assertFalse(baseline["eligible_for_promotion"])

    def test_development_replay_is_chronological_and_deduplicated(self):
        rows = []
        for index, result in enumerate(("LOSS", "WIN")):
            rows.append({
                "ticker": "KXBTC15M-HISTORY",
                "series_ticker": "KXBTC15M",
                "asset": "BTC",
                "status": "settled",
                "captured_at": (NOW - timedelta(hours=2 - index)).isoformat(),
                "entry_price": 40,
                "fee_per_contract": 0.0165,
                "minutes_to_close": 7,
                "edge": 3.5,
                "skip_reasons": ["directional_flow_too_weak"],
                "result": result,
            })
        report = sprint.development_summary(
            rows,
            self.config,
            cutoff=NOW.isoformat(),
        )
        self.assertEqual(report["settled"], 1)
        self.assertEqual(report["losses"], 1)
        self.assertFalse(report["counts_toward_qualification"])
        self.assertFalse(report["eligible_for_promotion"])

    def test_gate_snapshot_migrates_before_first_promotable_observation(self):
        state = sprint.empty_state(self.settings, now=NOW)
        start = state["prospective_started_at"]
        state["configuration"].pop("candidate_gate_snapshot")
        state["records"] = [
            settled_record(NOW, lane=sprint.EXACT_40_BASELINE)
        ]

        normalized = sprint.normalize_state(
            state,
            settings={**self.settings, "CRYPTO_15M_TIERED_MIN_EDGE": "3"},
            now=NOW + timedelta(minutes=5),
        )

        self.assertEqual(normalized["prospective_started_at"], start)
        self.assertEqual(len(normalized["records"]), 1)
        self.assertIn("candidate_gate_snapshot", normalized["configuration"])
        self.assertEqual(
            normalized["configuration"]["candidate_gate_snapshot"]["minimum_edge_cents"],
            3.0,
        )
        self.assertIn("before first promotable", normalized["configuration_migration_note"])

    def test_sprint_freeze_blocks_before_bankroll_or_authenticated_order_call(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_SPRINT_SHADOW_ONLY": "true",
            "CRYPTO_LIVE_ORDER_ENABLED": "true",
            "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "true",
        })
        row = candidate()
        row["crypto_live_campaign"] = {"enabled": True}
        with patch.object(crypto, "live_order_review") as review, patch.object(
            crypto, "reserve_live_order"
        ) as reserve, patch.object(crypto, "kalshi_private_request") as private_request:
            preflight = crypto.preflight_live_kalshi_order(settings, row, 1.0)
            order = crypto.place_live_kalshi_order(settings, {"bets": []}, row, 1.0)
        self.assertEqual(preflight["error"], "crypto_15m_sprint_live_freeze")
        self.assertEqual(order["error"], "crypto_15m_sprint_live_freeze")
        self.assertTrue(order["execution_freeze"]["blocked_before_bankroll_reservation"])
        review.assert_not_called()
        reserve.assert_not_called()
        private_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
