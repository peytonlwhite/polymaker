from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import json
import os
import tempfile
import unittest

import dashboard
import process_supervision as supervision


class WorkerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.started = datetime.now(timezone.utc)
        (self.root / '.sports_bot.pid').write_text(json.dumps({
            'pid': 123, 'label': 'sports bot', 'started_at': self.started.isoformat()}))

    def probe(self, **changes):
        return Mock(return_value={'running': True, 'image': r'C:\Python\python3.13.exe',
                                  'created_at': self.started.timestamp() - 1, **changes})

    def test_current_worker_is_verified_without_a_launcher(self):
        result = supervision.worker_status('sports', self.root, self.probe())
        self.assertTrue(result['identity_verified'])
        self.assertEqual(result['pid'], 123)

    def test_recycled_pid_is_unknown_and_cannot_be_stopped(self):
        result = supervision.worker_status('sports', self.root, self.probe(created_at=self.started.timestamp()+30))
        self.assertIsNone(result['running'])
        self.assertEqual(result['reason'], 'worker_start_time_mismatch')

    def test_unrelated_executable_is_not_a_worker(self):
        result = supervision.worker_status('sports', self.root, self.probe(image=r'C:\Windows\notepad.exe'))
        self.assertIsNone(result['running'])

    def test_permission_failure_is_unknown_not_offline(self):
        result = supervision.worker_status('sports', self.root, self.probe(running=None, reason='denied'))
        self.assertIsNone(result['running'])

    def test_exited_process_is_offline(self):
        self.assertFalse(supervision.worker_status('sports', self.root, self.probe(running=False))['running'])

    def test_bad_guard_does_not_authorize_a_duplicate_launch(self):
        (self.root / '.sports_bot.pid').write_text('{')
        self.assertIsNone(supervision.worker_status('sports', self.root, self.probe())['running'])

    def test_native_probe_finds_current_process_and_missing_pid(self):
        self.assertTrue(supervision.native_process(os.getpid())['running'])
        self.assertFalse(supervision.native_process(0)['running'])


class DashboardProcessTests(unittest.TestCase):
    def test_status_prefers_live_worker_over_stale_stopped_registry_and_does_not_write(self):
        observed = {'running': True, 'pid': 999, 'identity_verified': True, 'status': 'running'}
        with patch.object(dashboard, 'load_processes', return_value={'sports': {'pid': 111, 'running': False}}), \
                patch.object(dashboard, 'worker_status', return_value=observed), \
                patch.object(dashboard, 'save_processes') as save:
            result = dashboard.process_status()['sports']
        self.assertEqual(result['pid'], 999)
        self.assertTrue(result['running'])
        save.assert_not_called()

    def test_unverified_worker_blocks_start_without_subprocess(self):
        with patch.object(dashboard, 'process_status', return_value={'crypto': {'running': None}}), \
                patch.object(dashboard.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                dashboard.start_process('crypto')
        spawn.assert_not_called()

    def test_crypto_start_passes_continuous_loop_to_launcher(self):
        with patch.object(dashboard, 'process_status', side_effect=[{'crypto': {'running': False}}, {'crypto': {'running': True}}]), \
                patch.object(dashboard, 'merged_process_env', return_value={'CRYPTO_RUN_LOOP': 'false'}), \
                patch.object(dashboard, 'load_processes', return_value={}), patch.object(dashboard, 'save_processes'), \
                patch.object(dashboard.subprocess, 'Popen', return_value=Mock(pid=123)) as spawn:
            dashboard.start_process('crypto')
        self.assertEqual(spawn.call_args.kwargs['env']['CRYPTO_RUN_LOOP'], 'true')

    def test_stop_targets_only_verified_scanner_not_companion_tree(self):
        with patch.object(dashboard, 'load_processes', return_value={'crypto': {'pid': 111}}), \
                patch.object(dashboard, 'worker_status', return_value={'running': True, 'pid': 999}), \
                patch.object(dashboard, 'save_processes'), patch.object(dashboard, 'process_status', return_value={'crypto': {}}), \
                patch.object(dashboard.subprocess, 'run') as stop:
            dashboard.stop_process('crypto')
        self.assertEqual(stop.call_args.args[0], ['taskkill', '/PID', '999', '/F'])

    def test_unknown_identity_never_kills_a_process(self):
        with patch.object(dashboard, 'load_processes', return_value={}), \
                patch.object(dashboard, 'worker_status', return_value={'running': None}), \
                patch.object(dashboard.subprocess, 'run') as stop:
            with self.assertRaises(ValueError):
                dashboard.stop_process('sports')
        stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
