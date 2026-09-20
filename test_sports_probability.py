import unittest
from datetime import datetime, timezone

import sports_paper_bettor as sports
from sports_probability import (
    PRICING_VERSION,
    VALIDATION_VERSION,
    apply_beta_calibration,
    blend_with_kalshi,
    build_calibration_state,
    build_consensus,
    calibration_profile,
    calibration_weight,
    probability_distribution,
    probability_aware_sizing,
    remove_vig,
    settlement_time_bucket,
)


NOW = datetime(2026, 7, 19, 15, 0, tzinfo=timezone.utc)


def book(key, *, home=-150, away=130, spread_home=-110, spread_away=-110):
    return {
        "key": key,
        "last_update": "2026-07-19T14:59:30Z",
        "markets": [
            {
                "key": "h2h",
                "last_update": "2026-07-19T14:59:30Z",
                "outcomes": [
                    {"name": "Home", "price": home},
                    {"name": "Away", "price": away},
                ],
            },
            {
                "key": "spreads",
                "last_update": "2026-07-19T14:59:30Z",
                "outcomes": [
                    {"name": "Home", "point": -2.5, "price": spread_home},
                    {"name": "Away", "point": 2.5, "price": spread_away},
                ],
            },
            {
                "key": "totals",
                "last_update": "2026-07-19T14:59:30Z",
                "outcomes": [
                    {"name": "Over", "point": 8.5, "price": -105},
                    {"name": "Under", "point": 8.5, "price": -115},
                ],
            },
        ],
    }


