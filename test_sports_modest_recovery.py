import copy
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

import sports_aibetpicks as picks
import sports_modest_recovery as recovery
import sports_paper_bettor as sports

NOW = datetime(2026, 9, 16, 11, 0, tzinfo=recovery.CHICAGO)


def settled(profit=-10, *, lane="aibetpicks", identity="loss", when=None, **changes):
    when = when or NOW - timedelta(hours=1)
    return {"id": identity, "source": "aibetpicks" if lane == "aibetpicks" else "edge_scanner",
            "strategy_owner": "aibetpicks" if lane == "aibetpicks" else "live_campaign",
            "mode": "live", "status": "settled", "unit_size": 10, "stake": 10,
            "profit": profit, "result": "WIN" if profit > 0 else "LOSS",
            "placed_at": (when - timedelta(hours=2)).isoformat(), "settled_at": when.isoformat(), **changes}


def used(extra, *, lane="aibetpicks", identity="boost", is_open=True, when=None, game="other"):
    row = settled(0, lane=lane, identity=identity, when=when)
    row.update(status="open" if is_open else "settled", game_key=game,
               sports_units={"recovery": {"version": recovery.VERSION, "base_units": 1, "added_units": extra}})
    return row


def review(rows, *, lane="aibetpicks", candidate=None, base=1, maximum=5, now=NOW, ledger=None):
    return recovery.review({"history": rows, recovery.LEDGER_KEY: ledger or {}},
                           candidate or {"entry_price": 50, "edge": 3}, lane=lane,
                           base_units=base, maximum_units=maximum, is_enabled=True, now=now)


