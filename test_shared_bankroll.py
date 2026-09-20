import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import shared_bankroll


class SharedCyclePlanTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(shared_bankroll.DEFAULTS)

    def test_cycle_targets_decay_and_share_one_profit_ledger(self):
        plan = shared_bankroll.shared_cycle_plan(25, 20, self.settings)
        self.assertEqual([row["target"] for row in plan["cycles"]], [20.0, 10.0, 5.0, 2.5])
        self.assertEqual(plan["current_cycle"], 2)
        self.assertEqual(plan["target_remaining"], 5.0)

    def test_loss_does_not_expand_cycle_target(self):
        plan = shared_bankroll.shared_cycle_plan(-15, 20, self.settings)
        self.assertEqual(plan["current_cycle"], 1)
        self.assertEqual(plan["target_remaining"], 20.0)

    def test_all_four_cycles_complete_on_system_profit(self):
        plan = shared_bankroll.shared_cycle_plan(50, 20, self.settings)
        self.assertEqual(plan["status"], "complete")
        self.assertEqual(plan["completed_cycles"], 4)
        self.assertEqual(plan["target_remaining"], 0.0)

    def test_crypto_cap_buffer_adds_after_percentage_cap(self):
        self.assertEqual(
            shared_bankroll.effective_cap_with_buffer(
                cash=3135.15,
                pct=0.20,
                flat_cap=0,
                buffer=300,
            ),
            927.03,
        )

    def test_reconciliation_snapshot_includes_marked_account_equity(self):
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "account": {
                "cash_balance": 0.73,
                "balance_raw": {"portfolio_value": 63806},
            },
        }
        with patch.object(shared_bankroll, "read_json", return_value=payload):
            snapshot = shared_bankroll.reconciliation_cash_snapshot(
                "unused.json", "test"
            )
        self.assertEqual(snapshot["cash"], 0.73)
        self.assertEqual(snapshot["portfolio_value"], 638.06)
        self.assertEqual(snapshot["equity"], 638.79)

    def test_recovery_allocation_uses_quality_instead_of_equal_slices(self):
        context = {"target_profit": 10.0}
        qualified = shared_bankroll.recovery_quality_allocation(
            "crypto", {"edge": 12, "confidence": 80}, context, settings=self.settings
        )
        elite = shared_bankroll.recovery_quality_allocation(
            "crypto", {"edge": 20, "confidence": 90}, context, settings=self.settings
        )
        strong_sports = shared_bankroll.recovery_quality_allocation(
            "sports",
            {
                "edge": 12,
                "confidence_score": 92,
                "pro_review": {"score": 120},
                "final_bet_score": 95,
            },
            context,
            settings=self.settings,
        )
        self.assertEqual(qualified["tier"], "qualified_partial")
        self.assertEqual(qualified["target_profit"], 4.0)
        self.assertEqual(elite["tier"], "elite_full")
        self.assertEqual(elite["target_profit"], 10.0)
        self.assertEqual(strong_sports["tier"], "strong_partial")
        self.assertEqual(strong_sports["target_profit"], 7.0)

    def test_manual_sports_results_do_not_change_strategy_profit(self):
        now = datetime.now().astimezone().isoformat()
        portfolio = {
            "history": [
                {
                    "mode": "live",
                    "status": "settled",
                    "settled_at": now,
                    "strategy_owner": "small_edge",
                    "profit": -5.0,
                },
                {
                    "mode": "live",
                    "status": "settled",
                    "settled_at": now,
                    "strategy_owner": "user_bet",
                    "source": "user_manual",
                    "profit": -200.0,
                },
            ]
        }
        rows = shared_bankroll.settled_rows_today(portfolio, "sports", live_only=True)
        self.assertEqual(shared_bankroll.realized_profit(rows), -5.0)

    def test_manual_crypto_results_do_not_change_strategy_profit(self):
        now = datetime.now().astimezone().isoformat()
        portfolio = {
            "history": [
                {
                    "mode": "live",
                    "status": "settled",
                    "settled_at": now,
                    "strategy_owner": "crypto_15m_campaign",
                    "profit": -5.0,
                },
                {
                    "mode": "live",
                    "status": "settled",
                    "settled_at": now,
                    "strategy_owner": "user_bet",
                    "source": "user_manual",
                    "profit": 200.0,
                },
            ]
        }
        rows = shared_bankroll.settled_rows_today(portfolio, "crypto", live_only=True)
        self.assertEqual(shared_bankroll.realized_profit(rows), -5.0)


