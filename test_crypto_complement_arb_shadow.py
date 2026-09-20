import unittest
from datetime import datetime, timedelta, timezone

import crypto_complement_arb_shadow as shadow


NOW = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)


def candidate(**overrides):
    row = {
        "market_lane": "crypto_15m",
        "is_15m_market": True,
        "ticker": "KXBTC15M-ARB",
        "event_ticker": "KXBTC15M",
        "series_ticker": "KXBTC15M",
        "asset": "BTC",
        "close_time": (NOW + timedelta(minutes=10)).isoformat(),
        "minutes_to_close": 10,
        "fee_schedule": {
            "authoritative": True,
            "fee_type": "quadratic",
            "fee_multiplier": 1.0,
            "series_ticker": "KXBTC15M",
        },
        "kalshi_microstructure": {
            "sequence_valid": True,
            "fresh": True,
            "book_consistent": True,
            "age_seconds": 0.2,
            "best_yes_bid_cents": 40,
            "best_no_bid_cents": 50,
            "best_yes_entry_price_cents": 51,
            "best_no_entry_price_cents": 61,
            "last_trade_ts": 100,
            "last_trade_price_cents": 45,
        },
    }
    row.update(overrides)
    return row


class ComplementArbShadowTests(unittest.TestCase):
    def test_capture_records_dual_maker_and_non_profitable_taker_parity(self):
        ledger = shadow.empty_ledger(now=NOW)
        added, fills = shadow.capture_candidates(ledger, [candidate()], now=NOW)
        self.assertEqual(len(added), 1)
        self.assertEqual(fills, [])
        self.assertAlmostEqual(added[0]["both_fill_locked_profit_dollars"], 0.10)
        self.assertFalse(added[0]["queue_position_modeled"])
        parity = ledger["parity_records"][0]
        self.assertGreater(parity["two_leg_cost_cents"], 100)
        self.assertFalse(parity["executable_locked_profit"])

    def test_strict_trade_through_can_fill_both_legs_on_distinct_ticks(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(ledger, [candidate()], now=NOW)
        yes_tick = candidate()
        yes_tick["kalshi_microstructure"] = {
            **yes_tick["kalshi_microstructure"],
            "last_trade_ts": 101,
            "last_trade_price_cents": 39,
        }
        _added, fills = shadow.capture_candidates(
            ledger, [yes_tick], now=NOW + timedelta(seconds=1)
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(ledger["records"][0]["yes"]["status"], "filled")
        self.assertEqual(ledger["records"][0]["no"]["status"], "working")

        no_tick = candidate()
        no_tick["kalshi_microstructure"] = {
            **no_tick["kalshi_microstructure"],
            "last_trade_ts": 102,
            "last_trade_price_cents": 51,
        }
        _added, fills = shadow.capture_candidates(
            ledger, [no_tick], now=NOW + timedelta(seconds=2)
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(ledger["records"][0]["fill_count"], 2)

    def test_finalized_settlement_includes_all_fill_outcomes(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(ledger, [candidate()], now=NOW)
        row = ledger["records"][0]
        row["yes"]["status"] = "filled"
        row["no"]["status"] = "filled"
        later = NOW + timedelta(minutes=20)
        self.assertEqual(
            shadow.settle_records(
                ledger,
                lambda _ticker: {"status": "closed", "result": "yes"},
                now=later,
            ),
            [],
        )
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
        self.assertEqual(settled[0]["fill_outcome"], "both_legs")
        self.assertAlmostEqual(settled[0]["virtual_profit"], 0.10)

    def test_inconsistent_or_inexact_books_fail_closed(self):
        ledger = shadow.empty_ledger(now=NOW)
        inconsistent = candidate()
        inconsistent["kalshi_microstructure"]["book_consistent"] = False
        inexact = candidate(ticker="KXBTC15M-INEXACT", fee_schedule={})
        added, _fills = shadow.capture_candidates(
            ledger, [inconsistent, inexact], now=NOW
        )
        self.assertEqual(added, [])


if __name__ == "__main__":
    unittest.main()
