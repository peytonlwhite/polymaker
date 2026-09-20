import hashlib
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sports_aibetpicks as picks
import sports_paper_bettor as sports

NOW = datetime(2026, 9, 15, 14, 0, tzinfo=picks.CHICAGO)


def source_pick(**changes):
    return {"pick_id": "tennis-pick", "event_id": "tennis-event", "date": "2026-09-15",
            "status": "pending", "sport_key": "tennis_wta_guadalajara_open",
            "home_team": "Sloane Stephens", "away_team": "Janice Tjen", "selection": "Janice Tjen",
            "game": "Janice Tjen vs Sloane Stephens", "pick": "Janice Tjen ML",
            "direction": "away", "commence_time": "2026-09-16T00:30:00Z",
            "market_scope": "full_game", "settlement_rules": "tennis_full_match_games_v1",
            "bet_type": "moneyline", "type": "moneyline", "line": None, "odds": -175,
            "bookmaker": "lowvig", "stake_units": 1, **changes}


def listing(player="Janice Tjen", suffix="TJE", **changes):
    return {"ticker": "KXWTAMATCH-26SEP15STETJE-" + suffix, "event_ticker": "KXWTAMATCH-26SEP15STETJE",
            "title": player + " wins", "yes_sub_title": player, "status": "active",
            "rules_primary": f"If {player} wins the Stephens vs Tjen professional tennis match in the 2026 WTA Guadalajara Round Of 16 after a ball has been played, then the market resolves to Yes.",
            "rules_secondary": "Walkovers before play resolve to a fair price.", **changes}


