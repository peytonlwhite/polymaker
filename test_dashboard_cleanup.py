import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dashboard


class DashboardCleanupTests(unittest.TestCase):
    def test_tail_preserves_unicode_crlf_and_unterminated_last_line(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'activity.log'
            lines = ['older ' + 'x' * 65530, 'Sports · café', 'Crypto · 50¢', 'latest']
            path.write_bytes('\r\n'.join(lines).encode('utf-8'))
            self.assertEqual(dashboard.read_tail(path, 3), lines[-3:])
            self.assertEqual(dashboard.read_lines(path, 0), lines)
            path.write_bytes(path.read_bytes() + b'\r\n')
            self.assertEqual(dashboard.read_tail(path, 3), lines[-3:])

    def test_small_tail_does_not_read_an_entire_large_log(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'activity.log'
            path.write_bytes(b'old entry\n' * 200000 + b'latest entry\n')
            with path.open('rb') as stream:
                reads = []
                original_read = stream.read

                def read(size=-1):
                    data = original_read(size)
                    reads.append(len(data))
                    return data

                with patch.object(stream, 'read', side_effect=read), patch.object(Path, 'open', return_value=stream):
                    self.assertEqual(dashboard.read_tail(path, 2), ['old entry', 'latest entry'])
                self.assertLess(sum(reads), 100000)

    def test_tail_spans_chunks_without_losing_long_entries(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'activity.log'
            lines = ['ignored', 'é' * 70000, 'final']
            path.write_text('\n'.join(lines), encoding='utf-8')
            self.assertEqual(dashboard.read_tail(path, 2), lines[-2:])

    def test_missing_and_empty_logs_are_empty(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'missing.log'
            self.assertEqual(dashboard.read_tail(path), [])
            path.touch()
            self.assertEqual(dashboard.read_tail(path), [])

    def test_event_tail_ignores_partial_json_without_discarding_valid_events(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'events.jsonl'
            path.write_text('{"old":1}\n{"recent":2}\npartial\n{"recent":3}', encoding='utf-8')
            self.assertEqual(dashboard.read_jsonl_events(path, 3), [{'recent': 2}, {'recent': 3}])

    def test_edit_preserves_unexposed_settings_and_saved_secret(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            saved = {'SPORTS_EXECUTION_MODE': 'paper', 'SPORTS_PHASE_TWO_ENABLED': 'true',
                     'SPORTS_CUSTOM_RESEARCH_FLAG': 'keep', 'XAI_API_KEY': 'test-secret'}
            path.write_text(json.dumps(saved), encoding='utf-8')
            with patch.object(dashboard, 'SETTINGS_FILE', path), patch.object(dashboard, 'switch_sports_mode_files') as switch:
                dashboard.save_settings({'SPORTS_UNIT_SIZE_PCT': '0.5'})
                switch.assert_not_called()
            result = json.loads(path.read_text(encoding='utf-8'))
            for key, value in saved.items():
                self.assertEqual(result[key], value)
            self.assertEqual(result['SPORTS_UNIT_SIZE_PCT'], '0.5')


if __name__ == '__main__':
    unittest.main()
