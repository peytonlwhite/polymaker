import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage_maintenance as storage


class StorageMaintenanceTests(unittest.TestCase):
    def test_rotate_and_compress_preserves_analytics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sports_paper_events.jsonl"
            source.write_text('{"type":"settled"}\n' * 20, encoding="utf-8")
            archive = root / "archives" / "storage_retention"
            with patch.object(storage, "ARCHIVE_ROOT", archive):
                rotated = storage.rotate_file(source, max_bytes=10)
                self.assertFalse(source.exists())
                compressed = storage._gzip_file(rotated)
                self.assertTrue(compressed.exists())
                with gzip.open(compressed, "rt", encoding="utf-8") as handle:
                    self.assertEqual(len(handle.readlines()), 20)

    def test_event_archives_never_expire(self):
        self.assertEqual(storage._category(Path("sports_paper_events.jsonl.20200101T000000Z.gz")), "analytics")
        self.assertEqual(storage._category(Path("sports_paper_log.txt.20200101T000000Z.gz")), "logs")

    def test_cached_health_read(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "health.json"
            report.write_text(json.dumps({"status": "healthy", "total_bytes": 12}), encoding="utf-8")
            with patch.object(storage, "HEALTH_FILE", report):
                self.assertEqual(storage.read_storage_health()["total_bytes"], 12)


if __name__ == "__main__":
    unittest.main()
