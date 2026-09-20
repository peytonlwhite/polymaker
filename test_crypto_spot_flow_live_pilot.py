import inspect
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_paper_bettor as bettor
from crypto_spot_flow_live_pilot import (
    CONFIRMED_TIER,
    CONTEMPORANEOUS_SPOT_FLOW_LANE,
    OWNER,
    SPOT_ONLY_TIER,
    build_execution_candidates as raw_build_execution_candidates,
    exact_signal_review,
    prospective_summary,
    risk_review,
    size_for_bankroll,
)


REGISTERED = "2026-08-24T16:41:01+00:00"


def settings(**overrides):
    value = dict(bettor.DEFAULT_SETTINGS)
    value.update(
        {
            "CRYPTO_EXECUTION_MODE": "live",
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED": "true",
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_REGISTERED_AT": REGISTERED,
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_TOURNAMENT_GATE": "true",
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_FORWARD_GATE": "true",
            "CRYPTO_15M_SPRINT_SHADOW_ONLY": "true",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
        }
    )
    value.update(overrides)
    return value


def candidate(ticker="KXBTC15M-TEST", flow=0.40):
    fee_schedule = {
        "authoritative": True,
        "exact": True,
        "supported": True,
        "fee_type": "quadratic",
        "coefficient": 0.07,
        "fee_multiplier": 1.0,
    }
    return {
        "ticker": ticker,
        "exchange_index": 2,
        "event_ticker": "KXBTC15M-TEST-EVENT",
        "series_ticker": "KXBTC15M",
        "asset": "BTC",
        "market_lane": "crypto_15m",
        "is_15m_market": True,
        "market_kind": "above",
        "minutes_to_close": 8.0,
        "close_time": "2026-08-24T17:00:00+00:00",
        "side": "no",
        "entry_price": 55.0,
        "yes_ask": 40.0,
        "no_ask": 61.0,
        "model_prob_yes": 50.0,
        "edge": -2.0,
        "confidence": 10.0,
        "fee_schedule": fee_schedule,
        "fee_schedule_exact": True,
        "data_quality": {"score": 0.99},
        "kalshi_microstructure": {
            "sequence_valid": True,
            "fresh": True,
            "book_consistent": True,
            "age_seconds": 0.2,
            "spread_yes_cents": 1.0,
            "best_yes_entry_price_cents": 40.0,
            "best_no_entry_price_cents": 61.0,
            "yes_mid_change_60s_pp": 0.0,
        },
        "microstructure": {
            "coinbase": {
                "connected": True,
                "stream_age_seconds": 0.2,
                "mid_return_60s_bps": 10.0,
                "mid_return_15s_bps": 2.0,
                "trade_flow_60s": flow,
                "trade_count_60s": 30,
            },
            "kraken": {
                "connected": True,
                "stream_age_seconds": 0.3,
                "mid_return_60s_bps": 9.0,
                "mid_return_15s_bps": 2.0,
                "trade_flow_60s": flow + 0.05,
                "trade_count_60s": 32,
            },
        },
    }


def spot_only_candidate(ticker="KXBTC15M-SPOT-ONLY"):
    row = candidate(ticker=ticker, flow=0.05)
    row["microstructure"]["kraken"]["trade_flow_60s"] = 0.04
    row["microstructure"]["coinbase"]["mid_return_60s_bps"] = 12.0
    row["microstructure"]["kraken"]["mid_return_60s_bps"] = 11.0
    return row


def reconciliation(cash=3000.0, generated_at="2026-08-24T16:42:00+00:00"):
    return {
        "generated_at": generated_at,
        "account": {"ok": True, "cash_balance": cash, "positions_complete": True, "orders_complete": True, "resting_orders": [], "balance_raw": {"balance_breakdown": [{"exchange_index": 2, "balance": str(cash)}]}},
        "unmatched_local_tickers": [],
        "unmatched_remote_tickers": [],
    }


