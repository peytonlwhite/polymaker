from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from crypto_btc_random_shadow import (ARMS, POLICY, RandomCycleLab, american_odds,
                                      btc_window, local_day, local_time, sizing)
from crypto_btc_shadow_forecast import completed_candles, forecast, calibration, advance_horizons
from crypto_btc_random_worker import allowed_request, ProductionQuotes

START = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
BEFORE = START-timedelta(seconds=30)
NOW = START+timedelta(seconds=5)
FEE = {"authoritative": True, "fee_type": "quadratic", "fee_multiplier": 1,
       "series_ticker": "KXBTC15M"}


def prediction(now=BEFORE, p=.57):
    return {"available": True, "generated_at": now.isoformat(), "raw_probability_up": p,
            "probability_up": p, "direction": "up" if p > .5 else "down", "horizons": {}}


def market(start=START):
    return {"ticker": f"KXBTC15M-TEST-{int(start.timestamp())}", "status": "active",
            "series_ticker": "KXBTC15M", "close_time": (start+timedelta(minutes=15)).isoformat(),
            "market_type": "binary", "strike_type": "greater_or_equal"}


def quote(ticker, side, contracts=1, price=50, now=NOW, **extra):
    return {"ticker": ticker, "side": side, "best_entry_price_cents": price,
            "full_size_entry_price_cents": price, "available_contracts": 100,
            "fetched_at": now.isoformat(), "book_consistent": True, "spread_cents": 2,
            "sequence_valid": True, **extra}


