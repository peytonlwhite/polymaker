import gzip
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_prospective_shadow as research
from crypto_execution_safety import complete_pages, shard_cash, confirmation_reasons
from crypto_evidence import retain_records, flush_evidence_archives, iter_jsonl, jsonl_sources
from crypto_pricing import kalshi_order_fee, kalshi_fill_fee
from crypto_spot_flow_live_pilot import validation_review
from crypto_market_stream import KalshiCryptoStream
from test_crypto_asset_specialist_shadow import candidate as specialist_candidate

NOW = datetime(2026, 9, 3, 15, tzinfo=timezone.utc)


def candidate(*args, **kwargs):
    row = specialist_candidate(*args, **kwargs)
    for venue in row["microstructure"].values():
        venue["book_age_seconds"] = .2
    for side in ("yes", "no"):
        row[side + "_ask"] = row["kalshi_microstructure"]["best_" + side + "_entry_price_cents"]
    return row


def quote(_ticker, side, contracts, price=60, now=NOW):
    return {"fetched_at": now.isoformat(), "checked_at": now.isoformat(),
            "sequence_valid": True, "full_size_entry_price_cents": price,
            "available_contracts": 20, "source": "independent_rest"}


class ExecutionEvidenceTests(unittest.TestCase):
    def test_background_training_never_blocks_or_reuses_old_evaluation(self):
        import crypto_paper_bettor as bettor
        from concurrent.futures import Future
        from unittest.mock import Mock
        pending = Future()
        with patch.object(bettor, "LEARNING_TRAINING_EXECUTOR", Mock()), patch.object(bettor, "LEARNING_TRAINING_FUTURE", pending):
            state = bettor.background_learning_state({"status": "active", "active": True}, {})
            self.assertEqual(state["status"], "collecting")
            self.assertTrue(state["training_in_background"])
            self.assertFalse(state["active"])
            pending.set_result({"status": "shadow", "active": False, "evaluation_revision": bettor.EVALUATION_REVISION})
            result = bettor.background_learning_state({}, {})
            self.assertEqual(result["evaluation_revision"], bettor.EVALUATION_REVISION)
            self.assertIsNone(bettor.LEARNING_TRAINING_FUTURE)

    def test_all_pages_and_shards_are_required(self):
        paths = []
        def request(path):
            paths.append(path)
            return ({"market_positions": [{"ticker": "B"}], "cursor": ""} if "cursor=" in path
                    else {"market_positions": [{"ticker": "A"}], "cursor": "page 2"}), {}
        rows = complete_pages(request, "/portfolio/positions", "market_positions", params={"limit": 1000})
        self.assertEqual([r["ticker"] for r in rows], ["A", "B"])
        self.assertIn("cursor=page+2", paths[1])
        self.assertNotIn("exchange_index", paths[0])

    def test_repeated_cursor_and_partial_errors_cannot_be_success(self):
        with self.assertRaisesRegex(ValueError, "repeated_portfolio_cursor"):
            complete_pages(lambda _: ({"orders": [], "cursor": "same"}, {}), "/orders", "orders")
        with self.assertRaises(ValueError):
            complete_pages(lambda _: ({}, {}), "/orders", "orders")

    def test_shard_zero_and_unfunded_exchange_never_use_total_cash(self):
        account = {"cash_balance": 5000, "balance_raw": {"balance_breakdown": [
            {"exchange_index": 0, "balance": "15.1256"}, {"exchange_index": 2, "balance": "0.0000"}]}}
        self.assertEqual(shard_cash(account, 0), 15.1256)
        self.assertEqual(shard_cash(account, 2), 0)
        self.assertIsNone(shard_cash(account, 1))
        self.assertIsNone(shard_cash(account, None))

    def test_confirmation_future_missing_and_stale_fail(self):
        self.assertTrue(confirmation_reasons({}, now=NOW))
        self.assertTrue(confirmation_reasons({"fetched_at": (NOW + timedelta(seconds=1)).isoformat()}, now=NOW))
        self.assertIn("confirmation_quote_stale", confirmation_reasons({"fetched_at": (NOW - timedelta(seconds=3)).isoformat()}, now=NOW))
        self.assertEqual(confirmation_reasons({"fetched_at": NOW.isoformat()}, now=NOW), [])

    def test_disabled_gates_are_bypassed_never_passed(self):
        settings = {"CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED": "true",
                    "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_TOURNAMENT_GATE": "false",
                    "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_FORWARD_GATE": "false"}
        result = validation_review({}, {"records": []}, settings)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["gates"]["spot_tournament_interim"])
        self.assertEqual(result["gate_status"]["spot_tournament_interim"], "bypassed")

    def test_decimal_fee_and_precision_are_explicit(self):
        fee = kalshi_order_fee(40, 1, schedule={"authoritative": True})
        self.assertEqual(fee["fee_dollars"], .0168)
        self.assertEqual(fee["trade_fee_dollars"], .0168)
        self.assertAlmostEqual(fee["conservative_fee_dollars"], .02)
        self.assertFalse(fee["net_fee_exact"])
        aligned = kalshi_fill_fee(-.4, .0168, balance_precision=.01)
        self.assertEqual(aligned["balance_change_dollars"], -.42)
        self.assertAlmostEqual(aligned["net_fee_dollars"], .02)
        with self.assertRaises(ValueError):
            kalshi_fill_fee(-.4, .0168, balance_precision=.1)

    def test_trade_message_does_not_refresh_depth_or_restore_invalid_book(self):
        # Constructor takes credentials but no network action occurs until start().
        stream = KalshiCryptoStream(api_key_id="unused", private_key_path="unused")
        with patch("crypto_market_stream.time.time", return_value=1000):
            stream._update_snapshot("T", {"yes_levels": [[40, 10]], "no_levels": [[59, 10]],
                "sequence_valid": True, "book_consistent": True}, "orderbook_snapshot")
        with patch("crypto_market_stream.time.time", return_value=1004):
            stream._update_snapshot("T", {"trade_count_60s": 1}, "trade")
            self.assertFalse(stream.snapshot("T")["fresh"])
            stream._invalidate_books()
            stream._update_snapshot("T", {"best_yes_entry_price_cents": 41}, "ticker")
            self.assertFalse(stream.snapshot("T")["fresh"])
            self.assertIsNone(stream.orderbook_payload("T"))

    def test_lossless_compaction_keeps_outcomes_and_archives_raw(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "ledger.json"
            ledger = {"records": [{"id": "a", "status": "settled", "profit": .5,
                                    "fresh_input_snapshot": {"test": 1}}]}
            retain_records(ledger, 0)
            self.assertEqual(len(ledger["records"]), 1)
            self.assertEqual(ledger["records"][0]["profit"], .5)
            flush_evidence_archives(path, ledger)
            archive = Path(work) / ledger["evidence_archive_files"][0]
            restored = list(iter_jsonl([archive]))
            self.assertEqual(restored[0]["record"]["fresh_input_snapshot"], {"test": 1})
            self.assertNotIn("_pending_evidence_archives", ledger)

    def test_compressed_dataset_is_read_when_active_file_is_absent(self):
        from crypto_15m_learning import load_dataset, FEATURE_SCHEMA_VERSION
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); (root / "archives").mkdir()
            with gzip.open(root / "archives" / "data.jsonl.2026.gz", "wt", encoding="utf-8") as out:
                out.write(json.dumps({"ticker": "T", "label_yes": 1, "features": {},
                                      "feature_schema_version": FEATURE_SCHEMA_VERSION}) + "\n")
            self.assertEqual(len(load_dataset(root / "data.jsonl")), 1)


