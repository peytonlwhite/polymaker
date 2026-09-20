import unittest

from sports_analytics import compact_candidate_snapshot, pricing_analytics


class SportsAnalyticsTests(unittest.TestCase):
    def test_snapshot_keeps_pricing_timing_bucket(self):
        snapshot = compact_candidate_snapshot(
            {
                "kalshi_ticker": "TEST",
                "game_started": True,
                "pricing_v2": {"timing_bucket": "live_early"},
            },
            scan_id="scan",
            scan_reason="test",
        )
        self.assertEqual(snapshot["timing_bucket"], "live_early")

    def test_snapshot_records_calibrated_probability_interval_and_sizing(self):
        snapshot = compact_candidate_snapshot(
            {
                "kalshi_ticker": "PROBABILITY",
                "probability_sizing": {
                    "active": True,
                    "calibrated_win_probability": 68,
                    "lower_win_probability_90": 62,
                    "upper_win_probability_90": 74,
                    "sizing_probability": 66.5,
                    "target_units": 3,
                },
            },
            scan_id="scan",
            scan_reason="test",
        )
        self.assertEqual(68, snapshot["calibrated_win_probability"])
        self.assertEqual(62, snapshot["lower_win_probability_90"])
        self.assertEqual(74, snapshot["upper_win_probability_90"])
        self.assertEqual(66.5, snapshot["sizing_probability"])

    def test_snapshot_preserves_real_zero_values(self):
        snapshot = compact_candidate_snapshot(
            {
                "entry_price": 44,
                "kalshi_spread": 9,
                "raw_edge": 8,
                "edge": 7,
                "pricing_v2": {
                    "ask_cents": 0.0,
                    "spread_cents": 0.0,
                    "raw_edge_pp": 0.0,
                    "net_conservative_edge_pp": 0.0,
                },
            },
            scan_id="scan",
            scan_reason="test",
        )
        self.assertEqual(0.0, snapshot["entry_ask"])
        self.assertEqual(0.0, snapshot["entry_spread"])
        self.assertEqual(0.0, snapshot["raw_edge_pp"])
        self.assertEqual(0.0, snapshot["net_conservative_edge_pp"])

    def test_pricing_analytics_reports_quality_and_fixed_horizon_clv(self):
        analytics = pricing_analytics([{
            "source": "edge_scanner",
            "result": "WIN",
            "profit": 2,
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "bet_timing_bucket": "early_live",
            "confidence_score": 88,
            "pro_review": {"score": 104},
            "final_bet_score": 91,
            "pricing_v2": {
                "fair_probability": 55,
                "book_probability": 56,
                "kalshi_mid_probability": 50,
                "independent_outcome_probability": 56,
                "calibrated_ensemble_probability": 54,
                "probability_distribution": {"median_probability": 53.5},
            },
            "probability_sizing": {"calibrated_win_probability": 54},
            "fixed_horizon_clv": {
                "5m": {
                    "on_time": True,
                    "clv_vs_entry_ask_cents": 2,
                    "book_move_pp": 1.5,
                    "book_identity_valid": True,
                    "book_time_valid": True,
                    "book_identity_reason": "matched",
                }
            },
        }])
        self.assertEqual(analytics["quality_scores"]["final"]["count"], 1)
        self.assertEqual(analytics["calibrated_model"]["count"], 1)
        self.assertEqual(analytics["posterior_median"]["count"], 1)
        self.assertEqual(analytics["independent_outcome"]["count"], 1)
        self.assertEqual(
            analytics["fixed_horizon_clv"]["5m"]["average_kalshi_clv_cents"],
            2,
        )
        self.assertEqual(
            analytics["fixed_horizon_clv"]["5m"]["average_book_move_pp"],
            1.5,
        )

    def test_pricing_analytics_excludes_unverified_legacy_book_clv(self):
        analytics = pricing_analytics([{
            "source": "edge_scanner",
            "result": "LOSS",
            "pricing_v2": {"fair_probability": 50},
            "fixed_horizon_clv": {
                "5m": {
                    "on_time": True,
                    "clv_vs_entry_ask_cents": 3,
                    "book_move_pp": -12,
                }
            },
        }])
        horizon = analytics["fixed_horizon_clv"]["5m"]
        self.assertEqual(horizon["kalshi_clv_count"], 1)
        self.assertEqual(horizon["book_move_count"], 0)
        self.assertEqual(horizon["book_identity_invalid_count"], 1)
        self.assertEqual(
            horizon["book_identity_invalid_reasons"],
            {"legacy_unverified_side_line": 1},
        )

    def test_pending_book_identity_is_not_counted_as_invalid(self):
        analytics = pricing_analytics([{
            "source": "edge_scanner",
            "result": "WIN",
            "pricing_v2": {"fair_probability": 50},
            "fixed_horizon_clv": {
                "5m": {
                    "on_time": True,
                    "book_identity_valid": None,
                    "book_identity_pending": True,
                    "book_identity_reason": "matching_candidate_unavailable",
                }
            },
        }])
        horizon = analytics["fixed_horizon_clv"]["5m"]
        self.assertEqual(0, horizon["book_identity_invalid_count"])
        self.assertEqual(1, horizon["book_identity_pending_count"])
        self.assertEqual(
            {"matching_candidate_unavailable": 1},
            horizon["book_identity_pending_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
