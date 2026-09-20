import unittest
from datetime import datetime, timedelta, timezone

import crypto_rejection_shadow as shadow


class CryptoRejectionShadowTests(unittest.TestCase):
    def candidate(self, ticker, reason):
        return {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "series_ticker": "KXBTC15M",
            "market_lane": "crypto_15m",
            "asset": "BTC",
            "side": "yes",
            "entry_price": 50,
            "edge": 8,
            "confidence": 75,
            "minutes_to_close": 5,
            "close_time": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "skip_reasons": [reason],
        }

    def test_captures_each_rejection_cohort_once_and_settles(self):
        ledger = shadow.empty_ledger()
        candidates = [
            self.candidate("KXBTC15M-DIRECTION", "campaign_directional_confirmation"),
            self.candidate("KXBTC15M-MID", "campaign_mid_price_quality_filter"),
        ]
        added = shadow.capture_candidates(ledger, candidates, virtual_stake=1)
        self.assertEqual(len(added), 2)
        self.assertEqual(shadow.capture_candidates(ledger, candidates), [])
        settled = shadow.settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "yes"},
            fee_per_contract=lambda _price: 0,
            grace_minutes=0,
        )
        self.assertEqual(len(settled), 2)
        summary = shadow.summarize(ledger)
        self.assertEqual(summary["settled"], 2)
        cohorts = {row["cohort"] for row in summary["cohorts"]}
        self.assertTrue({
            "directional_confirmation",
            "directional_opposition",
            "directional_flow_too_weak",
            "directional_insufficient_consensus",
            "directional_data_unavailable",
            "one_source_shadow",
            "mid_price_6_9_edge",
            "commodity_live_shadow",
        }.issubset(cohorts))

    def test_commodity_pause_captures_only_otherwise_qualified_candidates(self):
        ledger = shadow.empty_ledger()
        qualified = self.candidate("KXGOLD15M-SHADOW", "commodity_live_shadow")
        qualified.update({
            "series_ticker": "KXGOLD15M",
            "market_lane": "commodity_15m",
            "asset": "GOLD",
        })
        weak = dict(qualified)
        weak["ticker"] = "KXGOLD15M-WEAK"
        weak["skip_reasons"] = [
            "commodity_live_shadow",
            "campaign_directional_confirmation",
        ]

        added = shadow.capture_candidates(ledger, [qualified, weak], virtual_stake=1)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["cohort"], "commodity_live_shadow")
        self.assertEqual(added[0]["market_lane"], "commodity_15m")


if __name__ == "__main__":
    unittest.main()
