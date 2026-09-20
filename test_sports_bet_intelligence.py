import unittest
from datetime import datetime, timezone

from sports_bet_intelligence import (
    enrich_candidates,
    market_microstructure,
    update_unit_explanation,
)


def candidate(ticker="TEST-ML", market_type="moneyline", probability=62.0, line=None):
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    selected = "Home Team" if market_type != "total" else "Over 8.5"
    return {
        "kalshi_ticker": ticker,
        "game_key": "away team|home team|2026-08-16",
        "event_id": "event-1",
        "sport_key": "baseball_mlb",
        "market_type": market_type,
        "selected_team": selected,
        "total_side": "over" if market_type == "total" else "",
        "market_line": line,
        "order_side": "yes",
        "entry_bid": 55.0,
        "entry_price": 56.0,
        "estimated_fee_edge_pp": 0.6,
        "edge": probability - 56.6,
        "odds_source": "odds_api",
        "odds_fetched_at": now,
        "stream_snapshot": {
            "source": "kalshi_websocket",
            "fresh": True,
            "age_seconds": 0.5,
            "quote_received_at": now,
            "exchange_ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
            "orderbook_valid": True,
            "yes_bid": 55.0,
            "yes_ask": 56.0,
            "yes_bid_size": 20.0,
            "yes_ask_size": 15.0,
            "yes_bid_levels": [{"price": 55.0, "size": 20.0}, {"price": 54.0, "size": 30.0}],
            "yes_ask_levels": [{"price": 56.0, "size": 15.0}, {"price": 57.0, "size": 25.0}],
            "no_bid_levels": [{"price": 44.0, "size": 15.0}],
            "no_ask_levels": [{"price": 45.0, "size": 20.0}],
            "recent_trade_count_60s": 3,
            "recent_trade_contracts_60s": 12.0,
        },
        "pricing_v2": {
            "ok": True,
            "fair_probability": probability,
            "independent_outcome_probability": probability + 1,
            "kalshi_market_probability": 55.5,
            "ensemble_probability_uncalibrated": probability,
            "calibrated_ensemble_probability": probability - 0.5,
            "net_edge_pp": probability - 56.6,
            "net_conservative_edge_pp": probability - 59.0,
            "probability_distribution": {
                "median_probability": probability,
                "robust_probability": probability - 2,
                "all_in_price_cents": 56.85,
                "estimated_slippage_cents": 0.25,
                "posterior_p_edge_positive": 0.84,
            },
            "consensus": {
                "probability": probability + 1,
                "family_probability_range_pp": 2.0,
                "outlier_book_family_count": 0,
                "average_age_minutes": 0.2,
                "update_token": f"token-{ticker}",
                "observations": [
                    {"family": "pinnacle", "books": ["pinnacle"], "probability": probability + 1.5, "last_update": now},
                    {"family": "draftkings", "books": ["draftkings"], "probability": probability + 0.5, "last_update": now},
                ],
            },
        },
        "outcome_probability_challengers": {
            "independent_books": probability + 1,
            "kalshi_market": 55.5,
            "execution_probability": probability,
        },
    }


class SportsBetIntelligenceTests(unittest.TestCase):
    def test_microstructure_walks_depth_and_reports_flow(self):
        review = market_microstructure(candidate())
        self.assertEqual(review["ask_depth_2c"], 40.0)
        self.assertEqual(review["fill_scenarios"]["25"]["vwap_cents"], 56.4)
        self.assertEqual(review["recent_trade_count_60s"], 3)
        self.assertFalse(review["affects_execution"])

    def test_enrichment_adds_cross_market_joint_and_sync_reviews(self):
        moneyline = candidate(probability=62.0)
        spread = candidate("TEST-SPREAD", "spread", probability=70.0, line=-1.5)
        state, summary = enrich_candidates(
            [moneyline, spread],
            previous_state={},
            portfolio={"bets": []},
            sharp_books=("pinnacle",),
        )
        card = spread["bet_intelligence"]
        self.assertEqual(card["cross_market_consistency"]["status"], "contradiction_detected")
        self.assertEqual(card["joint_scenarios"]["pair_count"], 1)
        self.assertIn(card["source_synchronization"]["status"], {"synchronized", "usable"})
        self.assertEqual(summary["candidate_count"], 2)
        self.assertIn("TEST-SPREAD|yes", state["candidates"])

    def test_prior_family_prices_produce_steam_and_sharp_retail_signal(self):
        row = candidate(probability=62.0)
        previous = {
            "candidates": {
                "TEST-ML|yes": {
                    "consensus_probability": 60.0,
                    "families": {"pinnacle": 60.0, "draftkings": 59.5},
                    "history": [],
                }
            }
        }
        enrich_candidates(
            [row],
            previous_state=previous,
            portfolio={"bets": []},
            sharp_books=("pinnacle",),
        )
        market = row["bet_intelligence"]["sportsbook_market"]
        self.assertEqual(market["steam_direction"], "up")
        self.assertEqual(market["sharp_signal"], "sharp_higher")

    def test_unit_explanation_names_next_tier_blockers_and_fill(self):
        row = candidate()
        enrich_candidates([row], previous_state={}, portfolio={"bets": []})
        review = {
            "reason": "eligible",
            "raw_target_units": 2,
            "probability_target_units": 2,
            "target_units": 2,
            "additional_units": 2,
            "unit_size": 20,
            "requested_stake": 40,
            "metrics": {
                "qualification_edge": 6,
                "confidence": 84,
                "pro_score": 91,
                "final_score": 85,
                "independent_book_families": 3,
            },
            "requirements": {
                "3": {
                    "min_edge": 8,
                    "min_confidence": 88,
                    "min_pro_score": 95,
                    "min_final_score": 90,
                    "min_book_families": 4,
                }
            },
        }
        explanation = update_unit_explanation(row, review)
        self.assertEqual(explanation["next_unit_tier"], 3)
        self.assertEqual(len(explanation["next_unit_blockers"]), 5)
        self.assertGreater(
            row["bet_intelligence"]["market_microstructure"]["requested_stake_fill"]["contracts"],
            70,
        )

    def test_unit_explanation_selects_next_half_unit_tier(self):
        row = candidate()
        enrich_candidates([row], previous_state={}, portfolio={"bets": []})
        review = {
            "reason": "eligible",
            "raw_target_units": 2.0,
            "probability_target_units": 2.0,
            "target_units": 2.0,
            "additional_units": 2.0,
            "unit_size": 20,
            "requested_stake": 40,
            "metrics": {
                "qualification_edge": 1.25,
                "confidence": 78,
                "pro_score": 91,
                "final_score": 86,
                "independent_book_families": 2,
            },
            "requirements": {
                "2.5": {
                    "min_edge": 1.5,
                    "min_confidence": 79,
                    "min_pro_score": 92.5,
                    "min_final_score": 87,
                    "min_book_families": 2,
                },
                "3": {
                    "min_edge": 2,
                    "min_confidence": 82,
                    "min_pro_score": 95,
                    "min_final_score": 89,
                    "min_book_families": 2,
                },
            },
        }
        explanation = update_unit_explanation(row, review)
        self.assertEqual(2.5, explanation["next_unit_tier"])
        self.assertEqual(
            {"qualification_edge", "confidence", "pro_score", "final_score"},
            {item["metric"] for item in explanation["next_unit_blockers"]},
        )


if __name__ == "__main__":
    unittest.main()
