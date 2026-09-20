from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import sports_itf_shadow as itf
import sports_itf_shadow_worker as worker

NOW = datetime(2026, 9, 14, 18, tzinfo=timezone.utc)
EVENT = "KXITFMATCH-26SEP14ALPBET"


def pair():
    return [{"ticker": EVENT + "-" + short, "event_ticker": EVENT, "market_type": "binary",
             "status": "active", "notional_value_dollars": "1", "yes_sub_title": name,
             "no_sub_title": name, "custom_strike": {"tennis_competitor": short},
             "rules_primary": "If " + name + " wins the Alpha vs Beta professional tennis match after a ball has been played, then the market resolves to Yes."}
            for short, name in [("ALP", "Alpha"), ("BET", "Beta")]]


def book(bid, ask, count=1000):
    return {"orderbook_fp": {"yes_dollars": [[str(bid), str(count)]],
                             "no_dollars": [[str(round(1 - ask, 4)), str(count)]]}}


def books(bid=.25, ask=.30, count=1000):
    return {EVENT + "-ALP": book(bid, ask, count),
            EVENT + "-BET": book(round(1 - ask - .02, 4), round(1 - bid + .02, 4), count)}


def context(now=NOW, phase="pregame"):
    return {"phase": phase, "first_at": itf.stamp(now - timedelta(seconds=2)),
            "confirmed_at": itf.stamp(now - timedelta(seconds=1)), "status_at": itf.stamp(now)}


class ITFShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = itf.Experiment(self.root / "ledger.json", unit_dollars=20, now=NOW)

    def tearDown(self):
        self.temp.cleanup()

    def enter(self, quotes=None, now=NOW, phase="pregame"):
        quotes = quotes or books()
        return self.engine.observe(pair(), quotes, quotes, context(now, phase), 1, now)

    def test_baseline_and_mid_price_enter_one_unit_with_fees(self):
        entries = self.enter()
        self.assertEqual({e["strategy"] for e in entries}, {"all_underdogs", "mid_price"})
        for row in entries:
            self.assertLessEqual(row["cost"], 20)
            self.assertGreater(row["cost"], 19.65)
            self.assertGreater(row["fee"], 0)
            self.assertEqual(row["contracts"], 63)
            self.assertAlmostEqual(row["cost"], row["premium"] + row["fee"])

    def test_baseline_accepts_wide_spreads_without_quality_filter(self):
        entries = self.enter(books(.01, .30))
        self.assertEqual([r["strategy"] for r in entries], ["all_underdogs"])

    def test_live_longshot_is_included(self):
        entries = self.enter(books(.12, .15), phase="live")
        self.assertEqual({r["strategy"] for r in entries}, {"all_underdogs", "longshots"})
        self.assertTrue(all(r["phase"] == "live" for r in entries))

    def test_every_series_including_doubles_is_discovered(self):
        for series in itf.SERIES:
            rows = pair()
            for row in rows:
                row["ticker"] = row["ticker"].replace("KXITFMATCH", series)
                row["event_ticker"] = row["event_ticker"].replace("KXITFMATCH", series)
            self.assertEqual(len(itf.pair_markets(rows)), 1)

    def test_outside_itf_and_ambiguous_competitors_are_excluded(self):
        rows = pair()
        rows[1]["custom_strike"] = deepcopy(rows[0]["custom_strike"])
        self.assertEqual(itf.pair_markets(rows), {})
        rows = pair()
        for row in rows:
            row["ticker"] = row["ticker"].replace("KXITF", "KXATP")
            row["event_ticker"] = row["event_ticker"].replace("KXITF", "KXATP")
        self.assertEqual(itf.pair_markets(rows), {})
        self.assertEqual(itf.pair_markets(pair()[:1]), {})

    def test_cheaper_opponent_no_is_mapped_to_correct_player(self):
        q = books()
        q[EVENT + "-BET"] = book(.73, .77)
        entries = self.enter(q)
        self.assertTrue(entries)
        for row in entries:
            self.assertEqual((row["selected"], row["ticker"], row["side"]), ("Alpha", EVENT + "-BET", "no"))

    def test_never_bets_both_sides_after_a_favorite_flip_or_restart(self):
        self.enter()
        self.engine.persist()
        self.engine = itf.Experiment(self.engine.path, now=NOW + timedelta(hours=1))
        q = {EVENT + "-ALP": book(.65, .70), EVENT + "-BET": book(.25, .30)}
        self.assertEqual(self.enter(q, now=NOW + timedelta(hours=1)), [])
        self.assertEqual(len(self.engine.state["positions"]), 2)

    def test_crossed_event_both_outcomes_under_half_is_excluded(self):
        q = {EVENT + "-ALP": book(.25, .30), EVENT + "-BET": book(.30, .35)}
        self.assertEqual(self.enter(q), [])

    def test_price_flip_during_confirmation_does_not_fill(self):
        second = {EVENT + "-ALP": book(.65, .70), EVENT + "-BET": book(.25, .30)}
        self.assertEqual(self.engine.observe(pair(), books(), second, context(), 1, NOW), [])

    def test_missing_or_disappearing_depth_cannot_fill(self):
        self.assertEqual(self.enter(books(count=1)), [])
        self.assertIsNone(itf.simulated_fill({.30: 100}, {.30: 0}, 20, .4999, 1))
        self.assertIsNone(itf.simulated_fill({.30: 100}, {.31: 100}, 20, .4999, 1))

    def test_depth_walk_charges_fees_and_respects_whole_contract_budget(self):
        result = itf.simulated_fill({.29: 10, .30: 100}, {.29: 5, .30: 200}, 20, .4, 1)
        self.assertEqual(result["fills"][0]["contracts"], 5)
        self.assertLessEqual(result["cost"], 20)
        self.assertEqual(result["contracts"], sum(r["contracts"] for r in result["fills"]))
        self.assertGreater(result["fee"], 0)
        self.assertIsNone(itf.simulated_fill({.60: 1000}, {.60: 1000}, 20, .9, 1))

    def test_stale_and_future_and_unconfirmed_data_are_excluded(self):
        for c in [context(NOW - timedelta(minutes=1)), context(NOW + timedelta(minutes=1)),
                  {**context(), "confirmed_at": context()["first_at"]}]:
            self.assertEqual(self.engine.observe(pair(), books(), books(), c, 1, NOW), [])

    def test_known_finished_and_unknown_matches_do_not_enter(self):
        for p in ["finished", "unknown", "identity_mismatch"]:
            self.assertEqual(self.enter(phase=p), [])

    def test_match_phase_uses_status_not_expiration_and_rejects_winner(self):
        m = {"start_date": itf.stamp(NOW + timedelta(hours=1)), "details": {"status": "SCH"}}
        self.assertEqual(itf.phase(m, None, NOW), "pregame")
        self.assertEqual(itf.phase(m, {"details": {"status": "in_progress"}}, NOW), "live")
        self.assertEqual(itf.phase(m, {"details": {"status": "in_progress", "winner": "ALP"}}, NOW), "finished")
        m["start_date"] = itf.stamp(NOW - timedelta(hours=1))
        self.assertEqual(itf.phase(m, None, NOW), "unknown")
        self.assertEqual(itf.phase(m, {"details": {"status": "not_started"}}, NOW), "pregame")

    def test_strengthening_requires_observed_anchor_and_prior_confirmation(self):
        self.enter(books(.20, .25), now=NOW)
        self.enter(books(.24, .29), now=NOW + timedelta(minutes=4))
        entries = self.enter(books(.24, .29), now=NOW + timedelta(minutes=5))
        self.assertEqual([e["strategy"] for e in entries], ["strengthening"])
        self.assertTrue(entries[0]["momentum_evidence"])

    def test_strengthening_does_not_mix_pregame_and_live_or_cherry_pick_trough(self):
        self.enter(books(.28, .33), now=NOW)
        self.enter(books(.20, .25), now=NOW + timedelta(minutes=1))
        self.enter(books(.24, .29), now=NOW + timedelta(minutes=5))
        self.assertEqual(self.enter(books(.24, .29), now=NOW + timedelta(minutes=6)), [])
        self.assertEqual(self.enter(books(.35, .40), now=NOW + timedelta(minutes=7), phase="live"), [])

    def test_week_deadline_does_not_reset_on_reload_or_accept_new_entries(self):
        self.engine.persist()
        end = itf.parsed(self.engine.state["ends_at"])
        self.assertEqual(self.enter(now=end), [])
        loaded = itf.Experiment(self.engine.path, unit_dollars=999, now=end + timedelta(days=1))
        self.assertEqual(loaded.state["unit_dollars"], 20)
        self.assertEqual(loaded.status(end), "complete")

    def test_week_is_168_hours_across_chicago_daylight_saving_change(self):
        start = datetime(2026, 10, 30, 13, tzinfo=itf.CENTRAL)
        engine = itf.Experiment(self.root / "dst.json", unit_dollars=20, now=start)
        engine.persist()
        end = itf.parsed(engine.state["ends_at"])
        self.assertEqual(end - start.astimezone(timezone.utc), timedelta(hours=168))
        self.assertEqual(end.utcoffset(), timedelta(hours=-6))

    def test_settles_after_week_and_keeps_cancellation_separate_from_win_rate(self):
        self.enter()
        after = NOW + timedelta(days=8)
        self.assertEqual(self.engine.status(after), "settling")
        ticker = EVENT + "-ALP"
        done = self.engine.settle({ticker: {"ticker": ticker, "status": "finalized", "settlement_value_dollars": "0.5000"}}, after)
        self.assertEqual(len(done), 2)
        self.assertTrue(all(r["result"] == "NON_BINARY" and r["payout"] == r["contracts"] * .5 for r in done))
        summary = self.engine.summary(after)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["strategies"][0]["wins"], 0)
        self.assertEqual(summary["strategies"][0]["non_binary"], 1)
        self.assertIsNone(summary["strategies"][0]["win_rate"])

    def test_no_side_settlement_is_inverted_and_fees_remain_in_pnl(self):
        q = books()
        q[EVENT + "-BET"] = book(.73, .77)
        self.enter(q)
        ticker = EVENT + "-BET"
        done = self.engine.settle({ticker: {"ticker": ticker, "status": "settled", "result": "no"}}, NOW)
        self.assertTrue(done)
        self.assertTrue(all(r["result"] == "WIN" and r["profit"] == round(r["contracts"] - r["cost"], 4) for r in done))
        self.assertEqual(self.engine.settle({ticker: {"ticker": ticker, "status": "settled", "result": "no"}}, NOW), [])

    def test_unresolved_provisional_and_missing_results_never_assume_loss(self):
        for data in [{"status": "closed", "result": "no"}, {"status": "finalized", "result": "scalar"},
                     {"status": "finalized", "settlement_value_dollars": "NaN"},
                     {"status": "finalized", "result": "yes", "is_provisional": True}]:
            self.assertIsNone(itf.settlement_value(data))

    def test_corrupt_or_changed_state_is_not_reset(self):
        self.engine.persist()
        raw = self.engine.path.read_text()
        self.engine.path.write_text("bad json")
        with self.assertRaises(ValueError):
            itf.Experiment(self.engine.path, unit_dollars=20)
        data = json.loads(raw)
        data["rules_hash"] = "changed"
        self.engine.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "rules changed"):
            itf.Experiment(self.engine.path)

    def test_compressed_state_and_multiple_gzip_audit_members_are_readable(self):
        self.enter()
        self.engine.persist()
        with gzip.open(str(self.engine.path) + ".gz", "wt", encoding="utf-8") as handle:
            handle.write(self.engine.path.read_text())
        self.engine.path.unlink()
        loaded = itf.Experiment(self.engine.path)
        self.assertEqual(len(loaded.state["positions"]), 2)
        for n in range(2):
            itf.append_audit(self.root, {"n": n}, NOW)
        archive = next((self.root / "archives" / "sports_itf_shadow").glob("*.gz"))
        self.assertEqual(list(itf.iter_audit(archive)), [{"n": 0}, {"n": 1}])

    def test_dashboard_marks_stale_and_failed_worker(self):
        report = self.engine.summary(NOW)
        itf.atomic_json(self.root / "sports_itf_shadow_report.json", report)
        self.assertEqual(itf.dashboard_summary(self.root, NOW)["health"], "healthy")
        self.assertEqual(itf.dashboard_summary(self.root, NOW + timedelta(minutes=4))["health"], "stale")
        report["worker"] = {"error": "network unavailable"}
        itf.atomic_json(self.root / "sports_itf_shadow_report.json", report)
        self.assertEqual(itf.dashboard_summary(self.root, NOW)["health"], "error")

    def test_milestone_identity_is_exact(self):
        m = {"id": "test", "related_event_tickers": [EVENT], "details": {"tour": "ITF",
             "first_competitor_id": "ALP", "second_competitor_id": "BET"}}
        self.assertEqual(worker.milestone_map([m], {EVENT: pair()}), {EVENT: m})
        m["details"]["second_competitor_id"] = "wrong"
        self.assertEqual(worker.milestone_map([m], {EVENT: pair()}), {})

    @patch("sports_itf_shadow_worker.time.sleep")
    @patch("sports_itf_shadow_worker.requests.Session")
    def test_transport_is_public_get_only_and_rejects_order_endpoints(self, session, sleep):
        response = Mock(status_code=200)
        response.json.return_value = {"markets": []}
        session.return_value.get.return_value = response
        api = worker.PublicKalshi()
        for path in ["/portfolio/orders", "/portfolio/balance", "/markets/KXATPMATCH-26SEP14A-B", "https://evil.test"]:
            with self.assertRaises(ValueError):
                api.get(path)
        api.get("/markets", {"series_ticker": "KXITFMATCH", "status": "open"})
        self.assertFalse(session.return_value.trust_env)
        self.assertEqual(session.return_value.get.call_count, 1)
        self.assertFalse(session.return_value.get.call_args.kwargs["allow_redirects"])
        session.return_value.post.assert_not_called()
        session.return_value.delete.assert_not_called()

    def test_pagination_is_complete_or_explicit_failure(self):
        api = worker.PublicKalshi()
        api.get = Mock(side_effect=[{"markets": [1], "cursor": "next"}, {"markets": [2], "cursor": ""}])
        self.assertEqual(api.pages("/markets", "markets", {}), [1, 2])
        api.get = Mock(return_value={"markets": [1], "cursor": "repeat"})
        with self.assertRaisesRegex(ValueError, "repeated"):
            api.pages("/markets", "markets", {})

    def test_regular_sports_live_itf_remains_disabled(self):
        import sports_paper_bettor
        self.assertIn("tennis_itf", sports_paper_bettor.DISABLED_BETTING_SPORTS)


if __name__ == "__main__":
    unittest.main()
