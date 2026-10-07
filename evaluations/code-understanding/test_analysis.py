"""Boundary and uncertainty checks for the optional native comparison baseline."""
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
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
from evaluations import analysis, real_calls
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
    def test_required_comparison_command_uses_configured_evidence_root(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            environment = {'REPO_GRAPH_EVAL_SOURCE_MAP': str(directory / 'source-map.json'),
                           'REPO_GRAPH_EVAL_WORK_ROOT': str(directory / 'evidence')}
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(analysis, 'compare_component', return_value={'status': 'passed'}) as compare, \
                    patch.object(analysis, 'write_result', return_value=0), \
                    patch.object(analysis, 'record_task'), redirect_stdout(io.StringIO()):
                self.assertEqual(analysis.main(['--compare', '--suite', 'component']), 0)
            compare.assert_called_once_with(directory / 'source-map.json', directory / 'evidence', None)

    def test_source_git_admission_is_read_only_serial_and_preserves_failure_stage(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            revision = 'a' * 40
            responses = [SimpleNamespace(stdout=os.fsencode(root) + b'\n'),
                         SimpleNamespace(stdout=(revision + '\n').encode()),
                         SimpleNamespace(stdout=b'')]
            with patch.object(real_calls.subprocess, 'run', side_effect=responses) as invoked:
                receipt = real_calls.checkout_identity(root, revision)
            self.assertEqual(receipt['status'], 'verified')
            for call in invoked.call_args_list:
                self.assertIn('core.preloadIndex=false', call.args[0])
                self.assertIn('index.threads=1', call.args[0])
                self.assertEqual(call.kwargs['env']['GIT_OPTIONAL_LOCKS'], '0')
                self.assertTrue(call.kwargs['pass_fds'])
            failed = responses[:2] + [subprocess.CalledProcessError(128, 'synthetic', stderr=b'SYNTHETIC_PRIVATE')]
            with patch.object(real_calls.subprocess, 'run', side_effect=failed):
                receipt = real_calls.checkout_identity(root, revision)
            self.assertEqual(receipt, {'status': 'checkout_identity_unavailable',
                'stage': 'git_status', 'error_kind': 'CalledProcessError', 'returncode': 128})
            self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(receipt))
            with real_calls.SourceRoot(root) as owned:
                with patch.object(real_calls.subprocess, 'run', return_value=SimpleNamespace(returncode=128, stdout=b'')):
                    receipt = real_calls.revision_blob_identity(owned, revision, 'tiny.go', b'package tiny\n')
                self.assertEqual(receipt, {'status': 'unavailable', 'stage': 'git_revision_blob',
                    'error_kind': 'CalledProcessError', 'returncode': 128})
                with patch.object(real_calls.subprocess, 'run', side_effect=subprocess.TimeoutExpired('synthetic', 20)):
                    receipt = real_calls.revision_blob_identity(owned, revision, 'tiny.go', b'package tiny\n')
                self.assertEqual(receipt['error_kind'], 'TimeoutExpired')
                self.assertIsNone(receipt['returncode'])

    def test_go_filename_policy_is_pinned_and_does_not_guess_platforms(self):
        names = {'api_client.go': 'neutral', 'api_op_GetItem.go': 'neutral',
                 'x_unknownplatform.go': 'neutral', 'linux.go': 'neutral',
                 'x_unix.go': 'neutral', 'x_linux.go': 'platform_variant',
                 'x_amd64.go': 'platform_variant', 'x_linux_amd64.go': 'platform_variant',
                 'x_linux.extra.go': 'platform_variant', 'x_ios.go': 'platform_variant',
                 'x_android.go': 'platform_variant', 'x_illumos.go': 'platform_variant',
                 'x_linux_test.go': 'test_file', 'x_test.go': 'test_file',
                 '_ignored.go': 'ignored_filename', '.ignored.go': 'ignored_filename',
                 '_ignored.s': 'ignored_filename', 'x.c': 'not_go_source'}
        for name, expected in names.items():
            with self.subTest(name=name):
                self.assertEqual(native.go_filename_class('pkg/' + name), expected)
        self.assertEqual(native.GO_FILENAME_POLICY['version'], 'go1.24.0-build-neutral-v1')
        self.assertEqual(native.GO_FILENAME_POLICY['source_revision'], '3901409b5d0fb7c85a3e6730a59943cc93b2835c')
        self.assertIn('unqualified', native.GO_FILENAME_POLICY['platform_selection'])
        self.assertIn('no future-platform inference', native.GO_FILENAME_POLICY['unknown_suffix'])

    def test_profile_archive_keeps_measured_code_and_bounds_failure_export(self):
        from evaluations.acceptance import PINS as CORPUS_PINS
        import hashlib
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            mapping = root / 'map.json'
            from evaluations.performance import IMPLEMENTATION_PATHS, LARGE_CORPORA
            mapping.write_text(json.dumps({'corpora': [{'id': name, 'source': str(root / name),
                'revision': CORPUS_PINS[name]} for name in LARGE_CORPORA]}))
            measured = {'commit': 'a' * 40, 'sha256': {path: hashlib.sha256(b'archived implementation').hexdigest()
                for path in IMPLEMENTATION_PATHS}, 'root_identity': '0' * 64}
            implementation = dict(measured, native_backend=dict(native.PINS))
            rows = [{'corpus': name, 'revision': CORPUS_PINS[name], 'engine': engine, 'repeat': run,
                     'exit_code': 1, 'identity_verified': True, 'implementation_after': measured,
                     'checkout_before': {'status': 'verified', 'actual_revision': CORPUS_PINS[name], 'clean': True, 'root_identity': '0' * 64},
                     'checkout_after': {'status': 'verified', 'actual_revision': CORPUS_PINS[name], 'clean': True, 'root_identity': '0' * 64},
                     'worker_wall_seconds': 1, 'stdout_sha256': '0' * 64, 'stderr_sha256': '0' * 64,
                     'result': {'records': [], 'error_kind': 'synthetic-negative'}}
                    for name in ('django', 'odoo', 'aws', 'kubernetes')
                    for engine in ('current-map', 'tree-sitter') for run in range(3)]
            rows[0]['result']['records'] = [{'run': 'fresh-output', 'status': 'partial', 'wall_seconds': 1,
                'artifact_bytes': 1, 'peak_rss_bytes': 1, 'stages': {}, 'source_reads': {}, 'counts': {},
                'semantic_facts_sha256': '0' * 64, 'input_inventory_sha256': '0' * 64, 'coverage': {'files': [
                {'path': 'failure.py', 'status': 'not_processed_after_deadline_exceeded', 'details': 'synthetic ' * 131072}]}}]
            archive = {'schema_version': 1, 'source_map_sha256': hashlib.sha256(mapping.read_bytes()).hexdigest(),
                       'corpus_revisions': {name: CORPUS_PINS[name] for name in ('django', 'odoo', 'aws', 'kubernetes')},
                       'implementation': implementation, 'environment': {'python': 'synthetic', 'platform': 'synthetic',
                        'cpu_count': 1, 'gpu_used': False}, 'records': rows}
            path = root / 'profile.json'
            path.write_text(json.dumps(archive))
            with patch.object(analysis.subprocess, 'check_output', return_value=b'archived implementation'):
                result = analysis.profile_component(mapping, recorded_report=path)
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(len(result['case_results']), 24)
            self.assertEqual(result['implementation']['commit'], 'a' * 40)
            self.assertTrue(result['reporting_implementation']['from_archive'])
            self.assertNotEqual(result['implementation']['sha256']['evaluations/analysis.py'],
                                result['reporting_implementation']['analysis_sha256'])
            self.assertLess(len(json.dumps(result)), 65536)
            self.assertEqual(result['case_results'][0]['result']['records'][0]['failed_files'],
                             [{'path': 'failure.py', 'status': 'not_processed_after_deadline_exceeded'}])
            with patch.object(analysis.subprocess, 'check_output', return_value=b'wrong archived code'):
                with self.assertRaisesRegex(ValueError, 'recorded commit'):
                    analysis.profile_component(mapping, recorded_report=path)
            import copy
            malformed = [lambda a: a['implementation'].update(sha256={}),
                lambda a: a['records'][0].update(identity_verified='false'),
                lambda a: a['records'][0].update(worker_wall_seconds=-1),
                lambda a: a['records'][0].update(stdout_sha256='invalid'),
                lambda a: a['records'][0].pop('checkout_before'),
                lambda a: a['records'][0]['checkout_after'].update(clean=False),
                lambda a: a['records'][0]['result']['records'][0]['coverage']['files'][0].update(path='C:\\synthetic-private\\file.py'),
                lambda a: a['records'][0]['result'].update(raw_source='synthetic private text')]
            with patch.object(analysis.subprocess, 'check_output', return_value=b'archived implementation'):
                for edit in malformed:
                    value = copy.deepcopy(archive)
                    edit(value)
                    path.write_text(json.dumps(value))
                    with self.assertRaises((ValueError, KeyError)):
                        analysis.profile_component(mapping, recorded_report=path)
            path.write_text(json.dumps(archive).replace('"worker_wall_seconds": 1', '"worker_wall_seconds": 1e999'))
            with self.assertRaisesRegex(ValueError, 'Non-finite'):
                analysis.profile_component(mapping, recorded_report=path)
            mapping.write_text('{"corpora":[]}')
            with self.assertRaisesRegex(ValueError, 'Required pinned'):
                analysis.profile_component(mapping, recorded_report=path)
            mapping.write_text(json.dumps({'corpora': [{'id': name, 'source': str(root / name),
                'revision': CORPUS_PINS[name]} for name in LARGE_CORPORA]}))
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                analysis.profile_component(mapping, recorded_report=path)

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
    def registered_compact(self, blobs, context):
        configurations, collected = {}, []
        for supplied in blobs:
            if supplied['kind'] == 'configuration':
                configurations[supplied['path']] = supplied['content']
                continue
            source = {key: supplied[key] for key in ('path', 'language', 'content', 'kind', 'sha256', 'bytes')}
            file = native.collect_file(source)
            encoded = file.to_json()
            file = native.CollectedFile.from_json(encoded, file.record, hashlib.sha256(encoded).hexdigest())
            self.assertEqual(file.to_json(), encoded)
            self.assertEqual(set(file.record), {'path', 'language', 'sha256', 'bytes', 'kind'})
            self.assertFalse(hasattr(file, 'tree'))
            self.assertFalse(hasattr(file, 'raw'))
            self.assertTrue(all('targets' not in fact for fact, _, _ in file.candidates))
            collected.append(file)
        return collected, configurations

    def test_registered_go_compact_round_trip_has_one_resolver_and_no_owner_mutation(self):
        blobs, context = self.registered_go(alias='dep', package='different')
        collected, configurations = self.registered_compact(blobs, context)
        before = [file.to_json() for file in collected]
        expected = native.extract(blobs, go_context=context)['facts']
        for _ in range(2):
            actual = native.resolve_collected(collected, configurations, go_context=context)
            self.assertEqual(actual['facts'], expected)
            self.assertEqual([file.to_json() for file in collected], before)
            call = next(item for item in actual['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call')
            self.assertEqual(call['certainty'], 'resolved')
            target = next(item for item in actual['facts']['definitions'] if item['id'] == call['targets'][0])
            self.assertEqual((target['repository_id'], target['revision'], target['path']), ('dependency', 'c' * 40, 'pkg/main.go'))
            target['provenance']['repository_id'] = 'output_mutation'
            self.assertEqual([file.to_json() for file in collected], before)

    def test_registered_go_alias_metadata_is_typed_and_default_package_is_conservative(self):
        from copy import deepcopy
        for alias, package, certainty in [('dep', 'different', 'resolved'), ('', 'pkg', 'resolved'), ('', 'different', 'unresolved')]:
            blobs, context = self.registered_go(alias=alias, package=package)
            collected, configurations = self.registered_compact(blobs, context)
            caller = next(file for file in collected if file.path == native.snapshot_path('caller', 'a' * 40, 'main.go'))
            self.assertIs(caller.imports[0]['explicit_alias'], bool(alias))
            result = native.resolve_collected(collected, configurations, go_context=context)
            call = next(item for item in result['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call')
            self.assertEqual(call['certainty'], certainty)
            for malformed in (None, 1, 'true'):
                payload = deepcopy(caller.payload())
                payload['imports'][0]['explicit_alias'] = malformed
                encoded = json.dumps(payload).encode()
                with self.assertRaises(ValueError):
                    native.CollectedFile.from_json(encoded, caller.record, hashlib.sha256(encoded).hexdigest())
            payload = deepcopy(caller.payload())
            payload['imports'][0]['explicit_alias'] = not bool(alias)
            encoded = json.dumps(payload).encode()
            with self.assertRaisesRegex(ValueError, 'import'):
                native.CollectedFile.from_json(encoded, caller.record, hashlib.sha256(encoded).hexdigest())

    def test_registered_go_compact_control_flags_withhold_bodyless_cgo_and_directives(self):
        from copy import deepcopy
        baseline, source_context = self.registered_go()
        variants = [
            (b'package pkg\nfunc Target() int\n', 'go_bodyless_function', 'bodyless'),
            (b'package pkg\nimport "C"\nfunc Target() int {return 1}\n', 'go_cgo_import', 'CGO'),
            (b'//go:build arbitrary\npackage pkg\nfunc Target() int {return 1}\n', 'go_control_directive', 'directive')]
        for raw, flag, reason in variants:
            blobs, context = deepcopy(baseline), deepcopy(source_context)
            provider = next(item for item in blobs if item['repository_id'] == 'dependency' and item['kind'] == 'source')
            provider.update(content=raw, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            next(item for item in context['packages'] if item['repository_id'] == 'dependency')['files'][0].update(bytes=provider['bytes'], sha256=provider['sha256'])
            collected, configurations = self.registered_compact(blobs, context)
            self.assertTrue(next(file for file in collected if file.path == provider['path']).syntax_metadata[flag])
            result = native.resolve_collected(collected, configurations, go_context=context)
            call = next(item for item in result['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call')
            self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
            self.assertIn(reason, call['reason'])
            self.assertEqual(result['facts'], native.extract(blobs, go_context=context)['facts'])

    def test_registered_go_exact_pseudo_versions_remain_unqualified_after_handoff(self):
        from copy import deepcopy
        baseline, source_context = self.registered_go()
        for version in ('v2.0.0-20250101120000-abcdef123456', 'v2.0.0-0.20250101120000-abcdef123456', 'v2.0.0-beta.0.20250101120000-abcdef123456'):
            blobs, context = deepcopy(baseline), deepcopy(source_context)
            context['dependencies'][0]['version'] = version
            control = next(item for item in blobs if item['repository_id'] == 'caller' and item['kind'] == 'configuration')
            raw = b'module example.test/caller\nrequire example.test/lib/v2 ' + version.encode() + b'\n'
            control.update(content=raw, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            collected, configurations = self.registered_compact(blobs, context)
            result = native.resolve_collected(collected, configurations, go_context=context)
            call = next(item for item in result['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call')
            self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
            self.assertIn('No unique qualified dependency', call['reason'])
            self.assertEqual(result['facts'], native.extract(blobs, go_context=context)['facts'])

    def registered_go(self, alias='dep', package='pkg'):
        pins = {'caller': 'a' * 40, 'other': 'b' * 40, 'dependency': 'c' * 40}
        contents = {
            ('caller', 'go.mod'): b'module example.test/caller\nrequire example.test/lib/v2 v2.0.0\n',
            ('other', 'go.mod'): b'module example.test/other\nrequire example.test/lib/v2 v2.0.0\n',
            ('dependency', 'go.mod'): b'module example.test/lib/v2\n',
            ('dependency', 'pkg/main.go'): f'package {package}\nfunc Target() int {{return 1}}\n'.encode(),
        }
        for owner in ('caller', 'other'):
            binding = (alias + ' ') if alias else ''
            name = alias or 'pkg'
            contents[owner, 'main.go'] = (f'package caller\nimport {binding}"example.test/lib/v2/pkg"\n'
                f'func Call() int {{return {name}.Target()}}\n'
                f'func Shadow({name} interface{{}}) int {{return {name}.Target()}}\n').encode()
        blobs, packages = [], []
        for (owner, path), raw in contents.items():
            blobs.append({'path': native.snapshot_path(owner, pins[owner], path), 'physical_path': path,
                'repository_id': owner, 'revision': pins[owner], 'language': 'go' if path.endswith('.go') else 'unknown',
                'content': raw, 'kind': 'source' if path.endswith('.go') else 'configuration',
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
            if path.endswith('.go'):
                packages.append({'repository_id': owner, 'revision': pins[owner], 'directory': str(PurePosixPath(path).parent) if '/' in path else '',
                    'files': [{'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}]})
        registration = {'consumer_repository_id': 'caller', 'consumer_revision': pins['caller'],
            'dependency_repository_id': 'dependency', 'dependency_revision': pins['dependency'],
            'module_path': 'example.test/lib/v2', 'version': 'v2.0.0'}
        context = {'policy': 'declared_snapshot_only', 'dependencies': [registration], 'packages': packages,
            'controls': [{'repository_id': owner, 'revision': pin, 'qualified': True} for owner, pin in pins.items()]}
        return blobs, context

    def test_registered_go_preserves_domain_ownership_and_shadowing(self):
        blobs, context = self.registered_go()
        result = native.extract(blobs, go_context=context)
        self.assertEqual(result['status'], 'complete')
        calls = [item for item in result['facts']['sites'] if item['role'] == 'call']
        owned = [item for item in calls if item['repository_id'] == 'caller']
        self.assertEqual([item['certainty'] for item in owned], ['resolved', 'unresolved'])
        target = next(item for item in result['facts']['definitions'] if item['id'] == owned[0]['targets'][0])
        self.assertEqual((target['repository_id'], target['revision'], target['path']), ('dependency', 'c' * 40, 'pkg/main.go'))
        self.assertEqual(target['provenance']['repository_id'], 'dependency')
        self.assertTrue(target['id'].startswith(native.snapshot_path('dependency', 'c' * 40, 'pkg/main.go') + ':'))
        self.assertTrue(all(item['certainty'] == 'unresolved' for item in calls if item['repository_id'] == 'other'))
        self.assertNotEqual(owned[0]['id'], next(item['id'] for item in calls if item['repository_id'] == 'other'))
        self.assertIn('active build and MVS unqualified', result['limits']['go_dependency_policy'])

    def test_registered_go_ordinary_underscores_keep_actual_ownership(self):
        from copy import deepcopy
        baseline, source_context = self.registered_go()
        for name in ('api_client.go', 'api_op_GetItem.go', 'api_unknownplatform.go'):
            blobs, context = deepcopy(baseline), deepcopy(source_context)
            provider = next(item for item in blobs if item['repository_id'] == 'dependency' and item['kind'] == 'source')
            physical = 'pkg/' + name
            provider.update(path=native.snapshot_path('dependency', 'c' * 40, physical), physical_path=physical)
            next(item for item in context['packages'] if item['repository_id'] == 'dependency')['files'][0]['path'] = physical
            with self.subTest(name=name):
                result = native.extract(blobs, go_context=context)
                call = next(item for item in result['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call')
                self.assertEqual(call['certainty'], 'resolved')
                definition = next(item for item in result['facts']['definitions'] if item['id'] == call['targets'][0])
                self.assertEqual((definition['repository_id'], definition['revision'], definition['path']), ('dependency', 'c' * 40, physical))
                self.assertEqual(result['limits']['go_filename_policy'], native.GO_FILENAME_POLICY)

    def test_registered_go_platform_and_directive_variants_stay_unknown(self):
        from copy import deepcopy
        baseline, source_context = self.registered_go()
        variants = [('x_linux.go', b'', 'GOOS/GOARCH'), ('x_amd64.go', b'', 'GOOS/GOARCH'),
                    ('x_linux_amd64.go', b'', 'GOOS/GOARCH'),
                    ('api_client.go', b'//go:build special\n', 'directive'),
                    ('api_client.go', b'// +build special\n', 'directive'),
                    ('api_client.go', b'//go:generate unknown\n', 'directive')]
        for name, prefix, reason in variants:
            blobs, context = deepcopy(baseline), deepcopy(source_context)
            provider = next(item for item in blobs if item['repository_id'] == 'dependency' and item['kind'] == 'source')
            physical = 'pkg/' + name
            raw = prefix + provider['content']
            provider.update(path=native.snapshot_path('dependency', 'c' * 40, physical), physical_path=physical,
                            content=raw, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            next(item for item in context['packages'] if item['repository_id'] == 'dependency')['files'][0].update(
                path=physical, bytes=provider['bytes'], sha256=provider['sha256'])
            with self.subTest(name=name, prefix=prefix):
                call = next(item for item in native.extract(blobs, go_context=context)['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call')
                self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
                self.assertIn(reason, call['reason'])

    def test_registered_go_ignored_blobs_never_emit_structural_facts(self):
        blobs, context = self.registered_go()
        for path in ('pkg/_ignored.go', 'pkg/.ignored.go', 'pkg/main_test.go'):
            raw = b'\xff ignored bytes are not Go package input'
            blobs.append({'path': native.snapshot_path('dependency', 'c' * 40, path), 'physical_path': path,
                'repository_id': 'dependency', 'revision': 'c' * 40, 'language': 'go', 'content': raw,
                'kind': 'source', 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
        result = native.extract(blobs, go_context=context)
        self.assertEqual(result['status'], 'complete')
        excluded = [item for item in result['inventory'] if item['status'] == 'go_filename_excluded']
        self.assertEqual(len(excluded), 3)
        self.assertTrue(all(item['filename_policy'] == native.GO_FILENAME_POLICY['version'] for item in excluded))
        self.assertFalse(any(native.go_filename_class(item['path']) in ('ignored_filename', 'test_file') for item in result['facts']['definitions'] + result['facts']['sites']))
        self.assertEqual(next(item['certainty'] for item in result['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call'), 'resolved')

    def test_registered_go_manifest_cannot_count_ignored_file_as_active(self):
        blobs, context = self.registered_go()
        manifest = next(item for item in context['packages'] if item['repository_id'] == 'dependency')
        for name in ('_ignored.go', '.ignored.go', 'main_test.go'):
            with self.subTest(name=name), patch.object(native, 'backend') as backend:
                manifest['files'][0]['path'] = 'pkg/' + name
                with self.assertRaisesRegex(ValueError, 'eligible non-test'):
                    native.extract(blobs, go_context=context)
                backend.assert_not_called()

    def test_registered_go_rejects_namespace_duplicates_before_collection(self):
        from copy import deepcopy
        blobs, context = self.registered_go()
        duplicate = deepcopy(blobs) + [dict(blobs[0])]
        with patch.object(native, 'backend') as backend:
            with self.assertRaisesRegex(ValueError, 'duplicate snapshot'):
                native.extract(duplicate, go_context=context)
            backend.assert_not_called()
        for field, value in [('physical_path', 'pkg/../main.go'), ('path', 'main.go'), ('sha256', '0' * 64), ('targets', ['gold'])]:
            bad = deepcopy(blobs)
            bad[0][field] = value
            with patch.object(native, 'backend') as backend:
                with self.assertRaises((ValueError, OSError)):
                    native.extract(bad, go_context=context)
                backend.assert_not_called()
        context['dependencies'].append(dict(context['dependencies'][0]))
        with patch.object(native, 'backend') as backend:
            with self.assertRaisesRegex(ValueError, 'Duplicate dependency'):
                native.extract(blobs, go_context=context)
            backend.assert_not_called()

    def test_registered_go_unqualified_variants_and_package_ambiguity(self):
        from copy import deepcopy
        baseline, source_context = self.registered_go()
        for variant in ('version', 'revision', 'replace', 'workspace', 'incomplete', 'bytes', 'build', 'duplicate_binding'):
            blobs, context = deepcopy(baseline), deepcopy(source_context)
            provider = next(item for item in blobs if item['repository_id'] == 'dependency' and item['kind'] == 'source')
            manifest = next(item for item in context['packages'] if item['repository_id'] == 'dependency')
            if variant == 'version': context['dependencies'][0]['version'] = 'v2.0.1'
            elif variant == 'revision': context['dependencies'][0]['dependency_revision'] = 'd' * 40
            elif variant == 'replace':
                item = next(item for item in blobs if item['repository_id'] == 'caller' and item['kind'] == 'configuration')
                item['content'] += b'replace example.test/lib/v2 => ../foreign\n'
                item.update(bytes=len(item['content']), sha256=hashlib.sha256(item['content']).hexdigest())
            elif variant == 'workspace': context['controls'][0]['qualified'] = False
            elif variant == 'incomplete': manifest['files'] = []
            elif variant == 'bytes': manifest['files'][0]['sha256'] = '0' * 64
            elif variant in ('build', 'duplicate_binding'):
                provider['content'] += b'//go:build special\n' if variant == 'build' else b'func Target() int {return 2}\n'
                provider.update(bytes=len(provider['content']), sha256=hashlib.sha256(provider['content']).hexdigest())
                manifest['files'][0].update(bytes=provider['bytes'], sha256=provider['sha256'])
            with self.subTest(variant=variant):
                calls = [item for item in native.extract(blobs, go_context=context)['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call']
                self.assertTrue(calls)
                self.assertTrue(all(item['certainty'] == 'unresolved' and not item['targets'] for item in calls))
        # Explicit aliases are lexical source declarations. The basename cannot
        # stand in for a different declared default package name.
        blobs, context = self.registered_go(alias='', package='different')
        self.assertTrue(all(item['certainty'] == 'unresolved' for item in native.extract(blobs, go_context=context)['facts']['sites']))
        blobs, context = self.registered_go(alias='dep', package='different')
        self.assertEqual(next(item['certainty'] for item in native.extract(blobs, go_context=context)['facts']['sites'] if item['repository_id'] == 'caller' and item['role'] == 'call'), 'resolved')

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




    def change_go_source(self, blobs, context, owner, path, raw):
        pin = next(x['revision'] for x in context['controls'] if x['repository_id'] == owner)
        item = next((x for x in blobs if x['repository_id'] == owner and x['physical_path'] == path), None)
        if item is None:
            item = {'path': native.snapshot_path(owner, pin, path), 'physical_path': path,
                    'repository_id': owner, 'revision': pin, 'language': 'go' if path.endswith('.go') else 'unknown',
                    'kind': 'source' if path.endswith('.go') else 'configuration'}
            blobs.append(item)
        item.update(content=raw, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if path.endswith('.go'):
            directory = str(PurePosixPath(path).parent) if '/' in path else ''
            manifest = next((x for x in context['packages'] if x['repository_id'] == owner and x['directory'] == directory), None)
            if manifest is None:
                manifest = {'repository_id': owner, 'revision': pin, 'directory': directory, 'files': []}
                context['packages'].append(manifest)
            receipt = next((x for x in manifest['files'] if x['path'] == path), None)
            if receipt is None:
                receipt = {'path': path}
                manifest['files'].append(receipt)
            receipt.update(bytes=len(raw), sha256=item['sha256'])

    def differentiated_go(self):
        blobs, context = self.registered_go()
        for control in context['controls']:
            control['namespace_qualified'] = control['qualified']
        self.change_go_source(blobs, context, 'caller', 'main.go',
            b'package caller\nimport dep "example.test/lib/v2/pkg"\nimport own "example.test/caller/own"\n'
            b'func Local() int{return 2}\nfunc Direct() int{return Local()}\n'
            b'func Alias() int{return own.Target()}\nfunc External() int{return dep.Target()}\n')
        self.change_go_source(blobs, context, 'caller', 'own/main.go', b'package own\nfunc Target() int{return 3}\n')
        return blobs, context

    def go_call_certainties(self, blobs, context):
        result = native.extract(blobs, go_context=context)
        calls = [x for x in result['facts']['sites'] if x['repository_id'] == 'caller' and x['role'] == 'call']
        return {x['text']: x for x in calls}

    def test_declared_go_legacy_false_and_typed_namespace_controls(self):
        blobs, context = self.differentiated_go()
        control = context['controls'][0]
        control.pop('namespace_qualified')
        control['qualified'] = False
        self.assertTrue(all(x['certainty'] == 'unresolved' for x in self.go_call_certainties(blobs, context).values()))
        for field, value in [('namespace_qualified', 1), ('namespace_qualified', 'true'), ('qualified', 1)]:
            with self.subTest(field=field, value=value):
                control.update(qualified=True, namespace_qualified=True)
                control[field] = value
                with self.assertRaisesRegex(ValueError, 'source-control metadata'):
                    native.extract(blobs, go_context=context)

    def test_declared_go_external_false_preserves_owned_namespace_only(self):
        blobs, context = self.differentiated_go()
        context['controls'][0]['qualified'] = False
        calls = self.go_call_certainties(blobs, context)
        for text in ('Local()', 'own.Target()'):
            self.assertEqual(calls[text]['certainty'], 'resolved')
            self.assertIn('explicit declared snapshot', calls[text]['reason'])
            self.assertEqual(calls[text]['provenance']['binding_scope'], 'declared_snapshot_only')
            self.assertFalse(calls[text]['provenance']['active_build_qualified'])
        self.assertEqual((calls['dep.Target()']['certainty'], calls['dep.Target()']['targets']), ('unresolved', []))

    def test_declared_go_replacements_keep_local_namespace_and_refuse_external(self):
        for module in ('example.test/unrelated', 'example.test/lib/v2'):
            blobs, context = self.differentiated_go()
            self.change_go_source(blobs, context, 'caller', 'go.mod',
                ('module example.test/caller\nrequire example.test/lib/v2 v2.0.0\nreplace '+module+' => ../foreign\n').encode())
            calls = self.go_call_certainties(blobs, context)
            self.assertEqual(calls['Local()']['certainty'], 'resolved')
            self.assertEqual(calls['own.Target()']['certainty'], 'resolved')
            self.assertEqual(calls['dep.Target()']['certainty'], 'unresolved')

    def test_declared_go_provider_environment_does_not_mask_hard_failure(self):
        for namespace, expected in ((True, 'resolved'), (False, 'unresolved')):
            blobs, context = self.differentiated_go()
            control = next(x for x in context['controls'] if x['repository_id'] == 'dependency')
            control.update(qualified=False, namespace_qualified=namespace)
            self.assertEqual(self.go_call_certainties(blobs, context)['dep.Target()']['certainty'], expected)

    def test_declared_go_own_import_refuses_required_or_registered_overlap(self):
        for required, registered in ((True, False), (False, True), (True, True)):
            blobs, context = self.differentiated_go()
            if required:
                self.change_go_source(blobs, context, 'caller', 'go.mod',
                    b'module example.test/caller\nrequire example.test/lib/v2 v2.0.0\nrequire example.test/caller/own v1.0.0\n')
            if registered:
                entry = dict(context['dependencies'][0], module_path='example.test/caller/own', version='v1.0.0')
                context['dependencies'].append(entry)
            call = self.go_call_certainties(blobs, context)['own.Target()']
            self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
            self.assertIn('Overlapping', call['reason'])

    def test_declared_go_external_provider_ambiguity_stays_unknown(self):
        blobs, context = self.differentiated_go()
        self.change_go_source(blobs, context, 'other', 'go.mod', b'module example.test/lib/v2\n')
        context['dependencies'].append(dict(context['dependencies'][0], dependency_repository_id='other', dependency_revision='b' * 40))
        call = self.go_call_certainties(blobs, context)['dep.Target()']
        self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))

    def test_declared_go_hard_namespace_failure_and_package_ambiguity_stay_unknown(self):
        blobs, context = self.differentiated_go()
        context['controls'][0].update(qualified=False, namespace_qualified=False)
        self.assertTrue(all(x['certainty'] == 'unresolved' for x in self.go_call_certainties(blobs, context).values()))
        blobs, context = self.differentiated_go()
        self.change_go_source(blobs, context, 'caller', 'extra.go', b'package caller\nfunc Local() int{return 9}\n')
        self.assertEqual(self.go_call_certainties(blobs, context)['Local()']['certainty'], 'unresolved')
        blobs, context = self.differentiated_go()
        duplicate = dict(blobs[-1])
        with self.assertRaisesRegex(ValueError, 'duplicate snapshot'):
            native.extract(blobs + [duplicate], go_context=context)

    def test_declared_go_module_control_subset_is_not_line_skipping(self):
        work = lambda: native.Work(native.Budget(), None)
        good = b'module example.test/m\ngodebug (\n panicnil=1\n default=go1.27\n)\nreplace (\n example.test/one => ../one\n example.test/two v1.0.0 => example.test/new v1.2.0\n)\n'
        parsed = native.go_module(good, work())
        self.assertEqual(parsed['runtime_controls'], {'panicnil': '1', 'default': 'go1.27'})
        self.assertEqual(len(parsed['replacements']), 2)
        bad = [b'module x\nmodule y\n', b'module x\ngodebug (\npanicnil=1\n',
               b'module x\ngodebug (\nrequire (\n)\n', b'module x\ngodebug panicnil\n',
               b'module x\ngodebug panicnil=1 extra\n', b'module x\ngodebug panicnil="1"\n',
               b'module x\ngodebug panicnil=1\ngodebug panicnil=0\n', b'module x\nreplace a =>\n',
               b'module x\nreplace a ../a\n', b'module x\nreplace a => ../a extra\n',
               b'module x\nreplace a => "../a"\n', b'module x\nreplace a => ../a\nreplace a => ../b\n',
               b'module x\nunknown opaque\n', b'module x\ngo nonsense\n', b'module x\nrequire a "v1.0.0"\n']
        bad.extend([b'module x\nrequire a vbogus\n', b'module x\nreplace a v1 => ../a\n'])
        for raw in bad:
            with self.subTest(raw=raw):
                self.assertIsNone(native.go_module(raw, work()))

    def test_declared_go_godebug_records_runtime_uncertainty_not_build_success(self):
        blobs, context = self.differentiated_go()
        self.change_go_source(blobs, context, 'caller', 'go.mod',
            b'module example.test/caller\ngodebug tlsmlkem=0\nrequire example.test/lib/v2 v2.0.0\n')
        calls = self.go_call_certainties(blobs, context)
        self.assertEqual(calls['Local()']['certainty'], 'resolved')
        self.assertFalse(calls['Local()']['provenance']['runtime_qualified'])
        self.assertFalse(calls['Local()']['provenance']['mvs_qualified'])


@unittest.skipUnless(AVAILABLE, 'optional pinned analysis backend not installed')
class DeclaredGoSourceTests(unittest.TestCase):
    def go_extract(self, scratch, variant=None):
        PIN = 'a' * 40
        from evaluations.tree_sitter_baseline import extract
        contents = {'left': {'go.mod': b'module example.test/left\nrequire example.test/lib/v2 v2.0.0\n',
                            'main.go': b'package p\nimport dep "example.test/lib/v2/pkg"\nfunc Call() int {return dep.Target()}\n'},
                    'right': {'go.mod': b'module example.test/lib/v2\n',
                              'pkg/main.go': b'package pkg\nfunc Target() int {return 1}\n',
                              'pkg/extra.go': b'package pkg\nfunc Other() int {return 2}\n',
                              'pkg/extra_test.go': b'package pkg\nfunc Target() int {return 3}\n'}}
        provider_path = 'pkg/main.go'
        if variant == 'ordinary_underscores':
            provider_path = 'pkg/api_client.go'
            contents['right'][provider_path] = contents['right'].pop('pkg/main.go')
            contents['right']['pkg/api_op_GetItem.go'] = contents['right'].pop('pkg/extra.go')
        elif variant == 'platform_filename':
            provider_path = 'pkg/api_client_linux.go'
            contents['right'][provider_path] = contents['right'].pop('pkg/main.go')
        elif variant == 'ignored':
            contents['right'].update({'pkg/_ignored.go': b'\xff ignored source',
                                     'pkg/.ignored.go': b'\xff ignored source',
                                     'pkg/_ignored.s': b'ignored native build input'})
        elif variant == 'git_ignored_source':
            contents['right']['.gitignore'] = b'/pkg/hidden.go\n'
        roots, selected = {}, {}
        for repository, files in contents.items():
            root = Path(scratch) / repository
            root.mkdir()
            roots[repository] = {'id': repository, 'source': str(root), 'revision': PIN}
            for path, raw in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(raw)
            paths = ['main.go'] if repository == 'left' else [provider_path]
            selected[repository] = [{'path': path, 'revision': PIN, 'language': 'go', 'kind': 'source',
                'sha256': hashlib.sha256(files[path]).hexdigest(), 'bytes': len(files[path])} for path in paths]
        registration = {'consumer_repository_id': 'left', 'consumer_revision': PIN,
            'dependency_repository_id': 'right', 'dependency_revision': PIN,
            'module_path': 'example.test/lib/v2', 'version': 'v2.0.0'}
        if variant == 'symlink':
            outside = Path(scratch) / 'outside.go'
            outside.write_bytes(b'package pkg\nfunc Canary() int {return 7}\n')
            (Path(roots['right']['source']) / 'pkg/unsafe.go').symlink_to(outside)
        elif variant == 'ignored':
            outside = Path(scratch) / 'outside.go'
            outside.write_bytes(b'package pkg\nfunc Canary() int {return 7}\n')
            ignored = Path(roots['right']['source']) / 'pkg/_ignored.go'
            ignored.unlink()
            ignored.symlink_to(outside)
        elif variant == 'workspace':
            (Path(roots['left']['source']) / 'go.work').write_text('go 1.23\nuse ../right\n')
        elif variant == 'vendor':
            vendor = Path(roots['left']['source']) / 'vendor'
            vendor.mkdir()
            (vendor / 'modules.txt').write_text('# example.test/lib/v2 v2.0.0\n')
        elif variant in ('provider_workspace', 'provider_invalid_control', 'provider_nested'):
            target = Path(roots['right']['source']) / ('pkg/go.mod' if variant == 'provider_nested' else 'go.work')
            target.write_bytes(b'\xff invalid control' if variant == 'provider_invalid_control' else b'module example.test/nested\n' if variant == 'provider_nested' else b'go 1.23\nuse .\n')
        elif variant == 'duplicate_binding':
            (Path(roots['right']['source']) / 'pkg/extra.go').write_bytes(b'package pkg\nfunc Target() int {return 2}\n')
        elif variant == 'assembly':
            (Path(roots['right']['source']) / 'pkg/extra.s').write_bytes(b'private synthetic unsupported build input\n')
        git_env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull}
        for repository, entry in roots.items():
            git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                   '-c', 'commit.gpgSign=false', '-c', 'user.name=Fixture',
                   '-c', 'user.email=fixture@example.test']
            for args in (['init', '--quiet'], ['add', '.'], ['commit', '--quiet', '-m', 'Synthetic source']):
                subprocess.run(git + args, cwd=entry['source'], env=git_env,
                               capture_output=True, check=True, timeout=5)
            pin = subprocess.check_output(git + ['rev-parse', 'HEAD'], cwd=entry['source'],
                                          env=git_env, text=True, timeout=5).strip()
            entry['revision'] = pin
            for item in selected[repository]:
                item['revision'] = pin
        registration.update(consumer_revision=roots['left']['revision'], dependency_revision=roots['right']['revision'])
        if variant == 'git_ignored_source':
            (Path(roots['right']['source']) / 'pkg/hidden.go').write_bytes(b'package pkg\nfunc Canary() int {return 7}\n')
        with patch.object(real_calls, 'extract', wraps=extract) as observed, \
                patch.object(real_calls, 'scan') as legacy:
            runs, raw = real_calls.extract_selected(selected, roots, native.Budget(), [registration])
        legacy.assert_not_called()
        observed.assert_called_once()
        for blob in observed.call_args.args[0]:
            self.assertEqual(set(blob), {'path', 'physical_path', 'repository_id', 'revision', 'language',
                                        'kind', 'content', 'sha256', 'bytes'})
        return runs, raw, contents, roots, selected, registration


    def test_ignored_discovered_source_cannot_acquire_revision_ownership(self):
        with tempfile.TemporaryDirectory() as scratch:
            runs, raw, _, roots, _, _ = self.go_extract(scratch, 'git_ignored_source')
            self.assertEqual(real_calls.checkout_identity(Path(roots['right']['source']), roots['right']['revision'])['status'], 'verified')
            self.assertFalse(runs['right']['source_control']['qualified'])
            rejected = next(item for item in runs['right']['verification'] if item['path'] == 'pkg/hidden.go')
            self.assertEqual(rejected['status'], 'source_read_error')
            self.assertNotIn(('right', 'pkg/hidden.go'), raw)
            call = next(item for item in runs['left']['facts']['sites'] if item['role'] == 'call')
            self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
            with real_calls.SourceRoot(Path(roots['right']['source'])) as source:
                original = (source.root / 'pkg/main.go').read_bytes()
                binding = real_calls.revision_blob_identity(source, roots['right']['revision'], 'pkg/main.go', original)
                self.assertEqual(binding['status'], 'verified')
                with self.assertRaisesRegex(ValueError, 'differ'):
                    real_calls.revision_blob_identity(source, roots['right']['revision'], 'pkg/main.go', original + b'\n')
            # Git must not give a subdirectory the repository root's relative paths.
            nested = Path(roots['right']['source']) / 'pkg'
            self.assertEqual(real_calls.checkout_identity(nested, roots['right']['revision'])['status'],
                             'source_root_not_git_top_level')


    def test_registered_controller_fails_closed_on_package_and_control_variants(self):
        for variant in ('symlink', 'workspace', 'vendor', 'duplicate_binding', 'assembly'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as scratch:
                runs, raw, _, _, _, _ = self.go_extract(scratch, variant)
                sites = [item for item in runs['left']['facts']['sites'] if item['role'] == 'call']
                self.assertEqual(len(sites), 1)
                self.assertEqual((sites[0]['certainty'], sites[0]['targets']), ('unresolved', []))
                self.assertNotIn(b'Canary', b''.join(raw.values()))
                self.assertNotIn(scratch, json.dumps(runs))
        with tempfile.TemporaryDirectory() as scratch:
            _, _, _, roots, selected, registration = self.go_extract(scratch)
            roots['right']['source'] = roots['left']['source']
            with patch.object(real_calls, 'extract') as observed:
                with self.assertRaisesRegex(ValueError, 'Duplicate physical'):
                    real_calls.extract_selected(selected, roots, native.Budget(), [registration])
                observed.assert_not_called()


    def test_registered_controller_shared_filename_policy_and_exclusion_receipts(self):
        for variant in ('ordinary_underscores', 'ignored'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as scratch:
                runs, raw, _, _, _, _ = self.go_extract(scratch, variant)
                call = next(item for item in runs['left']['facts']['sites'] if item['role'] == 'call')
                self.assertEqual(call['certainty'], 'resolved')
                self.assertTrue(runs['right']['source_control']['qualified'])
                definition = next(item for item in runs['right']['facts']['definitions'] if item['id'] == call['targets'][0])
                self.assertEqual(definition['repository_id'], 'right')
                if variant == 'ordinary_underscores':
                    self.assertEqual(definition['path'], 'pkg/api_client.go')
                    self.assertIn('pkg/api_op_GetItem.go', {item['path'] for item in runs['right']['inventory']})
                else:
                    excluded = {item['path']: item for item in runs['right']['filename_exclusions']}
                    self.assertEqual(set(excluded), {'pkg/_ignored.go', 'pkg/.ignored.go', 'pkg/_ignored.s', 'pkg/extra_test.go'})
                    self.assertTrue(all(item['status'] == 'go_filename_excluded' and
                        item['filename_policy'] == 'go1.24.0-build-neutral-v1' for item in excluded.values()))
                    self.assertFalse(any(path[1] in excluded for path in raw))
                    self.assertNotIn(b'Canary', b''.join(raw.values()))
                self.assertNotIn(scratch, json.dumps(runs))


    def test_registered_controller_platform_filename_stays_unknown(self):
        with tempfile.TemporaryDirectory() as scratch:
            runs, _, _, _, _, _ = self.go_extract(scratch, 'platform_filename')
            call = next(item for item in runs['left']['facts']['sites'] if item['role'] == 'call')
            self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
            self.assertIn('GOOS/GOARCH', call['reason'])

    def test_differentiated_controller_provider_environment_and_hard_boundary(self):
        for variant, namespace in [('provider_workspace', True), ('provider_nested', False), ('provider_invalid_control', False)]:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as scratch:
                runs, _, _, _, _, _ = self.go_extract(scratch, variant)
                control = runs['right']['source_control']
                self.assertEqual(control['namespace_qualified'], namespace)
                self.assertFalse(control['qualified'])
                call = next(x for x in runs['left']['facts']['sites'] if x['role'] == 'call')
                self.assertEqual(call['certainty'], 'resolved' if namespace else 'unresolved')
                receipts = runs['right']['source_control_evidence']
                self.assertTrue(any(x['status'] == 'verified' for x in receipts) if namespace else
                                any(x['status'] == 'source_read_error' or x.get('reason') == 'nested_module_namespace_unqualified' for x in receipts))

    def test_differentiated_controller_consumer_environment_retains_control_bytes(self):
        for variant in ('workspace', 'vendor'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as scratch:
                runs, _, _, _, _, _ = self.go_extract(scratch, variant)
                self.assertTrue(runs['left']['source_control']['namespace_qualified'])
                self.assertFalse(runs['left']['source_control']['qualified'])
                call = next(x for x in runs['left']['facts']['sites'] if x['role'] == 'call')
                self.assertEqual((call['certainty'], call['targets']), ('unresolved', []))
                receipt = next(x for x in runs['left']['source_control_evidence'] if x['status'] == 'verified')
                self.assertEqual(len(receipt['sha256']), 64)
                self.assertEqual(receipt['revision_blob']['status'], 'verified')

    def test_differentiated_controller_control_read_failure_never_becomes_absence(self):
        original = real_calls.SourceRoot.read
        def missing(owned, path, *args, **kwargs):
            if path == 'go.work':
                raise FileNotFoundError('synthetic disappeared control after info')
            return original(owned, path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as scratch, patch.object(real_calls.SourceRoot, 'read', missing):
            runs, _, _, _, _, _ = self.go_extract(scratch, 'provider_workspace')
        self.assertFalse(runs['right']['source_control']['namespace_qualified'])
        receipt = next(x for x in runs['right']['source_control_evidence'] if x['path'] == 'go.work')
        self.assertEqual(receipt['status'], 'source_read_error')

    def test_differentiated_controller_control_actual_bytes_stay_capped(self):
        original = real_calls.SourceRoot.read
        def grown(owned, path, *args, **kwargs):
            if path == 'go.work':
                raw = b'x' * (native.Budget().max_file_bytes + 1)
                return raw, hashlib.sha256(raw).hexdigest(), SimpleNamespace(st_size=len(raw))
            return original(owned, path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as scratch, patch.object(real_calls.SourceRoot, 'read', grown):
            runs, _, _, _, _, _ = self.go_extract(scratch, 'provider_workspace')
        self.assertFalse(runs['right']['source_control']['namespace_qualified'])
        self.assertEqual(next(x for x in runs['right']['source_control_evidence'] if x['path'] == 'go.work')['status'], 'source_read_error')


if __name__ == '__main__':
    unittest.main()
