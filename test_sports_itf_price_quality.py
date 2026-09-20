from copy import deepcopy
from datetime import timedelta, timezone, datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import sports_itf_price_quality as quality
import sports_itf_price_quality_worker as worker
from sports_itf_shadow import Experiment, stamp
from test_sports_itf_shadow import NOW, EVENT, pair, book, context


def quotes(favorite_bid=.68, favorite_ask=.70, count=1000):
    return {EVENT + "-ALP": book(round(1 - favorite_ask, 4), round(1 - favorite_bid, 4), count),
            EVENT + "-BET": book(favorite_bid, favorite_ask, count)}


class PriceQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = Experiment(self.root / "parent.json", unit_dollars=19.49, now=NOW).state
        self.source["last_scan_at"] = stamp(NOW)
        self.source["implementation_hash"] = "test-parent"
        self.engine = quality.PriceQuality(self.root, register=True, source=self.source, now=NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def enter(self, now=NOW, phase="pregame", q=None, ctx=None):
        ctx = ctx or {**context(now, phase), "scheduled_start": stamp(now + timedelta(hours=2)), "tournament": "Test Open"}
        q = q or quotes()
        return self.engine.observe(pair(), q, q, ctx, 1, now)

    def test_paired_entries_same_event_time_with_separate_five_dollar_budgets(self):
        entries = self.enter()
        self.assertEqual([r["strategy"] for r in entries], ["paired_dog", "paired_favorite"])
        self.assertEqual(entries[0]["placed_at"], entries[1]["placed_at"])
        self.assertNotEqual(entries[0]["selected_ticker"], entries[1]["selected_ticker"])
        for row in entries:
            self.assertLessEqual(row["cost"], 5)
            self.assertGreater(row["cost"], 4)
            self.assertGreater(row["fee"], 0)
            self.assertFalse(quality.position_accounting(row, 5)[1])
        self.assertGreater(entries[1]["entry_price"], .5)
        self.engine.persist()

    def test_no_half_pair_when_favorite_price_or_depth_fails(self):
        self.assertFalse(self.enter(q=quotes(.8, .82)))
        q = quotes()
        q[EVENT + "-ALP"]["orderbook_fp"]["yes_dollars"][0][1] = "1"
        q[EVENT + "-BET"]["orderbook_fp"]["no_dollars"][0][1] = "1"
        self.assertFalse(self.enter(q=q))
        self.assertFalse(self.engine.state["positions"])

    def test_spread_limit_and_ambiguous_pair_fail_closed(self):
        self.assertFalse(self.enter(q=quotes(.65, .70)))
        self.assertFalse(self.enter(q={EVENT + "-ALP": book(.40, .44), EVENT + "-BET": book(.40, .44)}))
        bad = pair()
        bad[1]["custom_strike"] = deepcopy(bad[0]["custom_strike"])
        self.assertFalse(self.engine.observe(bad, quotes(), quotes(), context(), 1, NOW))

    def test_stale_status_books_missing_future_start_and_finished_match_rejected(self):
        for field in ("status_at", "confirmed_at", "first_at"):
            ctx = {**context(), "scheduled_start": stamp(NOW + timedelta(hours=1))}
            ctx[field] = stamp(NOW - timedelta(seconds=31))
            self.assertFalse(self.enter(ctx=ctx))
        self.assertFalse(self.enter(ctx=context()))
        self.assertFalse(self.enter(ctx={**context(), "scheduled_start": stamp(NOW - timedelta(seconds=1))}))
        self.assertFalse(self.enter(phase="finished"))

    def test_favorite_flip_between_snapshots_rejected(self):
        ctx = {**context(), "scheduled_start": stamp(NOW + timedelta(hours=1))}
        self.assertFalse(self.engine.observe(pair(), quotes(), quotes(.30, .32), ctx, 1, NOW))
        self.assertEqual(self.engine.state["rejections"], {"ambiguous_or_changed_favorite": 1})

    def test_favorite_fill_uses_minimum_confirmed_depth_and_charges_all_fees(self):
        self.assertIsNone(quality.fill_at_asks({.7: 100}, {.7: 1}, 5, .8, 1))
        self.assertIsNone(quality.fill_at_asks({.7: 100}, {.71: 100}, 5, .8, 1))
        fill = quality.fill_at_asks({.7: 3, .71: 100}, {.7: 4, .71: 100}, 5, .8, 1)
        self.assertEqual(fill["contracts"], 6)
        self.assertEqual(fill["fills"][0]["contracts"], 3)
        self.assertEqual(fill["fee"], .1)
        self.assertAlmostEqual(fill["cost"], 4.33)
        self.assertIsNone(quality.fill_at_asks({.81: 100}, {.81: 100}, 5, .8, 1))
        self.assertIsNone(quality.fill_at_asks({.7: 100}, {.7: 100}, 5, .8, float("nan")))

    def test_restart_prevents_reentry_and_protocol_tamper(self):
        self.enter()
        self.engine.persist()
        resumed = quality.PriceQuality(self.root, now=NOW)
        self.assertFalse(resumed.observe(pair(), quotes(), quotes(), {**context(), "scheduled_start": stamp(NOW + timedelta(hours=1))}, 1, NOW))
        state = json.loads((self.root / quality.STATE).read_text())
        state["unit_dollars"] = 10
        (self.root / quality.STATE).write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            quality.PriceQuality(self.root, now=NOW)

    def test_missing_corrupt_and_report_only_state_never_reset(self):
        with self.assertRaises(ValueError):
            quality.PriceQuality(self.root, now=NOW)
        (self.root / quality.REPORT).write_text("{}")
        with self.assertRaises(ValueError):
            quality.PriceQuality(self.root, register=True, source=self.source, now=NOW)
        (self.root / quality.STATE).write_text("{")
        with self.assertRaises(ValueError):
            quality.PriceQuality(self.root, register=True, source=self.source, now=NOW)

    def test_old_events_excluded_and_stale_registration_refused(self):
        self.engine.state["excluded_events"] = [EVENT]
        self.assertFalse(self.enter())
        self.source["last_scan_at"] = stamp(NOW - timedelta(minutes=4))
        with self.assertRaises(ValueError):
            quality.PriceQuality(self.root, register=True, source=self.source, now=NOW)

    def test_compressed_ledger_recovery(self):
        self.enter()
        self.engine.persist()
        path = self.root / quality.STATE
        with gzip.open(str(path) + ".gz", "wb") as stream:
            stream.write(path.read_bytes())
        path.unlink()
        restored = quality.PriceQuality(self.root, now=NOW)
        self.assertEqual(len(restored.state["positions"]), 2)

    def test_settlement_handles_no_side_fractional_and_provisional_results(self):
        entries = self.enter()
        # Force routing through opponent NO with an executable cheaper ask.
        row = entries[1]
        row["side"] = "no"
        markets = {r["ticker"]: {"ticker": r["ticker"], "status": "settled", "settlement_value_dollars": ".5"} for r in entries}
        for m in markets.values():
            m["is_provisional"] = True
        self.assertFalse(self.engine.settle(markets, NOW))
        for m in markets.values():
            m["is_provisional"] = False
        self.assertEqual(len(self.engine.settle(markets, NOW)), 2)
        self.assertTrue(all(r["result"] == "NON_BINARY" for r in entries))
        self.assertAlmostEqual(row["profit"], row["contracts"] * .5 - row["cost"])
        self.engine.persist()

    def test_no_side_payout_and_fee_accounting(self):
        q = quotes()
        q[EVENT + "-ALP"] = book(.31, .33)
        entries = self.enter(q=q)
        favorite = entries[1]
        self.assertEqual(favorite["side"], "no")
        self.engine.settle({favorite["ticker"]: {"ticker": favorite["ticker"], "status": "finalized", "result": "no"}}, NOW)
        self.assertEqual(favorite["payout"], favorite["contracts"])
        self.assertAlmostEqual(favorite["profit"], favorite["contracts"] - favorite["cost"])
        self.assertFalse(quality.position_accounting(favorite, 5)[1])

    def test_live_momentum_requires_anchor_recent_and_both_snapshots(self):
        self.assertFalse(self.enter(phase="live", q=quotes(.60, .62)))
        self.assertFalse(self.enter(NOW + timedelta(minutes=5), "live", quotes(.64, .66)))
        entries = self.enter(NOW + timedelta(minutes=6), "live", quotes(.64, .66))
        self.assertEqual([r["strategy"] for r in entries], ["live_favorite_momentum"])
        self.assertEqual(len(entries[0]["momentum_evidence"]), 2)
        self.assertEqual(entries[0]["momentum_evidence"][0]["at"], stamp(NOW))
        self.assertFalse(self.enter(NOW + timedelta(minutes=7), "live", quotes(.64, .66)))

    def test_momentum_does_not_select_best_historical_trough(self):
        self.enter(phase="live", q=quotes(.64, .66))
        self.enter(NOW + timedelta(minutes=1), "live", quotes(.60, .62))
        self.enter(NOW + timedelta(minutes=5), "live", quotes(.65, .67))
        self.assertFalse(self.enter(NOW + timedelta(minutes=6), "live", quotes(.65, .67)))

    def test_open_risk_and_tournament_caps_include_fee_inclusive_budget(self):
        original_pair = pair()
        for i in range(6):
            event = EVENT + str(i)
            p = deepcopy(original_pair)
            for r in p:
                r["event_ticker"] = event
                r["ticker"] = r["ticker"].replace(EVENT, event)
            q = {k.replace(EVENT, event): v for k, v in quotes().items()}
            ctx = {**context(), "scheduled_start": stamp(NOW + timedelta(hours=1)), "tournament": "Test Open"}
            entries = self.engine.observe(p, q, q, ctx, 1, NOW)
            self.assertEqual(len(entries), 2 if i < 5 else 0)
        self.assertEqual(len(self.engine.state["positions"]), 10)
        # One member reaching its cap prevents both paired entries.
        self.engine.state["entry_stops"]["paired_dog"] = stamp(NOW)
        self.assertFalse(self.engine.risk_allows("paired_dog", "Other", NOW))

    def test_drawdown_stop_stays_latched_after_later_recovery(self):
        with patch.object(quality, "metrics", return_value={"drawdown_units": 50}):
            self.assertFalse(self.engine.risk_allows("paired_favorite", "Test", NOW))
        self.assertFalse(self.engine.risk_allows("paired_favorite", "Test", NOW + timedelta(hours=1)))

    def test_seven_day_window_and_post_deadline_settlement(self):
        self.enter()
        end = NOW + timedelta(days=7)
        self.assertFalse(self.enter(end))
        self.assertEqual(self.engine.status(end), "settling")
        markets = {r["ticker"]: {"ticker": r["ticker"], "status": "settled", "result": "no"} for r in self.engine.state["positions"].values()}
        self.engine.settle(markets, end)
        self.assertEqual(self.engine.status(end), "complete")
        # Registration crossing a DST boundary remains 168 hours.
        at = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
        self.source["last_scan_at"] = stamp(at)
        other = quality.PriceQuality(self.root, register=True, source=self.source, now=at)
        other.validate()

    def test_mismatched_pair_or_accounting_corruption_refuses_persistence(self):
        self.enter()
        self.engine.state["positions"]["paired_dog|" + EVENT]["cost"] += .01
        with self.assertRaises(ValueError):
            self.engine.persist()
        del self.engine.state["positions"]["paired_dog|" + EVENT]
        with self.assertRaises(ValueError):
            self.engine.persist()

    def test_summary_only_counts_new_trades_and_marks_unproven(self):
        self.enter()
        summary = self.engine.summary(NOW)
        self.assertEqual(summary["bankroll_dollars"], 5000)
        self.assertEqual(summary["unit_dollars"], 5)
        self.assertEqual(sum(r["settled"] for r in summary["strategies"]), 0)
        self.assertTrue(all(r["evidence"] == "Insufficient new evidence" for r in summary["strategies"]))
        self.assertEqual(summary["paired_comparison"]["completed_matches"], 0)
        self.assertEqual(summary["paired_comparison"]["pending_matches"], 1)

    def test_worker_runs_complete_two_snapshot_scan_with_verified_match_identity(self):
        api = Mock(calls=0)
        api.markets.return_value = pair()
        milestone = {"id": "test-id", "start_date": stamp(NOW + timedelta(hours=1)), "status": "SCH",
                     "details": {"tour": "ITF", "first_competitor_id": "ALP", "second_competitor_id": "BET", "tournament_name": "Test Open"},
                     "related_event_tickers": [EVENT]}
        api.milestones.return_value = [milestone]
        api.live.return_value = {"test-id": {"details": {"status": "not_started", "competitor1_id": "ALP", "competitor2_id": "BET"}}}
        api.books.side_effect = [(quotes(), {t: stamp(NOW - timedelta(seconds=2)) for t in quotes()}),
                                 (quotes(), {t: stamp(NOW - timedelta(seconds=1)) for t in quotes()})]
        with patch.object(worker, "now_central", return_value=NOW), patch.object(worker.time, "sleep"):
            coverage = worker.scan(api, self.engine, self.root, {"KXITFMATCH": 1})
        self.assertEqual(coverage["new_entries"], 2)
        self.assertEqual(api.books.call_count, 2)
        self.assertEqual(quality.PriceQuality(self.root, now=NOW).summary(NOW)["position_count"], 2)

    def test_pair_comparison_waits_for_both_official_settlements(self):
        entries = self.enter()
        dog, favorite = entries
        self.engine.settle({dog["ticker"]: {"ticker": dog["ticker"], "status": "settled", "result": "no"}}, NOW)
        self.assertEqual(self.engine.summary(NOW)["paired_comparison"]["completed_matches"], 0)
        self.engine.settle({favorite["ticker"]: {"ticker": favorite["ticker"], "status": "settled", "result": "yes"}}, NOW)
        comparison = self.engine.summary(NOW)["paired_comparison"]
        self.assertEqual(comparison["completed_matches"], 1)
        self.assertAlmostEqual(comparison["favorite_minus_dog_profit_units"], (favorite["profit"] - dog["profit"]) / 5)

    def test_worker_settles_missing_closed_markets_without_creating_new_entries(self):
        self.enter()
        api = Mock(calls=0)
        api.markets.return_value = []
        api.get.side_effect = lambda path: {"market": {"ticker": path.split("/")[-1], "status": "settled", "result": "yes"}}
        with patch.object(worker, "now_central", return_value=NOW + timedelta(days=7)):
            coverage = worker.scan(api, self.engine, self.root, {})
        self.assertEqual(coverage["new_entries"], 0)
        self.assertEqual(coverage["new_settlements"], 2)
        api.markets.assert_not_called()
        self.assertTrue(list((self.root / "archives" / "sports_itf_price_quality").glob("*.jsonl.gz")))

    def test_failed_or_mismatched_settlement_leaves_position_open(self):
        self.enter()
        api = Mock(calls=0)
        api.markets.return_value = []
        api.get.return_value = {"market": {"ticker": "WRONG", "status": "settled", "result": "yes"}}
        with patch.object(worker, "now_central", return_value=NOW):
            coverage = worker.scan(api, self.engine, self.root, {})
        self.assertEqual(coverage["new_settlements"], 0)
        self.assertTrue(coverage["settlement_errors"])

    def test_dashboard_fresh_stale_and_error_health(self):
        self.assertEqual(quality.dashboard_summary(self.root, NOW)["health"], "not_started")
        self.engine.finish_scan({}, NOW)
        report = self.engine.summary(NOW)
        quality.atomic_json(self.root / quality.REPORT, report)
        self.assertEqual(quality.dashboard_summary(self.root, NOW)["health"], "healthy")
        self.assertEqual(quality.dashboard_summary(self.root, NOW + timedelta(minutes=4))["health"], "stale")
        report["worker"] = {"error": "Unavailable"}
        quality.atomic_json(self.root / quality.REPORT, report)
        self.assertEqual(quality.dashboard_summary(self.root, NOW)["health"], "error")


if __name__ == "__main__":
    unittest.main()
