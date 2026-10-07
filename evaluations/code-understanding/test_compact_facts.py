"""Small synthetic checks for one node-free collector and its JSON boundary."""
import copy
from dataclasses import fields, is_dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations import tree_sitter_baseline as native

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))


def blob(source, language='python', path='main.py'):
    return {'path': path, 'language': language, 'content': source.encode('utf-8')}


def digest(encoded):
    return hashlib.sha256(encoded).hexdigest()


@unittest.skipUnless(AVAILABLE, 'optional pinned analysis backend not installed')
class CompactFactsTests(unittest.TestCase):
    def test_native_objects_are_released_before_binding(self):
        collectors = []
        original = native.FileFacts.release
        def observed(file):
            original(file)
            collectors.append(file)
        with patch.object(native.FileFacts, 'release', observed):
            file = native.collect_file(blob('def finish():\n    return 1\ndef run():\n    return finish()\n'))
        self.assertIsNone(collectors[0].tree)
        self.assertIsNone(collectors[0].raw)
        self.assertFalse(collectors[0].nodes)
        self.assertFalse(collectors[0].scopes)
        self.assertFalse(collectors[0].callable_nodes)
        seen = set()
        def plain(value):
            if id(value) in seen:
                return
            seen.add(id(value))
            self.assertFalse(type(value).__module__.startswith('tree_sitter'))
            self.assertNotIsInstance(value, (bytes, native.Work))
            if is_dataclass(value):
                for field in fields(value):
                    plain(getattr(value, field.name))
            elif isinstance(value, dict):
                for key, item in value.items():
                    plain(key)
                    plain(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    plain(item)
            else:
                self.assertIsInstance(value, (str, int, bool, type(None)))
        plain(file)
        result = native.resolve_collected([file])
        call = next(s for s in result['facts']['sites'] if s['role'] == 'call')
        self.assertEqual(call['targets'], [file.definitions[0]['id']])

    def test_four_languages_round_trip_and_rebinding_match_extract(self):
        examples = [
            blob('def finish(value):\n    return value\ndef run():\n    step = finish\n    return step(1)\n'),
            blob('function finish(value){return value;} function run(){const step=finish;return step(1);}', 'javascript', 'main.js'),
            blob('function finish(value:number):number{return value;} function run(){const step=finish;return step(1);}', 'typescript', 'main.ts'),
            blob('package p\nfunc Finish(value int)int{return value}\nfunc Run()int{step:=Finish;return step(1)}\n', 'go', 'main.go'),
        ]
        for source in examples:
            with self.subTest(language=source['language']):
                file = native.collect_file(source)
                encoded = file.to_json()
                loaded = native.CollectedFile.from_json(encoded, file.record, digest(encoded))
                self.assertEqual(loaded.to_json(), encoded)
                expected = native.extract([source])['facts']
                self.assertEqual(native.resolve_collected([loaded])['facts'], expected)
                self.assertEqual(native.resolve_collected([loaded])['facts'], expected)
                self.assertTrue(all(isinstance(b.node, native.Position)
                    for scope in loaded.scopes for entries in scope.bindings.values() for b in entries))

    def test_local_namespace_import_and_parameter_shadowing(self):
        sources = [blob('export function finish(){return 1;}', 'javascript', 'helper.js'),
                   blob("import * as lib from './helper.js';function run(){return lib.finish();}function shadow(lib){return lib.finish();}",
                        'javascript', 'main.js')]
        files = [native.collect_file(source) for source in sources]
        facts = native.resolve_collected(files)['facts']
        calls = [s for s in facts['sites'] if s['text'] == 'lib.finish()']
        self.assertEqual([s['certainty'] for s in calls], ['resolved', 'unresolved'])
        self.assertEqual(calls[0]['targets'], [files[0].definitions[0]['id']])
        self.assertEqual(calls[1]['resolution_method'], 'unsupported_receiver')
        self.assertEqual(facts, native.extract(sources)['facts'])

    def test_json_rejects_forged_links_stale_identity_and_wrong_types(self):
        file = native.collect_file(blob('def finish():\n    return 1\ndef run():\n    step = finish\n    return step()\n'))
        original = file.payload()
        edits = [
            lambda p: p.update(collector_sha256='0' * 64),
            lambda p: p['versions'].update({'tree-sitter': 'unqualified'}),
            lambda p: p['record'].update(path='foreign.py'),
            lambda p: p['record'].update(bytes=True),
            lambda p: p['record'].update(path='./main.py'),
            lambda p: p['definitions'][0].update(id='foreign:0:1'),
            lambda p: p['definitions'][0]['range'].update(start_byte=True),
            lambda p: p['definitions'][0]['range'].update(end_byte=file.record['bytes'] + 1),
            lambda p: p['definitions'].append(copy.deepcopy(p['definitions'][0])),
            lambda p: p['scopes'][1].update(parent=1),
            lambda p: p['scopes'][1].update(id=True),
            lambda p: p['scopes'][0]['bindings']['finish'][0].update(scope=1),
            lambda p: p['scopes'][0]['bindings']['finish'][0].update(value='foreign:0:1'),
            lambda p: p['candidates'][0].update(scope=True),
            lambda p: p['candidates'][0]['fact'].update(caller='foreign:0:1'),
            lambda p: p['candidates'][0]['fact'].update(targets=['forged']),
            lambda p: p['candidates'][0]['callee'].update(start_byte=True),
            lambda p: p['candidates'][0]['callee'].update(base_identifier=True),
            lambda p: p['counts'].update(nodes=0),
            lambda p: p.update(partial=1),
            lambda p: p['syntax_metadata'].update(go_cgo_import='false'),
        ]
        for edit in edits:
            with self.subTest(edit=edits.index(edit)):
                payload = copy.deepcopy(original)
                edit(payload)
                encoded = json.dumps(payload).encode('utf-8')
                # A fresh digest still cannot bypass typed source/link validation.
                with self.assertRaises(ValueError):
                    native.CollectedFile.from_json(encoded, file.record, digest(encoded))
        encoded = file.to_json()
        with self.assertRaisesRegex(ValueError, 'producer digest'):
            native.CollectedFile.from_json(encoded + b' ', file.record, digest(encoded))
        wrong_record = dict(file.record, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'identity'):
            native.CollectedFile.from_json(encoded, wrong_record, digest(encoded))
        with patch.object(native, '_LOADED_SOURCE_SHA256', '0' * 64):
            with self.assertRaisesRegex(ValueError, 'changed since module import'):
                file.to_json()

    def test_json_rejects_duplicate_keys_nonfinite_and_float_overflow(self):
        file = native.collect_file(blob('def finish():\n    return 1\nfinish()\n'))
        encoded = file.to_json()
        negatives = [encoded.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1)]
        for number in (b'NaN', b'Infinity', b'-Infinity', b'1e999', b'1.0'):
            negatives.append(encoded.replace(b'"schema_version":1', b'"schema_version":' + number, 1))
        for malformed in negatives:
            with self.subTest(payload=malformed[:40]):
                with self.assertRaises(ValueError):
                    native.CollectedFile.from_json(malformed, file.record, digest(malformed))

    def test_byte_caps_and_aggregate_admission_are_explicit(self):
        source = blob('def finish():\n    return 1\nfinish()\n')
        file = native.collect_file(source)
        with self.assertRaisesRegex(native.StopScan, 'collected_byte_budget'):
            native.collect_file(source, native.Budget(max_collected_bytes=8))
        with self.assertRaisesRegex(native.StopScan, 'handoff_byte_budget'):
            file.to_json(native.Budget(max_handoff_bytes=8))
        encoded = file.to_json()
        with self.assertRaisesRegex(ValueError, 'unbounded'):
            native.CollectedFile.from_json(encoded, file.record, digest(encoded), native.Budget(max_handoff_bytes=8))
        with self.assertRaisesRegex(ValueError, 'unbounded'):
            native.CollectedFile.from_json(encoded, file.record, digest(encoded), native.Budget(max_collected_bytes=8))
        second = native.collect_file(dict(source, path='second.py'))
        with self.assertRaisesRegex(native.StopScan, 'source_byte_budget'):
            native.resolve_collected([file, second], budget=native.Budget(max_total_bytes=len(source['content'])))
        with self.assertRaisesRegex(native.StopScan, 'node_budget'):
            native.resolve_collected([file, second], budget=native.Budget(max_nodes=file.counts['nodes']))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            native.resolve_collected([file, file])
        for values in ({'max_nodes': True}, {'max_facts': 1.0}, {'timeout_seconds': float('inf')},
                       {'timeout_seconds': float('nan')}, {'timeout_seconds': True}):
            with self.subTest(budget=values), self.assertRaises(ValueError):
                native.Budget(**values)

    def test_scan_reads_then_compacts_each_file_and_guards_identity(self):
        events = []
        read, collect = native.SourceRoot.read, native._collect_file
        def observed_read(source, path, *args, **kwargs):
            events.append(('read', path))
            return read(source, path, *args, **kwargs)
        def observed_collect(record, *args):
            events.append(('collect', record['path']))
            return collect(record, *args)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'a.py').write_text('def finish():\n    return 1\nfinish()\n')
            (root / 'b.py').write_text('def second():\n    return 2\nsecond()\n')
            with patch.object(native.SourceRoot, 'read', observed_read), patch.object(native, '_collect_file', observed_collect):
                result = native.scan(root, ['a.py', 'b.py'])
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(events, [('read', 'a.py'), ('collect', 'a.py'), ('read', 'b.py'), ('collect', 'b.py')])
            changed = native.scan(root, [{'path': 'a.py', 'sha256': '0' * 64}])
            self.assertEqual(changed['status'], 'partial')
            self.assertEqual(changed['inventory'][0]['status'], 'source_error')
            self.assertFalse(changed['facts']['definitions'])

    def test_cancel_after_parse_releases_collector(self):
        parsers, _ = native.backend()
        cancelled, collectors = [False], []
        class Parser:
            def parse(self, raw):
                tree = parsers['python'].parse(raw)
                cancelled[0] = True
                return tree
        release = native.FileFacts.release
        def observed(file):
            release(file)
            collectors.append(file)
        source = blob('def finish():\n    return 1\n')
        record = {'path': 'main.py', 'language': 'python', 'bytes': len(source['content']),
                  'sha256': digest(source['content']), 'kind': 'source'}
        with patch.object(native.FileFacts, 'release', observed):
            with self.assertRaisesRegex(native.StopScan, 'cancelled'):
                native._collect_file(record, source['content'], Parser(), native.Work(native.Budget(), lambda: cancelled[0]))
        self.assertIsNone(collectors[0].tree)
        self.assertIsNone(collectors[0].raw)
        self.assertFalse(collectors[0].scopes)

    def test_source_only_input_and_neutral_go_metadata(self):
        source = blob('//go:build custom\npackage sample\nimport "C"\nfunc External()\n', 'go', 'api_client.go')
        file = native.collect_file(source)
        self.assertEqual(file.syntax_metadata['package_clauses'][0]['name'], 'sample')
        self.assertTrue(file.syntax_metadata['go_control_directive'])
        self.assertTrue(file.syntax_metadata['go_bodyless_function'])
        self.assertTrue(file.syntax_metadata['go_cgo_import'])
        encoded = file.to_json()
        loaded = native.CollectedFile.from_json(encoded, file.record, digest(encoded))
        self.assertEqual(loaded.syntax_metadata, file.syntax_metadata)
        for forbidden in ('targets', 'certainty', 'oracle', 'definitions'):
            with self.subTest(key=forbidden), self.assertRaisesRegex(ValueError, 'Source-only'):
                native.collect_file(dict(source, **{forbidden: []}))
        with self.assertRaisesRegex(ValueError, 'identity'):
            native.collect_file(dict(source, sha256='0' * 64))


if __name__ == '__main__':
    unittest.main()