def candles(seconds, now=BEFORE, count=300, trend=.0001):
    end = int(now.timestamp())//seconds*seconds
    return [[end-(count-i)*seconds, 99, 101, 100, 100*(1+trend)**i, 10] for i in range(count)]


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"cycles.json"
        self.lab = RandomCycleLab(self.path, BEFORE)

    def prepare(self, p=.57):
        self.lab.prepare(prediction(p=p), BEFORE)
        self.lab.bind_markets([market()])

    def enter(self, price=50, now=NOW, confirm=None, snapshot=None, fee=FEE, clock=None):
        current = market()
        for side in ("yes", "no"):
            self.lab.enter(current, snapshot or quote(current["ticker"], side, price=price, now=now),
                           confirm or (lambda t, s, n: quote(t, s, n, price=price, now=now)),
                           fee, now, clock or (lambda: now))

    def test_six_independent_bankrolls_and_persisted_paired_draws(self):
        self.prepare()
        rows = {r["arm"]: r for r in self.lab.state["records"]}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows["random_fixed"]["side"], rows["random_recovery"]["side"])
        self.assertEqual(rows["prediction_fixed"]["side"], "yes")
        loaded = RandomCycleLab(self.path, NOW)
        loaded.prepare(prediction(p=.2), BEFORE+timedelta(seconds=10))
        self.assertEqual([(r["arm"], r["side"]) for r in loaded.state["records"]],
                         [(r["arm"], r["side"]) for r in self.lab.state["records"]])
        self.assertTrue(all(a["cash"] == 5000 for a in loaded.summary()["arms"]))

    def test_first_start_midwindow_and_exact_boundary_do_not_backfill(self):
        for current in (START, NOW, START-timedelta(minutes=2)):
            self.lab.prepare(prediction(current), current)
        self.assertEqual(self.lab.state["records"], [])

    def test_missing_or_future_model_does_not_block_random_or_fabricate_prediction(self):
        self.lab.prepare(prediction(START), BEFORE)
        self.assertEqual(len(self.lab.state["records"]), 4)
        self.assertTrue(all(r["probability_up"] == .5 for r in self.lab.state["records"]))

    def test_predictions_follow_down_without_changing_after_start(self):
        self.prepare(p=.4)
        self.lab.prepare(prediction(p=.8), NOW)
        selected = [r for r in self.lab.state["records"] if r["arm"].startswith("prediction")]
        self.assertTrue(all(r["side"] == "no" for r in selected))

    def test_btc_only_window_validation(self):
        self.assertIsNotNone(btc_window(market()))
        self.assertIsNone(btc_window({**market(), "ticker": "KXETH15M-TEST"}))
        self.assertIsNone(btc_window({**market(), "strike_type": "less"}))
        self.assertIsNone(btc_window({**market(), "status": "closed"}))
        self.assertIsNone(btc_window({**market(), "close_time": (START+timedelta(seconds=55)).isoformat()}))

    def test_random_entry_without_model_or_edge_and_integer_fee_inclusive_units(self):
        self.lab.prepare({}, BEFORE)
        self.lab.bind_markets([market()])
        self.enter()
        fixed = next(r for r in self.lab.state["records"] if r["arm"] == "random_fixed")
        self.assertEqual(fixed["status"], "filled")
        self.assertEqual(fixed["contracts"], 9)
        self.assertGreater(fixed["fees"], 0)
        self.assertLessEqual(fixed["cost"], 5)
        self.assertFalse(fixed["live_order"])
        self.assertAlmostEqual(self.lab.risk("random_fixed", NOW)["cash"], 5000-fixed["cost"])

    def test_odds_boundaries(self):
        self.assertAlmostEqual(american_odds(60), -150)
        self.assertAlmostEqual(american_odds(100/3), 200)
        for price, allowed in ((33, False), (100/3, True), (60, True), (61, False)):
            with self.subTest(price=price):
                self.lab = RandomCycleLab(Path(self.temp.name)/f"odds-{price}.json", BEFORE)
                self.prepare()
                self.enter(price)
                self.assertEqual(any(r["status"] == "filled" for r in self.lab.state["records"]), allowed)

    def test_confirmation_gates_stale_sequence_depth_spread_and_price_moves(self):
        bad = [{"fetched_at": (NOW-timedelta(seconds=3)).isoformat()}, {"sequence_valid": False},
               {"available_contracts": 0}, {"spread_cents": 8}, {"book_consistent": False},
               {"full_size_entry_price_cents": 51}, {"full_size_entry_price_cents": 33},
               {"ticker": "wrong"}, {"fetched_at": (NOW+timedelta(seconds=2)).isoformat()}]
        for i, extra in enumerate(bad):
            with self.subTest(extra=extra):
                self.lab = RandomCycleLab(Path(self.temp.name)/f"bad-{i}.json", BEFORE)
                self.prepare()
                def confirm(t, s, n):
                    return {**quote(t, s, n), **extra}
                self.enter(confirm=confirm)
                self.assertFalse(any(r["status"] == "filled" for r in self.lab.state["records"]))

    def test_decision_quote_stale_and_unknown_fees_never_confirm(self):
        self.prepare()
        confirm = Mock()
        for side in ("yes", "no"):
            self.enter(snapshot=quote(market()["ticker"], side, now=NOW-timedelta(seconds=3)), confirm=confirm)
        self.enter(fee={}, confirm=confirm)
        confirm.assert_not_called()

    def test_confirmation_crossing_cutoff_never_fills(self):
        self.prepare()
        late = START+timedelta(minutes=15)
        self.enter(confirm=lambda t, s, n: quote(t, s, n, now=late), clock=lambda: late)
        self.assertTrue(all(r["status"] == "waiting" for r in self.lab.state["records"]))

    def test_one_fill_per_cycle_restart_and_unsettled_blocks_new_draws(self):
        self.prepare()
        self.enter()
        rows = deepcopy(self.lab.state["records"])
        self.enter()
        self.assertEqual(self.lab.state["records"], rows)
        self.lab = RandomCycleLab(self.path, NOW)
        later = BEFORE+timedelta(minutes=15)
        self.lab.prepare(prediction(later), later)
        self.assertEqual(len(self.lab.state["records"]), 6)

    def test_settlement_official_final_only_cash_conservation_and_idempotence(self):
        self.prepare()
        self.enter()
        end = START+timedelta(minutes=16)
        for update in ({"status": "closed", "result": "yes"},
                       {"status": "finalized", "result": "yes", "is_provisional": True},
                       {"status": "finalized", "result": "yes", "settlement_value_dollars": ".5"},
                       {"status": "finalized", "result": ""}):
            self.lab.settle(lambda t: {"ticker": t, **update}, end)
            self.assertTrue(all(r["status"] == "filled" for r in self.lab.state["records"]))
        self.lab.settle(lambda t: {"ticker": t, "status": "finalized", "result": "yes"}, end)
        previous = deepcopy(self.lab.state)
        self.lab.settle(lambda t: self.fail("Repeated settlement"), end)
        self.assertEqual(previous, self.lab.state)
        for row in self.lab.state["records"]:
            risk = self.lab.risk(row["arm"], end)
            self.assertAlmostEqual(risk["cash"], 5000+row["profit"])
            self.assertAlmostEqual(row["profit"], row["payout"]-row["cost"])
        self.assertEqual(self.lab.state["forecasts"][0]["outcome"], 1)

    def test_no_fill_forecast_is_still_scored_and_cycle_expires(self):
        self.prepare()
        self.enter(80)
        end = START+timedelta(minutes=16)
        self.lab.settle(lambda t: {"ticker": t, "status": "finalized", "result": "no"}, end)
        self.assertTrue(all(r["status"] == "missed" for r in self.lab.state["records"]))
        self.assertEqual(self.lab.state["forecasts"][0]["outcome"], 0)
        self.assertEqual(self.lab.risk("random_fixed", end)["cash"], 5000)

    def test_settlement_failure_retains_position_and_does_not_redraw(self):
        self.prepare()
        self.enter()
        def failed(_):
            raise OSError("offline")
        self.lab.settle(failed, START+timedelta(hours=1))
        self.assertEqual(sum(r["status"] == "filled" for r in self.lab.state["records"]), 6)

    def test_balanced_random_deck_has_four_each_not_price_dependent(self):
        for i in range(8):
            when = BEFORE+timedelta(minutes=15*i)
            self.lab.prepare(prediction(when), when)
        selected = [r["side"] for r in self.lab.state["records"] if r["arm"] == "balanced_random"]
        self.assertEqual(selected.count("yes"), 4)
        self.assertEqual(selected.count("no"), 4)

    def test_recovery_is_capped_and_decreases_at_drawdown_or_long_streak(self):
        base = {"drawdown": 20, "loss_streak": 2, "cash": 4980, "daily_gross_loss": 0, "press_profit": 0}
        recovered = sizing("random_recovery", base, .6)
        self.assertAlmostEqual(recovered["recovery_requested"], .4)
        self.assertLessEqual(recovered["target_risk"], 7.5)
        self.assertEqual(sizing("random_recovery", {**base, "loss_streak": 6}, .6)["recovery_requested"], 0)
        self.assertLessEqual(sizing("random_recovery", {**base, "drawdown": 100}, .9)["target_risk"], 2.5)
        self.assertEqual(sizing("random_recovery", {**base, "drawdown": 250}, .9)["target_risk"], 0)
        self.assertEqual(sizing("random_recovery", {**base, "daily_gross_loss": 50}, .9)["target_risk"], 0)
        self.assertLessEqual(sizing("random_recovery", {**base, "daily_gross_loss": 49.8}, .9)["target_risk"], .2)

    def test_win_press_uses_profits_and_resets_after_loss(self):
        base = {"drawdown": 0, "loss_streak": 0, "cash": 5000, "daily_gross_loss": 0, "press_profit": 100}
        self.assertEqual(sizing("random_win_press", base, .5)["target_risk"], 7.5)
        self.assertEqual(sizing("random_win_press", {**base, "press_profit": 0}, .5)["target_risk"], 5)

    def test_all_loss_stress_stops_before_bankroll_can_be_exhausted(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                bankroll, daily_loss, losses = 5000., 0., 0
                for i in range(2000):
                    if i % 96 == 0:
                        daily_loss = 0.
                    risk = {"cash": bankroll, "drawdown": 5000-bankroll, "daily_gross_loss": daily_loss,
                            "loss_streak": losses, "press_profit": 0}
                    stake = sizing(arm, risk, .65)["target_risk"]
                    self.assertLessEqual(stake, 10)
                    daily_loss += stake
                    bankroll -= stake
                    losses += int(stake > 0)
                    self.assertLessEqual(daily_loss, 50.000001)
                self.assertGreaterEqual(bankroll, 4749.999999)

    def test_stale_or_wrong_series_fees_fail_closed(self):
        self.prepare()
        confirm = Mock()
        self.enter(fee={**FEE, "stale": True}, confirm=confirm)
        self.enter(fee={**FEE, "series_ticker": "KXETH15M"}, confirm=confirm)
        confirm.assert_not_called()

    def test_opposite_previous_outcome_does_not_change_future_random_draw(self):
        before = self.lab.random_side("random", START+timedelta(minutes=15))
        self.prepare()
        self.enter()
        self.lab.settle(lambda t: {"ticker": t, "status": "finalized", "result": "no"}, START+timedelta(minutes=16))
        self.assertEqual(self.lab.random_side("random", START+timedelta(minutes=15)), before)

    def test_archive_compaction_keeps_accounting_and_gzip_evidence(self):
        from crypto_evidence import retain_records, flush_evidence_archives, iter_jsonl
        self.prepare()
        self.enter()
        self.lab.settle(lambda t: {"ticker": t, "status": "finalized", "result": "yes"}, START+timedelta(minutes=16))
        expected = self.lab.risk("random_fixed", NOW)
        retain_records(self.lab.state, 0)
        flush_evidence_archives(self.path, self.lab.state)
        self.lab.persist()
        restored = RandomCycleLab(self.path)
        self.assertEqual(restored.risk("random_fixed", NOW), expected)
        paths = [self.path.parent/p for p in self.lab.state["evidence_archive_files"]]
        evidence = list(iter_jsonl(paths))
        self.assertEqual(len(evidence), 6)
        self.assertIn("signal_evidence", evidence[0]["record"])

    def test_chicago_day_and_daylight_saving_offsets(self):
        self.assertEqual(local_day("2026-09-18T02:00:00+00:00"), "2026-09-17")
        self.assertTrue(local_time("2026-09-18T02:00:00+00:00").endswith("-05:00"))
        self.assertTrue(local_time("2026-12-18T02:00:00+00:00").endswith("-06:00"))

    def test_corrupt_or_policy_changed_state_is_preserved_and_rejected(self):
        original = self.path.read_text()
        self.path.write_text("{")
        with self.assertRaises(ValueError):
            RandomCycleLab(self.path)
        self.assertEqual(self.path.read_text(), "{")
        state = json.loads(original)
        state["policy_hash"] = "different"
        self.path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            RandomCycleLab(self.path)
        self.assertEqual(json.loads(self.path.read_text())["policy_hash"], "different")


class ForecastTests(unittest.TestCase):
    def test_partial_future_and_gapped_candles_are_excluded(self):
        data = candles(60)
        original = completed_candles(data, 60, BEFORE)
        data.append([int(START.timestamp()), 1, 999999, 1, 999999, 1])
        self.assertEqual(completed_candles(data, 60, BEFORE), original)
        del data[-20]
        self.assertLess(len(completed_candles(data, 60, BEFORE)), 30)

    def test_all_horizons_require_real_history_and_trend_direction(self):
        for trend, direction in ((.0002, "up"), (-.0002, "down")):
            result = forecast(candles(60, trend=trend), candles(900, trend=trend), BEFORE)
            self.assertTrue(result["available"])
            self.assertEqual(result["direction"], direction)
            self.assertEqual(set(result["horizons"]), {"1h", "4h", "24h"})
            self.assertEqual(result["evaluation"]["matured"], 0)
            self.assertLess(abs(result["probability_up"]-.5), abs(result["horizons"]["1h"]["probability_up"]-.5))
        self.assertFalse(forecast(candles(60, count=100), candles(900), BEFORE)["available"])
        self.assertFalse(forecast(candles(60), candles(900, count=50), BEFORE)["available"])

    def test_calibration_cannot_use_other_assets_future_or_unresolved_labels(self):
        row = {"asset": "BTC", "status": "resolved", "resolved_at": BEFORE.isoformat(),
               "outcome": 1, "probability_up": .55, "raw_probability_up": .55}
        _, evaluation = calibration(.55, [row, {**row, "asset": "ETH"},
            {**row, "resolved_at": START.isoformat()}, {**row, "status": "open"}], BEFORE)
        self.assertEqual(evaluation["matured"], 1)
        self.assertAlmostEqual(evaluation["brier"], .45**2)

    def test_nonoverlapping_forecasts_use_exact_endpoint_and_expire_missing(self):
        minute, quarter = candles(60), candles(900)
        value = forecast(minute, quarter, BEFORE)
        records = []
        advance_horizons(records, value, minute, BEFORE)
        advance_horizons(records, value, minute, BEFORE+timedelta(seconds=5))
        self.assertEqual(len(records), 3)
        due = datetime.fromisoformat(records[0]["due_at"])
        advance_horizons(records, {}, candles(60, now=due), due)
        self.assertEqual(records[0]["status"], "resolved")
        self.assertEqual(records[1]["status"], "open")
        advance_horizons(records, {}, [], BEFORE+timedelta(days=2))
        self.assertEqual(records[1]["status"], "expired")
        self.assertEqual(records[2]["status"], "expired")


class WorkerTests(unittest.TestCase):
    def test_candle_request_has_explicit_bounds_to_avoid_stale_cached_response(self):
        bot = Mock(DEFAULT_SETTINGS={})
        ProductionQuotes(bot).candles(60)
        url = bot.http_json.call_args.args[0]
        self.assertIn("start=", url)
        self.assertIn("end=", url)
        self.assertTrue(allowed_request("GET", url))

    def test_network_allowlist_forbids_all_order_and_account_actions(self):
        base = "https://external-api.kalshi.com/trade-api/v2"
        self.assertTrue(allowed_request("GET", base+"/markets?series_ticker=KXBTC15M"))
        self.assertTrue(allowed_request("GET", base+"/markets/KXBTC15M-X/orderbook"))
        for method, url in (("POST", base+"/portfolio/orders"), ("DELETE", base+"/portfolio/orders/1"),
                            ("PUT", base+"/markets/X"), ("GET", base+"/portfolio/balance"),
                            ("GET", "https://example.com")):
            self.assertFalse(allowed_request(method, url))

    def test_discovery_uses_all_pages_and_rejects_repeated_cursor(self):
        bot = Mock(DEFAULT_SETTINGS={}, KALSHI_BASE="https://external-api.kalshi.com/trade-api/v2")
        quotes = ProductionQuotes(bot)
        bot.http_json.side_effect = [{"markets": [1], "cursor": "two"}, {"markets": [2]}]
        self.assertEqual(quotes.markets(), [1, 2])
        bot.http_json.side_effect = [{"markets": [1], "cursor": "two"}]*2
        with self.assertRaises(ValueError):
            quotes.markets()


if __name__ == "__main__":
    unittest.main()
