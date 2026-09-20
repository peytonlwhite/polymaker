import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import crypto_15m_learning as learning
import crypto_paper_bettor as crypto
from crypto_microstructure import mid_path_features
from crypto_scan_intelligence import (
    intelligence_history,
    record_scan_intelligence,
    research_feature_payload,
)


class CryptoScanIntelligenceTests(unittest.TestCase):
    def candidate(self, ticker="KXBTC15M-TEST", close_time=None):
        close_time = close_time or (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat()
        return {
            "asset": "BTC",
            "ticker": ticker,
            "event_ticker": ticker,
            "series_ticker": "KXBTC15M",
            "market_lane": "crypto_15m",
            "market_kind": "above",
            "is_15m_market": True,
            "side": "yes",
            "close_time": close_time,
            "minutes_to_close": 5.0,
            "entry_price": 40.0,
            "floor_strike": 100.0,
            "model_prob_yes": 70.0,
            "expected_edge": 27.0,
            "edge": 27.0,
            "edge_low": 20.0,
            "confidence": 99.0,
            "probability_net_edge_positive": 99.0,
            "selected_side_probability_low": 63.0,
            "selected_side_probability_high": 77.0,
            "exact_fee_cents": 0.0,
            "expected_slippage_cents": 1.0,
            "fee_schedule_exact": True,
            "fee_schedule": {
                "fee_type": "quadratic",
                "fee_multiplier": 0.0,
                "authoritative": True,
            },
            "probability": {
                "prob": 70.0,
                "p_yes": 70.0,
                "p_low": 63.0,
                "p_high": 77.0,
                "probability_sigma_pp": 4.0,
                "interval_level": 0.90,
                "market_implied_yes": 42.0,
                "target_distance_sigma": 0.8,
            },
            "model": {
                "spot": 101.0,
                "minute_vol": 0.001,
                "trend_bias": 0.5,
                "fakeout_risk": 0.0,
            },
            "microstructure": {
                "settlement_proxy_spot": 101.0,
                "settlement_proxy_source_count": 2,
                "cross_exchange_dispersion_bps": 1.0,
                "coinbase": {
                    "mid": 101.0,
                    "book_age_seconds": 0.2,
                    "book_imbalance": 0.0,
                    "trade_flow_60s": 0.0,
                    "trade_count_60s": 0,
                },
                "kraken": {
                    "mid": 101.0,
                    "book_age_seconds": 0.2,
                    "book_imbalance": 0.0,
                    "trade_flow_60s": 0.0,
                    "trade_count_60s": 0,
                },
            },
            "kalshi_microstructure": {
                "best_yes_bid_cents": 39.0,
                "best_yes_entry_price_cents": 40.0,
                "best_no_bid_cents": 60.0,
                "best_no_entry_price_cents": 61.0,
                "yes_levels": [[39.0, 20.0]],
                "no_levels": [[60.0, 10.0], [55.0, 10.0]],
                "book_imbalance": 0.0,
                "trade_flow_60s": 0.0,
                "sequence_valid": True,
                "age_seconds": 0.1,
            },
            "data_quality": {"score": 0.99},
            "flow_review": {"direction": "yes", "strength": 0.2},
            "skip_reasons": [],
            "decision": "eligible",
        }

    def test_missing_and_neutral_values_are_distinct(self):
        missing = self.candidate()
        missing["microstructure"]["coinbase"].pop("trade_flow_60s")
        missing_features = research_feature_payload(missing)
        neutral_features = research_feature_payload(self.candidate())

        self.assertEqual(missing_features["values"]["coinbase_trade_flow_60s"], 0.0)
        self.assertFalse(missing_features["available"]["coinbase_trade_flow_60s"])
        self.assertTrue(neutral_features["available"]["coinbase_trade_flow_60s"])

    def test_mid_path_features_are_time_aligned(self):
        history = [(940.0, 100.0), (985.0, 101.0), (1000.0, 102.0)]
        result = mid_path_features(history, now_ts=1000.0)
        self.assertGreater(result["mid_return_15s_bps"], 0)
        self.assertGreater(result["mid_return_60s_bps"], result["mid_return_15s_bps"])

    def test_tier_pricing_walks_book_and_reduces_large_tier_edge(self):
        candidate = self.candidate()
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_EXPECTED_SLIPPAGE_CENTS"] = "0"
        result = crypto.crypto_15m_execution_tier_pricing(
            settings,
            candidate,
            unit_size=4.0,
            tiers={1: {}, 2: {}},
        )
        one = result["tiers"]["1"]
        two = result["tiers"]["2"]
        self.assertTrue(one["complete_depth"])
        self.assertTrue(two["complete_depth"])
        self.assertEqual(one["vwap_entry_price_cents"], 40.0)
        self.assertEqual(two["vwap_entry_price_cents"], 42.5)
        self.assertLess(two["edge_low"], one["edge_low"])

    def test_fractional_cent_book_levels_do_not_create_phantom_asks(self):
        candidate = self.candidate()
        candidate["side"] = "no"
        candidate["entry_price"] = 61.0
        candidate["kalshi_microstructure"]["yes_levels"] = [
            [39.0, 20.0],
            [0.8, 10_000.0],
        ]
        candidate["kalshi_microstructure"]["best_no_entry_price_cents"] = 61.0
        result = crypto.crypto_15m_execution_tier_pricing(
            dict(crypto.DEFAULT_SETTINGS),
            candidate,
            unit_size=4.0,
            tiers={1: {}},
        )
        self.assertTrue(result["available"])
        self.assertTrue(result["book_consistent"])
        self.assertEqual(result["best_entry_price_cents"], 61.0)
        self.assertEqual(result["tiers"]["1"]["vwap_entry_price_cents"], 61.0)

    def test_inconsistent_book_and_top_quote_fail_closed(self):
        candidate = self.candidate()
        candidate["kalshi_microstructure"]["best_yes_entry_price_cents"] = 42.0
        result = crypto.crypto_15m_execution_tier_pricing(
            dict(crypto.DEFAULT_SETTINGS),
            candidate,
            unit_size=4.0,
            tiers={1: {}},
        )
        self.assertFalse(result["available"])
        self.assertFalse(result["book_consistent"])
        self.assertEqual(result["error"], "orderbook_best_price_mismatch")

    def test_lifecycle_records_checkpoints_and_settlement_counterfactuals(self):
        now = datetime.now(timezone.utc)
        candidate = self.candidate(close_time=(now + timedelta(seconds=10)).isoformat())
        candidate["execution_tier_pricing"] = crypto.crypto_15m_execution_tier_pricing(
            dict(crypto.DEFAULT_SETTINGS),
            candidate,
            unit_size=4.0,
            tiers={1: {}},
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "intelligence.jsonl"
            state = Path(directory) / "state.json"
            first = record_scan_intelligence(
                [candidate], ledger, state, now=now
            )
            self.assertEqual(first["candidate_records"], 1)
            record_scan_intelligence(
                [candidate], ledger, state, now=now + timedelta(seconds=16)
            )
            record_scan_intelligence(
                [],
                ledger,
                state,
                now=now + timedelta(seconds=130),
                settlements=[{
                    "ticker": candidate["ticker"],
                    "result": "yes",
                    "settlement_value": 101.0,
                    "settlement_margin": 1.0,
                }],
            )
            events = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertTrue(any(row["type"] == "candidate_checkpoint" for row in events))
            settlements = [row for row in events if row["type"] == "candidate_settlement"]
            self.assertTrue(settlements)
            self.assertTrue(settlements[0]["side_won"])
            self.assertGreater(
                settlements[0]["counterfactual_unit_tiers"][0]["counterfactual_profit"],
                0,
            )
            history = intelligence_history(ledger, minutes=60, now=now + timedelta(seconds=130))
            self.assertEqual(len(history["rows"]), 2)

    def test_training_snapshot_retains_continuous_settlement_margin(self):
        snapshot = learning.candidate_snapshot(self.candidate())
        self.assertEqual(learning.settlement_margin(snapshot, 101.25), 1.25)
        self.assertIn("feature_availability", snapshot)
        self.assertIn("research_features", snapshot)


if __name__ == "__main__":
    unittest.main()
