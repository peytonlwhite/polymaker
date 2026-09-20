import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import shared_bankroll as shared
import sports_capper_import as capper
import sports_paper_bettor as sports
import sports_probability as probability
from sports_analytics import calibration_metrics, pricing_analytics
from sports_audit_analytics import api_value_shadow, archive_continuity, capper_funnel, reconciliation_transition, source_performance_report
from sports_audit_support import binary_outcome, book_horizon_alignment, capper_parent_exposure_review, fill_edge, observation_age_minutes, parse_timestamp


class SportsAuditRemediationTests(unittest.TestCase):
    def mlb(self):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        return {"sport_key": "baseball_mlb", "market_type": "spread", "game_started": True, "market_line": -1.5, "entry_price": 42, "edge": 3, "sports_units": {"raw_target_units": 0.5}, "skip_reasons": [], "pricing_v2": {"ok": True, "independent_outcome_probability": 52, "kalshi_market_probability": 42, "consensus": {"ok": True, "average_age_minutes": 0.3, "observations": [{"family": family, "last_update": stamp, "line_model": "exact_line"} for family in ("a", "b")]}}}

    def test_two_fresh_exact_families_are_required_individually(self):
        candidate = self.mlb()
        self.assertTrue(sports.mlb_live_spread_guard_review(candidate)["ok"])
        candidate["pricing_v2"]["consensus"]["observations"][1]["last_update"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        review = sports.mlb_live_spread_guard_review(candidate)
        self.assertFalse(review["ok"])
        self.assertEqual(1, review["exact_book_families"])

    def test_fresh_family_member_cannot_hide_stale_contributor(self):
        now = datetime.now(timezone.utc)
        row = {"last_update": now.isoformat(), "contributing_updates": [now.isoformat(), (now - timedelta(minutes=3)).isoformat()]}
        self.assertEqual(3, observation_age_minutes(row, now=now))
        self.assertIsNone(observation_age_minutes({"age_minutes": 0.1}, now=now))

    def test_failed_mandatory_refresh_never_reaches_exchange_quote(self):
        candidate = self.mlb()
        with patch.object(sports, "fetch_post_ai_event_odds", return_value=(None, {"status": "budget_exhausted"})), patch.object(sports, "post_ai_revalidation_required", return_value=True), patch.object(sports, "append_jsonl"), patch.object(sports, "sports_stream") as stream:
            review = sports.post_ai_revalidate_candidate(candidate, {})
            self.assertFalse(review["ok"])
            self.assertIn("mlb_required_revalidation_unavailable", candidate["skip_reasons"])
            guard = sports.live_order_pricing_guard(candidate)
            self.assertFalse(guard["ok"])
            self.assertEqual("mlb_required_revalidation_unavailable", guard["error"])
            stream.assert_not_called()

    def test_final_order_rechecks_elapsed_book_age(self):
        candidate = self.mlb()
        candidate["post_ai_revalidation"] = {"ok": True, "requested": True, "fetch": {"status": "fetched"}}
        for row in candidate["pricing_v2"]["consensus"]["observations"]:
            row["last_update"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with patch.object(sports, "sports_stream") as stream:
            self.assertFalse(sports.live_order_pricing_guard(candidate)["ok"])
            stream.assert_not_called()

    def test_focused_scans_preserve_unrelated_confirmation(self):
        candidate = {"event_id": "mlb", "kalshi_ticker": "MLB", "market_type": "spread", "selected_team": "Team", "pricing_v2": {"consensus": {"update_token": "one"}}}
        key = sports.candidate_key(candidate)
        state = {key: {"distinct_update_count": 2, "seen_count": 2, "missed_count": 0}}
        with patch.object(sports, "load_watchlist", side_effect=lambda: state), patch.object(sports, "save_watchlist"):
            for _ in range(8):
                sports.update_watchlist([], complete_scan=False)
            self.assertTrue(sports.has_watchlist_confirmation(candidate, state))
            for _ in range(5):
                sports.update_watchlist([], complete_scan=True)
            self.assertNotIn(key, state)

    def test_focused_report_does_not_extend_broad_discovery_deadline(self):
        now = datetime(2026, 9, 4, 22, tzinfo=timezone.utc)
        report = {"generated_at": now.isoformat(), "scan_scope": "focused_incremental", "last_full_scan": {"generated_at": (now - timedelta(minutes=11)).isoformat()}}
        with patch.object(sports, "settings_file_value", return_value="10"):
            self.assertTrue(sports.broad_discovery_review(report, now)["due"])
            report["scan_scope"] = "full"
            self.assertFalse(sports.broad_discovery_review(report, now)["due"])

    def test_future_labels_cannot_change_earlier_hierarchical_fold(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{"source": "edge_scanner", "game_key": f"g-{i}", "sport_key": "baseball_mlb", "market_type": "total", "bet_timing_bucket": "early_live", "generated_at": (start + timedelta(days=i)).isoformat(), "settled_at": (start + timedelta(days=i, hours=3)).isoformat(), "result": "WIN" if i % 2 else "LOSS", "pricing_v2": {"book_probability": 75, "kalshi_mid_probability": 25}} for i in range(80)]
        future_wins = [dict(row, result="WIN") if i >= 48 else row for i, row in enumerate(rows)]
        future_losses = [dict(row, result="LOSS") if i >= 48 else row for i, row in enumerate(rows)]
        first = probability.build_calibration_state(future_wins)
        second = probability.build_calibration_state(future_losses)
        for level, key in [("sports", "baseball mlb"), ("sport_markets", "baseball mlb|total"), ("segments", probability.segment_key("baseball_mlb", "total", "early_live"))]:
            a = first[level][key]["walk_forward_validation"]
            b = second[level][key]["walk_forward_validation"]
            self.assertEqual(a["folds"][0], b["folds"][0])
            self.assertFalse(a["promotion_enabled"])

    def test_overlapping_unsettled_training_events_are_purged(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{"game_key": str(i), "placed_at": (start + timedelta(hours=i)).isoformat(), "settled_at": (start + timedelta(days=10)).isoformat(), "result": "WIN", "pricing_v2": {"book_probability": 60, "kalshi_mid_probability": 50}} for i in range(80)]
        review = probability._walk_forward_validation(rows, default_weight=.5, prior_strength=25, recency_half_life_days=60)
        self.assertFalse(review["validated"])
        self.assertEqual([], review["folds"])

    def test_scalar_outcome_and_integrity_do_not_become_binary_labels(self):
        row = {"result": "LOSS", "settlement_side_value": 50, "pricing_v2": {"book_probability": 60, "kalshi_mid_probability": 50, "fair_probability": 55}}
        self.assertIsNone(binary_outcome(row))
        self.assertIsNone(probability._row_prediction(row))
        self.assertEqual(0, calibration_metrics([row])["count"])
        self.assertIsNone(binary_outcome({"result": "WIN", "strategy_analytics_excluded": True}))
        self.assertEqual(1, binary_outcome({"result": "LOSS", "settlement_side_value": 100}))

    def test_chicago_legacy_times_and_utc_provider_times(self):
        self.assertEqual("2026-09-04T00:54:19+00:00", parse_timestamp("2026-09-03T19:54:19").isoformat())
        self.assertEqual("2026-01-04T01:54:19+00:00", parse_timestamp("2026-01-03T19:54:19").isoformat())
        self.assertEqual("2026-09-03T19:54:19+00:00", probability._parse_time("2026-09-03T19:54:19").isoformat())

    def test_missing_lower_bound_is_not_zero_probability(self):
        review = fill_edge({"trusted_capper": True, "model_prob": 55}, 56, 1.72)
        self.assertIsNone(review["edge"])
        self.assertEqual(55, review["fill_reference_probability"])
        self.assertAlmostEqual(2.28, fill_edge({"model_prob_lower": 60}, 56, 1.72)["edge"])

    def test_late_consensus_is_not_fixed_horizon_clv(self):
        mark = {"captured_at": "2026-09-04T12:01:00-05:00"}
        good = book_horizon_alignment(mark, observed_at="2026-09-04T17:01:20Z", provider_updates=["2026-09-04T17:00:50Z"])
        late = book_horizon_alignment(mark, observed_at="2026-09-04T17:08:00Z", provider_updates=["2026-09-04T17:07:50Z"])
        self.assertTrue(good["book_time_valid"])
        self.assertFalse(late["book_time_valid"])
        self.assertEqual(7, late["book_attachment_delay_minutes"])

    def test_three_leg_parent_conserves_two_units_and_preserves_derivatives(self):
        parent = {"ticket_id": "parent", "pick_type": "parlay", "capper_units": 2, "target_event_date": (datetime.now().date() + timedelta(days=1)).isoformat(), "legs": [{"selection": "A", "market_type": "spread", "line_unit": "sets"}, {"selection": "B", "market_type": "total", "line_unit": "games"}, {"selection": "C", "market_type": "btts"}]}
        store = {"tickets": [parent]}
        capper._expand_parlay_leg_watch_tickets(store)
        children = store["tickets"][1:]
        self.assertEqual([1, .5, .5], [row["parlay_leg_max_units"] for row in children])
        self.assertEqual(["sets", "games", None], [row["line_unit"] for row in children])
        self.assertTrue(all(row["posted_odds"] is None for row in children))
        capper._expand_parlay_leg_watch_tickets(store)
        self.assertEqual(4, len(store["tickets"]))

    def test_parent_budget_counts_pending_and_deduplicates_persisted_fills(self):
        meta = {"parent_capper_ticket_id": "parent", "parlay_parent_total_unit_cap": 2, "unit_size": 10}
        commitments = {"r": {"parent_capper_ticket_id": "parent", "units": 1.5}}
        self.assertFalse(capper_parent_exposure_review({}, commitments, meta, 10)["ok"])
        portfolio = {"history": [{"parent_capper_ticket_id": "parent", "stake": 15, "unit_size": 10, "live_order": {"shared_bankroll": {"reservation_id": "r"}}}]}
        review = capper_parent_exposure_review(portfolio, commitments, meta, 5)
        self.assertTrue(review["ok"])
        self.assertEqual(1.5, review["used_units"])

    def test_concurrent_parent_reservations_cannot_overallocate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio = root / "portfolio.json"
            portfolio.write_text('{"bets": [], "history": []}')
            meta = {"parent_capper_ticket_id": "parent", "parlay_parent_total_unit_cap": 2, "unit_size": 10}
            settings = {"SHARED_BANKROLL_RESERVATIONS_ENABLED": True, "SHARED_RESERVATION_TTL_SECONDS": 60}
            with patch.object(shared, "STATE_FILE", root / "state.json"), patch.object(shared, "STATE_LOCK_FILE", root / "state.lock"), patch.object(shared, "SPORTS_PORTFOLIO_FILE", portfolio), patch.object(shared, "load_settings", return_value=settings), patch.object(shared, "live_order_review", return_value={"ok": True, "approved_stake": 15, "requested_stake": 15}):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: shared.reserve_live_order("sports", 15, metadata=meta), range(2)))
                self.assertEqual(1, sum(result["ok"] for result in results))
                self.assertIn("capper_parent_unit_cap", [result.get("error") for result in results])

    def test_reports_separate_manual_and_integrity_excluded_results(self):
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        base = {"result": "WIN", "settled_at": now.isoformat(), "stake": 10, "profit": 5, "unit_size": 10, "game_key": "same", "pricing_v2": {"fair_probability": 55}}
        rows = [{**base, "source": "edge_scanner"}, {**base, "source": "trusted_capper"}, {**base, "source": "trusted_capper", "strategy_analytics_excluded": True}, {**base, "source": "user_manual", "profit": 10000}]
        report = source_performance_report(rows, now=now)
        self.assertEqual(5, report["lanes"]["autonomous"]["7d"]["financial"]["after_fee_profit"])
        self.assertEqual(1, report["lanes"]["capper"]["7d"]["intended_strategy"]["records"])
        self.assertEqual(2, report["lanes"]["capper"]["7d"]["financial"]["records"])
        self.assertEqual(1, pricing_analytics(rows)["tracked_settled"])

    def test_funnel_counts_partial_expired_fills_and_manual_satisfaction_separately(self):
        now = datetime.now(timezone.utc)
        base = {"created_at": now.isoformat(), "executable": True, "pick_type": "straight", "scan_count": 1}
        rows = [{**base, "status": "expired", "fills": [{"contracts": 1}]}, {**base, "status": "placed", "status_reason": "target_satisfied_by_existing_position"}, {**base, "pick_type": "parlay", "executable": False}]
        stages = capper_funnel(rows, now=now)["windows"]["7d"]["stages"]
        self.assertEqual(2, stages["eligible"])
        self.assertEqual(1, stages["partial_fill"])
        self.assertEqual(1, stages["existing_position_satisfied"])
        self.assertEqual(0, stages.get("manual_position_satisfied", 0))
        self.assertEqual(1, stages["has_fill"])

    def test_api_shadow_never_suppresses_requests_or_exposes_provider_key(self):
        rows = [{"at": f"2026-09-04T12:00:{sec:02d}-05:00", "sport_key": "tennis", "markets": "alternate_totals", "event_id": "event", "actual_cost": 2, "provider_key": "sensitive-test-marker"} for sec in (0, 20)]
        report = api_value_shadow(rows)
        self.assertEqual(0, report["requests_suppressed"])
        self.assertEqual(1, report["groups"]["tennis|alternate_totals"]["same_update_interval_calls"])
        self.assertNotIn("sensitive-test-marker", json.dumps(report))

    def test_compressed_continuity_reports_gaps_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl.gz"
            with gzip.open(path, "wt") as handle:
                handle.write('{"generated_at":"2026-09-04T12:00:00-05:00"}\ntruncated\n')
            report = archive_continuity([path], now="2026-09-04T18:00:00-05:00", days=3)
            self.assertEqual(1, report["malformed_records"])
            self.assertEqual(["2026-09-02", "2026-09-03"], report["days_without_retained_records"])

    def test_successful_mlb_refresh_can_complete_its_quote_guard(self):
        candidate = self.mlb()
        candidate["pricing_v2"]["consensus"].update(probability=52, uncertainty_pp=.5, independent_family_count=2)
        candidate["pricing_v2"]["requested_book_weight"] = .8
        game = {"_odds_fetched_at": datetime.now(timezone.utc).isoformat(), "_odds_source": "post_ai_event_refresh"}
        with patch.object(sports, "fetch_post_ai_event_odds", return_value=(game, {"status": "fetched"})), patch.object(sports, "apply_pricing_v2_to_candidate"), patch.object(sports, "apply_native_pregame_pricing_review"), patch.object(sports, "append_jsonl"), patch.object(sports, "SPORTS_PRICING_V2_ENFORCEMENT", "active"), patch.object(sports, "SPORTS_PRICING_V2_ENABLED", True), patch.object(sports, "candidate_order_unit_required_edge", return_value=0), patch.object(sports, "sports_stream") as stream:
            stream.return_value.snapshot.return_value = {"fresh": True, "yes_bid": 41, "yes_ask": 42, "yes_ask_size": 50}
            review = sports.post_ai_revalidate_candidate(candidate, {})
            self.assertTrue(review["ok"], review)
            self.assertTrue(sports.live_order_pricing_guard(candidate)["ok"])
            self.assertEqual(2, stream.return_value.snapshot.call_count)

    def test_cache_preserves_offset_and_rejects_future_timestamp(self):
        now = datetime.now(timezone.utc)
        cache = {"sports": {"baseball_mlb": {"generated_at": (now - timedelta(minutes=3)).isoformat(), "scores": [{"id": "g"}], "games": []}}}
        self.assertIsNone(sports.cached_scores_for_sport(cache, "baseball_mlb", max_age_minutes=2))
        cache["sports"]["baseball_mlb"]["generated_at"] = (now + timedelta(minutes=1)).isoformat()
        self.assertIsNone(sports.cached_scores_for_sport(cache, "baseball_mlb", max_age_minutes=2))
        self.assertEqual("future_timestamp", sports.cached_odds_entry(cache, "baseball_mlb")["reason"])

    def test_partial_nonfocused_scan_does_not_reset_broad_deadline(self):
        now = datetime.now(timezone.utc)
        report = {"generated_at": now.isoformat(), "scan_scope": "partial_discovery", "last_broad_discovery_at": (now - timedelta(minutes=20)).isoformat()}
        self.assertTrue(sports.broad_discovery_review(report, now)["due"])

    def test_parent_state_survives_shared_snapshot_rebuild(self):
        ledger = {"r": {"parent_capper_ticket_id": "p", "units": 1}}
        def read(path, default=None):
            return {"capper_parent_commitments": ledger} if path == shared.STATE_FILE else default
        with patch.object(shared, "read_json", side_effect=read), patch.object(shared, "live_cash_from_reconciliation", return_value={"cash": 1000}), patch.object(shared, "write_json") as write:
            shared.build_shared_bankroll_state(settings=dict(shared.DEFAULTS))
            self.assertEqual(ledger, write.call_args.args[1]["capper_parent_commitments"])

    def test_parent_reservation_rejects_missing_portfolio(self):
        meta = {"parent_capper_ticket_id": "p", "parlay_parent_total_unit_cap": 2, "unit_size": 10}
        with tempfile.TemporaryDirectory() as directory, patch.object(shared, "load_settings", return_value={"SHARED_BANKROLL_RESERVATIONS_ENABLED": True}), patch.object(shared, "live_order_review", return_value={"ok": True, "approved_stake": 10}):
            with patch.object(shared, "STATE_FILE", Path(directory) / "state.json"), patch.object(shared, "STATE_LOCK_FILE", Path(directory) / "state.lock"), patch.object(shared, "SPORTS_PORTFOLIO_FILE", Path(directory) / "missing.json"):
                review = shared.reserve_live_order("sports", 10, metadata=meta)
            self.assertEqual("capper_parent_portfolio_unavailable", review["error"])

    def test_capper_coverage_is_not_truncated_by_dashboard_display_limit(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = [{"created_at": now, "pick_type": "straight", "executable": True, "status": "watching"} for _ in range(150)]
        with patch.object(capper, "list_tickets", return_value=rows):
            report = capper.ticket_summary(limit=100)
        self.assertEqual(100, len(report["tickets"]))
        self.assertEqual(150, report["coverage"]["audit_funnel_shadow"]["windows"]["7d"]["stages"]["eligible"])

    def test_calibration_excludes_untagged_manual_and_capper_owners(self):
        self.assertFalse(probability._calibration_row_eligible({"strategy_owner": "user_manual"}))
        self.assertFalse(probability._calibration_row_eligible({"source": "edge_scanner", "strategy_owner": "trusted_capper"}))
        self.assertFalse(probability._calibration_row_eligible({}))
        self.assertTrue(probability._calibration_row_eligible({"source": "edge_scanner"}))

    def test_historical_live_watch_snapshot_is_counted_without_new_flags(self):
        now = datetime.now(timezone.utc)
        row = {"created_at": now.isoformat(), "pick_type": "straight", "last_scan_at": now.isoformat(), "match_snapshot": {"price_review": {"ok": True}, "components": [{"game_started": True}]}}
        stages = capper_funnel([row], now=now)["windows"]["7d"]["stages"]
        self.assertEqual([1, 1, 1, 1], [stages[key] for key in ("scanned", "matched", "live_watched", "acceptable_price")])

    def test_calibration_cache_invalidates_on_labels_availability_and_settings(self):
        row = {"source": "edge_scanner", "game_key": "g", "result": "WIN", "placed_at": "2026-01-01T12:00:00-06:00", "settled_at": "2026-01-01T15:00:00-06:00", "pricing_v2": {"book_probability": 60, "kalshi_mid_probability": 50}}
        original = probability.calibration_training_fingerprint([row], {"promotion_enabled": False})
        for updates in ({"result": "LOSS"}, {"settled_at": "2026-01-02T15:00:00-06:00"}, {"strategy_analytics_excluded": True}, {"pricing_v2": {"book_probability": 61, "kalshi_mid_probability": 50}}):
            self.assertNotEqual(original, probability.calibration_training_fingerprint([{**row, **updates}], {"promotion_enabled": False}))
        self.assertNotEqual(original, probability.calibration_training_fingerprint([row], {"promotion_enabled": True}))
        self.assertEqual(original, probability.calibration_training_fingerprint([row, {"source": "user_manual", "result": "WIN"}], {"promotion_enabled": False}))

    def test_unchanged_calibration_reuses_fit_but_new_settlement_rebuilds(self):
        portfolio = {"history": [{"source": "edge_scanner", "game_key": "g", "result": "WIN", "placed_at": "2026-01-01T12:00:00-06:00", "settled_at": "2026-01-01T15:00:00-06:00", "pricing_v2": {"book_probability": 60, "kalshi_mid_probability": 50}}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(sports, "SPORTS_PROBABILITY_STATE_FILE", str(Path(directory) / "calibration.json")), patch.object(sports, "build_calibration_state", wraps=probability.build_calibration_state) as fit:
            self.assertFalse(sports.refresh_probability_calibration(portfolio)["calibration_cache_reused"])
            self.assertTrue(sports.refresh_probability_calibration(portfolio)["calibration_cache_reused"])
            self.assertEqual(1, fit.call_count)
            portfolio["history"][0]["result"] = "LOSS"
            self.assertFalse(sports.refresh_probability_calibration(portfolio)["calibration_cache_reused"])
            self.assertEqual(2, fit.call_count)

    def test_invalid_probability_and_missing_prediction_time_cannot_validate(self):
        self.assertIsNone(probability._row_prediction({"result": "WIN", "pricing_v2": {"book_probability": float("nan"), "kalshi_mid_probability": 50}}))
        rows = [{"game_key": str(i), "settled_at": f"2026-01-{1+i%28:02d}T15:00:00-06:00", "result": "WIN", "pricing_v2": {"book_probability": 60, "kalshi_mid_probability": 50}} for i in range(80)]
        review = probability._walk_forward_validation(rows, default_weight=.5, prior_strength=25, recency_half_life_days=60)
        self.assertFalse(review["validated"])
        self.assertEqual([], review["folds"])

    def test_mlb_force_refresh_cannot_be_satisfied_by_not_required_review(self):
        candidate = self.mlb()
        candidate["post_ai_revalidation"] = {"ok": True, "requested": False, "status": "not_required"}
        with patch.object(sports, "SPORTS_POST_AI_ODDS_REVALIDATION_ENABLED", False), patch.object(sports, "SPORTS_MLB_LIVE_SPREAD_FORCE_REVALIDATION", True):
            self.assertTrue(sports.post_ai_revalidation_required(candidate))
            self.assertEqual("mlb_required_revalidation_unavailable", sports.live_order_pricing_guard(candidate)["error"])

    def test_unknown_pending_parent_units_fail_closed(self):
        metadata = {"parent_capper_ticket_id": "p", "unit_size": 10, "parlay_parent_total_unit_cap": 2}
        review = capper_parent_exposure_review({}, {"r": {"parent_capper_ticket_id": "p", "units": None}}, metadata, 5)
        self.assertEqual("capper_parent_commitment_units_unknown", review["error"])

    def test_execution_modes_and_missing_units_remain_explicit(self):
        now = datetime.now(timezone.utc)
        base = {"source": "edge_scanner", "result": "WIN", "settled_at": now.isoformat(), "stake": 10, "profit": 5}
        rows = [{**base, "mode": "live"}, {**base, "mode": "paper", "profit": 1000, "unit_size": 10}]
        modes = source_performance_report(rows, now=now)["lanes"]["autonomous"]["7d"]["execution_modes"]
        self.assertEqual(5, modes["live"]["financial"]["after_fee_profit"])
        self.assertEqual(1000, modes["paper"]["financial"]["after_fee_profit"])
        self.assertEqual(1, modes["live"]["financial"]["missing_unit_records"])

    def test_mlb_quotes_that_age_during_exchange_repricing_are_rejected(self):
        candidate = self.mlb()
        candidate["post_ai_revalidation"] = {"ok": True, "requested": True, "fetch": {"status": "fetched"}}
        candidate["pricing_v2"]["consensus"].update(probability=52, uncertainty_pp=.5, independent_family_count=2)
        candidate["pricing_v2"]["requested_book_weight"] = .8
        def delayed_quote(*args, **kwargs):
            for observation in candidate["pricing_v2"]["consensus"]["observations"]:
                observation["last_update"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
            return {"fresh": True, "yes_bid": 41, "yes_ask": 42, "yes_ask_size": 50}
        with patch.object(sports, "SPORTS_PRICING_V2_ENFORCEMENT", "active"), patch.object(sports, "SPORTS_PRICING_V2_ENABLED", True), patch.object(sports, "sports_stream") as stream:
            stream.return_value.snapshot.side_effect = delayed_quote
            review = sports.live_order_pricing_guard(candidate)
            stream.return_value.snapshot.assert_called_once()
        self.assertFalse(review["ok"])
        self.assertIn("mlb_live_spread_book_consensus_stale", review["mlb_review"]["failures"])

    def test_ambiguous_parent_commitment_survives_ttl_until_definitive_no_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"reservations": [], "reservation_history": [], "capper_parent_commitments": {"r": {"parent_capper_ticket_id": "p", "unit_size": 10, "units": 1.5}}}
            (root / "state.json").write_text(json.dumps(state))
            with patch.object(shared, "STATE_FILE", root / "state.json"), patch.object(shared, "STATE_LOCK_FILE", root / "state.lock"):
                shared.finalize_reservation("r", "submission_uncertain")
                held = json.loads((root / "state.json").read_text())
                self.assertEqual(1.5, held["capper_parent_commitments"]["r"]["units"])
                shared.prune_reservations(held)
                self.assertIn("r", held["capper_parent_commitments"])
                shared.finalize_reservation("r", "not_filled", 0)
                self.assertEqual({}, json.loads((root / "state.json").read_text())["capper_parent_commitments"])

    def test_reconciliation_incident_records_duration_and_resolution(self):
        first = reconciliation_transition({}, {"ok": False, "warnings": ["mismatch"]}, now="2026-09-04T12:00:00-05:00")
        resolved = reconciliation_transition(first, {"ok": True}, now="2026-09-04T12:03:00-05:00")
        self.assertEqual(180, resolved["last_incident_duration_seconds"])
        self.assertIsNone(resolved["incident_started_at"])


if __name__ == "__main__":
    unittest.main()
