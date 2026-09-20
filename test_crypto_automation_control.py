import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crypto_automation_control import (
    PROTECTED_RECOVERY_SETTINGS,
    PROTECTED_UNIT_RISK_SETTINGS,
    apply_candidate_stake_multiplier,
    build_health_report,
    candidate_control,
    evaluate_governor,
    is_recovery_candidate,
    price_band,
    strategy_identity,
)
from crypto_rejection_shadow import capture_candidates, empty_ledger


class CryptoAutomationControlTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "CRYPTO_ADAPTIVE_GOVERNOR_ENABLED": "true",
            "CRYPTO_ADAPTIVE_BOOTSTRAP_MIN_MARKETS": "100",
            "CRYPTO_ADAPTIVE_DEMOTION_MIN_MARKETS": "25",
            "CRYPTO_ADAPTIVE_TRANSITION_MIN_MARKETS": "10",
            "CRYPTO_ADAPTIVE_FULL_PROMOTION_MIN_MARKETS": "20",
            "CRYPTO_ADAPTIVE_SHADOW_PROMOTION_MIN_MARKETS": "25",
            "CRYPTO_ADAPTIVE_DRAWDOWN_DOLLARS": "300",
            "CRYPTO_ADAPTIVE_NEGATIVE_ROI_PCT": "-10",
            "CRYPTO_ADAPTIVE_DEMOTION_MAX_PROBABILITY": "0.20",
            "CRYPTO_ADAPTIVE_PROMOTION_MIN_PROBABILITY": "0.90",
            "CRYPTO_ADAPTIVE_DEFICIT_RECOVERY_FRACTION": "0.6666667",
            "CRYPTO_15M_MIN_EDGE": "4",
        }
        values.update(overrides)
        return values

    def test_price_bands_match_live_action_gate(self):
        expected = {
            34.99: "20-34c",
            35: "35-44c",
            44.99: "35-44c",
            45: "45-62c",
            62.99: "45-62c",
            63: "63-70c",
            70: "63-70c",
            71: "71-80c",
        }
        for price, label in expected.items():
            with self.subTest(price=price):
                self.assertEqual(price_band(price), label)

    def test_strategy_identity_excludes_secrets_but_tracks_strategy(self):
        learned = {"feature_schema_version": "schema-v2"}
        first = strategy_identity(
            {"CRYPTO_OPENAI_API_KEY": "secret-a", "CRYPTO_15M_MIN_EDGE": "4"},
            learned,
        )
        second = strategy_identity(
            {"CRYPTO_OPENAI_API_KEY": "secret-b", "CRYPTO_15M_MIN_EDGE": "4"},
            learned,
        )
        changed = strategy_identity(
            {"CRYPTO_OPENAI_API_KEY": "secret-b", "CRYPTO_15M_MIN_EDGE": "5"},
            learned,
        )
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(second["fingerprint"], changed["fingerprint"])

    def _settled_bets(self, settings, count, profit=-5.0):
        learned = {"feature_schema_version": "schema-v2"}
        identity = strategy_identity(settings, learned)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        bets = []
        for index in range(count):
            bets.append(
                {
                    "id": f"bet-{index}",
                    "ticker": f"KXBTC15M-{index}",
                    "status": "settled",
                    "market_lane": "crypto_15m",
                    "side": "yes",
                    "entry_price": 40,
                    "stake": 10,
                    "profit": profit,
                    "settled_at": (now + timedelta(minutes=15 * index)).isoformat(),
                    "strategy_identity": identity,
                    "strategy_owner": "crypto_15m_campaign",
                }
            )
        return learned, identity, bets

    def test_governor_stays_report_only_during_versioned_bootstrap(self):
        settings = self.settings(CRYPTO_ADAPTIVE_BOOTSTRAP_MIN_MARKETS="100")
        learned, _, bets = self._settled_bets(settings, 25)
        state = evaluate_governor(
            settings,
            {"bets": bets},
            learned,
            {"records": []},
            apply=True,
            now=datetime(2026, 8, 14, tzinfo=timezone.utc),
        )
        self.assertEqual(state["execution_mode"], "report_only")
        self.assertFalse(state["bootstrap"]["ready"])
        self.assertEqual(state["lanes"]["35-44c:YES"]["mode"], "live_full")

    def test_negative_lane_demotes_one_step_when_bootstrap_is_ready(self):
        settings = self.settings(CRYPTO_ADAPTIVE_BOOTSTRAP_MIN_MARKETS="1")
        learned, _, bets = self._settled_bets(settings, 25)
        state = evaluate_governor(
            settings,
            {"bets": bets},
            learned,
            {"records": []},
            apply=True,
            now=datetime(2026, 8, 14, tzinfo=timezone.utc),
        )
        lane = state["lanes"]["35-44c:YES"]
        self.assertEqual(state["execution_mode"], "adaptive")
        self.assertEqual(lane["mode"], "live_reduced")
        self.assertEqual(lane["stake_multiplier"], 0.5)
        self.assertEqual(len([row for row in state["history"] if row.get("lane")]), 1)

    def test_shadow_lane_preserves_recovery_candidate(self):
        settings = self.settings()
        identity = strategy_identity(settings, {"feature_schema_version": "schema-v2"})
        state = {
            "strategy_identity": identity,
            "execution_mode": "adaptive",
            "lanes": {"35-44c:YES": {"mode": "shadow", "stake_multiplier": 0}},
        }
        candidate = {
            "market_lane": "crypto_15m",
            "side": "yes",
            "entry_price": 40,
            "strategy_identity": identity,
            "crypto_live_campaign": {
                "cycle_goal": 5,
                "cycle_realized_profit": -5,
                "full_recovery_target_profit": 5,
                "recovery_mode": "bounded_unit_recovery",
            },
        }
        self.assertTrue(is_recovery_candidate(candidate))
        control = candidate_control(settings, candidate, state)
        self.assertTrue(control["recovery_exempt"])
        self.assertFalse(control["shadow"])
        self.assertEqual(control["stake_multiplier"], 1.0)

    def test_disabled_recovery_keeps_drawdown_analytics_without_governor_exemption(self):
        candidate = {
            "crypto_live_campaign": {
                "outstanding_drawdown": 500,
                "recovery_mode": "unit_flat",
                "bounded_unit_recovery": {
                    "enabled": False,
                    "drawdown_active": True,
                    "active": False,
                },
            },
        }
        self.assertFalse(is_recovery_candidate(candidate))

    def test_shadow_lane_blocks_normal_candidate_and_is_captured(self):
        settings = self.settings()
        identity = strategy_identity(settings, {"feature_schema_version": "schema-v2"})
        state = {
            "strategy_identity": identity,
            "execution_mode": "adaptive",
            "lanes": {"35-44c:NO": {"mode": "shadow", "stake_multiplier": 0}},
        }
        candidate = {
            "market_lane": "crypto_15m",
            "side": "no",
            "entry_price": 40,
            "ticker": "KXBTC15M-TEST",
            "close_time": "2026-08-13T20:15:00Z",
            "strategy_identity": identity,
            "strategy_version": identity["strategy_version"],
            "strategy_config_hash": identity["config_hash"],
            "feature_schema_version": identity["feature_schema_version"],
            "exact_fee_cents": 1,
        }
        control = candidate_control(settings, candidate, state)
        self.assertTrue(control["shadow"])
        candidate["adaptive_control"] = control
        candidate["skip_reasons"] = ["adaptive_lane_shadow"]
        self.assertEqual(apply_candidate_stake_multiplier(candidate, 10), 0)
        ledger = empty_ledger()
        added = capture_candidates(
            ledger,
            [candidate],
            now=datetime(2026, 8, 13, 20, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["cohort"], "adaptive_lane_shadow")
        self.assertEqual(added[0]["strategy_identity"], identity)

    def test_health_report_checks_process_freshness_streams_and_recovery(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "crypto_settings.json").write_text(
                json.dumps(
                    {
                        "CRYPTO_15M_UNIT_STAKING_ENABLED": "true",
                        "CRYPTO_15M_CONTINUOUS_OPPORTUNITY_MODE": "true",
                        "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
                        "CRYPTO_15M_UNIT_SIZE_PCT": "0.75",
                        "CRYPTO_15M_UNIT_MAX_PER_MARKET": "5",
                        "CRYPTO_15M_UNIT_MIN_DATA_QUALITY": "0.95",
                        "CRYPTO_15M_UNIT_WIN_PROB_MODEL_WEIGHT": "0.35",
                        "CRYPTO_15M_UNIT_KELLY_FRACTION": "0.25",
                        "CRYPTO_15M_GLOBAL_DAILY_LOSS_UNITS": "6",
                        "CRYPTO_15M_MIN_ENTRY_PRICE_CENTS": "40",
                        "CRYPTO_15M_MID_PRICE_MIN_CENTS": "40",
                        "CRYPTO_15M_SIMPLE_MIN_PRICE_CENTS": "40",
                        "CRYPTO_15M_TIERED_MIN_PRICE_CENTS": "40",
                        "CRYPTO_15M_MAX_UNCONFIRMED_MARKET_GAP": "8",
                        "CRYPTO_15M_FLOW_CAN_OVERRIDE_MARKET_GAP": "false",
                        "CRYPTO_15M_FLOW_CONFIRMATION_MIN_STRENGTH": "0.22",
                        "CRYPTO_15M_FLOW_CONFIRMATION_MIN_SOURCES": "3",
                        "CRYPTO_15M_DAILY_LOSS_CAP": "400",
                        "CRYPTO_15M_CROSS_BOT_CAP_RECOVERY_ENABLED": "true",
                        "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED": "true",
                        "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_MIN_UNITS": "1",
                        "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "6",
                        "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "6",
                        "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_ENABLED": "true",
                        "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS": "15",
                        "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS": "2",
                        "CRYPTO_15M_UNIT_RECOVERY_ENABLED": "false",
                        "CRYPTO_15M_POOLED_RECOVERY_ENABLED": "true",
                        "CRYPTO_15M_RECOVERY_MAX_OVERLAYS_PER_EXPIRY": "1",
                        "CRYPTO_15M_UNIT_RECOVERY_MIN_BASE_UNITS": "3",
                        "CRYPTO_15M_UNIT_RECOVERY_MIN_PROBABILITY_UNITS": "3",
                        "CRYPTO_15M_UNIT_RECOVERY_TARGET_DRAWDOWN_FRACTION": "0.33",
                        "CRYPTO_15M_UNIT_RECOVERY_MAX_BONUS_UNITS": "2",
                        "CRYPTO_15M_UNIT_RECOVERY_MAX_TOTAL_UNITS": "5",
                        "CRYPTO_15M_PARTIAL_RECOVERY_ENABLED": "true",
                        "CRYPTO_15M_PARTIAL_RECOVERY_FRACTION": "0.50",
                        "CRYPTO_15M_FULL_RECOVERY_LOSSES": "4",
                        "CRYPTO_SHARED_RECOVERY_ENABLED": "false",
                        "SHARED_CRYPTO_DAILY_LOSS_CAP": "0",
                        "SHARED_CRYPTO_DAILY_LOSS_PCT": "0.05",
                        "SHARED_CRYPTO_RISK_CAP_BUFFER": "0",
                        "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_CAP": "0",
                        "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_PCT": "0",
                        **PROTECTED_RECOVERY_SETTINGS,
                        **PROTECTED_UNIT_RISK_SETTINGS,
                    }
                ),
                encoding="utf-8",
            )
            (root / ".crypto_bot.pid").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            (root / "bot_processes.json").write_text(
                json.dumps({"crypto": {"pid": os.getpid(), "running": True}}),
                encoding="utf-8",
            )
            (root / "crypto_live_portfolio.json").write_text(
                json.dumps({
                    "bets": [],
                    "crypto_15m_high_water_ledgers": {
                        str(number): {
                            "mode": "continuous_high_water_drawdown",
                            "migration_cutoff": now.isoformat(),
                            "current_profit": 1.0,
                            "high_water_profit": 2.0,
                            "outstanding_drawdown": 1.0,
                        }
                        for number in range(1, 4)
                    },
                    "crypto_15m_pooled_high_water_ledger": {
                        "mode": "pooled_continuous_high_water_drawdown",
                        "migration_cutoff": now.isoformat(),
                        "current_profit": 1.0,
                        "high_water_profit": 2.0,
                        "outstanding_drawdown": 1.0,
                    },
                }),
                encoding="utf-8",
            )
            report = {
                "generated_at": now.isoformat(),
                "mode": "live",
                "crypto_15m_campaign": {
                    "continuous_opportunity_mode": True,
                    "progression_mode": "continuous_opportunity_units",
                    "cycle_workflow_enabled": False,
                    "cycle_tracking_only": False,
                    "cycle_completion_blocks": False,
                    "target_remaining": 0,
                    "configured_bot_count": 3,
                    "max_open_per_bot": 1,
                    "pooled_recovery_enabled": True,
                    "recovery_reservations": {
                        "open_overlay_count": 0,
                        "reserved_recovery_profit": 0,
                    },
                },
                "btc_15m_sprint": {
                    "mode": "paper_shadow_only",
                    "status": "COLLECTING",
                    "affects_execution": False,
                    "automatic_promotion": False,
                    "prospective_started_at": now.isoformat(),
                    "prospective_ends_at": (now + timedelta(hours=72)).isoformat(),
                    "configuration": {"policy_hash": "locked-test-policy"},
                    "execution_freeze": {
                        "active": True,
                        "healthy_intentional_state": True,
                    },
                },
                "crypto_15m_microstructure": {
                    "coinbase_stream_connected": True,
                    "kraken_stream_connected": True,
                    "coinbase_trade_gap_count": 0,
                    "kalshi_stream": {
                        "connected": True,
                        "thread_alive": True,
                        "last_message_at": now.isoformat(),
                        "sequence_gap_count": 0,
                    },
                },
                "live_reconciliation": {
                    "account": {"ok": True},
                    "readiness": {
                        "ok": False,
                        "blocking": [
                            "live_order_enabled",
                            "sprint_shadow_only_disabled",
                            "dry_run_disabled",
                        ],
                    },
                    "unmatched_local_tickers": [],
                },
                "spot_flow_live_pilot": {
                    "mode": "live_pilot_armed",
                    "configuration": {
                        "enabled": True,
                        "stake_fraction": 0.003,
                        "daily_loss_fraction": 0.009,
                        "open_exposure_fraction": 0.009,
                        "maximum_contracts": 5,
                        "maximum_adverse_move_cents": 1,
                        "maximum_pair_capture_delta_seconds": 2,
                        "maximum_open_per_expiry": 2,
                        "minimum_bankroll": 0,
                    },
                    "validation": {
                        "eligible": True,
                        "gates": {
                            "spot_tournament_interim": True,
                            "prospective_combo_forward": True,
                        },
                    },
                    "risk": {
                        "ok": False,
                        "reasons": ["pilot_bankroll_below_minimum"],
                        "bankroll": {"cash_balance": 0.43},
                    },
                    "eligible_candidates": 0,
                },
                "kalshi_market_fetch": {"outage": False, "network_failure": False},
                "market_regime": {
                    "affects_execution": False,
                    "execution_requested": False,
                    "execution_validation_qualified": False,
                },
                "scan_intelligence": {
                    "schema_version": 1,
                    "candidate_records": 1,
                    "active_records": 1,
                    "stats": {"candidate_records": 1},
                    "validation": {
                        "affects_live_probability": False,
                        "affects_live_entry_rules": False,
                    },
                    "dynamic_policy": {
                        "version": "dynamic-qualification-v1",
                        "status": "shadow",
                        "qualified_for_activation": False,
                        "affects_execution": False,
                        "independent_markets": 0,
                        "minimum_independent_markets": 100,
                        "activation_checks": {},
                    },
                },
            }
            (root / "crypto_live_report.json").write_text(json.dumps(report), encoding="utf-8")
            (root / "crypto_market_regime.json").write_text(
                json.dumps({
                    "version": "market-regime-v3",
                    "latest": {
                        "generated_at": now.isoformat(),
                        "global": {
                            "direction_1h": "neutral",
                            "direction_4h": "neutral",
                            "direction_24h": "neutral",
                        },
                    },
                    "forecast_records": [],
                    "forecast_evaluation": {
                        "horizons": {"1h": {}, "4h": {}, "24h": {}},
                    },
                }),
                encoding="utf-8",
            )
            (root / "crypto_live_log.txt").write_text("healthy\n", encoding="utf-8")
            health = build_health_report(root=root, now=now)
            self.assertEqual(health["status"], "healthy")
            self.assertTrue(health["recovery_protected"])
            self.assertTrue(health["unit_risk_protected"])


if __name__ == "__main__":
    unittest.main()
