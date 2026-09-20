from copy import deepcopy
from datetime import timedelta
import gzip
import json
from pathlib import Path
import tempfile
import unittest

import itf_shadow_accounting as accounting
import sports_itf_shadow as itf
from test_sports_itf_shadow import NOW, EVENT, pair, books, book, context


class ITFAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = itf.Experiment(self.root / "sports_itf_shadow_state.json", unit_dollars=19.49, now=NOW)

    def enter(self, price=.10):
        quotes = books(price - .01, price)
        return self.engine.observe(pair(), quotes, quotes, context(), 1, NOW)

    def settle(self, result="yes"):
        return self.engine.settle({EVENT + "-ALP": {"ticker": EVENT + "-ALP", "status": "finalized", "result": result}}, NOW)

    def save(self):
        self.engine.persist()
        itf.atomic_json(self.root / "sports_itf_shadow_report.json", self.engine.summary(NOW))

    def test_ten_cent_winner_earns_over_eight_units_after_fees(self):
        self.enter(.10)
        row = self.settle()[0]
        result, errors = accounting.position_accounting(row, 19.49)
        self.assertEqual([], errors)
        self.assertGreater(result["profit_units"], 8)
        self.assertAlmostEqual((row["contracts"] - row["cost"]) / 19.49, result["profit_units"])
        self.assertAlmostEqual(result["staked_units"] + result["profit_units"], result["payout_units"])

    def test_twenty_cent_win_and_loss_use_actual_fill_cost(self):
        self.enter(.20)
        row = self.settle()[0]
        gain, _ = accounting.position_accounting(row, 19.49)
        self.assertGreater(gain["profit_units"], 3.5)
        loss = {**row, "payout": 0, "profit": -row["cost"], "settlement_value": 0, "result": "LOSS"}
        result, errors = accounting.position_accounting(loss, 19.49)
        self.assertEqual([], errors)
        self.assertAlmostEqual(-row["cost"] / 19.49, result["profit_units"])
        self.assertNotEqual(-1, result["profit_units"])

    def test_opponent_no_win_and_nonbinary_payout_use_held_contract_value(self):
        quotes = books()
        quotes[EVENT + "-BET"] = book(.73, .77)
        self.engine.observe(pair(), quotes, quotes, context(), 1, NOW)
        ticker = EVENT + "-BET"
        rows = self.engine.settle({ticker: {"ticker": ticker, "status": "settled", "result": "no"}}, NOW)
        row = rows[0]
        self.assertEqual("no", row["side"])
        result, errors = accounting.position_accounting(row, 19.49)
        self.assertEqual([], errors)
        self.assertGreater(result["profit_units"], 2)
        partial = {**row, "settlement_value": .5, "payout": row["contracts"] / 2,
                   "profit": round(row["contracts"] / 2 - row["cost"], 4), "result": "NON_BINARY"}
        result, errors = accounting.position_accounting(partial, 19.49)
        self.assertEqual([], errors)
        self.assertAlmostEqual(partial["profit"] / 19.49, result["profit_units"])

    def test_open_bet_has_potential_profit_but_no_realized_profit(self):
        row = self.enter()[0]
        result, errors = accounting.position_accounting(row, 19.49)
        self.assertEqual([], errors)
        self.assertIsNone(result["profit_units"])
        self.assertIsNone(result["payout_units"])
        self.assertGreater(result["win_profit_units"], 8)

    def test_dashboard_uses_frozen_unit_and_does_not_write_or_restart_ledger(self):
        self.enter(); self.settle(); self.save()
        itf.atomic_json(self.root / "sports_paper_portfolio.json", {"sports_unit_daily_snapshot": {"unit_size_at_capture": 39.48}})
        before = self.engine.path.read_bytes()
        view = accounting.dashboard_summary(self.root, NOW)
        self.assertTrue(view["accounting"]["ok"])
        self.assertEqual(19.49, view["accounting"]["unit_dollars"])
        self.assertEqual(before, self.engine.path.read_bytes())
        self.assertEqual(self.engine.state["ends_at"], view["ends_at"])
        self.assertAlmostEqual(view["positions"][0]["profit"] / 19.49, view["positions"][0]["profit_units"])

    def test_all_rows_visible_and_totals_include_more_than_two_hundred(self):
        self.enter(); template = self.settle()[0]
        self.engine.state["positions"] = {}
        for i in range(205):
            row = deepcopy(template)
            row.update(event_ticker=f"KXITFMATCH-26SEP14TEST{i}", strategy="all_underdogs")
            row["id"] = row["strategy"] + "|" + row["event_ticker"]
            if i % 2:
                row.update(result="LOSS", payout=0, profit=-row["cost"], settlement_value=0)
            self.engine.state["positions"][row["id"]] = row
        self.save()
        self.assertEqual(200, len(self.engine.summary(NOW)["positions"]))
        view = accounting.dashboard_summary(self.root, NOW)
        self.assertEqual(205, len(view["positions"]))
        self.assertEqual(205, view["accounting"]["checked_positions"])
        self.assertTrue(view["accounting"]["ok"])
        strategy = view["strategies"][0]
        self.assertAlmostEqual(strategy["profit_units"], sum(r["profit_units"] for r in view["positions"]), places=4)
        self.assertAlmostEqual(strategy["gains_units"] + strategy["losses_units"], strategy["profit_units"], places=4)

    def test_corrupt_profit_or_payout_is_flagged_not_silently_rewritten(self):
        self.enter(); row = self.settle()[0]
        bad = {**row, "profit": 19.49}
        _, errors = accounting.position_accounting(bad, 19.49)
        self.assertIn("net_profit_mismatch", errors)
        self.assertEqual(19.49, bad["profit"])
        _, errors = accounting.position_accounting({**row, "payout": 1}, 19.49)
        self.assertIn("settlement_payout_mismatch", errors)

    def test_compressed_ledger_and_stale_worker_health_are_preserved(self):
        self.enter(); self.save()
        with gzip.open(str(self.engine.path) + ".gz", "wb") as handle:
            handle.write(self.engine.path.read_bytes())
        self.engine.path.unlink()
        view = accounting.dashboard_summary(self.root, NOW + timedelta(minutes=4))
        self.assertTrue(view["accounting"]["ok"])
        self.assertEqual("stale", view["health"])
        self.assertFalse(self.engine.path.exists())

    def test_missing_ledger_or_mismatched_cohort_is_reported_without_reset(self):
        self.enter(); self.save()
        self.engine.path.unlink()
        self.assertFalse(accounting.dashboard_summary(self.root, NOW)["accounting"]["ok"])
        self.assertFalse(self.engine.path.exists())
        self.save()
        report_path = self.root / "sports_itf_shadow_report.json"
        report = json.loads(report_path.read_text())
        report["unit_dollars"] = 39.48
        itf.atomic_json(report_path, report)
        self.assertFalse(accounting.dashboard_summary(self.root, NOW)["accounting"]["ok"])


if __name__ == "__main__":
    unittest.main()
