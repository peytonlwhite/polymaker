from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import shared_cash_pool as pool
import shared_bankroll as bankroll
import sports_paper_bettor as sports
import crypto_patient_runtime as runtime
import test_crypto_patient_promotion as patient_tests
from test_crypto_patient_promotion import candidate, settings


class Venue:
    def __init__(self, sports_cash="100", crypto_cash="0"):
        self.cash = {0: pool.amount(sports_cash), 2: pool.amount(crypto_cash)}
        self.allocation = {"allocations": [], "resting_margin_reservation": "sum"}
        self.posts = []
        self.transfer = None
        self.timeout = False
        self.destination_override = None

    def balance(self):
        return {"balance_dollars": str(sum(self.cash.values())), "portfolio_value": 0,
                "balance_breakdown": [{"exchange_index": i, "balance": str(v)} for i, v in self.cash.items()]}

    def request(self, path, method="GET", body=None):
        if method == "POST":
            self.posts.append((path, deepcopy(body)))
            if path == pool.ALLOCATION_PATH:
                self.allocation = deepcopy(body)
                if self.timeout:
                    raise TimeoutError()
                return {}
            if path != pool.TRANSFER_PATH:
                raise AssertionError("Only intra-account transfers allowed in this fake")
            # The journal must already contain the exact request on the wire.
            assert pool.read_object(pool.LEDGER)["pending"]["request"] == body
            self.transfer = {"transfer_id": "transfer-1", "source": "event_contract", "destination": "event_contract",
                             "source_exchange_shard": body["source_exchange_shard"],
                             "destination_exchange_shard": body["destination_exchange_shard"],
                             "amount": str(pool.Decimal(body["amount"]) / 10000), "status": "pending",
                             "created_ts": datetime.now().timestamp()}
            if self.timeout:
                raise TimeoutError()
            return {"transfer_id": "transfer-1"}
        if path == pool.ALLOCATION_PATH:
            return deepcopy(self.allocation)
        if path == "/portfolio/balance":
            return self.balance()
        if path.startswith("/markets/"):
            ticker = path.split("/")[-1]
            index = self.destination_override if self.destination_override is not None else (0 if ticker.startswith("SPORT") else 2)
            return {"market": {"ticker": ticker, "exchange_index": index}}
        if path == pool.TRANSFERS_PATH + "/transfer-1":
            return {"transfer": deepcopy(self.transfer)}
        raise AssertionError(path)

    def complete(self, *, reflect_balance=True):
        self.transfer["status"] = "complete"
        if reflect_balance:
            value = pool.amount(self.transfer["amount"])
            self.cash[self.transfer["source_exchange_shard"]] -= value
            self.cash[self.transfer["destination_exchange_shard"]] += value


class SharedCashTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module, attr, value in [(pool, "CONTROL", root / "control.json"), (pool, "LEDGER", root / "ledger.json"),
                                    (bankroll, "STATE_FILE", root / "state.json"), (bankroll, "STATE_LOCK_FILE", root / "state.lock")]:
            self.stack.enter_context(patch.object(module, attr, value))
        pool.write_json(bankroll.STATE_FILE, {"reservations": []})
        self.enable_local()
        self.venue = Venue()

    def enable_local(self):
        pool.write_json(pool.CONTROL, {"version": pool.VERSION, "enabled": True})
        pool.write_json(pool.LEDGER, {"version": pool.VERSION, "pending": None, "history": []})

    def run_scope(self, strategy="crypto", budget=10):
        ticker = "ETH-1" if strategy == "crypto" else "SPORT-1"
        with pool.cash_session(strategy, ticker, budget, self.venue.request) as result:
            return deepcopy(result)

    def test_disabled_means_no_account_access(self):
        pool.CONTROL.unlink()
        request = Mock(side_effect=AssertionError("account access"))
        with pool.cash_session("crypto", "ETH-1", 10, request) as row:
            self.assertEqual(row, {"ok": True, "enabled": False})
        request.assert_not_called()

    def test_funds_exact_deficit_and_waits_for_actual_arrival(self):
        self.venue = Venue("100", ".0052")
        first = self.run_scope()
        self.assertEqual(first["error"], "shared_cash_transfer_pending")
        self.assertEqual(self.venue.posts[0][1]["amount"], 99948)
        self.assertEqual(self.run_scope()["error"], "shared_cash_transfer_pending")
        self.venue.complete(reflect_balance=False)
        self.assertEqual(self.run_scope()["error"], "shared_cash_transfer_balance_unconfirmed")
        self.assertEqual(len(self.venue.posts), 1)
        self.venue.complete()
        self.assertTrue(self.run_scope()["ok"])
        self.assertEqual(len(self.venue.posts), 1)

    def test_either_bot_can_use_the_whole_free_pool_without_a_quota(self):
        for strategy, initial in (("sports", ("0", "100")), ("crypto", ("100", "0"))):
            with self.subTest(strategy=strategy):
                self.enable_local()
                self.venue = Venue(*initial)
                self.run_scope(strategy, budget=100)
                self.assertEqual(self.venue.posts[0][1]["amount"], 1000000)
                self.venue.complete()
                self.assertEqual(self.run_scope(strategy, 100)["capacity"], 100)

    def test_other_bot_reservation_protects_source_cash_including_fees(self):
        pool.write_json(bankroll.STATE_FILE, {"reservations": [
            {"status": "active", "stake": "80.25", "expires_at": "2000-01-01T00:00:00-06:00",
             "metadata": {"exchange_index": 0}}]})
        self.run_scope(budget=100)
        self.assertEqual(self.venue.posts[0][1]["amount"], 197500)

    def test_unknown_reservation_destination_fails_closed(self):
        pool.write_json(bankroll.STATE_FILE, {"reservations": [{"status": "active", "stake": 1, "metadata": {}}]})
        self.assertFalse(self.run_scope()["ok"])
        self.assertEqual(self.venue.posts, [])

    def test_uncertain_transfer_survives_next_attempt_without_resubmission(self):
        self.venue.timeout = True
        self.assertEqual(self.run_scope()["error"], "shared_cash_transfer_uncertain")
        self.assertEqual(self.run_scope("sports")["error"], "shared_cash_transfer_uncertain")
        self.assertEqual(len(self.venue.posts), 1)
        self.venue.complete()
        result = pool.bind_transfer(self.venue.request, "transfer-1")
        self.assertTrue(result["ok"])
        self.assertTrue(self.run_scope()["ok"])
        self.assertEqual(len(self.venue.posts), 1)

    def test_failed_transfer_and_wrong_identity_cannot_resume_entries(self):
        self.run_scope()
        self.venue.transfer["status"] = "failed"
        self.assertTrue(self.run_scope()["action_required"])
        self.venue.transfer.update(status="complete", amount="11")
        self.assertFalse(self.run_scope()["ok"])
        self.assertEqual(len(self.venue.posts), 1)

    def test_journal_write_failure_prevents_transfer(self):
        with patch.object(pool, "write_json", side_effect=OSError("disk full")):
            self.assertFalse(self.run_scope()["ok"])
        self.assertEqual(self.venue.posts, [])

    def test_lock_timeout_blocks_entry_without_any_account_request(self):
        @contextmanager
        def timeout():
            raise TimeoutError("lock held by another worker")
            yield
        with patch.object(bankroll, "shared_state_lock", timeout):
            self.assertEqual(self.run_scope()["error"], "shared_cash_preflight_failed")
        self.assertEqual(self.venue.posts, [])

    def test_missing_or_corrupt_journal_prevents_transfer(self):
        for content in (None, "{}", "garbage"):
            if content is None:
                pool.LEDGER.unlink()
            else:
                pool.LEDGER.write_text(content)
            self.assertFalse(self.run_scope()["ok"])
        self.assertEqual(self.venue.posts, [])

    def test_empty_pending_object_is_corruption_not_permission_to_repeat(self):
        pool.write_json(pool.LEDGER, {"version": pool.VERSION, "pending": {}, "history": []})
        self.assertFalse(self.run_scope()["ok"])
        self.assertEqual(self.venue.posts, [])

    def test_disabling_shared_reservations_blocks_activation_and_funding(self):
        with patch.object(bankroll, "load_settings", return_value={"SHARED_BANKROLL_RESERVATIONS_ENABLED": "false"}):
            self.assertFalse(self.run_scope()["ok"])
            with self.assertRaisesRegex(ValueError, "reservations must remain enabled"):
                pool.activate(self.venue.request)
        self.assertEqual(self.venue.posts, [])

    def test_fixed_target_or_changed_market_destination_blocks_routing(self):
        self.venue.allocation["allocations"] = [{"exchange_index": 0, "percent": 100}]
        self.assertEqual(self.run_scope()["error"], "shared_cash_fixed_allocation_present")
        self.venue.allocation["allocations"] = []
        self.venue.destination_override = 3
        self.assertFalse(self.run_scope()["ok"])
        self.assertEqual(self.venue.posts, [])

    def test_nonfinite_or_inconsistent_balance_cannot_be_spent(self):
        for change in ({"balance_dollars": "NaN"}, {"balance_dollars": "101"}, {"balance_breakdown": []}):
            raw = {**self.venue.balance(), **change}
            with self.assertRaises(ValueError):
                pool.balances(raw)

    def test_capacity_includes_fees_and_requires_current_thread_session(self):
        self.venue = Venue("0", "100")
        with pool.cash_session("crypto", "ETH-1", 10, self.venue.request) as row:
            self.assertTrue(row["ok"])
            self.assertIsNone(pool.capacity_review("crypto", "ETH-1", "10"))
            self.assertEqual(pool.capacity_review("crypto", "ETH-1", "10.01")["error"], "shared_cash_capacity_changed")
            self.assertIsNotNone(pool.capacity_review("sports", "SPORT-1", 1))
        self.assertIsNotNone(pool.capacity_review("crypto", "ETH-1", 1))

    def test_concurrent_requests_submit_only_one_transfer(self):
        barrier = threading.Barrier(3)
        results = []
        def work():
            barrier.wait()
            results.append(self.run_scope())
        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.venue.posts), 1)
        self.assertTrue(all(r["error"] == "shared_cash_transfer_pending" for r in results))

    def test_activation_preserves_resting_margin_rule_and_does_not_transfer(self):
        self.venue.allocation = {"allocations": [{"exchange_index": 0, "percent": 100}], "resting_margin_reservation": "max"}
        self.assertTrue(pool.activate(self.venue.request)["ok"])
        self.assertEqual(self.venue.posts, [(pool.ALLOCATION_PATH, {"allocations": [], "resting_margin_reservation": "max"})])
        self.assertTrue(pool.enabled())

    def test_activation_timeout_verifies_without_retry_and_never_claims_cash_moved(self):
        self.venue.timeout = True
        self.venue.allocation["allocations"] = [{"exchange_index": 2, "percent": 100}]
        result = pool.activate(self.venue.request)
        self.assertTrue(result["ok"])
        self.assertIn("Reload both", result["funding"]["message"])
        self.assertEqual(len(self.venue.posts), 1)
        self.assertEqual(self.venue.cash[0], 100)

    def test_activation_unknown_result_stays_disabled(self):
        request = Mock(side_effect=[{"allocations": [1]}, TimeoutError(), TimeoutError()])
        self.assertFalse(pool.activate(request)["ok"])
        self.assertFalse(pool.enabled())
        self.assertEqual(sum(c.kwargs.get("method") == "POST" for c in request.call_args_list), 1)

    def test_sports_transfer_never_calls_order_adapter(self):
        def request(*args, **kwargs):
            for key in ("api_key_id", "private_key_path", "private_key_pem"):
                kwargs.pop(key, None)
            return self.venue.request(*args, **kwargs), {}
        self.venue = Venue("0", "100")
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(sports, "SPORTS_LIVE_ORDER_ENABLED", True), \
                patch.object(sports, "kalshi_private_request", side_effect=request), \
                patch.object(sports, "_place_live_kalshi_order") as order:
            result = sports.place_live_kalshi_order({"kalshi_ticker": "SPORT-1"}, 10)
            self.assertEqual(result["error"], "shared_cash_transfer_pending")
            order.assert_not_called()

    def test_sports_funding_check_precedes_existing_price_and_risk_adapter(self):
        def request(*args, **kwargs):
            for key in ("api_key_id", "private_key_path", "private_key_pem"):
                kwargs.pop(key, None)
            return self.venue.request(*args, **kwargs), {}
        def existing(match, stake, portfolio):
            self.assertEqual(match["exchange_index"], 0)
            self.assertIsNone(pool.capacity_review("sports", "SPORT-1", 10.5))
            return {"ok": False, "error": "existing_risk_gate"}
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(sports, "SPORTS_LIVE_ORDER_ENABLED", True), \
                patch.object(sports, "kalshi_private_request", side_effect=request), \
                patch.object(sports, "_place_live_kalshi_order", side_effect=existing):
            self.assertEqual(sports.place_live_kalshi_order({"kalshi_ticker": "SPORT-1"}, 10)["error"], "existing_risk_gate")
        self.assertEqual(self.venue.posts, [])

    def test_patient_budget_is_sizing_ceiling_not_a_percent_allocation(self):
        raw = self.venue.balance()
        self.assertAlmostEqual(float(pool.patient_budget(raw, {"CRYPTO_ETH_PATIENT_BASE_STAKE_PCT": 1})), 2.6666666667)
        raw["portfolio_value_dollars"] = "100"
        del raw["portfolio_value"]
        self.assertAlmostEqual(float(pool.patient_budget(raw, {"CRYPTO_ETH_PATIENT_BASE_STAKE_PCT": 1})), 4.3333333333)

    def patient_adapter(self):
        adapter, sleep, mono = patient_tests.PatientRuntimeTests().adapter()
        adapter.kalshi_credentials = Mock(return_value={})
        adapter.kalshi_private_request = Mock(side_effect=lambda *a, **kw: (self.venue.request(*a, **kw), {}))
        def reconcile(*args):
            result = patient_tests.account(cash=float(sum(self.venue.cash.values())), now=adapter.utc_now())
            result["account"]["balance_raw"] = self.venue.balance()
            return result
        adapter.reconcile_live_account.side_effect = reconcile
        return adapter, sleep, mono

    def test_patient_waits_for_funding_then_refreshes_cash_quotes_and_preserves_size(self):
        adapter, advance, mono = self.patient_adapter()
        portfolio = {"bets": []}
        def sleep(seconds):
            if self.venue.transfer and self.venue.transfer["status"] == "pending":
                adapter.place_live_kalshi_order.assert_not_called()
                self.assertEqual(portfolio[patient_tests.p.FIELD]["records"]["ETH-1"]["status"], "waiting")
                self.venue.complete()
            advance(seconds)
        def submit(settings, portfolio, proposal, stake):
            self.assertGreater(bankroll._STATE_LOCK_LOCAL.depth, 0)
            self.assertIsNone(pool.capacity_review("crypto", "ETH-1", proposal[patient_tests.p.FIELD]["cost"]))
            self.assertEqual(proposal[patient_tests.p.FIELD]["base_budget"], 1)
            quote_at = proposal[patient_tests.p.FIELD]["confirmation"]["fetched_at"]
            self.assertEqual(quote_at, adapter.utc_now().isoformat())
            return {"ok": True}
        adapter.place_live_kalshi_order.side_effect = submit
        placed, report = runtime.run_patient_scan(adapter, settings(), portfolio, [candidate("ETH-1")], sleep=sleep, monotonic=mono)
        self.assertEqual(len(placed), 1)
        self.assertEqual(len(self.venue.posts), 1)
        self.assertEqual(mono(), 4)
        adapter.record_live_bet.assert_called_once()

    def test_patient_unknown_funding_never_creates_order_intent_or_calls_order_api(self):
        self.venue.timeout = True
        adapter, sleep, mono = self.patient_adapter()
        portfolio = {"bets": []}
        placed, report = runtime.run_patient_scan(adapter, settings(), portfolio, [candidate("ETH-1")], sleep=sleep, monotonic=mono)
        self.assertFalse(placed)
        self.assertEqual(report["status"], "blocked")
        row = portfolio[patient_tests.p.FIELD]["records"]["ETH-1"]
        self.assertEqual(row["status"], "waiting")
        self.assertNotIn("client_order_id", row)
        adapter.place_live_kalshi_order.assert_not_called()

    def test_patient_slow_cash_check_cannot_extend_entry_deadline(self):
        self.venue = Venue("0", "100")
        adapter, sleep, mono = self.patient_adapter()
        original = adapter.kalshi_private_request.side_effect
        def slow(*args, **kwargs):
            if args[0] == "/portfolio/balance":
                sleep(61)
            return original(*args, **kwargs)
        adapter.kalshi_private_request.side_effect = slow
        _, report = runtime.run_patient_scan(adapter, settings(), {"bets": []}, [candidate("ETH-1")], sleep=sleep, monotonic=mono)
        adapter.place_live_kalshi_order.assert_not_called()
        self.assertEqual(report["records"]["ETH-1"]["status"], "expired")

    def test_dashboard_rejects_the_superseded_exclusive_allocation_action(self):
        import dashboard
        with self.assertRaisesRegex(ValueError, "no percentage split"):
            dashboard.control_crypto_funding("full_crypto")

    def test_display_is_read_only_and_never_claims_exclusive_crypto_allocation(self):
        snapshot = pool.CONTROL.read_bytes()
        summary = pool.display_summary()
        self.assertFalse(summary["full_crypto_target_verified"])
        self.assertEqual(snapshot, pool.CONTROL.read_bytes())


if __name__ == "__main__":
    unittest.main()
