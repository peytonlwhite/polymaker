import unittest

import dashboard


class TrustedCapperAnalyticsTests(unittest.TestCase):
    def test_separate_units_record_price_and_comparison(self):
        open_rows = [{
            "source": "trusted_capper",
            "trusted_capper_ticket_id": "t-open",
            "status": "open",
            "stake": 40,
            "contracts": 80,
            "fee": 0,
            "unit_count": 2,
            "unit_size": 20,
            "capper_posted_odds": -110,
            "sport_key": "tennis_wta",
            "market_type": "moneyline",
        }]
        history = [
            {
                "source": "trusted_capper",
                "trusted_capper_ticket_id": "t-win",
                "result": "WIN",
                "stake": 40,
                "profit": 35,
                "contracts": 80,
                "fee": 0,
                "unit_count": 2,
                "unit_size": 20,
                "capper_posted_odds": -110,
                "sport_key": "tennis_wta",
                "market_type": "moneyline",
                "bet_timing_bucket": "pregame",
                "settled_at": "2026-08-09T15:00:00-05:00",
            },
            {
                "strategy_owner": "trusted_capper",
                "trusted_capper_ticket_id": "t-loss",
                "result": "LOSS",
                "stake": 20,
                "profit": -20,
                "contracts": 40,
                "fee": 0,
                "unit_count": 1,
                "unit_size": 20,
                "capper_posted_odds": -110,
                "sport_key": "baseball_mlb",
                "market_type": "moneyline",
                "game_started": True,
                "settled_at": "2026-08-09T16:00:00-05:00",
            },
            {
                "source": "edge_scanner",
                "strategy_owner": "live_campaign",
                "result": "WIN",
                "stake": 20,
                "profit": 18,
                "unit_count": 1,
                "unit_size": 20,
                "settled_at": "2026-08-09T17:00:00-05:00",
            },
        ]
        tickets = [
            {
                "ticket_id": "t-open", "executable": True, "status": "placed", "capper_units": 4,
                "selection": "Open pick",
                "native_counterfactual": {
                    "evaluated_at": "2026-08-09T14:00:00-05:00",
                    "status": "native_select",
                    "would_native_select": True,
                    "reasons": [],
                    "components": [{"edge": 4.0, "confidence": 90, "pro_score": 95, "final_score": 92, "independent_book_families": 4}],
                },
            },
            {
                "ticket_id": "t-win", "executable": True, "status": "settled", "capper_units": 4,
                "selection": "Win pick",
                "native_counterfactual": {
                    "evaluated_at": "2026-08-09T13:00:00-05:00",
                    "status": "native_reject",
                    "would_native_select": False,
                    "reasons": ["below_edge"],
                    "components": [{"edge": -2.0, "confidence": 88, "pro_score": 80, "final_score": 78, "independent_book_families": 3}],
                },
            },
            {"ticket_id": "t-loss", "executable": True, "status": "settled", "capper_units": 3},
            {"ticket_id": "track", "executable": False, "status": "unsupported", "capper_units": 4},
        ]

        result = dashboard.trusted_capper_analytics(open_rows, history, tickets, 20)

        self.assertEqual(1, result["wins"])
        self.assertEqual(1, result["losses"])
        self.assertEqual(50.0, result["win_rate"])
        self.assertEqual(3.0, result["risked_units"])
        self.assertEqual(0.75, result["net_units"])
        self.assertEqual(15.0, result["profit"])
        self.assertEqual(15.0, result["account_profit"])
        self.assertEqual(2, result["account_settled_positions"])
        self.assertEqual(1, result["account_open_positions"])
        self.assertEqual(25.0, result["roi"])
        self.assertEqual(2.0, result["open_units"])
        self.assertEqual(40.0, result["open_exposure"])
        self.assertEqual(3, result["executable_tickets"])
        self.assertEqual(1, result["track_only_tickets"])
        self.assertEqual(3, result["placed_tickets"])
        self.assertEqual(100.0, result["ticket_fill_rate"])
        self.assertAlmostEqual(2.38, result["average_price_improvement_pp"], places=2)
        self.assertEqual(["InfluencedBets", "Regular sports bot"], [row["label"] for row in result["comparison"]])
        counterfactual = result["native_counterfactual"]
        self.assertEqual(2, counterfactual["evaluated_tickets"])
        self.assertEqual(1, counterfactual["native_selected_tickets"])
        self.assertEqual(50.0, counterfactual["native_selection_rate_pct"])
        self.assertEqual("below_edge", counterfactual["reason_counts"][0]["reason"])

    def test_empty_analytics_are_zero_safe(self):
        result = dashboard.trusted_capper_analytics([], [], [], 20)
        self.assertEqual(0, result["settled_positions"])
        self.assertEqual(0.0, result["net_units"])
        self.assertIsNone(result["average_price_improvement_pp"])

    def test_identity_invalid_result_is_excluded_but_reported_separately(self):
        history = [{
            "source": "trusted_capper",
            "trusted_capper_ticket_id": "bad-match",
            "result": "WIN",
            "stake": 100,
            "profit": 161.08,
            "unit_count": 5,
            "unit_size": 20,
            "strategy_analytics_excluded": True,
        }]

        result = dashboard.trusted_capper_analytics([], history, [], 20)

        self.assertEqual(0, result["settled_positions"])
        self.assertEqual(0.0, result["profit"])
        self.assertEqual(1, result["strategy_integrity_excluded_positions"])
        self.assertEqual(161.08, result["strategy_integrity_excluded_profit"])
        self.assertEqual(161.08, result["account_profit"])
        self.assertEqual(1, result["account_settled_positions"])


if __name__ == "__main__":
    unittest.main()