def forward_ledger(count=100):
    rows = []
    start = datetime.fromisoformat(REGISTERED) + timedelta(minutes=1)
    for index in range(count):
        captured = start + timedelta(minutes=index * 45)
        ticker = f"KXBTC15M-FWD-{index}"
        for lane in ("spot_leads_kalshi", "cross_venue_flow_follow"):
            rows.append(
                {
                    "ticker": ticker,
                    "lane": lane,
                    "captured_at": captured.isoformat(),
                    "capture_batch_id": captured.isoformat(),
                    "expiry_key": (captured + timedelta(minutes=10)).isoformat(),
                    "status": "settled",
                    "primary": {
                        "side": "yes",
                        "virtual_profit": 0.20,
                        "total_cost_dollars": 0.40,
                    },
                    "comparator": {"side": "no", "virtual_profit": -0.20},
                }
            )
    return {"records": rows}



def build_execution_candidates(*args, **kwargs):
    # Execution/sizing fixtures assume independently completed qualification.
    # Gate enforcement itself is covered with the real validator in audit tests.
    kwargs.setdefault("precomputed_validation", {"eligible": True, "gates": {
        "pilot_enabled": True, "spot_tournament_interim": True, "prospective_combo_forward": True},
        "bypassed_gates": []})
    return raw_build_execution_candidates(*args, **kwargs)


