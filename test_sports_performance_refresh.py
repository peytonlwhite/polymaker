import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sports_paper_bettor as sports
from sports_build import PROCESS_IDENTITY, source_identity, strategy_fields
from sports_scan_planner import make_plan, focused_route, cadence_sleep
from sports_spread_audit import entry_evidence, spread_report
from sports_audit_analytics import source_performance_report
from sports_analytics import compact_candidate_snapshot
from sports_candidate_tracking import compact_calibration_observation

NOW = datetime(2026, 9, 14, 17, tzinfo=timezone.utc)


def candidate(index=0, **changes):
    return {"kalshi_ticker": f"T{index}", "sport_key": "baseball_mlb", "event_id": f"E{index}",
            "commence_time": (NOW + timedelta(minutes=30)).isoformat(), "edge": 3, **changes}


def settled(**changes):
    return {"mode": "live", "source": "edge_scanner", "market_type": "spread", "market_line": 1.5,
            "kalshi_order_side": "no", "game_key": "event-1", "result": "LOSS", "stake": 20, "profit": -20,
            "unit_size": 20, "settled_at": (NOW - timedelta(days=1)).isoformat(),
            "pricing_v2": {"consensus": {"independent_family_count": 2, "family_probability_range_pp": 2,
                "observations": [{"all_exact_lines": True, "selected_point": 1.5, "age_minutes": 0.5}]}}, **changes}


class FocusedScanTests(unittest.TestCase):
    def test_round_robin_covers_all_tickers_and_bounds_each_batch(self):
        rows = [candidate(i, sport_key=f"sport_{i % 4}") for i in range(40)]
        plan = make_plan(rows, now=NOW)
        visited = set()
        for _ in range(50):
            route = focused_route(plan, now=NOW)
            self.assertLessEqual(len(route["tickers"]), 16)
            self.assertLessEqual(len(route["sport_keys"]), 4)
            visited.update(route["tickers"])
            plan["cursor"] = route["next_cursor"]
        self.assertEqual({row["kalshi_ticker"] for row in rows}, visited)

    def test_alternate_lines_do_not_monopolize_first_batch(self):
        rows = [candidate(i, event_id="one") for i in range(30)] + [candidate(31, event_id="two")]
        route = focused_route(make_plan(rows, now=NOW), now=NOW)
        self.assertIn("T31", route["tickers"])

    def test_discovery_rebuild_keeps_next_unvisited_target(self):
        rows = [candidate(i) for i in range(40)]
        plan = make_plan(rows, now=NOW)
        first = focused_route(plan, now=NOW)
        plan["cursor"] = first["next_cursor"]
        rebuilt = make_plan(rows, now=NOW, previous=plan)
        second = focused_route(rebuilt, now=NOW)
        self.assertFalse(set(first["tickers"]) & set(second["tickers"]))

    def test_focused_scan_defers_unfilled_research_probes_without_network(self):
        with patch.object(sports, "fetch_kalshi_market_by_ticker") as fetch:
            result = sports.probe_candidate_registry_settlements({"pending": {"old": {"ticker": "old"}}}, {}, focused_scan=True)
        fetch.assert_not_called()
        self.assertEqual("next_broad_discovery", result["deferred"])

    def test_completed_far_future_and_unknown_start_are_excluded(self):
        rows = [candidate(1, game_completed=True), candidate(2, commence_time=None),
                candidate(3, commence_time=(NOW + timedelta(days=1)).isoformat()), candidate(4)]
        self.assertEqual(["T4"], focused_route(make_plan(rows, now=NOW), now=NOW)["tickers"])

    def test_live_and_pregame_use_distinct_start_to_start_cadence(self):
        plan = make_plan([candidate()], now=NOW)
        pregame = focused_route(plan, now=NOW)
        live = focused_route(make_plan([candidate(commence_time=NOW.isoformat())], now=NOW), now=NOW)
        self.assertEqual(60, pregame["target_interval_seconds"])
        self.assertEqual(40, live["target_interval_seconds"])
        self.assertEqual(17, cadence_sleep({"scan_duration_seconds": 23}, live))
        self.assertEqual(5, cadence_sleep({"scan_duration_seconds": 180}, live))
        self.assertEqual(97, cadence_sleep({"scan_duration_seconds": 23}, pregame, 2))

    def test_stale_or_future_plan_cannot_run(self):
        plan = make_plan([candidate()], now=NOW)
        self.assertFalse(focused_route(plan, now=NOW + timedelta(minutes=16))["enabled"])
        self.assertFalse(focused_route(plan, now=NOW - timedelta(seconds=1))["enabled"])

    def test_focused_expansion_does_not_reopen_all_active_or_imported_sports(self):
        catalog = [{"key": "baseball_mlb", "active": True}, {"key": "soccer_epl", "active": True}]
        with ExitStack() as stack:
            for name, value in {"SPORTS_ALL_ACTIVE_ENABLED": True, "SPORTS_ODDS_SKIP_INACTIVE": True}.items():
                stack.enter_context(patch.object(sports, name, value))
            stack.enter_context(patch.object(sports, "fetch_active_sports", return_value=catalog))
            tickets = stack.enter_context(patch.object(sports, "trusted_capper_active_sports", return_value={"soccer_epl"}))
            self.assertEqual(["baseball_mlb"], sports.expand_sport_keys(["baseball_mlb"], discover_all=False))
            tickets.assert_not_called()

    def test_scheduler_uses_focused_elapsed_time(self):
        now = datetime.now(timezone.utc)
        plan = make_plan([candidate(commence_time=(now + timedelta(minutes=20)).isoformat())], now=now)
        with patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1}):
            self.assertEqual(35, sports.next_sports_scan_sleep_seconds({"focused_scan_plan": plan, "scan_duration_seconds": 25}))

    def test_focused_quote_refresh_expires_live_cache_after_provider_interval(self):
        now = datetime.now(timezone.utc)
        game = {"id": "g", "commence_time": (now - timedelta(minutes=30)).isoformat()}
        cache = {"sports": {"baseball_mlb": {"generated_at": (now - timedelta(seconds=45)).isoformat(), "games": [game]}}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(sports, "odds_paid_refresh_pacing", return_value={"paid_refresh_multiplier": 1}))
            stack.enter_context(patch.object(sports, "trusted_capper_watches_exact_sport", return_value=False))
            stack.enter_context(patch.object(sports, "trusted_capper_broad_discovery_sport", return_value=False))
            status = sports.cached_odds_entry(cache, "baseball_mlb", focused_refresh=True, overnight_active=False)
        self.assertEqual("focused_refresh_due", status["reason"])
        self.assertEqual(40, status["refresh_seconds"])


