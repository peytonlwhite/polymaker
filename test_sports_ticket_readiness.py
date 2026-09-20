"""Regression cases for imported ticket identity and timezone-safe caching."""
from datetime import datetime, timezone
from collections import Counter
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sports_capper_import as capper
import sports_paper_bettor as sports


class TicketReadinessTests(unittest.TestCase):
    def test_stale_capper_soccer_event_refreshes_before_any_candidate_exists(self):
        game = {"id": "inter-napoli", "sport_key": "soccer_italy_serie_a",
                "home_team": "Inter Milan", "away_team": "Napoli", "commence_time": "2026-09-05T16:00:00Z"}
        market = {"ticker": "KXSERIEAGAME-26SEP05INTNAP-INT", "title": "Inter wins",
                  "yes_sub_title": "Inter", "rules_primary": "Inter vs Napoli",
                  "yes_bid": 49, "yes_ask": 50, "no_bid": 50, "no_ask": 51}
        ticket = {"status": "unmatched", "sport_key": "soccer", "target_event_date": "2026-09-05",
                  "components": [{"market_type": "moneyline", "selection": "Inter Milan"}]}
        fresh = {**game, "bookmakers": [{"key": "pinnacle", "markets": [{
            "key": "h2h", "last_update": "2026-09-05T16:10:00Z", "outcomes": [
                {"name": "Inter Milan", "price": -147}, {"name": "Napoli", "price": 350},
                {"name": "Draw", "price": 250}]}]}]}
        with patch.object(sports, "load_dynamic_actionable_kalshi_series"), \
                patch.object(sports, "_KALSHI_SPORTS_SERIES_META", {"KXSERIEAGAME": {
                    "contract_key": "SOCCERGAME", "tags": {"soccer"}}}), \
                patch.object(sports, "capper_ticket_date_expired", return_value=False), \
                patch.object(sports, "game_started", return_value=True), \
                patch.object(sports, "game_completed", return_value=False), \
                patch.object(sports, "live_odds_age_minutes", return_value=6), \
                patch.object(sports, "live_odds_max_age_minutes", return_value=2.5), \
                patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_ENABLED", True), \
                patch.object(sports, "SPORTS_LATE_LIVE_CONSENSUS_REFRESH_MAX_EVENTS", 1), \
                patch.object(sports, "odds_paid_refresh_pacing", return_value={}), \
                patch.object(sports, "sports_odds_priority_sports", return_value=set()), \
                patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)) as budget, \
                patch.object(sports, "odds_api_get_json", return_value=(fresh, {})) as fetch, \
                patch.object(sports, "record_odds_spend") as spend, \
                patch.object(sports, "append_jsonl"):
            refreshed, status = sports.enrich_with_late_live_consensus_refresh(
                [game], [], capper_tickets=[ticket, ticket], kalshi_markets=[market])
            self.assertEqual(1, status["capper_watch_events"])
            self.assertEqual(1, status["requested_events"])
            self.assertEqual(1, fetch.call_count)
            self.assertIn("/events/inter-napoli/odds", fetch.call_args.args[0])
            self.assertEqual("h2h", fetch.call_args.kwargs["params"]["markets"])
            spend.assert_called_once()
            self.assertEqual("2026-09-05T16:10:00Z", refreshed[0]["bookmakers"][0]["markets"][0]["last_update"])
            fetch.reset_mock()
            for invalid in [{**ticket, "status": "cancelled"},
                            {**ticket, "target_event_date": "2026-09-06"},
                            {**ticket, "components": [{"market_type": "moneyline", "selection": "Inter Miami"}]}]:
                self.assertEqual({}, sports.capper_stale_soccer_moneyline_refreshes([invalid], [game], [market]))
            self.assertEqual({}, sports.capper_stale_soccer_moneyline_refreshes([ticket], [game], [{**market, "yes_ask": 80}]))
            with patch.object(sports, "live_odds_age_minutes", return_value=0.5):
                self.assertEqual({}, sports.capper_stale_soccer_moneyline_refreshes([ticket], [game], [market]))
            budget.return_value = (False, {}, 0)
            _, blocked = sports.enrich_with_late_live_consensus_refresh(
                [game], [], capper_tickets=[ticket], kalshi_markets=[market])
            self.assertEqual(1, blocked["budget_skips"])
            fetch.assert_not_called()

    def test_soccer_fallback_keeps_full_club_identity_and_direct_win_side(self):
        ticket = {"sport_key": "soccer"}
        component = {"market_type": "moneyline", "selection": "Inter Milan"}
        game = {"home_team": "Inter Milan", "away_team": "Napoli"}
        with patch.object(sports, "capper_odds_fallback_schedule_game", return_value=game):
            self.assertEqual("yes", sports.capper_odds_fallback_selection_side(
                ticket, component, {"yes_sub_title": "Inter"}, "moneyline")["order_side"])
            for wrong in ["Inter Miami", "Napoli"]:
                self.assertIsNone(sports.capper_odds_fallback_selection_side(
                    ticket, component, {"yes_sub_title": wrong}, "moneyline"))
        with patch.object(sports, "capper_odds_fallback_schedule_game", return_value=None):
            self.assertIsNone(sports.capper_odds_fallback_selection_side(
                ticket, component, {"yes_sub_title": "Inter Miami"}, "moneyline"))

    def test_soccer_win_never_uses_opponent_no_even_when_book_omits_draw(self):
        game = {"id": "inter-napoli", "sport_key": "soccer_italy_serie_a",
                "home_team": "Inter Milan", "away_team": "Napoli",
                "commence_time": "2026-09-05T16:00:00Z", "bookmakers": [{
                    "key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Inter Milan", "price": -147},
                        {"name": "Napoli", "price": 350},
                        {"name": "Draw", "price": 250}]}]}]}
        market = {"ticker": "KXSERIEAGAME-26SEP05INTNAP-INT", "title": "Inter wins",
                  "yes_sub_title": "Inter", "rules_primary": "Inter vs Napoli after 90 minutes plus stoppage time (does not include extra time or penalties).",
                  "yes_bid": 49, "yes_ask": 50, "no_bid": 50, "no_ask": 51,
                  "volume": 100000, "liquidity": 100000}
        with patch.object(sports, "live_game_allowed", return_value=True), \
                patch.object(sports, "is_supported_kalshi_series", return_value=True), \
                patch.object(sports, "load_dynamic_actionable_kalshi_series"), \
                patch.object(sports, "market_with_stream_snapshot", side_effect=lambda m: (m, None)):
            for sport_key, include_draw in [("soccer_italy_serie_a", True),
                                             ("soccer_italy_serie_a", False),
                                             ("basketball_nba", True)]:
                with self.subTest(sport=sport_key, draw=include_draw):
                    row = copy.deepcopy(game)
                    row["sport_key"] = sport_key
                    if not include_draw:
                        row["bookmakers"][0]["markets"][0]["outcomes"].pop()
                    # Test the outcome mapping independently of league metadata.
                    with patch.object(sports, "market_matches_provider_sport", return_value=True):
                        diagnostics = Counter()
                        self.assertIsNone(sports.evaluate_candidate(
                            row, market, diagnostics, order_side_override="no"))
                        self.assertEqual(1, diagnostics["inverse_moneyline_not_binary"])
            yes = sports.evaluate_candidate(game, market, order_side_override="yes")
            self.assertIsNotNone(yes)
            self.assertEqual(("Inter Milan", "yes"), (yes["selected_team"], yes["order_side"]))
            self.assertEqual(["yes"], [r["order_side"] for r in yes["equivalent_contract_review"]["routes"]])

    @patch.object(sports, "SPORTS_CAPPER_ENABLED", True)
    def test_soccer_moneyline_discovery_fetches_game_series_not_season_winner(self):
        day = datetime.now(sports.LOCAL_TZ).date().isoformat()
        ticket = {"status": "watching", "sport_key": "soccer", "target_event_date": day,
                  "components": [{"selection": "Inter Milan", "market_type": "moneyline"}],
                  "schedule_snapshot": {"games": [{"sport_key": "soccer_italy_serie_a"}]}}
        metadata = {"KXSERIEAGAME": {"contract_key": "SOCCERGAME"},
                    "KXSERIEAWGAME": {"contract_key": "SOCCERGAME"},
                    "KXSERIEA": {"contract_key": "SOCCERLEAGUE"}}
        with patch.object(sports, "list_capper_tickets", return_value=[ticket]), \
                patch.object(sports, "load_dynamic_actionable_kalshi_series"), \
                patch.object(sports, "load_cached_active_sports", return_value=[]), \
                patch.object(sports, "_KALSHI_SPORTS_SERIES_META", metadata):
            self.assertEqual(("KXSERIEAGAME",), sports.trusted_capper_requested_soccer_derivative_series())

    def test_btts_only_event_opens_exact_paid_league_without_enabling_native_btts(self):
        game = {"id": "hull-villa", "sport_key": "soccer_epl", "home_team": "Hull City",
                "away_team": "Aston Villa", "commence_time": "2026-09-05T16:30:00Z"}
        market = {"ticker": "KXEPLBTTS-26SEP05HULAVL-BTTS", "status": "active",
                  "title": "Both Teams To Score", "rules_primary": "Hull City vs Aston Villa",
                  "expected_expiration_time": "2026-09-05T20:30:00Z",
                  "yes_bid": 54, "yes_ask": 55, "no_bid": 45, "no_ask": 46,
                  "volume": 100000, "liquidity": 100000}
        ticket = {"status": "watching", "sport_key": "soccer", "target_event_date": "2026-09-05",
                  "components": [{"selection": "Hull/Aston Villa", "market_type": "btts", "btts_side": "yes"}]}
        schedule = {"sports": {"soccer_epl": {"games": [game]},
                               "soccer_spain_la_liga": {"games": [{**game, "id": "wrong", "home_team": "Real Madrid", "away_team": "Barcelona"}]}}}
        with patch.object(sports, "_KALSHI_SPORTS_SERIES_META", {"KXEPLBTTS": {
                "contract_key": "SOCCERBTTS", "title": "epl btts", "tags": {"soccer"}}}), \
                patch.object(sports, "load_dynamic_actionable_kalshi_series"), \
                patch.object(sports, "capper_ticket_date_expired", return_value=False), \
                patch.object(sports, "SPORTS_PREGAME_ENABLED", True), \
                patch.object(sports, "SPORTS_PREGAME_ALL_OPEN_ENABLED", True):
            kwargs = {"schedule_cache": schedule, "now": datetime(2026, 9, 5, 16, tzinfo=timezone.utc)}
            self.assertEqual([], sports.build_kalshi_first_odds_gate([market], list(schedule["sports"]), **kwargs)["sport_keys"])
            gate = sports.build_kalshi_first_odds_gate([market], list(schedule["sports"]), capper_tickets=[ticket], **kwargs)
            self.assertEqual(["soccer_epl"], gate["sport_keys"])
            self.assertEqual({"soccer_epl": ["hull-villa"]}, gate["actionable_event_ids_by_sport"])
            self.assertEqual({"soccer_epl": ["btts"]}, gate["market_types_by_sport"])
            for invalid in [{**ticket, "status": "cancelled"}, {**ticket, "target_event_date": "2026-09-06"}]:
                self.assertEqual({}, sports.capper_btts_gate_evidence([market], [invalid], schedule))
            self.assertEqual({}, sports.capper_btts_gate_evidence([{**market, "yes_ask": 100}], [ticket], schedule))

    def test_mixed_tennis_cfb_heading_retains_college_league_across_picks(self):
        drafts = capper.parse_capper_text("""🎾 Tennis Add & CFB
💎🎾 Michael Zheng ML -141 5u
🏈 Oregon -24 -124 4u
🏈 Duke -7 -118 4u
🏈 UCLA ML -125 4u
""")["drafts"]
        self.assertEqual(["tennis"] + ["americanfootball_ncaaf"] * 3,
                         [d["sport_key"] for d in drafts])
        self.assertEqual([("Michael Zheng", "moneyline", None, -141, 5),
                          ("Oregon", "spread", -24, -124, 4),
                          ("Duke", "spread", -7, -118, 4),
                          ("UCLA", "moneyline", None, -125, 4)],
                         [(d["selection"], d["market_type"], d.get("market_line"),
                           d["posted_odds"], d["capper_units"]) for d in drafts])

    def test_explicit_nfl_heading_resets_mixed_college_context(self):
        drafts = capper.parse_capper_text("""Tennis Add & CFB
🏈 Oregon -24 -124 4u
NFL Add
🏈 Dolphins ML -110 2u
""")["drafts"]
        self.assertEqual(["americanfootball_ncaaf", "americanfootball_nfl"],
                         [d["sport_key"] for d in drafts])

    def test_parser_migration_repairs_mixed_header_without_changing_posted_pick(self):
        text = "🎾 Tennis Add & CFB\n🏈 Oregon -24 -124 4u"
        draft = capper.parse_capper_text(text)["drafts"][0]
        legacy = {**draft, "ticket_id": "legacy-mixed", "parser_version": 11,
                  "sport_key": "americanfootball_nfl", "sport_label": "NFL",
                  "status": "unmatched", "import_raw_text": text,
                  "schedule_snapshot": {"games": [{"sport_key": "americanfootball_nfl"}]}}
        store = {"tickets": [legacy]}
        self.assertTrue(capper._migrate_parser_metadata(store))
        self.assertEqual("americanfootball_ncaaf", legacy["sport_key"])
        self.assertEqual((-24, -124, 4), (legacy["market_line"], legacy["posted_odds"], legacy["capper_units"]))
        self.assertEqual({}, legacy["schedule_snapshot"])

    def test_inter_milan_correction_rejects_inter_miami_and_survives_migration(self):
        ticket = {"sport_key": "soccer", "target_event_date": "2026-09-05"}
        component = {"selection": "Inter Milan", "market_type": "moneyline"}
        for name, expected in [("Inter Milan", True), ("Inter Miami CF", False)]:
            candidate = {"sport_key": "soccer_italy_serie_a", "selected_team": name,
                         "home_team": name, "away_team": "Napoli", "market_type": "moneyline",
                         "commence_time": "2026-09-05T16:00:00Z"}
            with self.subTest(name=name):
                self.assertEqual(expected, bool(sports.capper_component_selection_match_score(component, candidate, ticket)))
                self.assertEqual(expected, sports.capper_ticket_matches_game(ticket, component, candidate))
        draft = capper.parse_capper_text("Soccer\nInter ML -147 4u")["drafts"][0]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"), \
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"), \
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"):
            row = capper.approve_drafts([draft])["approved"][0]
            capper.update_ticket(row["ticket_id"], {"schedule_snapshot": {"games": [{"home_team": "Inter Miami CF"}]}})
            corrected = capper.edit_ticket(row["ticket_id"], {"selection": "Inter Milan"})
            self.assertEqual({}, corrected["schedule_snapshot"])
            corrected["parser_version"] = 11
            before = copy.deepcopy(corrected)
            capper._migrate_parser_metadata({"tickets": [corrected]})
            self.assertEqual(before, corrected)

    def test_tennis_batch_preserves_straights_parlays_and_set_handicap(self):
        drafts = capper.parse_capper_text("""Tennis
Flavio Cobolli ML -102 5u
Learner Tien ML -120 4u
2-Leg Parlay -102 4u
• Luciano Darderi ML
• Karen Khachanov ML
2-Leg Parlay -145 4u
• Naomi Osaka ML
• Iva Jovic +1.5 Sets
""")["drafts"]
        self.assertEqual(4, len(drafts))
        self.assertEqual([(5, -102), (4, -120), (4, -102), (4, -145)],
                         [(d["capper_units"], d["posted_odds"]) for d in drafts])
        self.assertEqual(["straight", "straight", "parlay", "parlay"],
                         [d["pick_type"] for d in drafts])
        leg = drafts[-1]["legs"][1]
        self.assertEqual("Iva Jovic", leg["selection"])
        self.assertEqual(("spread", 1.5, "set"),
                         (leg["market_type"], leg["market_line"], leg["line_unit"]))

    def test_soccer_parlay_total_keeps_pair_and_double_chance_is_not_moneyline(self):
        drafts = capper.parse_capper_text("""Soccer
2-Leg Parlay -155 4u
• Werder Bremen/Leipzig BTTS YES
• Over 2.5 Match Goals
2-Leg Parlay -105 5u
• Newcastle & Draw
• Newcastle/Bournemouth BTTS YES
""")["drafts"]
        self.assertEqual(2, len(drafts))
        total = drafts[0]["legs"][1]
        self.assertEqual("Werder Bremen/Leipzig", total["event_hint"])
        self.assertEqual(("total", 2.5, "over"),
                         (total["market_type"], total["market_line"], total["total_side"]))
        self.assertEqual("double_chance", drafts[1]["legs"][0]["market_type"])
        self.assertFalse(drafts[1]["legs"][0]["executable"])

    def test_btts_schedule_requires_both_clubs_in_either_order(self):
        ticket = {"sport_key": "soccer", "target_event_date": "2026-09-05"}
        component = {"market_type": "btts", "selection": "Athletic Bilbao/Atletico Madrid"}
        base = {"commence_time": "2026-09-05T14:15:00Z", "sport_key": "soccer_spain_la_liga"}
        for home, away, expected in [
            ("Athletic Bilbao", "Atlético Madrid", True),
            ("Atlético Madrid", "Athletic Bilbao", True),
            ("Sao Paulo", "Atletico Mineiro", False),
            ("Juventude", "Atletico Goianiense", False),
            ("Athletic Bilbao", "Real Madrid", False),
            ("Athletic Bilbao", "Atletico Mineiro", False),
        ]:
            with self.subTest(home=home, away=away):
                self.assertEqual(expected, sports.capper_ticket_matches_game(
                    ticket, component, {**base, "home_team": home, "away_team": away}))

    def test_nottingham_typo_matches_exact_btts_without_wrong_opponent(self):
        selection = "Nottingham Forrest/Tottenham"
        game = {"home_team": "Nottingham Forest", "away_team": "Tottenham Hotspur",
                "sport_key": "soccer_epl", "commence_time": "2026-09-05T14:00:00Z"}
        market = {"ticker": "KXEPLBTTS-26SEP05NFOTOT-BTTS", "title": "Both Teams To Score",
                  "yes_sub_title": "Nottingham Forest vs Tottenham Hotspur"}
        self.assertTrue(sports.capper_btts_selection_matches(selection, game, market))
        self.assertFalse(sports.capper_btts_selection_matches(
            selection, {**game, "away_team": "Leeds United"}, market))

    def test_cache_writer_uses_chicago_on_a_utc_host(self):
        class UtcHostClock(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        with patch.object(sports, "datetime", UtcHostClock):
            cache = {"sports": {}}
            sports.update_odds_cache_for_sport(cache, "baseball_mlb", [])
            self.assertEqual("2026-09-05T10:00:00-05:00",
                             cache["sports"]["baseball_mlb"]["generated_at"])
            self.assertTrue(sports.cached_odds_entry(cache, "baseball_mlb")["valid"])


if __name__ == "__main__":
    unittest.main()
