import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sports_operations_guardian import evaluate_health, iso_now
import sports_paper_bettor as sports


NOW = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)


class SportsOperationsGuardianTests(unittest.TestCase):
    def test_runtime_health_refresh_is_nonfatal(self):
        with patch(
            "sports_operations_guardian.check",
            return_value={"healthy": True, "process": {"pid": 123}, "warnings": []},
        ):
            result = sports.refresh_operations_health_snapshot("test")
        self.assertTrue(result["ok"])
        self.assertEqual(123, result["pid"])

    def test_healthy_process_requires_no_action(self):
        with patch("sports_operations_guardian.process_exists", return_value=True):
            health, state = evaluate_health(
                pid_payload={"pid": 123},
                report={
                    "generated_at": "2026-08-13T17:55:00+00:00",
                    "strategy_identity": sports.sports_strategy_identity(),
                    "live_reconciliation": {"account": {"ok": True}},
                },
                intents={}, settings={}, state={}, now=NOW,
            )
        self.assertTrue(health["healthy"])
        self.assertEqual("none", health["action"])
        self.assertEqual(0, state["consecutive_stale_reports"])
        self.assertNotIn("strategy_version_mismatch", health["warnings"])

    def test_old_strategy_version_is_reported(self):
        with patch("sports_operations_guardian.process_exists", return_value=True):
            health, _state = evaluate_health(
                pid_payload={"pid": 123},
                report={
                    "generated_at": "2026-08-13T17:55:00+00:00",
                    "strategy_identity": {"version": "sports-price-quality-v4"},
                    "live_reconciliation": {"account": {"ok": True}},
                },
                intents={}, settings={}, state={}, now=NOW,
            )
        self.assertIn("strategy_version_mismatch", health["warnings"])

    def test_source_feed_failure_is_unhealthy_without_restarting_worker(self):
        for source_health in ("error", "stale"):
            with self.subTest(source_health=source_health), patch("sports_operations_guardian.process_exists", return_value=True):
                health, _ = evaluate_health(
                    pid_payload={"pid": 123},
                    report={"generated_at": NOW.isoformat(), "execution_mode": "paper",
                            "aibetpicks": {"enabled": True, "health": source_health}},
                    intents={}, settings={}, state={}, now=NOW,
                )
            self.assertFalse(health["healthy"])
            self.assertEqual("alert", health["action"])
            self.assertEqual("", health["restart_reason"])
            self.assertIn("aibetpicks_feed_" + source_health, health["warnings"])

    def test_disabled_or_scheduled_source_feed_does_not_raise_false_alarm(self):
        for source in ({"enabled": False, "health": "error"}, {"enabled": True, "health": "scheduled"},
                       {"enabled": True, "health": "healthy"}):
            with self.subTest(source=source), patch("sports_operations_guardian.process_exists", return_value=True):
                health, _ = evaluate_health(
                    pid_payload={"pid": 123},
                    report={"generated_at": NOW.isoformat(), "execution_mode": "paper", "aibetpicks": source},
                    intents={}, settings={}, state={}, now=NOW,
                )
            self.assertTrue(health["healthy"])
            self.assertEqual("none", health["action"])

    def test_missing_process_requests_restart(self):
        with patch("sports_operations_guardian.process_exists", return_value=False):
            health, _state = evaluate_health(
                pid_payload={"pid": 123}, report={}, intents={}, settings={}, state={}, now=NOW
            )
        self.assertEqual("restart", health["action"])

    def test_stale_process_needs_two_consecutive_checks(self):
        with patch("sports_operations_guardian.process_exists", return_value=True):
            first, state = evaluate_health(
                pid_payload={"pid": 123},
                report={"generated_at": "2026-08-13T16:00:00+00:00"},
                intents={}, settings={}, state={}, now=NOW,
            )
            second, _state = evaluate_health(
                pid_payload={"pid": 123},
                report={"generated_at": "2026-08-13T16:00:00+00:00"},
                intents={}, settings={}, state=state, now=NOW,
            )
        self.assertEqual("none", first["action"])
        self.assertEqual("restart", second["action"])

    def test_recent_active_intent_blocks_restart(self):
        with patch("sports_operations_guardian.process_exists", return_value=False):
            health, _state = evaluate_health(
                pid_payload={"pid": 123}, report={},
                intents={"intents": [{
                    "status": "filled_pending_portfolio",
                    "updated_at": "2026-08-13T17:58:00+00:00",
                }]},
                settings={}, state={}, now=NOW,
            )
        self.assertEqual("alert", health["action"])
        self.assertFalse(health["safe_to_restart"])

    def test_unresolved_intent_never_ages_out_of_restart_guard(self):
        for status in ("prepared", "ambiguous", "response_received", "response_recovered", "filled_pending_portfolio"):
            with self.subTest(status=status), patch("sports_operations_guardian.process_exists", return_value=False):
                health, _state = evaluate_health(
                    pid_payload={"pid": 123}, report={},
                    intents={"intents": [{"status": status, "updated_at": "2026-08-12T12:00:00-05:00"}]},
                    settings={}, state={}, now=NOW,
                )
            self.assertFalse(health["healthy"])
            self.assertFalse(health["safe_to_restart"])
            self.assertEqual("alert", health["action"])
            self.assertIn("unresolved_order_intents", health["warnings"])
            self.assertIn("stale_order_intents", health["warnings"])

    def test_resolved_intent_does_not_block_restart(self):
        with patch("sports_operations_guardian.process_exists", return_value=False):
            health, _state = evaluate_health(
                pid_payload={"pid": 123}, report={},
                intents={"intents": [{"status": "recorded", "updated_at": "2026-08-12T12:00:00-05:00"}]},
                settings={}, state={}, now=NOW,
            )
        self.assertTrue(health["safe_to_restart"])
        self.assertEqual("restart", health["action"])

    def test_account_fetch_failure_is_unhealthy_without_forcing_restart(self):
        with patch("sports_operations_guardian.process_exists", return_value=True):
            health, _state = evaluate_health(
                pid_payload={"pid": 123},
                report={"generated_at": NOW.isoformat(), "execution_mode": "live",
                        "live_reconciliation": {"account": {"ok": False}}},
                intents={}, settings={}, state={}, now=NOW,
            )
        self.assertFalse(health["healthy"])
        self.assertEqual("alert", health["action"])
        self.assertIn("live_reconciliation_not_ok", health["warnings"])

    def test_fetched_account_with_position_mismatch_is_unhealthy(self):
        for field in ("position_mismatches", "unmatched_local_tickers", "unmatched_remote_tickers"):
            with self.subTest(field=field), patch("sports_operations_guardian.process_exists", return_value=True):
                health, _state = evaluate_health(
                    pid_payload={"pid": 123},
                    report={"generated_at": NOW.isoformat(), "execution_mode": "live",
                            "live_reconciliation": {"account": {"ok": True}, field: ["TEST"]}},
                    intents={}, settings={}, state={}, now=NOW,
                )
            self.assertFalse(health["healthy"])
            self.assertIn("live_reconciliation_mismatch", health["warnings"])
            self.assertEqual("alert", health["action"])

    def test_audit_failure_or_warning_is_unhealthy(self):
        for audit in ({"ok": False}, {"ok": True, "warnings": ["cash_mismatch"]}):
            with self.subTest(audit=audit), patch("sports_operations_guardian.process_exists", return_value=True):
                health, _state = evaluate_health(
                    pid_payload={"pid": 123},
                    report={"generated_at": NOW.isoformat(), "execution_mode": "live",
                            "live_reconciliation": {"account": {"ok": True}}, "live_audit": audit},
                    intents={}, settings={}, state={}, now=NOW,
                )
            self.assertFalse(health["healthy"])
            self.assertEqual("alert", health["action"])
            self.assertIn("live_audit_not_ok", health["warnings"])

    def test_paper_report_does_not_require_live_reconciliation(self):
        with patch("sports_operations_guardian.process_exists", return_value=True):
            health, _state = evaluate_health(
                pid_payload={"pid": 123},
                report={"generated_at": NOW.isoformat(), "execution_mode": "paper"},
                intents={}, settings={}, state={}, now=NOW,
            )
        self.assertTrue(health["healthy"])
        self.assertNotIn("live_reconciliation_not_ok", health["warnings"])

    def test_report_age_and_display_use_chicago_seasonal_offset(self):
        for month, offset in ((1, "-06:00"), (7, "-05:00")):
            now = datetime(2026, month, 13, 18, 0, tzinfo=timezone.utc)
            local_hour = 12 if month == 1 else 13
            with self.subTest(month=month), patch("sports_operations_guardian.process_exists", return_value=True):
                health, _state = evaluate_health(
                    pid_payload={"pid": 123},
                    report={"generated_at": f"2026-{month:02d}-13T{local_hour - 1:02d}:55:00",
                            "execution_mode": "paper"},
                    intents={}, settings={}, state={}, now=now,
                )
            self.assertEqual(5.0, health["report"]["age_minutes"])
            self.assertTrue(iso_now(now).endswith(offset))

    def test_quiet_hours_use_chicago_even_with_another_host_timezone(self):
        for month, utc_hour in ((1, 14), (7, 13)):
            now = datetime(2026, month, 13, utc_hour, tzinfo=timezone.utc)
            with self.subTest(month=month), patch("sports_operations_guardian.process_exists", return_value=True):
                health, _state = evaluate_health(
                    pid_payload={"pid": 123}, report={"execution_mode": "paper"}, intents={},
                    settings={"SPORTS_QUIET_HOURS_ENABLED": "true", "SPORTS_QUIET_START_HOUR": "21", "SPORTS_QUIET_END_HOUR": "9"},
                    state={}, now=now,
                )
            self.assertTrue(health["report"]["quiet_hours"])


if __name__ == "__main__":
    unittest.main()
