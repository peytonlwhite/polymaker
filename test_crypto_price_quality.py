import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import crypto_microstructure as micro
import crypto_paper_bettor as bot
import crypto_maker_shadow as maker_shadow
from crypto_market_stream import KalshiCryptoStream
from crypto_pricing import (
    conservative_edge_metrics,
    kalshi_order_fee,
    settlement_window_horizons,
)
from secure_settings import read_secure_settings, write_secure_settings


class PriceQualityTests(unittest.TestCase):
    def test_neutral_kalshi_quantity_book_is_neutral_at_unequal_prices(self):
        responses = [
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.80", "10"], ["0.79", "5"]],
                    "no_dollars": [["0.20", "10"], ["0.19", "5"]],
                }
            },
            {"trades": []},
        ]
        with patch.object(micro, "request_json", side_effect=responses):
            row = micro.kalshi_market_microstructure("https://example.test", "T")
        self.assertEqual(row["book_imbalance"], 0.0)
        self.assertEqual(row["yes_depth_contracts"], row["no_depth_contracts"])

    def test_trade_momentum_300s_excludes_hour_old_trade(self):
        now = 10_000.0
        trades = [
            {"timestamp": now - 3600, "price": 50, "size": 1, "direction": 1},
            {"timestamp": now - 200, "price": 100, "size": 1, "direction": 1},
            {"timestamp": now - 10, "price": 110, "size": 1, "direction": 1},
        ]
        features = micro.trade_flow_features(trades, now_ts=now)
        self.assertAlmostEqual(features["trade_momentum_300s"], __import__("math").log(1.1))

    def test_final_minute_settlement_window_horizons(self):
        normal = settlement_window_horizons(10, 60)
        self.assertAlmostEqual(normal["drift_minutes"], 9.5)
        self.assertAlmostEqual(normal["variance_minutes"], 10 - 2 / 3)
        inside = settlement_window_horizons(0.5, 60)
        self.assertAlmostEqual(inside["drift_minutes"], 0.25)
        self.assertAlmostEqual(inside["variance_minutes"], 1 / 6)

    def test_final_minute_probability_uses_observed_settlement_window(self):
        now = datetime.now(timezone.utc)
        settings = dict(bot.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_MARKET_ANCHOR_WEIGHT": "0",
            "CRYPTO_EXTREME_PROB_SHRINK": "0",
            "CRYPTO_15M_DYNAMIC_BASIS_BUFFER_ENABLED": "false",
            "CRYPTO_15M_SETTLEMENT_BASIS_BUFFER_BPS": "0",
        })
        market = {
            "close_time": (now + timedelta(seconds=30)).isoformat(),
            "floor_strike": 100,
        }
        base_model = {
            "spot": 100,
            "minute_vol": 0.01,
            "drift_per_minute": 0,
            "trend_drift_per_minute": 0,
            "reversion_drift_per_minute": 0,
        }
        no_partial = bot.probability_for_15m_market(market, base_model, settings, now=now)
        with_partial = bot.probability_for_15m_market(
            market,
            {
                **base_model,
                "settlement_partial_window": {
                    "average": 90,
                    "observations": 30,
                    "source_count": 1,
                },
            },
            settings,
            now=now,
        )
        self.assertTrue(with_partial["partial_settlement_window_active"])
        self.assertAlmostEqual(with_partial["effective_remaining_window_target"], 110, places=4)
        self.assertLess(with_partial["prob"], no_partial["prob"])

    def test_exact_fee_rounding_and_maker_schedule(self):
        schedule = {
            "series_ticker": "KXBTC15M",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
            "authoritative": True,
        }
        taker = kalshi_order_fee(47, 1, schedule=schedule, liquidity_role="taker")
        maker = kalshi_order_fee(47, 1, schedule=schedule, liquidity_role="maker")
        self.assertEqual(taker["fee_dollars"], 0.0175)
        self.assertEqual(maker["fee_dollars"], 0.0)
        self.assertTrue(taker["exact"])

    def test_lower_edge_bound_controls_trade(self):
        metrics = conservative_edge_metrics(
            60,
            "yes",
            55,
            fee_per_contract_dollars=0.02,
            expected_slippage_cents=1,
            interval={"p_low": 54, "p_high": 66, "probability_sigma_pp": 3},
        )
        self.assertGreater(metrics["expected_edge"], 0)
        self.assertLess(metrics["edge_low"], 0)

    def test_market_implied_probability_does_not_triple_count_last_trade(self):
        probability = bot.market_implied_yes_probability({
            "yes_bid": 40,
            "yes_ask": 42,
            "no_bid": 58,
            "no_ask": 60,
            "last_price": 90,
        })
        self.assertEqual(probability, 41)

    def test_kalshi_sequence_gap_invalidates_book(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="missing")
        stream._handle_message(json.dumps({
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 10,
            "msg": {"market_ticker": "T", "yes": [[40, 2]], "no": [[58, 2]]},
        }))
        self.assertTrue(stream.snapshot("T", max_age_seconds=10)["fresh"])
        stream._handle_message(json.dumps({
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 12,
            "msg": {"market_ticker": "T", "side": "yes", "price": 41, "delta": 1},
        }))
        self.assertFalse(stream.snapshot("T", max_age_seconds=10)["fresh"])
        self.assertEqual(stream.status()["sequence_gap_count"], 1)

    def test_coinbase_heartbeat_gap_schedules_backfill(self):
        stream = micro.CoinbaseMicrostructureStream({"BTC": "BTC-USD"})
        with patch.object(stream, "_schedule_trade_backfill") as schedule:
            stream._handle_message({"type": "heartbeat", "product_id": "BTC-USD", "last_trade_id": 100})
            stream._handle_message({"type": "heartbeat", "product_id": "BTC-USD", "last_trade_id": 103})
        schedule.assert_called_once_with("BTC-USD", 100, 103)
        self.assertEqual(stream.trade_gap_count, 1)

    def test_kraken_v2_checksum_validation(self):
        stream = micro.KrakenMicrostructureStream(["BTC"])
        message = json.loads(json.dumps({
            "channel": "book",
            "type": "snapshot",
            "data": [{
                "symbol": "BTC/USD",
                "bids": [
                    {"price": 45283.5, "qty": 0.10000000}, {"price": 45283.4, "qty": 1.54582015},
                    {"price": 45282.1, "qty": 0.10000000}, {"price": 45281.0, "qty": 0.10000000},
                    {"price": 45280.3, "qty": 1.54592586}, {"price": 45279.0, "qty": 0.07990000},
                    {"price": 45277.6, "qty": 0.03310103}, {"price": 45277.5, "qty": 0.30000000},
                    {"price": 45277.3, "qty": 1.54602737}, {"price": 45276.6, "qty": 0.15445238},
                ],
                "asks": [
                    {"price": 45285.2, "qty": 0.00100000}, {"price": 45286.4, "qty": 1.54571953},
                    {"price": 45286.6, "qty": 1.54571109}, {"price": 45289.6, "qty": 1.54560911},
                    {"price": 45290.2, "qty": 0.15890660}, {"price": 45291.8, "qty": 1.54553491},
                    {"price": 45294.7, "qty": 0.04454749}, {"price": 45296.1, "qty": 0.35380000},
                    {"price": 45297.5, "qty": 0.09945542}, {"price": 45299.5, "qty": 0.18772827},
                ],
                "checksum": 3310070434,
            }],
        }), parse_float=Decimal)
        bid_quantities = [
            "0.10000000", "1.54582015", "0.10000000", "0.10000000", "1.54592586",
            "0.07990000", "0.03310103", "0.30000000", "1.54602737", "0.15445238",
        ]
        ask_quantities = [
            "0.00100000", "1.54571953", "1.54571109", "1.54560911", "0.15890660",
            "1.54553491", "0.04454749", "0.35380000", "0.09945542", "0.18772827",
        ]
        for level, quantity in zip(message["data"][0]["bids"], bid_quantities):
            level["qty"] = Decimal(quantity)
        for level, quantity in zip(message["data"][0]["asks"], ask_quantities):
            level["qty"] = Decimal(quantity)
        stream._handle_message(message)
        self.assertEqual(stream.checksum_failure_count, 0)
        self.assertGreater(stream.snapshot("BTC")["mid"], 0)

    def test_one_source_lane_is_shadow_only(self):
        settings = dict(bot.DEFAULT_SETTINGS)
        candidate = {
            "side": "yes",
            "flow_review": {
                "direction": "yes",
                "strength": 0.20,
                "source_count": 1,
                "healthy_source_count": 1,
                "yes_source_count": 1,
                "no_source_count": 0,
            },
            "data_quality": {"source_freshness": 1.0, "kalshi_freshness": 1.0},
        }
        review = bot.crypto_15m_directional_confirmation_review(settings, candidate)
        self.assertFalse(review["ok"])
        self.assertEqual(review["reason"], "directional_insufficient_consensus")
        self.assertTrue(review["one_source_shadow_eligible"])
        self.assertEqual(review["lane"], "one_source_shadow")

    def test_maker_taker_shadow_requires_trade_fill_evidence(self):
        ledger = maker_shadow.empty_ledger()
        candidate = {
            "market_lane": "crypto_15m",
            "ticker": "T",
            "event_ticker": "E",
            "series_ticker": "KXBTC15M",
            "asset": "BTC",
            "side": "yes",
            "close_time": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
            "minutes_to_close": 8,
            "selected_side_probability_low": 60,
            "edge_low": 5,
            "exact_fee_cents": 1.75,
            "fee_schedule": {"fee_type": "quadratic", "fee_multiplier": 1, "authoritative": True},
            "kalshi_microstructure": {
                "best_yes_bid_cents": 50,
                "best_yes_entry_price_cents": 53,
                "last_trade_ts": 100,
            },
        }
        update = maker_shadow.update_candidates(ledger, [candidate])
        self.assertEqual(len(update["added"]), 1)
        self.assertEqual(ledger["records"][0]["status"], "awaiting_fill")
        candidate["kalshi_microstructure"]["last_trade_price_cents"] = 50
        candidate["kalshi_microstructure"]["last_trade_ts"] = 101
        update = maker_shadow.update_candidates(ledger, [candidate])
        self.assertEqual(update["filled"], [])
        self.assertEqual(ledger["records"][0]["status"], "awaiting_fill")
        self.assertEqual(
            ledger["records"][0]["equal_limit_trade_observations"],
            1,
        )
        candidate["kalshi_microstructure"]["last_trade_price_cents"] = 49
        candidate["kalshi_microstructure"]["last_trade_ts"] = 102
        update = maker_shadow.update_candidates(ledger, [candidate])
        self.assertEqual(len(update["filled"]), 1)
        self.assertEqual(
            update["filled"][0]["fill_evidence"],
            "public_trade_strictly_through_limit",
        )
        maker_shadow.settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            grace_minutes=0,
        )
        summary = maker_shadow.summarize(ledger)
        self.assertEqual(summary["settled"], 1)
        self.assertGreater(summary["maker_incremental_profit"], 0)
        self.assertEqual(summary["fill_model"], maker_shadow.STRICT_FILL_MODEL)
        self.assertFalse(summary["automatic_promotion"])
        self.assertEqual(summary["research_role"], "execution_overlay_only")
        self.assertTrue(summary["recommendation_only"])
        self.assertFalse(summary["independent_signal"])
        self.assertFalse(summary["promotion_eligible"])
        self.assertFalse(summary["activation_eligible"])
        self.assertFalse(
            summary["qualification_gates"][
                "independent_upstream_signal_validated"
            ]["passed"]
        )

    def test_legacy_equal_price_maker_fills_are_non_promotable(self):
        ledger = {
            "version": "crypto-maker-taker-shadow-v1",
            "mode": "shadow_only",
            "records": [{
                "id": "legacy",
                "ticker": "T",
                "event_ticker": "E",
                "status": "settled",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "fill_evidence": "public_trade_at_or_through_limit",
                "maker_price_cents": 50,
                "maker_fee_cents": 0,
                "taker_price_cents": 53,
                "taker_fee_cents": 1.75,
                "maker_profit_per_contract": 0.5,
                "taker_profit_per_contract": 0.4525,
            }],
        }
        summary = maker_shadow.summarize(ledger, minimum_independent_markets=1)
        self.assertEqual(summary["settled"], 0)
        self.assertEqual(summary["legacy_non_promotable"]["settled"], 1)
        self.assertFalse(ledger["records"][0]["eligible_for_promotion"])

    def test_paper_settlement_requires_official_finalized_result(self):
        settings = dict(bot.DEFAULT_SETTINGS)
        settings["CRYPTO_SETTLE_GRACE_MINUTES"] = "0"
        base_bet = {
            "status": "open",
            "mode": "paper",
            "ticker": "T",
            "asset": "BTC",
            "side": "yes",
            "close_time": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "contracts": 2,
            "stake": 1,
        }
        portfolio = {"balance": 10, "bets": [dict(base_bet)]}
        with patch.object(bot, "http_json", return_value={"market": {"status": "closed", "result": ""}}):
            self.assertEqual(bot.settle_open_bets(settings, portfolio, {"BTC": {"spot": 1_000_000}}), 0)
        with patch.object(bot, "http_json", return_value={"market": {"status": "finalized", "result": "yes", "expiration_value": "100"}}):
            self.assertEqual(bot.settle_open_bets(settings, portfolio, {"BTC": {"spot": 1}}), 1)
        self.assertEqual(portfolio["bets"][0]["settlement_source"], "kalshi_finalized_market")
        self.assertEqual(portfolio["bets"][0]["official_settlement_value"], 100)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_dpapi_secure_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.dpapi"
            write_secure_settings(path, {"CRYPTO_OPENAI_API_KEY": "secret"})
            self.assertEqual(read_secure_settings(path)["CRYPTO_OPENAI_API_KEY"], "secret")
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
