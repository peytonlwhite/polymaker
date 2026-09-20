import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import crypto_15m_learning as learning
import crypto_microstructure as micro
import crypto_paper_bettor as crypto
from crypto_microstructure import (
    book_features,
    combined_flow_review,
    trade_flow_features,
)


class CryptoMicrostructureTests(unittest.TestCase):
    def test_kalshi_orderbook_exposes_executable_entry_prices(self):
        responses = [
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.6100", "12.00"]],
                    "no_dollars": [["0.5700", "8.00"]],
                }
            },
            {"trades": []},
        ]
        with patch.object(micro, "request_json", side_effect=responses):
            result = micro.kalshi_market_microstructure(
                "https://example.test",
                "TEST",
            )

        self.assertEqual(result["best_yes_entry_price_cents"], 43.0)
        self.assertEqual(result["best_no_entry_price_cents"], 39.0)
        self.assertIsNotNone(result["orderbook_fetched_at"])

    def test_kalshi_trade_rate_limit_does_not_discard_executable_book(self):
        responses = [
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.6100", "12.00"]],
                    "no_dollars": [["0.5700", "8.00"]],
                }
            },
            RuntimeError("HTTP 429"),
        ]
        with patch.object(micro, "request_json", side_effect=responses):
            result = micro.kalshi_market_microstructure(
                "https://example.test",
                "TEST",
            )

        self.assertEqual(result["best_yes_entry_price_cents"], 43.0)
        self.assertEqual(result["best_no_entry_price_cents"], 39.0)
        self.assertIsNone(result["trades_fetched_at"])
        self.assertIn("HTTP 429", result["trades_error"])

    def test_book_features_capture_depth_imbalance_and_microprice(self):
        features = book_features(
            bids=[[99.0, 10.0], [98.5, 2.0]],
            asks=[[101.0, 2.0], [101.5, 1.0]],
            depth_bps=200,
        )
        self.assertGreater(features["book_imbalance"], 0)
        self.assertGreater(features["microprice"], features["mid"])
        self.assertAlmostEqual(features["spread_bps"], 200.0, places=6)

    def test_trade_flow_uses_aggressor_direction_and_time_windows(self):
        now = 1_000.0
        features = trade_flow_features([
            {"timestamp": now - 5, "price": 100, "size": 2, "direction": 1},
            {"timestamp": now - 20, "price": 100, "size": 1, "direction": -1},
            {"timestamp": now - 90, "price": 100, "size": 8, "direction": -1},
        ], now_ts=now)
        self.assertEqual(features["trade_count_10s"], 1)
        self.assertAlmostEqual(features["trade_flow_10s"], 1.0)
        self.assertAlmostEqual(features["trade_flow_30s"], 1.0 / 3.0)
        self.assertLess(features["trade_flow_300s"], 0)
        self.assertEqual(features["trade_count_900s"], 3)
        self.assertEqual(features["trade_count_3600s"], 3)

    def test_combined_review_counts_only_directionally_confirming_sources(self):
        review = combined_flow_review(
            {
                "underlying_flow_score": 0.35,
                "settlement_proxy_source_count": 2,
                "cross_exchange_dispersion_bps": 2.0,
                "underlying_flow_agreement": False,
                "source_flow_scores": {
                    "coinbase_ws": 0.50,
                    "kraken_rest_l2": -0.20,
                },
            },
            {"book_imbalance": 0.40, "trade_flow_60s": 0.30},
        )
        self.assertEqual(review["direction"], "yes")
        self.assertEqual(review["source_count"], 3)
        self.assertEqual(review["confirming_source_count"], 2)
        self.assertEqual(review["yes_source_count"], 2)
        self.assertEqual(review["no_source_count"], 1)


