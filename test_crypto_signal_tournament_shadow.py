import unittest
from datetime import datetime, timedelta, timezone

import crypto_signal_tournament_shadow as shadow


NOW = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)


def candidate(**overrides):
    venue = {
        "connected": True,
        "stream_age_seconds": 0.5,
        "trade_count_60s": 100,
        "trade_flow_60s": 0.35,
        "mid_return_60s_bps": 10.0,
        "mid_return_30s_bps": 4.0,
        "mid_return_15s_bps": 3.0,
        "top_imbalance": 0.30,
        "microprice_displacement_bps": 0.20,
    }
    row = {
        "market_lane": "crypto_15m",
        "is_15m_market": True,
        "ticker": "KXBTC15M-TEST",
        "event_ticker": "KXBTC15M",
        "series_ticker": "KXBTC15M",
        "asset": "BTC",
        "close_time": (NOW + timedelta(minutes=10)).isoformat(),
        "minutes_to_close": 10,
        "data_quality": {"score": 0.99},
        "fee_schedule": {
            "authoritative": True,
            "fee_type": "quadratic",
            "fee_multiplier": 1.0,
            "series_ticker": "KXBTC15M",
        },
        "microstructure": {
            "coinbase": dict(venue),
            "kraken": dict(venue),
        },
        "kalshi_microstructure": {
            "source": "websocket",
            "sequence_valid": True,
            "fresh": True,
            "book_consistent": True,
            "age_seconds": 0.2,
            "spread_yes_cents": 2,
            "best_yes_entry_price_cents": 40,
            "best_no_entry_price_cents": 61,
            "trade_flow_60s": 0.30,
            "trade_count_60s": 10,
            "yes_mid_change_60s_pp": 0.2,
            "yes_mid_change_30s_pp": 0.0,
        },
    }
    row.update(overrides)
    return row


