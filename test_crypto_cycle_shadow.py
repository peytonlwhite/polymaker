import unittest
from datetime import datetime, timedelta, timezone

from crypto_cycle_shadow import (
    candidate_review,
    configuration,
    edge_discovery_review,
    empty_state,
    normalize_state,
    run_scan,
    synchronized_learned_probability,
)
from crypto_pricing import kalshi_order_fee


class CryptoCycleShadowV6Tests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)
        self.settings = {
            "CRYPTO_CYCLE_SHADOW_ENABLED": "true",
            "CRYPTO_CYCLE_SHADOW_BOT_COUNT": "3",
            "CRYPTO_CYCLE_SHADOW_CYCLE_COUNT": "100",
            "CRYPTO_CYCLE_SHADOW_RESEARCH_GOAL_MARKETS": "100",
            "CRYPTO_CYCLE_SHADOW_SETTLEMENT_GRACE_MINUTES": "0",
            "CRYPTO_CYCLE_SHADOW_SETTLEMENT_RETRY_MINUTES": "0.1",
        }

    def candidate(
        self,
        ticker="KXBTC15M-TEST",
        *,
        asset="BTC",
        side="yes",
        minutes=11.0,
        price=40.0,
        close_time=None,
    ):
        close_time = close_time or (self.now + timedelta(minutes=minutes)).isoformat()
        return {
            "asset": asset,
            "ticker": ticker,
            "event_ticker": ticker,
            "series_ticker": f"KX{asset}15M",
            "market_lane": "crypto_15m",
            "is_15m_market": True,
            "side": side,
            "close_time": close_time,
            "minutes_to_close": minutes,
            "entry_price": price,
            "exact_fee_cents": 0.0,
            "expected_slippage_cents": 0.0,
            "fee_schedule_exact": True,
            "fee_schedule": {
                "fee_type": "quadratic_with_maker_fees",
                "fee_multiplier": 1.0,
                "authoritative": True,
                "source": "test",
                "series_ticker": f"KX{asset}15M",
            },
            "selected_side_probability": 45.0,
            "selected_side_probability_low": 41.0,
            "selected_side_probability_high": 49.0,
            "probability": {
                "p_yes": 45.0,
                "market_implied_yes": 40.0,
                "heuristic_prob_before_ml": 46.0,
                "target_distance_sigma": 0.60,
                "settlement_source": "CF Benchmarks",
            },
            "learned_15m_model": {
                "prob_yes": 47.0,
                "calibration_error_reserve_pp": 2.5,
            },
            "data_quality": {"score": 0.98},
            "flow_review": {
                "direction": "no",
                "strength": 0.20,
                "source_scores": {
                    "coinbase_ws": -0.20,
                    "kraken_ws_v2": -0.15,
                    "kalshi_trades": 0.0,
                },
            },
            "kalshi_microstructure": {
                "sequence_valid": True,
                "age_seconds": 0.1,
                "spread_yes_cents": 2.0,
                "best_yes_bid_cents": price - 2.0,
                "best_no_bid_cents": 100.0 - price,
                "last_trade_price_cents": price,
                "last_trade_ts": (self.now - timedelta(seconds=2)).isoformat(),
            },
            "execution_tier_pricing": {
                "available": True,
                "book_consistent": True,
                "total_available_contracts": 1000,
                "expected_adverse_selection_cents": 0.0,
            },
        }

    def late_candidate(self, ticker="KXBTC15M-LATE", **kwargs):
        kwargs.setdefault("minutes", 2.0)
        kwargs.setdefault("price", 25.0)
        return self.candidate(ticker, **kwargs)

    def finalized(self, result, when=None):
        return {
            "status": "finalized",
            "result": result,
            "settlement_ts": (when or self.now).isoformat(),
        }

    def run_shadow(self, state, candidates, *, result=None, now=None):
        current = now or self.now
        fetch = lambda _ticker: self.finalized(result, current) if result else {"status": "open"}
        return run_scan(state, candidates, fetch, settings=self.settings, now=current)

    def test_closed_market_does_not_settle_shadow_record(self):
        state = empty_state(self.settings, now=self.now)
        state, _summary, added, _settled, _cycles = self.run_shadow(
            state, [self.candidate()]
        )
        self.assertEqual(len(added), 1)
        state, _summary, _added, settled, _cycles = run_scan(
            state,
            [],
            lambda _ticker: {"status": "closed", "result": "yes"},
            settings=self.settings,
            now=self.now + timedelta(minutes=20),
        )
        self.assertEqual(settled, [])
        self.assertEqual(state["records"][0]["status"], "open")

    def test_v6_configuration_has_bounded_recovery_and_two_lanes(self):
        config = configuration(self.settings)
        self.assertEqual(config["early_minimum_minutes"], 10.0)
        self.assertEqual(config["early_maximum_minutes"], 13.0)
        self.assertEqual(config["early_minimum_price_cents"], 35.0)
        self.assertEqual(config["early_maximum_price_cents"], 44.0)
        self.assertEqual(config["early_minimum_opposing_sources"], 2)
        self.assertEqual(config["late_minimum_minutes"], 1.75)
        self.assertEqual(config["late_maximum_minutes"], 2.5)
        self.assertEqual(config["cycle_goal_dollars"], 0.25)
        self.assertEqual(config["max_attempts"], 4)
        self.assertEqual(config["max_contracts"], 3)
        self.assertEqual(config["max_attempt_cost_dollars"], 1.5)
        self.assertEqual(config["max_cycle_loss_dollars"], 3.0)
        self.assertNotIn("XRP", config["allowed_assets"])

    def test_positive_edge_discovery_configuration_is_bounded_and_shadow_only(self):
        config = configuration(self.settings)
        self.assertTrue(config["edge_discovery_enabled"])
        self.assertEqual(config["edge_discovery_minimum_minutes"], 2.0)
        self.assertEqual(config["edge_discovery_maximum_minutes"], 13.0)
        self.assertEqual(config["edge_discovery_minimum_raw_edge_cents"], 1.0)
        self.assertEqual(config["edge_discovery_minimum_raw_edge_low_cents"], -1.5)
        self.assertEqual(config["edge_discovery_maximum_raw_market_gap_pp"], 6.0)
        self.assertEqual(
            config["edge_discovery_maximum_residual_correction_pp"], 5.0
        )
        self.assertEqual(config["edge_discovery_minimum_target_sigma"], -0.5)
        summary = run_scan(
            empty_state(self.settings, now=self.now),
            [],
            lambda _ticker: {"status": "open"},
            settings=self.settings,
            now=self.now,
        )[1]["positive_edge_discovery"]
        self.assertFalse(summary["affects_execution"])
        self.assertFalse(summary["automatic_promotion"])

    def discovery_candidate(self, ticker="KXBTC15M-DISCOVERY", **kwargs):
        candidate = self.candidate(ticker, **kwargs)
        candidate["selected_side_probability"] = 41.0
        candidate["selected_side_probability_low"] = 38.0
        candidate["probability"]["p_yes"] = 41.0
        candidate["probability"]["heuristic_prob_before_ml"] = 46.0
        candidate["learned_15m_model"].update({
            "prob_yes": 46.0,
            "calibration_error_reserve_pp": 2.5,
            "prediction_market_probability_yes": 40.0,
        })
        candidate["flow_review"].update({
            "direction": "yes",
            "strength": 0.20,
            "source_scores": {
                "coinbase_ws": 0.20,
                "kraken_ws_v2": 0.0,
                "kalshi_trades": 0.0,
            },
        })
        return candidate

    def test_positive_edge_discovery_decomposes_raw_edge_from_market_guardrail(self):
        review = edge_discovery_review(
            self.discovery_candidate(), configuration(self.settings)
        )
        self.assertTrue(review["eligible"], review["reasons"])
        decomposition = review["decomposition"]
        self.assertGreater(decomposition["raw_model_edge_cents"], 2.0)
        self.assertGreater(decomposition["raw_model_edge_low_cents"], 0.0)
        self.assertLess(decomposition["market_anchored_edge_cents"], 0.0)
        self.assertLess(decomposition["market_guardrail_adjustment_pp"], 0.0)
        self.assertEqual(decomposition["aligned_sources"], ["coinbase_ws"])
        self.assertEqual(
            decomposition["prediction_synchronization_status"],
            "scan_snapshot_synchronized",
        )

    def test_live_refresh_reanchors_learned_residual_instead_of_creating_false_edge(self):
        candidate = self.discovery_candidate("KXBTC15M-SYNC", price=10.0)
        candidate["probability"]["market_implied_yes"] = 10.0
        candidate["learned_15m_model"].update({
            "prob_yes": 46.0,
            "prediction_market_probability_yes": 40.0,
        })
        candidate["live_underlying_refresh"] = {"enabled": True, "ok": True}
        synchronized = synchronized_learned_probability(candidate)
        self.assertTrue(synchronized["available"])
        self.assertEqual(synchronized["probability_yes"], 15.0)
        self.assertEqual(synchronized["learned_residual_pp"], 6.0)
        self.assertEqual(synchronized["applied_residual_pp"], 5.0)
        self.assertTrue(synchronized["residual_clipped"])
        self.assertEqual(synchronized["market_move_pp"], -30.0)
        self.assertEqual(
            synchronized["status"],
            "market_residual_reanchored_after_live_refresh",
        )

    def test_live_refresh_without_prediction_anchor_fails_discovery_closed(self):
        candidate = self.discovery_candidate("KXBTC15M-NO-ANCHOR")
        candidate["learned_15m_model"].pop(
            "prediction_market_probability_yes", None
        )
        candidate["live_underlying_refresh"] = {"enabled": True, "ok": True}
        review = edge_discovery_review(candidate, configuration(self.settings))
        self.assertFalse(review["eligible"])
        self.assertIn(
            "independent_model_synchronization_unavailable",
            review["reasons"],
        )

    def test_positive_edge_discovery_bounds_disagreement_and_keeps_flow_gates(self):
        config = configuration(self.settings)
        candidate = self.discovery_candidate("KXBTC15M-GAP")
        candidate["learned_15m_model"]["prob_yes"] = 50.0
        bounded = edge_discovery_review(candidate, config)
        self.assertTrue(bounded["eligible"], bounded["reasons"])
        self.assertEqual(bounded["decomposition"]["raw_market_gap_pp"], 5.0)
        self.assertTrue(
            bounded["decomposition"]["market_residual_clipped"]
        )
        candidate = self.discovery_candidate("KXBTC15M-OPPOSE")
        candidate["flow_review"]["source_scores"] = {
            "coinbase_ws": -0.20,
            "kraken_ws_v2": -0.15,
        }
        reasons = edge_discovery_review(candidate, config)["reasons"]
        self.assertIn("directional_alignment", reasons)
        self.assertIn("directional_opposition", reasons)
        candidate = self.discovery_candidate("KXBTC15M-LATE", minutes=1.0)
        self.assertIn(
            "discovery_time_window",
            edge_discovery_review(candidate, config)["reasons"],
        )

    def test_discovery_captures_only_best_candidate_per_expiry_without_live_effect(self):
        lower = self.discovery_candidate("KXBTC15M-DISCOVERY-LOW", minutes=5.0)
        higher = self.discovery_candidate(
            "KXETH15M-DISCOVERY-HIGH", asset="ETH", minutes=5.0
        )
        higher["probability"]["market_implied_yes"] = 41.0
        higher["learned_15m_model"].update({
            "prob_yes": 47.0,
            "prediction_market_probability_yes": 41.0,
        })
        state, summary, added, _settled, _cycles = self.run_shadow(
            empty_state(self.settings, now=self.now), [lower, higher]
        )
        self.assertEqual(added, [])
        self.assertEqual(len(state["edge_discovery_records"]), 1)
        self.assertEqual(
            state["edge_discovery_records"][0]["ticker"],
            "KXETH15M-DISCOVERY-HIGH",
        )
        self.assertFalse(state["edge_discovery_records"][0]["affects_execution"])
        self.assertEqual(
            state["edge_discovery_records"][0]["strategy"],
            "positive_edge_discovery_v3_bounded_residual",
        )
        self.assertEqual(summary["positive_edge_discovery"]["captured_this_scan"], 1)
        closest = summary["positive_edge_discovery"]["candidate_funnel"][
            "best_reviewed"
        ][0]
        self.assertEqual(closest["ticker"], "KXETH15M-DISCOVERY-HIGH")
        self.assertTrue(closest["eligible"])

    def test_discovery_settles_taker_forecasts_and_tracks_brier_separately(self):
        candidate = self.discovery_candidate(
            "KXBTC15M-DISCOVERY-SETTLE",
            minutes=5.0,
            close_time=self.now.isoformat(),
        )
        state, _summary, _added, _settled, _cycles = self.run_shadow(
            empty_state(self.settings, now=self.now), [candidate]
        )
        state, summary, _added, _settled, _cycles = self.run_shadow(
            state, [], result="yes", now=self.now + timedelta(minutes=1)
        )
        record = state["edge_discovery_records"][0]
        self.assertEqual(record["status"], "settled")
        self.assertGreater(record["raw_model_taker_profit"], 0)
        self.assertNotEqual(record["raw_model_brier"], record["market_anchored_brier"])
        discovery = summary["positive_edge_discovery"]
        self.assertEqual(discovery["settled"], 1)
        self.assertEqual(discovery["settled_this_scan"], 1)

    def test_discovery_maker_requires_a_later_numeric_timestamp_trade(self):
        candidate = self.discovery_candidate("KXBTC15M-DISCOVERY-MAKER", minutes=5.0)
        proposed_epoch = self.now.timestamp()
        candidate["kalshi_microstructure"].update({
            "spread_yes_cents": 3.0,
            "best_yes_bid_cents": 37.0,
            "last_trade_ts": proposed_epoch - 1.0,
        })
        state, _summary, _added, _settled, _cycles = self.run_shadow(
            empty_state(self.settings, now=self.now), [candidate]
        )
        update = self.discovery_candidate("KXBTC15M-DISCOVERY-MAKER", minutes=4.0)
        update["kalshi_microstructure"].update({
            "last_trade_price_cents": 38.0,
            "last_trade_ts": proposed_epoch + 1.0,
        })
        state, _summary, _added, _settled, _cycles = self.run_shadow(
            state, [update], now=self.now + timedelta(minutes=1)
        )
        maker = state["edge_discovery_records"][0]["maker_counterfactual"]
        self.assertEqual(maker["status"], "filled")
        self.assertFalse(maker["queue_position_modeled"])

    def test_early_lane_requires_model_advantage_and_two_opposing_sources(self):
        candidate = self.candidate()
        review = candidate_review(candidate, configuration(self.settings))
        self.assertTrue(review["eligible"])
        self.assertEqual(review["lane"], "early_flow_fade")
        self.assertEqual(len(review["observed"]["opposing_sources"]), 2)
        candidate["flow_review"]["source_scores"]["kraken_ws_v2"] = 0.0
        review = candidate_review(candidate, configuration(self.settings))
        self.assertFalse(review["eligible"])
        self.assertIn("early_flow_opposition", review["early_reasons"])
        candidate = self.candidate("KXBTC15M-NOADV")
        candidate["selected_side_probability"] = 40.1
        review = candidate_review(candidate, configuration(self.settings))
        self.assertFalse(review["eligible"])
        self.assertIn("early_model_advantage", review["early_reasons"])

    def test_late_lane_does_not_require_flow_alignment(self):
        candidate = self.late_candidate()
        candidate["flow_review"]["source_scores"] = {
            "coinbase_ws": 0.30,
            "kraken_ws_v2": 0.25,
        }
        review = candidate_review(candidate, configuration(self.settings))
        self.assertTrue(review["eligible"])
        self.assertEqual(review["lane"], "late_convex")

    def test_no_side_uses_complemented_yes_spread(self):
        candidate = self.candidate("KXBTC15M-NO", side="no")
        candidate["selected_side_probability"] = 65.0
        candidate["flow_review"]["source_scores"] = {
            "coinbase_ws": 0.20,
            "kraken_ws_v2": 0.15,
        }
        review = candidate_review(candidate, configuration(self.settings))
        self.assertTrue(review["eligible"])
        self.assertEqual(review["observed"]["spread_cents"], 2.0)

    def test_xrp_is_disabled_and_shadow_cannot_affect_live(self):
        candidate = self.candidate("KXXRP15M-TEST", asset="XRP")
        review = candidate_review(candidate, configuration(self.settings))
        self.assertFalse(review["eligible"])
        self.assertIn("xrp_disabled", review["reasons"])
        state, summary, added, _settled, _cycles = self.run_shadow(
            empty_state(self.settings, now=self.now), [self.candidate()]
        )
        self.assertEqual(len(added), 1)
        self.assertFalse(state["affects_execution"])
        self.assertFalse(summary["affects_execution"])
        self.assertFalse(added[0]["affects_execution"])

    def test_exact_fee_sequence_depth_and_settlement_mapping_are_hard_gates(self):
        config = configuration(self.settings)
        fields = [
            ("fee_schedule_exact", False, "exact_fee_unavailable"),
            ("probability.settlement_source", "", "settlement_mapping_unavailable"),
            ("kalshi_microstructure.sequence_valid", False, "orderbook_sequence_invalid"),
            ("execution_tier_pricing.available", False, "executable_depth_unavailable"),
        ]
        for index, (path, value, reason) in enumerate(fields):
            candidate = self.candidate(f"KXBTC15M-GATE-{index}")
            target = candidate
            parts = path.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            self.assertIn(reason, candidate_review(candidate, config)["reasons"])

    def test_only_one_global_position_per_expiry_and_best_early_candidate_wins(self):
        lower = self.candidate("KXBTC15M-LOW")
        lower["selected_side_probability"] = 42.0
        higher = self.candidate("KXETH15M-HIGH", asset="ETH")
        higher["selected_side_probability"] = 48.0
        _state, summary, added, _settled, _cycles = self.run_shadow(
            empty_state(self.settings, now=self.now), [lower, higher]
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["ticker"], "KXETH15M-HIGH")
        self.assertEqual(summary["open"], 1)

    def test_three_bots_can_take_three_distinct_expiries(self):
        candidates = []
        for index, asset in enumerate(("BTC", "ETH", "SOL")):
            candidate = self.candidate(
                f"KX{asset}15M-{index}",
                asset=asset,
                close_time=(self.now + timedelta(minutes=11 + index)).isoformat(),
            )
            candidates.append(candidate)
        _state, summary, added, _settled, _cycles = self.run_shadow(
            empty_state(self.settings, now=self.now), candidates
        )
        self.assertEqual({row["bot_number"] for row in added}, {1, 2, 3})
        self.assertEqual(summary["open"], 3)

    def test_deficit_target_recovery_sizes_up_and_win_closes_cycle(self):
        state = empty_state(self.settings, now=self.now)
        loss = self.candidate("KXBTC15M-LOSS", close_time=self.now.isoformat())
        state, _summary, first, _settled, _cycles = self.run_shadow(state, [loss])
        self.assertEqual(first[0]["contracts"], 1)
        state, _summary, _added, settled, cycles = self.run_shadow(
            state, [], result="no", now=self.now + timedelta(minutes=1)
        )
        self.assertEqual(len(settled), 1)
        self.assertEqual(cycles, [])
        recovery_time = self.now + timedelta(minutes=2)
        recovery = self.candidate(
            "KXBTC15M-RECOVER",
            close_time=recovery_time.isoformat(),
        )
        state, _summary, added, _settled, _cycles = self.run_shadow(
            state, [recovery], now=recovery_time
        )
        self.assertEqual(added[0]["attempt_type"], "bounded_recovery")
        self.assertEqual(added[0]["contracts"], 2)
        _state, summary, _added, _settled, cycles = self.run_shadow(
            state, [], result="yes", now=recovery_time + timedelta(minutes=1)
        )
        self.assertEqual(cycles[0]["outcome"], "win")
        self.assertGreaterEqual(cycles[0]["profit"], 0.25)
        self.assertEqual(summary["cycles_won"], 1)

    def test_four_losses_close_cycle_without_unbounded_stake(self):
        state = empty_state(self.settings, now=self.now)
        state["bots"][1]["status"] = "complete"
        state["bots"][2]["status"] = "complete"
        cycles = []
        for index in range(4):
            current = self.now + timedelta(minutes=index * 3)
            candidate = self.late_candidate(
                f"KXBTC15M-L{index}",
                price=10.0,
                close_time=current.isoformat(),
            )
            state, _summary, added, _settled, _cycles = self.run_shadow(state, [candidate], now=current)
            self.assertEqual(added[0]["contracts"], 1)
            self.assertLessEqual(added[0]["total_cost_dollars"], 1.5)
            state, _summary, _added, _settled, cycles = self.run_shadow(
                state, [], result="no", now=current + timedelta(minutes=1)
            )
        self.assertEqual(cycles[0]["reason"], "maximum_attempts_reached")
        self.assertEqual(cycles[0]["attempts"], 4)
        self.assertEqual(state["bots"][0]["cycle_number"], 2)

    def test_infeasible_recovery_closes_loss_and_reuses_candidate_as_fresh_base(self):
        state = empty_state(self.settings, now=self.now)
        bot = state["bots"][0]
        bot["cycle_profit"] = -2.0
        bot["attempts"] = 2
        candidate = self.candidate()
        state, _summary, added, _settled, cycles = self.run_shadow(state, [candidate])
        self.assertEqual(cycles[0]["reason"], "recovery_contract_cap")
        self.assertEqual(added[0]["cycle_number"], 2)
        self.assertEqual(added[0]["attempt"], 1)
        self.assertEqual(added[0]["contracts"], 1)

    def test_recovery_walks_executable_depth_and_records_vwap(self):
        state = empty_state(self.settings, now=self.now)
        state["bots"][0]["cycle_profit"] = -0.70
        state["bots"][0]["attempts"] = 1
        candidate = self.candidate("KXBTC15M-WALK")
        candidate["kalshi_microstructure"]["no_levels"] = [[60.0, 1.0], [55.0, 5.0]]
        state, _summary, added, _settled, _cycles = self.run_shadow(state, [candidate])
        self.assertEqual(added[0]["contracts"], 2)
        self.assertEqual(added[0]["vwap_entry_price_cents"], 42.5)
        self.assertEqual(added[0]["marginal_entry_price_cents"], 45.0)
        self.assertEqual([row["price_cents"] for row in added[0]["fills"]], [40.0, 45.0])

    def test_flat_counterfactual_is_same_candidate_one_contract(self):
        state = empty_state(self.settings, now=self.now)
        candidate = self.candidate("KXBTC15M-FLAT", close_time=self.now.isoformat())
        candidate["exact_fee_cents"] = 1.0
        candidate["expected_slippage_cents"] = 1.0
        state, _summary, added, _settled, _cycles = self.run_shadow(state, [candidate])
        self.assertEqual(added[0]["flat_counterfactual"]["contracts"], 1)
        expected_fee = kalshi_order_fee(40, 1, schedule=candidate["fee_schedule"])["fee_dollars"]
        self.assertAlmostEqual(
            added[0]["flat_counterfactual"]["total_cost_dollars"],
            0.41 + expected_fee,
            places=4,
        )
        _state, summary, _added, _settled, _cycles = self.run_shadow(
            state, [], result="no", now=self.now + timedelta(minutes=1)
        )
        self.assertFalse(summary["flat_stake_counterfactual"]["affects_execution"])
        self.assertEqual(summary["flat_stake_counterfactual"]["settled"], 1)

    def test_maker_counterfactual_requires_subsequent_trade_then_compares_settlement(self):
        state = empty_state(self.settings, now=self.now)
        candidate = self.candidate("KXBTC15M-MAKER")
        candidate["kalshi_microstructure"].update({
            "spread_yes_cents": 3.0,
            "best_yes_bid_cents": 37.0,
        })
        state, _summary, added, _settled, _cycles = self.run_shadow(state, [candidate])
        self.assertEqual(added[0]["maker_counterfactual"]["status"], "pending")
        self.assertEqual(added[0]["maker_counterfactual"]["price_cents"], 38.0)
        update = self.candidate("KXBTC15M-MAKER", minutes=9.5)
        update["kalshi_microstructure"].update({
            "last_trade_price_cents": 38.0,
            "last_trade_ts": (self.now + timedelta(minutes=1)).isoformat(),
        })
        state, _summary, _added, _settled, _cycles = self.run_shadow(
            state, [update], now=self.now + timedelta(minutes=1)
        )
        maker = state["records"][0]["maker_counterfactual"]
        self.assertEqual(maker["status"], "filled")
        close = datetime.fromisoformat(state["records"][0]["close_time"])
        _state, summary, _added, settled, _cycles = self.run_shadow(
            state, [], result="yes", now=close + timedelta(minutes=1)
        )
        self.assertGreater(settled[0]["maker_counterfactual"]["incremental_profit_vs_taker"], 0)
        self.assertEqual(summary["maker_taker_counterfactual"]["filled"], 1)
        self.assertFalse(summary["maker_taker_counterfactual"]["queue_position_modeled"])

    def test_unfilled_maker_quote_expires_at_eight_and_a_half_minutes(self):
        state = empty_state(self.settings, now=self.now)
        candidate = self.candidate("KXBTC15M-MAKER-EXPIRE")
        candidate["kalshi_microstructure"]["spread_yes_cents"] = 3.0
        state, _summary, _added, _settled, _cycles = self.run_shadow(state, [candidate])
        state, _summary, _added, _settled, _cycles = self.run_shadow(
            state, [], now=self.now + timedelta(minutes=2.5)
        )
        self.assertEqual(state["records"][0]["maker_counterfactual"]["status"], "expired")

    def test_v5_state_migrates_without_losing_records(self):
        state = empty_state(self.settings, now=self.now)
        state["version"] = "crypto-cycle-shadow-v5"
        state["records"].append({"id": "preserved", "status": "settled"})
        migrated = normalize_state(state, self.settings, now=self.now)
        self.assertEqual(migrated["version"], "crypto-cycle-shadow-v6")
        self.assertEqual(migrated["records"][0]["id"], "preserved")
        self.assertEqual(migrated["edge_discovery_records"], [])
        self.assertFalse(migrated["affects_execution"])


if __name__ == "__main__":
    unittest.main()
