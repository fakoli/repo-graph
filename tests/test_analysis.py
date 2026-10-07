"""Synthetic source/ownership regressions for the optional structural index."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_graph.analysis import IndexLimits, StructuralIndex
from repo_graph import analysis_native as native
from repo_graph.source import SourceRoot


AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))
FACT_KINDS = ('definitions', 'sites', 'scopes', 'relationships')


def canonical(rows):
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def facts(index):
    return {kind: canonical(index.read_facts(kind)) for kind in FACT_KINDS}


def write_sources(root, sources):
    for path, content in sources.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode('utf-8'))


def blobs(root, paths):
    languages = {'.py': 'python', '.go': 'go', '.js': 'javascript', '.ts': 'typescript'}
    return [{'path': path, 'language': languages[Path(path).suffix],
             'content': (root / path).read_bytes()} for path in paths]


@unittest.skipUnless(AVAILABLE, 'Optional analysis extra is not installed')
class StructuralIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name)
        self.root, self.output = self.scratch / 'repo', self.scratch / 'out'
        self.root.mkdir()

    def ready(self, index, paths, **options):
        result = index.refresh(paths, **options)
        self.assertEqual(result['status'], 'ready', result)
        self.assertTrue(result['generation'])
        self.assertTrue(result['source_identity'])
        return result

    def clean_parity(self, index, paths):
        clean = native.extract(blobs(self.root, paths))
        self.assertEqual(clean['status'], 'complete', clean)
        for kind in ('definitions', 'sites'):
            self.assertEqual(canonical(index.read_facts(kind)), canonical(clean['facts'][kind]))

    def test_persisted_facts_round_trip_physical_utf8_and_lexical_links(self):
        source = '# élève 東京\r\ndef café():\r\n    return 1\r\ndef appel():\r\n    return café()\r\n'
        write_sources(self.root, {'main.py': source})
        index = StructuralIndex(self.root, self.output)
        raw = source.encode('utf-8')
        inventory = [{'path': 'main.py', 'language': 'python', 'kind': 'source',
                      'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}]
        result = self.ready(index, inventory)
        self.clean_parity(index, ['main.py'])
        retained = facts(index)
        for row in retained['definitions'] + retained['sites']:
            location = row['range']
            self.assertEqual(raw[location['start_byte']:location['end_byte']].decode('utf-8'), row['text'])
            self.assertEqual(row['provenance']['source_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row['provenance']['evidence_kind'], 'static_syntax')
        call = next(row for row in retained['sites'] if row['role'] == 'call')
        target = next(row for row in retained['definitions'] if row['name'] == 'café')
        self.assertEqual(call['targets'], [target['id']])
        self.assertTrue(any(row['path'] == 'main.py' and row['kind'] == 'module' for row in retained['scopes']))
        self.assertTrue(any(row['path'] == 'main.py' and row['name'] == 'appel' and row['parent'] is not None
                            for row in retained['scopes']))
        self.assertTrue(any(row['site_id'] == call['id'] and row['target_id'] == target['id']
                            for row in retained['relationships']))
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(facts(reopened), retained)
        metadata = reopened.metadata()
        self.assertEqual(metadata['generation'], result['generation'])
        for key in ('repository_identity', 'analyzer_identity', 'config_identity'):
            self.assertTrue(metadata[key])
        self.assertEqual([path.name for path in self.output.glob('*.db')], ['search.db'])
        with self.assertRaises(ValueError):
            list(reopened.read_facts('invented'))

    def test_serial_and_queued_modes_publish_identical_source_facts(self):
        sources = {
            'main.py': 'def target(): return 1\ndef call(): return target()\n',
            'main.go': 'package main\nfunc Target() int { return 1 }\nfunc Call() int { return Target() }\n',
            'main.js': 'function target() { return 1; }\nfunction call() { return target(); }\n',
            'main.ts': 'function target(): number { return 1; }\nfunction call(): number { return target(); }\n',
        }
        write_sources(self.root, sources)
        serial = StructuralIndex(self.root, self.output, limits=IndexLimits(batch_files=2))
        queued = StructuralIndex(self.root, self.scratch / 'queued', limits=IndexLimits(batch_files=2))
        serial_result = self.ready(serial, iter(sources), mode='serial', concurrency=1)
        queued_result = self.ready(queued, iter(sources), mode='queued', concurrency=2)
        self.assertEqual(facts(serial), facts(queued))
        self.assertEqual(serial_result['source_identity'], queued_result['source_identity'])
        self.assertEqual(serial_result['coverage'], queued_result['coverage'])
        self.assertEqual(serial_result['resources']['batches'], 2)
        self.assertEqual(queued_result['resources']['batches'], 2)
        self.clean_parity(queued, list(sources))

    def test_lazy_single_file_batches_resolve_imports_after_all_collection(self):
        sources = {'package/caller.py': 'from .provider import target as alias\ndef call(): return alias()\n',
                   'package/provider.py': 'def target(): return 1\n',
                   'package/absolute.py': 'from package.provider import target as alias\ndef call(): return alias()\n'}
        write_sources(self.root, sources)
        index = StructuralIndex(self.root, self.output, limits=IndexLimits(batch_files=1))
        visited = []
        def inventory():
            for path in sources:
                visited.append(path)
                yield path
        result = self.ready(index, inventory())
        self.assertEqual(visited, list(sources))
        self.assertEqual(result['resources']['batches'], 3)
        self.clean_parity(index, list(sources))
        absolute = next(row for row in index.read_facts('sites')
                        if row['role'] == 'call' and row['path'] == 'package/absolute.py')
        self.assertEqual((absolute['certainty'], absolute['targets']), ('unresolved', []))
        self.assertIn('absolute Python import environment', absolute['reason'])
        call = next(row for row in index.read_facts('sites')
                    if row['role'] == 'call' and row['path'] == 'package/caller.py')
        self.assertEqual(call['certainty'], 'resolved')
        self.assertEqual(len(call['targets']), 1)
        sources['package/provider.py'] = 'def renamed(): return 1\n'
        write_sources(self.root, {'package/provider.py': sources['package/provider.py']})
        updated = self.ready(index, (path for path in sources))
        self.assertEqual(updated['resources']['changed_files_collected'], 1)
        self.assertEqual(updated['resources']['unchanged_source_collections_reused'], 2)
        self.clean_parity(index, list(sources))
        call = next(row for row in index.read_facts('sites')
                    if row['role'] == 'call' and row['path'] == 'package/caller.py')
        self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
        self.assertEqual(list(index.read_facts('relationships')), [])

    def test_reopen_reuses_unchanged_collections_without_reparsing(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\ndef call(): return target()\n'})
        original = StructuralIndex(self.root, self.output)
        first = self.ready(original, ['main.py'])
        retained = facts(original)
        reopened = StructuralIndex(self.root, self.output)
        with patch.object(native, 'collect_file', side_effect=AssertionError('Unchanged source reparsed')):
            second = self.ready(reopened, ['main.py'])
        self.assertEqual(second['resources']['changed_files_collected'], 0)
        self.assertEqual(second['resources']['unchanged_source_collections_reused'], 1)
        self.assertEqual(second['source_identity'], first['source_identity'])
        self.assertEqual(second['generation'], first['generation'])
        self.assertEqual(facts(reopened), retained)

    def test_unsupported_inventory_remains_visible_without_source_facts(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\n',
                                  'other.rs': 'fn undiscovered() {}\n'})
        index = StructuralIndex(self.root, self.output)
        result = self.ready(index, ['main.py', 'other.rs'])
        self.assertEqual(result['coverage']['files_total'], 2)
        self.assertEqual(result['coverage']['files_supported'], 1)
        self.assertEqual(result['coverage']['files_unsupported'], 1)
        for kind in FACT_KINDS:
            self.assertFalse(any(row.get('path') == 'other.rs' for row in index.read_facts(kind)))
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata()['generation'], result['generation'])

    def test_explicit_output_cannot_exchange_facts_between_equal_source_roots(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\n'})
        owned = StructuralIndex(self.root, self.output)
        self.ready(owned, ['main.py'])
        retained, metadata = facts(owned), owned.metadata()
        before = (self.output / 'search.db').read_bytes()
        foreign = self.scratch / 'foreign'
        foreign.mkdir()
        write_sources(foreign, {'main.py': 'def target(): return 1\n'})
        stamp = (self.root / 'main.py').stat()
        os.utime(foreign / 'main.py', ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        try:
            index = StructuralIndex(foreign, self.output)
            result = index.refresh(['main.py'])
        except (ValueError, OSError, RuntimeError):
            pass
        else:
            self.assertIn(result['status'], ('failed', 'interrupted'))
            self.assertFalse(result['published'])
            with self.assertRaises((ValueError, OSError, RuntimeError)):
                list(index.read_facts('definitions'))
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata(), metadata)
        self.assertEqual(facts(reopened), retained)

    def test_failed_source_updates_and_cancel_preserve_published_artifact(self):
        write_sources(self.root, {'main.py': 'def target(): return 1\n'})
        index = StructuralIndex(self.root, self.output)
        self.ready(index, ['main.py'])
        retained, metadata = facts(index), index.metadata()
        before = (self.output / 'search.db').read_bytes()
        outside = self.scratch / 'outside.py'
        outside.write_text('def foreign(): return 9\n')
        (self.root / 'main.py').unlink()
        (self.root / 'main.py').symlink_to(outside)
        result = index.refresh(['main.py'])
        self.assertIn(result['status'], ('failed', 'interrupted'))
        self.assertFalse(result['published'])
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        (self.root / 'main.py').unlink()
        write_sources(self.root, {'main.py': 'def target(): return 2\n'})
        read = SourceRoot.read
        changed = False
        def mutate(boundary, path, *args, **kwargs):
            nonlocal changed
            result = read(boundary, path, *args, **kwargs)
            if boundary.root == self.root and path == 'main.py' and not changed:
                changed = True
                write_sources(self.root, {'main.py': 'def target(): return 3\n'})
            return result
        with patch.object(SourceRoot, 'read', mutate):
            result = index.refresh(['main.py'])
        self.assertTrue(changed)
        self.assertIn(result['status'], ('failed', 'interrupted'))
        self.assertFalse(result['published'])
        self.assertEqual((self.output / 'search.db').read_bytes(), before)
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            with self.subTest(mode=mode):
                result = index.refresh(['main.py'], mode=mode, concurrency=concurrency, cancel=lambda: True)
                self.assertEqual(result['status'], 'interrupted')
                self.assertFalse(result['published'])
                self.assertEqual((self.output / 'search.db').read_bytes(), before)
        reopened = StructuralIndex(self.root, self.output)
        self.assertEqual(reopened.metadata(), metadata)
        self.assertEqual(facts(reopened), retained)


class StructuralValidationTests(unittest.TestCase):
    def test_invalid_inventory_queue_and_limit_arguments_are_rejected(self):
        with tempfile.TemporaryDirectory() as scratch:
            root, output = Path(scratch) / 'repo', Path(scratch) / 'out'
            root.mkdir()
            write_sources(root, {'main.py': 'def target(): return 1\n'})
            index = StructuralIndex(root, output)
            malformed = [
                ['main.py', 'main.py'], ['/main.py'], ['../main.py'], ['./main.py'],
                ['dir//main.py'], ['dir\\main.py'], [''], [True], 'main.py', [{}],
                [{'path': 'main.py', 'content': b'not metadata'}],
                [{'path': 'main.py', 'language': True}],
                [{'path': 'main.py', 'sha256': 'invalid'}],
                [{'path': 'main.py', 'bytes': True}],
                [{'path': 'main.py', 'bytes': -1}],
                [{'path': 'main.py', 'kind': 'invented'}],
            ]
            for inventory in malformed:
                with self.subTest(inventory=inventory), self.assertRaises((ValueError, OSError)):
                    index.refresh(inventory)
            arguments = [dict(mode='invented'), dict(concurrency=0), dict(concurrency=True),
                         dict(mode='queued', concurrency=5), dict(mode='serial', concurrency=2)]
            for options in arguments:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    index.refresh(['main.py'], **options)
            for options in (dict(max_files=0), dict(batch_files=True), dict(max_source_bytes=-1),
                            dict(max_index_bytes=0), dict(total_wall_seconds=float('inf'))):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    IndexLimits(**options)
            self.assertFalse((output / 'search.db').exists())


if __name__ == '__main__':
    unittest.main()
