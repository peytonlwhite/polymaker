import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crypto_market_stream import KalshiCryptoStream
from crypto_settlement_lag_shadow import (
    SettlementLagShadowWorker,
    empty_ledger,
    executable_entry,
    expected_window_reading,
    normalize_ledger,
    process_tick,
    required_remaining_average,
    settle_records,
    settlement_probability,
    summarize,
)


class SettlementLagShadowTests(unittest.TestCase):
    def settings(self):
        return {
            "CRYPTO_SETTLEMENT_LAG_SHADOW_ENABLED": "true",
            "CRYPTO_SETTLEMENT_LAG_MIN_READING": "25",
            "CRYPTO_SETTLEMENT_LAG_MAX_READING": "52",
            "CRYPTO_SETTLEMENT_LAG_EDGE_THRESHOLDS_CENTS": "4,6,8",
            "CRYPTO_SETTLEMENT_LAG_VIRTUAL_CONTRACTS": "10",
            "CRYPTO_SETTLEMENT_LAG_MIN_DEPTH_CONTRACTS": "10",
            "CRYPTO_SETTLEMENT_LAG_MAX_SPREAD_CENTS": "4",
            "CRYPTO_SETTLEMENT_LAG_MAX_QUOTE_AGE_SECONDS": "2",
            "CRYPTO_SETTLEMENT_LAG_SETTLEMENT_GRACE_MINUTES": "0",
        }

    def market(self, close=None):
        return {
            "ticker": "KXBTC15M-TEST",
            "close_time": (close or datetime.now(timezone.utc)).isoformat(),
            "target": 64000.0,
            "market_kind": "above",
            "fee_schedule": {
                "fee_type": "quadratic",
                "fee_multiplier": 1.0,
                "authoritative": True,
                "source": "test",
                "series_ticker": "KXBTC15M",
            },
        }

    def cf(self, **updates):
        row = {
            "index_id": "BRTI",
            "value": 64100.0,
            "source_ts_ms": 1_800_000_042_000,
            "upstream_received_at_ms": 1_800_000_042_001,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "window_close_ts_ms": 1_800_000_060_000,
            "window_average": 64100.0,
            "window_size": 42,
            "sequence_valid": True,
            "window_integrity": True,
            "fresh": True,
            "age_seconds": 0.05,
        }
        row.update(updates)
        return row

    def book(self, **updates):
        row = {
            "yes_levels": [(78.0, 30.0), (77.0, 30.0)],
            "no_levels": [(20.0, 30.0), (19.0, 30.0)],
            "fresh": True,
            "sequence_valid": True,
            "age_seconds": 0.05,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        row.update(updates)
        return row

    def history(self):
        return [
            {"value": 64099.5 + (index % 3) * 0.5, "received_unix": 1_800_000_000 + index}
            for index in range(60)
        ]

    def test_required_remaining_average_matches_settlement_math(self):
        self.assertAlmostEqual(
            required_remaining_average(64000, 64080, 42),
            63813.333333333336,
        )

    def test_probability_reports_separate_interval_and_heavy_tail_status(self):
        result = settlement_probability(
            target=64000,
            observed_average=64080,
            observed_count=42,
            current_value=64075,
            tick_sigma=5,
            market_kind="above",
            calibration_samples={},
        )
        self.assertEqual(result["method"], "heavy_tail_collecting")
        self.assertGreater(result["p_yes"], 90)
        self.assertLess(result["p_low"], result["p_yes"])
        self.assertGreater(result["p_high"], result["p_yes"])
        self.assertAlmostEqual(result["required_remaining_average"], 63813.33333333)

    def test_executable_entry_walks_real_contra_depth(self):
        execution = executable_entry(
            {
                "yes_levels": [(77.0, 20.0)],
                "no_levels": [(20.0, 5.0), (19.0, 5.0)],
            },
            "yes",
            10,
        )
        self.assertTrue(execution["full_fill"])
        self.assertEqual(execution["best_ask_cents"], 80.0)
        self.assertEqual(execution["vwap_cents"], 80.5)
        self.assertEqual(execution["book_walk_slippage_cents"], 0.5)
        self.assertEqual(execution["spread_cents"], 3.0)

    def test_tick_captures_three_isolated_policy_arms(self):
        ledger = empty_ledger(self.settings())
        review, added, _exits = process_tick(
            ledger,
            self.market(),
            self.cf(),
            self.history(),
            self.book(),
            self.settings(),
        )
        self.assertFalse(review["reasons"])
        self.assertEqual([row["edge_threshold_cents"] for row in added], [4.0, 6.0, 8.0])
        self.assertTrue(all(row["affects_execution"] is False for row in added))
        self.assertTrue(all(row["automatic_promotion"] is False for row in added))
        self.assertTrue(all(row["fee_schedule"]["exact"] for row in added))

    def test_integrity_failure_blocks_all_policy_arms(self):
        ledger = empty_ledger(self.settings())
        review, added, _exits = process_tick(
            ledger,
            self.market(),
            self.cf(window_integrity=False),
            self.history(),
            self.book(),
            self.settings(),
        )
        self.assertIn("brti_window_incomplete", review["reasons"])
        self.assertEqual(added, [])

    def test_source_time_mismatch_blocks_all_policy_arms(self):
        ledger = empty_ledger(self.settings())
        review, added, _exits = process_tick(
            ledger,
            self.market(),
            self.cf(
                window_integrity=False,
                window_time_aligned=False,
                expected_window_size=49,
                window_size=1,
                source_ts_ms=1_800_000_049_000,
            ),
            self.history(),
            self.book(),
            self.settings(),
        )
        self.assertIn("brti_window_time_mismatch", review["reasons"])
        self.assertEqual(review["expected_reading_from_source_time"], 49)
        self.assertEqual(added, [])

    def test_existing_misaligned_records_are_preserved_but_excluded(self):
        close = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
        source = int((close - timedelta(seconds=11)).timestamp() * 1000)
        payload = empty_ledger(self.settings())
        payload["records"] = [{
            "id": "legacy",
            "status": "settled",
            "ticker": "KXBTC15M-LEGACY",
            "close_time": close.isoformat(),
            "source_ts_ms": source,
            "reading": 1,
            "won": True,
            "total_cost_dollars": 1.0,
            "hold_counterfactual": {"virtual_profit": 3.69},
            "exit_counterfactual": {"virtual_profit": -1.04},
        }]
        ledger = normalize_ledger(payload, self.settings())
        self.assertEqual(expected_window_reading(source, close), 49)
        self.assertFalse(ledger["records"][0]["valid_for_evaluation"])
        report = summarize(ledger, self.settings())
        self.assertEqual(report["settled"], 0)
        self.assertEqual(report["diagnostic_invalid_records"], 1)
        self.assertEqual(report["diagnostic_invalid_hold_profit"], 3.69)

    def test_official_result_alone_settles_shadow_record(self):
        close = datetime.now(timezone.utc) - timedelta(minutes=3)
        ledger = empty_ledger(self.settings())
        process_tick(
            ledger,
            self.market(close),
            self.cf(),
            self.history(),
            self.book(),
            self.settings(),
        )
        self.assertTrue(ledger["records"])
        settled = settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        self.assertEqual(len(settled), 3)
        self.assertTrue(all(row["won"] for row in settled))
        self.assertTrue(all(row["hold_counterfactual"]["status"] == "settled" for row in settled))

    def test_closed_market_is_not_treated_as_final_settlement(self):
        close = datetime.now(timezone.utc) - timedelta(minutes=3)
        ledger = empty_ledger(self.settings())
        process_tick(
            ledger,
            self.market(close),
            self.cf(),
            self.history(),
            self.book(),
            self.settings(),
        )
        settled = settle_records(
            ledger,
            lambda _ticker: {"status": "closed", "result": "yes"},
            self.settings(),
        )
        self.assertEqual(settled, [])
        self.assertTrue(all(row["status"] == "open" for row in ledger["records"]))

    def test_next_fresh_tick_records_integer_fok_reconfirmation(self):
        close = datetime.now(timezone.utc) - timedelta(minutes=3)
        close_ms = 1_800_000_060_000
        ledger = empty_ledger(self.settings())
        process_tick(
            ledger,
            self.market(close),
            self.cf(
                window_size=42,
                source_ts_ms=close_ms - 18_000,
                window_close_ts_ms=close_ms,
            ),
            self.history(),
            self.book(),
            self.settings(),
        )
        process_tick(
            ledger,
            self.market(close),
            self.cf(
                window_size=43,
                source_ts_ms=close_ms - 17_000,
                window_close_ts_ms=close_ms,
            ),
            self.history(),
            self.book(yes_levels=[(77.0, 30.0)], no_levels=[(19.0, 30.0)]),
            self.settings(),
        )
        primary = next(row for row in ledger["records"] if row["market_primary"])
        fok = primary["reconfirmed_fok_counterfactual"]
        self.assertEqual(fok["status"], "filled")
        self.assertEqual(fok["contracts"], 10)
        self.assertTrue(fok["fee_schedule"]["exact"])
        self.assertGreaterEqual(fok["entry_slippage_vs_initial_cents"], 0)
        settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        report = summarize(ledger, self.settings())
        self.assertEqual(report["reconfirmed_fok"]["settled"], 1)
        self.assertGreater(report["reconfirmed_fok"]["profit"], 0)
        self.assertFalse(report["evaluation"]["ready_for_manual_review"])

    def test_summary_counts_independent_market_once_across_arms(self):
        close = datetime.now(timezone.utc) - timedelta(minutes=3)
        ledger = empty_ledger(self.settings())
        process_tick(
            ledger,
            self.market(close),
            self.cf(),
            self.history(),
            self.book(),
            self.settings(),
        )
        settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            self.settings(),
        )
        report = summarize(ledger, self.settings())
        self.assertEqual(report["independent_markets"], 1)
        self.assertEqual(report["settled"], 1)
        self.assertEqual(report["policy_arm_settled"], 3)
        self.assertEqual(report["policy_arm_wins"], 3)
        self.assertFalse(report["evaluation"]["automatic_promotion"])
        self.assertIn("minimum_reconfirmed_fok_markets", report["evaluation"]["gates"])

    def test_observation_dates_survive_bounded_window_history(self):
        ledger = empty_ledger(self.settings())
        ledger["observation_dates"] = [
            f"2026-07-{day:02d}" for day in range(1, 31)
        ]
        ledger["windows"] = {
            str(index): {
                "close_time": f"2026-08-{1 + index // 96:02d}T00:00:00+00:00",
                "ticks": {"1": {}},
            }
            for index in range(120)
        }
        normalized = normalize_ledger(ledger, self.settings())
        report = summarize(normalized, self.settings())
        self.assertGreaterEqual(report["observation_days"], 30)

    def test_fixed_reading_probability_is_recorded_without_trade_qualification(self):
        close = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=25)
        close_ms = int(close.timestamp() * 1000)
        ledger = empty_ledger(self.settings())
        review, added, _exits = process_tick(
            ledger,
            self.market(close),
            self.cf(
                source_ts_ms=close_ms - 25_000,
                window_close_ts_ms=close_ms,
                window_size=35,
                window_average=64100.0,
            ),
            self.history(),
            {"fresh": False, "sequence_valid": True, "age_seconds": 20},
            self.settings(),
        )
        self.assertTrue(review["observation_captured"])
        self.assertEqual(added, [])
        self.assertEqual(len(ledger["observations"]), 1)
        self.assertEqual(ledger["observations"][0]["reading"], 35)
        self.assertIsNotNone(ledger["observations"][0]["p_yes"])

    def test_worker_exposes_no_order_placement_method(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = SettlementLagShadowWorker(
                Path(temp) / "ledger.json",
                fetch_market=lambda _ticker: {},
            )
            self.assertFalse(hasattr(worker, "place_order"))
            self.assertFalse(hasattr(worker, "cancel_order"))

    def test_worker_drains_every_queued_brti_update(self):
        close = datetime.now(timezone.utc) + timedelta(minutes=1)
        close_ms = int(close.timestamp() * 1000)
        market = self.market(close)
        now_unix = time.time()
        book = {
            **self.book(),
            "received_unix": now_unix - 0.05,
        }

        class Stream:
            def cfbenchmark_history(self, _index_id, seconds=300):
                del seconds
                return [
                    {
                        **self_outer.cf(window_size=reading),
                        "source_ts_ms": close_ms - (60 - reading) * 1000,
                        "window_close_ts_ms": close_ms,
                        "received_unix": now_unix + reading * 0.001,
                        "market_snapshots": {market["ticker"]: book},
                    }
                    for reading in (1, 2)
                ]

        self_outer = self
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.json"
            worker = SettlementLagShadowWorker(
                path,
                fetch_market=lambda _ticker: {},
            )
            worker.configure(stream=Stream(), markets=[market], settings=self.settings())
            worker.start()
            deadline = time.time() + 2.0
            while time.time() < deadline and not path.exists():
                time.sleep(0.02)
            worker.stop()
            if worker._thread:
                worker._thread.join(timeout=2)
            payload = json.loads(path.read_text(encoding="utf-8"))
            window = next(iter(payload["windows"].values()))
            self.assertEqual(sorted(window["ticks"]), ["1", "2"])


class CfBenchmarkStreamTests(unittest.TestCase):
    def message(self, seq, source_ts_ms, window_size, average="64000.0"):
        return json.dumps({
            "type": "cfbenchmarks_value",
            "sid": 9,
            "seq": seq,
            "msg": {
                "index_id": "BRTI",
                "received_at": source_ts_ms + 1,
                "data": json.dumps({
                    "type": "value",
                    "id": "BRTI",
                    "time": source_ts_ms,
                    "value": "64000.0",
                }),
                "last_60s_windowed_average_15min": {
                    "value": average,
                    "window_size": window_size,
                    "window_start_ts_ms": 1_800_000_000_000,
                    "window_end_ts_exclusive": source_ts_ms,
                },
            },
        })

    def test_exact_final_minute_field_and_integrity_are_retained(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        close = 1_800_000_900_000
        stream._handle_message(self.message(1, close - 59_000, 1))
        stream._handle_message(self.message(2, close - 58_000, 2, "64001.0"))
        snapshot = stream.cfbenchmark_snapshot("BRTI", max_age_seconds=5)
        self.assertEqual(snapshot["window_size"], 2)
        self.assertEqual(snapshot["window_average"], 64001.0)
        self.assertTrue(snapshot["sequence_valid"])
        self.assertTrue(snapshot["window_integrity"])

    def test_missing_window_observation_invalidates_window(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        close = 1_800_000_900_000
        stream._handle_message(self.message(1, close - 59_000, 1))
        stream._handle_message(self.message(2, close - 57_000, 3))
        snapshot = stream.cfbenchmark_snapshot("BRTI", max_age_seconds=5)
        self.assertFalse(snapshot["window_integrity"])
        self.assertFalse(snapshot["fresh"])

    def test_midwindow_resubscription_is_rejected_by_source_time(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        close = 1_800_000_900_000
        stream._handle_message(self.message(1, close - 11_000, 1))
        snapshot = stream.cfbenchmark_snapshot("BRTI", max_age_seconds=5)
        self.assertEqual(snapshot["expected_window_size"], 49)
        self.assertFalse(snapshot["window_time_aligned"])
        self.assertFalse(snapshot["window_integrity"])
        self.assertFalse(snapshot["fresh"])

    def test_websocket_sequence_gap_invalidates_feed_and_requests_reconnect(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        close = 1_800_000_900_000
        stream._handle_message(self.message(1, close - 59_000, 1))
        stream._handle_message(self.message(3, close - 58_000, 2))
        snapshot = stream.cfbenchmark_snapshot("BRTI", max_age_seconds=5)
        self.assertFalse(snapshot["sequence_valid"])
        self.assertTrue(stream._reconnect_requested.is_set())
        self.assertEqual(stream.status()["cfbenchmark_sequence_gap_count"], 1)

    def test_final_minute_history_captures_same_moment_market_book(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        stream.replace_cfbenchmark_snapshot_tickers(["T"])
        stream._snapshots["T"] = {
            "yes_levels": [(40.0, 5.0)],
            "no_levels": [(58.0, 5.0)],
            "received_unix": time.time() - 0.05,
            "sequence_valid": True,
        }
        close = 1_800_000_900_000
        stream._handle_message(self.message(1, close - 59_000, 1))
        history = stream.cfbenchmark_history("BRTI", seconds=5)
        self.assertEqual(
            history[-1]["market_snapshots"]["T"]["yes_levels"],
            [(40.0, 5.0)],
        )

    def test_yes_price_orderbook_converts_no_side_once(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        stream._handle_message(json.dumps({
            "type": "orderbook_snapshot",
            "sid": 2,
            "seq": 1,
            "msg": {
                "market_ticker": "T",
                "yes_dollars": [["0.54", "5"]],
                "no_dollars": [["0.65", "6"]],
            },
        }))
        snapshot = stream.snapshot("T", max_age_seconds=5)
        self.assertEqual(snapshot["best_yes_bid_cents"], 54.0)
        self.assertEqual(snapshot["best_yes_entry_price_cents"], 65.0)
        self.assertEqual(snapshot["no_levels"], [(35.0, 6.0)])
        self.assertTrue(snapshot["book_consistent"])

    def test_crossed_unified_book_fails_closed_and_resyncs(self):
        stream = KalshiCryptoStream(api_key_id="test", private_key_pem="unused")
        stream._handle_message(json.dumps({
            "type": "orderbook_snapshot",
            "sid": 2,
            "seq": 1,
            "msg": {
                "market_ticker": "T",
                "yes_dollars": [["0.54", "5"]],
                "no_dollars": [["0.45", "6"]],
            },
        }))
        snapshot = stream.snapshot("T", max_age_seconds=5)
        self.assertFalse(snapshot["sequence_valid"])
        self.assertFalse(snapshot["book_consistent"])
        self.assertFalse(snapshot["fresh"])
        self.assertTrue(stream._reconnect_requested.is_set())
        self.assertEqual(stream.status()["book_inconsistency_count"], 1)


if __name__ == "__main__":
    unittest.main()
