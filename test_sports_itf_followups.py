import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta

import sports_itf_followups as follow
from sports_itf_shadow import CENTRAL, atomic_json, stamp

NOW = datetime(2026, 9, 16, 11, tzinfo=CENTRAL)


def position(event="new", parent="all_underdogs", *, when=None, settled=False, value=1):
    row = {"id": parent + "|" + event, "strategy": parent, "event_ticker": event,
           "mode": "shadow", "phase": "live", "series": "KXITFDOUBLES" if parent == "all_underdogs" else "KXITFWMATCH",
           "placed_at": stamp(when or NOW + timedelta(minutes=1)), "status": "open",
           "entry_price": .45, "entry_ask": .45, "entry_bid": .4, "side": "yes",
           "contracts": 40, "premium": 18, "fee": .7, "cost": 18.7,
           "fills": [{"price": .45, "contracts": 40, "fee": .7}]}
    if settled:
        row.update(status="settled", settled_at=stamp(NOW + timedelta(hours=2)),
                   payout=40 * value, profit=round(40 * value - 18.7, 4), settlement_value=value,
                   result="WIN" if value == 1 else "LOSS" if value == 0 else "NON_BINARY")
    return row


def source(*rows):
    return {"version": "itf-upsets-v1", "mode": "shadow", "rules_hash": "parent",
            "implementation_hash": "frozen-parent", "started_at": stamp(NOW - timedelta(days=2)),
            "ends_at": stamp(NOW + timedelta(days=5)), "unit_dollars": 19.49,
            "last_scan_at": stamp(NOW), "positions": {r["id"]: r for r in rows}}


class FollowupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source(position("old", when=NOW - timedelta(hours=2), settled=True))
        self.engine = follow.Followups(self.root, self.source, now=NOW, register=True)

    def sync(self, *rows, now=None):
        self.source["positions"].update({r["id"]: r for r in rows})
        self.engine.sync(self.source, now or NOW + timedelta(hours=3))
        return self.engine.summary(now or NOW + timedelta(hours=3))

    def test_registration_preserves_history_but_starts_new_scores_at_zero(self):
        view = self.engine.summary(NOW)
        self.assertEqual(1, view["strategies"][0]["historical"]["settled"])
        self.assertEqual(0, view["strategies"][0]["entries"])
        self.assertEqual(self.source["ends_at"], view["ends_at"])
        self.assertEqual(19.49, view["unit_dollars"])

    def test_only_registered_parent_phase_series_and_new_events_qualify(self):
        rows = [position("new"), position("women", "strengthening"), position("old", "strengthening"),
                position("pregame"), position("wrong_series"), position("wrong_parent", "mid_price")]
        rows[3]["phase"] = "pregame"
        rows[4]["series"] = "KXITFMATCH"
        view = self.sync(*rows)
        self.assertEqual([1, 1], [r["entries"] for r in view["strategies"]])

    def test_excludes_before_start_after_deadline_and_future_entries(self):
        view = self.sync(position("early", when=NOW-timedelta(seconds=1)),
                         position("end", when=NOW+timedelta(days=5)),
                         position("future", when=NOW+timedelta(days=1)))
        self.assertEqual(0, view["strategies"][0]["entries"])

    def test_selection_does_not_depend_on_outcome_or_observer_delay(self):
        view = self.sync(position("win", settled=True), position("loss", settled=True, value=0))
        self.assertEqual(2, view["strategies"][0]["settled"])
        self.assertEqual(1, view["strategies"][0]["wins"])
        self.assertEqual(1, view["strategies"][0]["losses"])

    def test_restart_and_replay_do_not_duplicate_or_reset(self):
        self.sync(position())
        self.engine.persist()
        self.engine = follow.Followups(self.root, self.source, now=NOW+timedelta(hours=4))
        view = self.sync(position(), now=NOW+timedelta(hours=4))
        self.assertEqual(1, view["strategies"][0]["entries"])
        self.assertEqual(stamp(NOW), view["started_at"])

    def test_exact_fee_net_profit_and_non_binary_settlement(self):
        self.sync(position())
        view = self.sync(position(settled=True, value=.5))
        result = view["strategies"][0]
        self.assertEqual(1, result["non_binary"])
        self.assertEqual(round(1.3/19.49, 4), result["profit_units"])
        self.assertEqual(0, result["open_risk_units"])

    def test_open_positions_are_separate_from_realized_results(self):
        result = self.sync(position())["strategies"][0]
        self.assertEqual(0, result["profit_units"])
        self.assertIsNone(result["roi_percent"])
        self.assertEqual(round(18.7/19.49, 4), result["open_risk_units"])
        self.assertEqual(-result["open_risk_units"], result["worst_case_units"])

    def test_does_not_mutate_parent_state(self):
        self.source["positions"].update({"all_underdogs|new": position()})
        before = copy.deepcopy(self.source)
        self.engine.sync(self.source, NOW+timedelta(hours=3))
        self.engine.persist()
        self.assertEqual(before, self.source)

    def test_accounting_mismatch_stops_update_without_losing_positions(self):
        self.sync(position())
        before = copy.deepcopy(self.engine.state)
        bad = position("bad")
        bad["cost"] = 20
        with self.assertRaisesRegex(ValueError, "accounting mismatch"):
            self.sync(bad)
        self.assertEqual(before, self.engine.state)

    def test_parent_fill_or_settlement_cannot_be_rewritten(self):
        self.sync(position(settled=True))
        with self.assertRaisesRegex(ValueError, "settlement changed"):
            self.sync(position())
        changed = position(settled=True)
        changed["entry_bid"] = .39
        with self.assertRaisesRegex(ValueError, "entry changed"):
            self.sync(changed)

    def test_missing_enrolled_parent_is_flagged(self):
        self.sync(position())
        del self.source["positions"]["all_underdogs|new"]
        with self.assertRaisesRegex(ValueError, "entry missing"):
            self.sync()

    def test_source_or_rule_change_refuses_resume(self):
        self.engine.persist()
        changed = copy.deepcopy(self.source)
        changed["unit_dollars"] = 100
        with self.assertRaisesRegex(ValueError, "source changed"):
            follow.Followups(self.root, changed, now=NOW)
        state = json.loads((self.root/follow.STATE).read_text())
        state["rules_hash"] = "changed"
        atomic_json(self.root/follow.STATE, state)
        with self.assertRaisesRegex(ValueError, "rules or source changed"):
            follow.Followups(self.root, self.source, now=NOW)

    def test_missing_or_corrupt_state_never_silently_resets(self):
        with self.assertRaisesRegex(ValueError, "explicit first registration"):
            follow.Followups(self.root, self.source, now=NOW)
        (self.root/follow.STATE).write_text('{broken')
        with self.assertRaises(ValueError):
            follow.Followups(self.root, self.source, now=NOW, register=True)

    def test_compressed_state_supported(self):
        with gzip.open(str(self.root/follow.STATE)+'.gz', 'wt') as handle:
            json.dump(self.engine.state, handle)
        loaded = follow.Followups(self.root, self.source, now=NOW)
        self.assertEqual(self.engine.state, loaded.state)

    def test_deadline_waits_for_parent_final_scan_and_open_settlements(self):
        end = NOW + timedelta(days=5)
        self.assertEqual("settling", self.engine.summary(end)["status"])
        self.source["last_scan_at"] = stamp(end)
        self.engine.sync(self.source, end)
        self.assertEqual("complete", self.engine.summary(end)["status"])

    def test_dashboard_flags_stale_source_even_if_observer_is_fresh(self):
        view = self.sync()
        atomic_json(self.root/follow.REPORT, view)
        self.assertEqual("stale", follow.dashboard_summary(self.root, NOW+timedelta(hours=3))["health"])

    def test_nominal_interval_deterministic_and_winner_dependence_visible(self):
        rows = [position("win", settled=True), position("loss", settled=True, value=0)]
        first = follow.metrics(rows, 19.49, bootstrap=True)
        self.assertEqual(first, follow.metrics(rows, 19.49, bootstrap=True))
        self.assertLess(first["without_best_win_units"], 0)
        self.assertLess(first["exploratory_roi_interval_95"][0], 0)


if __name__ == "__main__":
    unittest.main()
