import unittest
from datetime import datetime, timedelta, timezone

import crypto_execution_lab_shadow as shadow


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def candidate(**overrides):
    venue = {
        "connected": True,
        "stream_age_seconds": 0.2,
        "trade_count_60s": 100,
        "trade_flow_60s": 0.45,
        "trade_flow_300s": 0.25,
        "mid_return_60s_bps": 12.0,
        "mid_return_30s_bps": 6.0,
        "mid_return_15s_bps": 3.0,
    }
    depths = {
        side: {
            str(contracts): {
                "available_contracts": 20,
                "full_size_entry_price_cents": 60 if side == "yes" else 41,
            }
            for contracts in (1, 3, 5)
        }
        for side in ("yes", "no")
    }
    row = {
        "market_lane": "crypto_15m",
        "is_15m_market": True,
        "market_kind": "above",
        "ticker": "KXBTC15M-LAB",
        "event_ticker": "KXBTC15M",
        "series_ticker": "KXBTC15M",
        "asset": "BTC",
        "close_time": (NOW + timedelta(minutes=12)).isoformat(),
        "minutes_to_close": 12,
        "floor_strike": 100000,
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
            "source": "kalshi_websocket",
            "sequence_valid": True,
            "fresh": True,
            "book_consistent": True,
            "age_seconds": 0.1,
            "spread_yes_cents": 1,
            "best_yes_entry_price_cents": 60,
            "best_no_entry_price_cents": 41,
            "yes_mid_change_60s_pp": 0.2,
            "yes_mid_change_30s_pp": 0.0,
            "trade_flow_60s": 0.4,
            "trade_count_60s": 10,
        },
        "production_shadow_depth": depths,
        "probability": {
            "prob": 70,
            "p_low": 66,
            "p_high": 74,
            "target_distance_sigma": 1.5,
        },
    }
    row.update(overrides)
    return row


def confirmation(price=60, available=20, now=NOW):
    def fetch(_ticker, side, contracts):
        selected = price if side == "yes" else 100 - price + 1
        return {
            "available_contracts": available,
            "full_size_entry_price_cents": selected,
            "best_entry_price_cents": selected,
            "fetched_at": now.isoformat(),
        }

    return fetch


