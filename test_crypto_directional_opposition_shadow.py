import unittest
from datetime import datetime, timedelta, timezone

from crypto_directional_opposition_shadow import (
    VERSION,
    capture_candidates,
    empty_ledger,
    normalize_ledger,
    settle_records,
    summarize,
)


class DirectionalOppositionV2Tests(unittest.TestCase):
    def settings(self):
        return {
            "CRYPTO_DIRECTIONAL_OPPOSITION_V2_ENABLED": "true",
            "CRYPTO_DIRECTIONAL_OPPOSITION_V2_ASSETS": "BTC,ETH,SOL,DOGE",
            "CRYPTO_DIRECTIONAL_OPPOSITION_V2_VIRTUAL_STAKE": "1",
            "CRYPTO_DIRECTIONAL_OPPOSITION_V2_SETTLEMENT_GRACE_MINUTES": "0",
            "CRYPTO_DIRECTIONAL_OPPOSITION_V2_SETTLEMENT_RETRY_MINUTES": "0",
            "CRYPTO_DIRECTIONAL_PAIR_ENABLED": "true",
            "CRYPTO_DIRECTIONAL_PAIR_MIN_PRICE_CENTS": "35",
            "CRYPTO_DIRECTIONAL_PAIR_MAX_PRICE_CENTS": "44",
            "CRYPTO_DIRECTIONAL_PAIR_MIN_UNIQUE_MARKETS": "100",
            "CRYPTO_DIRECTIONAL_PAIR_MIN_EXPIRY_WINDOWS": "50",
            "CRYPTO_DIRECTIONAL_PAIR_MIN_OBSERVATION_DAYS": "30",
            "CRYPTO_DIRECTIONAL_REPLICATION_ENABLED": "true",
            "CRYPTO_DIRECTIONAL_REPLICATION_MIN_UNIQUE_MARKETS": "100",
            "CRYPTO_DIRECTIONAL_REPLICATION_MIN_OBSERVATION_DAYS": "30",
        }

    def candidate(self, lane, **updates):
        close = (datetime.now(timezone.utc) - timedelta(minutes=3)).replace(
            second=0, microsecond=0
        )
        opposition = lane == "directional_opposition"
        row = {
            "market_lane": "crypto_15m",
            "asset": "BTC",
            "ticker": "KXBTC15M-TEST",
            "event_ticker": "KXBTC15M-EVENT",
            "series_ticker": "KXBTC15M",
            "close_time": close.isoformat(),
            "side": "yes",
            "entry_price": 40.0,
            "exact_fee_cents": 1.7,
            "fee_schedule_exact": True,
            "fee_schedule": {
                "fee_type": "quadratic",
                "fee_multiplier": 1.0,
                "authoritative": True,
            },
            "edge": 4.0,
            "expected_edge": 2.3,
            "edge_low": 0.5,
            "confidence": 81.0,
            "selected_side_probability": 44.0,
            "selected_side_probability_low": 42.0,
            "selected_side_probability_high": 46.0,
            "data_quality": {"score": 0.95},
            "kalshi_microstructure": {
                "best_yes_bid_cents": 38.0,
                "best_yes_entry_price_cents": 40.0,
                "best_no_bid_cents": 60.0,
                "best_no_entry_price_cents": 62.0,
                "age_seconds": 0.1,
                "fresh": True,
                "sequence_valid": True,
                "book_consistent": True,
            },
            "minutes_to_close": 5.0,
            "directional_confirmation": {
                "enabled": True,
                "ok": not opposition,
                "reason": (
                    "directional_opposition"
                    if opposition
                    else "confirmed_by_directional_sources"
                ),
                "flow_strength": 0.2,
                "selected_side_source_count": 0 if opposition else 2,
            },
            "skip_reasons": ["directional_opposition"] if opposition else [],
            "strategy_version": "settlement-edge-v3-test",
            "strategy_config_hash": "config123",
            "feature_schema_version": "2",
            "strategy_identity": {
                "strategy_version": "settlement-edge-v3-test",
                "config_hash": "config123",
                "feature_schema_version": "2",
            },
        }
        row.update(updates)
        return row

    def test_captures_one_record_per_asset_expiry_and_lane(self):
        ledger = empty_ledger(self.settings())
        first = self.candidate("directional_opposition", expected_edge=2.0)
        better = self.candidate(
            "directional_opposition",
            ticker="KXBTC15M-BETTER",
            expected_edge=3.0,
            edge_low=1.0,
        )
        control = self.candidate("confirmation_control", ticker="KXBTC15M-CONTROL")
        added = capture_candidates(ledger, [first, better, control], self.settings())
        self.assertEqual(len(added), 2)
        self.assertEqual(
            {row["lane"] for row in added},
            {"directional_opposition", "confirmation_control"},
        )
        opposition = next(
            row for row in added if row["lane"] == "directional_opposition"
        )
        self.assertEqual(opposition["ticker"], "KXBTC15M-BETTER")
        self.assertEqual(opposition["version"], VERSION)
        self.assertFalse(opposition["affects_execution"])
        self.assertFalse(opposition["automatic_promotion"])
        self.assertEqual(
            capture_candidates(ledger, [first, better, control], self.settings()), []
        )

    def test_requires_exact_fee_and_current_strategy_identity(self):
        ledger = empty_ledger(self.settings())
        added = capture_candidates(
            ledger,
            [
                self.candidate("directional_opposition", fee_schedule_exact=False),
                self.candidate(
                    "confirmation_control",
                    strategy_config_hash="",
                    strategy_identity={},
                ),
            ],
            self.settings(),
        )
        self.assertEqual(added, [])

    def test_official_settlement_and_summary_keep_lanes_separate(self):
        ledger = empty_ledger(self.settings())
        capture_candidates(
            ledger,
            [
                self.candidate("directional_opposition"),
                self.candidate("confirmation_control", ticker="KXBTC15M-CONTROL"),
            ],
            self.settings(),
        )
        settled = settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        self.assertEqual(len(settled), 2)
        report = summarize(ledger, self.settings())
        self.assertEqual(report["settled"], 2)
        self.assertEqual({row["settled"] for row in report["lanes"]}, {1})
        self.assertFalse(report["evaluation"]["automatic_promotion"])
        self.assertEqual(report["evaluation"]["paired_markets_available"], 1)
        self.assertEqual(len(report["matched_strata"]), 1)
        self.assertEqual(report["paired_forward"]["settled"], 1)
        self.assertFalse(
            report["paired_forward"]["evaluation"]["ready_for_manual_review"]
        )

    def test_shadow_lane_does_not_require_positive_conservative_edge(self):
        ledger = empty_ledger(self.settings())
        added = capture_candidates(
            ledger,
            [self.candidate("directional_opposition", edge_low=-3.0, expected_edge=-1.0)],
            self.settings(),
        )
        self.assertEqual(len(added), 1)

    def test_crossed_book_is_rejected_and_visible_in_funnel(self):
        ledger = empty_ledger(self.settings())
        candidate = self.candidate("directional_opposition")
        candidate["kalshi_microstructure"] = {
            **candidate["kalshi_microstructure"],
            "best_yes_bid_cents": 54.0,
            "best_yes_entry_price_cents": 35.0,
            "book_consistent": False,
        }
        self.assertEqual(capture_candidates(ledger, [candidate], self.settings()), [])
        self.assertEqual(ledger["last_capture_funnel"]["reason:orderbook_crossed"], 1)

    def test_nonfinal_market_cannot_settle(self):
        ledger = empty_ledger(self.settings())
        capture_candidates(
            ledger, [self.candidate("directional_opposition")], self.settings()
        )
        settled = settle_records(
            ledger,
            lambda _ticker: {"status": "closed", "result": "yes"},
            self.settings(),
        )
        self.assertEqual(settled, [])
        self.assertEqual(ledger["records"][0]["status"], "open")

    def test_paired_forward_freezes_same_snapshot_fade_and_follow(self):
        ledger = empty_ledger(self.settings())
        captured_at = datetime.now(timezone.utc).replace(microsecond=0)
        candidate = self.candidate("directional_opposition")
        capture_candidates(
            ledger,
            [candidate],
            self.settings(),
            now=captured_at,
        )
        pair = ledger["paired_experiment"]["records"][0]
        self.assertEqual(pair["captured_at"], captured_at.isoformat())
        self.assertEqual(pair["fade"]["side"], "yes")
        self.assertEqual(pair["fade"]["entry_price_cents"], 40.0)
        self.assertEqual(pair["follow"]["side"], "no")
        self.assertEqual(pair["follow"]["entry_price_cents"], 62.0)
        self.assertGreater(pair["fade"]["exact_fee_cents"], 0)
        self.assertGreater(pair["follow"]["exact_fee_cents"], 0)
        self.assertFalse(pair["affects_execution"])
        self.assertFalse(pair["automatic_promotion"])
        self.assertEqual(
            pair["integer_contract_forward"]["fade"]["contracts"], 1
        )
        capture_candidates(ledger, [candidate], self.settings(), now=captured_at)
        self.assertEqual(len(ledger["paired_experiment"]["records"]), 1)

    def test_paired_forward_only_uses_registered_opposition_price_band(self):
        ledger = empty_ledger(self.settings())
        capture_candidates(
            ledger,
            [
                self.candidate("confirmation_control"),
                self.candidate(
                    "directional_opposition",
                    ticker="KXBTC15M-45C",
                    entry_price=45.0,
                ),
            ],
            self.settings(),
        )
        self.assertEqual(ledger["paired_experiment"]["records"], [])
        funnel = ledger["last_paired_capture_funnel"]
        self.assertEqual(
            funnel["reason:paired_price_outside_registered_band"], 1
        )

    def test_paired_forward_requires_both_executable_asks_and_official_result(self):
        ledger = empty_ledger(self.settings())
        missing = self.candidate("directional_opposition")
        missing["kalshi_microstructure"].pop("best_no_entry_price_cents")
        capture_candidates(ledger, [missing], self.settings())
        self.assertEqual(ledger["paired_experiment"]["records"], [])

        candidate = self.candidate(
            "directional_opposition", ticker="KXBTC15M-PAIR-SETTLE"
        )
        capture_candidates(ledger, [candidate], self.settings())
        settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        report = summarize(ledger, self.settings())["paired_forward"]
        self.assertEqual(report["unique_settled_markets"], 1)
        self.assertGreater(report["fade"]["virtual_profit"], 0)
        self.assertEqual(report["follow"]["virtual_profit"], -1.0)
        self.assertGreater(report["paired_profit_delta"], 0)
        self.assertIsNone(
            report["delta_cluster_inference"]["one_sided_90_lower"]
        )
        self.assertFalse(report["automatic_promotion"])

    def test_asset_specific_replications_start_fresh_and_keep_hypotheses_separate(self):
        ledger = empty_ledger(self.settings())
        doge = self.candidate(
            "directional_opposition",
            asset="DOGE",
            ticker="KXDOGE15M-REPLICATION",
            event_ticker="KXDOGE15M-EVENT",
            series_ticker="KXDOGE15M",
        )
        eth = self.candidate(
            "directional_opposition",
            asset="ETH",
            ticker="KXETH15M-REPLICATION",
            event_ticker="KXETH15M-EVENT",
            series_ticker="KXETH15M",
        )
        capture_candidates(ledger, [doge, eth], self.settings())
        rows = ledger["asset_replication_experiment"]["records"]
        self.assertEqual(len(rows), 2)
        hypotheses = {row["hypothesis"]: row for row in rows}
        self.assertEqual(hypotheses["doge_fade"]["primary_arm_name"], "fade")
        self.assertEqual(hypotheses["eth_follow"]["primary_arm_name"], "follow")
        self.assertTrue(all(row["affects_execution"] is False for row in rows))
        settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        report = summarize(ledger, self.settings())["asset_replications"]
        self.assertFalse(report["automatic_promotion"])
        self.assertFalse(report["historical_backfill"])
        self.assertEqual({row["settled"] for row in report["hypotheses"]}, {1})
        self.assertTrue(
            all(
                not row["evaluation"]["ready_for_manual_review"]
                for row in report["hypotheses"]
            )
        )

    def test_new_execution_and_asset_revisions_do_not_backfill_old_pair(self):
        ledger = empty_ledger(self.settings())
        capture_candidates(
            ledger, [self.candidate("directional_opposition")], self.settings()
        )
        old_pair = ledger["paired_experiment"]["records"][0]
        old_pair.pop("integer_contract_forward", None)
        ledger["paired_experiment"].pop("execution_revision", None)
        ledger.pop("asset_replication_experiment", None)
        normalized = normalize_ledger(ledger, self.settings())
        self.assertNotIn(
            "integer_contract_forward",
            normalized["paired_experiment"]["records"][0],
        )
        self.assertEqual(
            normalized["asset_replication_experiment"]["records"], []
        )

    def test_legacy_comparison_is_never_promotable_and_reports_matched_delta(self):
        ledger = empty_ledger(self.settings())
        capture_candidates(
            ledger,
            [
                self.candidate("directional_opposition"),
                self.candidate("confirmation_control", ticker="KXBTC15M-CONTROL"),
            ],
            self.settings(),
        )
        settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        report = summarize(ledger, self.settings())
        self.assertTrue(report["legacy_observational_only"])
        self.assertFalse(report["evaluation"]["ready_for_manual_review"])
        self.assertEqual(report["matched_observational_diagnostics"]["pairs"], 1)
        self.assertEqual(report["matched_observational_diagnostics"]["same_side_pairs"], 1)


if __name__ == "__main__":
    unittest.main()