class SpotFlowLivePilotTests(unittest.TestCase):
    def test_exact_signal_requires_same_direction_spot_and_flow(self):
        confirmed = exact_signal_review(candidate(), settings())
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["tier"], CONFIRMED_TIER)
        self.assertEqual(confirmed["maximum_contracts"], 5)
        opposed = candidate(flow=-0.40)
        opposed["microstructure"]["kraken"]["trade_flow_60s"] = -0.45
        review = exact_signal_review(opposed, settings())
        self.assertFalse(review["ok"])
        self.assertIn("spot_flow_direction_mismatch", review["reasons"])

    def test_confirmed_tier_uses_the_documented_two_to_thirteen_minute_window(self):
        too_early = candidate("KXBTC15M-CONFIRMED-EARLY")
        too_early["minutes_to_close"] = 13.1
        too_late = candidate("KXBTC15M-CONFIRMED-LATE")
        too_late["minutes_to_close"] = 1.9

        self.assertIn(
            "pilot_confirmed_time_window",
            exact_signal_review(too_early, settings())["reasons"],
        )
        self.assertIn(
            "pilot_confirmed_time_window",
            exact_signal_review(too_late, settings())["reasons"],
        )

    def test_spot_only_tier_uses_stronger_registered_rules_and_smaller_cap(self):
        review = exact_signal_review(
            spot_only_candidate(),
            settings(CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED="true"),
        )

        self.assertTrue(review["ok"])
        self.assertEqual(review["tier"], SPOT_ONLY_TIER)
        self.assertEqual(review["maximum_contracts"], 3)
        self.assertNotIn("cross_venue_flow_signal_absent", review["reasons"])

    def test_spot_only_tier_keeps_price_time_and_strength_floors(self):
        enabled = settings(
            CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED="true"
        )
        low_price = spot_only_candidate("KXBTC15M-SPOT-LOW-PRICE")
        low_price["kalshi_microstructure"]["best_yes_entry_price_cents"] = 34.0
        self.assertIn(
            "pilot_spot_only_price_outside_band",
            exact_signal_review(low_price, enabled)["reasons"],
        )

        too_late = spot_only_candidate("KXBTC15M-SPOT-TOO-LATE")
        too_late["minutes_to_close"] = 4.9
        self.assertIn(
            "pilot_spot_only_time_window",
            exact_signal_review(too_late, enabled)["reasons"],
        )

        weak = spot_only_candidate("KXBTC15M-SPOT-WEAK")
        weak["microstructure"]["coinbase"]["mid_return_60s_bps"] = 9.0
        weak["microstructure"]["kraken"]["mid_return_60s_bps"] = 9.0
        self.assertIn(
            "pilot_spot_only_strength_below_floor",
            exact_signal_review(weak, enabled)["reasons"],
        )

    def test_spot_only_tier_never_overrides_registered_opposing_flow(self):
        opposed = spot_only_candidate("KXBTC15M-SPOT-OPPOSED")
        opposed["microstructure"]["coinbase"]["trade_flow_60s"] = -0.40
        opposed["microstructure"]["kraken"]["trade_flow_60s"] = -0.45

        review = exact_signal_review(
            opposed,
            settings(CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED="true"),
        )

        self.assertFalse(review["ok"])
        self.assertIn("spot_flow_direction_mismatch", review["reasons"])

    def test_absent_spot_signal_does_not_cascade_to_price_band_rejection(self):
        row = candidate(flow=0.40)
        row["microstructure"]["coinbase"]["mid_return_60s_bps"] = 0.0
        row["microstructure"]["kraken"]["mid_return_60s_bps"] = 0.0

        review = exact_signal_review(row, settings())

        self.assertFalse(review["ok"])
        self.assertIn("spot_lead_signal_absent", review["reasons"])
        self.assertNotIn("pilot_price_outside_band", review["reasons"])
        self.assertIsNone(review["entry_price_cents"])

    def test_selected_side_with_real_out_of_band_price_is_rejected(self):
        row = candidate(flow=0.40)
        row["kalshi_microstructure"]["best_yes_entry_price_cents"] = 81.0

        review = exact_signal_review(row, settings())

        self.assertFalse(review["ok"])
        self.assertIn("pilot_price_outside_band", review["reasons"])

    def test_sizing_uses_verified_bankroll_fraction_and_contract_cap(self):
        row = candidate()
        row["entry_price"] = 40.0
        at_three_hundred = size_for_bankroll(row, 300, settings())
        at_fifteen = size_for_bankroll(row, 1500, settings())
        at_three = size_for_bankroll(row, 3000, settings())
        at_five = size_for_bankroll(row, 5000, settings())
        self.assertEqual(at_three_hundred["target_total_cost"], 0.90)
        self.assertEqual(at_three_hundred["contracts"], 2)
        self.assertEqual(at_three_hundred["principal_stake"], 0.80)
        self.assertEqual(at_fifteen["target_total_cost"], 4.50)
        self.assertEqual(at_fifteen["contracts"], 5)
        self.assertEqual(at_fifteen["principal_stake"], 2.00)
        self.assertEqual(at_three["contracts"], 5)
        self.assertEqual(at_three["principal_stake"], 2.00)
        self.assertEqual(at_five["contracts"], 5)
        self.assertEqual(at_five["principal_stake"], 2.00)

    def test_forward_gate_uses_only_post_registration_same_side_pairs(self):
        summary = prospective_summary(forward_ledger(), settings(
            CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_FORWARD_GATE="true"
        ))
        self.assertEqual(summary["settled"], 100)
        self.assertGreaterEqual(summary["observation_days"], 3)
        self.assertTrue(summary["eligible"])
        self.assertGreater(summary["expiry_cluster_inference"]["lower_bound"], 0)

    def test_forward_gate_rejects_time_mismatched_legacy_pair_and_uses_direct_record(self):
        captured = datetime.fromisoformat(REGISTERED) + timedelta(minutes=1)
        legacy_rows = []
        for lane, offset in (
            ("spot_leads_kalshi", 0),
            ("cross_venue_flow_follow", 30),
        ):
            legacy_rows.append(
                {
                    "ticker": "KXBTC15M-NONCONTEMP",
                    "lane": lane,
                    "captured_at": (captured + timedelta(seconds=offset)).isoformat(),
                    "status": "settled",
                    "primary": {
                        "side": "yes",
                        "virtual_profit": 0.20,
                        "total_cost_dollars": 0.40,
                    },
                    "comparator": {"side": "no", "virtual_profit": -0.20},
                }
            )
        direct = {
            "ticker": "KXBTC15M-DIRECT",
            "lane": CONTEMPORANEOUS_SPOT_FLOW_LANE,
            "captured_at": captured.isoformat(),
            "capture_batch_id": captured.isoformat(),
            "expiry_key": (captured + timedelta(minutes=10)).isoformat(),
            "status": "settled",
            "primary": {
                "side": "yes",
                "virtual_profit": 0.25,
                "total_cost_dollars": 0.40,
            },
            "comparator": {"side": "no", "virtual_profit": -0.25},
        }
        summary = prospective_summary(
            {"records": [*legacy_rows, direct]}, settings()
        )
        self.assertEqual(summary["settled"], 1)
        self.assertEqual(
            summary["pairing_diagnostics"]["direct_same_scan_settled"], 1
        )
        self.assertEqual(
            summary["pairing_diagnostics"]["noncontemporaneous_rejected"], 1
        )

    def test_below_minimum_bankroll_is_fail_closed(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        clones, status = build_execution_candidates(
            [candidate()],
            {"bets": []},
            reconciliation(cash=2999),
            {"lanes": []},
            {"records": []},
            settings=settings(CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_BANKROLL="3000"),
            now=now,
        )
        self.assertEqual(len(clones), 1)
        self.assertFalse(clones[0]["spot_flow_live_pilot"]["eligible"])
        self.assertIn(
            "pilot_bankroll_below_minimum",
            clones[0]["spot_flow_live_pilot"]["reasons"],
        )
        self.assertEqual(status["eligible_candidates"], 0)

    def test_dynamic_sizing_has_no_arbitrary_bankroll_floor(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        clones, status = build_execution_candidates(
            [candidate()],
            {"bets": []},
            reconciliation(cash=1500),
            {"lanes": []},
            {"records": []},
            settings=settings(),
            now=now,
        )
        self.assertEqual(status["risk"]["bankroll"]["sizing_bankroll"], 1500)
        self.assertEqual(clones[0]["spot_flow_live_pilot"]["sizing"]["contracts"], 5)
        self.assertTrue(clones[0]["spot_flow_live_pilot"]["eligible"])
        self.assertEqual(
            clones[0]["spot_flow_live_pilot"]["signal_matched_at"],
            now.isoformat(),
        )

        tiny, tiny_status = build_execution_candidates(
            [candidate()],
            {"bets": []},
            reconciliation(cash=0.75),
            {"lanes": []},
            {"records": []},
            settings=settings(),
            now=now,
        )
        self.assertFalse(tiny[0]["spot_flow_live_pilot"]["eligible"])
        self.assertIn(
            "pilot_stake_below_one_contract",
            tiny[0]["spot_flow_live_pilot"]["reasons"],
        )
        self.assertEqual(tiny_status["eligible_candidates"], 0)

    def test_compact_precomputed_validation_avoids_large_ledger_dependency(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        validation = {
            "eligible": True,
            "gates": {
                "pilot_enabled": True,
                "spot_tournament_interim": True,
                "prospective_combo_forward": True,
            },
            "spot_tournament": {},
            "prospective_combo": {"eligible": True},
        }
        clones, status = build_execution_candidates(
            [candidate()],
            {"bets": []},
            reconciliation(cash=1500),
            {},
            {"records": "not-a-list-and-must-not-be-read"},
            settings=settings(),
            now=now,
            precomputed_validation=validation,
        )
        self.assertEqual(status["validation"], validation)
        self.assertTrue(clones[0]["spot_flow_live_pilot"]["eligible"])

    def test_spot_only_candidate_is_separately_tracked_and_capped_at_three(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        clones, status = build_execution_candidates(
            [spot_only_candidate()],
            {"bets": []},
            reconciliation(cash=3000),
            {"lanes": []},
            {"records": []},
            settings=settings(
                CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED="true"
            ),
            now=now,
        )

        self.assertEqual(len(clones), 1)
        review = clones[0]["spot_flow_live_pilot"]
        self.assertTrue(review["eligible"])
        self.assertEqual(review["tier"], SPOT_ONLY_TIER)
        self.assertEqual(review["sizing"]["contracts"], 3)
        self.assertEqual(review["sizing"]["maximum_contracts"], 3)
        self.assertEqual(status["funnel"][f"signal_matched:{SPOT_ONLY_TIER}"], 1)
        self.assertEqual(status["funnel"][f"eligible:{SPOT_ONLY_TIER}"], 1)
        self.assertEqual(status["recent_candidates"][0]["tier"], SPOT_ONLY_TIER)

    def test_sports_positions_do_not_shrink_crypto_equity_sizing(self):
        now = datetime.now(timezone.utc)
        account = reconciliation(
            cash=0.73,
            generated_at=now.isoformat(),
        )
        account["account"]["balance_raw"]["portfolio_value"] = 63806
        row = candidate()
        row["entry_price"] = 41.0
        row["yes_ask"] = 41.0
        clones, status = build_execution_candidates(
            [row],
            {"bets": []},
            account,
            {"lanes": []},
            {"records": []},
            settings=settings(),
            now=now,
        )
        bankroll = status["risk"]["bankroll"]
        sizing = clones[0]["spot_flow_live_pilot"]["sizing"]
        self.assertEqual(bankroll["cash_balance"], 0.73)
        self.assertEqual(bankroll["account_equity"], 638.79)
        self.assertEqual(bankroll["sizing_bankroll"], 638.79)
        self.assertEqual(bankroll["sizing_basis"], "verified_account_equity")
        self.assertEqual(sizing["contracts"], 1)
        self.assertTrue(sizing["cash_limited"])
        self.assertTrue(clones[0]["spot_flow_live_pilot"]["eligible"])

    def test_candidate_isolated_from_campaign_units_and_recovery(self):
        row = candidate()
        row["crypto_live_campaign"] = {"enabled": True, "applied_stake": 100}
        row["crypto_units"] = {"target_units": 5}
        row["shared_recovery"] = {"active": True}
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        clones, _status = build_execution_candidates(
            [row],
            {"bets": []},
            reconciliation(cash=3000),
            {"lanes": []},
            {"records": []},
            settings=settings(),
            now=now,
        )
        clone = clones[0]
        self.assertTrue(clone["spot_flow_live_pilot"]["eligible"])
        self.assertEqual(clone["strategy_owner"], OWNER)
        self.assertNotIn("crypto_live_campaign", clone)
        self.assertNotIn("crypto_units", clone)
        self.assertFalse(clone["recovery_context"]["active"])

    def test_risk_caps_daily_loss_and_stale_reconciliation(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        portfolio = {
            "bets": [
                {
                    "mode": "live",
                    "strategy_owner": OWNER,
                    "status": "settled",
                    "placed_at": "2026-08-24T16:20:00+00:00",
                    "profit": -30.0,
                }
            ]
        }
        review = risk_review(
            portfolio,
            reconciliation(cash=3000, generated_at="2026-08-24T16:30:00+00:00"),
            {"eligible": True},
            settings=settings(),
            now=now,
        )
        self.assertIn("pilot_reconciliation_stale", review["reasons"])
        self.assertIn("pilot_daily_loss_cap", review["reasons"])

    def test_legacy_freeze_does_not_block_pilot_preflight(self):
        pilot = candidate()
        pilot["spot_flow_live_pilot"] = {"active": True, "eligible": True}
        pilot["strategy_owner"] = OWNER
        legacy = candidate("KXBTC15M-LEGACY")
        self.assertTrue(
            bettor.preflight_live_kalshi_order(settings(), pilot, 1.20)["ok"]
        )
        self.assertEqual(
            bettor.preflight_live_kalshi_order(settings(), legacy, 1.20)["error"],
            "crypto_15m_sprint_live_freeze",
        )

    def test_pilot_dry_run_is_fok_and_preserves_pilot_owner(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        clones, _status = build_execution_candidates(
            [candidate()],
            {"bets": []},
            reconciliation(cash=3000),
            {"lanes": []},
            {"records": []},
            settings=settings(),
            now=now,
        )
        pilot = clones[0]
        stake = bettor.candidate_stake(settings(), pilot, 3000, {"bets": []})
        order = bettor.place_live_kalshi_order(
            settings(
                CRYPTO_LIVE_DRY_RUN="true",
                CRYPTO_LIVE_ORDER_ENABLED="true",
            ),
            {"bets": []},
            pilot,
            stake,
        )
        self.assertTrue(order["dry_run"])
        self.assertEqual(order["request"]["time_in_force"], "fill_or_kill")
        self.assertEqual(order["contracts"], 5)
        self.assertEqual(bettor.crypto_strategy_owner(pilot), OWNER)

    def test_live_record_keeps_pilot_audit_payload_without_campaign_accounting(self):
        now = datetime(2026, 8, 24, 16, 42, tzinfo=timezone.utc)
        clones, _status = build_execution_candidates(
            [candidate()],
            {"bets": []},
            reconciliation(cash=3000),
            {"lanes": []},
            {"records": []},
            settings=settings(),
            now=now,
        )
        portfolio = {"bets": []}
        bet = bettor.record_live_bet(
            portfolio,
            clones[0],
            {
                "ok": True,
                "actual_stake": 1.20,
                "contracts": 3,
                "price": 40,
                "executed_price": 40,
                "response": {},
            },
            persist=False,
        )
        self.assertEqual(bet["strategy_owner"], OWNER)
        self.assertIsNone(bet["bot_number"])
        self.assertTrue(bet["spot_flow_live_pilot"]["active"])
        self.assertNotIn("crypto_live_campaign", clones[0])

    def test_pre_submit_refresh_reconfirms_signal_depth_and_one_cent_ceiling(self):
        row = candidate()
        row["close_time"] = (
            datetime.now(timezone.utc) + timedelta(minutes=8)
        ).isoformat()
        live_reconciliation = reconciliation(
            cash=3000,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        clones, _status = build_execution_candidates(
            [row],
            {"bets": []},
            live_reconciliation,
            {"lanes": []},
            {"records": []},
            settings=settings(),
        )

        class Stream:
            def __init__(self, payload):
                self.payload = payload

            def snapshot(self, _asset):
                return dict(self.payload)

        coinbase = row["microstructure"]["coinbase"]
        kraken = row["microstructure"]["kraken"]
        fresh_book = {
            "orderbook_fp": {
                "yes_dollars": [["0.39", "10"]],
                "no_dollars": [["0.60", "10"]],
            }
        }
        with (
            patch.object(bettor, "COINBASE_MICROSTRUCTURE_STREAM", Stream(coinbase)),
            patch.object(bettor, "KRAKEN_MICROSTRUCTURE_STREAM", Stream(kraken)),
            patch.object(
                bettor,
                "fetch_fresh_kalshi_orderbook",
                return_value=(fresh_book, datetime.now(timezone.utc).isoformat()),
            ),
            patch.object(bettor, "read_json", return_value=live_reconciliation),
        ):
            refreshed = bettor.refresh_live_spot_flow_pilot_quote(
                settings(), {"bets": []}, clones[0]
            )
        self.assertTrue(refreshed["ok"])
        self.assertEqual(refreshed["refreshed_price_cents"], 40.0)
        self.assertEqual(refreshed["required_contracts"], 5)
        self.assertGreaterEqual(refreshed["signal_to_refresh_seconds"], 0)

        adverse_book = {
            "orderbook_fp": {
                "yes_dollars": [["0.41", "10"]],
                "no_dollars": [["0.58", "10"]],
            }
        }
        adverse_candidate = clones[0].copy()
        adverse_candidate["entry_price"] = 40.0
        with (
            patch.object(bettor, "COINBASE_MICROSTRUCTURE_STREAM", Stream(coinbase)),
            patch.object(bettor, "KRAKEN_MICROSTRUCTURE_STREAM", Stream(kraken)),
            patch.object(
                bettor,
                "fetch_fresh_kalshi_orderbook",
                return_value=(adverse_book, datetime.now(timezone.utc).isoformat()),
            ),
            patch.object(bettor, "read_json", return_value=live_reconciliation),
        ):
            adverse = bettor.refresh_live_spot_flow_pilot_quote(
                settings(), {"bets": []}, adverse_candidate
            )
        self.assertFalse(adverse["ok"])
        self.assertEqual(adverse["error"], "pilot_live_quote_adverse_move")

    def test_pre_submit_refresh_preserves_spot_only_three_contract_cap(self):
        row = spot_only_candidate()
        row["close_time"] = (
            datetime.now(timezone.utc) + timedelta(minutes=8)
        ).isoformat()
        live_reconciliation = reconciliation(
            cash=3000,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        enabled = settings(
            CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED="true"
        )
        clones, _status = build_execution_candidates(
            [row],
            {"bets": []},
            live_reconciliation,
            {"lanes": []},
            {"records": []},
            settings=enabled,
        )

        class Stream:
            def __init__(self, payload):
                self.payload = payload

            def snapshot(self, _asset):
                return dict(self.payload)

        fresh_book = {
            "orderbook_fp": {
                "yes_dollars": [["0.39", "10"]],
                "no_dollars": [["0.60", "10"]],
            }
        }
        with (
            patch.object(
                bettor,
                "COINBASE_MICROSTRUCTURE_STREAM",
                Stream(row["microstructure"]["coinbase"]),
            ),
            patch.object(
                bettor,
                "KRAKEN_MICROSTRUCTURE_STREAM",
                Stream(row["microstructure"]["kraken"]),
            ),
            patch.object(
                bettor,
                "fetch_fresh_kalshi_orderbook",
                return_value=(fresh_book, datetime.now(timezone.utc).isoformat()),
            ),
            patch.object(bettor, "read_json", return_value=live_reconciliation),
        ):
            refreshed = bettor.refresh_live_spot_flow_pilot_quote(
                enabled,
                {"bets": []},
                clones[0],
            )

        self.assertTrue(refreshed["ok"])
        self.assertEqual(refreshed["required_contracts"], 3)
        self.assertEqual(
            refreshed["pilot_review"]["revalidated_tier"],
            SPOT_ONLY_TIER,
        )
        self.assertEqual(
            refreshed["pilot_review"]["sizing"]["maximum_contracts"],
            3,
        )

    def test_live_execution_precedes_slow_shadow_ledgers(self):
        source = inspect.getsource(bettor.run_scan)
        execution_index = source.index("for candidate in execution_candidates:")
        for delayed_work in (
            "cycle_shadow = run_crypto_cycle_shadow(",
            "low_edge_shadow = run_low_edge_shadow(",
            "rejection_shadow = run_rejection_shadow(",
            "directional_opposition_v2 = run_directional_opposition_v2(",
            "signal_tournament_shadow = run_signal_tournament_shadow(",
            "execution_lab_shadow = run_execution_lab_shadow(",
            "asset_specialist_shadow = run_asset_specialist_shadow(",
            "complement_arb_shadow = run_complement_arb_shadow(",
            "maker_taker_shadow = run_maker_taker_shadow(",
        ):
            self.assertLess(execution_index, source.index(delayed_work), delayed_work)
        self.assertIn("deferred_until_after_live_decision", source)


if __name__ == "__main__":
    unittest.main()