class CryptoGuardAndProbabilityTests(unittest.TestCase):
    def settings(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_DISAGREEMENT_GUARD_ENABLED": "true",
            "CRYPTO_15M_MAX_UNCONFIRMED_MARKET_GAP": "12",
            "CRYPTO_15M_FLOW_CONFIRMATION_MIN_STRENGTH": "0.22",
            "CRYPTO_15M_FLOW_CONFIRMATION_MIN_SOURCES": "2",
            "CRYPTO_15M_FLOW_CONFIRMATION_MAX_DISPERSION_BPS": "20",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED": "true",
            "CRYPTO_15M_DIRECTIONAL_CONFIRMATION_MIN_SOURCES": "2",
        })
        return settings

    def test_large_disagreement_is_blocked_without_two_confirming_sources(self):
        candidate = {
            "side": "yes",
            "probability": {"raw_market_gap": 18},
            "flow_review": {
                "direction": "yes",
                "strength": 0.40,
                "source_count": 3,
                "confirming_source_count": 1,
                "yes_source_count": 1,
                "cross_exchange_dispersion_bps": 2,
            },
        }
        review = crypto.crypto_15m_disagreement_review(self.settings(), candidate)
        self.assertFalse(review["ok"])
        self.assertEqual(review["reason"], "model_market_disagreement_unconfirmed")

    def test_large_disagreement_passes_with_two_aligned_live_sources(self):
        candidate = {
            "side": "no",
            "probability": {"raw_market_gap": 18},
            "flow_review": {
                "direction": "no",
                "strength": 0.40,
                "source_count": 3,
                "confirming_source_count": 2,
                "no_source_count": 2,
                "cross_exchange_dispersion_bps": 2,
            },
        }
        review = crypto.crypto_15m_disagreement_review(self.settings(), candidate)
        self.assertTrue(review["ok"])
        self.assertTrue(review["flow_confirmed"])

    def test_directional_gate_rejects_weak_aggregate_flow(self):
        candidate = {
            "side": "yes",
            "flow_review": {
                "direction": "yes",
                "strength": 0.08,
                "source_count": 3,
                "yes_source_count": 2,
                "no_source_count": 1,
            },
        }
        review = crypto.crypto_15m_directional_confirmation_review(
            self.settings(),
            candidate,
        )
        self.assertFalse(review["ok"])
        self.assertEqual(review["selected_side_source_count"], 2)
        self.assertFalse(review["flow_strength_passes"])

    def test_directional_gate_accepts_two_sources_with_matching_strong_flow(self):
        candidate = {
            "side": "yes",
            "flow_review": {
                "direction": "yes",
                "strength": 0.10,
                "source_count": 3,
                "yes_source_count": 2,
                "no_source_count": 1,
            },
        }
        review = crypto.crypto_15m_directional_confirmation_review(
            self.settings(),
            candidate,
        )
        self.assertTrue(review["ok"])
        self.assertTrue(review["flow_direction_matches"])
        self.assertTrue(review["flow_strength_passes"])

    def test_directional_gate_rejects_one_source_aligned_with_pick(self):
        candidate = {
            "side": "no",
            "flow_review": {
                "direction": "yes",
                "strength": 0.30,
                "source_count": 3,
                "yes_source_count": 2,
                "no_source_count": 1,
            },
        }
        review = crypto.crypto_15m_directional_confirmation_review(
            self.settings(),
            candidate,
        )
        self.assertFalse(review["ok"])
        self.assertEqual(review["reason"], "directional_opposition")
        self.assertEqual(review["selected_side_source_count"], 1)

    def test_campaign_candidate_records_directional_skip_reason(self):
        candidate = {
            "ticker": "KXBTC15M-WINDOW",
            "side": "yes",
            "flow_review": {
                "direction": "no",
                "strength": 0.40,
                "source_count": 3,
                "yes_source_count": 0,
                "no_source_count": 3,
            },
            "disagreement_guard": {"enabled": True, "ok": True},
        }
        with patch.object(
            crypto,
            "crypto_15m_campaign_review",
            return_value={"eligible": True},
        ):
            self.assertFalse(
                crypto.candidate_passes(self.settings(), {}, candidate)
            )
        self.assertIn(
            "directional_opposition",
            candidate["skip_reasons"],
        )

    def test_directional_reason_is_hidden_when_base_campaign_filter_failed(self):
        candidate = {
            "ticker": "KXBTC15M-WINDOW",
            "side": "yes",
            "flow_review": {
                "direction": "no",
                "strength": 0.40,
                "source_count": 3,
                "yes_source_count": 0,
                "no_source_count": 3,
            },
            "disagreement_guard": {"enabled": True, "ok": True},
        }
        with patch.object(
            crypto,
            "crypto_15m_campaign_review",
            return_value={
                "eligible": False,
                "reason": "campaign_quality_filter",
            },
        ):
            self.assertFalse(
                crypto.candidate_passes(self.settings(), {}, candidate)
            )
        self.assertEqual(
            candidate["skip_reasons"],
            ["campaign_quality_filter"],
        )
        self.assertNotIn("directional_confirmation", candidate)

    def test_dynamic_basis_uses_half_dispersion_with_configured_ceiling(self):
        now = datetime.now(timezone.utc)
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_DYNAMIC_BASIS_BUFFER_ENABLED": "true",
            "CRYPTO_15M_SETTLEMENT_BASIS_BUFFER_BPS": "3",
            "CRYPTO_15M_MAX_SETTLEMENT_BASIS_BUFFER_BPS": "20",
        })
        market = {
            "close_time": (now + timedelta(minutes=5)).isoformat(),
            "floor_strike": 100.0,
            "yes_bid": 45,
            "yes_ask": 47,
            "no_bid": 52,
            "no_ask": 54,
        }
        model = {
            "spot": 100.0,
            "minute_vol": 0.001,
            "cross_exchange_dispersion_bps": 30,
            "trend_drift_per_minute": 0,
            "reversion_drift_per_minute": 0,
            "drift_per_minute": 0,
            "fakeout_risk": 0,
        }
        probability = crypto.probability_for_15m_market(
            market, model, settings=settings, now=now
        )
        self.assertEqual(probability["basis_buffer_bps"], 15.0)

    def test_15m_probability_exposes_canonical_settlement_mapping(self):
        now = datetime.now(timezone.utc)
        settings = dict(crypto.DEFAULT_SETTINGS)
        model = crypto.estimate_15m_asset_model({
            "spot": 100.0,
            "closes": [100.0] * 90,
            "source": "coinbase",
            "settlement_proxy_used": True,
            "microstructure": {"settlement_proxy_source_count": 2},
        }, settings=settings)
        probability = crypto.probability_for_15m_market({
            "close_time": (now + timedelta(minutes=5)).isoformat(),
            "floor_strike": 100.0,
            "yes_bid": 49.0,
            "yes_ask": 51.0,
            "no_bid": 49.0,
            "no_ask": 51.0,
        }, model, settings=settings, now=now)
        self.assertEqual(model["settlement_source"], "CF Benchmarks")
        self.assertEqual(probability["settlement_source"], "CF Benchmarks")
        self.assertEqual(
            probability["effective_target"],
            probability["effective_remaining_window_target"],
        )

    def test_proxy_alignment_preserves_returns_and_removes_exchange_basis(self):
        asset_row = {
            "spot": 100.0,
            "closes": [98.0, 99.0, 100.0],
        }
        crypto.align_asset_history_to_proxy(asset_row, 101.0)
        self.assertEqual(asset_row["coinbase_spot"], 100.0)
        self.assertEqual(asset_row["spot"], 101.0)
        self.assertAlmostEqual(asset_row["closes"][-1], 101.0)
        original_return = 100.0 / 99.0
        aligned_return = asset_row["closes"][-1] / asset_row["closes"][-2]
        self.assertAlmostEqual(aligned_return, original_return)

    def test_active_learning_updates_the_guard_gap_from_learned_probability(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_LEARNING_ENABLED": "true",
            "CRYPTO_15M_LEARNING_ACTIVE_ENABLED": "true",
            "CRYPTO_15M_UNIT_WIN_PROB_MODEL_WEIGHT": "0.35",
        })
        probability = {
            "prob": 52.0,
            "pre_anchor_prob": 55.0,
            "market_implied_yes": 50.0,
            "market_anchor_weight": 0.425,
            "raw_market_gap": 5.0,
        }
        candidate = {}
        with patch.object(crypto, "predict_15m_candidate", return_value={
            "available": True,
            "active": True,
            "qualified_for_activation": True,
            "interval_qualified": True,
            "prob_yes": 90.0,
            "prob_yes_low": 82.0,
            "prob_yes_high": 96.0,
            "evaluation": {
                "test_markets": 200,
                "market_weighted_market_baseline": {"brier": 0.20},
                "market_weighted_calibrated_ensemble": {"brier": 0.15},
                "after_fee_policy": {"roi": 0.10},
            },
        }):
            result = crypto.apply_15m_learning_prediction(
                settings, candidate, probability, {"active": True}
            )
        self.assertEqual(result["heuristic_raw_market_gap"], 5.0)
        self.assertEqual(result["raw_market_gap"], 40.0)
        self.assertEqual(result["adaptive_model_weight"], 0.35)
        self.assertEqual(result["prob"], 64.0)
        self.assertEqual(
            candidate["learned_15m_model"][
                "prediction_market_probability_yes"
            ],
            50.0,
        )

    def test_losing_learned_model_cannot_move_live_probability(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        probability = {
            "prob": 60.0,
            "market_implied_yes": 50.0,
            "raw_market_gap": 10.0,
        }
        learned = {
            "available": True,
            "active": False,
            "prob_yes": 95.0,
            "evaluation": {
                "test_markets": 100,
                "market_weighted_market_baseline": {"brier": 0.18},
                "market_weighted_calibrated_ensemble": {"brier": 0.25},
                "after_fee_policy": {"roi": -0.20},
            },
        }
        with patch.object(crypto, "predict_15m_candidate", return_value=learned):
            result = crypto.apply_15m_learning_prediction(
                settings,
                {},
                probability,
                {"status": "shadow"},
            )
        self.assertEqual(result["adaptive_probability_source"], "guardrailed_heuristic_residual")
        self.assertEqual(result["adaptive_model_weight"], 0.0)
        self.assertEqual(result["adaptive_residual_weight"], 0.10)
        self.assertEqual(result["prob"], 51.0)


class CryptoLearningPipelineTests(unittest.TestCase):
    def candidate(self, ticker, close_time):
        return {
            "ticker": ticker,
            "event_ticker": ticker,
            "asset": "BTC",
            "close_time": close_time,
            "is_15m_market": True,
            "minutes_to_close": 5.2,
            "model_prob_yes": 58.0,
            "model": {"minute_vol": 0.001},
            "probability": {
                "market_implied_yes": 55.0,
                "pre_anchor_prob": 60.0,
                "target_distance_sigma": 0.2,
            },
            "microstructure": {},
            "kalshi_microstructure": {},
        }

    def test_snapshots_dedupe_then_resolve_to_append_only_dataset(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / "pending.json"
            dataset = Path(directory) / "dataset.jsonl"
            candidate = self.candidate(
                "KXBTC15M-TEST",
                (now - timedelta(minutes=3)).isoformat(),
            )
            first = learning.record_pending_snapshots(
                [candidate], pending, now=now - timedelta(minutes=4)
            )
            second = learning.record_pending_snapshots(
                [candidate], pending, now=now - timedelta(minutes=3, seconds=30)
            )
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            result = learning.resolve_pending_snapshots(
                "https://example.invalid",
                pending,
                dataset,
                now=now,
                settlement_grace_minutes=2,
                result_fetcher=lambda _ticker: "yes",
            )
            self.assertEqual(result["resolved_markets"], 1)
            self.assertEqual(result["resolved_snapshots"], 1)
            row = json.loads(dataset.read_text(encoding="utf-8").strip())
            self.assertEqual(row["label_yes"], 1)
            self.assertEqual(row["ticker"], "KXBTC15M-TEST")

    def test_market_split_never_leaks_one_contract_across_partitions(self):
        rows = []
        for market_index in range(20):
            for snapshot_index in range(3):
                rows.append({
                    "ticker": f"T{market_index:02d}",
                    "close_time": f"2026-01-{market_index + 1:02d}T00:00:00+00:00",
                    "features": {},
                    "label_yes": market_index % 2,
                    "snapshot_index": snapshot_index,
                })
        train, validation, test = learning.unique_market_split(rows)
        groups = [
            {row["ticker"] for row in partition}
            for partition in (train, validation, test)
        ]
        self.assertFalse(groups[0] & groups[1])
        self.assertFalse(groups[0] & groups[2])
        self.assertFalse(groups[1] & groups[2])

    def test_each_market_receives_one_total_training_weight(self):
        rows = (
            [{"ticker": "MANY"} for _index in range(20)]
            + [{"ticker": "ONE"}]
            + [{"ticker": "THREE"} for _index in range(3)]
        )
        weights = learning.market_sample_weights(rows)
        totals = {}
        for row, weight in zip(rows, weights):
            totals[row["ticker"]] = totals.get(row["ticker"], 0.0) + weight
        self.assertEqual(set(totals), {"MANY", "ONE", "THREE"})
        for total in totals.values():
            self.assertAlmostEqual(total, 1.0)

    def test_zero_residual_model_preserves_market_probability(self):
        model = {
            "type": "market_residual_logistic",
            "features": list(learning.RESIDUAL_FEATURE_NAMES),
            "means": [0.0] * len(learning.RESIDUAL_FEATURE_NAMES),
            "stds": [1.0] * len(learning.RESIDUAL_FEATURE_NAMES),
            "weights": [0.0] * len(learning.RESIDUAL_FEATURE_NAMES),
            "intercept": 0.0,
        }
        row = {
            "market_probability": 0.73,
            "features": {name: 0.0 for name in learning.FEATURE_NAMES},
        }
        self.assertAlmostEqual(learning.predict_logistic(model, row), 0.73)

    def test_empirical_probability_interval_is_market_weighted_and_ordered(self):
        rows = []
        probabilities = []
        labels = []
        for market in range(80):
            snapshots = 10 if market == 0 else 1
            for _snapshot in range(snapshots):
                rows.append({
                    "ticker": f"T{market:03d}",
                    "asset": "BTC",
                    "minute_bucket": "4-8",
                })
                probabilities.append(0.65)
                labels.append(1 if market < 52 else 0)
        calibration = learning.fit_empirical_probability_calibration(
            probabilities,
            labels,
            rows,
        )
        interval = learning.empirical_probability_interval(
            calibration,
            rows[-1],
            0.65,
        )
        self.assertTrue(interval["available"])
        self.assertLessEqual(interval["low"], interval["probability"])
        self.assertLessEqual(interval["probability"], interval["high"])
        self.assertEqual(interval["markets"], 80)

    def test_sparse_empirical_bin_preserves_uncalibrated_probability(self):
        rows = [
            {
                "ticker": f"T{market:03d}",
                "asset": "BTC",
                "minute_bucket": "4-8",
            }
            for market in range(4)
        ]
        calibration = learning.fit_empirical_probability_calibration(
            [0.46] * len(rows),
            [1, 0, 1, 0],
            rows,
        )
        interval = learning.empirical_probability_interval(
            calibration,
            rows[0],
            0.46,
        )
        self.assertFalse(interval["available"])
        self.assertEqual(interval["reason"], "insufficient_exact_bin_markets")
        self.assertEqual(interval["markets"], 4)
        self.assertEqual(interval["minimum_markets"], 30)
        self.assertAlmostEqual(interval["probability"], 0.46)
        self.assertAlmostEqual(interval["low"], 0.46)
        self.assertAlmostEqual(interval["high"], 0.46)

    def test_empirical_interval_does_not_borrow_a_distant_bin(self):
        rows = [
            {
                "ticker": f"T{market:03d}",
                "asset": "BTC",
                "minute_bucket": "4-8",
            }
            for market in range(40)
        ]
        calibration = learning.fit_empirical_probability_calibration(
            [0.85] * len(rows),
            [1 if market < 30 else 0 for market in range(40)],
            rows,
        )
        interval = learning.empirical_probability_interval(
            calibration,
            rows[0],
            0.46,
        )
        self.assertFalse(interval["available"])
        self.assertAlmostEqual(interval["probability"], 0.46)

    def test_small_dataset_stays_collecting_and_cannot_affect_bets(self):
        rows = []
        for market_index in range(39):
            features = {name: 0.0 for name in learning.FEATURE_NAMES}
            rows.append({
                "ticker": f"T{market_index:03d}",
                "asset": "BTC",
                "minute_bucket": "4-8",
                "close_time": f"2026-01-{(market_index % 28) + 1:02d}T00:00:00+00:00",
                "market_probability": 0.5,
                "heuristic_probability": 0.5,
                "features": features,
                "label_yes": market_index % 2,
            })
        state = learning.train_walk_forward_state(
            rows,
            minimum_shadow_markets=40,
            minimum_active_markets=300,
        )
        self.assertEqual(state["status"], "collecting")
        self.assertFalse(state["active"])

    def test_shadow_model_trains_but_does_not_activate_before_300_markets(self):
        rows = []
        for market_index in range(50):
            label = market_index % 2
            features = {name: 0.0 for name in learning.FEATURE_NAMES}
            features["market_probability"] = 0.75 if label else 0.25
            features["heuristic_probability"] = 0.70 if label else 0.30
            rows.append({
                "ticker": f"T{market_index:03d}",
                "asset": "BTC",
                "minute_bucket": "4-8",
                "close_time": (
                    datetime(2026, 1, 1, tzinfo=timezone.utc)
                    + timedelta(minutes=15 * market_index)
                ).isoformat(),
                "market_probability": features["market_probability"],
                "heuristic_probability": features["heuristic_probability"],
                "features": features,
                "label_yes": label,
            })
        state = learning.train_walk_forward_state(
            rows,
            minimum_shadow_markets=40,
            minimum_active_markets=300,
        )
        self.assertEqual(state["status"], "shadow")
        self.assertFalse(state["active"])
        self.assertIn("evaluation", state)

    def test_collecting_state_refreshes_when_new_labels_arrive(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.jsonl"
            state_file = Path(directory) / "state.json"
            features = {name: 0.0 for name in learning.FEATURE_NAMES}
            row = {
                "feature_schema_version": learning.FEATURE_SCHEMA_VERSION,
                "ticker": "T001",
                "asset": "BTC",
                "minute_bucket": "4-8",
                "close_time": "2026-01-01T00:00:00+00:00",
                "market_probability": 0.5,
                "heuristic_probability": 0.5,
                "features": features,
                "label_yes": 1,
            }
            state_file.write_text(json.dumps({
                "generated_at": "2020-01-01T00:00:00+00:00",
                "status": "collecting",
                "active": False,
                "rows": 0,
                "unique_markets": 0,
            }), encoding="utf-8")
            dataset.write_text(json.dumps(row) + "\n", encoding="utf-8")
            state = learning.load_or_train_state(
                dataset,
                state_file,
                retrain_minutes=10_000_000,
                minimum_shadow_markets=40,
                minimum_active_markets=300,
            )
            self.assertEqual(state["rows"], 1)
            self.assertEqual(state["unique_markets"], 1)

    def test_centered_calibration_cannot_learn_directional_base_rate_shift(self):
        calibrator = learning.centered_calibrator({
            "slope": 1.4,
            "intercept": 0.8,
            "samples": 100,
        })
        self.assertEqual(calibrator["slope"], 1.4)
        self.assertEqual(calibrator["intercept"], 0.0)
        self.assertEqual(calibrator["mode"], "centered_slope_only")
        self.assertAlmostEqual(learning.apply_platt(0.5, calibrator), 0.5)

    def test_confidence_bucket_is_symmetric_for_yes_and_no_predictions(self):
        self.assertEqual(learning.confidence_bucket(0.57), "55-60")
        self.assertEqual(learning.confidence_bucket(0.43), "55-60")
        self.assertEqual(learning.confidence_bucket(0.62), "60-65")
        self.assertEqual(learning.confidence_bucket(0.38), "60-65")

    def test_midrange_calibration_measures_selected_side_accuracy(self):
        result = learning.selected_side_calibration(
            [0.58, 0.42, 0.62, 0.38, 0.80],
            [1, 0, 0, 1, 1],
            low=0.55,
            high=0.65,
        )
        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["markets"], 4)
        self.assertEqual(result["accuracy"], 0.5)
        self.assertGreater(result["calibration_gap"], 0.0)

    def test_force_shadow_prevents_a_qualified_model_from_affecting_live_bets(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings["CRYPTO_15M_ML_FORCE_SHADOW"] = "true"
        settings["CRYPTO_15M_ML_BACKGROUND_TRAINING"] = "false"
        with tempfile.TemporaryDirectory() as directory, patch.object(
            crypto,
            "CRYPTO_15M_MODEL_STATE_FILE",
            Path(directory) / "model.json",
        ), patch.object(
            crypto,
            "resolve_15m_training_snapshots",
            return_value={"resolved_markets": 0, "resolved_snapshots": 0, "pending": 0},
        ), patch.object(
            crypto,
            "load_or_train_15m_model",
            return_value={
                "status": "active",
                "active": True,
                "activation_reason": "walk_forward_outperforms_market",
            },
        ):
            state, _resolution = crypto.run_15m_learning_cycle(settings)
        self.assertFalse(state["active"])
        self.assertEqual(state["status"], "shadow")
        self.assertTrue(state["qualified_for_activation"])
        self.assertEqual(state["activation_reason"], "forced_shadow_for_live_calibration")

    def test_runtime_log_rotation_moves_large_file_without_loading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crypto_live_events.jsonl"
            path.write_bytes(b"x" * 20)
            rotated = crypto.rotate_append_file(
                path,
                incoming_bytes=1,
                max_bytes=10,
                backup_count=2,
            )
            self.assertIsNotNone(rotated)
            self.assertTrue(rotated.exists())
            self.assertFalse(path.exists())
            self.assertEqual(rotated.stat().st_size, 20)

    def test_blend_probability_uses_validation_selected_weights(self):
        self.assertAlmostEqual(
            learning.blend_probability(
                0.8,
                0.2,
                {"logistic": 0.75, "boosted": 0.25},
            ),
            0.65,
        )


if __name__ == "__main__":
    unittest.main()
