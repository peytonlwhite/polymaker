import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import sports_paper_bettor as sports
import sports_capper_import as capper


class TrustedCapperExecutionTests(unittest.TestCase):
    def test_btts_yes_maps_to_exact_full_game_soccer_contract(self):
        draft = capper.parse_capper_text(
            "⚽️ UEFA Super Cup\n⚽️ PSG/Aston Villa BTTS YES -133 4u"
        )["drafts"][0]
        draft["target_event_date"] = "2027-08-12"
        ticket = {**draft, "ticket_id": "btts-ticket", "status": "watching"}
        market = {
            "ticker": "KXUEFASCBTTS-27AUG12PSGAVL",
            "event_ticker": "KXUEFASCBTTS-27AUG12PSGAVL",
            "series_ticker": "KXUEFASCBTTS",
            "title": "Paris Saint-Germain vs Aston Villa Both Teams to Score",
            "yes_bid": 55,
            "yes_ask": 56,
            "no_bid": 43,
            "no_ask": 45,
            "volume": 12000,
            "liquidity": 100000,
        }
        game = {
            "id": "psg-avl",
            "sport_key": "soccer_uefa_super_cup",
            "sport_title": "UEFA Super Cup",
            "home_team": "Paris Saint-Germain",
            "away_team": "Aston Villa",
            "commence_time": "2027-08-12T20:00:00Z",
            "bookmakers": [],
        }
        rows = sports.build_trusted_capper_btts_candidates([ticket], [market], [game])
        self.assertEqual(1, len(rows))
        candidate = rows[0]
        self.assertEqual("btts", candidate["market_type"])
        self.assertEqual("yes", candidate["btts_side"])
        self.assertEqual("yes", candidate["order_side"])
        self.assertEqual(56, candidate["entry_price"])
        self.assertTrue(candidate["capper_btts_special"])
        self.assertTrue(candidate["capper_event_identity_verified"])
        self.assertEqual(market["event_ticker"], candidate["event_ticker"])
        self.assertEqual("trusted_capper_posted_btts_price_only", candidate["pricing_basis"])
        self.assertEqual(0, candidate["independent_book_family_count"])
        self.assertIsNone(candidate["edge"])

    def test_btts_posted_price_does_not_override_independent_consensus(self):
        component = {
            "component_id": "btts",
            "market_type": "btts",
            "selection": "West Ham/Wolves",
            "btts_side": "yes",
        }
        ticket = {
            "ticket_id": "posted-and-consensus",
            "status": "watching",
            "sport_key": "soccer",
            "pick_type": "straight",
            "market_type": "btts",
            "selection": "West Ham/Wolves",
            "posted_odds": -165,
            "target_event_date": "2027-08-12",
            "components": [component],
        }
        market = {
            "ticker": "KXEFLCHAMPIONSHIPBTTS-27AUG12WHUWOL-BTTS",
            "event_ticker": "KXEFLCHAMPIONSHIPBTTS-27AUG12WHUWOL",
            "series_ticker": "KXEFLCHAMPIONSHIPBTTS",
            "title": "Both Teams To Score",
            "rules_primary": "West Ham and Wolverhampton both score in the match",
            "yes_bid": 54,
            "yes_ask": 55,
            "no_bid": 44,
            "no_ask": 46,
            "volume": 12000,
            "liquidity": 100000,
        }
        updated = datetime.now(timezone.utc).isoformat()
        game = {
            "id": "west-ham-wolves",
            "sport_key": "soccer_efl_champ",
            "sport_title": "Championship",
            "home_team": "West Ham United",
            "away_team": "Wolverhampton Wanderers",
            "commence_time": "2027-08-12T20:00:00Z",
            "_odds_fetched_at": updated,
            "bookmakers": [
                {
                    "key": key,
                    "last_update": updated,
                    "markets": [{
                        "key": "btts",
                        "last_update": updated,
                        "outcomes": [
                            {"name": "Yes", "price": 110},
                            {"name": "No", "price": -140},
                        ],
                    }],
                }
                for key in ("pinnacle", "fanduel")
            ],
        }

        candidate = sports.build_trusted_capper_btts_candidates(
            [ticket], [market], [game]
        )[0]

        self.assertEqual("odds_api_btts_same_book_devig", candidate["pricing_basis"])
        self.assertEqual(2, candidate["independent_book_family_count"])
        self.assertNotAlmostEqual(
            sports.american_to_prob(ticket["posted_odds"]) * 100.0,
            candidate["model_prob"],
            places=2,
        )
        self.assertIsNotNone(candidate["edge"])

    def test_fast_recheck_reprices_from_kalshi_without_creating_new_model_edge(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        priced = sports.refresh_trusted_capper_fast_candidate(
            {
                "commence_time": future,
                "order_side": "yes",
                "entry_price": 55,
                "model_prob": 60,
                "edge": 4.5,
                "live_odds_age_minutes": 0.5,
                "kalshi_liquidity": 10000,
            },
            {"yes_bid": 49, "yes_ask": 50, "no_bid": 50, "no_ask": 51},
            elapsed_minutes=0.5,
        )
        price_only = sports.refresh_trusted_capper_fast_candidate(
            {
                "commence_time": future,
                "order_side": "yes",
                "model_prob": 62,
                "edge": None,
                "kalshi_liquidity": 10000,
            },
            {"yes_bid": 49, "yes_ask": 50, "no_bid": 50, "no_ask": 51},
        )
        self.assertEqual(50, priced["entry_price"])
        self.assertGreater(priced["edge"], 0)
        self.assertEqual(1.0, priced["live_odds_age_minutes"])
        self.assertIsNone(price_only["edge"])

    def test_fast_recheck_does_not_reuse_expired_live_game_state(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()
        priced = sports.refresh_trusted_capper_fast_candidate(
            {
                "commence_time": past,
                "order_side": "yes",
                "entry_price": 55,
                "edge": 4.5,
                "estimated_fee_edge_pp": 0,
                "has_live_score_context": True,
                "game_state_features": {
                    "provider_fetched_at": stale,
                    "provider_age_seconds": 240,
                    "provider_fresh": True,
                    "progress_ready": True,
                    "authoritative_progress": True,
                    "score_available": True,
                    "state_quality": "authoritative_progress",
                },
            },
            {"yes_bid": 49, "yes_ask": 50, "no_bid": 50, "no_ask": 51},
        )
        self.assertFalse(priced["has_live_score_context"])
        self.assertFalse(priced["game_state_features"]["authoritative_progress"])
        self.assertFalse(priced["game_state_features"]["live_state_fresh"])

    def test_fast_recheck_target_refreshes_expired_live_state(self):
        candidate = {
            "event_id": "event-1",
            "sport_key": "basketball_wnba",
            "home_team": "Home",
            "away_team": "Away",
            "commence_time": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "game_started": True,
            "entry_price": 50,
            "edge": 3,
            "has_live_score_context": False,
            "game_state_features": {"live_state_fresh": False},
        }
        fetched_at = datetime.now(timezone.utc).isoformat()
        enriched = [{
            "id": "event-1",
            "sport_key": "basketball_wnba",
            "home_team": "Home",
            "away_team": "Away",
            "premium_live_state": {
                "provider": "espn_scoreboard",
                "verified_progress_source": True,
                "fetched_at": fetched_at,
                "period": 3,
                "clock": "04:20",
                "home_score": 60,
                "away_score": 58,
            },
        }]
        with patch.object(
            sports,
            "enrich_games_with_configured_live_data",
            return_value=(enriched, {"provider": "public", "enriched_games": 1, "errors": []}),
        ):
            refreshed, status = sports.refresh_trusted_capper_fast_live_state([candidate])
        self.assertEqual(1, status["refreshed_candidates"])
        self.assertTrue(refreshed[0]["has_live_score_context"])
        self.assertTrue(refreshed[0]["game_state_features"]["authoritative_progress"])

    def test_fast_recheck_lane_makes_no_odds_api_call(self):
        context = {
            "captured_epoch": sports.time.time(),
            "placement_limit": 4,
            "candidates": [{
                "kalshi_ticker": "KXTEST-ONE",
                "commence_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "order_side": "yes",
                "model_prob": 60,
                "edge": 4,
                "kalshi_liquidity": 10000,
            }],
        }
        with patch.object(sports, "load_trusted_capper_fast_context", return_value=context), patch.object(
            sports, "sports_stream", return_value=None
        ), patch.object(
            sports,
            "fetch_kalshi_market_by_ticker",
            return_value={"yes_bid": 49, "yes_ask": 50, "no_bid": 50, "no_ask": 51},
        ), patch.object(
            sports, "load_portfolio", return_value={"balance": 1000, "bets": [], "history": []}
        ), patch.object(
            sports,
            "reconcile_live_account",
            return_value={"remote_open_count": 0, "remote_market_exposure": 0},
        ), patch.object(
            sports,
            "process_trusted_capper_tickets",
            return_value={"reviewed": 1, "ready": 0, "waiting": 1, "blocked": 0, "placed": []},
        ), patch.object(sports, "load_watchlist", return_value={}), patch.object(
            sports, "save_portfolio"
        ), patch.object(sports, "odds_api_get_json") as odds_mock:
            result = sports.run_trusted_capper_fast_recheck()
        odds_mock.assert_not_called()
        self.assertEqual(0, result["odds_api_calls"])
        self.assertEqual(1, result["reviewed"])

    def test_fast_recheck_refreshes_near_eligible_live_capper_book_evidence(self):
        updated = datetime.now(timezone.utc).isoformat()
        candidate = {
            "kalshi_ticker": "KXWTAMATCH-TEST-PEG",
            "sport_key": "tennis_wta_test",
            "event_id": "pegula-event",
            "market_type": "moneyline",
            "selected_team": "Jessica Pegula",
            "commence_time": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "game_started": True,
            "game_completed": False,
            "has_live_score_context": True,
            "order_side": "yes",
            "entry_price": 50,
            "edge": -1,
            "live_odds_age_minutes": 2,
        }
        game = {
            "id": "pegula-event",
            "home_team": "Jessica Pegula",
            "away_team": "Leylah Fernandez",
            "commence_time": candidate["commence_time"],
            "bookmakers": [{
                "key": "pinnacle",
                "last_update": updated,
                "markets": [{"key": "h2h", "last_update": updated, "outcomes": []}],
            }],
        }

        def apply_pricing(_game, row, _state):
            row["edge"] = 3.25
            row["pricing_v2"] = {"ok": True, "consensus": {"ok": True}}

        with (
            patch.object(sports, "SPORTS_CAPPER_FAST_LIVE_BOOK_REFRESH_ENABLED", True),
            patch.object(sports, "ODDS_API_KEYS", ["test-key"]),
            patch.object(sports, "SPORTS_CAPPER_FAST_LIVE_BOOK_CACHE", {}),
            patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)),
            patch.object(sports, "odds_api_get_json", return_value=(game, {})) as fetch,
            patch.object(sports, "record_odds_spend"),
            patch.object(sports, "apply_pricing_v2_to_candidate", side_effect=apply_pricing),
            patch.object(sports, "apply_native_pregame_pricing_review"),
            patch.object(sports, "assign_candidate_confidence"),
        ):
            refreshed, status = sports.refresh_trusted_capper_fast_live_books([candidate], {})

        fetch.assert_called_once()
        self.assertEqual(1, status["requested_events"])
        self.assertEqual(1, status["refreshed_candidates"])
        self.assertEqual(3.25, refreshed[0]["edge"])
        self.assertTrue(refreshed[0]["fast_recheck"]["live_book_refreshed"])

    def test_candidate_evaluation_exposes_progress_checkpoints(self):
        checkpoints = []
        games = [
            {"id": "one", "sport_key": "baseball_mlb"},
            {"id": "two", "sport_key": "baseball_mlb"},
        ]
        pairs, _diagnostics = sports.evaluate_candidate_pairs_for_games(
            games,
            [],
            {},
            {},
            progress_callback=lambda: checkpoints.append(True),
        )
        self.assertEqual([], pairs)
        self.assertEqual(2, len(checkpoints))

    def test_parlay_btts_leg_uses_independent_book_consensus_without_parent_odds(self):
        component = {
            "component_id": "saudi-btts",
            "market_type": "btts",
            "selection": "Al Hilal/Al Ahli",
            "btts_side": "yes",
        }
        ticket = {
            "ticket_id": "saudi-btts-leg",
            "status": "watching",
            "sport_key": "soccer",
            "pick_type": "parlay_leg",
            "market_type": "btts",
            "selection": "Al Hilal/Al Ahli",
            "btts_side": "yes",
            "posted_odds": None,
            "target_event_date": "2027-08-12",
            "components": [component],
        }
        market = {
            "ticker": "KXSAUDIPLBTTS-27AUG12HILAHI-BTTS",
            "event_ticker": "KXSAUDIPLBTTS-27AUG12HILAHI",
            "series_ticker": "KXSAUDIPLBTTS",
            "title": "Al Hilal vs Al Ahli Both Teams to Score",
            "yes_bid": 61,
            "yes_ask": 62,
            "no_bid": 37,
            "no_ask": 39,
            "volume": 12000,
            "liquidity": 100000,
        }
        updated = datetime.now(timezone.utc).isoformat()
        game = {
            "id": "hilal-ahli",
            "sport_key": "soccer_saudi_arabia_pro_league",
            "sport_title": "Saudi Pro League",
            "home_team": "Al-Hilal",
            "away_team": "Al-Ahli",
            "commence_time": "2027-08-12T20:00:00Z",
            "_odds_fetched_at": updated,
            "bookmakers": [
                {
                    "key": key,
                    "last_update": updated,
                    "markets": [{
                        "key": "btts",
                        "last_update": updated,
                        "outcomes": [
                            {"name": "Yes", "price": -130},
                            {"name": "No", "price": 105},
                        ],
                    }],
                }
                for key in ("pinnacle", "fanduel")
            ],
        }

        rows = sports.build_trusted_capper_btts_candidates([ticket], [market], [game])

        self.assertEqual(1, len(rows))
        self.assertEqual("odds_api_btts_same_book_devig", rows[0]["pricing_basis"])
        self.assertEqual(2, rows[0]["independent_book_family_count"])
        self.assertGreater(rows[0]["model_prob"], 50)

    def test_btts_identity_accepts_wolves_alias_but_not_half_market(self):
        game = {
            "sport_key": "soccer_efl_champ",
            "home_team": "West Ham United",
            "away_team": "Wolverhampton Wanderers",
            "commence_time": "2026-09-01T18:45:00Z",
        }
        full_market = {
            "ticker": "KXEFLCHAMPIONSHIPBTTS-26SEP01WHUWOL-BTTS",
            "event_ticker": "KXEFLCHAMPIONSHIPBTTS-26SEP01WHUWOL",
            "title": "Both Teams To Score",
            "rules_primary": (
                "If West Ham and Wolverhampton both score a goal in the match, "
                "the market resolves to Yes."
            ),
        }
        half_market = {
            **full_market,
            "ticker": "KXEFLCHAMPIONSHIP1HBTTS-26SEP01WHUWOL-BTTS",
            "event_ticker": "KXEFLCHAMPIONSHIP1HBTTS-26SEP01WHUWOL",
            "title": "Both teams score in the 1st Half?",
        }
        self.assertTrue(
            sports.capper_btts_selection_matches("West Ham/Wolves", game, full_market)
        )
        self.assertEqual("btts", sports.classify_kalshi_market(full_market)["type"])
        self.assertEqual("unsupported", sports.classify_kalshi_market(half_market)["type"])
        ordinary_market = {
            "ticker": "KXEFLCHAMPIONSHIPGAME-26SEP01WHUWOL-WHU",
            "event_ticker": "KXEFLCHAMPIONSHIPGAME-26SEP01WHUWOL",
            "title": "West Ham wins",
        }
        capper_markets = sports.trusted_capper_market_universe(
            [ordinary_market],
            [ordinary_market, full_market, half_market],
        )
        self.assertEqual(
            {ordinary_market["ticker"], full_market["ticker"]},
            {market["ticker"] for market in capper_markets},
        )
        saudi_total = {
            "ticker": "KXSAUDIPLTOTAL-26SEP01HILAAS-3",
            "series_ticker": "KXSAUDIPLTOTAL",
            "title": "Will over 2.5 goals be scored?",
        }
        with patch.object(
            sports,
            "trusted_capper_requested_soccer_derivative_series",
            return_value=("KXSAUDIPLTOTAL",),
        ):
            capper_markets = sports.trusted_capper_market_universe(
                [ordinary_market],
                [ordinary_market, saudi_total],
            )
        self.assertEqual(
            {ordinary_market["ticker"], saudi_total["ticker"]},
            {market["ticker"] for market in capper_markets},
        )

    def test_soccer_event_hint_accepts_common_middlesbrough_misspelling(self):
        candidate = {
            "sport_key": "soccer_efl_champ",
            "home_team": "Burnley",
            "away_team": "Middlesbrough",
        }
        self.assertEqual(
            100,
            sports.capper_event_hint_match_score(
                "Burnley/Middlesborough",
                candidate,
            ),
        )

    def test_yrfi_maps_to_kalshi_rfi_yes_contract_before_first_pitch(self):
        draft = capper.parse_capper_text(
            "MLB Add\nRangers/Angels YRFI -125 3u"
        )["drafts"][0]
        draft["target_event_date"] = "2027-08-11"
        ticket = {
            **draft,
            "ticket_id": "yrfi-ticket",
            "status": "watching",
        }
        market = {
            "ticker": "KXMLBRFI-27AUG112355TEXLAA",
            "event_ticker": "KXMLBRFI-27AUG112355TEXLAA",
            "series_ticker": "KXMLBRFI",
            "title": "Texas vs LA Angels First Inning Run?",
            "yes_bid": 53,
            "yes_ask": 54,
            "no_bid": 45,
            "no_ask": 47,
            "volume": 10000,
            "liquidity": 100000,
        }
        game = {
            "id": "tex-laa",
            "sport_key": "baseball_mlb",
            "sport_title": "MLB",
            "home_team": "Los Angeles Angels",
            "away_team": "Texas Rangers",
            "commence_time": "2027-08-12T03:55:00Z",
            "bookmakers": [],
        }
        rows = sports.build_trusted_capper_first_inning_candidates(
            [ticket], [market], [game]
        )
        self.assertEqual(1, len(rows))
        candidate = rows[0]
        self.assertEqual("first_inning_run", candidate["market_type"])
        self.assertEqual("yrfi", candidate["first_inning_side"])
        self.assertEqual("yes", candidate["order_side"])
        self.assertEqual(54, candidate["entry_price"])
        self.assertFalse(candidate["game_started"])
        self.assertTrue(candidate["capper_first_inning_special"])
        self.assertTrue(candidate["capper_event_identity_verified"])
        self.assertEqual(market["event_ticker"], candidate["event_ticker"])

    def test_first_inning_matcher_never_confuses_red_sox_with_reds_or_white_sox(self):
        wrong_game = {
            "sport_key": "baseball_mlb",
            "home_team": "Chicago White Sox",
            "away_team": "Cincinnati Reds",
            "commence_time": "2026-08-12T23:40:00Z",
        }
        wrong_market = {
            "ticker": "KXMLBRFI-26AUG121940CINCWS",
            "title": "Cincinnati vs Chicago WS First Inning Run?",
        }
        self.assertFalse(sports.capper_first_inning_selection_matches(
            "Red Sox/Blue Jays NRFI", wrong_game, wrong_market
        ))

        correct_game = {
            "sport_key": "baseball_mlb",
            "home_team": "Toronto Blue Jays",
            "away_team": "Boston Red Sox",
            "commence_time": "2026-08-12T23:40:00Z",
        }
        correct_market = {
            "ticker": "KXMLBRFI-26AUG121940BOSTOR",
            "title": "Boston vs Toronto First Inning Run?",
        }
        self.assertTrue(sports.capper_first_inning_selection_matches(
            "Red Sox/Blue Jays NRFI", correct_game, correct_market
        ))

    def test_integrity_audit_excludes_mismatched_capper_result_without_changing_profit(self):
        bad_row = {
            "source": "trusted_capper",
            "status": "settled",
            "result": "WIN",
            "profit": 161.08,
            "market_type": "first_inning_run",
            "selected_team": "Red Sox/Blue Jays NRFI",
            "sport_key": "baseball_mlb",
            "home_team": "Chicago White Sox",
            "away_team": "Cincinnati Reds",
            "kalshi_ticker": "KXMLBRFI-26AUG121940CINCWS",
            "kalshi_title": "Cincinnati vs Chicago WS First Inning Run?",
        }
        portfolio = {"bets": [], "history": [bad_row]}

        self.assertEqual(1, sports.audit_trusted_capper_event_identity(portfolio))
        self.assertTrue(bad_row["strategy_analytics_excluded"])
        self.assertEqual("trusted_capper_event_identity_mismatch", bad_row["strategy_integrity"]["reason"])
        self.assertEqual(161.08, bad_row["profit"])
        self.assertEqual(0, sports.audit_trusted_capper_event_identity(portfolio))

    def test_integrity_audit_keeps_execution_verified_btts_when_legacy_market_metadata_is_generic(self):
        row = {
            "source": "trusted_capper",
            "status": "settled",
            "result": "LOSS",
            "profit": -64.65,
            "market_type": "btts",
            "selected_team": "Real Sociedad/Celta Vigo",
            "sport_key": "soccer_spain_la_liga",
            "home_team": "Real Sociedad",
            "away_team": "Celta Vigo",
            "kalshi_ticker": "KXLALIGABTTS-26SEP03RSORCC-BTTS",
            "kalshi_title": "Both Teams To Score",
            "capper_event_identity_verified": True,
            "strategy_analytics_excluded": True,
        }
        portfolio = {"bets": [], "history": [row]}

        self.assertEqual(1, sports.audit_trusted_capper_event_identity(portfolio))
        self.assertFalse(row["strategy_analytics_excluded"])
        self.assertTrue(row["strategy_integrity"]["valid"])
        self.assertEqual(
            "verified_stored_execution_identity",
            row["strategy_integrity"]["reason"],
        )
        self.assertEqual(-64.65, row["profit"])
        self.assertEqual(0, sports.audit_trusted_capper_event_identity(portfolio))

    def test_special_capper_market_requires_verified_event_identity_at_preflight(self):
        candidate = {
            "capper_first_inning_special": True,
            "capper_event_identity_verified": False,
        }
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(
            sports, "live_trading_ready", return_value=True
        ):
            self.assertEqual(
                "capper_event_identity_unverified",
                sports.live_execution_preflight_skip_reason({"bets": []}, candidate, 20),
            )

    def setUp(self):
        legacy_enabled = patch.object(sports, "SPORTS_CAPPER_ENABLED", True)
        legacy_enabled.start()
        self.addCleanup(legacy_enabled.stop)
        self.component = {
            "component_id": "component-1",
            "selection": "Tigers",
            "market_type": "moneyline",
            "market_line": None,
            "risk_fraction": 1.0,
        }
        self.ticket = {
            "ticket_id": "ticket-1",
            "source": "InfluencedBets",
            "sport_key": "baseball_mlb",
            "selection": "Tigers",
            "market_type": "moneyline",
            "posted_odds": -116,
            "capper_units": 3.5,
            "pick_type": "straight",
            "executable": True,
            "status": "watching",
            "target_event_date": "2026-08-09",
            "components": [self.component],
            "fills": [],
        }
        self.candidate = {
            "sport_key": "baseball_mlb",
            "selected_team": "Detroit Tigers",
            "market_type": "moneyline",
            "market_line": None,
            "kalshi_ticker": "KXMLBGAME-TEST-DET",
            "order_side": "yes",
            "game_key": "game-1",
            "event_id": "game-1",
            "game_started": False,
            "game_completed": False,
            "commence_time": "2026-08-09T20:00:00Z",
            "minutes_until_start": 120,
            "pregame_eligible": True,
            "entry_price": 52,
            "entry_bid": 51,
            "entry_american_odds": 108,
            "estimated_fee_edge_pp": 1,
            "edge": 2.5,
            "confidence_score": 80,
            "kalshi_volume": 10000,
            "kalshi_liquidity": 1000,
            "kalshi_spread": 1,
            "skip_reasons": ["below_edge", "pricing_v2_insufficient_independent_books"],
        }

    @patch.object(sports, "SPORTS_PREGAME_ENABLED", True)
    def test_capper_off_counterfactual_uses_normal_native_quality_gate(self):
        candidate = {
            **self.candidate,
            "commence_time": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
            "minutes_until_start": 60,
            "entry_price": 45,  # Exercise native quality independently of favorite-price tiers.
            "edge": 6.0,
            "confidence_score": 95.0,
            "feature_score": 100.0,
            "book_count": 5,
            "sharp_book_count": 1,
            "independent_book_family_count": 5,
            "skip_reasons": [],
            "book_line_model": "exact_line",
            "pricing_v2": {
                "ok": True,
                "uncertainty_pp": 1.0,
                "consensus": {
                    "independent_family_count": 5,
                    "sharp_book_count": 1,
                    "line_ladder_interpolated_family_count": 0,
                    "observations": [
                        {"family": str(index), "age_minutes": 1.0, "line_model": "exact_line"}
                        for index in range(5)
                    ],
                },
            },
        }
        with patch.object(
            sports,
            "edge_confirmation_review",
            return_value={"ok": True, "reason": "confirmed"},
        ):
            selected = sports.native_strategy_counterfactual_review(candidate, {})
            rejected = sports.native_strategy_counterfactual_review({
                **candidate,
                "edge": -2.0,
            }, {})
        self.assertTrue(selected["would_native_select"])
        self.assertEqual("native_select", selected["status"])
        self.assertFalse(rejected["would_native_select"])
        self.assertIn("below_edge", rejected["reasons"])
        self.assertFalse(selected["affects_execution"])

    def test_ticket_counterfactual_preserves_first_decision_and_components(self):
        with patch.object(
            sports,
            "native_strategy_counterfactual_review",
            return_value={
                "would_native_select": False,
                "status": "native_reject",
                "reasons": ["below_edge"],
            },
        ):
            first = sports.capper_off_counterfactual_review(
                self.ticket,
                [(self.component, self.candidate)],
                {},
            )
            later = sports.capper_off_counterfactual_review(
                {**self.ticket, "native_counterfactual": first},
                [(self.component, self.candidate)],
                {},
            )
        self.assertEqual("native_reject", later["first_status"])
        self.assertEqual(first["first_evaluated_at"], later["first_evaluated_at"])
        self.assertEqual("component-1", later["components"][0]["component_id"])

    def test_alias_and_exact_market_matching(self):
        matches = sports.capper_component_candidates(self.ticket, self.component, [self.candidate])
        self.assertEqual(1, len(matches))
        self.assertEqual("KXMLBGAME-TEST-DET", matches[0][1]["kalshi_ticker"])
        # A positive spread may use ML only as the explicitly configured final
        # fallback; it is labeled as a stricter outcome for auditability.
        wrong_line = {**self.component, "market_type": "spread", "market_line": 1.5}
        fallback = sports.capper_component_candidates(self.ticket, wrong_line, [self.candidate])
        self.assertEqual(1, len(fallback))
        self.assertEqual("moneyline_fallback", fallback[0][1]["capper_line_substitution"]["kind"])
        self.assertEqual(
            "stricter_than_positive_spread",
            fallback[0][1]["capper_line_substitution"]["outcome_tradeoff"],
        )

    def test_tennis_identity_never_matches_on_shared_first_name(self):
        ticket = {
            **self.ticket,
            "sport_key": "tennis",
            "target_event_date": "2026-08-09",
        }
        component = {
            **self.component,
            "selection": "Daniel Merida ML (LIVE)",
        }
        wrong_player = {
            **self.candidate,
            "sport_key": "tennis_atp_cincinnati_open",
            "selected_team": "Adolfo Daniel Vallejo",
        }
        correct_player = {
            **wrong_player,
            "selected_team": "Daniel Merida",
            "kalshi_ticker": "KXATPMATCH-MERIDA",
        }
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [wrong_player]))
        matches = sports.capper_component_candidates(ticket, component, [correct_player])
        self.assertEqual(1, len(matches))
        self.assertEqual("KXATPMATCH-MERIDA", matches[0][1]["kalshi_ticker"])
        bad_snapshot = {
            **ticket,
            "match_snapshot": {
                "components": [{
                    "component_id": "component-1",
                    "selection": "Adolfo Daniel Vallejo",
                }]
            },
        }
        self.assertTrue(sports.capper_ticket_has_invalid_tennis_snapshot(bad_snapshot, [component]))

    def test_mma_prop_text_cannot_substring_match_a_fighter_moneyline(self):
        ticket = {
            **self.ticket,
            "sport_key": "mma",
            "target_event_date": "2026-08-22",
        }
        malformed_legacy_component = {
            **self.component,
            "selection": "Anthony Wint by KO/TKO",
        }
        candidate = {
            **self.candidate,
            "sport_key": "mma_mixed_martial_arts",
            "selected_team": "Anthony Wint",
            "commence_time": "2026-08-23T00:15:00Z",
        }
        self.assertEqual(
            [],
            sports.capper_component_candidates(ticket, malformed_legacy_component, [candidate]),
        )
        exact_component = {**malformed_legacy_component, "selection": "Anthony Wint"}
        self.assertEqual(
            1,
            len(sports.capper_component_candidates(ticket, exact_component, [candidate])),
        )

    def test_mma_default_import_date_repairs_from_one_exact_fighter_match(self):
        ticket = {
            **self.ticket,
            "sport_key": "mma",
            "selection": "Anthony Wint",
            "target_event_date": "2026-08-21",
            "event_date_source": "import_date_default",
            "components": [{**self.component, "selection": "Anthony Wint"}],
        }
        game = {
            "id": "wint-chatman",
            "sport_key": "mma_mixed_martial_arts",
            "home_team": "Anthony Wint",
            "away_team": "Terrance Chatman",
            "commence_time": "2026-08-23T00:15:00Z",
        }
        repaired = sports.capper_mma_schedule_date_repair(
            ticket,
            [game],
            now=datetime(2026, 8, 22, 3, 30, tzinfo=timezone.utc),
        )
        self.assertEqual("2026-08-22", repaired["target_event_date"])
        self.assertEqual("2026-08-21", repaired["prior_target_event_date"])
        self.assertEqual("schedule_exact_fighter_match", repaired["event_date_source"])

        explicit = {**ticket, "event_date_source": "user_edited"}
        self.assertIsNone(sports.capper_mma_schedule_date_repair(
            explicit,
            [game],
            now=datetime(2026, 8, 22, 3, 30, tzinfo=timezone.utc),
        ))

    def test_cfb_default_import_date_repairs_from_one_exact_near_term_matchup(self):
        ticket = {
            **self.ticket,
            "sport_key": "americanfootball_ncaaf",
            "selection": "LSU",
            "target_event_date": "2026-09-03",
            "event_date_source": "import_date_default",
            "components": [{**self.component, "selection": "LSU"}],
        }
        game = {
            "id": "clemson-lsu",
            "sport_key": "americanfootball_ncaaf",
            "home_team": "LSU Tigers",
            "away_team": "Clemson Tigers",
            "commence_time": "2026-09-05T23:30:00Z",
        }

        repaired = sports.capper_cfb_schedule_date_repair(
            ticket,
            [game],
            now=datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual("2026-09-05", repaired["target_event_date"])
        self.assertEqual("2026-09-03", repaired["prior_target_event_date"])
        self.assertEqual("schedule_exact_cfb_match", repaired["event_date_source"])
        self.assertEqual("clemson-lsu", repaired["schedule_snapshot"]["games"][0]["id"])

        explicit = {**ticket, "event_date_source": "user_edited"}
        self.assertIsNone(sports.capper_cfb_schedule_date_repair(
            explicit,
            [game],
            now=datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc),
        ))

    def test_cfb_date_repair_requires_unambiguous_school_identity(self):
        ticket = {
            **self.ticket,
            "sport_key": "americanfootball_ncaaf",
            "selection": "Miami",
            "target_event_date": "2026-09-03",
            "event_date_source": "import_date_default",
            "components": [{**self.component, "selection": "Miami"}],
        }
        games = [
            {
                "id": "miami-stanford",
                "sport_key": "americanfootball_ncaaf",
                "home_team": "Miami Hurricanes",
                "away_team": "Stanford Cardinal",
                "commence_time": "2026-09-05T20:00:00Z",
            },
            {
                "id": "miami-oh-pitt",
                "sport_key": "americanfootball_ncaaf",
                "home_team": "Miami (OH) RedHawks",
                "away_team": "Pittsburgh Panthers",
                "commence_time": "2026-09-05T16:00:00Z",
            },
        ]

        self.assertGreater(sports.capper_cfb_name_match_score("Miami", games[0]["home_team"]), 0)
        self.assertEqual(0, sports.capper_cfb_name_match_score("Miami", games[1]["home_team"]))
        repaired = sports.capper_cfb_schedule_date_repair(
            ticket,
            games,
            now=datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual("miami-stanford", repaired["schedule_snapshot"]["games"][0]["id"])

    def test_cfb_alias_matching_is_school_safe(self):
        self.assertGreater(sports.capper_cfb_name_match_score("UGA", "Georgia Bulldogs"), 0)
        self.assertEqual(0, sports.capper_cfb_name_match_score("Georgia", "Georgia State Panthers"))
        self.assertGreater(sports.capper_cfb_name_match_score("Miss State", "Mississippi State Bulldogs"), 0)
        self.assertEqual(0, sports.capper_cfb_name_match_score("Florida", "Florida State Seminoles"))

    def test_cbb_default_import_date_repairs_from_one_exact_near_term_matchup(self):
        ticket = {
            **self.ticket,
            "sport_key": "basketball_ncaab",
            "selection": "UConn",
            "target_event_date": "2026-11-05",
            "event_date_source": "import_date_default",
            "components": [{**self.component, "selection": "UConn"}],
        }
        game = {
            "id": "uconn-duke",
            "sport_key": "basketball_ncaab",
            "home_team": "Connecticut Huskies",
            "away_team": "Duke Blue Devils",
            "commence_time": "2026-11-08T01:00:00Z",
        }

        repaired = sports.capper_cfb_schedule_date_repair(
            ticket,
            [game],
            now=datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual("2026-11-07", repaired["target_event_date"])
        self.assertEqual("schedule_exact_cbb_match", repaired["event_date_source"])
        self.assertEqual("uconn-duke", repaired["schedule_snapshot"]["games"][0]["id"])

    def test_cbb_matching_rejects_similarly_named_school(self):
        ticket = {
            **self.ticket,
            "sport_key": "basketball_ncaab",
            "selection": "Kansas",
            "target_event_date": "2026-11-07",
        }
        component = {**self.component, "selection": "Kansas"}
        wrong_game = {
            "sport_key": "basketball_ncaab",
            "home_team": "Kansas State Wildcats",
            "away_team": "Duke Blue Devils",
            "commence_time": "2026-11-08T01:00:00Z",
        }
        correct_game = {
            **wrong_game,
            "home_team": "Kansas Jayhawks",
        }
        wrong_candidate = {
            **wrong_game,
            "market_type": "moneyline",
            "selected_team": "Kansas State Wildcats",
        }

        self.assertFalse(sports.capper_ticket_matches_game(ticket, component, wrong_game))
        self.assertTrue(sports.capper_ticket_matches_game(ticket, component, correct_game))
        self.assertEqual(
            0,
            sports.capper_component_selection_match_score(
                component,
                wrong_candidate,
                ticket=ticket,
            ),
        )
        self.assertGreater(
            sports.capper_cfb_name_match_score("UConn", "Connecticut Huskies"),
            0,
        )

    def test_cbb_odds_exhaustion_fallback_rejects_similarly_named_school(self):
        ticket = {
            **self.ticket,
            "sport_key": "basketball_ncaab",
            "selection": "Kansas",
        }
        component = {**self.component, "selection": "Kansas", "market_type": "moneyline"}
        market = {
            "series_ticker": "KXNCAAMBGAME",
            "ticker": "KXNCAAMBGAME-26NOV07KSTDKE-KST",
            "yes_sub_title": "Kansas State",
            "rules_primary": (
                "If Kansas State wins the Duke vs Kansas State men's college basketball "
                "game, then the market resolves to Yes."
            ),
        }

        self.assertIsNone(
            sports.capper_odds_fallback_selection_side(
                ticket,
                component,
                market,
                "moneyline",
            )
        )

    def test_exposure_limit_steps_down_to_largest_available_unit(self):
        group = {
            "ok": True,
            "existing": {"game": 70, "team": 70, "sport": 100},
            "limits": {"game": 100, "team": 100, "sport": 200},
        }
        with (
            patch.object(sports, "effective_live_max_stake", return_value=500),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=1000),
            patch.object(sports, "live_exposure", return_value=100),
            patch.object(sports, "trusted_capper_active_sports", return_value=set()),
            patch.object(sports, "live_group_exposure_review", return_value=group),
        ):
            review = sports.live_stake_step_down_review(
                {"balance": 900, "bets": []},
                self.candidate,
                50,
                unit_size=10,
                unit_step=1,
                minimum_units=1,
            )
        self.assertTrue(review["ok"])
        self.assertTrue(review["resized"])
        self.assertEqual(3, review["approved_units"])
        self.assertEqual(30, review["approved_stake"])
        self.assertEqual("live_game_exposure_cap", review["limiting_reason"])

    def test_exchange_breakdown_does_not_bucket_account_available_cash(self):
        group = {
            "ok": True,
            "existing": {"game": 0, "team": 0, "sport": 0},
            "limits": {"game": 0, "team": 0, "sport": 0},
        }
        with (
            patch.object(sports, "effective_live_max_stake", return_value=0),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=0),
            patch.object(sports, "trusted_capper_active_sports", return_value=set()),
            patch.object(sports, "live_group_exposure_review", return_value=group),
        ):
            review = sports.live_stake_step_down_review(
                {"balance": 6079.89, "bets": []},
                {**self.candidate, "exchange_index": 0},
                280.48,
                unit_size=70.12,
                unit_step=0.5,
                minimum_units=0.5,
            )
        self.assertTrue(review["ok"])
        self.assertFalse(review["resized"])
        self.assertEqual(4.0, review["approved_units"])
        self.assertEqual(280.48, review["approved_stake"])
        self.assertEqual("", review["limiting_reason"])
        self.assertNotIn("exchange_0_available_cash", review["caps"])
        self.assertEqual("account_available_balance", review["cash_review"]["scope"])

    def test_stale_reconciliation_is_refreshed_immediately_before_execution(self):
        report = {"account": {"ok": True, "cash_balance": 100.0}}
        portfolio = {"balance": 100.0, "bets": []}
        with (
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_RECONCILE_LIVE_ON_SCAN", True),
            patch.object(
                sports,
                "live_reconciliation_preflight_skip_reason",
                return_value="live_reconciliation_stale",
            ),
            patch.object(sports, "reconcile_live_account", return_value=report) as reconcile,
            patch.object(sports, "save_portfolio") as save,
        ):
            result = sports.refresh_live_reconciliation_before_execution(portfolio)
        self.assertTrue(result["refreshed"])
        self.assertEqual("live_reconciliation_stale", result["reason"])
        reconcile.assert_called_once_with(portfolio)
        save.assert_called_once_with(portfolio)

    def test_half_unit_cent_rounding_does_not_trigger_available_cash_skip(self):
        group = {
            "ok": True,
            "existing": {"game": 0, "team": 0, "sport": 0},
            "limits": {"game": 0, "team": 0, "sport": 0},
        }
        unit_size = 100.49
        requested_stake = round(0.5 * unit_size, 2)
        with (
            patch.object(sports, "effective_live_max_stake", return_value=0),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=0),
            patch.object(sports, "trusted_capper_active_sports", return_value=set()),
            patch.object(sports, "live_group_exposure_review", return_value=group),
        ):
            review = sports.live_stake_step_down_review(
                {"balance": 4458.22, "bets": []},
                self.candidate,
                requested_stake,
                unit_size=unit_size,
                unit_step=0.5,
                minimum_units=0.5,
            )
        self.assertTrue(review["ok"])
        self.assertFalse(review["resized"])
        self.assertEqual(0.5, review["approved_units"])
        self.assertEqual(requested_stake, review["approved_stake"])
        self.assertEqual("", review["limiting_reason"])

    def test_requested_partial_units_are_attributed_to_requested_increment(self):
        group = {
            "ok": True,
            "existing": {"game": 0, "team": 0, "sport": 0},
            "limits": {"game": 0, "team": 0, "sport": 0},
        }
        with (
            patch.object(sports, "effective_live_max_stake", return_value=0),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=0),
            patch.object(sports, "trusted_capper_active_sports", return_value=set()),
            patch.object(sports, "live_group_exposure_review", return_value=group),
        ):
            review = sports.live_stake_step_down_review(
                {"balance": 5890.0, "bets": []},
                {**self.candidate, "exchange_index": 3},
                36.32,
                unit_size=70.12,
                unit_step=0.5,
                minimum_units=0.5,
            )
        self.assertTrue(review["ok"])
        self.assertTrue(review["resized"])
        self.assertEqual(0.5, review["approved_units"])
        self.assertEqual("requested_stake_unit_increment", review["limiting_reason"])

    def test_ten_percent_game_cap_accepts_full_five_units(self):
        portfolio = {"balance": 900, "bets": []}
        candidate = {
            **self.candidate,
            "home_team": "Detroit Tigers",
            "away_team": "Chicago White Sox",
        }
        with (
            patch.object(sports, "sports_unit_bankroll_base", return_value=1000),
            patch.object(sports, "SPORTS_MAX_GAME_EXPOSURE_PCT", 0.10),
            patch.object(sports, "SPORTS_MAX_TEAM_EXPOSURE_PCT", 0.10),
            patch.object(sports, "SPORTS_MAX_SPORT_EXPOSURE_PCT", 0.20),
        ):
            review = sports.live_group_exposure_review(portfolio, candidate, 100)
        self.assertTrue(review["ok"])
        self.assertEqual(100, review["limits"]["game"])
        self.assertEqual("daily_unit_snapshot", review["bankroll_basis"])

    def test_cached_schedule_keeps_unmatched_ticket_monitored(self):
        schedule_game = {
            "id": "scheduled-game",
            "sport_key": "baseball_mlb",
            "home_team": "Detroit Tigers",
            "away_team": "Chicago White Sox",
            "commence_time": "2026-08-09T20:00:00Z",
        }
        with patch.object(sports, "load_json_file", return_value={
            "sports": {"baseball_mlb": {"games": [schedule_game]}}
        }):
            games = sports.capper_monitoring_games(self.ticket, [])
        self.assertEqual(1, len(games))
        self.assertTrue(sports.capper_ticket_matches_game(self.ticket, self.component, games[0]))

    def test_trusted_capper_missing_depth_submits_one_unit_fok_probe(self):
        candidate = {
            **self.candidate,
            "trusted_capper": True,
            "capper_posted_odds": -110,
            "capper_effective_probability_ceiling": 100,
            "orderbook_depth_valid": False,
            "executable_contracts_at_ask": None,
            "sports_units": {"unit_size": 20, "additional_units": 2},
            "live_campaign": {
                "active": True,
                "eligible": True,
                "requested_stake": 40,
                "applied_stake": 40,
                "max_single_stake": 0,
            },
        }
        order = {
            "fill_count_fp": "40.00",
            "taker_fill_cost_dollars": "20.0000",
            "taker_fees_dollars": "0.7200",
            "yes_price_dollars": "0.5000",
        }
        pricing = {
            "ok": True,
            "required_edge": -100,
            "candidate_updates": {"entry_price": 50, "edge": 2.5},
        }
        with (
            patch.object(sports, "live_order_start_guard", return_value={"ok": True}),
            patch.object(sports, "pregame_game_allowed", return_value=True),
            patch.object(sports, "live_order_pricing_guard", return_value=pricing),
            patch.object(sports, "effective_live_max_stake", return_value=0),
            patch.object(sports, "kalshi_private_request", return_value=({"order": order}, {})),
            patch.object(sports, "upsert_live_order_intent"),
            patch.object(sports, "SPORTS_CAPPER_DEPTH_PROBE_ENABLED", True),
            patch.object(sports, "SPORTS_CAPPER_DEPTH_PROBE_MAX_UNITS", 1),
            patch.object(
                sports,
                "reserve_live_order",
                return_value={
                    "ok": True,
                    "approved_stake": 20,
                    "reservation_id": "sports-reserve-test",
                },
            ),
            patch.object(sports, "finalize_reservation"),
        ):
            result = sports.place_live_kalshi_order(candidate, 40, portfolio={"balance": 1000, "bets": []})
        self.assertTrue(result["ok"], result)
        self.assertEqual(40, result["contracts"])
        self.assertEqual(20, result["actual_stake"])
        self.assertEqual("trusted_capper_fok_probe", result["execution_depth_review"]["mode"])

    def test_relaxed_pregame_fallback_requires_one_independent_book_family(self):
        zero_families = {**self.candidate, "independent_book_family_count": 0}
        one_family = {**self.candidate, "independent_book_family_count": 1}
        self.assertEqual(
            "capper_insufficient_independent_books",
            sports.capper_candidate_hard_reason({"bets": []}, zero_families),
        )
        self.assertEqual("", sports.capper_candidate_hard_reason({"bets": []}, one_family))

    def test_exact_straight_priority_uses_posted_pick_as_pregame_signal(self):
        exact = {
            **self.candidate,
            "capper_exact_straight": True,
            "independent_book_family_count": 0,
        }
        with patch.object(sports, "SPORTS_CAPPER_EXACT_PREGAME_MIN_BOOK_FAMILIES", 0):
            self.assertEqual("", sports.capper_candidate_hard_reason({"bets": []}, exact))

    def test_exact_straight_live_still_requires_one_current_book(self):
        base = {
            **self.candidate,
            "capper_exact_straight": True,
            "game_started": True,
            "has_live_score_context": True,
            "live_odds_age_minutes": 0.5,
        }
        with patch.object(sports, "SPORTS_CAPPER_EXACT_LIVE_MIN_BOOK_FAMILIES", 1):
            self.assertEqual(
                "capper_insufficient_independent_books",
                sports.capper_candidate_hard_reason(
                    {"bets": []}, {**base, "independent_book_family_count": 0}
                ),
            )
            self.assertEqual(
                "",
                sports.capper_candidate_hard_reason(
                    {"bets": []}, {**base, "independent_book_family_count": 1}
                ),
            )

    def test_promoted_exact_pick_keeps_posted_half_units_for_ordinary_edge(self):
        ticket = {**self.ticket, "capper_units": 3.5}
        candidate = {
            **self.candidate,
            "capper_exact_straight": True,
            "edge": -3.0,
            "independent_book_family_count": 2,
            "pricing_v2": {"ok": True},
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_FULL_POSTED_UNITS_ENABLED", True),
            patch.object(sports, "pro_decision_score", return_value={"score": 91}),
            patch.object(sports, "final_bet_score", return_value={"score": 88}),
        ):
            quality = sports.capper_ticket_quality(
                ticket, [(self.component, candidate)], {}
            )
        self.assertEqual(3.5, quality["target_units"])
        self.assertEqual("trusted_capper_full_posted_units", quality["sizing_band"])
        self.assertFalse(quality["severe_negative_edge_review"]["applied"])

    def test_promoted_pick_reduces_only_reliably_confirmed_severe_negative_edge(self):
        ticket = {**self.ticket, "capper_units": 4}

        def quality_for(edge, families=2, pricing_ok=True):
            candidate = {
                **self.candidate,
                "capper_exact_straight": True,
                "edge": edge,
                "independent_book_family_count": families,
                "pricing_v2": {"ok": pricing_ok},
            }
            with (
                patch.object(sports, "SPORTS_CAPPER_FULL_POSTED_UNITS_ENABLED", True),
                patch.object(sports, "pro_decision_score", return_value={"score": 91}),
                patch.object(sports, "final_bet_score", return_value={"score": 88}),
            ):
                return sports.capper_ticket_quality(
                    ticket, [(self.component, candidate)], {}
                )

        severe = quality_for(-6.1)
        critical = quality_for(-10.1)
        single_book = quality_for(-12.0, families=1)
        unavailable_pricing = quality_for(-12.0, pricing_ok=False)

        self.assertEqual(3, severe["target_units"])
        self.assertEqual("severe", severe["severe_negative_edge_review"]["severity"])
        self.assertEqual(2, critical["target_units"])
        self.assertEqual("critical", critical["severe_negative_edge_review"]["severity"])
        self.assertEqual(4, single_book["target_units"])
        self.assertEqual(4, unavailable_pricing["target_units"])

    def test_promoted_parlay_children_retain_parent_exposure_split(self):
        ticket = {
            **self.ticket,
            "pick_type": "parlay_leg",
            "capper_units": 5,
            "parlay_leg_max_units": 2.5,
        }
        candidate = {
            **self.candidate,
            "edge": 3,
            "independent_book_family_count": 2,
            "pricing_v2": {"ok": True},
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_FULL_POSTED_UNITS_ENABLED", True),
            patch.object(sports, "pro_decision_score", return_value={"score": 91}),
            patch.object(sports, "final_bet_score", return_value={"score": 88}),
        ):
            quality = sports.capper_ticket_quality(
                ticket, [(self.component, candidate)], {}
            )
        self.assertEqual(2.5, quality["target_units"])
        self.assertTrue(quality["parent_exposure_split_retained"])

    def test_promoted_exact_live_pick_ignores_small_model_disagreement(self):
        candidate = {
            **self.candidate,
            "capper_exact_straight": True,
            "game_started": True,
            "has_live_score_context": True,
            "live_odds_age_minutes": 0.5,
            "edge": -3,
            "independent_book_family_count": 1,
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_FULL_POSTED_UNITS_ENABLED", True),
            patch.object(sports, "SPORTS_CAPPER_EXACT_LIVE_MIN_BOOK_FAMILIES", 1),
        ):
            self.assertEqual(
                "", sports.capper_candidate_hard_reason({"bets": []}, candidate)
            )

    def test_promoted_exact_order_guard_does_not_restore_live_edge_floor(self):
        candidate = {
            **self.candidate,
            "capper_exact_straight": True,
            "game_started": True,
            "edge": -3,
            "capper_posted_odds": -116,
            "capper_effective_probability_ceiling": 100,
        }
        market = {"yes_bid": 53, "yes_ask": 54, "no_bid": 46, "no_ask": 47}
        with (
            patch.object(sports, "SPORTS_CAPPER_FULL_POSTED_UNITS_ENABLED", True),
            patch.object(sports, "sports_stream", return_value=None),
            patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=market),
        ):
            review = sports.trusted_capper_order_pricing_guard(candidate)
        self.assertTrue(review["ok"])
        self.assertIsNone(review["required_edge"])

    def test_capper_order_guard_uses_rest_when_stream_is_missing(self):
        candidate = {
            **self.candidate,
            "capper_posted_odds": -116,
            "capper_effective_probability_ceiling": 55,
        }
        market = {"yes_bid": 51, "yes_ask": 52, "no_bid": 48, "no_ask": 49}
        with (
            patch.object(sports, "sports_stream", return_value=None),
            patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=market),
            patch.object(sports, "SPORTS_KALSHI_STREAM_REQUIRE_FOR_LIVE", True),
        ):
            result = sports.trusted_capper_order_pricing_guard(candidate)
        self.assertTrue(result["ok"])
        self.assertEqual("kalshi_rest_order_guard", result["source"])

    def test_negative_spread_can_select_better_spread_or_moneyline(self):
        component = {**self.component, "market_type": "spread", "market_line": -2}
        ticket = {**self.ticket, "market_type": "spread", "market_line": -2, "components": [component]}
        exact = {
            **self.candidate,
            "market_type": "spread",
            "market_line": -2,
            "kalshi_ticker": "EXACT",
            "edge": -1,
        }
        better = {
            **self.candidate,
            "market_type": "spread",
            "market_line": -1,
            "kalshi_ticker": "BETTER",
            "edge": 3,
            "independent_book_family_count": 2,
        }
        moneyline = {
            **self.candidate,
            "kalshi_ticker": "ML",
            "edge": 2,
            "independent_book_family_count": 3,
        }
        matches = sports.capper_component_candidates(ticket, component, [exact, moneyline, better])
        self.assertEqual(1, len(matches))
        self.assertEqual("BETTER", matches[0][1]["kalshi_ticker"])
        self.assertEqual("better_spread", matches[0][1]["capper_line_substitution"]["kind"])

        spread_only = sports.capper_component_candidates(ticket, component, [exact, better])
        self.assertEqual("BETTER", spread_only[0][1]["kalshi_ticker"])
        self.assertEqual("better_spread", spread_only[0][1]["capper_line_substitution"]["kind"])
        self.assertEqual(1, spread_only[0][1]["capper_line_substitution"]["line_improvement"])
        ml_only = sports.capper_component_candidates(ticket, component, [moneyline])
        self.assertEqual("ML", ml_only[0][1]["kalshi_ticker"])
        self.assertEqual("moneyline_fallback", ml_only[0][1]["capper_line_substitution"]["kind"])

    def test_negative_spread_moneyline_fallback_requires_own_value(self):
        component = {**self.component, "market_type": "spread", "market_line": -2}
        ticket = {**self.ticket, "market_type": "spread", "market_line": -2, "components": [component]}
        moneyline = {
            **self.candidate,
            "edge": 0.5,
            "independent_book_family_count": 2,
        }
        match = sports.capper_component_candidates(ticket, component, [moneyline])[0][1]
        review = sports.capper_ticket_price_review(ticket, [(component, match)])
        self.assertTrue(review["ok"])
        self.assertEqual(0, review["tolerance_pp"])
        self.assertEqual(review["effective_probability"], review["posted_probability"])

        weak = {**match, "edge": -0.01}
        self.assertFalse(sports.capper_ticket_price_review(ticket, [(component, weak)])["ok"])
        unconfirmed = {**match, "independent_book_family_count": 0}
        review = sports.capper_ticket_price_review(ticket, [(component, unconfirmed)])
        self.assertFalse(review["ok"])
        self.assertEqual("better_line_substitution_insufficient_independent_books", review["reason"])

    def test_positive_spread_only_moves_to_larger_positive_spread(self):
        component = {**self.component, "market_type": "spread", "market_line": 2}
        ticket = {**self.ticket, "market_type": "spread", "market_line": 2, "components": [component]}
        moneyline = {**self.candidate, "independent_book_family_count": 3}
        better = {
            **self.candidate,
            "market_type": "spread",
            "market_line": 3,
            "kalshi_ticker": "PLUS3",
            "independent_book_family_count": 2,
        }
        matches = sports.capper_component_candidates(ticket, component, [moneyline, better])
        self.assertEqual(1, len(matches))
        self.assertEqual("PLUS3", matches[0][1]["kalshi_ticker"])

        ml_only = sports.capper_component_candidates(ticket, component, [moneyline])
        self.assertEqual(1, len(ml_only))
        fallback = ml_only[0][1]["capper_line_substitution"]
        self.assertEqual("moneyline_fallback", fallback["kind"])
        self.assertEqual("stricter_than_positive_spread", fallback["outcome_tradeoff"])

    def test_positive_spread_prefers_nearest_better_line_over_extreme_ladder(self):
        component = {
            **self.component,
            "selection": "Colorado",
            "market_type": "spread",
            "market_line": 7,
        }
        ticket = {
            **self.ticket,
            "sport_key": "americanfootball_ncaaf",
            "selection": "Colorado",
            "market_type": "spread",
            "market_line": 7,
            "components": [component],
        }
        nearby = {
            **self.candidate,
            "sport_key": "americanfootball_ncaaf",
            "selected_team": "Colorado Buffaloes",
            "market_type": "spread",
            "market_line": 7.5,
            "kalshi_ticker": "COLORADO-PLUS7HALF",
            "edge": -1.2,
            "independent_book_family_count": 3,
        }
        extreme = {
            **nearby,
            "market_line": 27.5,
            "kalshi_ticker": "COLORADO-PLUS27HALF",
            "entry_price": 91,
            "edge": 2.0,
        }

        matches = sports.capper_component_candidates(
            ticket,
            component,
            [extreme, nearby],
        )

        self.assertEqual(1, len(matches))
        selected = matches[0][1]
        self.assertEqual("COLORADO-PLUS7HALF", selected["kalshi_ticker"])
        self.assertEqual(0.5, selected["capper_line_substitution"]["line_improvement"])
        extreme_review = sports.capper_substitution_value_review({
            **extreme,
            "capper_line_substitution": sports.capper_spread_substitution(
                ticket,
                component,
                extreme,
            ),
        })
        self.assertFalse(extreme_review["ok"])
        self.assertEqual("capper_substitution_price_outside_range", extreme_review["reason"])

    def test_inverse_no_spread_prefers_live_better_line_for_same_team(self):
        component = {
            **self.component,
            "selection": "Atlanta Dream",
            "market_type": "spread",
            "market_line": 3.5,
        }
        ticket = {
            **self.ticket,
            "sport_key": "basketball_wnba",
            "selection": "Atlanta Dream",
            "market_type": "spread",
            "market_line": 3.5,
            "components": [component],
        }
        exact = {
            **self.candidate,
            "sport_key": "basketball_wnba",
            "selected_team": "Atlanta Dream",
            "market_type": "spread",
            "market_line": 3.5,
            "kalshi_ticker": "KXWNBASPREAD-TEST-LV4",
            "order_side": "no",
            "edge": 2.0,
            "independent_book_family_count": 2,
        }
        better = {
            **exact,
            "market_line": 6.5,
            "kalshi_ticker": "KXWNBASPREAD-TEST-LV7",
            "edge": 1.0,
        }

        matches = sports.capper_component_candidates(ticket, component, [exact, better])

        self.assertEqual(1, len(matches))
        selected = matches[0][1]
        self.assertEqual("KXWNBASPREAD-TEST-LV7", selected["kalshi_ticker"])
        self.assertEqual("no", selected["order_side"])
        self.assertEqual("better_spread", selected["capper_line_substitution"]["kind"])
        self.assertEqual(3.0, selected["capper_line_substitution"]["line_improvement"])

        exact_only = sports.capper_component_candidates(ticket, component, [exact])
        self.assertEqual(1, len(exact_only))
        self.assertEqual("no", exact_only[0][1]["order_side"])
        self.assertNotIn("capper_line_substitution", exact_only[0][1])

    def test_chicago_inverse_no_spread_accepts_bounded_closest_line(self):
        component = {
            **self.component,
            "selection": "Chicago Sky",
            "market_type": "spread",
            "market_line": 4.5,
        }
        ticket = {
            **self.ticket,
            "sport_key": "basketball_wnba",
            "selection": "Chicago Sky",
            "market_type": "spread",
            "market_line": 4.5,
            "components": [component],
        }
        closest = {
            **self.candidate,
            "sport_key": "basketball_wnba",
            "selected_team": "Chicago Sky",
            "market_type": "spread",
            "market_line": 3.5,
            "kalshi_ticker": "KXWNBASPREAD-TEST-NY4",
            "order_side": "no",
            "edge": 0.5,
            "independent_book_family_count": 1,
        }

        matches = sports.capper_component_candidates(ticket, component, [closest])

        self.assertEqual(1, len(matches))
        selected = matches[0][1]
        self.assertEqual("no", selected["order_side"])
        self.assertEqual("closest_spread", selected["capper_line_substitution"]["kind"])
        self.assertEqual(-1.0, selected["capper_line_substitution"]["line_improvement"])
        self.assertTrue(sports.capper_ticket_price_review(ticket, [(component, selected)])["ok"])

    def test_closest_spread_is_bounded_and_requires_its_own_nonnegative_edge(self):
        component = {**self.component, "market_type": "spread", "market_line": 3.5}
        ticket = {**self.ticket, "market_type": "spread", "market_line": 3.5, "components": [component]}
        closest = {
            **self.candidate,
            "market_type": "spread",
            "market_line": 2.5,
            "kalshi_ticker": "PLUS2HALF",
            "edge": 0.25,
            "independent_book_family_count": 1,
        }
        matches = sports.capper_component_candidates(ticket, component, [closest])
        self.assertEqual(1, len(matches))
        self.assertEqual("closest_spread", matches[0][1]["capper_line_substitution"]["kind"])
        self.assertTrue(sports.capper_ticket_price_review(ticket, [(component, matches[0][1])])["ok"])

        too_far = {**closest, "market_line": 1.5, "kalshi_ticker": "PLUS1HALF"}
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [too_far]))
        negative = {**closest, "edge": -0.01}
        match = sports.capper_component_candidates(ticket, component, [negative])[0][1]
        self.assertFalse(sports.capper_ticket_price_review(ticket, [(component, match)])["ok"])

    def test_total_fallback_keeps_direction_and_exact_event_identity(self):
        component = {
            **self.component,
            "selection": "Under 45.5",
            "market_type": "total",
            "market_line": 45.5,
            "total_side": "under",
            "event_hint": "Lions/Bears",
        }
        ticket = {
            **self.ticket,
            "sport_key": "americanfootball_nfl",
            "selection": "Under 45.5",
            "market_type": "total",
            "market_line": 45.5,
            "components": [component],
        }
        base = {
            **self.candidate,
            "sport_key": "americanfootball_nfl",
            "home_team": "Detroit Lions",
            "away_team": "Chicago Bears",
            "selected_team": "Under 46.5",
            "total_side": "under",
            "market_type": "total",
            "market_line": 46.5,
            "kalshi_ticker": "UNDER46HALF",
            "edge": -0.5,
            "independent_book_family_count": 1,
        }
        matches = sports.capper_component_candidates(ticket, component, [base])
        self.assertEqual(1, len(matches))
        self.assertEqual("better_total", matches[0][1]["capper_line_substitution"]["kind"])
        self.assertTrue(sports.capper_ticket_price_review(ticket, [(component, matches[0][1])])["ok"])

        closest = {
            **base,
            "selected_team": "Under 44.5",
            "market_line": 44.5,
            "kalshi_ticker": "UNDER44HALF",
            "edge": 0.25,
        }
        matches = sports.capper_component_candidates(ticket, component, [closest])
        self.assertEqual("closest_total", matches[0][1]["capper_line_substitution"]["kind"])

        wrong_game = {**base, "home_team": "Houston Texans", "away_team": "Dallas Cowboys"}
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [wrong_game]))
        wrong_side = {**base, "selected_team": "Over 46.5", "total_side": "over"}
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [wrong_side]))

    def test_team_total_never_matches_full_game_total(self):
        component = {
            **self.component,
            "selection": "Lions Under 15.5",
            "market_type": "team_total",
            "market_line": 15.5,
            "total_side": "under",
            "team_total_team": "Lions",
            "event_hint": "Lions",
        }
        ticket = {**self.ticket, "sport_key": "americanfootball_nfl", "components": [component]}
        full_game = {
            **self.candidate,
            "sport_key": "americanfootball_nfl",
            "home_team": "Detroit Lions",
            "away_team": "Chicago Bears",
            "selected_team": "Under 45.5",
            "total_side": "under",
            "market_type": "total",
            "market_line": 45.5,
        }
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [full_game]))

    def test_pending_capper_spread_prioritizes_on_demand_alternate_odds(self):
        component = {
            **self.component,
            "selection": "Lions",
            "market_type": "spread",
            "market_line": 3.5,
        }
        ticket = {
            **self.ticket,
            "sport_key": "americanfootball_nfl",
            "selection": "Lions",
            "market_type": "spread",
            "components": [component],
        }
        game = {
            "id": "lions-bears",
            "sport_key": "americanfootball_nfl",
            "home_team": "Detroit Lions",
            "away_team": "Chicago Bears",
            "commence_time": "2026-08-09T20:00:00Z",
            "bookmakers": [],
        }
        with (
            patch.object(sports, "list_capper_tickets", return_value=[ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED", True),
            patch.object(sports, "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN", 8),
            patch.object(sports, "SPORTS_ALTERNATE_ODDS_CACHE", {}),
            patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)),
            patch.object(sports, "odds_api_get_json", return_value=({**game}, {})) as get_odds,
            patch.object(sports, "record_odds_spend"),
            patch.object(sports, "append_jsonl"),
            patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1}),
        ):
            _games, status = sports.enrich_with_on_demand_alternate_odds([game], [])
        self.assertEqual(1, status["capper_priority_events"])
        self.assertEqual(1, status["requested_events"])
        self.assertEqual("alternate_spreads", get_odds.call_args.kwargs["params"]["markets"])

    def test_tennis_set_spread_matches_only_same_unit_yes_or_no_contract(self):
        component = {
            **self.component,
            "selection": "Jodar",
            "market_type": "spread",
            "market_line": 1.5,
            "line_unit": "set",
        }
        ticket = {
            **self.ticket,
            "sport_key": "tennis",
            "selection": "Jodar",
            "market_type": "spread",
            "market_line": 1.5,
            "pick_type": "parlay_leg",
            "components": [component],
        }
        base = {
            **self.candidate,
            "sport_key": "tennis_atp",
            "selected_team": "Jodar",
            "market_type": "spread",
            "market_line": 1.5,
        }
        game_spread = {**base, "kalshi_ticker": "KXATPGAMESPREAD-TEST-JODAR"}
        unknown_spread = {**base, "kalshi_ticker": "UNKNOWN-SPREAD"}
        set_spread = {**base, "kalshi_ticker": "KXATPSETSPREAD-TEST-JODAR"}
        opposing_set_spread_no = {
            **base,
            "kalshi_ticker": "KXATPSETSPREAD-TEST-OPPONENT",
            "order_side": "no",
        }
        moneyline = {
            **base,
            "market_type": "moneyline",
            "market_line": None,
            "kalshi_ticker": "KXATPMATCH-TEST-JODAR",
        }
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [game_spread]))
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [unknown_spread]))
        self.assertEqual(1, len(sports.capper_component_candidates(ticket, component, [set_spread])))
        no_match = sports.capper_component_candidates(ticket, component, [opposing_set_spread_no])
        self.assertEqual(1, len(no_match))
        self.assertEqual("no", no_match[0][1]["order_side"])
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [moneyline]))

    def test_tennis_game_total_matches_only_game_total_series(self):
        component = {
            **self.component,
            "selection": "Over 22.5",
            "market_type": "total",
            "market_line": 22.5,
            "total_side": "over",
            "event_hint": "Paul/Cobolli",
            "line_unit": "game",
        }
        ticket = {
            **self.ticket,
            "sport_key": "tennis",
            "selection": "Over 22.5",
            "market_type": "total",
            "market_line": 22.5,
            "components": [component],
        }
        base = {
            **self.candidate,
            "sport_key": "tennis_atp",
            "home_team": "Tommy Paul",
            "away_team": "Flavio Cobolli",
            "selected_team": "Over 22.5",
            "market_type": "total",
            "market_line": 22.5,
            "total_side": "over",
        }
        game_total = {**base, "kalshi_ticker": "KXATPGAMETOTAL-TEST-23"}
        live_game_total = {**base, "kalshi_ticker": "KXATPGTOTAL-26AUG20PAUCOB-23"}
        set_total = {**base, "kalshi_ticker": "KXATPTOTALSETS-TEST-23"}
        unknown_total = {**base, "kalshi_ticker": "KXATPTOTAL-UNKNOWN-23"}
        self.assertEqual(1, len(sports.capper_component_candidates(ticket, component, [game_total])))
        self.assertEqual(1, len(sports.capper_component_candidates(ticket, component, [live_game_total])))
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [set_total]))
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [unknown_total]))

        better_game_total = {
            **game_total,
            "kalshi_ticker": "KXATPGAMETOTAL-TEST-22",
            "selected_team": "Over 21.5",
            "market_line": 21.5,
        }
        better = sports.capper_component_candidates(ticket, component, [better_game_total])
        self.assertEqual("better_total", better[0][1]["capper_line_substitution"]["kind"])
        self.assertEqual("game", better[0][1]["capper_line_substitution"]["offered_line_unit"])

    def test_derivative_spread_units_never_fall_back_to_full_event_moneyline(self):
        moneyline = {**self.candidate, "market_type": "moneyline", "market_line": None}
        for line_unit in ("set", "game", "period", "quarter", "half", "inning", "round"):
            with self.subTest(line_unit=line_unit):
                component = {
                    **self.component,
                    "market_type": "spread",
                    "market_line": 1.5,
                    "line_unit": line_unit,
                }
                ticket = {
                    **self.ticket,
                    "market_type": "spread",
                    "market_line": 1.5,
                    "components": [component],
                }
                self.assertEqual(
                    [],
                    sports.capper_component_candidates(ticket, component, [moneyline]),
                )

    def test_unmatched_set_spread_watch_policy_disables_moneyline_fallback(self):
        component = {
            **self.component,
            "selection": "Mensik",
            "market_type": "spread",
            "market_line": 1.5,
            "line_unit": "set",
        }
        ticket = {
            **self.ticket,
            "sport_key": "tennis",
            "selection": "Mensik",
            "market_type": "spread",
            "market_line": 1.5,
            "line_unit": "set",
            "pick_type": "parlay_leg",
            "components": [component],
        }
        moneyline = {
            **self.candidate,
            "sport_key": "tennis_atp",
            "selected_team": "Mensik",
            "market_type": "moneyline",
            "market_line": None,
        }
        updates = []

        with (
            patch.object(sports, "list_capper_tickets", return_value=[ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(
                sports,
                "update_capper_ticket",
                side_effect=lambda _ticket_id, values, history_reason=None: updates.append(dict(values)),
            ),
        ):
            result = sports.process_trusted_capper_tickets(
                {"balance": 1000, "bets": [], "history": []},
                [moneyline],
                {},
                0,
                games=[],
            )

        self.assertEqual(1, result["waiting"])
        self.assertEqual("unmatched", updates[-1]["status"])
        self.assertIn("set_spread_market", updates[-1]["status_reason"])
        self.assertFalse(updates[-1]["watch_policy"]["spread_moneyline_fallback"])
        self.assertTrue(updates[-1]["watch_policy"]["derivative_line_unit_strict"])
        self.assertEqual(["set"], updates[-1]["watch_policy"]["required_line_units"])

    def test_synthetic_run_line_does_not_substitute(self):
        component = {**self.component, "market_type": "spread", "market_line": -1.5}
        ticket = {
            **self.ticket,
            "pick_type": "synthetic_run_line",
            "market_type": "synthetic_run_line",
            "components": [component],
        }
        better = {**self.candidate, "market_type": "spread", "market_line": -1, "kalshi_ticker": "BETTER"}
        self.assertEqual([], sports.capper_component_candidates(ticket, component, [better]))

    def test_parlay_leg_uses_trusted_straight_rules_not_parent_odds(self):
        component = {**self.component, "selection": "Ben Shelton"}
        ticket = {
            **self.ticket,
            "selection": "Ben Shelton",
            "posted_odds": None,
            "pick_type": "parlay_leg",
            "price_strategy": "trusted_straight_current_value",
            "parlay_leg_max_units": 2,
            "components": [component],
        }
        candidate = {
            **self.candidate,
            "selected_team": "Ben Shelton",
            "sport_key": "tennis_atp",
            "edge": 4.0,
            "confidence_score": 90,
            "independent_book_family_count": 3,
            "pro_review": {"score": 96},
            "final_bet_score": 96,
        }
        ticket["sport_key"] = "tennis"
        matches = sports.capper_component_candidates(ticket, component, [candidate])
        self.assertEqual(1, len(matches))
        review = sports.capper_ticket_price_review(ticket, [(component, matches[0][1])])
        self.assertTrue(review["ok"])
        self.assertEqual("parlay_leg_trusted_straight_qualified", review["reason"])
        self.assertEqual(review["effective_probability"], review["posted_probability"])
        with (
            patch.object(sports, "pro_decision_score", return_value={"score": 96}),
            patch.object(sports, "final_bet_score", return_value={"score": 96}),
        ):
            quality = sports.capper_ticket_quality(ticket, [(component, matches[0][1])], {})
        self.assertEqual(2, quality["target_units"])

        even_edge = {**candidate, "edge": 0.0}
        self.assertTrue(sports.capper_parlay_leg_value_review(even_edge)["ok"])

        small_negative_pregame = {**candidate, "edge": -0.99}
        self.assertTrue(sports.capper_parlay_leg_value_review(small_negative_pregame)["ok"])

        negative_pregame = {**candidate, "edge": -1.01}
        failed = sports.capper_parlay_leg_value_review(negative_pregame)
        self.assertFalse(failed["ok"])
        self.assertIn("edge", failed["failures"])

        live_small_disagreement = {**candidate, "game_started": True, "edge": -1.99}
        self.assertTrue(sports.capper_parlay_leg_value_review(live_small_disagreement)["ok"])

        outside_price_range = {**candidate, "entry_price": 68, "edge": 5}
        failed = sports.capper_parlay_leg_value_review(outside_price_range)
        self.assertFalse(failed["ok"])
        self.assertIn("price_range", failed["failures"])

    def test_event_date_uses_central_time_and_rejects_tomorrow(self):
        # 01:00 UTC is still Aug 9 in Chicago and remains eligible.
        same_central_day = {**self.candidate, "commence_time": "2026-08-10T01:00:00Z"}
        self.assertEqual(1, len(sports.capper_component_candidates(self.ticket, self.component, [same_central_day])))
        # 06:00 UTC is Aug 10 in Chicago and must never match the Aug 9 ticket.
        tomorrow_central = {**self.candidate, "commence_time": "2026-08-10T06:00:00Z"}
        self.assertEqual([], sports.capper_component_candidates(self.ticket, self.component, [tomorrow_central]))
        self.assertEqual([], sports.capper_component_candidates(self.ticket, self.component, [{**self.candidate, "commence_time": None}]))

    def test_priority_path_places_mapped_two_units_without_normal_quality_veto(self):
        updates = []

        def capture_update(ticket_id, values, history_reason=None):
            updates.append((ticket_id, dict(values)))
            return {**self.ticket, **values}

        order = {
            "ok": True,
            "actual_stake": 20,
            "contracts": 2,
            "price": 52,
            "candidate_updates": {"entry_price": 52},
            "response": {},
            "request": {"client_order_id": "test-order"},
        }
        placed_bet = {**self.candidate, "source": "trusted_capper", "stake": 20}
        with (
            patch.object(sports, "list_capper_tickets", return_value=[self.ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "update_capper_ticket", side_effect=capture_update),
            patch.object(sports, "capper_candidate_hard_reason", return_value=""),
            patch.object(sports, "capper_ticket_price_review", return_value={
                "ok": True,
                "posted_probability": 53.704,
                "tolerance_pp": 0,
                "components": [{"component_id": "component-1", "risk_fraction": 1, "effective_probability": 53}],
            }),
            patch.object(sports, "capper_ticket_quality", return_value={"target_units": 2}),
            patch.object(sports, "sports_live_campaign_context", return_value={"bot_live_active_open_count": 10}),
            patch.object(sports, "live_trading_ready", return_value=True),
            patch.object(sports, "live_execution_preflight_skip_reason", return_value=""),
            patch.object(sports, "effective_live_max_stake", return_value=300),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=0),
            patch.object(sports, "effective_live_campaign_daily_loss_cap", return_value=0),
            patch.object(sports, "place_live_kalshi_order_with_retry", return_value=order) as place_order,
            patch.object(sports, "capper_record_filled_bet", return_value=placed_bet),
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True),
        ):
            result = sports.process_trusted_capper_tickets(
                {"balance": 1000, "bets": [], "history": []},
                [self.candidate],
                {},
                0,
            )
        self.assertEqual(1, len(result["placed"]))
        self.assertEqual(20, place_order.call_args.args[1])
        self.assertTrue(place_order.call_args.args[0]["trusted_capper"])
        self.assertEqual("placed", updates[-1][1]["status"])
        self.assertTrue(updates[-1][1]["last_scan_at"])
        self.assertEqual(1, updates[-1][1]["scan_count"])

    def test_promoted_live_pick_keeps_half_units_through_medium_ai_review(self):
        updates = []
        live_candidate = {
            **self.candidate,
            "game_started": True,
            "has_live_score_context": True,
            "live_odds_age_minutes": 0.5,
        }
        order = {
            "ok": True,
            "actual_stake": 70,
            "contracts": 134,
            "price": 52,
            "candidate_updates": {},
            "response": {},
            "request": {"client_order_id": "test-full-units"},
        }
        with (
            patch.object(sports, "SPORTS_CAPPER_FULL_POSTED_UNITS_ENABLED", True),
            patch.object(sports, "list_capper_tickets", return_value=[self.ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "update_capper_ticket", side_effect=lambda ticket_id, values, history_reason=None: updates.append(dict(values))),
            patch.object(sports, "capper_candidate_hard_reason", return_value=""),
            patch.object(sports, "capper_ticket_price_review", return_value={
                "ok": True,
                "posted_probability": 53.704,
                "tolerance_pp": 0,
                "components": [{"component_id": "component-1", "risk_fraction": 1, "effective_probability": 53}],
            }),
            patch.object(sports, "capper_ticket_quality", return_value={
                "target_units": 3.5,
                "sizing_band": "trusted_capper_full_posted_units",
            }),
            patch.object(sports, "deterministic_ai_clearance", return_value=None) as deterministic_review,
            patch.object(sports, "sports_ai_risk_check", return_value={
                "ok": True,
                "risk_level": "medium",
                "effective_provider": "deterministic",
            }) as ai_review,
            patch.object(sports, "live_trading_ready", return_value=True),
            patch.object(sports, "live_execution_preflight_skip_reason", return_value=""),
            patch.object(sports, "live_stake_step_down_review", return_value={"approved_units": 3.5}),
            patch.object(sports, "effective_sports_unit_size", return_value=20),
            patch.object(sports, "sports_unit_bankroll_base", return_value=1000),
            patch.object(sports, "place_live_kalshi_order_with_retry", return_value=order) as place_order,
            patch.object(sports, "capper_record_filled_bet", return_value={"id": "placed"}),
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True),
        ):
            result = sports.process_trusted_capper_tickets(
                {"balance": 1000, "bets": [], "history": []},
                [live_candidate],
                {},
                0,
            )
        deterministic_review.assert_called_once()
        ai_review.assert_called_once()
        self.assertEqual(1, len(result["placed"]))
        self.assertEqual(70, place_order.call_args.args[1])
        placed_candidate = place_order.call_args.args[0]
        self.assertEqual(3.5, placed_candidate["sports_units"]["target_units"])
        self.assertEqual(
            "trusted_capper_full_posted_units",
            placed_candidate["sports_units"]["source"],
        )
        self.assertEqual(
            "high_critical_veto_only_full_posted_units",
            placed_candidate["trusted_capper_quality"]["ai_risk_policy"],
        )

    def test_exact_manual_position_satisfies_ticket_before_new_order_edge_gate(self):
        updates = []

        def capture_update(ticket_id, values, history_reason=None):
            updates.append((ticket_id, dict(values)))
            return {**self.ticket, **values}

        portfolio = {
            "balance": 1000,
            "bets": [{
                "status": "open",
                "source": "manual_import",
                "kalshi_ticker": self.candidate["kalshi_ticker"],
                "order_side": self.candidate["order_side"],
                "unit_count": 2.447,
            }],
            "history": [],
        }
        with (
            patch.object(sports, "list_capper_tickets", return_value=[self.ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "update_capper_ticket", side_effect=capture_update),
            patch.object(sports, "capper_candidate_hard_reason", return_value="capper_live_edge_below_floor"),
            patch.object(sports, "capper_ticket_price_review", return_value={
                "ok": True,
                "posted_probability": 53.704,
                "tolerance_pp": 0,
                "components": [{"component_id": "component-1", "risk_fraction": 1, "effective_probability": 53}],
            }),
            patch.object(sports, "capper_ticket_quality", return_value={"target_units": 2}),
            patch.object(sports, "place_live_kalshi_order_with_retry") as place_order,
        ):
            result = sports.process_trusted_capper_tickets(
                portfolio,
                [self.candidate],
                {},
                0,
            )

        place_order.assert_not_called()
        self.assertEqual(0, result["blocked"])
        self.assertEqual(1, result["ready"])
        self.assertEqual("placed", updates[-1][1]["status"])
        self.assertEqual("target_satisfied_by_existing_position", updates[-1][1]["status_reason"])
        self.assertEqual(2.447, updates[-1][1]["placed_units"])
        self.assertEqual({"component-1": 2.447}, updates[-1][1]["component_achieved_units"])
        self.assertFalse(updates[-1][1]["manual_fallback_recommended"])
        self.assertEqual("", updates[-1][1]["manual_fallback_reason"])

    def test_stored_exact_total_match_credits_manual_position_after_book_line_disappears(self):
        updates = []
        component = {
            **self.component,
            "component_id": "total-component",
            "selection": "Over 22.5",
            "market_type": "total",
            "market_line": 22.5,
            "line_unit": "game",
            "total_side": "over",
            "event_hint": "Paul/Cobolli",
        }
        ticket = {
            **self.ticket,
            "sport_key": "tennis",
            "selection": "Over 22.5",
            "market_type": "total",
            "market_line": 22.5,
            "line_unit": "game",
            "components": [component],
            "first_match_snapshot": {
                "at": "2026-08-20T15:29:31-05:00",
                "quality": {"target_units": 2},
                "components": [{
                    "component_id": "total-component",
                    "ticker": "KXATPGTOTAL-26AUG20PAUCOB-23",
                    "selection": "Over 22.5",
                    "market_type": "total",
                    "market_line": 22.5,
                    "line_substitution": None,
                }],
            },
        }
        market = {
            "ticker": "KXATPGTOTAL-26AUG20PAUCOB-23",
            "series_ticker": "KXATPGTOTAL",
            "title": "Over 22.5 games",
        }
        portfolio = {
            "balance": 1000,
            "bets": [{
                "status": "open",
                "source": "user_manual",
                "kalshi_ticker": market["ticker"],
                "order_side": "yes",
                "unit_count": 2.4,
            }],
            "history": [],
        }

        with (
            patch.object(sports, "list_capper_tickets", return_value=[ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(
                sports,
                "update_capper_ticket",
                side_effect=lambda ticket_id, values, history_reason=None: updates.append((ticket_id, dict(values))),
            ),
            patch.object(sports, "place_live_kalshi_order_with_retry") as place_order,
        ):
            result = sports.process_trusted_capper_tickets(
                portfolio,
                [],
                {},
                0,
                kalshi_markets=[market],
                games=[],
            )

        place_order.assert_not_called()
        self.assertEqual(0, result["waiting"])
        self.assertEqual(1, result["ready"])
        self.assertEqual("placed", updates[-1][1]["status"])
        self.assertEqual("target_satisfied_by_existing_position", updates[-1][1]["status_reason"])
        self.assertEqual(2.4, updates[-1][1]["placed_units"])
        self.assertEqual("yes", updates[-1][1]["existing_position_attribution"]["total-component"]["side"])

    def test_recorded_component_fills_follow_equivalent_contract_switch(self):
        ticket = {
            **self.ticket,
            "target_units": 3,
            "placed_units": 0.997,
            "component_achieved_units": {"component-1": 0.997},
            "fills": [
                {"component_id": "component-1", "ticker": "YES-TICKER", "units": 0.997},
                {"component_id": "component-1", "ticker": "YES-TICKER", "units": 0.997},
                {"component_id": "component-1", "ticker": "NO-TICKER", "units": 0.997},
            ],
        }
        equivalent_candidate = {
            **self.candidate,
            "kalshi_ticker": "NO-TICKER",
            "order_side": "no",
        }
        portfolio = {
            "balance": 1000,
            "bets": [
                {
                    "status": "open",
                    "kalshi_ticker": fill["ticker"],
                    "order_side": "yes" if fill["ticker"] == "YES-TICKER" else "no",
                    "trusted_capper_ticket_id": "ticket-1",
                    "trusted_capper_component_id": "component-1",
                    "unit_count": fill["units"],
                }
                for fill in ticket["fills"]
            ],
            "history": [],
        }

        self.assertEqual(
            2.991,
            sports.capper_existing_market_units(
                portfolio,
                equivalent_candidate,
                ticket=ticket,
                component=self.component,
            ),
        )
        self.assertTrue(sports.capper_target_satisfied(2.991, 3))
        self.assertFalse(sports.capper_target_satisfied(2.94, 3))

    def test_equivalent_contract_switch_cannot_fill_past_ticket_target(self):
        updates = []

        def capture_update(ticket_id, values, history_reason=None):
            updates.append((ticket_id, dict(values)))
            return {**ticket, **values}

        ticket = {
            **self.ticket,
            "target_units": 3,
            "placed_units": 0.997,
            "component_achieved_units": {"component-1": 0.997},
            "fills": [
                {"component_id": "component-1", "ticker": "YES-TICKER", "units": 0.997},
                {"component_id": "component-1", "ticker": "YES-TICKER", "units": 0.997},
                {"component_id": "component-1", "ticker": "YES-TICKER", "units": 0.997},
            ],
        }
        equivalent_candidate = {
            **self.candidate,
            "kalshi_ticker": "NO-TICKER",
            "order_side": "no",
        }
        with (
            patch.object(sports, "list_capper_tickets", return_value=[ticket]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "update_capper_ticket", side_effect=capture_update),
            patch.object(sports, "capper_candidate_hard_reason", return_value=""),
            patch.object(sports, "capper_ticket_price_review", return_value={
                "ok": True,
                "posted_probability": 53.704,
                "tolerance_pp": 0,
                "components": [{"component_id": "component-1", "risk_fraction": 1, "effective_probability": 53}],
            }),
            patch.object(sports, "capper_ticket_quality", return_value={"target_units": 3}),
            patch.object(sports, "live_trading_ready", return_value=True),
            patch.object(sports, "place_live_kalshi_order_with_retry") as place_order,
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True),
        ):
            result = sports.process_trusted_capper_tickets(
                {"balance": 1000, "bets": [], "history": []},
                [equivalent_candidate],
                {},
                0,
            )

        self.assertEqual([], result["placed"])
        place_order.assert_not_called()
        self.assertEqual("placed", updates[-1][1]["status"])
        self.assertEqual(2.991, updates[-1][1]["placed_units"])
        self.assertEqual(
            {"component-1": 2.991},
            updates[-1][1]["component_achieved_units"],
        )

    def test_trusted_position_is_separate_but_still_counts_toward_market_unit_cap(self):
        portfolio = {
            "bets": [{
                "source": "trusted_capper",
                "trusted_capper_ticket_id": "old-ticket",
                "strategy_owner": "live_campaign",
                "status": "open",
                "kalshi_ticker": self.candidate["kalshi_ticker"],
                "order_side": "yes",
                "unit_count": 3,
            }],
            "history": [],
        }
        self.assertTrue(sports.ensure_live_campaign_bot_numbers(portfolio))
        self.assertEqual("trusted_capper", portfolio["bets"][0]["strategy_owner"])
        self.assertEqual(3, sports.sports_unit_existing_market_units(portfolio, self.candidate, bot_number=1))

    def test_trusted_preflight_ignores_full_normal_slot_pool(self):
        candidate = {
            **self.candidate,
            "trusted_capper": True,
            "game_started": True,
            "has_live_score_context": True,
            "live_campaign": {"active": True, "bot_number": 1},
        }
        full_campaign = {
            "complete": False,
            "daily_loss_cap_hit": False,
            "bot_live_active_open_count": 10,
            "daily_loss_remaining": 0,
            "daily_loss_cap": 0,
        }
        full_portfolio = {
            "bets": [
                {
                    "status": "open",
                    "mode": "live",
                    "strategy_owner": "live_campaign",
                }
                for _ in range(sports.SPORTS_LIVE_CAMPAIGN_MAX_OPEN)
            ]
        }
        with (
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True),
            patch.object(sports, "live_trading_ready", return_value=True),
            patch.object(sports, "load_live_order_intents", return_value={"intents": []}),
            patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""),
            patch.object(sports, "sports_live_campaign_context", return_value=full_campaign),
            patch.object(sports, "configured_live_campaign_bot_count", return_value=1),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=0),
            patch.object(
                sports,
                "live_order_review",
                return_value={"ok": True, "approved_stake": 20},
            ),
        ):
            self.assertEqual("", sports.live_execution_preflight_skip_reason(full_portfolio, candidate, 20))
            self.assertEqual(
                "campaign_open_slots_full",
                sports.live_execution_preflight_skip_reason(full_portfolio, {**candidate, "trusted_capper": False}, 20),
            )

    def test_pending_capper_ticket_does_not_reserve_normal_lane_money(self):
        candidate = {
            **self.candidate,
            "trusted_capper": False,
            "game_started": True,
            "has_live_score_context": True,
            "live_campaign": {"active": True, "bot_number": 1},
        }
        campaign = {
            "complete": False,
            "daily_loss_cap_hit": False,
            "bot_live_active_open_count": 0,
            "daily_loss_remaining": 1000,
            "daily_loss_cap": 1000,
            "global_daily_loss_cap": 1000,
        }
        with (
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True),
            patch.object(sports, "live_trading_ready", return_value=True),
            patch.object(sports, "load_live_order_intents", return_value={"intents": []}),
            patch.object(sports, "live_reconciliation_preflight_skip_reason", return_value=""),
            patch.object(sports, "sports_live_campaign_context", return_value=campaign),
            patch.object(sports, "configured_live_campaign_bot_count", return_value=1),
            patch.object(sports, "effective_live_open_exposure_cap", return_value=200),
            patch.object(sports, "effective_live_max_stake", return_value=300),
            patch.object(sports, "live_exposure", return_value=100),
            patch.object(sports, "effective_sports_unit_size", return_value=20),
            patch.object(sports, "trusted_capper_active_sports", return_value={"americanfootball_nfl"}),
            patch.object(sports, "live_group_exposure_review", return_value={"ok": True}),
            patch.object(
                sports,
                "live_order_review",
                return_value={"ok": True, "approved_stake": 30},
            ),
        ):
            self.assertEqual(
                "",
                sports.live_execution_preflight_skip_reason({"bets": []}, candidate, 30),
            )
            self.assertEqual(
                "",
                sports.live_execution_preflight_skip_reason(
                    {"bets": []}, {**candidate, "trusted_capper": True}, 30
                ),
            )

    def test_approved_soccer_ticket_can_unlock_only_actionable_full_match_series(self):
        market = {
            "ticker": "KXSOCCERGAME-TEST-PAL",
            "event_ticker": "KXSOCCERGAME-TEST",
            "series_ticker": "KXSOCCERGAME",
            "title": "Will Palmeiras win the match?",
        }
        cache = {"at": sports.time.monotonic(), "sports": {"soccer"}}
        with (
            patch.object(sports, "_TRUSTED_CAPPER_ACTIVE_SPORT_CACHE", cache),
            patch.object(sports, "_DYNAMIC_ACTIONABLE_KALSHI_SERIES", {"KXSOCCERGAME"}),
            patch.object(sports, "_KALSHI_SPORTS_SERIES_META", {
                "KXSOCCERGAME": {"tags": {"soccer"}, "title": "soccer match winner", "contract_key": "GAME"}
            }),
        ):
            self.assertTrue(sports.is_supported_kalshi_series(market))
            self.assertEqual("soccer", sports.kalshi_market_canonical_sport(market))
            self.assertTrue(sports.kalshi_sport_identity_matches_provider("soccer", "soccer_brazil_campeonato"))

    def test_odds_exhaustion_requires_authoritative_zero_for_every_configured_account(self):
        with patch.object(sports, "ODDS_API_KEYS", ["one", "two"]):
            self.assertTrue(sports.odds_provider_accounts_exhausted({
                "accounts": {
                    "0": {"exhausted": True, "provider_remaining": 0},
                    "1": {"exhausted": True, "provider_remaining": 0},
                }
            }))
            self.assertFalse(sports.odds_provider_accounts_exhausted({
                "accounts": {
                    "0": {"exhausted": True, "provider_remaining": 0},
                    "1": {"exhausted": False, "provider_remaining": 10},
                }
            }))
            self.assertFalse(sports.odds_provider_accounts_exhausted({
                "accounts": {
                    "0": {"exhausted": True, "provider_remaining": 0},
                }
            }))

    def test_odds_exhausted_builder_uses_exact_tennis_match_not_derivatives(self):
        target = datetime.now(sports.LOCAL_TZ).date()
        stamp = target.strftime("%y%b%d").upper()
        component = {
            "component_id": "tennis-player",
            "selection": "Tomas Machac",
            "market_type": "moneyline",
            "market_line": None,
            "risk_fraction": 1.0,
        }
        ticket = {
            **self.ticket,
            "ticket_id": "tennis-ticket",
            "sport_key": "tennis",
            "selection": "Tomas Machac",
            "target_event_date": target.isoformat(),
            "components": [component],
        }
        markets = [
            {
                "ticker": f"KXATPMATCH-{stamp}ROTMAC-MAC",
                "event_ticker": f"KXATPMATCH-{stamp}ROTMAC",
                "series_ticker": "KXATPMATCH",
                "title": "Tomas Machac wins",
                "yes_sub_title": "Tomas Machac",
                "status": "active",
                "yes_bid": 55,
                "yes_ask": 56,
                "no_bid": 44,
                "no_ask": 45,
                "volume": 1000,
                "liquidity": 1000,
            },
            {
                "ticker": f"KXATPGSPREAD-{stamp}ROTMAC-MAC4",
                "event_ticker": f"KXATPGSPREAD-{stamp}ROTMAC",
                "series_ticker": "KXATPGSPREAD",
                "title": "Will Tomas Machac win at least 3.5 more games?",
                "yes_sub_title": "Tomas Machac -3.5 games",
                "status": "active",
                "yes_bid": 49,
                "yes_ask": 50,
                "no_bid": 50,
                "no_ask": 51,
                "volume": 1000,
                "liquidity": 1000,
            },
        ]
        with (
            patch.object(sports, "odds_provider_accounts_exhausted", return_value=True),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "market_with_stream_snapshot", side_effect=lambda market: (market, {})),
        ):
            rows = sports.build_trusted_capper_odds_exhausted_candidates([ticket], markets)
        self.assertEqual(1, len(rows))
        self.assertEqual("KXATPMATCH", rows[0]["kalshi_ticker"].split("-")[0])
        self.assertEqual("Tomas Machac", rows[0]["selected_team"])
        self.assertTrue(rows[0]["capper_event_identity_verified"])
        self.assertTrue(rows[0]["capper_odds_api_exhausted_fallback"])

    def test_provider_missing_builder_recovers_only_exact_cfb_total_with_posted_price_guard(self):
        target = datetime.now(sports.LOCAL_TZ).date()
        stamp = target.strftime("%y%b%d").upper()
        component = {
            "component_id": "miami-stanford-under",
            "selection": "Under 48.5",
            "event_hint": "Miami/Stanford",
            "market_type": "total",
            "market_line": 48.5,
            "total_side": "under",
            "risk_fraction": 1.0,
        }
        ticket = {
            **self.ticket,
            "ticket_id": "miami-stanford-ticket",
            "sport_key": "americanfootball_ncaaf",
            "sport_label": "CFB",
            "selection": "Under 48.5",
            "market_type": "total",
            "posted_odds": -116,
            "target_event_date": target.isoformat(),
            "components": [component],
        }
        exact = {
            "ticker": f"KXNCAAFTOTAL-{stamp}MIASTAN-49",
            "event_ticker": f"KXNCAAFTOTAL-{stamp}MIASTAN",
            "series_ticker": "KXNCAAFTOTAL",
            "exchange_index": 0,
            "title": "Miami Hurricanes at Stanford Cardinal: Over 48.5 points scored",
            "status": "active",
            "yes_bid": 43,
            "yes_ask": 44,
            "no_bid": 56,
            "no_ask": 57,
            "volume": 1000,
            "liquidity": 1000,
        }
        alternate = {
            **exact,
            "ticker": f"KXNCAAFTOTAL-{stamp}MIASTAN-50",
            "title": "Miami Hurricanes at Stanford Cardinal: Over 49.5 points scored",
        }
        game = {
            "id": "miami-stanford",
            "sport_key": "americanfootball_ncaaf",
            "sport_title": "NCAAF",
            "home_team": "Stanford Cardinal",
            "away_team": "Miami Hurricanes",
            "commence_time": datetime(
                target.year,
                target.month,
                target.day,
                20,
                tzinfo=sports.LOCAL_TZ,
            ).astimezone(timezone.utc).isoformat(),
        }
        self.assertGreater(
            sports.capper_cfb_name_match_score("Miami (FL)", "Miami Hurricanes"),
            0,
        )
        self.assertEqual(
            0,
            sports.capper_cfb_name_match_score("Miami (FL)", "Miami (OH) RedHawks"),
        )
        with (
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "capper_odds_fallback_schedule_game", return_value=game),
            patch.object(sports, "market_with_stream_snapshot", side_effect=lambda market: (market, {})),
            patch.object(
                sports,
                "market_prices",
                side_effect=lambda market: {
                    "yes_bid": market["yes_bid"],
                    "yes_ask": market["yes_ask"],
                    "no_bid": market["no_bid"],
                    "no_ask": market["no_ask"],
                },
            ),
            patch.object(sports, "is_supported_kalshi_series", return_value=True),
            patch.object(sports, "kalshi_market_canonical_sport", return_value="americanfootball_ncaaf"),
            patch.object(sports, "kalshi_market_date", return_value=target),
            patch.object(sports, "capper_odds_fallback_market_type", return_value="total"),
            patch.object(
                sports,
                "classify_kalshi_market",
                side_effect=lambda market: {
                    "type": "total",
                    "side": "Over",
                    "point": 48.5 if market is exact else 49.5,
                },
            ),
            patch.object(sports, "capper_sport_matches", return_value=True),
            patch.object(
                sports,
                "capper_odds_fallback_selection_side",
                return_value={
                    "order_side": "no",
                    "selected_team": "Under 48.5",
                    "market_type": "total",
                    "total_side": "under",
                },
            ),
        ):
            rows = sports.build_trusted_capper_provider_missing_candidates(
                [ticket],
                [exact, alternate],
                [],
            )

        self.assertEqual(1, len(rows))
        candidate = rows[0]
        self.assertEqual(exact["ticker"], candidate["kalshi_ticker"])
        self.assertEqual(("total", "under", "no", 48.5), (
            candidate["market_type"],
            candidate["total_side"],
            candidate["order_side"],
            candidate["market_line"],
        ))
        self.assertTrue(candidate["capper_event_identity_verified"])
        self.assertTrue(candidate["capper_provider_event_missing_fallback"])
        self.assertFalse(candidate["capper_odds_api_exhausted_fallback"])
        self.assertEqual(0, candidate["exchange_index"])
        self.assertNotEqual(
            "odds_api_exhausted_capper_price_in_range",
            sports.capper_ticket_price_review(ticket, [(component, candidate)])["reason"],
        )

    def test_odds_exhausted_capper_bypasses_strategy_checks_but_not_price_range(self):
        candidate = {
            **self.candidate,
            "capper_odds_api_exhausted_fallback": True,
            "capper_event_identity_verified": True,
            "game_started": True,
            "has_live_score_context": False,
            "live_odds_age_minutes": None,
            "edge": None,
            "confidence_score": 0,
            "book_count": 0,
            "independent_book_family_count": 0,
            "entry_price": 52,
        }
        with patch.object(sports, "SPORTS_MIN_KALSHI_LIQUIDITY", 0):
            self.assertEqual("", sports.capper_candidate_hard_reason({"bets": []}, candidate))
            self.assertEqual(
                "capper_quote_outside_configured_odds_range",
                sports.capper_candidate_hard_reason(
                    {"bets": []},
                    {**candidate, "entry_price": sports.SPORTS_LIVE_CAMPAIGN_MAX_PRICE_CENTS + 1},
                ),
            )
        review = sports.capper_ticket_price_review(
            {**self.ticket, "posted_odds": -1000},
            [(self.component, candidate)],
        )
        quality = sports.capper_ticket_quality(
            {**self.ticket, "capper_units": 3.5},
            [(self.component, candidate)],
            {},
        )
        self.assertTrue(review["ok"])
        self.assertTrue(review["strategy_checks_bypassed"])
        self.assertEqual(3.5, quality["target_units"])
        self.assertTrue(quality["strategy_checks_bypassed"])

    def test_odds_exhausted_order_guard_rechecks_only_live_kalshi_range(self):
        candidate = {
            **self.candidate,
            "entry_price": 52,
            "edge": None,
            "capper_posted_odds": -1000,
            "capper_odds_api_exhausted_fallback": True,
            "capper_event_identity_verified": True,
        }
        market = {"yes_bid": 53, "yes_ask": 54, "no_bid": 46, "no_ask": 47}
        with (
            patch.object(sports, "sports_stream", return_value=None),
            patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=market),
        ):
            review = sports.trusted_capper_order_pricing_guard(candidate)
        self.assertTrue(review["ok"])
        self.assertIsNone(review["required_edge"])
        self.assertEqual("odds_api_exhausted_capper_price_in_range", review["capper_price_review"]["reason"])

        with (
            patch.object(sports, "sports_stream", return_value=None),
            patch.object(sports, "fetch_kalshi_market_by_ticker", return_value={
                **market,
                "yes_bid": sports.SPORTS_LIVE_CAMPAIGN_MAX_PRICE_CENTS,
                "yes_ask": sports.SPORTS_LIVE_CAMPAIGN_MAX_PRICE_CENTS + 1,
            }),
        ):
            blocked = sports.trusted_capper_order_pricing_guard(candidate)
        self.assertFalse(blocked["ok"])
        self.assertEqual("capper_quote_outside_configured_odds_range", blocked["error"])

    def test_odds_exhausted_order_guard_requires_fresh_rest_market_existence(self):
        candidate = {
            **self.candidate,
            "capper_odds_api_exhausted_fallback": True,
            "capper_event_identity_verified": True,
        }
        stream = unittest.mock.Mock()
        stream.snapshot.return_value = {
            "fresh": True,
            "yes_bid": 53,
            "yes_ask": 54,
        }
        with (
            patch.object(sports, "sports_stream", return_value=stream),
            patch.object(
                sports, "fetch_kalshi_market_by_ticker", return_value=None
            ) as fetch_market,
        ):
            review = sports.trusted_capper_order_pricing_guard(candidate)
        self.assertFalse(review["ok"])
        self.assertEqual("capper_market_unavailable_at_order", review["error"])
        fetch_market.assert_called_once_with(candidate["kalshi_ticker"])
        stream.snapshot.assert_not_called()

    def test_market_not_found_is_definitive_not_an_ambiguous_submission(self):
        rejected = sports.definitive_live_order_submission_rejection(
            RuntimeError(
                'HTTP 404: {"error":{"code":"market_not_found","message":"market not found"}}'
            )
        )
        timeout = sports.definitive_live_order_submission_rejection(
            TimeoutError("request timed out")
        )
        self.assertTrue(rejected["definitive"])
        self.assertEqual("live_order_market_not_found", rejected["error"])
        self.assertFalse(timeout["definitive"])
        self.assertEqual("live_order_submission_ambiguous", timeout["error"])

    def test_insufficient_balance_is_definitive_not_an_ambiguous_submission(self):
        rejected = sports.definitive_live_order_submission_rejection(
            RuntimeError('HTTP 400: {"error":{"code":"insufficient_balance"}}')
        )
        self.assertTrue(rejected["definitive"])
        self.assertEqual("live_order_insufficient_balance", rejected["error"])
        self.assertEqual("insufficient_balance", rejected["exchange_code"])

    def test_recovery_immediately_clears_stored_market_not_found_intent(self):
        intent = {
            "client_order_id": "missing-market-order",
            "status": "ambiguous",
            "ticker": "MISSING-MARKET",
            "submit_error": (
                'HTTP 404: {"error":{"code":"market_not_found",'
                '"message":"market not found"}}'
            ),
        }
        with (
            patch.object(
                sports,
                "load_live_order_intents",
                return_value={"intents": [intent]},
            ),
            patch.object(sports, "upsert_live_order_intent") as update_intent,
            patch.object(sports, "lookup_order_by_client_id") as lookup_order,
        ):
            recovered = sports.recover_pending_live_order_intents(
                {"balance": 1000, "bets": [], "history": []}
            )
        self.assertEqual([], recovered)
        lookup_order.assert_not_called()
        update_intent.assert_called_once_with(
            "missing-market-order",
            status="rejected_no_order",
            rejection_reason="live_order_market_not_found",
            exchange_code="market_not_found",
            recovered_definitive_rejection=True,
        )

    def test_odds_exhausted_start_guard_allows_persistent_market_watch_without_score_feed(self):
        review = sports.live_order_start_guard({
            "capper_odds_api_exhausted_fallback": True,
            "capper_event_identity_verified": True,
            "game_started": False,
            "game_completed": False,
            "commence_time": None,
            "kalshi_market_start": None,
        })
        self.assertTrue(review["ok"])
        self.assertTrue(review["trusted_capper_odds_api_exhausted_fallback"])

    def test_odds_exhausted_out_of_range_pick_stays_on_live_price_watch(self):
        updates = []
        target = datetime.now(sports.LOCAL_TZ).date()
        ticket = {**self.ticket, "target_event_date": target.isoformat()}
        candidate = {
            **self.candidate,
            "commence_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": sports.SPORTS_LIVE_CAMPAIGN_MAX_PRICE_CENTS + 5,
            "capper_odds_api_exhausted_fallback": True,
            "capper_event_identity_verified": True,
            "capper_fallback_ticket_id": ticket["ticket_id"],
            "capper_fallback_component_id": self.component["component_id"],
        }
        with (
            patch.object(sports, "list_capper_tickets", return_value=[ticket]),
            patch.object(sports, "odds_provider_accounts_exhausted", return_value=True),
            patch.object(sports, "build_trusted_capper_odds_exhausted_candidates", return_value=[candidate]),
            patch.object(sports, "build_trusted_capper_first_inning_candidates", return_value=[]),
            patch.object(sports, "build_trusted_capper_btts_candidates", return_value=[]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "update_capper_ticket", side_effect=lambda ticket_id, values, history_reason=None: updates.append(dict(values))),
            patch.object(sports, "place_live_kalshi_order_with_retry") as place_order,
        ):
            result = sports.process_trusted_capper_tickets(
                {"balance": 1000, "bets": [], "history": []}, [], {}, 0, kalshi_markets=[]
            )
        place_order.assert_not_called()
        self.assertEqual(1, result["waiting"])
        self.assertEqual("waiting_price", updates[-1]["status"])
        self.assertEqual("waiting_for_capper_odds_range", updates[-1]["status_reason"])
        self.assertTrue(updates[-1]["odds_api_exhausted_fallback"])

    def test_odds_exhausted_in_range_pick_reaches_order_without_ai_or_model_gate(self):
        updates = []
        target = datetime.now(sports.LOCAL_TZ).date()
        ticket = {
            **self.ticket,
            "target_event_date": target.isoformat(),
            "capper_units": 3.5,
        }
        candidate = {
            **self.candidate,
            "commence_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": 52,
            "edge": None,
            "confidence_score": 0,
            "book_count": 0,
            "independent_book_family_count": 0,
            "game_started": True,
            "has_live_score_context": False,
            "capper_odds_api_exhausted_fallback": True,
            "capper_event_identity_verified": True,
            "capper_fallback_ticket_id": ticket["ticket_id"],
            "capper_fallback_component_id": self.component["component_id"],
        }
        placed_bet = {"id": "placed", "source": "trusted_capper"}
        with (
            patch.object(sports, "list_capper_tickets", return_value=[ticket]),
            patch.object(sports, "odds_provider_accounts_exhausted", return_value=True),
            patch.object(sports, "build_trusted_capper_odds_exhausted_candidates", return_value=[candidate]),
            patch.object(sports, "build_trusted_capper_first_inning_candidates", return_value=[]),
            patch.object(sports, "build_trusted_capper_btts_candidates", return_value=[]),
            patch.object(sports, "capper_ticket_date_expired", return_value=False),
            patch.object(sports, "update_capper_ticket", side_effect=lambda ticket_id, values, history_reason=None: updates.append(dict(values))),
            patch.object(sports, "sports_ai_risk_check") as ai_review,
            patch.object(sports, "live_trading_ready", return_value=True),
            patch.object(sports, "live_execution_preflight_skip_reason", return_value=""),
            patch.object(sports, "live_stake_step_down_review", return_value={"approved_units": 3.5}),
            patch.object(sports, "effective_sports_unit_size", return_value=20),
            patch.object(sports, "sports_unit_bankroll_base", return_value=1000),
            patch.object(sports, "place_live_kalshi_order_with_retry", return_value={
                "ok": True, "actual_stake": 70, "price": 52, "contracts": 134,
                "candidate_updates": {},
            }) as place_order,
            patch.object(sports, "capper_record_filled_bet", return_value=placed_bet),
            patch.object(sports, "EXECUTION_MODE", "live"),
            patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True),
        ):
            result = sports.process_trusted_capper_tickets(
                {"balance": 1000, "bets": [], "history": []}, [], {}, 0, kalshi_markets=[]
            )
        ai_review.assert_not_called()
        self.assertEqual(1, len(result["placed"]))
        self.assertEqual(1, result["odds_api_exhausted_fallback_placed"])
        self.assertEqual(70, place_order.call_args.args[1])
        self.assertEqual(3.5, place_order.call_args.args[0]["sports_units"]["target_units"])
        self.assertTrue(place_order.call_args.args[0]["capper_odds_api_exhausted_fallback"])


if __name__ == "__main__":
    unittest.main()
