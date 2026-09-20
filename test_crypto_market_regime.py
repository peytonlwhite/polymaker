import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import crypto_paper_bettor as crypto
from crypto_15m_learning import candidate_snapshot
from crypto_market_regime import (
    annotate_candidates,
    build_regime_snapshot,
    persist_snapshot,
    summarize_dataset,
    update_forecast_records,
)


def asset_row(closes, flow=0.5):
    return {
        "spot": closes[-1],
        "closes": closes,
        "source": "test",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "microstructure": {
            "underlying_flow_score": flow,
            "settlement_proxy_source_count": 3,
            "coinbase": {
                "trade_flow_300s": flow,
                "trade_flow_900s": flow,
                "trade_flow_3600s": flow,
                "trade_notional_300s": 1000,
            },
            "kraken": {
                "trade_flow_300s": flow,
                "trade_flow_900s": flow,
                "trade_flow_3600s": flow,
                "trade_notional_300s": 800,
            },
        },
    }


class MarketRegimeTests(unittest.TestCase):
    def test_rising_price_and_buy_flow_create_up_regime(self):
        closes = [100 * (1.0003 ** index) for index in range(300)]
        snapshot = build_regime_snapshot({"BTC": asset_row(closes, 0.6)})

        self.assertEqual(snapshot["mode"], "shadow_only")
        self.assertFalse(snapshot["affects_execution"])
        self.assertEqual(snapshot["assets"]["BTC"]["direction"], "up")
        self.assertGreater(snapshot["assets"]["BTC"]["score_1h"], 0)
        self.assertGreater(snapshot["assets"]["BTC"]["features"]["aggressive_buy_sell_notional_5m"], 0)

    def test_daily_card_is_a_real_independent_24h_forecast(self):
        minute_closes = [100 * (1.0002 ** index) for index in range(300)]
        row = asset_row(minute_closes, 0.4)
        long_start = minute_closes[-1] / (1.0015 ** 299)
        row["closes_15m"] = [long_start * (1.0015 ** index) for index in range(300)]

        snapshot = build_regime_snapshot({"BTC": row})
        btc = snapshot["assets"]["BTC"]

        self.assertTrue(btc["forecast_horizons"]["24h"]["available"])
        self.assertEqual(btc["direction_24h"], "up")
        self.assertGreater(btc["score_24h"], 0)
        self.assertGreater(btc["probability_up_24h"], 0.5)
        self.assertEqual(snapshot["global"]["direction_24h"], "up")

    def test_daily_forecast_fails_neutral_when_long_history_is_missing(self):
        snapshot = build_regime_snapshot({"BTC": asset_row([100 + index for index in range(300)])})
        daily = snapshot["assets"]["BTC"]["forecast_horizons"]["24h"]

        self.assertFalse(daily["available"])
        self.assertEqual(daily["status"], "insufficient_history")
        self.assertEqual(daily["direction"], "neutral")
        self.assertEqual(daily["probability_up"], 0.5)

    def test_forward_ledger_deduplicates_origins_and_resolves_at_due_time(self):
        origin = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
        row = asset_row([100 * (1.0002 ** index) for index in range(300)], 0.5)
        long_start = row["spot"] / (1.001 ** 299)
        row["closes_15m"] = [long_start * (1.001 ** index) for index in range(300)]
        first = build_regime_snapshot({"BTC": row}, now=origin)

        records, evaluation = update_forecast_records([], first, now=origin)
        records, _ = update_forecast_records(records, first, now=origin + timedelta(minutes=5))
        self.assertEqual(len(records), 3)
        self.assertEqual(evaluation["horizons"]["1h"]["open_forecasts"], 1)

        matured = json.loads(json.dumps(first))
        matured["generated_at"] = (origin + timedelta(hours=1)).isoformat()
        matured["assets"]["BTC"]["spot"] *= 1.01
        records, evaluation = update_forecast_records(records, matured, now=origin + timedelta(hours=1))

        self.assertEqual(evaluation["horizons"]["1h"]["forecasts"], 1)
        self.assertEqual(evaluation["horizons"]["1h"]["directional_correct"], 1)
        self.assertEqual(evaluation["horizons"]["1h"]["independent_windows"], 1)
        self.assertIsNotNone(evaluation["horizons"]["1h"]["brier_score"])

    def test_candidate_annotation_never_changes_live_fields_and_is_capped(self):
        snapshot = build_regime_snapshot({"BTC": asset_row([100 + index for index in range(300)], 1.0)})
        snapshot["ai_shadow"] = {
            "enabled": True,
            "model": "test-model",
            "global": {"direction": "up", "confidence": 0.6},
            "assets": {},
        }
        candidate = {
            "asset": "BTC",
            "is_15m_market": True,
            "side": "yes",
            "model_prob_yes": 58.0,
            "edge": 4.0,
            "entry_price": 54.0,
            "confidence": 72,
        }
        original = dict(candidate)

        self.assertEqual(annotate_candidates([candidate], snapshot, maximum_adjustment_pp=0.25), 1)
        shadow = candidate["market_regime_shadow"]
        for key, value in original.items():
            self.assertEqual(candidate[key], value)
        self.assertFalse(shadow["affects_execution"])
        self.assertLessEqual(abs(shadow["yes_probability_adjustment_pp"]), 0.25)
        self.assertEqual(shadow["ai_model"], "test-model")

    def test_bounded_live_adjustment_updates_probability_and_edge(self):
        snapshot = build_regime_snapshot(
            {"BTC": asset_row([100 + index for index in range(300)], 1.0)}
        )
        candidate = {
            "asset": "BTC",
            "is_15m_market": True,
            "side": "yes",
            "model_prob_yes": 54.0,
            "entry_price": 51.0,
            "raw_edge": 3.0,
            "estimated_fee_edge_pp": 0.5,
            "net_edge": 2.5,
            "edge": 2.5,
            "confidence": 72,
        }

        annotate_candidates(
            [candidate],
            snapshot,
            execution_enabled=True,
            live_maximum_adjustment_pp=0.25,
            live_minimum_reliability=0.65,
        )

        live = candidate["market_regime_live"]
        self.assertTrue(live["applied"])
        self.assertTrue(live["affects_execution"])
        self.assertLessEqual(abs(live["yes_probability_adjustment_pp"]), 0.25)
        self.assertGreater(candidate["model_prob_yes"], 54.0)
        self.assertGreater(candidate["edge"], 2.5)
        self.assertEqual(candidate["confidence"], 72)

    def test_bounded_live_adjustment_fails_closed_when_snapshot_is_stale(self):
        snapshot = build_regime_snapshot(
            {"BTC": asset_row([100 + index for index in range(300)], 1.0)}
        )
        snapshot["generated_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        ).isoformat()
        candidate = {
            "asset": "BTC", "is_15m_market": True, "side": "yes",
            "model_prob_yes": 54.0, "entry_price": 51.0, "raw_edge": 3.0,
            "estimated_fee_edge_pp": 0.5, "net_edge": 2.5, "edge": 2.5,
            "confidence": 72,
        }

        annotate_candidates(
            [candidate],
            snapshot,
            execution_enabled=True,
            live_maximum_age_minutes=10,
        )

        self.assertFalse(candidate["market_regime_live"]["applied"])
        self.assertIn("snapshot_stale", candidate["market_regime_live"]["reasons"])
        self.assertEqual(candidate["model_prob_yes"], 54.0)
        self.assertEqual(candidate["edge"], 2.5)

    def test_learning_snapshot_keeps_shadow_prediction_for_resolution(self):
        regime = {"mode": "shadow_only", "original_yes_probability": 55, "shadow_yes_probability": 56}
        snapshot = candidate_snapshot({
            "ticker": "TEST-1",
            "asset": "BTC",
            "is_15m_market": True,
            "minutes_to_close": 5,
            "model_prob_yes": 55,
            "side": "yes",
            "entry_price": 51,
            "edge": 4,
            "confidence": 72,
            "probability": {"market_implied_yes": 51},
            "market_regime_shadow": regime,
        })
        self.assertEqual(snapshot["market_regime_shadow"], regime)
        self.assertEqual(snapshot["side"], "yes")

    def test_dataset_validation_requires_unique_markets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dataset.jsonl"
            rows = []
            for index in range(300):
                rows.append({
                    "ticker": f"MARKET-{index}",
                    "label_yes": 1,
                    "market_regime_shadow": {
                        "original_yes_probability": 50,
                        "shadow_yes_probability": 60,
                        "selected_side": "yes",
                        "alignment": "aligned",
                    },
                })
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            result = summarize_dataset(path)

        self.assertEqual(result["unique_markets"], 300)
        self.assertTrue(result["qualified_for_live"])
        self.assertGreater(result["brier_improvement"], 0)

    def test_snapshot_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "regime.json"
            for index in range(4):
                persist_snapshot(path, {"generated_at": f"2026-01-01T00:0{index}:00+00:00"}, maximum_history=2)
            state = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(state["history"]), 2)
        self.assertEqual(state["latest"]["generated_at"], "2026-01-01T00:03:00+00:00")

    def test_scan_context_persists_and_annotates_without_execution_effect(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_MARKET_REGIME_ENABLED": "true",
            "CRYPTO_MARKET_REGIME_AI_ENABLED": "false",
            "CRYPTO_NEWS_ENABLED": "false",
        })
        candidate = {
            "asset": "BTC", "is_15m_market": True, "side": "yes",
            "model_prob_yes": 55, "edge": 3, "entry_price": 52, "confidence": 71,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            regime_path = Path(temp_dir) / "regime.json"
            dataset_path = Path(temp_dir) / "dataset.jsonl"
            with patch.object(crypto, "CRYPTO_MARKET_REGIME_FILE", regime_path), patch.object(
                crypto, "CRYPTO_15M_TRAINING_DATASET_FILE", dataset_path
            ), patch.object(crypto, "event_line"):
                result = crypto.build_market_regime_scan_context(
                    settings,
                    {"BTC": asset_row([100 + index * 0.1 for index in range(300)], 0.5)},
                    {},
                    [candidate],
                )

        self.assertEqual(result["mode"], "shadow_only")
        self.assertFalse(result["affects_execution"])
        self.assertEqual(result["annotated_candidates"], 1)
        self.assertIn("market_regime_shadow", candidate)
        self.assertEqual(candidate["edge"], 3)

    def test_live_portfolio_can_disaster_recover_from_durable_ledger(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_LIVE_FAIL_CLOSED_PORTFOLIO": "true",
            "CRYPTO_LIVE_LEDGER_RECOVERY_ENABLED": "true",
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = root / "live.json"
            backup = root / "backup.json"
            ledger = root / "ledger.json"
            main.write_bytes(b"\0" * 100)
            backup.write_bytes(b"\0" * 100)
            ledger.write_text(json.dumps({"bets": {"row-1": {
                "id": "row-1", "mode": "live", "status": "settled",
                "placed_at": "2026-01-01T00:00:00+00:00",
            }}}), encoding="utf-8")
            with patch.object(crypto, "PORTFOLIO_FILE", main), patch.object(
                crypto, "LIVE_PORTFOLIO_FILE", main
            ), patch.object(crypto, "LIVE_PORTFOLIO_BACKUP_FILE", backup), patch.object(
                crypto, "LIVE_BOT_LEDGER_FILE", ledger
            ), patch.object(crypto, "log_line"):
                portfolio = crypto.load_portfolio(settings)

            self.assertEqual(len(portfolio["bets"]), 1)
            self.assertEqual(portfolio["bets"][0]["id"], "row-1")
            self.assertTrue(portfolio["live_ledger_disaster_recovery"]["requires_account_reconciliation"])
            self.assertEqual(json.loads(main.read_text(encoding="utf-8"))["bets"][0]["id"], "row-1")

    def test_local_ai_refresh_does_not_block_scan_thread(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_MARKET_REGIME_AI_ENABLED"] = "true"
        completed = {
            "enabled": True, "status": "ok", "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "global": {"direction_1h": "neutral", "direction_4h": "up", "confidence": 0.4},
            "assets": {},
        }

        def delayed_request(_settings, _snapshot):
            time.sleep(0.15)
            return completed

        crypto.MARKET_REGIME_AI_JOB = None
        crypto.MARKET_REGIME_AI_RESULT = None
        started = time.monotonic()
        with patch.object(crypto, "_request_market_regime_ai_shadow", side_effect=delayed_request):
            pending = crypto.run_market_regime_ai_shadow(settings, {"assets": {}}, {})
            elapsed = time.monotonic() - started
            crypto.MARKET_REGIME_AI_JOB.join(timeout=1)
            result = crypto.run_market_regime_ai_shadow(settings, {"assets": {}}, {})

        self.assertLess(elapsed, 0.10)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
