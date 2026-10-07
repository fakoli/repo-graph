"""Boundary and uncertainty checks for the optional native comparison baseline."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
from contextlib import redirect_stdout
import io
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations.analysis import grade, record_task, write_result
from evaluations import analysis
from evaluations import tree_sitter_baseline as native

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))


def blob(content, language='python', path=None):
    suffix = {'python': 'py', 'go': 'go', 'javascript': 'js', 'typescript': 'ts'}[language]
    return {'path': path or 'main.' + suffix, 'language': language, 'content': content.encode('utf-8')}


def site(result, source):
    return next(item for item in result['facts']['sites'] if item['text'] == source and item['role'] == 'call')


class BackendTests(unittest.TestCase):
    def test_work_root_creation_pins_parent_before_mkdir(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source, parent = root / 'source', root / 'parent'
            source.mkdir()
            parent.mkdir()
            mapping = root / 'map.json'
            mapping.write_text(json.dumps({'corpora': [{'source': str(source)}]}))
            mkdir = os.mkdir
            def swap(path, *args, **kwargs):
                if Path(path).name == 'new-workers':
                    parent.rename(root / 'old-parent')
                    parent.symlink_to(source, target_is_directory=True)
                return mkdir(path, *args, **kwargs)
            with patch.object(os, 'mkdir', side_effect=swap):
                with analysis.worker_directory(mapping, parent / 'new-workers') as pinned:
                    (pinned / 'proof').write_text('owned')
            self.assertEqual((root / 'old-parent/new-workers/proof').read_text(), 'owned')
            self.assertEqual(list(source.iterdir()), [])

    def test_evaluation_work_root_swap_cannot_write_to_source(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source, work = root / 'source', root / 'workers'
            source.mkdir()
            (source / 'canary').write_text('unchanged')
            mapping = root / 'map.json'
            mapping.write_text(json.dumps({'corpora': [{'source': str(source)}]}))
            with analysis.worker_directory(mapping, work) as pinned:
                work.rename(root / 'original-workers')
                work.symlink_to(source, target_is_directory=True)
                (pinned / 'lifecycle-proof').mkdir()
                self.assertTrue((root / 'original-workers/lifecycle-proof').is_dir())
                self.assertFalse((source / 'lifecycle-proof').exists())
            self.assertEqual((source / 'canary').read_text(), 'unchanged')
            with self.assertRaisesRegex(ValueError, 'outside source'):
                with analysis.worker_directory(mapping, work):
                    pass

    def test_screen_rejects_malformed_records_and_boolean_schema(self):
        decision = json.loads((ROOT / analysis.INPUTS / 'engine-decisions.json').read_text())
        malformed = [[], dict(decision, input_identity=[]), dict(decision, sources=[])]
        with patch.object(analysis, 'frozen_inputs', return_value=({}, decision['input_identity'])):
            for value in malformed:
                with self.subTest(value=type(value).__name__), patch.object(analysis, 'read_json', return_value=(value, 'digest')):
                    with self.assertRaises(ValueError):
                        analysis.screen_engines()
        original = analysis.read_json
        def altered(source, path):
            return (dict(decision, schema_version=True), 'digest') if path.endswith('engine-decisions.json') else original(source, path)
        with patch.object(analysis, 'read_json', side_effect=altered):
            self.assertEqual(analysis.screen_engines()['status'], 'failed')

    def test_missing_backend_replaces_primary_and_aggregate_status(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            path = 'evaluations/results/code-understanding/engine.json'
            write_result(root, path, {'source_identity': 'key', 'tasks': {'T004': {'status': 'passed'},
                         'T005': {'status': 'passed'}}}, 4096)
            with (patch.object(analysis, 'ROOT', root), patch.object(analysis, 'component',
                    side_effect=native.BackendUnavailable('Missing analysis backend')),
                    redirect_stdout(io.StringIO())):
                self.assertEqual(analysis.main(['--engine', 'tree-sitter']), 2)
            report = json.loads((root / path).read_text())
            self.assertEqual(report['tasks']['T005']['status'], 'blocked')
            self.assertEqual(json.loads((root / analysis.DEFAULT_OUTPUT).read_text())['status'], 'blocked')

    def test_task_reporting_preserves_freeze_and_never_selects_engine(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            freeze = {'status': 'passed', 'source_identity': 'frozen-key', 'case_results': [{'id': 'freeze'}]}
            path = 'evaluations/results/code-understanding/engine.json'
            write_result(root, path, {'tasks': {'T004': freeze}, 'source_identity': 'frozen-key'}, 4096)
            result = {'status': 'passed', 'input_identity': {'fixture': 'frozen-key'},
                      'case_results': [{'id': 'direct', 'status': 'passed'}],
                      'coverage_failures': [{'id': 'receiver', 'status': 'failed'}]}
            record_task(root, 'T005', result, 'native-component.json', 4096)
            report = json.loads((root / path).read_text())
            self.assertEqual(report['tasks']['T004'], freeze)
            self.assertEqual(report['tasks']['T005']['coverage_failures'], result['coverage_failures'])
            self.assertEqual(report['status'], 'in_progress')
            self.assertFalse(report['engine_selected'])
            self.assertFalse(report['qualification_complete'])

    def test_missing_backend_is_explicit_and_no_install(self):
        with patch.object(native.metadata, 'version', side_effect=native.metadata.PackageNotFoundError('tree-sitter')):
            with self.assertRaisesRegex(native.BackendUnavailable, 'Missing analysis backend'):
                native.backend()

    def test_unqualified_version_is_rejected(self):
        with patch.object(native.metadata, 'version', return_value='999.0.0'):
            with self.assertRaisesRegex(native.BackendUnavailable, 'versions differ'):
                native.backend()

    def test_budgets_require_positive_values(self):
        with self.assertRaises(ValueError):
            native.Budget(max_nodes=0)

    def test_output_rejects_ancestor_and_leaf_symlinks(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root, outside = Path(root), Path(outside)
            (outside / 'result.json').write_text('outside-canary')
            (root / 'linked').symlink_to(outside, target_is_directory=True)
            (root / 'result.json').symlink_to(outside / 'result.json')
            for path in ('linked/result.json', 'result.json'):
                with self.assertRaises(OSError):
                    write_result(root, path, {'safe': True}, 1024)
            self.assertEqual((outside / 'result.json').read_text(), 'outside-canary')
            write_result(root, 'valid/result.json', {'safe': True}, 1024)
            self.assertEqual(json.loads((root / 'valid/result.json').read_text()), {'safe': True})

    def test_output_budget_leaves_existing_result_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'result.json'
            target.write_text('previous')
            with self.assertRaisesRegex(ValueError, 'output budget'):
                write_result(root, 'result.json', {'large': 'x' * 100}, 10)
            self.assertEqual(target.read_text(), 'previous')

    def test_span_uses_point_tuple_interface_without_getters(self):
        raw = b'\n' * 300 + b'target\nnext\n'
        # Tuples have no .row/.column getters. Also exercise a half-open end
        # at a newline boundary and the source signature terminator extension.
        node = SimpleNamespace(start_byte=300, end_byte=307,
                               start_point=(300, 0), end_point=(301, 0))
        result = native.span(raw, node)
        self.assertEqual(result, {'start_byte': 300, 'end_byte': 307,
                                 'start_line': 301, 'end_line': 301})
        self.assertEqual(native.span(raw, node, end=308)['end_line'], 302)


@unittest.skipUnless(AVAILABLE, 'Optional analysis extra is not installed')
class ExtractionTests(unittest.TestCase):
    def test_parameter_shadowing_never_binds_to_global_function(self):
        sources = {
            'python': 'def local():\n    return 1\ndef caller(local):\n    return local()\n',
            'go': 'package p\nfunc local() int { return 1 }\nfunc caller(local func() int) int { return local() }\n',
            'javascript': 'function local(){return 1;} function caller(local){return local();}',
            'typescript': 'function local():number{return 1;} function caller(local:()=>number):number{return local();}',
        }
        for language, source in sources.items():
            with self.subTest(language=language):
                result = native.extract([blob(source, language)])
                self.assertEqual(result['status'], 'complete')
                call = site(result, 'local()')
                self.assertEqual(call['certainty'], 'unresolved')
                self.assertEqual(call['targets'], [])
                self.assertIn('parameter', call['reason'])

    def test_reassigned_alias_remains_unknown(self):
        sources = {
            'python': 'def local():\n    return 1\ndef caller(other):\n    step = local\n    step = other\n    return step()\n',
            'go': 'package p\nfunc local() int {return 1}\nfunc caller(other func() int) int {step:=local;step=other;return step()}\n',
            'javascript': 'function local(){return 1;} function caller(other){let step=local; step=other; return step();}',
            'typescript': 'function local():number{return 1;} function caller(other:()=>number):number{let step=local; step=other; return step();}',
        }
        for language, source in sources.items():
            with self.subTest(language=language):
                result = native.extract([blob(source, language)])
                self.assertEqual(site(result, 'step()')['certainty'], 'unresolved')

    def test_conditional_python_definition_does_not_gain_exact_target(self):
        result = native.extract([blob('if enabled:\n    def local():\n        return 1\ndef caller():\n    return local()\n')])
        self.assertEqual(site(result, 'local()')['certainty'], 'unresolved')

    def test_local_python_import_shadows_known_global(self):
        result = native.extract([blob('def local():\n    return 1\ndef caller():\n    from absent import local\n    return local()\n')])
        self.assertEqual(site(result, 'local()')['certainty'], 'unresolved')

    def test_python_decorators_and_explicit_nonlocal_mutations_are_unknown(self):
        sources = [
            '@replace\ndef local():\n    return 1\ndef caller():\n    return local()\n',
            'def local():\n    return 1\ndef replace(other):\n    global local\n    local = other\ndef caller():\n    return local()\n',
            'def outer():\n    def local():\n        return 1\n    def replace(other):\n        nonlocal local\n        local = other\n    return local()\n',
        ]
        for source in sources:
            with self.subTest(source=source.splitlines()[0]):
                result = native.extract([blob(source)])
                self.assertEqual(site(result, 'local()')['certainty'], 'unresolved')

    def test_javascript_var_shadows_global_through_function_scope(self):
        result = native.extract([blob('function local(){return 1;} function caller(){if(flag){var local=other;} return local();}', 'javascript')])
        self.assertEqual(site(result, 'local()')['certainty'], 'unresolved')

    def test_block_shadowing_and_outer_binding_are_distinct(self):
        source = 'function local(){return 1;} function caller(){ {const local=()=>2; local();} return local();}'
        result = native.extract([blob(source, 'javascript')])
        calls = [item for item in result['facts']['sites'] if item['role'] == 'call' and item['text'] == 'local()']
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item['certainty'] == 'resolved' for item in calls))
        self.assertNotEqual(calls[0]['targets'], calls[1]['targets'])

    def test_parse_failure_withholds_exact_bindings_and_is_observable(self):
        result = native.extract([blob('def local():\n    return 1\ndef caller():\n    return local()\n! broken [\n')])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['inventory'][0]['status'], 'partial_parse')
        self.assertTrue(result['inventory'][0]['parse_errors'])
        self.assertEqual(site(result, 'local()')['certainty'], 'unresolved')

    def test_go_call_conversion_ambiguity_is_retained(self):
        result = native.extract([blob('package p\nfunc run(table map[string]func(int)int, name string)int{return table[name](1)}\n', 'go')])
        call = site(result, 'table[name](1)')
        self.assertEqual(call['certainty'], 'unresolved')
        self.assertEqual(call['syntax_role'], 'call_or_conversion')
        self.assertIn('type conversion', call['reason'])

    def test_import_module_selection_precedes_export_matching(self):
        sources = [
            blob('import { finish } from "./helpers"; export function imported(){return finish();}', 'typescript', 'main.ts'),
            blob('export function other(){return 1;}', 'typescript', 'helpers.ts'),
            blob('export function finish(){return 2;}', 'typescript', 'helpers.tsx'),
        ]
        result = native.extract(sources)
        call = site(result, 'finish()')
        self.assertEqual(call['certainty'], 'unresolved')
        self.assertIn('multiple inventoried module paths', call['reason'])
        control = native.extract([sources[0], sources[2]])
        self.assertEqual(site(control, 'finish()')['certainty'], 'resolved')

    def test_python_file_package_ambiguity_does_not_choose_matching_export(self):
        sources = [
            blob('from .helpers import finish\ndef imported():\n    return finish()\n', path='package/main.py'),
            blob('def other():\n    return 1\n', path='package/helpers.py'),
            blob('def finish():\n    return 2\n', path='package/helpers/__init__.py'),
        ]
        self.assertEqual(site(native.extract(sources), 'finish()')['certainty'], 'unresolved')

    def test_unicode_source_ranges_roundtrip_original_bytes(self):
        content = '# café λ 🌱\ndef café():\n    return 1\ndef caller():\n    return café()\n'
        result = native.extract([blob(content)])
        raw = content.encode('utf-8')
        for record in result['facts']['definitions'] + result['facts']['sites']:
            start, end = record['range']['start_byte'], record['range']['end_byte']
            self.assertEqual(raw[start:end].decode(), record['text'])
            self.assertEqual(record['range']['start_line'], raw[:start].count(b'\n') + 1)
            self.assertEqual(record['range']['end_line'], raw[:end - 1].count(b'\n') + 1)
        self.assertEqual(site(result, 'café()')['certainty'], 'resolved')

    def test_many_large_row_numbers_survive_collection_and_roundtrip(self):
        import gc
        sources = [blob('\n' * 300 + f'def target_{i}():\n    return 1\n', path=f'file_{i}.py') for i in range(32)]
        for _ in range(3):
            result = native.extract(sources)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(len(result['facts']['definitions']), 32)
            for definition in result['facts']['definitions']:
                self.assertEqual(definition['range']['start_line'], 301)
                self.assertEqual(definition['range']['end_line'], 302)
            gc.collect()

    def test_large_native_rows_and_columns_survive_finite_subprocess(self):
        # Isolate native heap corruption from the test runner. The original
        # Point.row getter fails on integers outside CPython's small-int cache.
        code = '''import faulthandler, gc, json
faulthandler.enable(all_threads=True)
from evaluations.tree_sitter_baseline import extract
raw = ("\\n" * 300 + "def target():\\n    return " + " " * 300 + "1\\n").encode()
for iteration in range(100):
    result = extract([{"path": "large.py", "language": "python", "content": raw}])
    assert result["status"] == "complete", result["status"]
    definition = result["facts"]["definitions"][0]
    assert definition["range"]["start_line"] == 301
    assert definition["range"]["end_line"] == 302
    assert raw[definition["range"]["start_byte"]:definition["range"]["end_byte"]].decode() == definition["text"]
    if iteration % 5 == 0: gc.collect()
print(json.dumps({"iterations": 100, "status": "complete"}))
'''
        completed = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertEqual(json.loads(completed.stdout), {'iterations': 100, 'status': 'complete'})

    def test_callable_value_reference_is_separate_from_call(self):
        result = native.extract([blob('def local():\n    return 1\ndef caller():\n    step = local\n    return step()\n')])
        references = [item for item in result['facts']['sites'] if item['text'] == 'local']
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['role'], 'reference')
        self.assertEqual(site(result, 'step()')['role'], 'call')

    def test_guarded_inventory_preserves_errors_and_valid_control(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root, outside = Path(root), Path(outside)
            (outside / 'helpers.py').write_text('def outside_canary():\n    return 1\n')
            (outside / 'go.mod').write_text('module outside-canary\n')
            (root / 'linked').symlink_to(outside, target_is_directory=True)
            (root / 'valid.py').write_text('def valid():\n    return 1\n')
            (root / 'bad.py').write_bytes(b'\xff')
            paths = ['linked/helpers.py', 'linked/go.mod', 'valid.py', 'missing.py', 'bad.py', '../escape.py']
            result = native.scan(root, paths)
            self.assertEqual([item['path'] for item in result['inventory']], paths)
            self.assertEqual(result['inventory'][2]['status'], 'parsed')
            self.assertTrue(all(item['status'] == 'source_error' for i, item in enumerate(result['inventory']) if i != 2))
            self.assertEqual([item['name'] for item in result['facts']['definitions']], ['valid'])
            self.assertNotIn('outside_canary', json.dumps(result))

    def test_ancestor_swap_during_guarded_open_does_not_expose_bytes(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root, outside = Path(root), Path(outside)
            (root / 'package').mkdir()
            (root / 'package' / 'helpers.py').write_text('def in_root():\n    return 1\n')
            (outside / 'helpers.py').write_text('def outside_canary():\n    return 1\n')
            original_open = os.open
            swapped = False
            def swap(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == 'package' and kwargs.get('dir_fd') is not None and not swapped:
                    swapped = True
                    (root / 'package').rename(root / 'moved')
                    (root / 'package').symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)
            with patch.object(os, 'open', side_effect=swap):
                result = native.scan(root, ['package/helpers.py'])
            self.assertTrue(swapped)
            self.assertEqual(result['inventory'][0]['status'], 'source_error')
            self.assertEqual(result['facts']['definitions'], [])
            self.assertNotIn('outside_canary', json.dumps(result))

    def test_hash_mismatch_and_duplicate_inventory_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'valid.py').write_text('def valid():\n    return 1\n')
            result = native.scan(root, [{'path': 'valid.py', 'sha256': '0' * 64}])
            self.assertEqual(result['inventory'][0]['status'], 'source_error')
            self.assertEqual(result['facts']['definitions'], [])
            with self.assertRaisesRegex(ValueError, 'unique'):
                native.scan(root, ['valid.py', 'valid.py'])

    def test_cancel_and_node_budget_preserve_inventory_without_false_success(self):
        sources = [blob('def local():\n    return 1\n', path='a.py'), blob('def second():\n    return 2\n', path='b.py')]
        result = native.extract(sources, cancel=lambda: True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['inventory']), 2)
        self.assertEqual(result['facts']['definitions'], [])
        result = native.extract(sources, budget=native.Budget(max_nodes=1))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'node_budget_exceeded')
        self.assertLessEqual(result['resources']['nodes_visited'], 2)
        self.assertEqual(len(result['inventory']), 2)

    def test_file_and_byte_budget_do_not_read_excluded_source(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'a.py').write_text('def first():\n    return 1\n')
            (root / 'b.py').write_text('def excluded_canary():\n    return 2\n')
            result = native.scan(root, ['a.py', 'b.py'], budget=native.Budget(max_files=1))
            self.assertEqual(result['inventory'][1]['status'], 'file_budget_exceeded')
            self.assertNotIn('excluded_canary', json.dumps(result))
            result = native.scan(root, ['a.py'], budget=native.Budget(max_file_bytes=4))
            self.assertEqual(result['inventory'][0]['status'], 'source_byte_budget_exceeded')
            self.assertEqual(result['facts']['definitions'], [])

    def test_grader_does_not_influence_source_extraction(self):
        source = blob('def local():\n    return 1\ndef caller():\n    return local()\n')
        before = native.extract([source])
        definition = before['facts']['definitions'][0]
        call = site(before, 'local()')
        fixture = {'definitions': [dict(definition, id='TRUTH.local')], 'cases': [dict(call, id='TRUTH.call', targets=['wrong-target'], construct='direct.python')]}
        _, outcomes = grade(before['facts'], fixture)
        self.assertEqual(outcomes[0]['status'], 'failed')
        after = native.extract([source])
        self.assertEqual(before['facts'], after['facts'])


if __name__ == '__main__':
    unittest.main()
