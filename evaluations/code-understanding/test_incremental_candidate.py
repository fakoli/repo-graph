"""Small source-backed update checks; not monorepo/scale qualification."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations.incremental_candidate import Candidate
from evaluations.tree_sitter_baseline import Budget

AVAILABLE = all(importlib.util.find_spec(name) for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go', 'tree_sitter_javascript', 'tree_sitter_typescript'))
MAIN = b'from .helper import finish\ndef run():\n    return finish()\n'
HELPER = b'def finish():\n    return 1\n'
SEED = 'main.py:%d:%d' % (MAIN.index(b'def run'), len(MAIN.rstrip()))


@unittest.skipUnless(AVAILABLE, 'optional pinned analysis backend not installed')
class IncrementalChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='incremental-candidate-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'source'; self.root.mkdir()
        self.logs = self.base / 'evidence'; self.logs.mkdir()
        (self.root / 'main.py').write_bytes(MAIN)
        (self.root / 'helper.py').write_bytes(HELPER)
        self.index = Candidate(self.root, budget=Budget(timeout_seconds=20))

    def refresh(self, paths=('main.py', 'helper.py'), *, candidate=None, **options):
        return (candidate or self.index).refresh(paths, evidence_directory=self.logs, **options)

    def test_unchanged_collections_are_reused_in_both_modes(self):
        first = self.refresh()
        self.assertEqual(first['status'], 'complete', first)
        self.assertEqual(first['resources']['changed_files_collected'], 2)
        result = self.refresh(mode='queued', concurrency=2)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['generation'], first['generation'])
        self.assertEqual(result['semantic_facts_sha256'], first['semantic_facts_sha256'])
        self.assertEqual(result['resources']['changed_files_collected'], 0)
        self.assertEqual(result['resources']['unchanged_source_collections_reused'], 2)
        call = self.index.snapshot.query(SEED)['rows'][0]
        self.assertEqual(call['certainty'], 'resolved')
        self.assertEqual(call['target']['id'], 'helper.py:0:%d' % len(HELPER.rstrip()))

    def test_body_change_matches_clean_rebuild(self):
        first = self.refresh()
        (self.root / 'helper.py').write_bytes(HELPER.replace(b'return 1', b'return 2'))
        result = self.refresh(mode='queued', concurrency=2)
        clean = self.refresh(candidate=Candidate(self.root, budget=Budget(timeout_seconds=20)))
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['semantic_facts_sha256'], clean['semantic_facts_sha256'])
        self.assertNotEqual(result['generation'], first['generation'])
        self.assertEqual(result['resources']['changed_files_collected'], 1)
        self.assertEqual(result['resources']['unchanged_source_collections_reused'], 1)

    def test_negative_import_addition_deletion_and_configuration_match_rebuild(self):
        absent = self.refresh(('main.py',))
        self.assertEqual(absent['status'], 'complete', absent)
        self.assertEqual(self.index.snapshot.query(SEED)['rows'][0]['certainty'], 'unresolved')
        added = self.refresh()
        self.assertEqual(self.index.snapshot.query(SEED)['rows'][0]['certainty'], 'resolved')
        removed = self.refresh(('main.py',), mode='queued', concurrency=2)
        clean = self.refresh(('main.py',), candidate=Candidate(self.root, budget=Budget(timeout_seconds=20)))
        self.assertEqual(removed['semantic_facts_sha256'], clean['semantic_facts_sha256'])
        self.assertNotEqual(added['semantic_facts_sha256'], removed['semantic_facts_sha256'])
        (self.root / 'settings.json').write_bytes(b'{"mode":1}')
        paths = ['main.py', {'path': 'settings.json', 'language': 'python', 'kind': 'configuration'}]
        before = self.refresh(paths)
        (self.root / 'settings.json').write_bytes(b'{"mode":2}')
        after = self.refresh(paths)
        self.assertEqual(before['semantic_facts_sha256'], after['semantic_facts_sha256'])
        self.assertNotEqual(before['generation'], after['generation'])
        self.assertEqual(after['resources']['changed_files_collected'], 0)

    def test_cancel_and_partial_attempt_preserve_ready_snapshot(self):
        first = self.refresh(); ready = self.index.snapshot
        stopped = self.refresh(cancel=lambda: True, mode='queued', concurrency=2)
        self.assertEqual(stopped['status'], 'interrupted', stopped)
        self.assertIs(self.index.snapshot, ready)
        (self.root / 'unsupported.rs').write_bytes(b'fn main() {}')
        stopped = self.refresh(['main.py', 'helper.py', {'path': 'unsupported.rs', 'language': 'unknown'}])
        self.assertEqual(stopped['status'], 'failed', stopped)
        self.assertEqual(stopped['stop_reason'], 'unsupported_language_in_admitted_scope')
        self.assertIs(self.index.snapshot, ready)
        self.assertEqual(self.index.snapshot.generation, first['generation'])
        self.assertTrue(any(item['status'] == 'unsupported_language' for item in stopped['inventory']))

    def test_source_change_before_publication_and_code_change_keep_previous(self):
        from evaluations import queued_collector
        first = self.refresh(); ready = self.index.snapshot
        original = queued_collector.collect_files
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.root / 'helper.py').write_bytes(HELPER.replace(b'return 1', b'return 9'))
            return result
        with patch.object(queued_collector, 'collect_files', changed):
            stopped = self.refresh()
        self.assertEqual(stopped['stop_reason'], 'source_changed_before_publication', stopped)
        self.assertEqual(stopped['validation_failures'][0]['path'], 'helper.py')
        self.assertIs(self.index.snapshot, ready)
        from evaluations import incremental_candidate
        snapshot = incremental_candidate.Snapshot
        def changed_during_staging(*args, **kwargs):
            staged = snapshot(*args, **kwargs)
            (self.root / 'helper.py').write_bytes(HELPER.replace(b'return 1', b'return 8'))
            return staged
        with patch.object(incremental_candidate, 'Snapshot', changed_during_staging):
            stopped = self.refresh()
        self.assertEqual(stopped['stop_reason'], 'source_changed_before_publication', stopped)
        self.assertIs(self.index.snapshot, ready)
        limits = []
        def capture_limits(*args, **kwargs):
            limits.append(kwargs['limits'])
            return original(*args, **kwargs)
        with patch.object(queued_collector, 'collect_files', capture_limits):
            result = self.refresh()
        self.assertEqual(result['status'], 'complete', result)
        self.assertTrue(0 < limits[0].worker_wall_seconds <= limits[0].total_wall_seconds <= 20)
        ready = self.index.snapshot
        first = result
        with patch('evaluations.incremental_candidate.collector_identity', side_effect=ValueError('stale loaded code')):
            stopped = self.refresh()
        self.assertEqual(stopped['status'], 'failed', stopped)
        self.assertIs(self.index.snapshot, ready)
        self.assertEqual(self.index.snapshot.generation, first['generation'])
        for error in (MemoryError, RecursionError):
            with patch('evaluations.incremental_candidate.Snapshot', side_effect=error):
                stopped = self.refresh()
            self.assertEqual(stopped['status'], 'failed', stopped)
            self.assertEqual(stopped['error_kind'], error.__name__)
            self.assertIs(self.index.snapshot, ready)

    def test_owner_replacement_and_source_symlinks_refuse_cache_reuse(self):
        self.refresh(); ready = self.index.snapshot
        self.root.rename(self.base / 'old-source'); self.root.mkdir()
        (self.root / 'main.py').write_bytes(MAIN); (self.root / 'helper.py').write_bytes(HELPER)
        stopped = self.refresh()
        self.assertEqual(stopped['status'], 'failed', stopped)
        self.assertIs(self.index.snapshot, ready)
        clone = Candidate(self.root, budget=Budget(timeout_seconds=20))
        result = self.refresh(candidate=clone)
        self.assertEqual(result['status'], 'complete', result)
        self.assertNotEqual(result['generation'], ready.generation)
        (self.root / 'helper.py').unlink(); (self.root / 'helper.py').symlink_to(self.base / 'old-source' / 'helper.py')
        stopped = self.refresh(candidate=clone)
        self.assertEqual(stopped['status'], 'failed', stopped)

    def test_native_stages_use_whole_update_deadline_and_runtime_errors_are_retained(self):
        from evaluations import incremental_candidate as candidate, queued_collector
        from evaluations.tree_sitter_baseline import CollectedFile
        self.refresh(); ready = self.index.snapshot
        stages = [(CollectedFile, 'from_json'), (CollectedFile, 'to_json'), (candidate, 'resolve_collected')]
        for owner, name in stages:
            original = getattr(owner, name)
            def expired(*args, **kwargs):
                with patch.object(candidate, 'time', SimpleNamespace(monotonic=lambda: time.monotonic() + 21)):
                    return original(*args, **kwargs)
            if name != 'from_json':
                (self.root / 'helper.py').write_bytes(HELPER.replace(b'return 1', b'return 3'))
            with patch.object(owner, name, expired):
                stopped = self.refresh()
            self.assertEqual(stopped['status'], 'interrupted', (name, stopped))
            self.assertEqual(stopped['stop_reason'], 'deadline_exceeded', (name, stopped))
            self.assertIs(self.index.snapshot, ready)
        with patch.object(queued_collector, 'collect_files', side_effect=RuntimeError('directory fsync failed')):
            stopped = self.refresh()
        self.assertEqual(stopped['status'], 'failed', stopped)
        self.assertEqual(stopped['error_kind'], 'RuntimeError', stopped)
        self.assertIs(self.index.snapshot, ready)


if __name__ == '__main__':
    unittest.main()
