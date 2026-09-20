import copy
from contextlib import ExitStack
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import sports_aibetpicks as picks
import sports_paper_bettor as sports

NOW = datetime(2026, 9, 14, 10, 1, tzinfo=picks.CHICAGO)


def pick(**changes):
    return {"pick_id": "pick-1", "event_id": "odds-event-1", "date": "2026-09-14",
            "sport_key": "baseball_mlb", "status": "pending", "home_team": "Minnesota Twins",
            "away_team": "Detroit Tigers", "selection": "Detroit Tigers", "direction": "away",
            "commence_time": "2026-09-14T20:10:00Z", "market_scope": "full_game",
            "settlement_rules": "two_way_including_overtime", "bet_type": "moneyline",
            "type": "moneyline", "line": None, "odds": "+110", "pick": "Detroit Tigers ML",
            "game": "Detroit Tigers @ Minnesota Twins", "stake_units": 1, **changes}


def market(kind="moneyline", **changes):
    suffix = picks.SUFFIX[kind]
    title = {"moneyline": "Detroit at Minnesota: Detroit wins?",
             "spread": "Detroit at Minnesota: Detroit wins by over 1.5 runs?",
             "total": "Detroit at Minnesota: Over 8.5 total runs?"}[kind]
    return {"ticker": f"KXMLB{suffix}-26SEP141610DETMIN-DET2", "series_ticker": "KXMLB" + suffix,
            "event_ticker": f"KXMLB{suffix}-26SEP141610DETMIN", "title": title,
            "yes_sub_title": "Detroit", "status": "active", **changes}


