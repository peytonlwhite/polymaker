import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crypto_dynamic_qualification import (
    dynamic_policy_configuration,
    dynamic_policy_validation,
    dynamic_qualification_review,
)
from crypto_scan_intelligence import record_scan_intelligence


class DynamicCryptoQualificationTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "CRYPTO_DYNAMIC_QUALIFICATION_ENABLED": "true",
            "CRYPTO_DYNAMIC_AUTO_PROMOTE_ENABLED": "true",
            "CRYPTO_DYNAMIC_MIN_SETTLED_MARKETS": "100",
            "CRYPTO_DYNAMIC_RECENT_VALIDATION_MARKETS": "40",
            "CRYPTO_15M_MIN_ENTRY_PRICE_CENTS": "40",
            "CRYPTO_15M_MAX_ENTRY_PRICE_CENTS": "70",
            "CRYPTO_15M_MIN_MINUTES_REMAINING": "2",
            "CRYPTO_15M_MAX_MINUTES_REMAINING": "12",
        }
        values.update(overrides)
        return values

    def candidate(self, ticker="KXBTC15M-DYNAMIC"):
        return {
            "asset": "BTC",
            "ticker": ticker,
            "event_ticker": ticker,
            "series_ticker": "KXBTC15M",
            "market_lane": "crypto_15m",
            "market_kind": "above",
            "is_15m_market": True,
            "side": "yes",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "minutes_to_close": 5.0,
            "entry_price": 40.0,
            "fee_schedule_exact": True,
            "exact_fee_cents": 1.0,
            "expected_slippage_cents": 1.0,
            "expected_edge": 2.5,
            "edge": 2.5,
            "edge_low": 0.75,
            "confidence": 90.0,
            "probability_net_edge_positive": 90.0,
            "selected_side_probability_low": 50.5,
            "selected_side_probability_high": 58.0,
            "probability": {
                "p_yes": 54.0,
                "p_low": 50.5,
                "p_high": 58.0,
                "settlement_source": "Pyth",
                "model_disagreement": 2.0,
                "market_implied_yes": 42.0,
            },
            "model": {"settlement_source": "Pyth", "spot": 100.0},
            "microstructure": {},
            "kalshi_microstructure": {
                "sequence_valid": True,
                "age_seconds": 0.1,
                "spread_yes_cents": 1.0,
            },
            "data_quality": {"score": 0.99},
            "flow_review": {
                "direction": "yes",
                "strength": 0.20,
                "healthy_source_count": 3,
                "source_scores": {
                    "coinbase_ws": 0.20,
                    "kraken_ws_v2": 0.15,
                    "kalshi_trades": 0.01,
                },
            },
            "disagreement_guard": {
                "enabled": True,
                "ok": True,
                "raw_market_gap": 2.0,
            },
            "skip_reasons": ["campaign_quality_filter"],
            "decision": "skipped",
        }

    def execution(self):
        return {
            "available": True,
            "tiers": {
                "0.5": {
                    "units": 0.5,
                    "complete_depth": True,
                    "contracts": 10,
                    "book_cost_dollars": 4.8,
                    "fee_dollars": 0.1,
                    "expected_adverse_selection_dollars": 0.1,
                    "break_even_probability": 50.0,
                    "expected_edge": 2.0,
                    "edge_low": 0.5,
                    "probability_net_edge_positive": 90.0,
                },
                "1": {
                    "units": 1.0,
                    "complete_depth": True,
                    "contracts": 20,
                    "book_cost_dollars": 10.2,
                    "fee_dollars": 0.2,
                    "expected_adverse_selection_dollars": 0.2,
                    "break_even_probability": 53.0,
                    "expected_edge": 1.0,
                    "edge_low": -2.5,
                    "probability_net_edge_positive": 70.0,
                },
            },
        }

    def probability(self):
        return {
            "win_probability": 54.0,
            "win_probability_low": 51.0,
            "sizing_probability_low": 50.5,
        }

    def review(self, candidate=None, active=False):
        settings = self.settings()
        config = dynamic_policy_configuration(settings)
        state = {
            "policy_hash": config["policy_hash"],
            "status": "active" if active else "shadow",
            "qualified_for_activation": active,
            "activation_reason": "locked_validation_passed" if active else "collecting",
        }
        return dynamic_qualification_review(
            settings,
            candidate or self.candidate(),
            self.execution(),
            self.probability(),
            {0.5: {}, 1.0: {}},
            state,
        )

    def test_one_cent_edge_lane_is_shadow_and_contract_floor_remains_40_cents(self):
        review = self.review()
        self.assertTrue(review["shadow_eligible"])
        self.assertFalse(review["eligible"])
        self.assertFalse(review["affects_execution"])
        self.assertEqual(review["target_units"], 0.5)
        self.assertEqual(review["contract_price_range_cents"], [40.0, 70.0])

    def test_promoted_policy_can_activate_same_half_unit_candidate(self):
        review = self.review(active=True)
        self.assertTrue(review["eligible"])
        self.assertTrue(review["affects_execution"])
        self.assertEqual(review["target_units"], 0.5)

    def test_one_cent_contract_is_rejected_even_though_one_cent_edge_is_allowed(self):
        candidate = self.candidate()
        candidate["entry_price"] = 1.0
        review = self.review(candidate)
        self.assertFalse(review["shadow_eligible"])
        self.assertIn("contract_price_range", review["hard_vetoes"])

    def test_two_opposing_sources_are_a_hard_veto(self):
        candidate = self.candidate()
        candidate["flow_review"]["direction"] = "no"
        candidate["flow_review"]["source_scores"] = {
            "coinbase_ws": -0.20,
            "kraken_ws_v2": -0.15,
            "kalshi_trades": 0.01,
        }
        review = self.review(candidate)
        self.assertFalse(review["shadow_eligible"])
        self.assertIn("directional_opposition", review["hard_vetoes"])

    def test_descriptive_15m_settlement_mapping_is_accepted(self):
        candidate = self.candidate()
        candidate["probability"].pop("settlement_source")
        candidate["model"].pop("settlement_source")
        candidate["probability"]["settlement_price_source"] = (
            "CF Benchmarks RTI settlement; Coinbase + Kraken live proxy with basis buffer"
        )
        review = self.review(candidate)
        self.assertTrue(review["shadow_eligible"])
        self.assertNotIn("settlement_mapping_unavailable", review["hard_vetoes"])

    def profitable_outcomes(self, count):
        config = dynamic_policy_configuration(self.settings())
        rows = []
        for index in range(count):
            won = index % 5 != 4
            probability = 0.8
            market_probability = 0.7
            cost = 0.7
            profit = 0.3 if won else -0.7
            rows.append({
                "policy_hash": config["policy_hash"],
                "ticker": f"MARKET-{index:03d}",
                "won": won,
                "total_cost_dollars": cost,
                "profit": profit,
                "roi": profit / cost,
                "predicted_win_probability": probability,
                "market_break_even_probability": market_probability,
                "brier": (probability - float(won)) ** 2,
                "market_brier": (market_probability - float(won)) ** 2,
            })
        return rows

    def test_locked_validation_requires_100_independent_markets(self):
        below = dynamic_policy_validation(self.settings(), self.profitable_outcomes(99))
        ready = dynamic_policy_validation(self.settings(), self.profitable_outcomes(100))
        self.assertFalse(below["qualified"])
        self.assertTrue(ready["qualified"])
        self.assertTrue(all(ready["checks"].values()))

    def test_lifecycle_counts_one_shadow_outcome_per_market(self):
        now = datetime.now(timezone.utc)
        settings = self.settings()
        candidate = self.candidate()
        candidate["execution_tier_pricing"] = self.execution()
        candidate["dynamic_qualification"] = self.review()
        candidate["crypto_units"] = {
            "eligible": False,
            "target_units": 0,
            "qualification_policy": "legacy_probability_edge_tiers",
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            state_file = Path(directory) / "state.json"
            record_scan_intelligence(
                [candidate], ledger, state_file, settings=settings, now=now
            )
            record_scan_intelligence(
                [candidate],
                ledger,
                state_file,
                settings=settings,
                now=now + timedelta(seconds=16),
            )
            record_scan_intelligence(
                [],
                ledger,
                state_file,
                settings=settings,
                now=now + timedelta(seconds=130),
                settlements=[{"ticker": candidate["ticker"], "result": "yes"}],
            )
            state = json.loads(state_file.read_text())
            outcomes = state["dynamic_policy"]["outcomes"]
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["ticker"], candidate["ticker"])

    def test_counterfactual_collector_counts_rejected_market_once(self):
        now = datetime.now(timezone.utc)
        settings = self.settings()
        candidate = self.candidate()
        candidate["execution_tier_pricing"] = self.execution()
        review = self.review()
        review.update({
            "shadow_eligible": False,
            "eligible": False,
            "target_units": 0.0,
            "lane": "",
            "reason": "dynamic_quality_filter",
            "hard_vetoes": [],
        })
        candidate["dynamic_qualification"] = review
        candidate["crypto_units"] = {
            "eligible": False,
            "target_units": 0,
            "qualification_policy": "legacy_probability_edge_tiers",
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            state_file = Path(directory) / "state.json"
            record_scan_intelligence(
                [candidate], ledger, state_file, settings=settings, now=now
            )
            record_scan_intelligence(
                [candidate],
                ledger,
                state_file,
                settings=settings,
                now=now + timedelta(seconds=16),
            )
            record_scan_intelligence(
                [],
                ledger,
                state_file,
                settings=settings,
                now=now + timedelta(seconds=130),
                settlements=[{"ticker": candidate["ticker"], "result": "no"}],
            )
            state = json.loads(state_file.read_text())
            dynamic = state["dynamic_policy"]
            self.assertEqual(dynamic["outcomes"], [])
            self.assertEqual(len(dynamic["counterfactual_outcomes"]), 1)
            observation = dynamic["counterfactual_outcomes"][0]
            self.assertEqual(observation["ticker"], candidate["ticker"])
            self.assertFalse(observation["shadow_policy_selected"])
            self.assertTrue(observation["tier_outcomes"])
            validation = dynamic["counterfactual_validation"]
            self.assertEqual(validation["observed_independent_markets"], 1)
            self.assertFalse(validation["affects_execution"])
            self.assertTrue(
                validation["activation_uses_policy_selected_markets_only"]
            )

    def test_counterfactual_collector_excludes_operationally_invalid_snapshot(self):
        now = datetime.now(timezone.utc)
        settings = self.settings()
        candidate = self.candidate()
        candidate["execution_tier_pricing"] = self.execution()
        review = self.review()
        review.update({
            "shadow_eligible": False,
            "target_units": 0.0,
            "hard_vetoes": ["kalshi_quote_stale"],
        })
        candidate["dynamic_qualification"] = review
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            state_file = Path(directory) / "state.json"
            record_scan_intelligence(
                [candidate], ledger, state_file, settings=settings, now=now
            )
            record_scan_intelligence(
                [],
                ledger,
                state_file,
                settings=settings,
                now=now + timedelta(seconds=130),
                settlements=[{"ticker": candidate["ticker"], "result": "yes"}],
            )
            dynamic = json.loads(state_file.read_text())["dynamic_policy"]
            self.assertEqual(dynamic["counterfactual_outcomes"], [])
            self.assertEqual(
                dynamic["counterfactual_validation"]["observed_independent_markets"],
                0,
            )


if __name__ == "__main__":
    unittest.main()