class ProspectiveResearchTests(unittest.TestCase):
    def run_capture(self, row=None, confirm=quote, refresh=None, now=NOW):
        ledger = research.empty_ledger(now=NOW)
        research.capture(ledger, [row or candidate()], refresh or (lambda c: c), confirm, now=now)
        return ledger

    def test_eth_assignment_controls_and_shared_portfolio_are_not_double_counted(self):
        ledger = self.run_capture()
        eth = [r for r in ledger["records"] if r["lane"].startswith("eth_")]
        self.assertEqual(len(eth), 2)
        self.assertEqual(sum(r["primary_sample"] for r in eth), 1)
        self.assertEqual(sum(r["portfolio_included"] for r in ledger["records"]), 1)
        research.capture(ledger, [candidate()], lambda c: c, quote, now=NOW)
        self.assertEqual(len([r for r in ledger["records"] if r["lane"].startswith("eth_")]), 2)

    def test_stale_confirmation_and_deleted_signal_cannot_enter(self):
        stale = self.run_capture(confirm=lambda *a: quote(*a, now=NOW - timedelta(seconds=3)))
        # checked_at must reflect the actual decision, not the stale payload time.
        stale = self.run_capture(confirm=lambda *a: {**quote(*a, now=NOW - timedelta(seconds=3)), "checked_at": NOW.isoformat()})
        self.assertFalse(stale["records"])
        calls = [0]
        def refresh(row):
            calls[0] += 1
            if calls[0] == 1:
                return row
            return candidate(direction=1)
        changed = self.run_capture(refresh=refresh)
        self.assertFalse(changed["records"])

    def test_no_chase_and_one_cent_control_differ_at_confirmation(self):
        row = candidate("BTC")
        ledger = self.run_capture(row, confirm=lambda *a: quote(*a, price=61))
        self.assertEqual([r["lane"] for r in ledger["records"]], ["spot_one_cent_control"])
        self.assertFalse(ledger["records"][0]["portfolio_included"])

    def test_favorite_control_tracks_when_empirical_interval_is_unqualified(self):
        row = candidate("ETH", minutes=4)
        row["probability"] = {"target_distance_sigma": -2}
        row["probability_interval"] = {"p_yes": 10, "p_low": 1, "p_high": 20, "interval_status": "input_quality_interval_only"}
        ledger = self.run_capture(row)
        self.assertNotIn("distance_favorite", [r["lane"] for r in ledger["records"]])
        self.assertIn("distance_price_time_control", [r["lane"] for r in ledger["records"]])
        self.assertTrue(any("empirical_interval_unqualified" in r["reasons"] for r in ledger["attempts"]))

    def test_entry_after_close_or_before_registration_is_rejected(self):
        self.assertFalse(self.run_capture(now=NOW + timedelta(minutes=20))["records"])
        self.assertFalse(self.run_capture(now=NOW - timedelta(seconds=1),
            confirm=lambda *a: quote(*a, now=NOW - timedelta(seconds=1)))["records"])

    def test_only_official_final_outcomes_settle_and_stress_is_separate(self):
        ledger = self.run_capture()
        later = NOW + timedelta(minutes=20)
        research.settle(ledger, lambda _: {"status": "closed", "result": "no"}, now=later)
        self.assertTrue(all(r["status"] == "open" for r in ledger["records"]))
        research.settle(ledger, lambda _: {"status": "finalized", "result": "no"}, now=later)
        for row in ledger["records"]:
            self.assertAlmostEqual(row["profit_dollars"] - row["stress_profit_dollars"], .01 * row["contracts"])

    def test_review_has_fixed_cutoff_and_cannot_automatically_promote(self):
        ledger = research.empty_ledger(now=NOW)
        research.fixed_reviews(ledger, NOW + timedelta(days=29))
        self.assertEqual(ledger["reviews"], [])
        research.fixed_reviews(ledger, NOW + timedelta(days=30))
        self.assertEqual(len(ledger["reviews"]), 1)
        review = json.dumps(ledger["reviews"][0], sort_keys=True)
        research.fixed_reviews(ledger, NOW + timedelta(days=35))
        self.assertEqual(json.dumps(ledger["reviews"][0], sort_keys=True), review)
        self.assertTrue(all(r["status"] == "continue_shadow" for r in ledger["reviews"][0]["lanes"]))
        self.assertFalse(ledger["reviews"][0]["automatic_promotion"])

    def test_same_day_repetition_cannot_manufacture_cluster_confidence(self):
        rows = [{"contracts": 3, "stress_profit_dollars": 1, "day": "same"} for _ in range(1000)]
        self.assertIsNone(research.cluster_bound(rows, lambda r: r["day"], .005))

    def test_brti_fixed_reading_late_arrival_is_excluded(self):
        ledger = research.empty_ledger(now=NOW - timedelta(minutes=1))
        market = {"ticker": "BRTI", "close_time": (NOW + timedelta(seconds=25)).isoformat(), "target": 100}
        cf = {"window_size": 35, "source_ts_ms": (NOW - timedelta(seconds=3)).timestamp() * 1000}
        research.capture_brti(ledger, market, {}, cf, {}, [], quote, now=NOW)
        self.assertFalse(ledger["forecasts"])
        self.assertIn("late_or_unverifiable_source_arrival", ledger["attempts"][0]["reasons"])

    def test_brti_observation_is_prospective_and_uses_remaining_average(self):
        ledger = research.empty_ledger(now=NOW - timedelta(minutes=1))
        market = {"ticker": "BRTI", "close_time": (NOW + timedelta(seconds=25)).isoformat(), "target": 100}
        cf = {"window_size": 35, "source_ts_ms": NOW.timestamp() * 1000, "value": 101, "window_average": 101}
        review = {"source_time_aligned": True, "sequence_valid": True, "window_integrity": True}
        research.capture_brti(ledger, market, review, cf, {}, [], quote, now=NOW)
        observation = ledger["forecasts"][0]
        self.assertAlmostEqual(observation["required_remaining_average"], (6000 - 3535) / 25)
        self.assertEqual(observation["captured_at"], NOW.isoformat())
        research.capture_brti(ledger, market, review, cf, {}, [], quote, now=NOW)
        self.assertEqual(len(ledger["forecasts"]), 1)


if __name__ == "__main__":
    unittest.main()
