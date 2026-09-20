from pathlib import Path
import hashlib
import json
import tempfile
import unittest
import frozen_crypto_shadow_runner as runner


class FrozenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        bodies = {name: '# archived ' + name for name in runner.FILES}
        self.identity = runner.digest({name: runner.digest(body) for name, body in bodies.items()})
        self.source = self.root / 'archives/frozen_crypto_shadow' / self.identity / 'source'
        self.source.mkdir(parents=True)
        manifest = {}
        for name, body in bodies.items():
            target = self.source / name
            target.write_text(body, encoding='utf-8')
            manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        (self.source.parent / 'manifest.json').write_text(json.dumps({'files': manifest}))
        self.state = self.root / 'crypto_shadow_expansion.json'
        self.state.write_text(json.dumps({'mode': 'shadow_only', 'implementation_hash': self.identity, 'records': ['preserved']}))

    def test_exact_source_is_verified_without_changing_the_cohort(self):
        before = self.state.read_bytes()
        source, identity = runner.verified_source(self.root)
        self.assertEqual(source, self.source)
        self.assertEqual(identity, self.identity)
        self.assertEqual(self.state.read_bytes(), before)

    def test_changed_source_cannot_resume_collection(self):
        (self.source / runner.FILES[0]).write_text('different rules')
        with self.assertRaises(ValueError):
            runner.verified_source(self.root)

    def test_mutable_paths_use_existing_workspace_and_hash_reads_use_retained_source(self):
        root = runner.SourceAndDataRoot(self.root, self.source)
        self.assertEqual(root / 'crypto_shadow_expansion.json', self.state)
        self.assertEqual(root / 'crypto_shadow_expansion.lock', self.root / 'crypto_shadow_expansion.lock')
        self.assertEqual(root / 'crypto_paper_bettor.py', self.source / 'crypto_paper_bettor.py')
        self.assertEqual(Path(root), self.root)

    def test_private_account_adapter_is_unavailable(self):
        with self.assertRaisesRegex(RuntimeError, 'cannot call'):
            runner.deny_live_access('/portfolio/events/orders', method='POST')

    def test_missing_manifest_dependency_is_rejected(self):
        (self.source.parent / 'manifest.json').write_text(json.dumps({'files': {}}))
        with self.assertRaises(ValueError):
            runner.verified_source(self.root)


if __name__ == '__main__':
    unittest.main()
