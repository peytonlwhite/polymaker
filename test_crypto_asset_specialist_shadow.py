import unittest
from datetime import datetime, timedelta, timezone

import crypto_asset_specialist_shadow as shadow


NOW = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)


def candidate(asset="ETH", direction=-1, minutes=12, **overrides):
    venue = {
        "connected": True,
        "stream_age_seconds": 0.2,
        "trade_count_60s": 100,
        "trade_flow_60s": 0.45 * direction,
        "trade_flow_300s": 0.25 * direction,
        "mid_return_60s_bps": 12.0 * direction,
        "mid_return_30s_bps": 6.0 * direction,
        "mid_return_15s_bps": 3.0 * direction,
    }
    selected_side = "yes" if direction > 0 else "no"
    selected_price = 60
    yes_price = selected_price if selected_side == "yes" else 41
    no_price = selected_price if selected_side == "no" else 41
    depths = {
        side: {
            str(contracts): {
                "available_contracts": 20,
                "full_size_entry_price_cents": yes_price
                if side == "yes"
                else no_price,
            }
            for contracts in (1, 3, 5)
        }
        for side in ("yes", "no")
    }
    row = {
        "market_lane": "crypto_15m",
        "is_15m_market": True,
        "market_kind": "above",
        "ticker": f"KX{asset}15M-SPECIALIST",
        "event_ticker": f"KX{asset}15M",
        "series_ticker": f"KX{asset}15M",
        "asset": asset,
        "close_time": (NOW + timedelta(minutes=minutes)).isoformat(),
        "minutes_to_close": minutes,
        "floor_strike": 100,
        "data_quality": {"score": 0.99},
        "fee_schedule": {
            "authoritative": True,
            "fee_type": "quadratic",
            "fee_multiplier": 1.0,
            "series_ticker": f"KX{asset}15M",
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
            "best_yes_entry_price_cents": yes_price,
            "best_no_entry_price_cents": no_price,
            "yes_mid_change_60s_pp": 0.2 * direction,
            "trade_flow_60s": 0.4 * direction,
            "trade_count_60s": 10,
        },
        "production_shadow_depth": depths,
    }
    row.update(overrides)
    return row


def confirmation(price=60, available=20):
    def fetch(_ticker, _side, _contracts):
        return {
            "available_contracts": available,
            "full_size_entry_price_cents": price,
            "best_entry_price_cents": price,
            "fetched_at": NOW.isoformat(),
        }

    return fetch


class AssetSpecialistShadowTests(unittest.TestCase):
    def test_eth_forward_family_and_deduplicated_portfolio_capture(self):
        ledger = shadow.empty_ledger(now=NOW)
        added, attempts = shadow.capture_candidates(
            ledger,
            [candidate("ETH", -1)],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        lanes = {row["lane"] for row in added}
        self.assertEqual(
            lanes,
            {shadow.ETH_CORE, shadow.ETH_LATE, shadow.ETH_NO, shadow.PORTFOLIO},
        )
        portfolio = next(row for row in added if row["lane"] == shadow.PORTFOLIO)
        self.assertEqual(portfolio["required_contracts"], 5)
        self.assertTrue(portfolio["portfolio_candidate"])
        self.assertEqual(len(attempts), 4)
        self.assertTrue(all(row["would_submit"] for row in attempts))
        self.assertTrue(all(not row["affects_execution"] for row in added))
        self.assertTrue(all(not row["automatic_promotion"] for row in added))

    def test_xrp_persistence_late_and_portfolio_use_one_ticker_position(self):
        ledger = shadow.empty_ledger(now=NOW)
        added, _attempts = shadow.capture_candidates(
            ledger,
            [candidate("XRP", 1)],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        lanes = {row["lane"] for row in added}
        self.assertEqual(
            lanes,
            {shadow.XRP_PERSISTENCE, shadow.XRP_LATE, shadow.PORTFOLIO},
        )
        portfolio = [row for row in added if row["lane"] == shadow.PORTFOLIO]
        self.assertEqual(len(portfolio), 1)
        self.assertEqual(portfolio[0]["required_contracts"], 5)

    def test_xrp_persistence_without_late_window_uses_three_contracts(self):
        ledger = shadow.empty_ledger(now=NOW)
        added, _attempts = shadow.capture_candidates(
            ledger,
            [candidate("XRP", 1, minutes=5)],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        lanes = {row["lane"] for row in added}
        self.assertEqual(lanes, {shadow.XRP_PERSISTENCE, shadow.PORTFOLIO})
        portfolio = next(row for row in added if row["lane"] == shadow.PORTFOLIO)
        self.assertEqual(portfolio["required_contracts"], 3)

    def test_second_quote_depth_and_move_fail_closed(self):
        ledger = shadow.empty_ledger(now=NOW)
        added, attempts = shadow.capture_candidates(
            ledger,
            [candidate("ETH", -1)],
            lambda row: row,
            confirmation(price=63, available=2),
            now=NOW,
        )
        self.assertEqual(added, [])
        reasons = {reason for row in attempts for reason in row["reasons"]}
        self.assertIn("insufficient_confirmed_fok_depth", reasons)
        self.assertIn("confirmation_adverse_move", reasons)

    def test_only_official_finalized_result_settles(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(
            ledger,
            [candidate("ETH", -1)],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        later = NOW + timedelta(minutes=20)
        self.assertEqual(
            shadow.settle_records(
                ledger,
                lambda _ticker: {"status": "closed", "result": "no"},
                now=later,
            ),
            [],
        )
        settled = shadow.settle_records(
            ledger,
            lambda _ticker: {"status": "finalized", "result": "no"},
            now=later,
        )
        self.assertEqual(len(settled), 4)
        self.assertTrue(all(row["arm"]["result"] == "WIN" for row in settled))
        summary = shadow.summarize(ledger, now=later)
        self.assertFalse(summary["automatic_promotion"])
        self.assertFalse(summary["historical_backfill"])
        self.assertAlmostEqual(summary["per_lane_confidence_level"], 0.991667)
        self.assertTrue(summary["portfolio_projection"]["one_position_per_ticker"])

    def test_policy_change_does_not_count_prior_records(self):
        ledger = shadow.empty_ledger(now=NOW)
        shadow.capture_candidates(
            ledger,
            [candidate("ETH", -1)],
            lambda row: row,
            confirmation(),
            now=NOW,
        )
        changed = {"CRYPTO_ASSET_SPECIALIST_PERSISTENCE_THRESHOLD": "0.20"}
        summary = shadow.summarize(ledger, settings=changed, now=NOW)
        self.assertEqual(summary["tracked"], 0)
        self.assertEqual(len(ledger["policy_history"]), 1)
        self.assertEqual(summary["policy_registered_at"], NOW.isoformat())


if __name__ == "__main__":
    unittest.main()