class SignalTournamentShadowTests(unittest.TestCase):
    def test_fresh_scan_captures_orthogonal_signals_and_consensus_sizing(self):
        ledger = shadow.empty_ledger(now=NOW)
        added = shadow.capture_candidates(ledger, [candidate()], now=NOW)
        lanes = {row["lane"] for row in added}
        self.assertEqual(
            lanes,
            {
                "cross_venue_flow_follow",
                "cross_venue_impulse_follow",
                "spot_leads_kalshi",
                "cross_venue_book_pressure",
                "kalshi_trade_alignment",
                "multi_signal_consensus",
                shadow.CONTEMPORANEOUS_SPOT_FLOW_LANE,
            },
        )
        direct = next(
            row
            for row in added
            if row["lane"] == shadow.CONTEMPORANEOUS_SPOT_FLOW_LANE
        )
        self.assertEqual(direct["capture_batch_id"], NOW.isoformat())
        self.assertTrue(direct["execution_validation_record"])
        self.assertEqual(direct["primary"]["side"], "yes")
        consensus = next(
            row for row in added if row["lane"] == shadow.CONSENSUS_LANE
        )
        self.assertEqual(consensus["consensus_sized"]["primary"]["contracts"], 5)
        self.assertFalse(consensus["affects_execution"])
        self.assertFalse(consensus["automatic_promotion"])

    def test_stale_or_inexact_inputs_fail_closed(self):
        ledger = shadow.empty_ledger(now=NOW)
        stale = candidate()
        stale["kalshi_microstructure"]["age_seconds"] = 3
        inexact = candidate(ticker="KXBTC15M-INEXACT", fee_schedule={})
        self.assertEqual(
            shadow.capture_candidates(ledger, [stale, inexact], now=NOW), []
        )

    def test_only_finalized_market_settles_and_opposite_is_contemporaneous(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(ledger, [candidate()], now=NOW)
        later = NOW + timedelta(minutes=20)
        self.assertEqual(
            shadow.settle_records(
                ledger,
                lambda _ticker: {"status": "closed", "result": "yes"},
                now=later,
            ),
            [],
        )
        for row in ledger["records"]:
            row.pop("last_settlement_check_at", None)
        settled = shadow.settle_records(
            ledger,
            lambda _ticker: {
                "status": "finalized",
                "result": "yes",
                "settlement_ts": later.isoformat(),
            },
            now=later,
        )
        self.assertEqual(len(settled), 7)
        flow = next(row for row in settled if row["lane"] == "cross_venue_flow_follow")
        self.assertEqual(flow["primary"]["result"], "WIN")
        self.assertEqual(flow["comparator"]["result"], "LOSS")
        self.assertGreater(flow["profit_delta"], 0)

    def test_summary_uses_multiple_testing_adjustment_and_never_promotes(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(ledger, [candidate()], now=NOW)
        summary = shadow.summarize(ledger)
        self.assertEqual(len(summary["lanes"]), 7)
        confidence = summary["configuration"][
            "per_hypothesis_one_sided_confidence_level"
        ]
        self.assertGreater(confidence, 0.99)
        self.assertEqual(summary["tracked"], 6)
        self.assertEqual(summary["contemporaneous_spot_flow"]["tracked"], 1)
        self.assertFalse(summary["automatic_promotion"])
        self.assertFalse(summary["historical_backfill"])

    def test_execution_parity_uses_refreshed_fok_price_not_snapshot_price(self):
        ledger = shadow.empty_ledger(now=NOW)
        added = shadow.capture_candidates(ledger, [candidate()], now=NOW)

        updates = shadow.attach_execution_parity(
            ledger,
            added,
            lambda _ticker, _side, _contracts: {
                "full_size_entry_price_cents": 42.0,
                "best_entry_price_cents": 42.0,
                "available_contracts": 20,
                "fetched_at": NOW.isoformat(),
            },
            now=NOW + timedelta(seconds=2),
        )

        self.assertEqual(len(updates), len(added))
        parity = updates[0]["execution_parity"]
        self.assertFalse(parity["would_submit"])
        self.assertEqual(parity["initial_entry_price_cents"], 40.0)
        self.assertEqual(parity["refreshed_entry_price_cents"], 42.0)
        self.assertIn("execution_parity_adverse_move", parity["reasons"])

    def test_execution_parity_settles_only_book_fillable_arm(self):
        ledger = shadow.empty_ledger(now=NOW)
        added = shadow.capture_candidates(ledger, [candidate()], now=NOW)
        shadow.attach_execution_parity(
            ledger,
            added,
            lambda _ticker, _side, _contracts: {
                "full_size_entry_price_cents": 40.0,
                "best_entry_price_cents": 40.0,
                "available_contracts": 20,
                "fetched_at": NOW.isoformat(),
            },
            now=NOW + timedelta(seconds=1),
        )
        later = NOW + timedelta(minutes=20)
        settled = shadow.settle_records(
            ledger,
            lambda _ticker: {
                "status": "finalized",
                "result": "yes",
                "settlement_ts": later.isoformat(),
            },
            now=later,
        )

        self.assertTrue(settled)
        arm = settled[0]["execution_parity"]["arm"]
        self.assertEqual(arm["result"], "WIN")
        summary = shadow.summarize(ledger)
        self.assertGreater(summary["execution_parity"]["settled"], 0)
        self.assertGreater(summary["execution_parity"]["profit"], 0)

    def test_live_pipeline_result_replaces_generic_spot_parity(self):
        ledger = shadow.empty_ledger(now=NOW)
        added = shadow.capture_candidates(ledger, [candidate()], now=NOW)
        shadow.attach_execution_parity(
            ledger,
            added,
            lambda _ticker, _side, _contracts: {
                "full_size_entry_price_cents": 40.0,
                "best_entry_price_cents": 40.0,
                "available_contracts": 20,
            },
            now=NOW,
        )
        promoted = candidate()
        promoted["side"] = "yes"
        promoted["spot_flow_live_pilot"] = {
            "active": True,
            "tier": "spot_flow_confirmed",
            "signal_matched_at": NOW.isoformat(),
            "reasons": ["pilot_price_outside_band"],
            "sizing": {"contracts": 5, "expected_fee_dollars": 0.05},
        }
        updated = shadow.apply_live_pipeline_parity(
            ledger,
            promoted,
            {
                "ok": False,
                "error": "pilot_price_outside_band",
                "original_price_cents": 40.0,
                "refreshed_price_cents": 90.0,
                "adverse_move_cents": 50.0,
                "available_contracts": 100,
            },
            now=NOW + timedelta(seconds=2),
        )

        self.assertIsNotNone(updated)
        parity = updated["execution_parity"]
        self.assertEqual(parity["source"], "exact_live_spot_flow_pipeline")
        self.assertEqual(parity["status"], "rejected")
        self.assertFalse(parity["would_submit"])
        self.assertIsNone(parity["arm"])
        summary = shadow.summarize(ledger)
        self.assertEqual(summary["execution_parity"]["live_policy"]["tracked"], 1)
        self.assertEqual(summary["execution_parity"]["live_policy"]["rejected"], 1)

    def test_exact_live_attempt_is_retained_when_snapshot_record_is_not_nearby(self):
        ledger = shadow.empty_ledger(now=NOW)
        promoted = candidate(ticker="KXBTC15M-LIVE-ONLY")
        promoted["side"] = "yes"
        promoted["spot_flow_live_pilot"] = {
            "active": True,
            "tier": "spot_flow_confirmed",
            "signal_matched_at": NOW.isoformat(),
            "reasons": [],
            "sizing": {"contracts": 2, "expected_fee_dollars": 0.02},
        }

        updated = shadow.apply_live_pipeline_parity(
            ledger,
            promoted,
            {
                "ok": True,
                "original_price_cents": 40.0,
                "refreshed_price_cents": 40.0,
                "adverse_move_cents": 0.0,
                "available_contracts": 10,
            },
            live_order={"ok": True},
            now=NOW + timedelta(seconds=2),
        )

        self.assertIsNotNone(updated)
        self.assertEqual(len(ledger["live_pipeline_attempts"]), 1)
        attempt = ledger["live_pipeline_attempts"][0]
        self.assertEqual(attempt["execution_status"], "filled")
        self.assertEqual(attempt["settlement_status"], "open")
        self.assertEqual(
            attempt["execution_parity"]["arm"]["entry_price_cents"], 40.0
        )


if __name__ == "__main__":
    unittest.main()
