import unittest
from datetime import datetime, timedelta, timezone

from crypto_low_edge_shadow import (
    capture_candidates,
    empty_ledger,
    settle_records,
    summarize,
)


class LowEdgeShadowTests(unittest.TestCase):
    def candidate(self, ticker, edge, confidence=65, **overrides):
        row = {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": "KXBTC15M",
            "asset": "BTC",
            "title": "BTC price up in next 15 mins?",
            "market_kind": "above",
            "side": "yes",
            "entry_price": 50,
            "raw_edge": edge + 0.5,
            "estimated_fee_edge_pp": 0.5,
            "edge": edge,
            "confidence": confidence,
            "model_prob_yes": 50 + edge + 0.5,
            "close_time": "2026-07-30T18:15:00+00:00",
            "minutes_to_close": 8,
            "is_15m_market": True,
            "initial_price_source": "kalshi_executable_orderbook",
            "low_edge_shadow_quality_ok": True,
            "low_edge_shadow_quality_controls": {"ok": True},
        }
        row.update(overrides)
        return row

    def test_captures_each_target_cohort_once_without_live_state(self):
        ledger = empty_ledger()
        candidates = [
            self.candidate("ONE", 1.4),
            self.candidate("TWO", 2.6, confidence=69),
            self.candidate("THREE", 3.6, confidence=71),
            self.candidate("TOO-LOW", 0.9),
            self.candidate("LIVE-EDGE", 4.0),
            self.candidate("LOW-CONF", 1.7, confidence=64.9),
        ]
        added = capture_candidates(
            ledger,
            candidates,
            now="2026-07-30T18:07:00+00:00",
        )
        self.assertEqual(
            [row["cohort"] for row in added],
            ["1-2%", "2-3%", "3-4%"],
        )
        self.assertTrue(all(row["mode"] == "paper_shadow_only" for row in added))
        self.assertTrue(all(row["virtual_stake"] == 1 for row in added))
        self.assertNotIn("bot_number", added[0])

        duplicate = capture_candidates(
            ledger,
            [self.candidate("ONE", 1.8, side="no")],
            now="2026-07-30T18:08:00+00:00",
        )
        self.assertEqual(duplicate, [])
        self.assertEqual(len(ledger["records"]), 3)

    def test_settlement_waits_for_finalized_kalshi_outcome(self):
        ledger = empty_ledger()
        capture_candidates(
            ledger,
            [self.candidate("ONE", 1.4)],
            now="2026-07-30T18:07:00+00:00",
        )
        now = datetime(2026, 7, 30, 18, 20, tzinfo=timezone.utc)
        settled = settle_records(
            ledger,
            lambda _ticker: {"status": "closed", "result": ""},
            fee_per_contract=lambda _price: 0.02,
            now=now,
            retry_minutes=0,
        )
        self.assertEqual(settled, [])
        self.assertEqual(ledger["records"][0]["status"], "open")
        self.assertIsNone(ledger["records"][0]["result"])

        settled = settle_records(
            ledger,
            lambda _ticker: {
                "status": "finalized",
                "result": "yes",
                "settlement_ts": (now + timedelta(seconds=1)).isoformat(),
            },
            fee_per_contract=lambda _price: 0.02,
            now=now + timedelta(minutes=1),
            retry_minutes=0,
        )
        self.assertEqual(len(settled), 1)
        row = ledger["records"][0]
        self.assertEqual(row["result"], "WIN")
        self.assertEqual(row["settlement_source"], "kalshi_finalized_market")
        self.assertAlmostEqual(row["virtual_profit"], 0.96)

    def test_summary_keeps_cohorts_separate(self):
        ledger = empty_ledger()
        capture_candidates(
            ledger,
            [
                self.candidate("ONE", 1.2),
                self.candidate("TWO", 2.2, side="no"),
            ],
            now="2026-07-30T18:07:00+00:00",
        )
        settle_records(
            ledger,
            lambda ticker: {
                "status": "finalized",
                "result": "yes" if ticker == "ONE" else "yes",
            },
            fee_per_contract=lambda _price: 0,
            now="2026-07-30T18:20:00+00:00",
            retry_minutes=0,
        )
        result = summarize(ledger)
        first, second, third = result["cohorts"]
        self.assertEqual((first["wins"], first["losses"]), (1, 0))
        self.assertEqual((second["wins"], second["losses"]), (0, 1))
        self.assertEqual(third["settled"], 0)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)
        self.assertFalse(result["affects_execution"])


if __name__ == "__main__":
    unittest.main()
