from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

import crypto_automation_control as health


class PatientHealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = {**health.PROTECTED_UNIT_RISK_SETTINGS, **health.PROTECTED_RECOVERY_SETTINGS,
                         'CRYPTO_ETH_PATIENT_ONLY_LIVE': 'true', 'CRYPTO_ETH_PATIENT_ENABLED': 'false',
                         'CRYPTO_ETH_PATIENT_BASE_STAKE_PCT': '1', 'CRYPTO_LIVE_ORDER_ENABLED': 'true',
                         'CRYPTO_15M_SPRINT_SHADOW_ONLY': 'true', 'CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL': 'true',
                         'CRYPTO_RECONCILE_LIVE_ON_SCAN': 'true', 'CRYPTO_LIVE_DEPTH_PREFLIGHT_ENABLED': 'true'}
        self.report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'live',
                       'eth_patient_promotion': {'owner': 'crypto_eth_patient_recovery', 'version': 'eth-patient-bankroll-v1', 'status': 'disabled'},
                       'live_reconciliation': {'account': {'ok': True, 'orders_complete': True, 'positions_complete': True}}}

    def review(self):
        (self.root / 'crypto_settings.json').write_text(json.dumps(self.settings))
        (self.root / 'crypto_live_report.json').write_text(json.dumps(self.report))
        with patch.object(health, '_pid_exists', return_value=True):
            return health.build_health_report(self.root)

    def test_paused_patient_profile_does_not_require_retired_strategies_to_be_live(self):
        result = self.review()
        rows = {r['name']: r for r in result['checks']}
        self.assertEqual(result['active_profile'], 'patient_eth')
        self.assertFalse(result['entries_enabled'])
        self.assertTrue(rows['patient_execution_controls']['ok'])
        self.assertTrue(rows['protected_unit_risk_settings']['ok'])
        self.assertFalse(rows['spot_flow_live_pilot_isolation']['applicable'])

    def test_active_patient_requires_correct_execution_switches(self):
        self.settings.update(CRYPTO_ETH_PATIENT_ENABLED='true', CRYPTO_LIVE_DRY_RUN='true')
        self.report['eth_patient_promotion']['status'] = 'watching'
        self.assertIn('patient_execution_controls', self.review()['critical_failures'])

    def test_bankroll_and_depth_guards_cannot_be_weakened_by_profile_selection(self):
        for key in ('CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL', 'CRYPTO_LIVE_DEPTH_PREFLIGHT_ENABLED', 'CRYPTO_15M_SPRINT_SHADOW_ONLY'):
            with self.subTest(key=key):
                self.settings[key] = 'false'
                self.assertIn('patient_execution_controls', self.review()['critical_failures'])
                self.settings[key] = 'true'

    def test_other_protected_risk_values_still_compare_exactly(self):
        self.settings['SHARED_CRYPTO_DAILY_LOSS_PCT'] = '0.95'
        self.assertIn('protected_unit_risk_settings', self.review()['critical_failures'])

    def test_incomplete_account_and_ambiguous_order_still_raise_critical(self):
        self.report['live_reconciliation']['account']['orders_complete'] = False
        (self.root / 'crypto_live_portfolio.json').write_text(json.dumps({'eth_patient_promotion': {'records': {'ETH': {'status': 'order_uncertain'}}}}))
        result = self.review()
        self.assertIn('patient_order_intents', result['critical_failures'])
        self.assertIn('patient_account_complete', result['critical_failures'])

    def test_unrecognized_profile_does_not_suppress_legacy_health_checks(self):
        self.report['eth_patient_promotion']['owner'] = 'unknown'
        result = self.review()
        self.assertEqual(result['active_profile'], 'legacy')
        self.assertIn('spot_flow_live_pilot_isolation', result['critical_failures'])


if __name__ == '__main__':
    unittest.main()