class SportsProbabilityTests(unittest.TestCase):
    def probability_pricing(self, probability, *, uncertainty=2.0, p_edge=0.99, events=100, bias=0.0):
        return {
            "ok": True,
            "fair_probability": probability,
            "uncertainty_pp": uncertainty,
            "empirical_uncertainty_pp": uncertainty,
            "p_edge_positive": p_edge,
            "calibration_source": "global",
            "calibration_diagnostics": {
                "effective_event_count": events,
                "calibration_bias_pp": bias,
            },
        }

    def test_probability_sizing_promotes_well_priced_calibrated_favorite(self):
        review = probability_aware_sizing(
            self.probability_pricing(72.0),
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=1,
            quality_cap_units=5,
            max_units=5,
        )
        self.assertTrue(review["active"])
        self.assertEqual(3, review["target_units"])
        self.assertEqual("promoted_by_probability_kelly", review["reason"])
        self.assertGreater(review["sizing_probability"], 70)
        self.assertEqual(3, review["promotion_cap_units"])

    def test_probability_sizing_reduces_high_variance_longshot(self):
        review = probability_aware_sizing(
            self.probability_pricing(30.0),
            entry_price_cents=25,
            bankroll=1000,
            unit_size=20,
            quality_units=5,
            quality_cap_units=5,
            max_units=5,
        )
        self.assertEqual(0.5, review["target_units"])
        self.assertEqual("reduced_by_probability_kelly", review["reason"])
        self.assertEqual(2, review["longshot_cap_units"])
        self.assertEqual(0.5, review["unit_increment"])

    def test_probability_sizing_applies_shrunk_calibration_bias_and_interval(self):
        review = probability_aware_sizing(
            self.probability_pricing(70.0, uncertainty=4.0, events=50, bias=10.0),
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=3,
            quality_cap_units=5,
            max_units=5,
            calibration_bias_prior_events=50,
        )
        self.assertEqual(65.0, review["calibrated_win_probability"])
        self.assertLess(review["lower_win_probability_90"], 60)
        self.assertLess(review["sizing_probability"], review["calibrated_win_probability"])

    def test_probability_sizing_does_not_promote_without_calibration_support(self):
        review = probability_aware_sizing(
            self.probability_pricing(72.0, p_edge=0.60, events=3),
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=1,
            quality_cap_units=5,
            max_units=5,
        )
        self.assertFalse(review["promotion_ready"])
        self.assertEqual(1, review["target_units"])

    def test_probability_sizing_falls_back_to_quality_units_without_v2_pricing(self):
        review = probability_aware_sizing(
            {"ok": False},
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=4,
            quality_cap_units=5,
            max_units=5,
        )
        self.assertFalse(review["active"])
        self.assertEqual(4, review["target_units"])

    def test_walk_forward_gate_caps_unvalidated_size_at_half_unit(self):
        pricing = self.probability_pricing(72.0)
        pricing["calibrated_ensemble_probability"] = 80.0
        pricing["calibration_diagnostics"]["walk_forward_validation"] = {
            "promotion_validated": False,
        }
        review = probability_aware_sizing(
            pricing,
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=1,
            quality_cap_units=5,
            max_units=5,
            require_walk_forward_validation=True,
        )
        self.assertEqual(0.5, review["target_units"])
        self.assertEqual("reduced_by_walk_forward_validation", review["reason"])
        self.assertEqual(0.5, review["walk_forward_size_cap_units"])
        self.assertFalse(review["promotion_ready"])
        self.assertFalse(review["beta_calibration_applied"])

    def test_walk_forward_validated_segment_can_size_above_half_unit(self):
        pricing = self.probability_pricing(72.0)
        pricing["calibration_source"] = "sport_market:baseball mlb|moneyline"
        pricing["calibration_diagnostics"]["walk_forward_validation"] = {
            "promotion_validated": True,
            "validation_version": VALIDATION_VERSION,
            "promotion_enabled": True,
        }
        review = probability_aware_sizing(
            pricing,
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=1,
            quality_cap_units=5,
            max_units=5,
            require_walk_forward_validation=True,
        )
        self.assertEqual(3, review["target_units"])
        self.assertTrue(review["walk_forward_promotion_validated"])
        self.assertEqual(5, review["walk_forward_size_cap_units"])

    def test_global_walk_forward_validation_does_not_unlock_segment_size(self):
        pricing = self.probability_pricing(72.0)
        pricing["calibration_diagnostics"]["walk_forward_validation"] = {
            "promotion_validated": True,
            "validation_version": VALIDATION_VERSION,
            "promotion_enabled": True,
        }
        review = probability_aware_sizing(
            pricing,
            entry_price_cents=60,
            bankroll=1000,
            unit_size=20,
            quality_units=3,
            quality_cap_units=5,
            max_units=5,
            require_walk_forward_validation=True,
        )
        self.assertEqual(0.5, review["target_units"])
        self.assertTrue(review["walk_forward_profile_validated"])
        self.assertFalse(review["walk_forward_promotion_validated"])

    def test_probability_distribution_reports_downside_and_positive_edge_probability(self):
        distribution = probability_distribution(
            {
                "fair_probability": 70.0,
                "calibrated_ensemble_probability": 70.0,
                "empirical_uncertainty_pp": 4.0,
            },
            entry_price_cents=60,
            estimated_fee_cents=1.0,
            estimated_slippage_cents=0.25,
            draw_count=401,
            robust_quantile=0.35,
        )
        self.assertTrue(distribution["ok"])
        self.assertLess(distribution["lower_probability_90"], distribution["median_probability"])
        self.assertLess(distribution["downside_probability_25"], distribution["median_probability"])
        self.assertGreater(distribution["posterior_p_edge_positive"], 0.8)
        self.assertEqual(61.25, distribution["all_in_price_cents"])

    def test_beta_identity_and_time_buckets(self):
        self.assertAlmostEqual(0.63, apply_beta_calibration(0.63, [1, 1, 0]), places=8)
        self.assertEqual("final_10m", settlement_time_bucket(8))
        self.assertEqual("10_to_30m", settlement_time_bucket(25))
        self.assertEqual("over_6h", settlement_time_bucket(500))

    def test_calibration_profile_prefers_sampled_conditional_context(self):
        key = "baseball mlb|total|mid live|40 to 60|final 10m"
        state = {
            "global": {"effective_event_count": 100, "book_weight": 0.2},
            "conditional_segments": {
                key: {"effective_event_count": 25, "book_weight": 0.7},
            },
        }
        selected = calibration_profile(
            state,
            sport_key="baseball_mlb",
            market_type="total",
            timing_bucket="mid_live",
            market_probability=50,
            settlement_bucket="final_10m",
            default_weight=0.5,
        )
        self.assertEqual(0.7, selected["book_weight"])
        self.assertTrue(selected["source"].startswith("conditional:"))

    def test_total_ladder_interpolates_only_between_monotone_lines(self):
        def ladder_book(key, over_low, under_low, over_high, under_high):
            return {
                "key": key,
                "last_update": "2026-07-19T14:59:30Z",
                "markets": [{
                    "key": "alternate_totals",
                    "last_update": "2026-07-19T14:59:30Z",
                    "outcomes": [
                        {"name": "Over", "point": 8.5, "price": over_low},
                        {"name": "Under", "point": 8.5, "price": under_low},
                        {"name": "Over", "point": 10.5, "price": over_high},
                        {"name": "Under", "point": 10.5, "price": under_high},
                    ],
                }],
            }

        game = {"bookmakers": [
            ladder_book("fanduel", -135, 110, 120, -145),
            ladder_book("draftkings", -130, 105, 115, -140),
        ]}
        consensus = build_consensus(
            game,
            market_type="total",
            side="Over",
            target_point=9.5,
            now=NOW,
        )
        self.assertTrue(consensus["ok"])
        self.assertEqual(2, consensus["line_ladder_interpolated_family_count"])
        self.assertGreater(consensus["line_ladder_uncertainty_pp"], 0)
        self.assertTrue(all(row["interpolated"] for row in consensus["observations"]))

        extrapolated = build_consensus(
            game,
            market_type="total",
            side="Over",
            target_point=11.5,
            now=NOW,
        )
        self.assertFalse(extrapolated["ok"])

    def test_power_devig_normalizes_inside_one_book(self):
        probabilities = remove_vig([0.6, 0.45], "power")
        self.assertAlmostEqual(sum(probabilities), 1.0, places=9)
        self.assertGreater(probabilities[0], probabilities[1])

    def test_moneyline_consensus_uses_paired_same_book_markets(self):
        game = {
            "bookmakers": [
                book("fanduel", home=-150, away=130),
                book("draftkings", home=-160, away=140),
                book("pinnacle", home=-155, away=138),
            ]
        }
        consensus = build_consensus(
            game,
            market_type="moneyline",
            selected_team="Home",
            sharp_books={"pinnacle"},
            now=NOW,
        )
        self.assertTrue(consensus["ok"])
        self.assertEqual(consensus["raw_book_count"], 3)
        self.assertEqual(consensus["independent_family_count"], 3)
        self.assertGreater(consensus["probability"], 55)
        self.assertLess(consensus["probability"], 65)
        self.assertTrue(all(row["paired_outcomes"] == 2 for row in consensus["observations"]))

    def test_correlated_skins_count_as_one_independent_family(self):
        game = {
            "bookmakers": [
                book("betonlineag"),
                book("lowvig"),
                book("pinnacle"),
            ]
        }
        consensus = build_consensus(
            game,
            market_type="moneyline",
            selected_team="Home",
            sharp_books={"pinnacle", "betonlineag"},
            now=NOW,
        )
        self.assertEqual(consensus["raw_book_count"], 3)
        self.assertEqual(consensus["independent_family_count"], 2)

    def test_normalized_underscore_aliases_collapse_to_one_family(self):
        game = {
            "bookmakers": [
                book("williamhill_us"),
                book("williamhill"),
                book("pinnacle"),
            ]
        }
        consensus = build_consensus(
            game,
            market_type="moneyline",
            selected_team="Home",
            sharp_books={"pinnacle"},
            now=NOW,
        )
        self.assertEqual(3, consensus["raw_book_count"])
        self.assertEqual(2, consensus["independent_family_count"])

    def test_spread_requires_exact_complementary_line(self):
        game = {"bookmakers": [book("fanduel"), book("draftkings"), book("pinnacle")]}
        consensus = build_consensus(
            game,
            market_type="spread",
            selected_team="Home",
            target_point=-2.5,
            sharp_books={"pinnacle"},
            now=NOW,
        )
        self.assertTrue(consensus["ok"])
        self.assertAlmostEqual(consensus["probability"], 50.0, places=1)
        missing = build_consensus(
            game,
            market_type="spread",
            selected_team="Home",
            target_point=-3.5,
            sharp_books={"pinnacle"},
            now=NOW,
        )
        self.assertFalse(missing["ok"])

    def test_alternate_markets_supply_exact_line_without_double_weighting_books(self):
        fanduel = book("fanduel")
        fanduel["markets"].append({
            "key": "alternate_totals",
            "last_update": "2026-07-19T14:59:45Z",
            "outcomes": [
                {"name": "Over", "point": 9.5, "price": 120},
                {"name": "Under", "point": 9.5, "price": -145},
            ],
        })
        draftkings = book("draftkings")
        draftkings["markets"].append({
            "key": "alternate_totals",
            "last_update": "2026-07-19T14:59:40Z",
            "outcomes": [
                {"name": "Over", "point": 9.5, "price": 115},
                {"name": "Under", "point": 9.5, "price": -140},
            ],
        })
        consensus = build_consensus(
            {"bookmakers": [fanduel, draftkings]},
            market_type="total",
            side="Over",
            target_point=9.5,
            now=NOW,
        )
        self.assertTrue(consensus["ok"])
        self.assertEqual(consensus["raw_book_count"], 2)
        self.assertEqual(consensus["independent_family_count"], 2)

    def test_unavailable_consensus_keeps_exact_line_diagnostics(self):
        consensus = build_consensus(
            {"bookmakers": [book("fanduel"), book("draftkings")]},
            market_type="total",
            side="Over",
            target_point=9.5,
            now=NOW,
        )
        pricing = blend_with_kalshi(consensus, bid_cents=40, ask_cents=42, book_weight=0.25)
        self.assertFalse(pricing["ok"])
        self.assertEqual(pricing["consensus"]["failure_diagnostics"]["paired_exact_line_books"], 0)

    def test_live_consensus_rejects_books_without_fresh_timestamps(self):
        fresh = book("fanduel")
        missing_time = book("draftkings")
        missing_time.pop("last_update")
        for market in missing_time["markets"]:
            market.pop("last_update")
        stale = book("pinnacle")
        stale["last_update"] = "2026-07-19T14:50:00Z"
        for market in stale["markets"]:
            market["last_update"] = "2026-07-19T14:50:00Z"
        consensus = build_consensus(
            {"bookmakers": [fresh, missing_time, stale]},
            market_type="moneyline",
            selected_team="Home",
            now=NOW,
            max_age_minutes=2,
            require_timestamps=True,
        )
        self.assertTrue(consensus["ok"])
        self.assertEqual(consensus["raw_book_count"], 1)
        self.assertEqual(consensus["stale_book_count"], 1)
        self.assertEqual(consensus["missing_timestamp_book_count"], 1)

    def test_isolated_extreme_book_family_is_audited_and_removed(self):
        normal = [book(key, home=-105, away=-105) for key in (
            "fanduel", "draftkings", "betmgm", "pinnacle",
            "bovada", "fanatics", "williamhill_us",
        )]
        extreme = book("mybookieag", home=-300, away=250)
        consensus = build_consensus(
            {"bookmakers": [*normal, extreme]},
            market_type="moneyline",
            selected_team="Home",
            sharp_books={"pinnacle", "fanduel", "draftkings"},
            now=NOW,
            min_uncertainty_pp=0.5,
        )
        self.assertTrue(consensus["ok"])
        self.assertEqual(8, consensus["raw_book_count"])
        self.assertEqual(8, consensus["pre_filter_independent_family_count"])
        self.assertEqual(7, consensus["independent_family_count"])
        self.assertEqual(1, consensus["outlier_book_family_count"])
        self.assertEqual("mybookieag", consensus["outlier_observations"][0]["family"])
        self.assertAlmostEqual(50.0, consensus["probability"], places=1)

    def test_broad_book_disagreement_is_not_hidden_as_an_outlier(self):
        high = [book(f"high{index}", home=-200, away=170) for index in range(3)]
        low = [book(f"low{index}", home=170, away=-200) for index in range(3)]
        consensus = build_consensus(
            {"bookmakers": [*high, *low]},
            market_type="moneyline",
            selected_team="Home",
            now=NOW,
            min_uncertainty_pp=0.5,
        )
        self.assertEqual(0, consensus["outlier_book_family_count"])
        self.assertEqual(6, consensus["independent_family_count"])
        self.assertGreater(consensus["uncertainty_pp"], 5)

    def test_half_point_floor_requires_authoritative_fresh_deep_agreement(self):
        candidate = {
            "game_started": True,
            "live_odds_age_minutes": 0.2,
            "kalshi_spread": 1.0,
            "game_state_features": {"authoritative_progress": True},
        }
        consensus = {
            "independent_family_count": 7,
            "sharp_book_count": 3,
            "average_age_minutes": 0.8,
            "sampling_half_width_pp": 1.1,
            "family_probability_range_pp": 5.0,
            "outlier_book_family_count": 1,
        }
        policy = sports.pricing_v2_uncertainty_policy(candidate, consensus)
        self.assertTrue(policy["eligible"])
        self.assertEqual(0.5, policy["floor_pp"])
        disagreement = sports.pricing_v2_uncertainty_policy(
            candidate,
            {**consensus, "family_probability_range_pp": 7.0},
        )
        self.assertFalse(disagreement["eligible"])
        self.assertGreaterEqual(disagreement["floor_pp"], 1.0)

    def test_blend_reports_conservative_lower_bound(self):
        consensus = {
            "version": PRICING_VERSION,
            "ok": True,
            "probability": 62.0,
            "uncertainty_pp": 2.0,
            "independent_family_count": 4,
        }
        blend = blend_with_kalshi(
            consensus,
            bid_cents=39,
            ask_cents=41,
            book_weight=0.25,
        )
        self.assertTrue(blend["ok"])
        self.assertAlmostEqual(blend["fair_probability"], 45.5, places=1)
        self.assertLess(blend["lower_probability"], blend["fair_probability"])
        self.assertLess(
            blend["conservative_edge_before_fee_pp"],
            blend["raw_edge_pp"],
        )

    def test_two_book_consensus_gets_full_configured_weight(self):
        consensus = {
            "version": PRICING_VERSION,
            "ok": True,
            "probability": 60.0,
            "uncertainty_pp": 2.0,
            "independent_family_count": 2,
        }
        blend = blend_with_kalshi(
            consensus,
            bid_cents=49,
            ask_cents=50,
            book_weight=0.75,
        )
        self.assertEqual(blend["effective_book_weight"], 0.75)
        self.assertAlmostEqual(blend["fair_probability"], 57.375, places=3)
        self.assertAlmostEqual(blend["uncertainty_pp"], 1.505, places=3)
        self.assertGreater(blend["conservative_edge_before_fee_pp"], 5.0)

    def test_uncertainty_floor_is_applied_once(self):
        consensus = {
            "version": PRICING_VERSION,
            "ok": True,
            "probability": 50.0,
            "uncertainty_pp": 0.0,
            "independent_family_count": 2,
        }
        blend = blend_with_kalshi(
            consensus,
            bid_cents=49,
            ask_cents=50,
            book_weight=0.75,
            uncertainty_floor_pp=1.0,
        )
        self.assertEqual(blend["uncertainty_pp"], 1.0)

    def test_calibration_learns_only_from_v2_rows_and_shrinks_weight(self):
        rows = []
        for index in range(60):
            outcome = "WIN" if index % 2 == 0 else "LOSS"
            rows.append(
                {
                    "source": "edge_scanner",
                    "result": outcome,
                    "sport_key": "baseball_mlb",
                    "market_type": "moneyline",
                    "bet_timing_bucket": "live",
                    "pricing_v2": {
                        "book_probability": 80 if outcome == "LOSS" else 20,
                        "kalshi_mid_probability": 50,
                    },
                }
            )
        state = build_calibration_state(rows, default_weight=0.25, prior_strength=50)
        self.assertEqual(state["global"]["count"], 60)
        self.assertLess(state["global"]["book_weight"], 0.25)
        weight, source = calibration_weight(
            state,
            sport_key="baseball_mlb",
            market_type="moneyline",
            timing_bucket="live",
            default_weight=0.25,
        )
        self.assertEqual(source, "baseball mlb|moneyline|live")
        self.assertLess(weight, 0.25)

    def test_calibration_excludes_capper_and_counts_games_not_tickets(self):
        base = {
            "result": "WIN",
            "source": "edge_scanner",
            "game_key": "same-game",
            "sport_key": "baseball_mlb",
            "market_type": "moneyline",
            "bet_timing_bucket": "early_live",
            "pricing_v2": {"book_probability": 55, "kalshi_mid_probability": 50},
        }
        capper = {**base, "source": "trusted_capper", "game_key": "capper-game"}
        state = build_calibration_state([base, dict(base), capper], default_weight=0.5, prior_strength=10)
        self.assertEqual(2, state["global"]["count"])
        self.assertEqual(1, state["global"]["effective_event_count"])

    def test_calibration_state_contains_beta_hierarchy_recency_and_walk_forward_audit(self):
        rows = []
        for index in range(80):
            outcome = "WIN" if index % 2 == 0 else "LOSS"
            rows.append({
                "result": outcome,
                "source": "candidate_observation",
                "game_key": f"game-{index}",
                "generated_at": f"2026-07-{1 + index // 4:02d}T{index % 4:02d}:00:00Z",
                "settled_at": f"2026-07-{1 + index // 4:02d}T{index % 4:02d}:30:00Z",
                "sport_key": "baseball_mlb",
                "market_type": "moneyline",
                "bet_timing_bucket": "early_live",
                "settlement_time_bucket": "30_to_120m",
                "entry_price": 55,
                "pricing_v2": {
                    "book_probability": 60 if outcome == "WIN" else 40,
                    "kalshi_mid_probability": 58 if outcome == "WIN" else 42,
                },
            })
        state = build_calibration_state(
            rows,
            default_weight=0.5,
            prior_strength=25,
            recency_half_life_days=30,
            walk_forward_min_train_events=30,
            walk_forward_min_validation_events=15,
        )
        self.assertEqual(PRICING_VERSION, state["version"])
        self.assertEqual(30.0, state["recency_half_life_days"])
        self.assertEqual("hierarchical_beta", state["global"]["beta_calibration"]["method"])
        self.assertTrue(state["global"]["walk_forward_validation"]["validated"])
        self.assertIn("baseball mlb", state["sports"])
        self.assertIn("baseball mlb|moneyline", state["sport_markets"])
        self.assertTrue(state["conditional_segments"])

    def test_betfair_regional_exchange_feeds_collapse_to_one_family(self):
        game = {
            "home_team": "Home",
            "away_team": "Away",
            "bookmakers": [
                book("betfair_ex_eu"),
                book("betfair_ex_uk"),
                book("pinnacle"),
            ],
        }
        consensus = build_consensus(
            game,
            market_type="moneyline",
            selected_team="Home",
            sharp_books={"betfair_ex_eu", "betfair_ex_uk", "pinnacle"},
            now=NOW,
            max_age_minutes=10,
        )
        self.assertTrue(consensus["ok"])
        self.assertEqual(2, consensus["independent_family_count"])
        self.assertEqual(2, consensus["sharp_book_count"])


if __name__ == "__main__":
    unittest.main()
