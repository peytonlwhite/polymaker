import copy
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dashboard
import sports_paper_bettor as sports
from sports_unit_accounting import filled_units, record_filled_units, reconcile_scanner_units, reserved_units


def entry(stake=2.66):
    return {"source": "edge_scanner", "strategy_owner": "live_campaign", "mode": "live",
            "status": "open", "kalshi_ticker": "TEST-MARKET", "order_side": "yes",
            "stake": stake, "unit_size": 39.48, "unit_count": 0.5,
            "sports_units": {"additional_units": 0.5, "target_units": 0.5,
                             "placed_units": 0.5, "unit_size": 39.48}}


class SportsFilledUnitTests(unittest.TestCase):
    def test_filled_risk_preserves_requested_tier(self):
        for stake in (2.66, 19.38):
            row = entry(stake)
            record_filled_units(row)
            self.assertAlmostEqual(stake / 39.48, row["unit_count"])
            self.assertEqual(row["unit_count"], row["sports_units"]["placed_units"])
            self.assertEqual(0.5, row["sports_units"]["additional_units"])
            self.assertEqual(0.5, row["sports_units"]["requested_units"])
            self.assertEqual(0.5, row["sports_units"]["target_units"])
            self.assertEqual(0.5, sports.sports_unit_existing_market_units({"bets": [row]}, row))

    def test_no_current_bankroll_fallback_for_invalid_entry_unit(self):
        for value in (None, 0, -1, float("nan"), float("inf")):
            self.assertIsNone(filled_units({"stake": 2.66, "unit_size": value}))
        self.assertIsNone(filled_units({"stake": float("inf"), "unit_size": 39.48}))

    def test_sizing_reserves_actual_risk_if_topup_exceeds_original_tier(self):
        row = entry(50)
        record_filled_units(row)
        self.assertAlmostEqual(50 / 39.48, reserved_units(row))

    def test_repair_is_idempotent_and_preserves_financials_and_other_lanes(self):
        row = {**entry(), "contracts": 7, "fee": .1155, "profit": 4.22,
               "live_order": {"actual_stake": 2.66}}
        manual = {**entry(95.52), "source": "user_manual", "strategy_owner": "user_bet"}
        ai = {**entry(), "source": "aibetpicks", "strategy_owner": "aibetpicks"}
        portfolio = {"balance": 100, "bets": [row, manual, ai], "history": [entry(19.38)]}
        before = copy.deepcopy(portfolio)
        changes = reconcile_scanner_units(portfolio, "2026-09-15T14:45:00-05:00")
        self.assertEqual(2, len(changes))
        self.assertEqual([], reconcile_scanner_units(portfolio, "2026-09-15T15:00:00-05:00"))
        self.assertEqual(before["bets"][1:], portfolio["bets"][1:])
        self.assertEqual(before["balance"], portfolio["balance"])
        for field in ("contracts", "fee", "profit", "stake", "live_order"):
            self.assertEqual(before["bets"][0][field], row[field])
        self.assertEqual(.5, row["unit_accounting_correction"]["previous_unit_count"])


