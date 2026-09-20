"""Retiring an execution worker must not erase account risk or expose old keys."""
import copy
import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dashboard
import shared_bankroll


class WeatherRetirementTests(unittest.TestCase):
    def test_retired_worker_cannot_start_or_stop_through_dashboard(self):
        with patch.object(dashboard.subprocess, 'Popen') as launch:
            for action in (dashboard.start_process, dashboard.stop_process):
                with self.assertRaisesRegex(ValueError, 'unknown_process'):
                    action('weather')
            launch.assert_not_called()

    def test_saved_retired_secrets_stay_private_and_cannot_be_reenabled(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            original = {'WEATHER_BOT_ENABLED': 'false', 'VISUAL_CROSSING_API_KEY': 'private-old-key',
                        'TOMORROW_API_KEY': 'private-old-key', 'OPEN_METEO_API_KEY': 'private-old-key',
                        'SPORTS_EXECUTION_MODE': 'paper', 'XAI_API_KEY': 'active-secret'}
            path.write_text(json.dumps(original), encoding='utf-8')
            with patch.object(dashboard, 'SETTINGS_FILE', path):
                dashboard.save_settings({'WEATHER_BOT_ENABLED': 'true', 'SPORTS_SCAN_INTERVAL_MINUTES': '2'})
                public = dashboard.public_settings()
            stored = json.loads(path.read_text(encoding='utf-8'))
            for key, value in original.items():
                self.assertEqual(stored[key], value)
            self.assertEqual(stored['SPORTS_SCAN_INTERVAL_MINUTES'], '2')
            self.assertNotIn('WEATHER_BOT_ENABLED', public)
            for key in ('VISUAL_CROSSING_API_KEY', 'TOMORROW_API_KEY', 'OPEN_METEO_API_KEY'):
                self.assertNotIn(key, public)
            self.assertNotIn('active-secret', json.dumps(public))

    def test_retired_strategy_cannot_reserve_cash_even_with_shared_checks_disabled(self):
        with patch.object(shared_bankroll, 'shared_state_lock', side_effect=nullcontext), \
             patch.object(shared_bankroll, 'load_settings', return_value={'SHARED_BANKROLL_ENABLED': 'false'}), \
             patch.object(shared_bankroll, 'write_json') as write:
            review = shared_bankroll.reserve_live_order('weather', 10)
        self.assertFalse(review['ok'])
        self.assertEqual(review['approved_stake'], 0)
        self.assertEqual(review['error'], 'shared_unsupported_strategy')
        write.assert_not_called()

    def test_retired_ledger_and_reservations_still_count_toward_account_risk(self):
        now = datetime.now().astimezone()
        retired = {'bets': [{'mode': 'live', 'status': 'open', 'stake': 12}],
                   'history': [{'mode': 'live', 'status': 'settled', 'settled_at': now.isoformat(), 'profit': -8}]}
        reservation = {'strategy': 'weather', 'status': 'active', 'stake': 3,
                       'expires_at': (now + timedelta(minutes=2)).isoformat()}

        def read(path, default):
            if Path(path).name == 'weather_live_portfolio.json':
                return copy.deepcopy(retired)
            if path == shared_bankroll.STATE_FILE:
                return {'reservations': [copy.deepcopy(reservation)]}
            return copy.deepcopy(default)

        with patch.object(shared_bankroll, 'shared_state_lock', side_effect=nullcontext), \
             patch.object(shared_bankroll, 'read_json', side_effect=read), \
             patch.object(shared_bankroll, 'live_cash_from_reconciliation', return_value={'cash': 1000}), \
             patch.object(shared_bankroll, 'write_json') as write:
            state = shared_bankroll.build_shared_bankroll_state(dict(shared_bankroll.DEFAULTS), save=False)
        self.assertEqual(state['system_live_open_exposure'], 15)
        self.assertEqual(state['retired_live_open_exposure'], 12)
        self.assertEqual(state['retired_live_open_count'], 1)
        self.assertEqual(state['system_live_realized_profit'], -8)
        self.assertEqual(state['system_worst_case_loss_used'], 23)
        self.assertNotIn('weather', state['strategy_caps'])
        write.assert_not_called()

    def test_state_build_does_not_read_retired_dashboard_files(self):
        with patch.object(dashboard, 'load_settings', return_value={}), \
             patch.object(dashboard, 'public_settings', return_value={}), \
             patch.object(dashboard, 'build_shared_bankroll_state', return_value={}), \
             patch.object(dashboard, 'build_sports_state', return_value={}), \
             patch.object(dashboard, 'build_crypto_state', return_value={}), \
             patch.object(dashboard, 'aibetpicks_summary', return_value={}), \
             patch.object(dashboard, 'itf_shadow_summary', return_value={}), \
             patch.object(dashboard, 'itf_followups_summary', return_value={}), \
             patch.object(dashboard, 'modest_recovery_summary', return_value={}), \
             patch.object(dashboard, 'process_status', return_value={}), \
             patch.object(dashboard, 'build_local_ai_state', return_value={}), \
             patch.object(dashboard, 'read_storage_health', return_value={}), \
             patch.object(dashboard, 'read_json', return_value={}) as read:
            state = dashboard.build_state()
        self.assertNotIn('weather', state)
        self.assertNotIn('events', state)
        self.assertNotIn('logs', state)
        self.assertEqual([call.args[0] for call in read.call_args_list], [dashboard.SPORTS_PORTFOLIO_FILE])

    def test_bot_today_badge_uses_only_active_automated_ledgers(self):
        self.assertIn('const liveProfit = Number(sportsHeader.bot_today_profit || 0) + Number(cryptoHeader.bot_today_profit || 0);', dashboard.HTML)
        self.assertNotIn('data-view="weather"', dashboard.HTML)


if __name__ == '__main__':
    unittest.main()