class ModestRecoveryPolicyTests(unittest.TestCase):
    def test_half_one_and_two_unit_additions_and_pause(self):
        cases = [([-10], .5), ([-10, -12], 1), ([-15, -15, -15], 2), ([-20, -20, -20], 0)]
        for profits, expected in cases:
            with self.subTest(profits=profits):
                r = review([settled(p, identity=str(i)) for i, p in enumerate(profits)])
                self.assertEqual(expected, r["added_units"])
                self.assertEqual(1 + expected, r["target_units"])

    def test_wins_reduce_deficit_and_restore_base(self):
        rows = [settled(-20), settled(15, identity="win")]
        self.assertEqual(.5, review(rows)["added_units"])
        rows.append(settled(5, identity="even"))
        self.assertEqual(0, review(rows)["added_units"])

    def test_lane_isolation_manual_paper_and_unsettled_losses_excluded(self):
        rows = [settled(-100, lane="scanner"), settled(-100, identity="manual", source="user_manual"),
                settled(-100, identity="paper", mode="paper"), settled(-100, identity="open", status="open")]
        self.assertEqual(0, review(rows)["added_units"])
        self.assertEqual(0, review(rows, lane="scanner")["added_units"])  # Severe scanner day pauses additions.
        self.assertEqual(.5, review([settled(-10, lane="scanner")], lane="scanner")["added_units"])

    def test_previous_day_carry_is_capped_expires_and_uses_central_time(self):
        yesterday = NOW - timedelta(days=1)
        r = review([settled(-40, when=yesterday)])
        self.assertEqual(3, r["carryover_units"])
        self.assertEqual(1, r["added_units"])
        self.assertEqual(0, review([settled(-40, when=NOW-timedelta(days=2))])["added_units"])
        # 04:30 UTC on the 16th is still September 15 in Chicago.
        row = settled(-10, settled_at="2026-09-16T04:30:00Z")
        self.assertEqual(1, review([row])["carryover_units"])
        winter = datetime(2026, 1, 16, 11, tzinfo=recovery.CHICAGO)
        row = settled(-10, when=winter, settled_at="2026-01-16T05:30:00Z")
        self.assertEqual(1, review([row], now=winter)["carryover_units"])

    def test_previous_severe_day_does_not_hide_behind_carry_cap(self):
        self.assertEqual("loss_additions_paused", review([settled(-70, when=NOW-timedelta(days=1))])["reason"])

    def test_winning_day_offsets_later_loss_and_push_does_not_trigger(self):
        self.assertEqual(0, review([settled(20), settled(-10, identity="later")])["added_units"])
        self.assertEqual(0, review([settled(0, result="PUSH")])["added_units"])

    def test_pending_results_and_missing_accounting_do_not_trigger(self):
        self.assertEqual(0, review([settled(-50, status="pending")])["added_units"])
        r = review([settled(-10), settled(-20, identity="bad", unit_size=None)])
        self.assertEqual("loss_additions_paused", r["reason"])

    def test_duplicate_records_and_restarts_do_not_double_losses_or_budget(self):
        loss, boost = settled(-10), used(.5)
        portfolio = {"history": [loss, copy.deepcopy(loss)], "bets": [boost]}
        recovery.retain_ledger(portfolio, NOW)
        original = recovery.summary(portfolio, NOW)
        self.assertEqual(1, original["lanes"]["aibetpicks"]["drawdown_units"])
        self.assertEqual(.5, original["combined_daily_extra_units"])
        restored = json.loads(json.dumps(portfolio))
        restored["history"] = []  # Simulate trimming/compressing history.
        restored["bets"] = []
        self.assertEqual(original, recovery.summary(restored, NOW))

    def test_late_settlement_releases_open_budget_and_uses_original_unit(self):
        boost = used(1, when=NOW-timedelta(days=3))
        portfolio = {"bets": [boost]}
        recovery.retain_ledger(portfolio, NOW)
        self.assertEqual(1, recovery.summary(portfolio, NOW)["combined_open_extra_units"])
        closed = {**boost, "status": "settled", "settled_at": NOW.isoformat(), "profit": -15}
        portfolio.update(bets=[], history=[closed])
        view = recovery.summary(portfolio, NOW)
        self.assertEqual(0, view["combined_open_extra_units"])
        self.assertEqual(1.5, view["lanes"]["aibetpicks"]["drawdown_units"])

    def test_daily_combined_open_and_lane_budgets(self):
        loss = settled(-25)
        self.assertEqual(0, review([loss, used(2)])["added_units"])
        self.assertEqual(0, review([loss, used(3, is_open=False)])["added_units"])
        self.assertEqual(0, review([loss, used(2, is_open=False), used(2, lane="scanner", identity="scanner", is_open=False)])["added_units"])
        self.assertEqual(.5, review([loss, used(1.5, lane="scanner")])["added_units"])

    def test_midnight_resets_daily_budget_but_retains_open_budget(self):
        yesterday = NOW - timedelta(days=1)
        rows = [settled(-20, when=yesterday), used(2, when=yesterday)]
        r = review(rows)
        self.assertEqual(0, r["combined_daily_extra_units"])
        self.assertEqual(0, r["added_units"])

    def test_price_and_base_limits_only_remove_bonus(self):
        losses = [settled(-15, identity=str(i)) for i in range(3)]
        self.assertEqual(0, review(losses, candidate={"entry_price": 80})["added_units"])
        self.assertEqual(1, review(losses, candidate={"entry_price": 30})["added_units"])
        self.assertEqual(.5, review(losses, base=4.5)["added_units"])
        self.assertEqual(0, review(losses, base=5)["added_units"])
        self.assertEqual(1, review(losses, base=.5)["added_units"])

    def test_scanner_quality_and_existing_positions_are_preserved(self):
        rows = [settled(-15, lane="scanner", identity=str(i)) for i in range(3)]
        for edge, expected in [(0, 0), (.2, .5), (1.2, 1), (2.2, 2)]:
            self.assertEqual(expected, review(rows, lane="scanner", candidate={"entry_price": 50, "edge": edge})["added_units"])
        self.assertEqual(0, review(rows, lane="scanner", candidate={"entry_price": 50, "edge": 3, "skip_reasons": ["blocked"]})["added_units"])
        self.assertEqual(0, review(rows, lane="scanner", candidate={"entry_price": 50, "edge": 3, "sports_units": {"existing_units": .5}})["added_units"])

    def test_same_event_does_not_stack_additions_across_lanes(self):
        self.assertEqual(0, review([settled(-10), used(.5, lane="scanner", game="match")], candidate={"entry_price": 50, "game_key": "match"})["added_units"])

    def test_review_does_not_spend_budget_or_mutate_live_state(self):
        portfolio = {"history": [settled(-10)], "bets": []}
        before = copy.deepcopy(portfolio)
        for _ in range(3):
            decision = recovery.review(portfolio, {"entry_price": 50}, lane="aibetpicks", base_units=1,
                                       maximum_units=5, now=NOW, is_enabled=True)
            self.assertEqual(.5, decision["added_units"])
        self.assertEqual(before, portfolio)

    def test_malformed_or_negative_base_never_creates_a_bet(self):
        for base in (None, float("nan"), float("inf"), -1, 0, True):
            self.assertEqual(0, review([settled(-25)], base=base)["added_units"])