class SportsFullUnitExecutionTests(unittest.TestCase):
    def execute(self, depth=100, approved_cash=19.38, fill_count=51):
        units = entry()["sports_units"]
        campaign = {"active": True, "planned_contracts": 51, "max_single_stake": 19.74,
                    "global_daily_loss_cap": 100, "sports_units": units}
        candidate = {"kalshi_ticker": "TEST-MARKET", "order_side": "yes", "entry_price": 38,
                     "game_started": True, "has_live_score_context": True,
                     "live_campaign": campaign, "sports_units": units, "edge": 10, "model_prob": 60}
        response = {"order": {"fill_count_fp": str(fill_count), "yes_price_dollars": ".38",
                              "taker_fill_cost_dollars": str(round(fill_count * .38, 2)),
                              "taker_fees_dollars": ".84"}}
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(sports, name, return_value=value))
                     for name, value in {
                         "live_order_start_guard": {"ok": True},
                         "live_order_pricing_guard": {"ok": True, "candidate_updates": {
                             "entry_price": 38, "orderbook_depth_valid": True,
                             "executable_contracts_at_ask": depth}},
                         "revalidate_live_campaign_order": {"ok": True, "campaign": campaign, "stake": 19.74},
                         "effective_live_max_stake": 19.74,
                         "effective_live_campaign_daily_loss_cap": 100,
                         "reserve_live_order": {"ok": True, "approved_stake": approved_cash, "reservation_id": "test"},
                         "kalshi_private_request": (response, {}),
                         "finalize_reservation": None, "upsert_live_order_intent": None,
                         "log_line": None,
                     }.items()}
            for name, value in {"SPORTS_FOK_REQUIRE_TOP_DEPTH": True, "SPORTS_FOK_TOP_DEPTH_UTILIZATION": .9,
                                "SPORTS_LIVE_MAX_PRICE_CENTS": 95, "SPORTS_PRICING_V2_ENABLED": False}.items():
                stack.enter_context(patch.object(sports, name, value))
            result = sports.place_live_kalshi_order(candidate, 19.74, portfolio={})
        return result, mocks

    def test_hibernian_depth_does_not_place_seven_contracts(self):
        result, mocks = self.execute(depth=8)
        self.assertEqual("fok_depth_below_requested_units", result["error"])
        self.assertEqual(51, result["execution_depth_review"]["requested_contracts"])
        self.assertEqual(7, result["execution_depth_review"]["usable_contracts"])
        mocks["reserve_live_order"].assert_not_called()
        mocks["kalshi_private_request"].assert_not_called()

    def test_depth_utilization_buffer_still_applies(self):
        result, mocks = self.execute(depth=51)
        self.assertEqual("fok_depth_below_requested_units", result["error"])
        mocks["kalshi_private_request"].assert_not_called()

    def test_sufficient_depth_places_full_rounded_stake_at_same_price(self):
        result, mocks = self.execute(depth=57)
        self.assertTrue(result["ok"], result)
        self.assertEqual(51, result["contracts"])
        self.assertEqual(19.38, result["actual_stake"])
        self.assertEqual(38, result["limit_price"])
        self.assertEqual(19.38, mocks["reserve_live_order"].call_args.args[1])
        self.assertEqual("fill_or_kill", mocks["kalshi_private_request"].call_args.kwargs["body"]["time_in_force"])

    def test_changed_cash_does_not_submit_smaller_order(self):
        result, mocks = self.execute(approved_cash=2.66)
        self.assertEqual("shared_cash_changed_before_submit", result["error"])
        mocks["kalshi_private_request"].assert_not_called()
        mocks["finalize_reservation"].assert_called_once_with("test", "released_available_cash_changed", 0)

    def test_unexpected_partial_fok_retains_recovery_guard(self):
        result, mocks = self.execute(fill_count=7)
        self.assertEqual("campaign_incomplete_fill", result["error"])
        self.assertEqual(2.66, result["actual_stake"])
        self.assertEqual("filled_pending_portfolio", mocks["upsert_live_order_intent"].call_args.kwargs["status"])

    def test_recovered_partial_fill_records_actual_units_once(self):
        candidate = {**entry(), "live_campaign": {"active": True}, "entry_price": 38}
        intent = {"client_order_id": "test-recovery", "status": "filled_pending_portfolio",
                  "candidate": candidate, "created_at": "2026-09-15T14:25:35-05:00",
                  "order_side": "yes", "requested_stake": 19.74,
                  "response": {"fill_count_fp": "7", "yes_price_dollars": ".38",
                               "taker_fill_cost_dollars": "2.66", "taker_fees_dollars": ".12"}}
        portfolio = {"balance": 100, "bets": []}
        with patch.object(sports, "load_live_order_intents", return_value={"intents": [intent]}), \
                patch.object(sports, "upsert_live_order_intent"), patch.object(sports, "save_portfolio"), \
                patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"), \
                patch.object(sports, "kalshi_private_request") as remote:
            recovered = sports.recover_pending_live_order_intents(portfolio)
            sports.recover_pending_live_order_intents(portfolio)
        self.assertEqual(1, len(portfolio["bets"]))
        self.assertAlmostEqual(2.66 / 39.48, recovered[0]["unit_count"])
        self.assertEqual(.5, recovered[0]["unit_target_units"])
        remote.assert_not_called()


class SportsLogUnitTests(unittest.TestCase):
    def line(self, suffix=""):
        stamp = datetime.now(dashboard.CENTRAL_TZ).replace(microsecond=0)
        return stamp, (f"[{stamp:%Y-%m-%d %H:%M:%S}] LIVE SPORTS BET: owner=live_campaign "
                       "moneyline Hibernian Yes TEST-MARKET stake=$2.66 edge=2.809% units=0.5u " + suffix)

    def test_new_placed_log_uses_stake_and_entry_unit(self):
        _, line = self.line("unit_size=39.48 requested_units=0.5")
        row = dashboard._sports_placed_row(line, {})
        self.assertAlmostEqual(2.66 / 39.48, row["units"])
        self.assertEqual(.5, row["requested_units"])

    def history(self, ledger):
        stamp, line = self.line()
        for row in ledger:
            row["placed_at"] = stamp.isoformat()
        with TemporaryDirectory() as tmp, patch.dict(dashboard.SPORTS_CANDIDATE_HISTORY_CACHE, {
            "path": "", "identity": None, "offset": 0, "fragment": b"", "rows": [], "latest_by_ticker": {}
        }, clear=True):
            path = Path(tmp) / "sports.log"
            path.write_text(line + "\n", encoding="utf-8")
            return dashboard.sports_candidate_history(path, portfolio={"bets": ledger})["rows"][0]

    def test_old_log_matches_scanner_not_separate_manual_position(self):
        row = self.history([entry(), {**entry(95.52), "source": "user_manual", "strategy_owner": "user_bet"}])
        self.assertAlmostEqual(2.66 / 39.48, row["units"])
        self.assertEqual(.5, row["requested_units"])

    def test_ambiguous_old_log_is_not_assigned_an_arbitrary_fill(self):
        row = self.history([entry(), entry()])
        self.assertEqual(.5, row["units"])
        self.assertNotIn("units_basis", row)


if __name__ == "__main__":
    unittest.main()
