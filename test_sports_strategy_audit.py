import unittest

from sports_strategy_audit import (
    compact_qualification_packet,
    performance_summary,
    segment_performance,
)


class SportsStrategyAuditTests(unittest.TestCase):
    def test_performance_summary_uses_after_fee_profit(self):
        rows = [
            {"result": "WIN", "stake": 10, "profit": 8},
            {"result": "LOSS", "stake": 10, "profit": -11},
        ]
        summary = performance_summary(rows)
        self.assertEqual(-3, summary["profit"])
        self.assertEqual(-15, summary["roi_pct"])

    def test_segments_require_minimum_sample(self):
        rows = [{
            "result": "WIN",
            "stake": 10,
            "profit": 8,
            "entry_price": 35,
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "bet_timing_bucket": "live",
        } for _ in range(5)]
        segments = segment_performance(rows)
        self.assertEqual(1, len(segments))
        self.assertTrue(segments[0]["segment"].endswith("30-39c"))

    def test_qualification_packet_compacts_repeated_unit_gate_details(self):
        state = {
            "segments": {
                "baseball_mlb|total": {
                    "status": "shadow",
                    "eligible_max_units": 0,
                    "unit_gates": {
                        str(unit): {
                            "passed": False,
                            "blockers": ["insufficient_independent_events"],
                            "all_selected": {"event_count": 4, "profit_units": 1.2},
                        }
                        for unit in range(1, 6)
                    },
                }
            }
        }
        compact = compact_qualification_packet(state)
        segment = compact["segments"]["baseball_mlb|total"]
        self.assertNotIn("unit_gates", segment)
        self.assertEqual(4, segment["representative_evidence"]["event_count"])


if __name__ == "__main__":
    unittest.main()
