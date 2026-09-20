from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import crypto_patient_promotion as p
import crypto_patient_runtime as runtime
import crypto_paper_bettor as bot

NOW = datetime(2026, 9, 16, 18, tzinfo=timezone.utc)


def candidate(ticker="KXETH15M-TEST", price=60):
    venue = {"connected": True, "stream_age_seconds": .1, "book_age_seconds": .1,
             "trade_flow_60s": .4, "trade_flow_300s": .25, "trade_count_60s": 45}
    return {"ticker": ticker, "asset": "ETH", "market_lane": "crypto_15m", "market_kind": "above",
            "exchange_index": 2, "event_ticker": ticker, "close_time": (NOW + timedelta(minutes=12)).isoformat(),
            "yes_ask": price, "no_ask": 101-price, "fee_schedule": {"authoritative": True},
            "data_quality": {"score": 1}, "microstructure": {"coinbase": dict(venue), "kraken": dict(venue)},
            "kalshi_microstructure": {"fresh": True, "sequence_valid": True, "book_consistent": True,
                                      "age_seconds": .1, "spread_yes_cents": 1},
            "model_prob_yes": 50, "edge": -2, "confidence": 50}


def quotes(price=59, now=NOW, depth=10000):
    return lambda q: {"full_size_entry_price_cents": price if q <= depth else None, "available_contracts": depth,
                      "fetched_at": now.isoformat(), "sequence_valid": True, "source": "kalshi_rest_orderbook"}


def account(cash=1000, value=0, now=NOW):
    return {"generated_at": now.isoformat(), "account": {"ok": True, "positions_complete": True,
            "orders_complete": True, "cash_balance": cash, "portfolio_value_dollars": value, "resting_orders": [],
            "balance_raw": {"balance_breakdown": [{"exchange_index": 2, "balance": cash}]}}}


def settings(**overrides):
    return {**bot.DEFAULT_SETTINGS, p.ENABLED: "true", "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_LIVE_ORDER_ENABLED": "true", "CRYPTO_LIVE_DRY_RUN": "false",
            "CRYPTO_LIVE_REQUIRE_CONFIRMATION": "false", "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "true",
            "CRYPTO_15M_SPRINT_SHADOW_ONLY": "true", **overrides}


def risk(bankroll=1000, **extra):
    return {"ok": True, "reasons": [], "bankroll": bankroll, "available_cash": bankroll,
            "drawdown": 0, "last_loss": False, "recovery_spent": 0, **extra}


def prepared():
    portfolio = {"bets": []}
    engine = p.PatientPromotion(portfolio)
    engine.observe(candidate(), quotes(60), NOW)
    row = engine.prepare(candidate(price=59), quotes(), risk(), settings(), NOW)
    intent = engine.state["records"][row["ticker"]]
    intent.update(status="submitting", client_order_id=row[p.FIELD]["client_order_id"])
    return portfolio, row


class PatientRuleTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(p.enabled(bot.DEFAULT_SETTINGS))

    def test_sizing_uses_one_percent_baseline_and_exact_fees(self):
        row = candidate(price=59)
        for venue in row["microstructure"].values():
            venue.update(trade_flow_60s=.30, trade_flow_300s=.16)
        size, _ = p.size_order(row, "yes", risk(), quotes(), 59, settings(), NOW)
        self.assertEqual(size["base_budget"], 10)
        self.assertEqual(size["target_budget"], 10)
        self.assertLessEqual(size["cost"], 10)
        self.assertGreater(p.order_cost(row, 59, size["contracts"] + 1), 10)
        self.assertGreater(size["contracts"], 5)

    def test_bankroll_growth_and_contraction_resize(self):
        sizes = [p.size_order(candidate(), "yes", risk(b), quotes(), 59, settings(), NOW)[0] for b in (500, 1000, 2000)]
        self.assertEqual([s["base_budget"] for s in sizes], [5, 10, 20])
        self.assertLess(sizes[0]["contracts"], sizes[1]["contracts"])
        self.assertLess(sizes[1]["contracts"], sizes[2]["contracts"])

    def test_original_strength_tiers(self):
        row = candidate()
        self.assertEqual(p.signal_tier(row, "yes"), 4)
        for venue in row["microstructure"].values():
            venue.update(trade_flow_60s=.6, trade_flow_300s=.4, trade_count_60s=60)
        self.assertEqual(p.signal_tier(row, "yes"), 5)
        row["data_quality"]["score"] = .91
        self.assertEqual(p.signal_tier(row, "yes"), 1)

    def test_recovery_adds_only_one_contract(self):
        base, _ = p.size_order(candidate(), "yes", risk(), quotes(), 59, settings(), NOW)
        recovery, _ = p.size_order(candidate(), "yes", risk(last_loss=True), quotes(), 59, settings(), NOW)
        self.assertEqual(recovery["contracts"], base["contracts"] + 1)
        self.assertEqual(recovery["recovery_extra_contracts"], 1)
        self.assertAlmostEqual(recovery["recovery_extra_cost"], recovery["cost"] - base["cost"])

    def test_recovery_budget_and_drawdown_reduce_size(self):
        for risk_value in (risk(last_loss=True, recovery_spent=10), risk(last_loss=True, drawdown=20)):
            size, _ = p.size_order(candidate(), "yes", risk_value, quotes(), 59, settings(), NOW)
            self.assertEqual(size["recovery_extra_contracts"], 0)
        size, _ = p.size_order(candidate(), "yes", risk(drawdown=60), quotes(), 59, settings(), NOW)
        self.assertIsNone(size)

    def test_shallow_depth_and_cash_downsize(self):
        size, _ = p.size_order(candidate(), "yes", risk(available_cash=2), quotes(depth=2), 59, settings(), NOW)
        self.assertEqual(size["contracts"], 2)
        self.assertLessEqual(size["cost"], 2)

    def test_unknown_fee_stale_and_nan_never_size(self):
        row = candidate()
        row["fee_schedule"] = {}
        self.assertIsNone(p.size_order(row, "yes", risk(), quotes(), 59, settings(), NOW)[0])
        self.assertIsNone(p.size_order(candidate(), "yes", risk(), quotes(now=NOW-timedelta(seconds=2)), 59, settings(), NOW)[0])
        self.assertIsNone(p.size_order(candidate(), "yes", risk(), quotes(price=float("nan")), 59, settings(), NOW)[0])

    def test_anchor_uses_confirmed_baseline_and_never_chases(self):
        engine = p.PatientPromotion({})
        engine.observe(candidate(price=60), quotes(59), NOW)
        record = engine.state["records"][candidate()["ticker"]]
        self.assertEqual(record["anchor_cents"], 59)
        self.assertIsNone(engine.prepare(candidate(price=59), quotes(59), risk(), settings(), NOW))
        self.assertIsNotNone(engine.prepare(candidate(price=58), quotes(58), risk(), settings(), NOW))

    def test_patient_deadline_and_restart_keep_missed_entry(self):
        portfolio = {}
        engine = p.PatientPromotion(portfolio)
        engine.observe(candidate(), quotes(60), NOW)
        self.assertIsNone(engine.prepare(candidate(price=59), quotes(now=NOW+timedelta(seconds=61)), risk(), settings(), NOW+timedelta(seconds=61)))
        engine = p.PatientPromotion(deepcopy(portfolio))
        self.assertEqual(engine.observe(candidate(), quotes(), NOW)["status"], "expired")

    def test_fractional_anchor_requires_full_cent_improvement(self):
        engine = p.PatientPromotion({})
        engine.observe(candidate(price=61), quotes(60.4), NOW)
        self.assertIsNone(engine.prepare(candidate(price=60), quotes(60), risk(), settings(), NOW))
        self.assertIsNotNone(engine.prepare(candidate(price=59), quotes(59), risk(), settings(), NOW))

    def test_below_contract_maps_upward_flow_to_no(self):
        row = candidate()
        row.update(market_kind="below", no_ask=60, yes_ask=41)
        self.assertEqual(p.signal_review(row, NOW), ("no", []))
        self.assertEqual(p.signal_tier(row, "no"), 4)

    def test_direction_flip_or_unverified_account_blocks(self):
        engine = p.PatientPromotion({})
        engine.observe(candidate(), quotes(60), NOW)
        row = candidate(price=59)
        for venue in row["microstructure"].values():
            venue["trade_flow_60s"] *= -1
            venue["trade_flow_300s"] *= -1
        self.assertIsNone(engine.prepare(row, quotes(), risk(), settings(), NOW))
        self.assertIsNone(engine.prepare(candidate(price=59), quotes(), risk(ok=False, reasons=["account_incomplete"]), settings(), NOW))

    def test_equity_includes_positions_but_cannot_spend_them(self):
        result = p.risk_context({}, account(10, 990), candidate(), settings(), NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(result["bankroll"], 1000)
        self.assertEqual(result["available_cash"], 10)

    def test_missing_equity_shard_and_reconciliation_fail_closed(self):
        for mutation in (lambda a: a["account"].pop("portfolio_value_dollars"),
                         lambda a: a["account"].pop("balance_raw"),
                         lambda a: a["account"].update(orders_complete=False),
                         lambda a: a.update(unmatched_remote_tickers=["KXETH-X"]),
                         lambda a: a.update(generated_at=(NOW-timedelta(seconds=91)).isoformat())):
            snapshot = account()
            mutation(snapshot)
            self.assertFalse(p.risk_context({}, snapshot, candidate(), settings(), NOW)["ok"])

    def test_recovery_only_uses_observed_live_own_settlements(self):
        old = {"id": "old", "mode": "live", "strategy_owner": p.OWNER, "status": "settled", "profit": -3,
               "settled_at": (NOW-timedelta(seconds=1)).isoformat(), "placed_at": NOW.isoformat(), "ticker": "old"}
        future = {**old, "id": "future", "profit": 50, "settled_at": (NOW+timedelta(seconds=1)).isoformat()}
        paper = {**old, "id": "paper", "mode": "paper", "profit": 100}
        result = p.risk_context({"bets": [old, future, paper], "history": [old]}, account(), candidate(), settings(), NOW)
        self.assertEqual(result["drawdown"], 3)
        self.assertTrue(result["last_loss"])

    def test_chicago_date_and_gross_losses_include_open_risk(self):
        row = {"id": "loss", "mode": "live", "status": "settled", "profit": -10,
               "settled_at": "2026-09-16T04:59:00+00:00", "ticker": "old"}
        now = datetime(2026, 9, 16, 5, 1, tzinfo=timezone.utc)
        result = p.risk_context({"bets": [row]}, account(now=now), candidate(), settings(), now)
        self.assertEqual(result["daily_loss"], 0)
        row["settled_at"] = now.isoformat()
        result = p.risk_context({"bets": [row]}, account(now=now), candidate(), settings(CRYPTO_15M_DAILY_LOSS_CAP="12"), now)
        self.assertEqual(result["available_cash"], 2)

    def test_order_intent_and_actual_confirmation_freshness(self):
        portfolio, row = prepared()
        self.assertEqual(p.order_reasons(settings(), portfolio, row, NOW), [])
        self.assertTrue(p.order_reasons(settings(), {}, row, NOW))
        self.assertIn("confirmation_quote_stale", p.order_reasons(settings(), portfolio, row, NOW+timedelta(seconds=2)))


class PatientExecutionTests(unittest.TestCase):
    def setUp(self):
        # Fail the test if any real network operation escapes a mock.
        self.net = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.portfolio, self.row = prepared()
        patches = {
            "utc_now": Mock(return_value=NOW), "allow_live_trading": Mock(return_value=True),
            "kalshi_credentials": Mock(return_value={"api_key_id": "test", "private_key_path": "test", "private_key_pem": ""}),
            "reserve_live_order": Mock(side_effect=lambda strategy, stake, **kw: {"ok": True, "approved_stake": stake, "reservation_id": "r"}),
            "finalize_reservation": Mock(), "fresh_kalshi_executable_depth": Mock(return_value={"ok": True}),
            "kalshi_private_request": Mock(return_value=({"order": {"fill_count": self.row[p.FIELD]["contracts"], "yes_price": 59}}, {})),
        }
        for name, mock in patches.items():
            manager = patch.object(bot, name, mock)
            manager.start()
            self.addCleanup(manager.stop)

    def test_fok_uses_saved_id_and_exact_whole_contracts(self):
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertTrue(result["ok"], result)
        body = bot.kalshi_private_request.call_args.kwargs["body"]
        self.assertEqual(body["client_order_id"], self.row[p.FIELD]["client_order_id"])
        self.assertEqual(body["time_in_force"], "fill_or_kill")
        self.assertEqual(float(body["count"]), self.row[p.FIELD]["contracts"])
        self.assertEqual(bot.kalshi_private_request.call_count, 1)
        self.assertEqual(bot.reserve_live_order.call_args.args[1], self.row[p.FIELD]["cost"])
        self.assertEqual(bot.finalize_reservation.call_args.args[1], "filled_pending_persist")

    def test_unknown_order_response_or_exception_keeps_reservation(self):
        bot.kalshi_private_request.return_value = ({"order": {"status": "resting"}}, {})
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertEqual(result["error"], "live_order_exception")
        bot.finalize_reservation.assert_not_called()
        bot.kalshi_private_request.side_effect = TimeoutError("mock timeout")
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertEqual(result["error"], "live_order_exception")
        bot.finalize_reservation.assert_not_called()

    def test_definite_fok_no_fill_releases_reservation(self):
        bot.kalshi_private_request.return_value = ({"order": {"status": "canceled", "fill_count": 0}}, {})
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertEqual(result["error"], "live_order_not_filled")
        self.assertEqual(bot.finalize_reservation.call_args.args[1], "not_filled")

    def test_persistence_failure_does_not_release_filled_reservation(self):
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        with patch.object(bot, "log_line"), patch.object(bot, "event_line"), patch.object(bot, "update_live_bot_ledger"), patch.object(bot, "save_portfolio", side_effect=OSError("mock disk full")):
            with self.assertRaises(OSError):
                bot.record_live_bet(self.portfolio, self.row, result)
        self.assertEqual(bot.finalize_reservation.call_args.args[1], "filled_pending_persist")

    def test_controls_block_before_any_reservation(self):
        for key, value in ((p.ENABLED, "false"), ("CRYPTO_LIVE_DRY_RUN", "true"),
                           ("CRYPTO_LIVE_REQUIRE_CONFIRMATION", "true"), ("CRYPTO_LIVE_ORDER_ENABLED", "false"),
                           ("CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL", "false")):
            result = bot.place_live_kalshi_order(settings(**{key: value}), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
            self.assertFalse(result["ok"])
        bot.reserve_live_order.assert_not_called()
        bot.kalshi_private_request.assert_not_called()

    def test_shared_bankroll_downshift_cannot_change_qualified_order(self):
        bot.reserve_live_order.return_value = {"ok": True, "approved_stake": .59, "reservation_id": "r"}
        bot.reserve_live_order.side_effect = None
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertEqual(result["error"], "patient_shared_size_changed")
        bot.kalshi_private_request.assert_not_called()

    def test_expiration_during_preflight_never_submits(self):
        bot.utc_now.side_effect = [NOW, NOW+timedelta(seconds=2)]
        result = bot.place_live_kalshi_order(settings(), self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertEqual(result["error"], "patient_confirmation_expired")
        bot.kalshi_private_request.assert_not_called()

    def test_legacy_sprint_remains_frozen(self):
        row = candidate()
        row.update(side="yes", entry_price=59)
        result = bot.place_live_kalshi_order(settings(), {}, row, 10)
        self.assertEqual(result["error"], "crypto_15m_sprint_live_freeze")
        bot.reserve_live_order.assert_not_called()

    def test_only_patient_selection_blocks_other_live_paths_even_when_paused(self):
        for selected in ("true", "false"):
            controls = settings(CRYPTO_ETH_PATIENT_ONLY_LIVE="true", CRYPTO_ETH_PATIENT_ENABLED=selected,
                                CRYPTO_15M_SPRINT_SHADOW_ONLY="false", CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED="true")
            with patch.object(bot, "is_spot_flow_live_pilot_candidate", return_value=True):
                result = bot.place_live_kalshi_order(controls, {}, candidate("KXBTC15M-OTHER"), 10)
            self.assertEqual(result["error"], "patient_only_live_selection")
        bot.reserve_live_order.assert_not_called()
        bot.kalshi_private_request.assert_not_called()

    def test_only_patient_profile_can_place_the_selected_order(self):
        result = bot.place_live_kalshi_order(settings(CRYPTO_ETH_PATIENT_ONLY_LIVE="true",
                                                     MULTI_MARKET_LIVE_ENABLED="false"),
                                           self.portfolio, self.row, self.row[p.FIELD]["principal_stake"])
        self.assertTrue(result["ok"], result)


class PatientRuntimeTests(unittest.TestCase):
    def adapter(self, outcome=None):
        clock = {"seconds": 0}
        def now():
            return NOW + timedelta(seconds=clock["seconds"])
        def sleep(seconds):
            clock["seconds"] += seconds
        def fresh(row):
            result = deepcopy(row)
            result["yes_ask"] = 59 if clock["seconds"] else 60
            return result
        def depth(payload, side, required_contracts):
            return quotes(59 if clock["seconds"] else 60, now())(required_contracts)
        adapter = SimpleNamespace(
            execution_mode=Mock(return_value="live"), crypto_patient_readiness=Mock(return_value={"ok": True}),
            load_settings=Mock(side_effect=lambda: settings()), utc_now=now,
            build_execution_shadow_runtime=Mock(return_value={"fresh_snapshot": fresh}),
            KALSHI_CRYPTO_STREAM=SimpleNamespace(snapshot=Mock(return_value={"sequence_valid": True})),
            fetch_fresh_kalshi_orderbook=Mock(side_effect=lambda *a, **kw: ({}, now().isoformat())),
            kalshi_orderbook_depth_summary=depth, save_portfolio=Mock(),
            reconcile_live_account=Mock(side_effect=lambda *a: account(now=now())),
            place_live_kalshi_order=Mock(return_value=outcome or {"ok": True}),
            record_live_bet=Mock(return_value={"id": "filled"}),
            build_execution_quality_record=Mock(return_value={}), persist_execution_quality_record=Mock(), event_line=Mock(),
            effective_scan_bet_limit=Mock(return_value=1),
        )
        return adapter, sleep, lambda: clock["seconds"]

    def test_disabled_runtime_has_no_external_effects(self):
        adapter, sleep, mono = self.adapter()
        runtime.run_patient_scan(adapter, {}, {}, [candidate()], sleep=sleep, monotonic=mono)
        adapter.save_portfolio.assert_not_called()
        adapter.place_live_kalshi_order.assert_not_called()

    def test_new_market_uses_ready_feeds_without_five_minute_delay(self):
        adapter, sleep, mono = self.adapter()
        placed, report = runtime.run_patient_scan(adapter, settings(), {"bets": []},
                                                 [candidate("KXETH15M-NEW")], sleep=sleep, monotonic=mono)
        self.assertEqual(len(placed), 1)
        self.assertEqual(mono(), 2)
        self.assertEqual(report["runtime_version"], runtime.RUNTIME_VERSION)

    def test_empty_market_list_is_watching_not_warming_up(self):
        adapter, sleep, mono = self.adapter()
        _, report = runtime.run_patient_scan(adapter, settings(), {}, [], sleep=sleep, monotonic=mono)
        self.assertEqual(report["status"], "watching")
        self.assertEqual(report["candidate_count"], 0)
        adapter.build_execution_shadow_runtime.assert_not_called()

    def test_new_market_still_rejects_stale_venue_data(self):
        adapter, sleep, mono = self.adapter()
        row = candidate()
        row["microstructure"]["coinbase"]["stream_age_seconds"] = 10
        _, report = runtime.run_patient_scan(adapter, settings(), {}, [row], sleep=sleep, monotonic=mono)
        self.assertEqual(report["rejections"]["coinbase_stale_or_missing"], 1)
        adapter.place_live_kalshi_order.assert_not_called()
        adapter.fetch_fresh_kalshi_orderbook.assert_not_called()

    def test_patient_wait_persists_intent_then_uses_normal_accounting(self):
        adapter, sleep, mono = self.adapter()
        portfolio = {"bets": []}
        def submit(*args):
            intent = portfolio[p.FIELD]["records"][candidate()["ticker"]]
            self.assertEqual(intent["status"], "submitting")
            self.assertTrue(adapter.save_portfolio.called)
            return {"ok": True}
        adapter.place_live_kalshi_order.side_effect = submit
        placed, report = runtime.run_patient_scan(adapter, settings(), portfolio, [candidate()], sleep=sleep, monotonic=mono)
        self.assertEqual(len(placed), 1)
        self.assertEqual(report["placed"], 1)
        self.assertEqual(mono(), 2)
        adapter.record_live_bet.assert_called_once()

    def test_timeout_is_not_retried_after_restart(self):
        adapter, sleep, mono = self.adapter({"ok": False, "error": "live_order_exception"})
        portfolio = {"bets": []}
        runtime.run_patient_scan(adapter, settings(), portfolio, [candidate()], sleep=sleep, monotonic=mono)
        _, report = runtime.run_patient_scan(adapter, settings(), deepcopy(portfolio), [candidate()], sleep=sleep, monotonic=mono)
        self.assertEqual(report["reasons"], ["unresolved_order_intent"])
        adapter.place_live_kalshi_order.assert_called_once()

    def test_disabling_while_waiting_prevents_submission(self):
        adapter, sleep, mono = self.adapter()
        adapter.load_settings.side_effect = [settings(), settings(**{p.ENABLED: "false"})]
        _, report = runtime.run_patient_scan(adapter, settings(), {}, [candidate()], sleep=sleep, monotonic=mono)
        self.assertEqual(report["status"], "blocked")
        adapter.place_live_kalshi_order.assert_not_called()

    def test_scan_limit_applies_across_simultaneous_opportunities(self):
        adapter, sleep, mono = self.adapter()
        rows = [candidate("KXETH15M-A"), candidate("KXETH15M-B")]
        placed, _ = runtime.run_patient_scan(adapter, settings(), {"bets": []}, rows, sleep=sleep, monotonic=mono)
        self.assertEqual(len(placed), 1)
        adapter.place_live_kalshi_order.assert_called_once()

    def test_missed_patient_entry_expires_without_immediate_fallback(self):
        adapter, sleep, mono = self.adapter()
        adapter.build_execution_shadow_runtime.return_value = {"fresh_snapshot": deepcopy}
        _, report = runtime.run_patient_scan(adapter, settings(), {}, [candidate()], sleep=sleep, monotonic=mono)
        self.assertEqual(report["records"][candidate()["ticker"]]["status"], "expired")
        adapter.place_live_kalshi_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
