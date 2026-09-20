import unittest

from sports_live_campaign import (
    build_campaign_context,
    campaign_bot_number,
    contract_fee,
    contract_win_profit,
    review_candidate,
)


TODAY = "2026-07-18"


def row_date(_value):
    return TODAY


def candidate(price=50, edge=13, confidence=91, pro=112, final=101):
    return {
        "game_started": True,
        "has_live_score_context": True,
        "market_type": "moneyline",
        "entry_price": price,
        "edge": edge,
        "confidence_score": confidence,
        "pro_review": {"score": pro},
        "final_bet_score": final,
        "game_key": "away::home",
        "event_ticker": "EVENT-1",
    }


THRESHOLDS = {
    "qualified": {"edge": 5, "confidence": 78, "pro_score": 90, "final_score": 90},
    "strong": {"edge": 8, "confidence": 84, "pro_score": 100, "final_score": 95},
    "elite": {"edge": 12, "confidence": 90, "pro_score": 110, "final_score": 100},
}


class SportsLiveCampaignTests(unittest.TestCase):
    def test_legacy_campaign_rows_default_to_bot_one(self):
        self.assertEqual(campaign_bot_number({"live_campaign": {"active": True}}), 1)
        self.assertEqual(campaign_bot_number({"bot_number": 2}), 2)

    def test_empty_goal_schedule_is_an_always_on_portfolio(self):
        portfolio = {
            "balance": 125.0,
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 25.0,
                "strategy_owner": "live_campaign",
                "placed_at": "placed",
                "settled_at": "settled",
                "live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 1,
                },
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(),
            daily_loss_cap=500,
            bot_number=1,
        )
        self.assertFalse(context["goals_enabled"])
        self.assertFalse(context["complete"])
        self.assertEqual(context["status"], "active")
        self.assertEqual(context["target_remaining"], 0.0)
        self.assertEqual(context["progression_mode"], "always_on_unit_portfolio")

    def test_campaign_id_reset_starts_cycle_one_but_preserves_daily_risk_accounting(self):
        portfolio = {
            "balance": 102.0,
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
                "strategy_owner": "live_campaign",
                "placed_at": "old-placed",
                "settled_at": "old-settled",
                "live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                    "campaign_id": "",
                    "bot_number": 1,
                },
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            daily_loss_cap=75,
            campaign_id="reset-1",
            bot_number=1,
        )
        self.assertEqual(context["campaign_id"], "reset-1")
        self.assertEqual(context["cycle_number"], 1)
        self.assertEqual(context["realized_profit"], 0.0)
        self.assertEqual(context["target_remaining"], 2.0)
        self.assertEqual(context["bot_daily_realized_profit"], 2.0)

        portfolio["history"].append({
            "status": "settled",
            "result": "WIN",
            "profit": 2.0,
            "strategy_owner": "live_campaign",
            "placed_at": "new-placed",
            "settled_at": "new-settled",
            "live_campaign": {
                "role": "primary",
                "campaign_date": TODAY,
                "campaign_id": "reset-1",
                "bot_number": 1,
            },
        })
        advanced = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            daily_loss_cap=75,
            campaign_id="reset-1",
            bot_number=1,
        )
        self.assertEqual(advanced["cycle_number"], 2)
        self.assertEqual(advanced["realized_profit"], 2.0)
        self.assertEqual(advanced["bot_daily_realized_profit"], 4.0)

    def test_first_primary_is_sized_to_net_twenty_profit(self):
        review = review_candidate({}, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertTrue(review["eligible"])
        self.assertEqual(review["role"], "primary")
        self.assertEqual(review["target_profit"], 20.0)
        self.assertEqual(review["applied_stake"], 21.0)
        self.assertEqual(review["planned_contracts"], 42)
        self.assertGreaterEqual(review["planned_win_profit"], 20.0)
        self.assertLess(contract_win_profit(41, 50, 0.07), 20.0)
        self.assertGreater(review["estimated_fee"], 0)

    def test_support_stakes_follow_quality(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "placed_at": "now",
                "game_key": "first::game",
                "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
            }]
        }
        review = review_candidate(portfolio, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertEqual(review["role"], "support")
        self.assertEqual(review["quality_tier"], "elite")
        self.assertEqual(review["target_profit"], 15.0)
        self.assertEqual(review["applied_stake"], 16.0)
        self.assertTrue(review["planned_target_met"])

    def test_settled_support_bets_do_not_prevent_refilling_open_slots(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "placed_at": "primary",
                "game_key": "first::game",
                "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
            }],
            "history": [
                {
                    "status": "settled",
                    "result": "WIN",
                    "profit": 5,
                    "strategy_owner": "live_campaign",
                    "placed_at": f"support-{index}",
                    "settled_at": f"support-{index}",
                    "live_campaign": {"role": "support", "campaign_date": TODAY, "cycle_number": 1},
                }
                for index in range(4)
            ],
        }
        review = review_candidate(portfolio, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertTrue(review["eligible"])
        self.assertEqual(review["role"], "support")

    def test_recovery_is_payout_aware_and_capped_by_quality(self):
        portfolio = {
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -20,
                "strategy_owner": "live_campaign",
                "placed_at": "now",
                "settled_at": "now",
                "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
            }]
        }
        elite = review_candidate(portfolio, candidate(price=50), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertEqual(elite["target_profit"], 40.0)
        self.assertTrue(elite["planned_target_met"])
        plus_money = review_candidate(portfolio, candidate(price=40), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertTrue(plus_money["planned_target_met"])
        qualified = review_candidate(
            portfolio,
            candidate(price=55, edge=6, confidence=80, pro=92, final=92),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
        )
        self.assertEqual(qualified["quality_tier"], "qualified")
        self.assertTrue(qualified["planned_target_met"])

    def test_primary_wins_advance_twenty_ten_five_then_stop(self):
        history = []
        goals = (20.0, 10.0, 5.0)
        for cycle, goal in enumerate(goals, start=1):
            review = review_candidate({"history": history}, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
            self.assertGreaterEqual(review["planned_win_profit"], goal)
            history.append({
                "status": "settled",
                "result": "WIN",
                "profit": goal,
                "strategy_owner": "live_campaign",
                "placed_at": f"placed-{cycle}",
                "settled_at": f"settled-{cycle}",
                "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": cycle},
            })
        context = build_campaign_context({"history": history}, TODAY, row_date)
        self.assertTrue(context["complete"])
        stopped = review_candidate({"history": history}, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertFalse(stopped["eligible"])
        self.assertEqual(stopped["reason"], "campaign_complete")

    def test_one_oversized_primary_win_advances_only_one_cycle(self):
        history = [{
            "status": "settled",
            "result": "WIN",
            "profit": 40,
            "strategy_owner": "live_campaign",
            "placed_at": "primary",
            "settled_at": "primary",
            "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
        }]
        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=(10.0, 5.0, 2.5),
        )
        self.assertEqual(context["cycle_number"], 2)
        self.assertFalse(context["complete"])
        self.assertEqual(context["target_remaining"], 5.0)

    def test_one_open_cycle_ledger_counts_settled_campaign_profit(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "placed_at": "primary",
                "stake": 10,
                "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
            }],
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 20,
                "strategy_owner": "live_campaign",
                "placed_at": "support",
                "settled_at": "support",
                "live_campaign": {"role": "support", "campaign_date": TODAY, "cycle_number": 1},
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(10.0, 5.0, 2.5),
        )
        self.assertEqual(context["cycle_number"], 2)
        self.assertFalse(context["complete"])

    def test_support_drawdown_is_recovered_even_after_primary_win(self):
        portfolio = {
            "history": [
                {
                    "status": "settled",
                    "result": "WIN",
                    "profit": 10,
                    "strategy_owner": "live_campaign",
                    "placed_at": "primary",
                    "settled_at": "primary",
                    "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
                },
                {
                    "status": "settled",
                    "result": "LOSS",
                    "profit": -15,
                    "strategy_owner": "live_campaign",
                    "placed_at": "support",
                    "settled_at": "support",
                    "live_campaign": {"role": "support", "campaign_date": TODAY, "cycle_number": 1},
                },
            ]
        }
        review = review_candidate(portfolio, candidate(price=50), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertEqual(review["cycle_number"], 1)
        self.assertEqual(review["cycle_goal"], 20.0)
        self.assertEqual(review["target_profit"], 25.0)
        self.assertGreaterEqual(review["planned_win_profit"], 25.0)

    def test_live_market_price_and_score_are_hard_requirements(self):
        not_live = candidate()
        not_live["game_started"] = False
        self.assertEqual(
            review_candidate({}, not_live, TODAY, row_date, thresholds=THRESHOLDS)["reason"],
            "campaign_live_only",
        )
        missing_score = candidate()
        missing_score["has_live_score_context"] = False
        self.assertEqual(
            review_candidate({}, missing_score, TODAY, row_date, thresholds=THRESHOLDS)["reason"],
            "campaign_live_score_required",
        )
        completed = candidate()
        completed["game_completed"] = True
        self.assertEqual(
            review_candidate({}, completed, TODAY, row_date, thresholds=THRESHOLDS)["reason"],
            "campaign_game_completed",
        )
        total = candidate()
        total["market_type"] = "total"
        self.assertEqual(
            review_candidate({}, total, TODAY, row_date, thresholds=THRESHOLDS)["reason"],
            "campaign_market_type",
        )
        self.assertEqual(
            review_candidate({}, candidate(price=61), TODAY, row_date, thresholds=THRESHOLDS)["reason"],
            "campaign_price_range",
        )

    def test_explicit_pregame_candidate_does_not_require_live_score(self):
        pregame = candidate()
        pregame.update({
            "game_started": False,
            "has_live_score_context": False,
            "pregame_eligible": True,
        })
        review = review_candidate(
            {},
            pregame,
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            pregame_enabled=True,
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["reason"], "eligible")

    def test_fixed_unit_stake_does_not_escalate_after_a_loss(self):
        portfolio = {
            "balance": 100,
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -20,
                "strategy_owner": "live_campaign",
                "placed_at": TODAY,
                "settled_at": TODAY,
                "live_campaign": {"active": True, "role": "primary", "campaign_date": TODAY},
            }],
            "bets": [],
        }
        review = review_candidate(
            portfolio,
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            max_open=6,
            daily_loss_cap=105,
            fixed_stake=15,
            fixed_stake_metadata={"additional_units": 1, "unit_size": 15},
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["recovery_mode"], "unit_flat")
        self.assertEqual(review["sizing_mode"], "confidence_units")
        self.assertEqual(review["applied_stake"], 15)
        self.assertGreater(review["full_recovery_target_profit"], review["planned_win_profit"])

    def test_spreads_require_strong_or_elite_quality(self):
        qualified_spread = candidate(edge=6, confidence=80, pro=92, final=92)
        qualified_spread["market_type"] = "spread"
        blocked = review_candidate({}, qualified_spread, TODAY, row_date, thresholds=THRESHOLDS)
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["quality_tier"], "qualified")
        self.assertEqual(blocked["reason"], "campaign_spread_requires_strong")

        strong_spread = candidate(edge=8, confidence=84, pro=100, final=95)
        strong_spread["market_type"] = "spread"
        allowed = review_candidate({}, strong_spread, TODAY, row_date, thresholds=THRESHOLDS)
        self.assertTrue(allowed["eligible"])
        self.assertEqual(allowed["quality_tier"], "strong")

    def test_spread_requires_market_specific_conservative_edge(self):
        spread = candidate(edge=8, confidence=90, pro=105, final=96)
        spread.update({
            "market_type": "spread",
            "minutes_since_start": 30,
            "pricing_v2": {"net_conservative_edge_pp": 2.9},
        })
        blocked = review_candidate(
            {}, spread, TODAY, row_date,
            thresholds=THRESHOLDS,
            spread_min_tier="qualified",
            spread_min_conservative_edge=3,
        )
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["reason"], "campaign_spread_conservative_edge")

        spread["pricing_v2"]["net_conservative_edge_pp"] = 3.0
        allowed = review_candidate(
            {}, spread, TODAY, row_date,
            thresholds=THRESHOLDS,
            spread_min_tier="qualified",
            spread_min_conservative_edge=3,
        )
        self.assertTrue(allowed["eligible"])

    def test_mid_live_spread_uses_higher_conservative_edge(self):
        spread = candidate(edge=8, confidence=90, pro=105, final=96)
        spread.update({
            "market_type": "spread",
            "minutes_since_start": 90,
            "pricing_v2": {"net_conservative_edge_pp": 4.9},
        })
        review = review_candidate(
            {}, spread, TODAY, row_date,
            thresholds=THRESHOLDS,
            spread_min_tier="qualified",
            spread_min_conservative_edge=3,
            mid_live_spread_min_conservative_edge=5,
        )
        self.assertFalse(review["eligible"])
        self.assertEqual(review["spread_edge_guard"]["timing_bucket"], "mid_live")

    def test_exact_line_total_is_allowed_with_separate_guards(self):
        total = candidate(edge=6, confidence=82, pro=95, final=92)
        total.update({
            "market_type": "total",
            "selected_team": "Over 8.5",
            "total_side": "over",
            "market_line": 8.5,
            "book_line": 8.5,
            "independent_book_family_count": 2,
            "sharp_book_count": 1,
            "pricing_v2": {
                "net_conservative_edge_pp": 3.2,
                "consensus": {"independent_family_count": 2, "sharp_book_count": 1},
            },
        })
        allowed = review_candidate(
            {}, total, TODAY, row_date,
            thresholds=THRESHOLDS,
            allowed_market_types={"moneyline", "spread", "total"},
            total_min_conservative_edge=3,
            total_min_book_families=2,
            total_min_sharp_books=1,
        )
        self.assertTrue(allowed["eligible"])
        self.assertEqual(allowed["total_market_guard"]["direction"], "over")

        total["book_line"] = 9.5
        blocked = review_candidate(
            {}, total, TODAY, row_date,
            thresholds=THRESHOLDS,
            allowed_market_types={"moneyline", "spread", "total"},
            total_min_conservative_edge=3,
            total_min_book_families=2,
            total_min_sharp_books=1,
        )
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["reason"], "campaign_total_line_mismatch")

    def test_campaign_rejects_recovery_when_full_size_exceeds_remaining_allowance(self):
        portfolio = {
            "history": [
                {
                    "status": "settled",
                    "result": "LOSS",
                    "profit": -120,
                    "strategy_owner": "live_campaign",
                    "placed_at": "first",
                    "settled_at": "first",
                    "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
                },
                {
                    "status": "settled",
                    "result": "LOSS",
                    "profit": -70,
                    "strategy_owner": "live_campaign",
                    "placed_at": "second",
                    "settled_at": "second",
                    "live_campaign": {"role": "primary", "campaign_date": TODAY, "cycle_number": 1},
                },
            ]
        }
        review = review_candidate(portfolio, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_full_size_unavailable")
        self.assertFalse(review["planned_target_met"])

    def test_five_two_dollar_cycles_require_five_separate_wins(self):
        goals = (2.0,) * 5
        history = []
        for cycle in range(1, 6):
            review = review_candidate(
                {"history": history},
                candidate(price=50),
                TODAY,
                row_date,
                thresholds=THRESHOLDS,
                cycle_goals=goals,
                max_open=1,
                partial_recovery_enabled=True,
            )
            self.assertEqual(review["cycle_number"], cycle)
            self.assertEqual(review["target_profit"], 2.0)
            history.append({
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
                "strategy_owner": "live_campaign",
                "placed_at": f"p-{cycle}",
                "settled_at": f"s-{cycle}",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            })
        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=goals,
            max_open=1,
            partial_recovery_enabled=True,
        )
        self.assertTrue(context["complete"])
        self.assertEqual(context["cycle_index"], 5)
        self.assertEqual(context["realized_profit"], 10.0)

    def test_large_win_completes_only_current_cycle(self):
        history = [{
            "status": "settled",
            "result": "WIN",
            "profit": 10.0,
            "strategy_owner": "live_campaign",
            "placed_at": "p-1",
            "settled_at": "s-1",
            "live_campaign": {"role": "primary", "campaign_date": TODAY},
        }]
        context = build_campaign_context(
            {"history": history},
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
        )
        self.assertEqual(context["cycle_number"], 2)
        self.assertEqual(context["cycle_start_profit"], 10.0)
        self.assertEqual(context["target_remaining"], 2.0)

    def test_first_loss_uses_full_cycle_recovery(self):
        history = [{
            "status": "settled",
            "result": "LOSS",
            "profit": -5.0,
            "strategy_owner": "live_campaign",
            "placed_at": "p-1",
            "settled_at": "s-1",
            "live_campaign": {"role": "primary", "campaign_date": TODAY},
        }]
        review = review_candidate(
            {"history": history},
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertEqual(review["cycle_loss_count"], 1)
        self.assertEqual(review["recovery_mode"], "full")
        self.assertEqual(review["target_profit"], 7.0)
        self.assertTrue(review["planned_target_met"])

    def test_two_losses_activate_half_drawdown_recovery(self):
        history = [
            {
                "status": "settled",
                "result": "LOSS",
                "profit": -5.0,
                "strategy_owner": "live_campaign",
                "placed_at": f"p-{index}",
                "settled_at": f"s-{index}",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }
            for index in (1, 2)
        ]
        review = review_candidate(
            {"history": history},
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertTrue(review["partial_recovery_active"])
        self.assertEqual(review["cycle_realized_profit"], -10.0)
        self.assertEqual(review["full_recovery_target_profit"], 12.0)
        self.assertEqual(review["target_profit"], 7.0)
        self.assertEqual(review["recovery_mode"], "partial")
        self.assertTrue(review["planned_target_met"])

    def test_partial_win_stays_in_cycle_and_next_target_is_three_fifty(self):
        history = [
            {
                "status": "settled",
                "result": result,
                "profit": profit,
                "strategy_owner": "live_campaign",
                "placed_at": f"p-{index}",
                "settled_at": f"s-{index}",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }
            for index, (result, profit) in enumerate(
                (("LOSS", -5.0), ("LOSS", -5.0), ("WIN", 7.0)),
                start=1,
            )
        ]
        review = review_candidate(
            {"history": history},
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertEqual(review["cycle_number"], 1)
        self.assertEqual(review["cycle_realized_profit"], -3.0)
        self.assertEqual(review["full_recovery_target_profit"], 5.0)
        self.assertEqual(review["target_profit"], 3.5)
        self.assertEqual(review["recovery_mode"], "partial")

    def test_one_open_bot_position_fills_campaign_slot(self):
        portfolio = {
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "game_key": "other-game",
                "live_campaign": {
                    "role": "primary",
                    "campaign_date": TODAY,
                },
            }]
        }
        review = review_candidate(
            portfolio,
            candidate(),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
        )
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_open_slots_full")

    def test_parallel_bot_lanes_keep_cycles_slots_and_recovery_isolated(self):
        portfolio = {
            "balance": 100.0,
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "bot_number": 1,
                "game_key": "away::home",
                "stake": 2.0,
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "campaign_date": TODAY,
                    "bot_number": 1,
                },
            }],
            "history": [
                {
                    "status": "settled",
                    "result": "WIN",
                    "profit": 2.0,
                    "strategy_owner": "live_campaign",
                    "bot_number": 1,
                    "placed_at": "p-1",
                    "settled_at": "s-1",
                    "live_campaign": {
                        "role": "primary",
                        "campaign_date": TODAY,
                        "bot_number": 1,
                    },
                },
                {
                    "status": "settled",
                    "result": "LOSS",
                    "profit": -5.0,
                    "strategy_owner": "live_campaign",
                    "bot_number": 2,
                    "placed_at": "p-2",
                    "settled_at": "s-2",
                    "live_campaign": {
                        "role": "primary",
                        "campaign_date": TODAY,
                        "bot_number": 2,
                    },
                },
            ],
        }
        bot_one = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
            bot_number=1,
        )
        bot_two = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
            bot_number=2,
        )
        self.assertEqual(bot_one["cycle_number"], 2)
        self.assertEqual(bot_one["bot_live_active_open_count"], 1)
        self.assertEqual(bot_one["bot_daily_open_risk"], 2.14)
        self.assertEqual(bot_two["cycle_number"], 1)
        self.assertEqual(bot_two["cycle_realized_profit"], -5.0)
        self.assertEqual(bot_two["target_remaining"], 7.0)
        self.assertEqual(bot_two["bot_live_active_open_count"], 0)
        self.assertEqual(bot_two["bot_daily_open_risk"], 0.0)

        review = review_candidate(
            portfolio,
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
            bot_number=2,
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["bot_number"], 2)
        self.assertEqual(review["target_profit"], 7.0)

    def test_assumed_loss_open_position_releases_slot_and_enters_cycle_drawdown(self):
        portfolio = {
            "balance": 100.0,
            "bets": [{
                "status": "open",
                "mode": "live",
                "strategy_owner": "live_campaign",
                "placed_at": "2026-07-18T12:00:00-05:00",
                "stake": 5.0,
                "fee": 0.1,
                "entry_price": 50,
                "game_key": "first::game",
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "campaign_date": TODAY,
                    "assumed_loss": True,
                    "assumed_loss_at": "2026-07-18T12:30:00-05:00",
                    "assumed_loss_amount": 5.1,
                },
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
        )
        self.assertEqual(context["bot_live_open_count"], 1)
        self.assertEqual(context["bot_live_active_open_count"], 0)
        self.assertEqual(context["assumed_loss_open_count"], 1)
        self.assertEqual(context["cycle_realized_profit"], -5.1)
        self.assertEqual(context["cycle_loss_count"], 1)
        self.assertEqual(context["target_remaining"], 7.1)
        self.assertEqual(context["bot_daily_open_risk"], 5.1)

        review = review_candidate(
            portfolio,
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["target_profit"], 7.1)
        self.assertEqual(review["recovery_mode"], "full")

    def test_assumed_loss_that_later_wins_counts_actual_profit(self):
        portfolio = {
            "balance": 105.0,
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 5.0,
                "strategy_owner": "live_campaign",
                "placed_at": "2026-07-18T12:00:00-05:00",
                "settled_at": "2026-07-18T13:00:00-05:00",
                "live_campaign": {
                    "active": True,
                    "role": "primary",
                    "campaign_date": TODAY,
                    "assumed_loss": True,
                    "assumed_loss_at": "2026-07-18T12:30:00-05:00",
                    "assumed_loss_amount": 5.1,
                },
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            partial_recovery_enabled=True,
        )
        self.assertEqual(context["assumed_loss_open_count"], 0)
        self.assertEqual(context["realized_profit"], 5.0)
        self.assertEqual(context["ledger_profit"], 5.0)
        self.assertEqual(context["cycle_number"], 2)
        self.assertEqual(context["cycle_realized_profit"], 0.0)

    def test_two_assumed_losses_release_slot_and_activate_partial_recovery(self):
        portfolio = {
            "balance": 100.0,
            "bets": [
                {
                    "status": "open",
                    "mode": "live",
                    "strategy_owner": "live_campaign",
                    "placed_at": f"2026-07-18T12:0{index}:00-05:00",
                    "stake": 5.0,
                    "fee": 0.1,
                    "entry_price": 50,
                    "game_key": f"game::{index}",
                    "live_campaign": {
                        "active": True,
                        "role": "primary",
                        "campaign_date": TODAY,
                        "assumed_loss": True,
                        "assumed_loss_at": f"2026-07-18T12:3{index}:00-05:00",
                        "assumed_loss_amount": 5.1,
                    },
                }
                for index in (1, 2)
            ],
        }
        review = review_candidate(
            portfolio,
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            daily_loss_cap=75,
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertTrue(review["eligible"])
        self.assertEqual(review["bot_live_open_count"], 2)
        self.assertEqual(review["bot_live_active_open_count"], 0)
        self.assertEqual(review["cycle_loss_count"], 2)
        self.assertEqual(review["cycle_realized_profit"], -10.2)
        self.assertTrue(review["partial_recovery_active"])
        self.assertEqual(review["target_profit"], 7.1)
        self.assertEqual(review["recovery_mode"], "partial")

    def test_assumed_loss_positions_still_consume_daily_loss_allowance(self):
        portfolio = {
            "balance": 20.0,
            "bets": [
                {
                    "status": "open",
                    "mode": "live",
                    "strategy_owner": "live_campaign",
                    "placed_at": f"2026-07-18T12:0{index}:00-05:00",
                    "stake": 40.0,
                    "fee": 0.0,
                    "entry_price": 50,
                    "game_key": f"game::{index}",
                    "live_campaign": {
                        "active": True,
                        "role": "primary",
                        "campaign_date": TODAY,
                        "assumed_loss": True,
                        "assumed_loss_at": f"2026-07-18T12:3{index}:00-05:00",
                        "assumed_loss_amount": 40.0,
                    },
                }
                for index in (1, 2)
            ],
        }
        review = review_candidate(
            portfolio,
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            daily_loss_cap=75,
            daily_loss_cap_pct=0,
            partial_recovery_enabled=True,
        )
        self.assertEqual(review["bot_live_active_open_count"], 0)
        self.assertEqual(review["bot_daily_open_risk"], 80.0)
        self.assertEqual(review["daily_loss_remaining"], 0.0)
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_daily_loss_cap")

    def test_daily_loss_cap_is_ten_percent_of_starting_day_bankroll_with_75_ceiling(self):
        portfolio = {
            "balance": 727.31,
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -10.0,
                "strategy_owner": "live_campaign",
                "placed_at": "p-1",
                "settled_at": "s-1",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }],
        }
        context = build_campaign_context(
            portfolio,
            TODAY,
            row_date,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            daily_loss_cap=75,
            daily_loss_cap_pct=0.10,
        )
        self.assertEqual(context["daily_loss_bankroll_base"], 737.31)
        self.assertEqual(context["percentage_daily_loss_cap"], 73.73)
        self.assertEqual(context["daily_loss_cap"], 73.73)
        self.assertEqual(context["daily_loss_remaining"], 63.73)
        self.assertEqual(context["daily_loss_cap_source"], "minimum_of_configured_and_bankroll_pct")

    def test_recovery_waits_when_complete_stake_exceeds_fifty_dollars(self):
        history = [
            {
                "status": "settled",
                "result": "LOSS",
                "profit": -30.0,
                "strategy_owner": "live_campaign",
                "placed_at": f"p-{index}",
                "settled_at": f"s-{index}",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }
            for index in (1, 2)
        ]
        review = review_candidate(
            {"balance": 737.31, "history": history},
            candidate(price=60),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=1,
            daily_loss_cap=75,
            daily_loss_cap_pct=0.10,
            first_loss_caps={"qualified": 50, "strong": 50, "elite": 50},
            later_loss_caps={"qualified": 50, "strong": 50, "elite": 50},
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )
        self.assertEqual(review["target_profit"], 32.0)
        self.assertGreater(review["raw_recovery_stake"], 50)
        self.assertEqual(review["max_single_stake"], 50)
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_full_size_unavailable")
        self.assertFalse(review["planned_target_met"])

    def test_zero_recovery_caps_mean_unlimited_not_zero_stake(self):
        history = [
            {
                "status": "settled",
                "result": "LOSS",
                "profit": -30.0,
                "strategy_owner": "live_campaign",
                "placed_at": f"p-{index}",
                "settled_at": f"s-{index}",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }
            for index in (1, 2)
        ]
        review = review_candidate(
            {"balance": 737.31, "history": history},
            candidate(price=60),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(2.0,) * 5,
            max_open=7,
            daily_loss_cap=0,
            daily_loss_cap_pct=0,
            first_loss_caps={"qualified": 0, "strong": 0, "elite": 0},
            later_loss_caps={"qualified": 0, "strong": 0, "elite": 0},
            partial_recovery_enabled=True,
            full_recovery_losses=2,
            partial_recovery_fraction=0.50,
        )

        self.assertTrue(review["eligible"])
        self.assertEqual(review["reason"], "eligible")
        self.assertEqual(review["max_single_stake"], 0)
        self.assertEqual(review["applied_stake"], review["raw_recovery_stake"])

    def test_ten_dollar_goal_is_met_at_every_allowed_price(self):
        for price in range(25, 61):
            with self.subTest(price=price):
                review = review_candidate(
                    {},
                    candidate(price=price, edge=6, confidence=80, pro=92, final=92),
                    TODAY,
                    row_date,
                    thresholds=THRESHOLDS,
                    cycle_goals=(10.0, 5.0, 2.5),
                )
                self.assertTrue(review["planned_target_met"])
                self.assertGreaterEqual(
                    contract_win_profit(review["planned_contracts"], price, 0.07),
                    10.0,
                )
                self.assertEqual(
                    review["estimated_fee"],
                    contract_fee(review["planned_contracts"], price, 0.07),
                )

    def test_small_remaining_target_still_plans_one_contract(self):
        portfolio = {
            "history": [{
                "status": "settled",
                "result": "WIN",
                "profit": 9.73,
                "strategy_owner": "live_campaign",
                "placed_at": "now",
                "settled_at": "now",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }]
        }
        review = review_candidate(
            portfolio,
            candidate(price=50),
            TODAY,
            row_date,
            thresholds=THRESHOLDS,
            cycle_goals=(10.0, 5.0, 2.5),
        )
        self.assertEqual(review["target_profit"], 0.27)
        self.assertEqual(review["planned_contracts"], 1)
        self.assertGreaterEqual(review["planned_win_profit"], 0.27)

    def test_daily_loss_cap_blocks_after_three_hundred_net_loss(self):
        portfolio = {
            "history": [{
                "status": "settled",
                "result": "LOSS",
                "profit": -300,
                "strategy_owner": "live_campaign",
                "placed_at": "now",
                "settled_at": "now",
                "live_campaign": {"role": "primary", "campaign_date": TODAY},
            }]
        }
        review = review_candidate(portfolio, candidate(), TODAY, row_date, thresholds=THRESHOLDS)
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "campaign_daily_loss_cap")


if __name__ == "__main__":
    unittest.main()
