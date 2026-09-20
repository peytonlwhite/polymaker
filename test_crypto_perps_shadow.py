import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crypto_perps_shadow as perps
import repair_crypto_perps_shadow as recovery


def candle(value, ts):
    text = f"{value:.6f}"
    return {
        "end_period_ts": ts,
        "price": {"open": text, "high": text, "low": text, "close": text, "previous": text},
    }


class CryptoPerpsShadowTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        folder = Path(self._tempdir.name)
        for name, filename in {
            "PORTFOLIO_FILE": "portfolio.json",
            "REPORT_FILE": "report.json",
            "CACHE_FILE": "cache.json",
            "LOG_FILE": "perps.log",
            "EVENTS_FILE": "perps-events.jsonl",
            "FORECASTS_FILE": "forecasts.json",
        }.items():
            patcher = patch.object(perps, name, folder / filename)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_empty_paper_ledger_adopts_requested_starting_equity(self):
        original = perps.PORTFOLIO_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                perps.PORTFOLIO_FILE = Path(folder) / "portfolio.json"
                portfolio = perps.load_portfolio({"CRYPTO_PERPS_SHADOW_STARTING_BALANCE": "500"})
                self.assertEqual(portfolio["starting_balance"], 500.0)
                self.assertEqual(portfolio["balance"], 500.0)
        finally:
            perps.PORTFOLIO_FILE = original

    def test_write_json_retries_transient_windows_replace_denial(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            real_replace = Path.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("transient Windows file lock")
                return real_replace(source, destination)

            with patch.object(Path, "replace", new=flaky_replace), patch.object(
                perps.time, "sleep"
            ) as sleep:
                perps.write_json(target, {"ok": True})

            self.assertEqual(attempts, 3)
            self.assertEqual(target.read_text(encoding="utf-8"), '{\n  "ok": true\n}')
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_asset_mapping_handles_scaled_contracts(self):
        self.assertEqual(perps.asset_for_market({"ticker": "KXKSHIBPERP"}), "SHIB")
        self.assertEqual(perps.asset_for_market({"ticker": "KXBCHPERP"}), "BCH")

    def paper_position(self, direction=-1):
        opened = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        return {"id": "paper-position", "status": "open", "mode": "paper_shadow_only",
                "ticker": "KXDOGEPERP", "asset": "DOGE", "direction": direction,
                "side": "short" if direction < 0 else "long", "entry_price": 8.6,
                "contracts": 1.0, "entry_fee": .0043, "funding_pnl": 0.0,
                "opened_at": opened, "funding_updated_at": opened,
                "stop_pct": .02, "target_pct": .04, "margin": 8.6,
                "unrealized_pnl": 0.0, "max_favorable_move": 0.0}

    def test_invalid_exit_quotes_never_mutate_paper_economics(self):
        for direction, side in [(-1, "ask"), (1, "bid")]:
            for bad in [922337203685477.6, 0, -1, None, "bad", float("nan"), float("inf")]:
                with self.subTest(direction=direction, bad=bad):
                    position = self.paper_position(direction)
                    original = deepcopy(position)
                    portfolio = {"balance": 499.9957, "positions": [position], "history": []}
                    market = {"bid": 8.59, "ask": 8.61, "settlement_mark_price": {"price": 8.6}, side: bad}
                    self.assertEqual(perps.close_positions(portfolio, {position['ticker']: market}, {}, {}, {}), [])
                    self.assertEqual(portfolio['balance'], 499.9957)
                    self.assertEqual(portfolio['history'], [])
                    self.assertTrue(position.pop('data_quality_block'))
                    self.assertEqual(position, original)

    def test_crossed_and_mark_inconsistent_books_are_blocked(self):
        for bid, ask, mark in [(8.7, 8.5, 8.6), (8.59, 9e8, 8.6), (8.59, 8.61, 922337203685477.6)]:
            self.assertFalse(perps.executable_market_valid(
                {"bid": bid, "ask": ask, "settlement_mark_price": {"price": mark}}))

    def test_real_large_loss_still_closes_on_valid_book(self):
        position = self.paper_position(-1)
        portfolio = {"balance": 499.9957, "positions": [position], "history": []}
        market = {"bid": 11.99, "ask": 12.0, "settlement_mark_price": {"price": 12.0}}
        closed = perps.close_positions(portfolio, {position['ticker']: market}, {}, {}, {})
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['exit_reason'], 'stop_loss')
        self.assertAlmostEqual(closed[0]['profit'], -3.4103)
        self.assertAlmostEqual(portfolio['balance'], 496.5897)

    def test_good_quote_recovers_after_rejected_sentinel(self):
        position = self.paper_position()
        portfolio = {"balance": 499.9957, "positions": [position], "history": []}
        market = {"bid": 8.9, "ask": 922337203685477.6, "settlement_mark_price": {"price": 8.91}}
        perps.close_positions(portfolio, {position['ticker']: market}, {}, {}, {})
        market['ask'] = 8.92
        self.assertEqual(len(perps.close_positions(portfolio, {position['ticker']: market}, {}, {}, {})), 1)
        self.assertNotIn('data_quality_block', position)
        self.assertEqual(position['exit_price'], 8.92)

    def test_invalid_funding_cannot_change_cash(self):
        for mark, rate in [(922337203685477.6, .01), (8.6, float('nan')), (8.6, float('inf')), (8.6, 922337203685477.6)]:
            position = self.paper_position()
            original = deepcopy(position)
            portfolio = {"balance": 500.0}
            self.assertFalse(perps.accrue_funding(position,
                {'settlement_mark_price': {'price': mark}}, {'funding': {'funding_rate': rate}}, portfolio))
            self.assertEqual(position, original)
            self.assertEqual(portfolio['balance'], 500.0)

    def test_invalid_candidate_entry_never_debits_cash(self):
        for price in [0, -1, float('nan'), float('inf'), 922337203685477.6]:
            portfolio = {'balance': 500.0, 'positions': [], 'history': []}
            candidate = {'eligible': True, 'asset': 'DOGE', 'direction': -1,
                         'entry_price': price, 'mark_price': 8.6, 'signal_score': 2.0}
            self.assertEqual(perps.open_positions(portfolio, [candidate], {}), [])
            self.assertEqual(portfolio['balance'], 500.0)

    def test_repair_preserves_history_and_archives_exact_input_once(self):
        good = {**self.paper_position(), 'id': 'good', 'exit_price': 8.5,
                'exit_fee': .00425, 'profit': .09145, 'status': 'settled', 'result': 'WIN'}
        bad = {**self.paper_position(), 'exit_price': 922337203685477.6,
               'exit_fee': 4e11, 'profit': -1e15, 'status': 'settled', 'result': 'LOSS'}
        active = {**self.paper_position(), 'id': 'active', 'funding_pnl': .01}
        portfolio = {'mode': 'paper_shadow_only', 'starting_balance': 500,
                     'balance': -1e15, 'history': [good, bad], 'positions': [active]}
        perps.write_json(perps.PORTFOLIO_FILE, portfolio)
        original_bytes = perps.PORTFOLIO_FILE.read_bytes()
        result = recovery.apply_recovery(perps.PORTFOLIO_FILE, perps.PORTFOLIO_FILE.parent / 'archives')
        repaired = perps.read_json(perps.PORTFOLIO_FILE, {})
        self.assertEqual(repaired['history'], portfolio['history'])
        self.assertEqual(repaired['positions'], portfolio['positions'])
        self.assertAlmostEqual(repaired['balance'], 500.09285)
        with gzip.open(result['backup_path'], 'rb') as handle:
            self.assertEqual(handle.read(), original_bytes)
        before_repeat = perps.PORTFOLIO_FILE.read_bytes()
        self.assertFalse(recovery.apply_recovery(perps.PORTFOLIO_FILE, perps.PORTFOLIO_FILE.parent / 'archives')['changed'])
        self.assertEqual(perps.PORTFOLIO_FILE.read_bytes(), before_repeat)
        report = perps.summarize(repaired, [], [], [], [], {})
        self.assertEqual(report['settled_count'], 1)
        self.assertEqual(report['losses'], 0)
        self.assertEqual(report['data_integrity']['unscored_settlements'], 1)
        self.assertIn('incomplete', report['error'])
        self.assertEqual(report['cash_reconciliation']['residual_dollars'], 0)

    def test_repair_refuses_live_ledger(self):
        with self.assertRaises(ValueError):
            recovery.recovery_preview({'mode': 'live'})

    def test_aligned_liquid_trend_can_become_paper_candidate(self):
        closes = [5.0 * math.exp(index * 0.004) for index in range(40)]
        history = {
            "candles": [candle(value, index * 3600) for index, value in enumerate(closes)],
            "funding": {"funding_rate": 0.0},
        }
        market = {
            "ticker": "KXBTCPERP",
            "bid": f"{closes[-1] - 0.001:.6f}",
            "ask": f"{closes[-1] + 0.001:.6f}",
            "price": f"{closes[-1]:.6f}",
            "settlement_mark_price": {"price": f"{closes[-1]:.6f}"},
            "volume_24h_notional_value_dollars": "10000000",
        }
        candidate = perps.build_candidate(market, history, {})
        perps.apply_paper_probation([candidate], {"CRYPTO_PERPS_SHADOW_PROBATION_MIN_CONFIDENCE": "75"})
        self.assertEqual(candidate["side"], "long")
        self.assertTrue(candidate["eligible"])
        self.assertGreater(candidate["signal_score"], 0.8)
        self.assertEqual(candidate["eligibility_lane"], "paper_probation")

    def test_wide_spread_is_observed_but_not_entered(self):
        closes = [5.0 * math.exp(index * 0.004) for index in range(40)]
        history = {
            "candles": [candle(value, index * 3600) for index, value in enumerate(closes)],
            "funding": {"funding_rate": 0.0},
        }
        market = {
            "ticker": "KXBTCPERP", "bid": "4.90", "ask": "5.10", "price": "5.00",
            "settlement_mark_price": {"price": "5.00"},
            "volume_24h_notional_value_dollars": "10000000",
        }
        candidate = perps.build_candidate(market, history, {})
        self.assertIn("spread_too_wide", candidate["skip_reasons"])
        self.assertFalse(candidate["eligible"])

    def test_walk_forward_validation_uses_executable_sides_and_fees(self):
        closes = [5.0 * math.exp(index * 0.004) for index in range(220)]
        candles = []
        for index, value in enumerate(closes):
            row = candle(value, index * 3600)
            row.update({
                "bid": {"open": f"{value * 0.9999:.6f}", "close": f"{value * 0.9999:.6f}"},
                "ask": {"open": f"{value * 1.0001:.6f}", "close": f"{value * 1.0001:.6f}"},
                "volume_notional_value_dollars": str(100000 + index * 100),
                "open_interest_notional_value_dollars": str(500000 + index * 100),
            })
            candles.append(row)
        result = perps.walk_forward_validation(
            {"candles": candles},
            {"CRYPTO_PERPS_SHADOW_MAX_HOLD_HOURS": "4", "CRYPTO_PERPS_SHADOW_MIN_VALIDATION_TRADES": "20"},
        )
        self.assertGreaterEqual(result["trades"], 20)
        self.assertGreater(result["avg_net_return"], 0)
        self.assertTrue(result["passing"])
        self.assertEqual(result["strategy"], "ensemble_stop_target_time")

    def test_probation_lane_is_tiny_and_limited_to_one_open_position(self):
        portfolio = {"balance": 500.0, "positions": [], "history": []}
        base = {
            "eligible": True, "eligibility_lane": "paper_probation", "side": "long", "direction": 1,
            "entry_price": 5.0, "mark_price": 5.0, "signal_score": 1.8, "confidence": 82,
            "hourly_vol": 0.008, "stop_pct": 0.02, "target_pct": 0.036,
        }
        candidates = [
            {**base, "ticker": "KXBTCPERP", "asset": "BTC"},
            {**base, "ticker": "KXETHPERP", "asset": "ETH"},
        ]
        opened = perps.open_positions(portfolio, candidates, {})
        self.assertEqual(len(opened), 1)
        self.assertLessEqual(opened[0]["notional"], 2.5)
        self.assertEqual(opened[0]["eligibility_lane"], "paper_probation")

    def test_legacy_position_economics_remain_immutable_on_probation(self):
        portfolio = {
            "balance": 499.99,
            "history": [],
            "positions": [{
                "status": "open", "asset": "HYPE", "direction": -1, "entry_price": 5.0,
                "notional": 10.0, "margin": 10.0, "contracts": 2.0, "entry_fee": 0.005,
                "entry_snapshot": {"validation_avg_net_pct": -0.2, "validation_profit_factor": 0.5},
            }],
        }
        perps.open_positions(portfolio, [], {})
        position = portfolio["positions"][0]
        self.assertEqual(position["eligibility_lane"], "paper_probation")
        self.assertEqual(position["notional"], 10.0)
        self.assertEqual(position["margin"], 10.0)
        self.assertEqual(position["contracts"], 2.0)
        self.assertEqual(position["entry_fee"], 0.005)
        self.assertEqual(portfolio["balance"], 499.99)
        self.assertNotIn("probation_resized_at", position)

    def test_cross_market_context_records_relative_and_btc_regime(self):
        rows = [
            {"asset": "BTC", "direction": 1, "momentum_12h": 0.02, "hourly_vol": 0.01, "signal_score": 1.0, "confidence": 75, "skip_reasons": [], "eligible": True},
            {"asset": "SOL", "direction": 1, "momentum_12h": 0.05, "hourly_vol": 0.01, "signal_score": 1.0, "confidence": 75, "skip_reasons": [], "eligible": True},
        ]
        result = perps.apply_cross_market_context(rows, {})
        self.assertGreater(result[1]["cross_sectional_momentum"], 0)
        self.assertTrue(result[1]["btc_regime_aligned"])
        self.assertGreater(result[1]["confidence"], 75)

    def test_forward_forecasts_track_models_without_positions(self):
        original = perps.FORECASTS_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                perps.FORECASTS_FILE = Path(folder) / "forecasts.json"
                candidates = [{
                    "ticker": "KXBTCPERP", "asset": "BTC",
                    "model_scores": {"trend": 1.0, "reversal": -1.1, "ensemble": 0.9},
                }]
                markets = {"KXBTCPERP": {"bid": "5.00", "ask": "5.01"}}
                ledger = perps.update_forward_forecasts(candidates, markets, {})
                self.assertEqual(len(ledger["open"]), 6)
                self.assertEqual(ledger["history"], [])
                self.assertEqual(perps.summarize_forecasts(ledger)["open_count"], 6)
        finally:
            perps.FORECASTS_FILE = original

    def test_forecast_sanitizer_removes_exchange_sentinel_prices(self):
        sentinel = 922337203685477.6
        ledger = {
            "open": [
                {"entry_price": sentinel},
                {"entry_price": 5.0},
            ],
            "history": [
                {
                    "entry_price": 5.0,
                    "exit_price": sentinel,
                    "net_return_pct": -1e15,
                },
                {
                    "entry_price": 5.0,
                    "exit_price": 5.01,
                    "net_return_pct": 0.1,
                },
            ],
        }

        cleaned = perps.sanitize_forecast_ledger(ledger)

        self.assertEqual(len(cleaned["open"]), 1)
        self.assertEqual(len(cleaned["history"]), 1)
        self.assertEqual(
            cleaned["sanitization"]["last_invalid_history_removed"],
            1,
        )

    def test_historical_model_wins_do_not_overwrite_portfolio_wins(self):
        original_report = perps.REPORT_FILE
        original_forecasts = perps.FORECASTS_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                perps.REPORT_FILE = Path(folder) / "report.json"
                perps.FORECASTS_FILE = Path(folder) / "forecasts.json"
                portfolio = {
                    "starting_balance": 500.0,
                    "balance": 500.0,
                    "positions": [],
                    "history": [],
                }
                candidates = [{
                    "asset": "BTC",
                    "validation_trades": 20,
                    "validation_win_rate": 55.0,
                    "validation_avg_net_pct": 0.1,
                    "historical_model_replay": {
                        "trend": {"trades": 3, "wins": 2, "avg_net_pct": 0.2},
                    },
                }]
                report = perps.summarize(portfolio, [], candidates, [], [], {})
                self.assertEqual(report["settled_count"], 0)
                self.assertEqual(report["wins"], 0)
                self.assertEqual(report["losses"], 0)
                self.assertEqual(report["win_rate"], 0.0)
                self.assertEqual(report["historical_model_summary"][0]["wins"], 2)
        finally:
            perps.REPORT_FILE = original_report
            perps.FORECASTS_FILE = original_forecasts

    def test_specialist_models_are_exposed_for_shadow_comparison(self):
        closes = [5.0 + math.sin(index / 3) * 0.01 for index in range(60)]
        closes[-1] = closes[-2] * 0.96
        candles = []
        for index, value in enumerate(closes):
            row = candle(value, index * 3600)
            row["volume_notional_value_dollars"] = "1000000" if index == len(closes) - 1 else "10000"
            row["open_interest_notional_value_dollars"] = str(500000 + index * 1000)
            candles.append(row)
        features = perps.technical_features(perps.candle_rows({"candles": candles}))
        models = perps.model_raw_scores(features)
        self.assertIn("range_reversion", models)
        self.assertIn("volume_breakout", models)
        self.assertIn("liquidation_reversal", models)
        self.assertIn("trend_pullback", models)
        self.assertGreater(models["liquidation_reversal"], 0)

    def test_module_has_no_live_order_entry_path(self):
        self.assertFalse(hasattr(perps, "place_live_order"))
        self.assertFalse(hasattr(perps, "kalshi_private_request"))


if __name__ == "__main__":
    unittest.main()