class AIBetPicksTests(unittest.TestCase):
    def setUp(self):
        self.dynamic = patch.object(sports, "load_dynamic_actionable_kalshi_series")
        self.dynamic.start()
        self.addCleanup(self.dynamic.stop)

    def test_moneyline_exact_team_and_side(self):
        candidate, issue = picks.exact_candidate(sports, pick(), market(), NOW)
        self.assertFalse(issue)
        self.assertEqual("yes", candidate["order_side"])
        self.assertEqual(sports.row_game_key(pick()), candidate["game_key"])
        self.assertEqual(sports.sports_strategy_identity()["build_id"], candidate["strategy_build_id"])
        self.assertEqual(sports.sports_strategy_identity()["config_hash"], candidate["strategy_config_hash"])

    def test_opposite_moneyline_maps_to_no(self):
        candidate, issue = picks.exact_candidate(sports, pick(selection="Minnesota Twins", direction="home"), market(), NOW)
        self.assertFalse(issue)
        self.assertEqual("no", candidate["order_side"])

    def test_spread_sign_and_opposite_team(self):
        p = pick(type="spread", bet_type="spread", selection="Minnesota Twins", direction="home", line=1.5)
        candidate, issue = picks.exact_candidate(sports, p, market("spread"), NOW)
        self.assertFalse(issue)
        self.assertEqual("no", candidate["order_side"])
        self.assertIsNone(picks.exact_candidate(sports, {**p, "line": -1.5}, market("spread"), NOW)[0])

    def test_over_under_and_exact_total(self):
        for direction, side in [("Over", "yes"), ("Under", "no")]:
            p = pick(type="total", bet_type="total", selection=direction, direction=direction.lower(), line=8.5)
            candidate, issue = picks.exact_candidate(sports, p, market("total"), NOW)
            self.assertFalse(issue)
            self.assertEqual(side, candidate["order_side"])
            self.assertIsNone(picks.exact_candidate(sports, {**p, "line": 9.5}, market("total"), NOW)[0])

    def test_integer_push_line_never_substituted(self):
        p = pick(type="spread", bet_type="spread", line=-2)
        self.assertEqual("push_line_not_equivalent", picks.validate_pick(p, NOW))

    def test_wrong_event_date_team_and_doubleheader_are_rejected(self):
        for changed in [market(ticker="KXMLBGAME-26SEP151610DETMIN-DET", event_ticker="KXMLBGAME-26SEP151610DETMIN"),
                        market(ticker="KXMLBGAME-26SEP142010DETMIN-DET", event_ticker="KXMLBGAME-26SEP142010DETMIN"),
                        market(title="Boston at Minnesota: Boston wins?", ticker="KXMLBGAME-26SEP141610BOSMIN-BOS", yes_sub_title="Boston")]:
            self.assertIsNone(picks.exact_candidate(sports, pick(), changed, NOW)[0])

    def test_partial_game_and_other_sport_are_rejected(self):
        for prefix in ["KXMLBINNINGTOTAL", "KXNFLCAME", "KXMLBRFI", "KXMLB1HGAME"]:
            self.assertIsNone(picks.exact_candidate(sports, pick(), market(series_ticker=prefix, ticker=prefix + "-26SEP141610DETMIN-DET"), NOW)[0])

    def test_regular_scanner_rejects_numbered_inning_totals(self):
        observed = {"ticker": "KXMLBINNINGTOTAL-26SEP141940NYYMIN-8-1",
                    "title": "8th inning: Over 0.5 runs", "yes_sub_title": "Over 0.5 runs in the 8th inning"}
        self.assertTrue(sports.is_partial_event_market(observed))
        self.assertEqual("unsupported", sports.classify_kalshi_market(observed)["type"])
        self.assertTrue(sports.is_partial_event_market({"title": "Eighth inning: Over 0.5 runs"}))
        self.assertFalse(sports.is_partial_event_market(market("total")))

    def test_contract_line_does_not_use_digits_in_team_name(self):
        terms = picks.contract_terms({"title": "Philadelphia 76ers wins by over 4.5 points", "floor_strike": 4.5, "strike_type": "greater"}, "spread")
        self.assertEqual(-4.5, terms["point"])
        self.assertEqual({}, picks.contract_terms({"title": "Over 8.5 runs", "floor_strike": 9.5}, "total"))

    def test_unstructured_stale_or_reviewed_pick_fails_closed(self):
        for changes in [{"event_id": ""}, {"direction": "home"}, {"selection": "Detroit"},
                        {"status": "win"}, {"date": "2026-09-13"}, {"manual_override": True},
                        {"result_needs_review": True}, {"market_scope": "first_half"},
                        {"commence_time": NOW.isoformat()}, {"commence_time": "2026-09-14T20:10:00"}]:
            with self.subTest(changes=changes):
                self.assertTrue(picks.validate_pick(pick(**changes), NOW))

    def test_feed_current_date_schema_clock_and_duplicate_bots(self):
        valid = {"schema_version": 1, "generated_at": NOW.isoformat(), "date": "2026-09-14", "bots": [{"bot_id": "a"}]}
        self.assertIs(valid, picks.validate_feed(valid, NOW))
        for changes in [{"schema_version": 2}, {"date": "2026-09-13"},
                        {"generated_at": (NOW - timedelta(minutes=6)).isoformat()},
                        {"bots": [{"bot_id": "a"}, {"bot_id": "a"}]}]:
            with self.assertRaises(ValueError):
                picks.validate_feed({**valid, **changes}, NOW)

    def test_hourly_window_and_central_dst(self):
        for month, utc_hour in [(1, 16), (7, 15)]:
            local = datetime(2026, month, 14, 10, tzinfo=picks.CHICAGO)
            self.assertEqual(utc_hour, local.astimezone(picks.timezone.utc).hour)
            self.assertIsNotNone(picks.polling_slot(local))
        self.assertIsNone(picks.polling_slot(NOW.replace(hour=9)))
        self.assertIsNotNone(picks.polling_slot(NOW.replace(hour=17)))
        self.assertIsNone(picks.polling_slot(NOW.replace(hour=18)))
        self.assertEqual(11, picks.next_poll({"source_policy_version": picks.VERSION, "last_slot": picks.polling_slot(NOW)}, NOW).hour)

    def test_new_bot_empty_php_history_does_not_block_other_picks(self):
        existing = {"bot_id": "existing", "today": pick(), "history": {"old": pick(status="win")}}
        feed = {"schema_version": 1, "generated_at": NOW.isoformat(), "date": "2026-09-14",
                "bots": [existing, {"bot_id": "new", "today": None, "history": []}]}
        validated = picks.validate_feed(feed, NOW)
        self.assertEqual(existing, validated["bots"][0])
        self.assertEqual({}, validated["bots"][1]["history"])
        self.assertEqual(0, picks.performance(validated["bots"][1], NOW)["sample"])
        self.assertEqual("", picks.validate_pick(validated["bots"][0]["today"], NOW))

    def test_empty_history_compatibility_does_not_accept_malformed_payloads(self):
        for history, today in [([pick()], None), (None, None), ("", None), (0, None),
                               (False, None), ([], []), ([], "pending")]:
            with self.subTest(history=history, today=today), self.assertRaisesRegex(ValueError, "invalid_bot_pick_payload"):
                picks.validate_feed({"schema_version": 1, "generated_at": NOW.isoformat(),
                    "date": "2026-09-14", "bots": [{"bot_id": "bad", "history": history, "today": today}]}, NOW)

    def test_book_depth_uses_opposite_best_bid_not_trade_volume(self):
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "100"]], "no_dollars": [["0.50", "900"], ["0.59", "25"]]}}
        self.assertEqual((40, 41, 25), picks.executable_book(book, "yes"))
        self.assertEqual((59, 60, 100), picks.executable_book(book, "no"))
        book["orderbook_fp"]["no_dollars"] = []
        self.assertEqual((None, None, None), picks.executable_book(book, "yes"))

    def test_fractional_price_and_nonfinite_depth_rejected(self):
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "100"]], "no_dollars": [["0.595", "25"]]}}
        self.assertIsNone(picks.executable_book(book, "yes")[1])
        book["orderbook_fp"]["no_dollars"] = [["0.59", "NaN"]]
        self.assertIsNone(picks.executable_book(book, "yes")[1])

    def test_sizing_uses_published_units_without_local_evidence(self):
        for units in [0.5, 1, 2.5, 3, 5, "2.25"]:
            result = picks.sizing(pick(stake_units=units), 5)
            self.assertTrue(result["ok"])
            self.assertEqual(float(units), result["target_units"])
            self.assertFalse(result["local_edge_required"])
            self.assertFalse(result["local_history_required"])

    def test_source_units_missing_invalid_or_over_limit_never_default_to_one(self):
        for units in [None, False, True, "", "2U", -1, 0, float('nan'), float('inf'), 6]:
            with self.subTest(units=units):
                result = picks.sizing(pick(stake_units=units), 5)
                self.assertFalse(result["ok"])
        self.assertEqual(6, picks.sizing(pick(stake_units=6), 5)["target_units"])

    def test_winning_record_can_still_have_negative_roi(self):
        history = {}
        for i in range(10):
            history[str(i)] = pick(pick_id=str(i), odds=-300, status="win" if i < 6 else "loss",
                commence_time=(NOW - timedelta(days=i + 1)).isoformat(), result_source="verified_scores")
        stats = picks.performance({"history": history}, NOW)
        self.assertEqual(6, stats["wins"])
        self.assertLess(stats["roi_pct"], 0)

    def test_manual_and_unverified_results_do_not_boost_units(self):
        base = pick(status="win", commence_time=(NOW - timedelta(days=1)).isoformat())
        history = {"a": base, "b": {**base, "pick_id": "b", "result_source": "scores", "manual_override": True}}
        self.assertEqual(0, picks.performance({"history": history}, NOW)["sample"])

    def test_same_pick_dedupes_across_bots_and_opposite_contract_sides(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        existing = {**candidate, "mode": "live"}
        self.assertTrue(picks.existing_duplicate({"history": [existing]}, candidate))
        self.assertTrue(picks.existing_duplicate({"bets": [{**existing, "order_side": "no"}]}, candidate))
        changed = {**existing, "kalshi_ticker": "different", "ticker": "different"}
        self.assertTrue(picks.existing_duplicate({"bets": [changed]}, candidate))

    def test_normal_scanner_equivalent_opponent_no_contract_is_duplicate(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        existing = {**candidate, "mode": "live", "source": "edge_scanner", "order_side": "no",
                    "kalshi_ticker": "KXMLBGAME-26SEP141610DETMIN-MIN", "aibetpicks_fingerprint": None}
        self.assertTrue(picks.existing_duplicate({"bets": [existing]}, candidate, sports))
        existing["selected_team"] = "Minnesota Twins"
        self.assertFalse(picks.existing_duplicate({"bets": [existing]}, candidate, sports))

    def test_economic_duplicate_check_preserves_doubleheaders_and_periods(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        existing = {**candidate, "mode": "live", "aibetpicks_fingerprint": None,
                    "kalshi_ticker": "KXMLBGAME-26SEP142010DETMIN-MIN", "commence_time": "2026-09-15T00:10:00Z"}
        self.assertFalse(picks.existing_duplicate({"bets": [existing]}, candidate, sports))
        existing.update(kalshi_ticker="KXMLB1HGAME-26SEP141610DETMIN-MIN", commence_time=pick()["commence_time"])
        self.assertFalse(picks.existing_duplicate({"bets": [existing]}, candidate, sports))

    def test_malformed_feed_and_history_fail_closed_without_crashing(self):
        valid = {"schema_version": 1, "generated_at": NOW.isoformat(), "date": "2026-09-14"}
        for payload in [[], {**valid, "bots": [{"bot_id": []}]}, {**valid, "bots": [{"bot_id": 3}]}]:
            with self.assertRaises(ValueError):
                picks.validate_feed(payload, NOW)
        malformed = [pick(type=[], bet_type=[]), pick(type="total", bet_type="total", direction=None, selection="Under", line=8.5), None]
        self.assertEqual(0, picks.performance({"history": dict(enumerate(malformed))}, NOW)["sample"])

    def test_retry_after_close_rolls_to_next_morning(self):
        current = NOW.replace(hour=17, minute=59)
        due = picks.next_poll({"retry_after": (current + timedelta(minutes=5)).isoformat()}, current)
        self.assertEqual("2026-09-15T10:00:00-05:00", due.isoformat())

    def test_dashboard_distinguishes_disabled_scheduled_waiting_stale_and_healthy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp) / "state.json"), patch.object(picks, "FEED_FILE", Path(tmp) / "feed.json"):
            self.assertEqual("disabled", picks.dashboard_summary(NOW, is_enabled=False)["health"])
            self.assertIsNone(picks.dashboard_summary(NOW, is_enabled=False)["next_poll"])
            self.assertEqual("scheduled", picks.dashboard_summary(NOW.replace(hour=9), is_enabled=True)["health"])
            self.assertEqual("waiting", picks.dashboard_summary(NOW, is_enabled=True)["health"])
            picks.save_json(picks.STATE_FILE, {"last_poll": (NOW - timedelta(minutes=66)).isoformat()})
            self.assertEqual("stale", picks.dashboard_summary(NOW, is_enabled=True)["health"])
            picks.save_json(picks.STATE_FILE, {"last_poll": NOW.isoformat()})
            picks.save_json(picks.FEED_FILE, {"date": "2026-09-14"})
            self.assertEqual("healthy", picks.dashboard_summary(NOW, is_enabled=True)["health"])

    def test_recovery_updates_source_fill_and_requires_definitive_no_fill(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        candidate.update(aibetpicks_bot_id="bot", sports_units={"unit_size": 10})
        def state():
            return {"decisions": {"bot:2026-09-14": {"status": "ambiguous", "bot_id": "bot", "pick_id": "pick-1"}}}
        current = state()
        filled = {**candidate, "id": "filled-1", "mode": "live", "stake": 10}
        self.assertTrue(picks.sync_recovered_decisions(current, {"history": [filled]}, sports))
        self.assertEqual("filled", current["decisions"]["bot:2026-09-14"]["status"])
        self.assertEqual(1, current["decisions"]["bot:2026-09-14"]["actual_units"])
        for status, expected in [(None, "ambiguous"), ("ambiguous", "ambiguous"), ("portfolio_committed", "ambiguous"), ("order_not_found", "waiting"), ("not_filled", "waiting"), ("rejected_no_order", "waiting")]:
            current = state()
            with patch.object(sports, "load_live_order_intents", return_value={"intents": [{"candidate": candidate, "status": status}] if status else []}):
                picks.sync_recovered_decisions(current, {}, sports)
            self.assertEqual(expected, current["decisions"]["bot:2026-09-14"]["status"])

    def test_unidentified_source_decision_cannot_match_unrelated_intent(self):
        state = {"decisions": {"bot:2026-09-14": {"status": "ambiguous"}}}
        with patch.object(sports, "load_live_order_intents", return_value={"intents": [{"status": "not_filled"}]}):
            self.assertFalse(picks.sync_recovered_decisions(state, {}, sports))

    def test_pending_owned_lane_exposure_counts_toward_ten_units(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        portfolio = {"bets": [{"mode": "live", "status": "pending", "strategy_owner": "aibetpicks", "stake": 95}]}
        with patch.object(sports, "EXECUTION_MODE", "live"), patch.object(sports, "live_trading_ready", return_value=True), patch.object(sports, "SPORTS_LIVE_EDGE_ORDER_ENABLED", True), patch.object(sports, "SPORTS_LIVE_CAMPAIGN_MAX_OPEN", 7), patch.object(sports, "effective_sports_unit_size", return_value=10):
            self.assertEqual("aibetpicks_open_10u_cap", picks.lane_preflight(sports, portfolio, candidate, 10))

    def test_stale_odds_cannot_support_increased_sizing(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        candidate.update(aibetpicks_fair_probability=0.60, aibetpicks_evidence_valid_until=(NOW - timedelta(seconds=1)).isoformat())
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "500"]], "no_dollars": [["0.58", "500"]]}}
        with patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=market()), patch.object(sports, "get_json", return_value=(book, {})), patch.object(sports, "SPORTS_MAX_SPREAD_CENTS", 5):
            result = picks.order_guard(sports, candidate, NOW)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["candidate_updates"]["aibetpicks_fair_probability"])
            self.assertIsNone(result["candidate_updates"]["edge"])
            candidate["aibetpicks_evidence_valid_until"] = (NOW + timedelta(seconds=1)).isoformat()
            self.assertGreater(picks.order_guard(sports, candidate, NOW)["candidate_updates"]["edge"], 0)

    def test_equivalent_contract_selection_uses_price_and_sufficient_depth(self):
        matches = [{"kalshi_ticker": key} for key in ("empty", "cheap_thin", "expensive", "best")]
        quotes = {
            "empty": {"ok": False, "error": "aibetpicks_quote_unavailable"},
            "cheap_thin": {"ok": True, "candidate_updates": {"entry_price": 40, "executable_contracts_at_ask": 2}},
            "expensive": {"ok": True, "candidate_updates": {"entry_price": 48, "executable_contracts_at_ask": 100}},
            "best": {"ok": True, "candidate_updates": {"entry_price": 45, "executable_contracts_at_ask": 100}},
        }
        with patch.object(picks, "order_guard", side_effect=lambda engine, row, now: quotes[row["kalshi_ticker"]]), patch.object(sports, "effective_sports_unit_size", return_value=10), patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95), patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 0.85):
            selected, reason = picks.select_executable_candidate(sports, matches, {}, NOW)
        self.assertFalse(reason)
        self.assertEqual("best", selected["kalshi_ticker"])

    def test_equivalent_contract_selection_cannot_ignore_global_price_limit(self):
        quote = {"ok": True, "candidate_updates": {"entry_price": 96, "executable_contracts_at_ask": 100}}
        with patch.object(picks, "order_guard", return_value=quote), patch.object(sports, "effective_sports_unit_size", return_value=10), patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95):
            candidate, reason = picks.select_executable_candidate(sports, [{"kalshi_ticker": "too-expensive"}], {}, NOW)
        self.assertIsNone(candidate)
        self.assertEqual("live_price_above_max", reason)

    def test_record_fill_retains_fee_when_exchange_omits_fee_fields(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        candidate.update(aibetpicks_bot_id="bot", sports_units={"unit_size": 10})
        order = {"actual_stake": 10, "contracts": 20, "candidate_updates": {"entry_price": 50, "exact_order_fee": 0.35}}
        portfolio = {"balance": 100}
        with patch.object(sports, "save_portfolio"), patch.object(sports, "append_jsonl"):
            bet = picks.record_fill(sports, portfolio, candidate, order)
        self.assertEqual(0.35, bet["fee"])
        self.assertEqual(9.65, sports.sports_settlement_financials(bet, 20, 10)["profit"])
        self.assertEqual(90, portfolio["balance"])

    def test_intent_recovery_preserves_units_fees_and_source_without_resubmission(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        candidate.update(aibetpicks_bot_id="bot", sports_units={"unit_size": 10}, entry_price=50)
        response = {"fill_count_fp": "10", "taker_fill_cost_dollars": "5.00", "yes_price_dollars": "0.50"}
        intent = {"client_order_id": "recover-1", "status": "filled_pending_portfolio", "candidate": candidate,
                  "order_side": "yes", "response": response}
        with patch.object(sports, "load_live_order_intents", return_value={"intents": [intent]}), patch.object(sports, "save_portfolio"), patch.object(sports, "upsert_live_order_intent"), patch.object(sports, "log_line"), patch.object(sports, "append_jsonl"), patch.object(sports, "kalshi_private_request") as request:
            portfolio = {"balance": 100}
            bet = sports.recover_pending_live_order_intents(portfolio)[0]
        self.assertEqual("aibetpicks", bet["source"])
        self.assertEqual(0.5, bet["unit_count"])
        self.assertEqual(0.18, bet["fee"])
        self.assertTrue(bet["id"])
        request.assert_not_called()

    def test_feed_http_redirect_is_never_followed(self):
        response = Mock(status_code=302)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("secure_settings.read_secure_settings", return_value={"token": "test-token"}), patch.object(picks.requests, "get", return_value=response) as get:
            with self.assertRaisesRegex(ValueError, "feed_http_302"):
                picks.fetch_feed(NOW)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_order_guard_rechecks_identity_and_depth(self):
        candidate, _ = picks.exact_candidate(sports, pick(), market(), NOW)
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "500"]], "no_dollars": [["0.58", "500"]]}}
        with patch.object(sports, "fetch_kalshi_market_by_ticker", return_value=market()), patch.object(sports, "get_json", return_value=(book, {})), patch.object(sports, "SPORTS_MAX_SPREAD_CENTS", 5):
            result = picks.order_guard(sports, candidate, NOW)
            self.assertTrue(result["ok"], result)
            self.assertEqual(500, result["candidate_updates"]["executable_contracts_at_ask"])
            candidate["order_side"] = "no"
            self.assertFalse(picks.order_guard(sports, candidate, NOW)["ok"])

    def test_new_lane_is_fill_or_kill_and_has_independent_source(self):
        candidate = {"aibetpicks": {"active": True}}
        self.assertEqual("fill_or_kill", sports.sports_live_order_time_in_force(candidate))
        self.assertEqual("aibetpicks", sports.strategy_owner_for_candidate(candidate))
        from sports_audit_support import strategy_lane
        self.assertEqual("aibetpicks", strategy_lane({"source": "aibetpicks"}))

    def test_poll_failure_blocks_cached_execution_and_retries_in_five_minutes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp) / "state.json"), patch.object(picks, "FEED_FILE", Path(tmp) / "feed.json"), patch.object(picks, "fetch_feed", side_effect=ValueError("feed_http_503")) as fetch, patch.object(picks, "enabled", return_value=True), patch.object(sports, "place_live_kalshi_order") as order:
            result = picks.process(sports, {}, [], now=NOW)
            self.assertEqual("feed_http_503", result["error"])
            picks.process(sports, {}, [], now=NOW + timedelta(minutes=1))
            self.assertEqual(1, fetch.call_count)
            picks.process(sports, {}, [], now=NOW + timedelta(minutes=5))
            self.assertEqual(2, fetch.call_count)
            order.assert_not_called()

    def test_retry_recovers_empty_history_feed_without_reopening_filled_pick(self):
        feed = {"schema_version": 1, "generated_at": NOW.isoformat(), "date": "2026-09-14",
                "bots": [{"bot_id": "filled", "today": pick(), "history": {}},
                         {"bot_id": "new", "today": None, "history": []}]}
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(feed).encode()]
        original = {"status": "filled", "bet_id": "existing-fill", "actual_stake": 10}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(picks, "STATE_FILE", Path(tmp) / "state.json"))
            stack.enter_context(patch.object(picks, "FEED_FILE", Path(tmp) / "feed.json"))
            stack.enter_context(patch.object(picks, "enabled", return_value=True))
            stack.enter_context(patch("secure_settings.read_secure_settings", return_value={"token": "test-token"}))
            stack.enter_context(patch.object(picks.requests, "get", return_value=response))
            order = stack.enter_context(patch.object(sports, "place_live_kalshi_order"))
            picks.save_json(picks.STATE_FILE, {
                "decisions": {"filled:2026-09-14": original}, "error": "invalid_bot_pick_payload",
                "retry_after": NOW.isoformat(), "last_slot": picks.polling_slot(NOW - timedelta(hours=1)),
            })
            result = picks.process(sports, {}, [], now=NOW)
            state = picks.read_json(picks.STATE_FILE)
            self.assertEqual(original, state["decisions"]["filled:2026-09-14"])
            self.assertIsNone(state["error"])
            self.assertEqual({}, picks.read_json(picks.FEED_FILE)["bots"][1]["history"])
        self.assertEqual("healthy", result["health"])
        self.assertEqual(2, len(result["bots"]))
        order.assert_not_called()

    def test_import_to_fill_records_one_unit_and_deduplicates_second_bot(self):
        feed = {"date": "2026-09-14", "bots": [{"bot_id": bot, "today": pick()} for bot in ("first", "second")]}
        quote = {"ok": True, "candidate_updates": {"entry_price": 40, "estimated_fee_edge_pp": 1.68, "executable_contracts_at_ask": 100}}
        fill = {"ok": True, "actual_stake": 10, "contracts": 25, "response": {"taker_fees_dollars": "0.42"}, "request": {"client_order_id": "test-import"}}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for obj, name, value in [
                (picks, "STATE_FILE", Path(tmp) / "state.json"), (picks, "FEED_FILE", Path(tmp) / "feed.json"),
                (sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95), (sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 0.85),
                (sports, "SPORTS_UNIT_MAX_PER_MARKET", 5),
            ]:
                stack.enter_context(patch.object(obj, name, value))
            for obj, name, value in [
                (picks, "enabled", True), (picks, "fetch_feed", feed), (picks, "order_guard", quote),
                (picks, "current_book_evidence", (None, 0, "exact_line_books_unavailable")), (picks, "lane_preflight", ""),
                (sports, "effective_sports_unit_size", 10), (sports, "save_portfolio", None),
                (sports, "upsert_live_order_intent", None), (sports, "append_jsonl", None),
            ]:
                stack.enter_context(patch.object(obj, name, return_value=value))
            order = stack.enter_context(patch.object(sports, "place_live_kalshi_order", return_value=fill))
            portfolio = {"balance": 100}
            result = picks.process(sports, portfolio, [market()], now=NOW)
            decisions = picks.read_json(picks.STATE_FILE)["decisions"]
        order.assert_called_once()
        self.assertEqual(1, result["placed"][0]["unit_count"])
        self.assertEqual(0.42, result["placed"][0]["fee"])
        self.assertEqual("filled", decisions["first:2026-09-14"]["status"])
        self.assertEqual("duplicate", decisions["second:2026-09-14"]["status"])

    def test_import_published_fractional_multiple_units_without_odds_api(self):
        feed = {"date": "2026-09-14", "bots": [{"bot_id": "source", "today": pick(stake_units=2.5), "history": {}}]}
        quote = {"ok": True, "candidate_updates": {"entry_price": 40, "estimated_fee_edge_pp": 1.68, "executable_contracts_at_ask": 100}}
        fill = {"ok": True, "actual_stake": 24.8, "contracts": 62, "response": {"taker_fees_dollars": "0.42"}, "request": {"client_order_id": "test-source"}}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for obj, name, value in [(picks, "STATE_FILE", Path(tmp)/"state.json"), (picks, "FEED_FILE", Path(tmp)/"feed.json"),
                                     (sports, "SPORTS_UNIT_MAX_PER_MARKET", 5), (sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95),
                                     (sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 0.85)]:
                stack.enter_context(patch.object(obj, name, value))
            for obj, name, value in [(picks, "enabled", True), (picks, "fetch_feed", feed), (picks, "order_guard", quote),
                                     (picks, "lane_preflight", ""), (sports, "effective_sports_unit_size", 10),
                                     (sports, "save_portfolio", None), (sports, "upsert_live_order_intent", None),
                                     (sports, "append_jsonl", None)]:
                stack.enter_context(patch.object(obj, name, return_value=value))
            evidence = stack.enter_context(patch.object(picks, "current_book_evidence", side_effect=AssertionError("Source sizing must not call Odds API")))
            order = stack.enter_context(patch.object(sports, "place_live_kalshi_order", return_value=fill))
            result = picks.process(sports, {"balance": 100}, [market()], now=NOW)
            decision = picks.read_json(picks.STATE_FILE)["decisions"]["source:2026-09-14"]
        evidence.assert_not_called()
        self.assertEqual(25, order.call_args.args[1])
        self.assertEqual(2.5, decision["published_units"])
        self.assertEqual(2.48, result["placed"][0]["unit_count"])
        self.assertEqual("published_stake_units", decision["sizing"]["basis"])

    def test_policy_upgrade_refreshes_feed_and_never_tops_up_filled_pick(self):
        feed = {"date": "2026-09-14", "bots": [{"bot_id": "bot", "today": pick(stake_units=3)}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp)/"state.json"), patch.object(picks, "FEED_FILE", Path(tmp)/"feed.json"), patch.object(picks, "enabled", return_value=True), patch.object(picks, "fetch_feed", return_value=feed) as fetch, patch.object(sports, "place_live_kalshi_order") as order:
            picks.save_json(picks.STATE_FILE, {"last_slot": picks.polling_slot(NOW), "last_poll": NOW.isoformat(),
                "decisions": {"bot:2026-09-14": {"status": "filled", "units": 1}}})
            picks.process(sports, {}, [], now=NOW)
            picks.process(sports, {}, [], now=NOW)
            state = picks.read_json(picks.STATE_FILE)
        fetch.assert_called_once()
        order.assert_not_called()
        self.assertEqual(1, state["decisions"]["bot:2026-09-14"]["units"])
        self.assertEqual(picks.VERSION, state["source_policy_version"])

    def test_missing_source_units_cannot_submit_or_guess_a_stake(self):
        feed = {"date": "2026-09-14", "bots": [{"bot_id": "bot", "today": pick(stake_units=None)}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp)/"state.json"), patch.object(picks, "FEED_FILE", Path(tmp)/"feed.json"), patch.object(picks, "enabled", return_value=True), patch.object(picks, "fetch_feed", return_value=feed), patch.object(sports, "place_live_kalshi_order") as order:
            picks.process(sports, {}, [market()], now=NOW)
            decision = picks.read_json(picks.STATE_FILE)["decisions"]["bot:2026-09-14"]
        order.assert_not_called()
        self.assertIsNone(decision["units"])
        self.assertEqual("aibetpicks_source_units_missing_or_invalid", decision["reason"])

    def test_source_lookup_finds_exact_line_omitted_from_broad_scan(self):
        p = pick(sport_key="americanfootball_nfl", home_team="Kansas City Chiefs", away_team="Denver Broncos",
                 selection="Denver Broncos", direction="away", type="spread", bet_type="spread", line=2.5)
        m = {"ticker": "KXNFLSPREAD-26SEP14DENKC-KC3", "series_ticker": "KXNFLSPREAD", "status": "active",
             "title": "Kansas City wins by over 2.5 points?", "yes_sub_title": "Kansas City wins by over 2.5 points",
             "strike_type": "greater", "floor_strike": 2.5,
             "rules_primary": "If Kansas City wins by more than 2.5 points in the Denver vs Kansas City Pro Football game originally scheduled for Sep 14, 2026, then the market resolves to Yes."}
        with patch.object(sports, "get_json", side_effect=[({"markets": [], "cursor": "next-page"}, {}), ({"markets": [m]}, {})]) as get:
            rows, diagnostics = picks.discover_source_candidates(sports, p, NOW)
        self.assertEqual(2, diagnostics["requested_pages"])
        self.assertEqual("no", rows[0]["order_side"])
        self.assertEqual(2.5, rows[0]["market_line"])
        self.assertIn("series_ticker=KXNFLSPREAD", get.call_args.args[0])
        self.assertIn("cursor=next-page", get.call_args.args[0])

    def test_source_lookup_is_bounded_and_cannot_substitute_a_line(self):
        p = pick(type="spread", bet_type="spread", line=-2.5)
        wrong = market("spread")  # -1.5 is not the published -2.5.
        with patch.object(sports, "get_json", side_effect=[({"markets": [wrong], "cursor": str(i)}, {}) for i in range(3)]) as get:
            rows, diagnostics = picks.discover_source_candidates(sports, p, NOW)
        self.assertEqual([], rows)
        self.assertEqual(3, get.call_count)
        self.assertTrue(diagnostics["truncated"])

    def test_duplicate_and_crash_pending_source_decisions_never_submit(self):
        for status in ["filled", "ambiguous", "duplicate"]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp) / "state.json"), patch.object(picks, "FEED_FILE", Path(tmp) / "feed.json"), patch.object(picks, "enabled", return_value=True), patch.object(sports, "place_live_kalshi_order") as order:
                picks.save_json(picks.STATE_FILE, {"source_policy_version": picks.VERSION, "last_slot": picks.polling_slot(NOW), "last_poll": NOW.isoformat(), "decisions": {"bot:2026-09-14": {"status": status}}})
                picks.save_json(picks.FEED_FILE, {"date": "2026-09-14", "bots": [{"bot_id": "bot", "today": pick()}]})
                picks.process(sports, {}, [market()], now=NOW)
                order.assert_not_called()

    def test_corrected_source_pick_can_replace_invalid_unsubmitted_record(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(picks, "STATE_FILE", Path(tmp) / "state.json"), patch.object(picks, "FEED_FILE", Path(tmp) / "feed.json"), patch.object(picks, "enabled", return_value=True), patch.object(sports, "place_live_kalshi_order") as order:
            picks.save_json(picks.STATE_FILE, {"source_policy_version": picks.VERSION, "last_slot": picks.polling_slot(NOW), "last_poll": NOW.isoformat(),
                "decisions": {"bot:2026-09-14": {"status": "invalid", "source_signature": "old", "reason": "missing_structured_identity"}}})
            picks.save_json(picks.FEED_FILE, {"date": "2026-09-14", "bots": [{"bot_id": "bot", "today": pick()}]})
            picks.process(sports, {}, [], now=NOW)
            decision = picks.read_json(picks.STATE_FILE)["decisions"]["bot:2026-09-14"]
            self.assertEqual("waiting", decision["status"])
            self.assertEqual("exact_contract_unavailable", decision["reason"])
            order.assert_not_called()

    def test_source_moneyline_uses_kalshi_settlement_including_nfl_ties(self):
        self.assertEqual("", picks.validate_pick(pick(sport_key="americanfootball_nfl"), NOW))

    def test_source_odds_are_optional_but_required_for_website_performance(self):
        for odds in (None, "", "nan", -175):
            source = pick(odds=odds, bookmaker=None, settlement_rules=None)
            self.assertEqual("", picks.validate_pick(source, NOW))
            self.assertIsNotNone(picks.exact_candidate(sports, source, market(), NOW)[0])
        self.assertEqual("missing_posted_odds", picks.validate_pick(pick(odds=None), NOW, history=True))

    def test_date_only_football_total_matches_exact_teams_and_line(self):
        p = pick(sport_key="americanfootball_nfl", home_team="Arizona Cardinals", away_team="Seattle Seahawks",
                 selection="Under", direction="under", type="total", bet_type="total", line=41.5)
        m = {"ticker": "KXNFLTOTAL-26SEP14SEAARI-42", "series_ticker": "KXNFLTOTAL", "status": "active",
             "title": "Full Game: over 41.5 points scored?", "yes_sub_title": "Over 41.5 points scored",
             "rules_primary": "If Seattle and Arizona collectively score more than 41.5 points in the Seattle vs Arizona Pro Football game originally scheduled for Sep 14, 2026, then the market resolves to Yes."}
        candidate, issue = picks.exact_candidate(sports, p, m, NOW)
        self.assertFalse(issue)
        self.assertEqual("no", candidate["order_side"])

    def test_book_consensus_collapses_correlated_families(self):
        game = {**pick(), "id": "odds-event-1", "bookmakers": []}
        for key in ["fanatics", "pointsbetus", "draftkings"]:
            game["bookmakers"].append({"key": key, "last_update": NOW.isoformat(), "markets": [{"key": "h2h", "outcomes": [
                {"name": "Detroit Tigers", "price": 110}, {"name": "Minnesota Twins", "price": -120}]}]})
        with patch.object(sports, "can_spend_odds_credits", return_value=(True, {}, 0)), patch.object(sports, "odds_api_get_json", return_value=(game, {"x-requests-last": "3"})), patch.object(sports, "record_odds_spend") as record:
            fair, families, status = picks.current_book_evidence(sports, pick(), NOW)
        self.assertEqual(2, families)
        self.assertGreater(fair, 0.45)
        self.assertEqual("fresh_exact_line_consensus", status)
        self.assertEqual("aibetpicks_exact_line_evidence", record.call_args.kwargs["purpose"])

    def run_executor(self, depth=1000, fill_count=None, ceiling=60, preflight="", units=1, edge=None, max_stake=None):
        fill_count = int(units * 20) if fill_count is None else fill_count
        requested_stake = units * 10
        candidate = {"aibetpicks": {"active": True, "pick": pick(stake_units=units)}, "kalshi_ticker": "TEST-MARKET", "order_side": "yes",
                     "entry_price": 50, "edge": edge, "market_type": "moneyline", "aibetpicks_price_ceiling": ceiling,
                     "sports_units": {"unit_size": 10, "target_units": units, "performance": {}, "book_families": 0}}
        response = {"order": {"fill_count_fp": str(fill_count), "taker_fill_cost_dollars": str(fill_count / 2),
                               "taker_fees_dollars": "0.36", "yes_price_dollars": "0.5000"}}
        with ExitStack() as stack:
            patches = {
                "live_order_start_guard": {"ok": True},
                "live_order_pricing_guard": {"ok": True, "candidate_updates": {"entry_price": 50, "orderbook_depth_valid": True, "executable_contracts_at_ask": depth}},
                "effective_live_max_stake": requested_stake if max_stake is None else max_stake,
                "reserve_live_order": {"ok": True, "approved_stake": requested_stake, "reservation_id": "test-reservation"},
                "kalshi_private_request": (response, {}),
                "finalize_reservation": None, "upsert_live_order_intent": None,
            }
            mocked = {name: stack.enter_context(patch.object(sports, name, return_value=value)) for name, value in patches.items()}
            stack.enter_context(patch.object(sports, "SPORTS_FOK_REQUIRE_TOP_DEPTH", True))
            stack.enter_context(patch.object(sports, "SPORTS_FOK_TOP_DEPTH_UTILIZATION", 0.85))
            stack.enter_context(patch.object(sports, "SPORTS_LIVE_MAX_PRICE_CENTS", 95))
            stack.enter_context(patch.object(sports, "SPORTS_UNIT_MAX_PER_MARKET", 5))
            stack.enter_context(patch.object(picks, "lane_preflight", return_value=preflight))
            result = sports.place_live_kalshi_order(candidate, requested_stake, portfolio={})
        return result, mocked

    def test_shared_executor_submits_base_unit_without_scanner_edge_gate(self):
        result, mocked = self.run_executor()
        self.assertTrue(result["ok"], result)
        self.assertEqual(20, result["contracts"])
        mocked["reserve_live_order"].assert_called_once()
        self.assertEqual("fill_or_kill", mocked["kalshi_private_request"].call_args.kwargs["body"]["time_in_force"])
        self.assertEqual("prepared", mocked["upsert_live_order_intent"].call_args_list[0].kwargs["status"])
        self.assertEqual("filled_pending_portfolio", mocked["upsert_live_order_intent"].call_args_list[-1].kwargs["status"])

    def test_subunit_depth_and_pending_intents_do_not_submit(self):
        for kwargs, expected in [({"depth": 10}, "aibetpicks_below_source_unit_capacity"),
                                 ({"preflight": "live_order_intent_pending"}, "live_order_intent_pending")]:
            result, mocked = self.run_executor(**kwargs)
            self.assertEqual(expected, result["error"])
            mocked["kalshi_private_request"].assert_not_called()
            mocked["reserve_live_order"].assert_not_called()

    def test_legacy_posted_price_ceiling_does_not_block_source_execution(self):
        result, mocked = self.run_executor(ceiling=1)
        self.assertTrue(result["ok"], result)
        mocked["kalshi_private_request"].assert_called_once()
        self.assertEqual(10, mocked["reserve_live_order"].call_args.args[1])
        self.assertEqual(50, mocked["reserve_live_order"].call_args.kwargs["price_cents"])

    def test_published_multiple_units_survive_negative_or_missing_local_edge(self):
        for edge in [None, -15]:
            result, mocked = self.run_executor(units=3, edge=edge)
            self.assertTrue(result["ok"], result)
            self.assertEqual(60, result["contracts"])
            self.assertEqual(30, mocked["reserve_live_order"].call_args.args[1])

    def test_published_fractional_unit_is_executable(self):
        result, mocked = self.run_executor(units=0.5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(10, result["contracts"])

    def test_full_source_stake_waits_when_depth_or_cash_cap_would_trim(self):
        for kwargs in [{"depth": 40}, {"max_stake": 10}]:
            result, mocked = self.run_executor(units=3, **kwargs)
            self.assertEqual("aibetpicks_below_source_unit_capacity", result["error"])
            mocked["kalshi_private_request"].assert_not_called()
            mocked["reserve_live_order"].assert_not_called()

    def test_unexpected_partial_fok_keeps_fill_pending_for_recovery(self):
        result, mocked = self.run_executor(fill_count=10)
        self.assertFalse(result["ok"])
        self.assertEqual("campaign_incomplete_fill", result["error"])
        self.assertEqual("filled_pending_portfolio", mocked["upsert_live_order_intent"].call_args.kwargs["status"])


if __name__ == "__main__":
    unittest.main()