class AIBetPicksTennisIdentityTests(unittest.TestCase):
    def setUp(self):
        mock = patch.object(sports, "load_dynamic_actionable_kalshi_series")
        mock.start()
        self.addCleanup(mock.stop)

    def test_tournament_key_and_tennis_schema_are_recognized(self):
        self.assertEqual("", picks.validate_pick(source_pick(), NOW))
        self.assertEqual(("KXWTAMATCH",), picks.supported_series(source_pick()))
        self.assertEqual(("KXATPMATCH",), picks.supported_series(source_pick(sport_key="tennis_atp_us_open")))
        self.assertEqual("tennis_wta_guadalajara_open", source_pick()["sport_key"])

    def test_wta_identity_recognizes_selected_player_on_both_listings(self):
        for market, side in ((listing(), "yes"), (listing("Sloane Stephens", "STE"), "no")):
            candidate, error = picks.exact_candidate(sports, source_pick(), market, NOW)
            self.assertFalse(error)
            self.assertEqual(side, candidate["order_side"])
            self.assertTrue(candidate["aibetpicks_settlement_review"]["execution_compatible"])
            self.assertEqual("kalshi_contract_rules", candidate["aibetpicks_settlement_review"]["basis"])

    def test_rejects_wrong_tour_period_player_date_and_doubles(self):
        wrong_markets = [
            listing(series_ticker="KXATPMATCH"), listing(series_ticker="KXWTAGAME"),
            listing(series_ticker="KXWTADOUBLES"), listing(yes_sub_title="J. Tjen"),
            listing(ticker="KXWTAMATCH-26SEP16STETJE-TJE", event_ticker="KXWTAMATCH-26SEP16STETJE"),
            listing(title="Janice Tjen wins", rules_primary="Janice Tjen vs Other Player"),
        ]
        for market in wrong_markets:
            with self.subTest(market=market):
                candidate, error = picks.exact_candidate(sports, source_pick(), market, NOW)
                self.assertIsNone(candidate)
                self.assertTrue(error)
        for change in ({"sport_key": "tennis_itf"}, {"sport_key": "tennis_wta_doubles"},
                       {"market_scope": "first_set"},
                       {"bet_type": "total", "type": "total", "line": 20.5},
                       {"direction": "home"}):
            self.assertTrue(picks.validate_pick(source_pick(**change), NOW))

    def test_order_guard_accepts_tennis_without_sportsbook_price_or_rules_gate(self):
        for changes in ({}, {"bookmaker": None, "odds": None, "settlement_rules": None}):
            candidate, _ = picks.exact_candidate(sports, source_pick(**changes), listing(), NOW)
            book = {"orderbook_fp": {"yes_dollars": [["0.68", "500"]], "no_dollars": [["0.30", "500"]]}}
            with patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=listing()), \
                    patch.object(sports, "get_json", return_value=(book, {})), \
                    patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95), \
                    patch.object(sports, "SPORTS_MAX_SPREAD_CENTS", 5):
                result = picks.order_guard(sports, candidate, now=NOW)
                self.assertTrue(result["ok"], result)
                self.assertEqual(70, result["candidate_updates"]["entry_price"])
                with patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 65):
                    self.assertEqual("live_price_above_max", picks.order_guard(sports, candidate, NOW)["error"])
                book["orderbook_fp"]["yes_dollars"][0][0] = "0.60"
                self.assertEqual("aibetpicks_quote_spread_too_wide", picks.order_guard(sports, candidate, NOW)["error"])

    def test_discovery_queries_match_series_not_individual_game(self):
        with patch.object(sports, "get_json", return_value=({"markets": [listing()]}, {})) as get:
            matches, review = picks.discover_source_candidates(sports, source_pick(), NOW)
        self.assertEqual(1, len(matches))
        self.assertIn("series_ticker=KXWTAMATCH", get.call_args.args[0])
        self.assertEqual(1, review["requested_pages"])

    def run_process(self, previous_status, *, existing=False, previous_reason="unsupported_sport_or_period"):
        pick = source_pick()
        feed = {"schema_version": 1, "date": pick["date"], "generated_at": NOW.isoformat(),
                "bots": [{"bot_id": "tennis_court", "name": "Court Edge Bot", "today": pick}]}
        decision = {"status": previous_status, "reason": previous_reason,
                    "retry_after": (NOW + timedelta(minutes=15)).isoformat(),
                    "source_signature": hashlib.sha256(json.dumps(pick, sort_keys=True).encode()).hexdigest()}
        key = "tennis_court:2026-09-15"
        state = {"last_poll": NOW.isoformat(), "last_slot": picks.polling_slot(NOW),
                 "source_policy_version": picks.VERSION, "decisions": {key: decision}}
        with TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp)/"state.json"), \
                patch.object(picks, "FEED_FILE", Path(tmp)/"feed.json"), \
                patch.object(picks, "enabled", return_value=True), \
                patch.object(sports, "effective_sports_unit_size", return_value=39.48), \
                patch.object(sports, "SPORTS_UNIT_MAX_PER_MARKET", 5), \
                patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95), \
                patch.object(sports, "SPORTS_MAX_SPREAD_CENTS", 5), \
                patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 0.85), \
                patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=listing()), \
                patch.object(sports, "get_json", return_value=({"orderbook_fp": {"yes_dollars": [["0.68", "500"]], "no_dollars": [["0.30", "500"]]}}, {})), \
                patch.object(picks, "lane_preflight", return_value=""), \
                patch.object(sports, "save_portfolio"), patch.object(sports, "upsert_live_order_intent"), \
                patch.object(sports, "append_jsonl"), \
                patch.object(sports, "load_live_order_intents", return_value={"intents": []}), \
                patch.object(sports, "place_live_kalshi_order", return_value={"ok": True, "actual_stake": 39.2, "contracts": 56}) as order:
            picks.save_json(picks.STATE_FILE, state)
            picks.save_json(picks.FEED_FILE, feed)
            portfolio = {"bets": [{"mode": "live", "status": "open", "source": "user_manual",
                                   "kalshi_ticker": listing()["ticker"], "stake": 97.41}] if existing else [], "history": []}
            picks.process(sports, portfolio, [listing()], now=NOW, allow_poll=False)
            output = picks.read_json(picks.STATE_FILE)["decisions"][key]
            if output["status"] == "filled" and previous_status != "filled":
                order.assert_called_once()
                self.assertEqual(39.48, order.call_args.args[1])
                picks.process(sports, portfolio, [listing()], now=NOW, allow_poll=False)
                order.assert_called_once()
            else:
                order.assert_not_called()
        return output

    def test_old_invalid_pick_rechecked_without_new_source_signature(self):
        row = self.run_process("invalid")
        self.assertEqual("filled", row["status"])
        self.assertEqual("KXWTAMATCH-26SEP15STETJE-TJE", row["ticker"])
        self.assertEqual("yes", row["side"])
        self.assertEqual(1, row["published_units"])
        self.assertEqual(39.48, row["planned_stake"])
        self.assertEqual(picks.PARSER_VERSION, row["parser_version"])

    def test_old_rules_review_reaches_executor_with_published_units(self):
        row = self.run_process("rules_review", previous_reason="aibetpicks_tennis_settlement_rules_differ")
        self.assertEqual("filled", row["status"])
        self.assertEqual(39.2, row["actual_stake"])

    def test_existing_manual_tjen_position_prevents_another_order(self):
        row = self.run_process("rules_review", existing=True)
        self.assertEqual("duplicate", row["status"])
        self.assertEqual(listing()["ticker"], row["ticker"])
        self.assertEqual("yes", row["side"])

    def test_old_missing_odds_rejection_is_reconsidered(self):
        self.assertEqual("filled", self.run_process("invalid", previous_reason="missing_posted_odds")["status"])

    def test_terminal_filled_duplicate_uncertain_and_expired_stay_closed(self):
        for status in ("filled", "duplicate", "ambiguous", "expired"):
            with self.subTest(status=status):
                self.assertEqual(status, self.run_process(status)["status"])


class AIBetPicksAtomicStateTests(unittest.TestCase):
    def test_transient_windows_reader_lock_retries_atomic_replace(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/"state.json"
            path.write_text('{"old": true}', encoding="utf-8")
            replace = Path.replace
            calls = []
            def locked_once(source, target):
                calls.append(target)
                self.assertEqual({"old": True}, json.loads(path.read_text()))
                if len(calls) == 1:
                    raise PermissionError("reader holds destination")
                return replace(source, target)
            with patch.object(Path, "replace", locked_once), patch.object(picks.time, "sleep"):
                picks.save_json(path, {"new": True})
            self.assertEqual({"new": True}, json.loads(path.read_text()))
            self.assertEqual(2, len(calls))
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_persistent_lock_fails_closed_and_keeps_original_state(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/"state.json"
            path.write_text('{"old": true}', encoding="utf-8")
            with patch.object(Path, "replace", side_effect=PermissionError("locked")) as replace, \
                    patch.object(picks.time, "sleep"):
                with self.assertRaises(PermissionError):
                    picks.save_json(path, {"new": True})
            self.assertEqual(5, replace.call_count)
            self.assertEqual({"old": True}, json.loads(path.read_text()))
            self.assertEqual([path], list(Path(tmp).iterdir()))


if __name__ == "__main__":
    unittest.main()