class CoordinationTests(unittest.TestCase):
    def review_state(self):
        return {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": 0.0,
            "system_live_daily_loss": 0.0,
            "system_live_daily_loss_cap": 0.0,
            "cycle_coordination": {"enabled": True, "status": "active", "single_owner_enabled": True, "owner": None},
            "recovery_coordination": {
                "single_owner_enabled": False,
                "waits_for_phase_two": False,
                "owner": "sports",
                "phase_two_owner": None,
                "open_slices": 1,
                "max_open_slices": 3,
                "max_cluster_open": 1,
                "open_stake": 5.0,
            },
            "correlation_coordination": {
                "enabled": True,
                "clusters": {"sports:game-a": {"positions": 1, "stake": 5.0}},
            },
            "strategy_caps": {"sports": {}, "crypto": {}},
            "system_live_open_exposure": 5.0,
            "system_live_open_exposure_cap": 0.0,
            "sports_live_open_exposure": 5.0,
            "crypto_live_open_exposure": 0.0,
            "reserved_by_strategy": {},
        }

    def test_coordination_owner_reads_positions_and_reservations(self):
        rows = {
            "sports": [{"strategy_owner": "phase_two_cycle"}],
            "crypto": [],
        }
        self.assertEqual(shared_bankroll.coordination_owner(rows, [], "phase_two"), "sports")
        reservations = [{
            "strategy": "crypto",
            "metadata": {"strategy_owner": "shared_recovery"},
        }]
        self.assertEqual(shared_bankroll.coordination_owner({"sports": [], "crypto": []}, reservations, "recovery"), "crypto")

    def test_liquidity_only_review_uses_all_unreserved_cash(self):
        state = self.review_state()
        state.update({
            "cash": 100.0,
            "account_equity": 500.0,
            "reserved_total": 5.0,
            "system_live_open_exposure": 5.0,
            "system_live_open_exposure_cap": 5.0,
        })
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            review = shared_bankroll.live_order_review(
                "sports",
                100,
                price_cents=50,
                metadata={"liquidity_only": True},
            )
        self.assertTrue(review["ok"])
        self.assertEqual(review["approved_stake"], 95.0)
        self.assertEqual(review["available_cash"], 95.0)

        state["cash"] = 0.2
        state["reserved_total"] = 0.0
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            blocked = shared_bankroll.live_order_review(
                "sports",
                5,
                price_cents=50,
                metadata={"liquidity_only": True},
            )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "shared_cash_below_one_contract")

    def test_crypto_loss_room_ignores_sports_and_user_managed_exposure(self):
        state = {
            "settings": {"SHARED_CRYPTO_ISOLATED_RISK_ENABLED": "true"},
            "system_live_daily_loss": 100.0,
            "system_live_daily_loss_cap": 300.0,
            "system_live_open_exposure": 250.0,
            "sports_live_open_exposure": 200.0,
            "crypto_live_open_exposure": 50.0,
            "crypto_bot_live_open_exposure": 10.0,
            "crypto_user_live_open_exposure": 40.0,
            "crypto_live_daily_loss": 5.0,
            "crypto_live_daily_loss_cap": 30.0,
            "reserved_by_strategy": {},
            "reserved_total": 0.0,
        }
        room = shared_bankroll.shared_loss_room_snapshot(state, strategy="crypto")
        self.assertEqual(room["scope"], "crypto")
        self.assertEqual(room["worst_case_used"], 15.0)
        self.assertEqual(room["remaining"], 15.0)
        self.assertTrue(room["other_strategy_exposure_ignored"])
        self.assertTrue(room["user_managed_exposure_ignored"])

    def test_accidental_sub_two_cent_crypto_bet_does_not_own_phase_two(self):
        settings = {**shared_bankroll.DEFAULTS, "CRYPTO_ANALYTICS_MIN_ENTRY_PRICE_CENTS": "2"}
        accidental = {"strategy_owner": "phase_two_cycle", "entry_price": 1}
        legitimate = {"strategy_owner": "phase_two_cycle", "entry_price": 2}
        self.assertIsNone(
            shared_bankroll.coordination_owner({"sports": [], "crypto": [accidental]}, [], "phase_two", settings=settings)
        )
        self.assertEqual(
            shared_bankroll.coordination_owner({"sports": [], "crypto": [legitimate]}, [], "phase_two", settings=settings),
            "crypto",
        )

    def test_completed_cycle_plan_pauses_phase_two_and_live_core(self):
        state = {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": 50.0,
            "system_live_daily_loss": 0.0,
            "system_live_daily_loss_cap": 0.0,
            "cycle_coordination": {"enabled": True, "status": "complete", "single_owner_enabled": True, "owner": None},
            "recovery_coordination": {"single_owner_enabled": True, "waits_for_phase_two": True, "owner": None, "phase_two_owner": None},
            "strategy_caps": {"sports": {}, "crypto": {}},
            "system_live_open_exposure": 0.0,
            "system_live_open_exposure_cap": 0.0,
            "sports_live_open_exposure": 0.0,
            "crypto_live_open_exposure": 0.0,
            "reserved_by_strategy": {},
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            phase = shared_bankroll.live_order_review("sports", 5, metadata={"strategy_owner": "phase_two_cycle"})
            edge = shared_bankroll.live_order_review("sports", 5, metadata={"strategy_owner": "live_core"})
        self.assertFalse(phase["ok"])
        self.assertEqual(phase["error"], "shared_cycle_plan_complete")
        self.assertFalse(edge["ok"])
        self.assertEqual(edge["error"], "shared_cycle_plan_complete")

    def test_first_profit_checkpoint_does_not_change_or_pause_live_core_stake(self):
        state = {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": 25.0,
            "system_live_daily_loss": 0.0,
            "system_live_daily_loss_cap": 5.0,
            "cycle_coordination": {"enabled": True, "status": "active", "current_cycle": 2, "single_owner_enabled": True, "owner": None},
            "recovery_coordination": {"single_owner_enabled": False, "waits_for_phase_two": False, "owner": None, "phase_two_owner": None},
            "strategy_caps": {"sports": {"max_stake": 10.0}, "crypto": {}},
            "system_live_open_exposure": 0.0,
            "system_live_open_exposure_cap": 10.0,
            "sports_live_open_exposure": 0.0,
            "crypto_live_open_exposure": 0.0,
            "reserved_by_strategy": {},
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            review = shared_bankroll.live_order_review("sports", 5, metadata={"strategy_owner": "live_core"})
        self.assertTrue(review["ok"])
        self.assertEqual(review["approved_stake"], 5.0)

    def test_sports_live_campaign_cannot_bypass_shared_loss_limits(self):
        state = {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": -40.0,
            "system_live_daily_loss": 40.0,
            "system_live_daily_loss_cap": 35.0,
            "cycle_coordination": {"enabled": True, "status": "complete", "single_owner_enabled": True, "owner": None},
            "recovery_coordination": {"single_owner_enabled": False, "waits_for_phase_two": False, "owner": None, "phase_two_owner": None},
            "strategy_caps": {"sports": {"max_stake": 10.0, "max_open_exposure": 10.0}, "crypto": {}},
            "system_live_open_exposure": 75.0,
            "system_live_open_exposure_cap": 75.0,
            "sports_live_open_exposure": 60.0,
            "crypto_live_open_exposure": 15.0,
            "reserved_by_strategy": {},
            "reserved_total": 0.0,
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            campaign = shared_bankroll.live_order_review(
                "sports",
                60,
                metadata={
                    "strategy_owner": "live_campaign",
                    "live_campaign": True,
                    "campaign_max_stake": 70,
                },
            )
            crypto = shared_bankroll.live_order_review(
                "crypto",
                5,
                metadata={
                    "strategy_owner": "crypto_15m_campaign",
                    "crypto_live_campaign": True,
                },
            )
        self.assertFalse(campaign["ok"])
        self.assertEqual(campaign["error"], "shared_worst_case_loss_room_exhausted")
        self.assertEqual(campaign["legacy_error"], "shared_daily_loss_cap_hit")
        self.assertEqual(campaign["loss_room"]["realized_daily_loss"], 40.0)
        self.assertEqual(campaign["loss_room"]["open_exposure"], 75.0)
        self.assertEqual(campaign["loss_room"]["remaining"], 0.0)
        self.assertTrue(crypto["ok"])
        self.assertEqual(crypto["approved_stake"], 5.0)
        self.assertEqual(crypto["loss_room"]["scope"], "crypto")
        self.assertTrue(crypto["loss_room"]["other_strategy_exposure_ignored"])

    def test_crypto_15m_campaign_cannot_bypass_shared_loss_exposure_caps(self):
        state = {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": -40.0,
            "system_live_daily_loss": 40.0,
            "system_live_daily_loss_cap": 35.0,
            "crypto_live_daily_loss": 10.0,
            "crypto_live_daily_loss_cap": 25.0,
            "cycle_coordination": {"enabled": True, "status": "complete", "single_owner_enabled": True, "owner": "sports"},
            "recovery_coordination": {"single_owner_enabled": False, "waits_for_phase_two": False, "owner": None, "phase_two_owner": None},
            "strategy_caps": {"sports": {}, "crypto": {"max_stake": 25.0, "max_open_exposure": 100.0}},
            "system_live_open_exposure_cap": 2.0,
            "system_live_open_exposure": 2.0,
            "crypto_live_open_exposure": 2.0,
            "crypto_bot_live_open_exposure": 15.0,
            "reserved_total": 0.0,
            "reserved_by_strategy": {},
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            review = shared_bankroll.live_order_review(
                "crypto",
                25.0,
                metadata={
                    "strategy_owner": "crypto_15m_campaign",
                    "crypto_live_campaign": True,
                },
            )
        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "shared_worst_case_loss_room_exhausted")
        self.assertEqual(review["loss_room"]["scope"], "crypto")
        self.assertEqual(review["loss_room"]["open_exposure"], 15.0)

    def test_live_campaign_is_trimmed_by_shared_strategy_and_system_caps(self):
        state = {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": -10.0,
            "system_live_daily_loss": 10.0,
            "system_live_daily_loss_cap": 100.0,
            "cycle_coordination": {"enabled": True, "status": "active", "single_owner_enabled": True, "owner": None},
            "recovery_coordination": {"single_owner_enabled": False, "waits_for_phase_two": False, "owner": None, "phase_two_owner": None},
            "strategy_caps": {"sports": {"max_stake": 30.0, "max_open_exposure": 50.0}, "crypto": {}},
            "system_live_open_exposure": 20.0,
            "system_live_open_exposure_cap": 100.0,
            "sports_live_open_exposure": 10.0,
            "crypto_live_open_exposure": 10.0,
            "reserved_by_strategy": {},
            "reserved_total": 0.0,
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            review = shared_bankroll.live_order_review(
                "sports",
                60.0,
                metadata={
                    "strategy_owner": "live_campaign",
                    "live_campaign": True,
                    "campaign_max_stake": 70.0,
                },
            )
        self.assertTrue(review["ok"])
        self.assertEqual(review["approved_stake"], 30.0)

    def test_concurrent_reservations_are_not_lost(self):
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            lock_path = Path(tmp) / "state.lock"
            review = {
                "ok": True,
                "approved_stake": 5.0,
                "requested_stake": 5.0,
            }
            with patch.object(shared_bankroll, "STATE_FILE", state_path), patch.object(
                shared_bankroll, "STATE_LOCK_FILE", lock_path
            ), patch.object(shared_bankroll, "live_order_review", return_value=review):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(
                        lambda strategy: shared_bankroll.reserve_live_order(strategy, 5.0),
                        ("sports", "crypto"),
                    ))
            stored = shared_bankroll.read_json(state_path, {})
        self.assertEqual(len(stored.get("reservations") or []), 2)
        self.assertEqual(len({row["reservation_id"] for row in results}), 2)

    def test_filled_reservation_stays_active_until_portfolio_persisted(self):
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            lock_path = Path(tmp) / "state.lock"
            shared_bankroll.write_json(
                state_path,
                {
                    "reservations": [{
                        "id": "reservation-1",
                        "strategy": "sports",
                        "stake": 10.0,
                        "status": "active",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                    }],
                    "reservation_history": [],
                },
            )
            with patch.object(shared_bankroll, "STATE_FILE", state_path), patch.object(
                shared_bankroll, "STATE_LOCK_FILE", lock_path
            ):
                shared_bankroll.finalize_reservation(
                    "reservation-1",
                    "filled_pending_persist",
                    7.5,
                )
                pending = shared_bankroll.read_json(state_path, {})
                active = shared_bankroll.prune_reservations(pending)
                shared_bankroll.finalize_reservation("reservation-1", "filled", 7.5)
                committed = shared_bankroll.read_json(state_path, {})
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "active")
        self.assertEqual(active[0]["phase"], "filled_pending_persist")
        self.assertEqual(active[0]["stake"], 7.5)
        self.assertEqual(committed.get("reservations"), [])

    def test_other_bot_cannot_duplicate_phase_two_or_recovery(self):
        base_state = {
            "enabled": True,
            "cash": 100.0,
            "daily_target": 20.0,
            "system_live_realized_profit": 0.0,
            "system_live_daily_loss": 0.0,
            "system_live_daily_loss_cap": 0.0,
            "cycle_coordination": {"enabled": True, "status": "active", "single_owner_enabled": True, "owner": "sports"},
            "recovery_coordination": {"single_owner_enabled": True, "waits_for_phase_two": True, "owner": None, "phase_two_owner": "sports"},
            "strategy_caps": {"sports": {}, "crypto": {}},
            "system_live_open_exposure": 0.0,
            "system_live_open_exposure_cap": 0.0,
            "sports_live_open_exposure": 0.0,
            "crypto_live_open_exposure": 0.0,
            "reserved_by_strategy": {},
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=base_state):
            phase = shared_bankroll.live_order_review("crypto", 5, metadata={"strategy_owner": "phase_two_cycle"})
            recovery = shared_bankroll.live_order_review("crypto", 5, metadata={"strategy_owner": "shared_recovery"})
        self.assertEqual(phase["error"], "shared_phase_two_owned_by_other_strategy")
        self.assertEqual(recovery["error"], "shared_phase_two_position_active")

    def test_same_game_markets_share_one_cluster(self):
        moneyline = {"game_key": "NYY-BOS-2026-07-15", "ticker": "NYY-ML"}
        runline = {"game_key": "NYY-BOS-2026-07-15", "ticker": "NYY-RL"}
        other_game = {"game_key": "CHC-STL-2026-07-15", "ticker": "CHC-ML"}
        self.assertEqual(
            shared_bankroll.correlation_cluster_key("sports", moneyline),
            shared_bankroll.correlation_cluster_key("sports", runline),
        )
        self.assertNotEqual(
            shared_bankroll.correlation_cluster_key("sports", moneyline),
            shared_bankroll.correlation_cluster_key("sports", other_game),
        )

    def test_other_bot_can_add_independent_recovery_slice(self):
        state = self.review_state()
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            review = shared_bankroll.live_order_review(
                "crypto",
                5,
                metadata={"strategy_owner": "shared_recovery", "recovery": True, "event_ticker": "KXBTC-NEW"},
            )
        self.assertTrue(review["ok"])
        self.assertEqual(review["approved_stake"], 5.0)

    def test_recovery_slice_cannot_duplicate_open_cluster(self):
        state = self.review_state()
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            review = shared_bankroll.live_order_review(
                "sports",
                5,
                metadata={"strategy_owner": "recovery_basket", "recovery": True, "game_key": "game-a"},
            )
        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "shared_recovery_cluster_already_open")

    def test_recovery_uses_recovery_cap_without_raising_normal_stake_cap(self):
        state = self.review_state()
        state["strategy_caps"]["sports"] = {"max_open_exposure": 50.0, "max_stake": 10.0}
        state["system_live_open_exposure_cap"] = 50.0
        settings = {
            **shared_bankroll.DEFAULTS,
            "SHARED_RECOVERY_MAX_STAKE_PCT": "0.60",
            "SHARED_RECOVERY_MAX_STAKE_CAP": "0",
            "SHARED_RECOVERY_MAX_TOTAL_STAKE_PCT": "0.60",
        }
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            recovery = shared_bankroll.live_order_review(
                "sports",
                80,
                settings=settings,
                metadata={"strategy_owner": "shared_recovery", "recovery": True, "game_key": "game-b"},
            )
            normal = shared_bankroll.live_order_review(
                "sports",
                80,
                settings=settings,
                metadata={"strategy_owner": "edge_scanner", "game_key": "game-b"},
            )
        self.assertTrue(recovery["ok"])
        self.assertEqual(recovery["approved_stake"], 55.0)
        self.assertTrue(normal["ok"])
        self.assertEqual(normal["approved_stake"], 10.0)

    def test_correlated_boost_is_trimmed_but_base_bet_remains(self):
        state = self.review_state()
        with patch.object(shared_bankroll, "build_shared_bankroll_state", return_value=state):
            boosted = shared_bankroll.live_order_review(
                "sports",
                12,
                metadata={"strategy_owner": "selective_edge", "game_key": "game-a", "enhanced_sizing": True, "base_stake": 3},
            )
            ordinary = shared_bankroll.live_order_review(
                "sports",
                3,
                metadata={"strategy_owner": "edge_scanner", "game_key": "game-a", "enhanced_sizing": False, "base_stake": 3},
            )
        self.assertTrue(boosted["ok"])
        self.assertTrue(boosted["correlation_adjusted"])
        self.assertEqual(boosted["approved_stake"], 3.0)
        self.assertTrue(ordinary["ok"])
        self.assertEqual(ordinary["approved_stake"], 3.0)


if __name__ == "__main__":
    unittest.main()
