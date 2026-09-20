import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import crypto_paper_bettor
import shared_bankroll
import sports_paper_bettor


class PerformanceResetTests(unittest.TestCase):
    def test_crypto_one_cent_rows_are_excluded_from_strategy_analytics(self):
        settings = dict(crypto_paper_bettor.DEFAULT_SETTINGS)
        settings["CRYPTO_ANALYTICS_MIN_ENTRY_PRICE_CENTS"] = "2"
        self.assertFalse(crypto_paper_bettor.crypto_analytics_included({"entry_price": 1}, settings))
        self.assertTrue(crypto_paper_bettor.crypto_analytics_included({"entry_price": 2}, settings))

    def test_shared_daily_profit_only_counts_settlements_after_cutoff(self):
        now = datetime.now().astimezone()
        portfolio = {
            "history": [
                {"mode": "live", "status": "settled", "settled_at": (now - timedelta(minutes=2)).isoformat(), "profit": 10},
                {"mode": "live", "status": "settled", "settled_at": (now + timedelta(minutes=2)).isoformat(), "profit": 3},
            ]
        }
        with patch.object(shared_bankroll, "active_daily_reset_cutoff", return_value=now):
            rows = shared_bankroll.settled_rows_today(portfolio, "sports", live_only=True)
        self.assertEqual(shared_bankroll.realized_profit(rows), 3.0)

    def test_sports_daily_profit_respects_reset_cutoff(self):
        now = datetime.now().astimezone()
        portfolio = {
            "history": [
                {"result": "WIN", "settled_at": (now - timedelta(minutes=2)).isoformat(), "profit": 10},
                {"result": "WIN", "settled_at": (now + timedelta(minutes=2)).isoformat(), "profit": 4},
            ]
        }
        with patch.object(sports_paper_bettor, "active_daily_reset_cutoff", return_value=now):
            self.assertEqual(sports_paper_bettor.daily_realized_profit(portfolio), 4.0)


if __name__ == "__main__":
    unittest.main()