class ExecutionLabShadowTests(unittest.TestCase):
    def test_fresh_full_depth_scan_captures_production_parity_hypotheses(self):
        ledger = shadow.empty_ledger(now=NOW)
        late_settlement = candidate(
            ticker="KXBTC15M-LAB-LATE",
            minutes_to_close=5,
            close_time=(NOW + timedelta(minutes=5)).isoformat(),
        )
        added, attempts = shadow.capture_candidates(
            ledger,
            [candidate(), late_settlement],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        lanes = {row["lane"] for row in added}
        self.assertTrue(
            {
                shadow.FLOW_CORE,
                shadow.FLOW_LATE,
                shadow.FLOW_BTC_ETH,
                shadow.FLOW_PERSISTENCE,
                shadow.SPOT_NO_CHASE,
                shadow.SPOT_FLOW,
                shadow.SETTLEMENT_DISTANCE,
            }.issubset(lanes)
        )
        self.assertEqual(len(attempts), len(added))
        self.assertTrue(all(row["would_submit"] for row in attempts))
        self.assertTrue(all(not row["affects_execution"] for row in added))
        self.assertTrue(all(not row["automatic_promotion"] for row in added))
        self.assertTrue(
            all((row["arm"]["fee_schedule"] or {}).get("exact") for row in added)
        )

    def test_second_quote_depth_and_adverse_move_fail_closed(self):
        ledger = shadow.empty_ledger(now=NOW)
        added, attempts = shadow.capture_candidates(
            ledger,
            [candidate()],
            lambda row: row,
            confirmation(price=63, available=2),
            now=NOW,
        )
        self.assertEqual(added, [])
        self.assertGreater(len(attempts), 0)
        self.assertTrue(all(not row["would_submit"] for row in attempts))
        reasons = {reason for row in attempts for reason in row["reasons"]}
        self.assertIn("insufficient_confirmed_fok_depth", reasons)
        self.assertIn("confirmation_adverse_move", reasons)

    def test_pullback_waits_then_enters_only_if_signal_persists(self):
        ledger = shadow.empty_ledger(
            settings={
                "CRYPTO_EXECUTION_LAB_PULLBACK_WAIT_SECONDS": "30",
                "CRYPTO_EXECUTION_LAB_PULLBACK_CENTS": "1",
            },
            now=NOW,
        )
        first = candidate()
        shadow.capture_candidates(
            ledger,
            [first],
            lambda row: row,
            confirmation(),
            settings={
                "CRYPTO_EXECUTION_LAB_PULLBACK_WAIT_SECONDS": "30",
                "CRYPTO_EXECUTION_LAB_PULLBACK_CENTS": "1",
            },
            now=NOW,
        )
        self.assertEqual(len(ledger["pending_pullbacks"]), 1)
        later = candidate()
        later["kalshi_microstructure"]["best_yes_entry_price_cents"] = 59
        later["production_shadow_depth"]["yes"]["3"][
            "full_size_entry_price_cents"
        ] = 59
        added, _attempts = shadow.capture_candidates(
            ledger,
            [later],
            lambda row: row,
            confirmation(price=59, now=NOW + timedelta(seconds=45)),
            settings={
                "CRYPTO_EXECUTION_LAB_PULLBACK_WAIT_SECONDS": "30",
                "CRYPTO_EXECUTION_LAB_PULLBACK_CENTS": "1",
            },
            now=NOW + timedelta(seconds=45),
        )
        pullbacks = [row for row in added if row["lane"] == shadow.SPOT_PULLBACK]
        self.assertEqual(len(pullbacks), 1)
        self.assertEqual(pullbacks[0]["arm"]["entry_price_cents"], 59)
        self.assertEqual(ledger["pending_pullbacks"], [])

    def test_policy_change_discards_old_pending_pullback(self):
        settings = {"CRYPTO_EXECUTION_LAB_PULLBACK_WAIT_SECONDS": "30"}
        ledger = shadow.empty_ledger(settings=settings, now=NOW)
        shadow.capture_candidates(
            ledger,
            [candidate()],
            lambda row: row,
            confirmation(),
            settings=settings,
            now=NOW,
        )
        self.assertEqual(len(ledger["pending_pullbacks"]), 1)
        self.assertEqual(
            ledger["pending_pullbacks"][0]["policy_hash"],
            ledger["configuration"]["policy_hash"],
        )

        changed = {"CRYPTO_EXECUTION_LAB_PULLBACK_WAIT_SECONDS": "45"}
        shadow.normalize_ledger(ledger, settings=changed, now=NOW)
        self.assertEqual(ledger["pending_pullbacks"], [])

    def test_only_official_finalized_market_settles(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(
            ledger,
            [candidate()],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        later = NOW + timedelta(minutes=20)
        self.assertEqual(
            shadow.settle_records(
                ledger,
                lambda _ticker: {"status": "closed", "result": "yes"},
                now=later,
            ),
            [],
        )
        settled = shadow.settle_records(
            ledger,
            lambda _ticker: {
                "status": "finalized",
                "result": "yes",
                "settlement_ts": later.isoformat(),
            },
            now=later,
        )
        self.assertGreater(len(settled), 0)
        self.assertTrue(all((row["arm"] or {}).get("result") == "WIN" for row in settled))
        summary = shadow.summarize(ledger)
        self.assertFalse(summary["automatic_promotion"])
        self.assertFalse(summary["historical_backfill"])
        self.assertEqual(summary["fill_model"], "fresh_visible_taker_fok_depth")


if __name__ == "__main__":
    unittest.main()
