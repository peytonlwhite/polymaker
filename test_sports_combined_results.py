import copy
import unittest
from datetime import datetime, timedelta

from sports_combined_results import CHICAGO, report
from sports_modest_recovery import retain_ledger


NOW = datetime(2026, 9, 16, 10, tzinfo=CHICAGO)


def row(identity, lane, profit, **overrides):
    result = {
        "id": identity, "mode": "live", "status": "settled",
        "source": "aibetpicks" if lane == "aibetpicks" else "edge_scanner",
        "strategy_owner": "aibetpicks" if lane == "aibetpicks" else "live_campaign",
        "placed_at": (NOW - timedelta(hours=2)).isoformat(),
        "settled_at": (NOW - timedelta(hours=1)).isoformat(),
        "profit": profit, "unit_size": 10,
    }
    result.update(overrides)
    return result


class CombinedResultsTests(unittest.TestCase):
    def test_offsets_wins_and_losses_and_preserves_separate_results(self):
        portfolio = {"history": [row("ai", "aibetpicks", -25), row("scanner", "scanner", 15)]}
        before = copy.deepcopy(portfolio)
        today = report(portfolio, NOW)["days"][1]
        self.assertEqual(-2.5, today["lanes"]["aibetpicks"]["net_units"])
        self.assertEqual(1.5, today["lanes"]["scanner"]["net_units"])
        self.assertEqual(-1, today["combined"]["net_units"])
        self.assertEqual(2, today["combined"]["settled_count"])
        self.assertEqual(before, portfolio)

    def test_excludes_manual_paper_and_open_positions(self):
        portfolio = {"history": [row("manual", "aibetpicks", -100, source="user_manual"),
                                  row("paper", "scanner", -100, mode="paper")],
                     "bets": [row("open", "scanner", -100, status="open")]}
        self.assertEqual(0, report(portfolio, NOW)["days"][1]["combined"]["settled_count"])

    def test_missing_accounting_is_unknown_instead_of_zero(self):
        portfolio = {"history": [row("ai", "aibetpicks", -25),
                                  row("scanner", "scanner", 15, unit_size=None)]}
        total = report(portfolio, NOW)["days"][1]["combined"]
        self.assertIsNone(total["net_units"])
        self.assertEqual(-2.5, total["known_net_units"])
        self.assertEqual(1, total["missing_accounting_count"])

    def test_retained_ledger_survives_history_trimming_and_deduplicates(self):
        portfolio = {"history": [row("ai", "aibetpicks", -25)]}
        retain_ledger(portfolio, NOW)
        expected = report(portfolio, NOW)
        portfolio["history"] = []
        self.assertEqual(expected, report(portfolio, NOW))
        self.assertEqual(1, expected["days"][1]["combined"]["settled_count"])

    def test_settlement_days_use_chicago_and_original_unit(self):
        portfolio = {"history": [row("ai", "aibetpicks", -20, unit_size=20,
                                      settled_at="2026-09-16T04:30:00Z")],
                     "unit_size": 100}
        result = report(portfolio, NOW)
        self.assertEqual(-1, result["days"][0]["combined"]["net_units"])
        self.assertEqual(0, result["days"][1]["combined"]["net_units"])


if __name__ == "__main__":
    unittest.main()