class ModestRecoveryIntegrationTests(unittest.TestCase):
    def source_candidate(self, units=1):
        return {"aibetpicks": {"active": True, "pick": {"stake_units": units}},
                "kalshi_ticker": "TEST", "order_side": "yes", "market_type": "moneyline",
                "entry_price": 50, "executable_contracts_at_ask": 1000, "orderbook_depth_valid": True}

    def test_source_sizing_keeps_published_base_and_optional_addition(self):
        with patch.object(recovery, "enabled", return_value=True), \
                patch.object(sports, "effective_sports_unit_size", return_value=10), \
                patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", .85), \
                patch.object(picks, "lane_preflight", return_value=""):
            size = picks.execution_sizing(sports, {"history": [settled(-10)]}, self.source_candidate(), now=NOW)
        self.assertEqual(1, size["published_units"])
        self.assertEqual(1.5, size["target_units"])

    def test_source_depth_and_exposure_trim_bonus_before_base(self):
        with patch.object(recovery, "enabled", return_value=True), \
                patch.object(sports, "effective_sports_unit_size", return_value=10), \
                patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 1), \
                patch.object(picks, "lane_preflight", return_value="exposure_cap"):
            candidate = self.source_candidate()
            size = picks.execution_sizing(sports, {"history": [settled(-25)]}, candidate, now=NOW)
            self.assertEqual(1, size["target_units"])
        with patch.object(recovery, "enabled", return_value=True), \
                patch.object(sports, "effective_sports_unit_size", return_value=10), \
                patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 1), \
                patch.object(picks, "lane_preflight", return_value=""):
            candidate["executable_contracts_at_ask"] = 30
            size = picks.execution_sizing(sports, {"history": [settled(-25)]}, candidate, now=NOW)
            self.assertEqual(1.5, size["target_units"])

    def test_scanner_integration_obeys_risk_caps_and_revalidation(self):
        portfolio = {"history": [settled(-25, lane="scanner")]}
        candidate = {"entry_price": 50, "edge": 3, "confidence_score": 100}
        with ExitStack() as stack:
            for name, value in {"live_campaign_candidate_book_counts": (10, 10), "candidate_pro_score": 100,
                                "final_score_value": 100, "low_edge_sport_clv_review": {}, "sports_unit_steps": [1],
                                "sports_unit_existing_market_units": 0, "effective_sports_unit_size": 10,
                                "sports_unit_bankroll_base": 1000,
                                "sports_probability_unit_review": ({"target_units": 1}, None)}.items():
                stack.enter_context(patch.object(sports, name, return_value=value))
            stack.enter_context(patch.object(sports, "SPORTS_UNIT_TIERS", {1: {"min_edge": .1}}))
            stack.enter_context(patch.object(sports, "SPORTS_UNIT_STAKING_ENABLED", True))
            stack.enter_context(patch.object(sports, "SPORTS_UNIT_MAX_PER_MARKET", 5))
            stack.enter_context(patch.object(sports, "EXECUTION_MODE", "live"))
            stack.enter_context(patch.object(recovery, "enabled", return_value=True))
            # Pass a time-local record because the engine uses the live clock.
            portfolio["history"][0] = settled(-25, lane="scanner", when=datetime.now(recovery.CHICAGO)-timedelta(minutes=1))
            with patch.object(sports, "sports_unit_risk_caps", return_value=[]):
                result = sports.sports_unit_candidate_review(portfolio, candidate)
                self.assertEqual(2, result["additional_units"])
                self.assertEqual(20, result["requested_stake"])
                self.assertEqual(1, result["recovery"]["base_units"])
            with patch.object(sports, "sports_unit_risk_caps", return_value=[{"max_units": 1}]):
                self.assertEqual(1, sports.sports_unit_candidate_review(portfolio, candidate)["additional_units"])

    def test_final_source_executor_rechecks_bonus_and_keeps_durable_intents(self):
        for enabled, budget_used, expected_count in [(True, False, 30), (False, False, 20), (True, True, 20)]:
            now = datetime.now(recovery.CHICAGO)
            portfolio = {"history": [settled(-10, when=now-timedelta(minutes=1))]}
            if budget_used:
                portfolio["bets"] = [used(2, lane="scanner", when=now-timedelta(minutes=1))]
            candidate = self.source_candidate()
            candidate["sports_units"] = {"unit_size": 10, "target_units": 1.5,
                "recovery": {"version": recovery.VERSION, "base_units": 1, "target_units": 1.5, "added_units": .5}}
            with ExitStack() as stack:
                stack.enter_context(patch.object(recovery, "enabled", return_value=enabled))
                stack.enter_context(patch.object(picks, "lane_preflight", return_value=""))
                mocks = {}
                for name, value in {"live_order_start_guard": {"ok": True},
                                    "live_order_pricing_guard": {"ok": True, "candidate_updates": {}},
                                    "effective_live_max_stake": 50, "log_line": None,
                                    "reserve_live_order": {"ok": True, "approved_stake": 50},
                                    "upsert_live_order_intent": None,
                                    "kalshi_private_request": ({"order": {"fill_count_fp": str(expected_count),
                                        "taker_fill_cost_dollars": str(expected_count*.5), "taker_fees_dollars": ".5"}}, {})}.items():
                    mocks[name] = stack.enter_context(patch.object(sports, name, return_value=value))
                stack.enter_context(patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95))
                stack.enter_context(patch.object(sports, "SPORTS_UNIT_MAX_PER_MARKET", 5))
                stack.enter_context(patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", .85))
                result = sports.place_live_kalshi_order(candidate, 15, portfolio)
            self.assertTrue(result["ok"], result)
            self.assertEqual(expected_count, result["contracts"])
            self.assertEqual("prepared", mocks["upsert_live_order_intent"].call_args_list[0].kwargs["status"])
            self.assertEqual("filled_pending_portfolio", mocks["upsert_live_order_intent"].call_args_list[-1].kwargs["status"])
            self.assertEqual("fill_or_kill", mocks["kalshi_private_request"].call_args.kwargs["body"]["time_in_force"])

    def test_source_executor_rejects_unapproved_addition_before_submission(self):
        candidate = self.source_candidate()
        candidate["sports_units"] = {"unit_size": 10, "target_units": 2}
        with patch.object(sports, "live_order_start_guard", return_value={"ok": True}), \
                patch.object(sports, "live_order_pricing_guard", return_value={"ok": True}), \
                patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95), \
                patch.object(sports, "kalshi_private_request") as submit:
            result = sports.place_live_kalshi_order(candidate, 20, {})
        self.assertEqual("aibetpicks_source_sizing_mismatch", result["error"])
        submit.assert_not_called()

    def test_fill_record_retains_recovery_metadata_for_budget_and_recovery(self):
        candidate = self.source_candidate()
        candidate.update(pick_id="pick", aibetpicks_bot_id="bot", game_key="match")
        candidate["sports_units"] = {"unit_size": 10, "target_units": 1.5, "published_units": 1,
                                    "recovery": {"version": recovery.VERSION, "base_units": 1, "added_units": .5}}
        order = {"ok": True, "actual_stake": 15, "contracts": 30,
                 "request": {"client_order_id": "fill"}, "response": {"taker_fees_dollars": ".5"}}
        portfolio = {"balance": 100, "bets": []}
        with patch.object(sports, "save_portfolio"), patch.object(sports, "upsert_live_order_intent"), \
                patch.object(sports, "append_jsonl"), patch.object(picks, "now_local", return_value=NOW):
            picks.record_fill(sports, portfolio, candidate, order)
        self.assertEqual(.5, recovery.summary(portfolio, NOW)["combined_open_extra_units"])
        recovery.retain_ledger(portfolio, NOW)
        self.assertEqual(.5, recovery.summary(json.loads(json.dumps(portfolio)), NOW)["combined_daily_extra_units"])


if __name__ == "__main__":
    unittest.main()