class BuildIdentityTests(unittest.TestCase):
    def test_source_changes_change_build_but_settings_and_runtime_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = root / "sports_example.py"
            code.write_bytes(b"value = 1\r\n")
            original = source_identity(root)["build_id"]
            code.write_bytes(b"value = 1\n")
            (root / "bot_settings.json").write_text('{"SECRET":"test-only"}')
            (root / "sports_portfolio.json").write_text('{}')
            self.assertEqual(original, source_identity(root)["build_id"])
            code.write_bytes(b"value = 2\n")
            self.assertNotEqual(original, source_identity(root)["build_id"])

    def test_running_identity_is_immutable_during_later_source_inspection(self):
        original = copy.deepcopy(PROCESS_IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            source_identity(directory)
        self.assertEqual(original, PROCESS_IDENTITY)

    def test_decision_and_calibration_snapshots_keep_build_and_start_time(self):
        row = {**candidate(), **strategy_fields()}
        snapshots = [compact_candidate_snapshot(row, scan_id="s", scan_reason="regular"), compact_calibration_observation(row)]
        for snapshot in snapshots:
            self.assertEqual(PROCESS_IDENTITY["build_id"], snapshot["strategy_build_id"])
            self.assertEqual(PROCESS_IDENTITY["process_started_at"], snapshot["strategy_process_started_at"])

    def test_build_cohorts_separate_live_paper_and_legacy(self):
        rows = [settled(strategy_build_id="new"), settled(mode="paper", strategy_build_id="new"), settled()]
        cohorts = source_performance_report(rows, now=NOW)["lanes"]["autonomous"]["30d"]["build_cohorts"]
        self.assertEqual(3, len(cohorts))
        self.assertEqual({("live", "new:unknown"), ("paper", "new:unknown"), ("live", "legacy_unknown:unknown")},
                         {(row["execution_mode"], row["build"]) for row in cohorts})


class SpreadAuditTests(unittest.TestCase):
    def test_no_contract_keeps_selected_positive_line(self):
        evidence = entry_evidence(settled())
        self.assertEqual("+1.5", evidence["signed_line"])
        self.assertEqual("verified", evidence["exact_line"])

    def test_missing_evidence_does_not_mean_good_agreement_or_exact_line(self):
        evidence = entry_evidence(settled(pricing_v2={}))
        self.assertEqual("unverified", evidence["exact_line"])
        self.assertEqual("missing", evidence["oldest_quote_age"])
        self.assertEqual("missing", evidence["book_agreement"])

    def test_wrong_selected_line_cannot_be_verified(self):
        row = settled(market_line=-1.5)
        self.assertEqual("unverified", entry_evidence(row)["exact_line"])

    def test_interpolated_lines_are_separate(self):
        row = settled()
        row["pricing_v2"]["consensus"]["observations"][0]["interpolated"] = True
        self.assertEqual("interpolated", entry_evidence(row)["exact_line"])

    def test_financial_and_intended_results_exclude_other_sources_and_future(self):
        rows = [settled(), settled(source="user_manual"), settled(source="aibetpicks"), settled(mode="paper"),
                settled(settled_at=(NOW + timedelta(days=1)).isoformat()),
                settled(strategy_analytics_excluded=True, profit=-5)]
        original = copy.deepcopy(rows)
        report = spread_report(rows, now=NOW, build_id="new", bootstrap_samples=0)
        window = report["windows"]["30d"]
        self.assertEqual(-25, window["financial"]["after_fee_profit"])
        self.assertEqual(-20, window["intended_strategy"]["after_fee_profit"])
        self.assertEqual(0, window["current_build"]["records"])
        self.assertEqual(original, rows)
        self.assertFalse(report["affects_execution"])

    def test_quote_age_uses_oldest_and_game_phase_records_quarter(self):
        row = settled(game_state_features={"quarter": 3, "score_context_fresh": True})
        row["pricing_v2"]["consensus"]["observations"].append({"age_minutes": 3})
        evidence = entry_evidence(row)
        self.assertEqual("over_2m", evidence["oldest_quote_age"])
        self.assertEqual("quarter_3", evidence["game_phase"])


if __name__ == "__main__":
    unittest.main()
