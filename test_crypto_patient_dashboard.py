from copy import deepcopy
from datetime import timedelta
import http.client
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

import dashboard
from crypto_patient_dashboard import LIVE_PROFILE, set_control, summary, load_portfolio
from crypto_patient_promotion import ENABLED, FIELD, VERSION
from crypto_patient_runtime import RUNTIME_VERSION
from test_crypto_patient_promotion import NOW, settings


class PatientDashboardTests(unittest.TestCase):
    def setUp(self):
        self.settings = settings(**{ENABLED: "false", "CRYPTO_LIVE_ORDER_ENABLED": "false"})
        self.globals = {"ALLOW_LIVE_TRADING": "true"}
        self.saved = deepcopy(self.settings)
        def save(changes):
            self.saved.update(changes)
            return dict(self.saved)
        self.save = Mock(side_effect=save)
        self.reload = Mock(return_value={"running": True, "pid": 123})

    def control(self, action, **kwargs):
        return set_control(action, self.settings, self.globals, {}, save=self.save,
                           reload_worker=self.reload, portfolio={"bets": []}, now=NOW, **kwargs)

    def test_status_read_cannot_activate_or_restart(self):
        result = summary(self.settings, self.globals, {}, now=NOW)
        self.assertEqual(result["label"], "READY TO MAKE LIVE")
        self.assertTrue(result["can_activate"])
        self.save.assert_not_called()
        self.reload.assert_not_called()

    def test_activation_changes_only_explicit_strategy_controls(self):
        result = self.control("activate")
        self.assertEqual(result["control"]["status"], "pending")
        self.assertEqual(self.saved[ENABLED], "true")
        self.assertEqual(self.saved["CRYPTO_ETH_PATIENT_BASE_STAKE_PCT"], "1")
        self.assertEqual(self.saved["CRYPTO_LIVE_ORDER_ENABLED"], "true")
        self.assertEqual(self.saved["CRYPTO_RUN_LOOP"], "true")
        self.assertEqual(set(self.save.call_args.args[0]), {ENABLED, "CRYPTO_ETH_PATIENT_BASE_STAKE_PCT",
                         "CRYPTO_ETH_PATIENT_REQUESTED_AT", "CRYPTO_LIVE_ORDER_ENABLED", "CRYPTO_RUN_LOOP"})
        self.assertEqual(self.saved["CRYPTO_15M_SPRINT_SHADOW_ONLY"], "true")
        self.reload.assert_called_once_with(needs_reload=True, open_positions=0)

    def test_unresolved_order_blocks_activation(self):
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            set_control("activate", self.settings, self.globals, {}, save=self.save, reload_worker=self.reload,
                        portfolio={"bets": [], FIELD: {"records": {"x": {"status": "order_uncertain"}}}}, now=NOW)
        self.save.assert_not_called()

    def test_controls_and_other_pilot_must_remain_isolated(self):
        for key, value in (("CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL", "false"),
                           ("CRYPTO_LIVE_REQUIRE_CONFIRMATION", "true"),
                           ("CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED", "true"),
                           ("CRYPTO_15M_SPRINT_SHADOW_ONLY", "false")):
            original = self.settings[key]
            self.settings[key] = value
            with self.assertRaises(ValueError):
                self.control("activate")
            self.settings[key] = original
        self.save.assert_not_called()
        self.reload.assert_not_called()

    def test_pause_does_not_stop_worker_or_touch_positions(self):
        self.settings[ENABLED] = "true"
        self.control("pause")
        self.save.assert_called_once_with({ENABLED: "false"})
        self.reload.assert_not_called()

    def test_duplicate_activation_does_not_restart_again(self):
        self.settings[ENABLED] = "true"
        self.control("activate")
        self.save.assert_not_called()
        self.reload.assert_not_called()

    def test_failed_reload_rolls_back_activation(self):
        self.reload.side_effect = RuntimeError("mock startup failure")
        with self.assertRaises(RuntimeError):
            self.control("activate")
        self.assertEqual(self.saved[ENABLED], "false")
        self.assertEqual(self.saved["CRYPTO_LIVE_ORDER_ENABLED"], "false")

    def test_status_requires_new_worker_acknowledgment(self):
        self.settings.update({**LIVE_PROFILE,
                              "CRYPTO_ETH_PATIENT_REQUESTED_AT": NOW.isoformat()})
        report = {"generated_at": (NOW-timedelta(seconds=1)).isoformat(), FIELD: {"version": VERSION, "runtime_version": RUNTIME_VERSION, "status": "watching"}}
        self.assertEqual(summary(self.settings, self.globals, report, now=NOW)["status"], "pending")
        report["generated_at"] = NOW.isoformat()
        self.assertEqual(summary(self.settings, self.globals, report, now=NOW)["status"], "active")
        report[FIELD]["status"] = "source_history_warmup"
        self.assertEqual(summary(self.settings, self.globals, report, now=NOW)["status"], "warming_up")
        self.assertEqual(summary(self.settings, self.globals, report, now=NOW+timedelta(seconds=181))["status"], "ready")

    def test_corrupt_portfolio_is_never_replaced(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/"portfolio.json"
            path.write_text("{broken")
            with self.assertRaises(json.JSONDecodeError):
                load_portfolio(path)
            self.assertEqual(path.read_text(), "{broken")

    def test_user_reload_preserves_sizing_and_other_controls(self):
        self.settings.update({ENABLED: "true", "CRYPTO_LIVE_ORDER_ENABLED": "true",
                              "CRYPTO_ETH_PATIENT_BASE_STAKE_PCT": "0.8",
                              "CRYPTO_ETH_PATIENT_REQUESTED_AT": (NOW-timedelta(hours=1)).isoformat()})
        report = {"generated_at": NOW.isoformat(), FIELD: {"version": VERSION, "status": "source_history_warmup"}}
        self.assertTrue(summary(self.settings, self.globals, report, now=NOW)["can_reload"])
        set_control("reload", self.settings, self.globals, report, save=self.save,
                    reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.save.assert_called_once_with({"CRYPTO_ETH_PATIENT_REQUESTED_AT": NOW.isoformat()})
        self.reload.assert_called_once_with(needs_reload=True, open_positions=0)
        report[FIELD]["runtime_version"] = RUNTIME_VERSION
        self.assertFalse(summary(self.settings, self.globals, report, now=NOW)["can_reload"])
        set_control("reload", self.settings, self.globals, report, save=self.save,
                    reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.assertEqual(self.reload.call_count, 1)

    def test_reload_requires_fresh_report_and_resolved_intents(self):
        self.settings.update({ENABLED: "true", "CRYPTO_LIVE_ORDER_ENABLED": "true",
                              "CRYPTO_ETH_PATIENT_REQUESTED_AT": (NOW-timedelta(hours=1)).isoformat()})
        report = {"generated_at": (NOW-timedelta(minutes=5)).isoformat(), FIELD: {"version": VERSION}}
        with self.assertRaisesRegex(ValueError, "fresh"):
            set_control("reload", self.settings, self.globals, report, save=self.save,
                        reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        report["generated_at"] = NOW.isoformat()
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            set_control("reload", self.settings, self.globals, report, save=self.save,
                        reload_worker=self.reload,
                        portfolio={"bets": [], FIELD: {"records": {"x": {"status": "submitting"}}}}, now=NOW)
        self.save.assert_not_called()
        self.reload.assert_not_called()

    def test_reload_failure_restores_request_timestamp(self):
        requested = (NOW-timedelta(hours=1)).isoformat()
        self.settings.update({ENABLED: "true", "CRYPTO_LIVE_ORDER_ENABLED": "true",
                              "CRYPTO_ETH_PATIENT_REQUESTED_AT": requested})
        report = {"generated_at": NOW.isoformat(), FIELD: {"version": VERSION}}
        self.reload.side_effect = RuntimeError("startup failed")
        with self.assertRaises(RuntimeError):
            set_control("reload", self.settings, self.globals, report, save=self.save,
                        reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.assertEqual(self.saved["CRYPTO_ETH_PATIENT_REQUESTED_AT"], requested)

    def test_missing_implementation_cannot_claim_activation(self):
        with self.assertRaisesRegex(ValueError, "not installed"):
            self.control("activate", code_installed=False)
        self.save.assert_not_called()

    def test_make_live_selects_only_patient_and_loads_no_timer_runtime(self):
        self.settings.update({ENABLED: "true", "CRYPTO_LIVE_ORDER_ENABLED": "true",
                              "CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED": "true",
                              "CRYPTO_15M_SPRINT_SHADOW_ONLY": "false"})
        report = {"generated_at": (NOW-timedelta(seconds=1)).isoformat(), FIELD: {"version": VERSION}}
        result = set_control("make_live", self.settings, self.globals, report, save=self.save,
                             reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.assertEqual(result["control"]["status"], "pending")
        self.assertFalse(result["control"]["can_make_live"])
        for key, value in LIVE_PROFILE.items():
            self.assertEqual(self.saved[key], value)
        self.reload.assert_called_once_with(needs_reload=True, open_positions=0)
        set_control("make_live", self.saved, self.globals, report, save=self.save,
                    reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.assertEqual(self.reload.call_count, 1)

    def test_make_live_has_no_report_age_or_research_waiting_gate(self):
        report = {"generated_at": (NOW-timedelta(hours=1)).isoformat(), FIELD: {"version": VERSION}}
        self.assertTrue(summary(self.settings, self.globals, report, now=NOW)["can_make_live"])
        set_control("make_live", self.settings, self.globals, report, save=self.save,
                    reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.reload.assert_called_once()

    def test_make_live_does_not_bypass_account_or_order_integrity(self):
        for change, portfolio in (({"CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false"}, {"bets": []}),
                                  ({}, {"bets": [], FIELD: {"records": {"x": {"status": "order_uncertain"}}}})):
            with self.assertRaises(ValueError):
                set_control("make_live", {**self.settings, **change}, self.globals, {}, save=self.save,
                            reload_worker=self.reload, portfolio=portfolio, now=NOW)
        self.save.assert_not_called()
        self.reload.assert_not_called()

    def test_make_live_does_not_restart_loaded_profile(self):
        selected = {**self.settings, **LIVE_PROFILE, "CRYPTO_ETH_PATIENT_REQUESTED_AT": NOW.isoformat()}
        report = {"generated_at": NOW.isoformat(), FIELD: {"version": VERSION,
                  "runtime_version": RUNTIME_VERSION, "status": "watching"}}
        result = set_control("make_live", selected, self.globals, report, save=self.save,
                             reload_worker=self.reload, portfolio={"bets": []}, now=NOW)
        self.assertEqual(result["control"]["status"], "active")
        self.save.assert_not_called()
        self.reload.assert_not_called()

    def test_endpoint_rejects_cross_origin_and_requires_explicit_header(self):
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(dashboard, "control_patient_crypto", return_value={"ok": True}) as control:
                for headers, expected in (({}, 400), ({"X-Crypto-Patient-Action": "1", "Origin": "https://example.com"}, 400),
                                           ({"X-Crypto-Patient-Action": "1"}, 200)):
                    connection = http.client.HTTPConnection(*server.server_address)
                    connection.request("POST", "/api/crypto/patient", json.dumps({"action": "activate"}),
                                       {"Content-Type": "application/json", **headers})
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    connection.close()
                control.assert_called_once_with("activate")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_worker_reload_stops_only_trader_not_process_tree(self):
        with TemporaryDirectory() as folder:
            with patch.object(dashboard, "read_json", return_value={"pid": 42}), patch.object(dashboard, "pid_running", return_value=True), \
                 patch.object(dashboard.subprocess, "run") as stop, patch.object(dashboard.subprocess, "Popen", return_value=Mock(pid=43)) as start, \
                 patch.object(dashboard, "merged_process_env", return_value={}), patch.object(dashboard, "load_processes", return_value={}), \
                 patch.object(dashboard, "save_processes"), patch("builtins.open", unittest.mock.mock_open()):
                result = dashboard.reload_patient_crypto_worker(needs_reload=True, open_positions=2)
        self.assertEqual(result["pid"], 43)
        self.assertIn("Stop-Process -Id 42", stop.call_args.args[0][-1])
        process_pattern = re.search("Name -notmatch '([^']+)'", stop.call_args.args[0][-1]).group(1)
        self.assertTrue(re.fullmatch(process_pattern, "python3.13.exe"))
        self.assertNotIn("taskkill", str(stop.call_args))
        self.assertEqual(start.call_args.kwargs["env"]["CRYPTO_RUN_LOOP"], "true")


if __name__ == "__main__":
    unittest.main()
