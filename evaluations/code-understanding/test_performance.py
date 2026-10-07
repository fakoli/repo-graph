"""Finite worker observations; no engine-selection or scale acceptance claims."""
import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evaluations import performance
from evaluations import real_calls
from evaluations.acceptance import PINS


class ObservedProfile(unittest.TestCase):
    def test_native_scan_output_swap_cannot_overwrite_source(self):
        from evaluations import tree_sitter_baseline as native
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-swap-') as scratch:
            root = Path(scratch)
            source, output = root / 'source', root / 'output'
            source.mkdir()
            output.mkdir()
            canary = source / 'native-facts.json'
            canary.write_text('PRIVATE_TEST_SOURCE')
            def swapped_scan(*args, **kwargs):
                output.rename(root / 'original-output')
                output.symlink_to(source, target_is_directory=True)
                return {'facts': {'definitions': [], 'sites': []}}
            captured = io.StringIO()
            with patch.object(native, 'scan', swapped_scan), redirect_stdout(captured):
                code = performance.structural_worker(['tree-sitter', str(source), str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(captured.getvalue())['error_kind'], 'ValueError')
            self.assertEqual(canary.read_text(), 'PRIVATE_TEST_SOURCE')

    def test_worker_rejects_graph_symlink_and_retains_truncation(self):
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-read-') as scratch:
            root = Path(scratch)
            source = root / 'source'
            source.mkdir()
            canary = source / 'private.json'
            canary.write_text('{"PRIVATE_TEST_SOURCE":true}')
            (source / 'a.py').write_text('PRIVATE_TEST_SOURCE' + ' ' * performance.builder.READ_LIMIT)
            graph = {'file_count': 1, 'files': ['a.py'], 'tree': [], 'dependencies': [],
                     'scope_edges': [], 'system': {}, 'search': {'documents': 1},
                     'scan': {'code_files': 1, 'failed': 0, 'truncated': 1, 'scanned': 1, 'reused': 0}}
            for symlink in (True, False):
                output = root / str(symlink)
                def fake_map(argv):
                    output.mkdir(exist_ok=True)
                    with performance.SourceRoot(source) as owner:
                        owner.read('a.py', performance.builder.READ_LIMIT)
                    if symlink:
                        (output / 'graph.json').symlink_to(canary)
                    else:
                        (output / 'graph.json').write_text(json.dumps(graph))
                    (output / 'scan-cache.json').write_text(json.dumps({'files': {'a.py': {'digest': 'a' * 64}}}))
                captured = io.StringIO()
                with patch.object(performance.builder, 'main', fake_map), redirect_stdout(captured):
                    code = performance.structural_worker(['current-map', str(source), str(output)])
                report = json.loads(captured.getvalue())
                with self.subTest(symlink=symlink):
                    self.assertEqual(code, 1 if symlink else 0)
                    if symlink:
                        self.assertEqual(report['error_kind'], 'OSError')
                        self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())
                    else:
                        self.assertTrue(all(r['status'] == 'partial' and r['coverage']['truncated'] == 1
                                            for r in report['records']))
                        for record in report['records']:
                            receipt = record['coverage']['files'][0]
                            self.assertEqual(receipt['path'], 'a.py')
                            self.assertEqual(receipt['status'], 'truncated')
                            self.assertTrue(receipt['reads']['inventory']['prefix_truncated'])
                        self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())
                self.assertEqual(canary.read_text(), '{"PRIVATE_TEST_SOURCE":true}')

    def test_atomic_report_preserves_source_canary_and_failed_workers(self):
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-boundary-') as scratch:
            root = Path(scratch)
            canary = root / 'source-canary'
            canary.write_text('PRIVATE_TEST_SOURCE')
            report = root / 'reports/profile.json'
            corpora = []
            for name in ('django', 'odoo', 'aws', 'kubernetes'):
                source = root / name
                source.mkdir()
                corpora.append({'id': name, 'source': str(source), 'revision': PINS[name]})
            config = root / 'sources.json'
            corpora.append({'id': 'sdk', 'source': str(root / 'unused-sdk'), 'revision': 'b' * 40})
            config.write_text(json.dumps({'corpora': corpora}))
            class FailedWorker:
                pid = 99999999
                returncode = 2
                def __init__(self, *args, **kwargs):
                    self_check.assertEqual(args[0][1:3], ['-I', '-B'])
                    self_check.assertEqual(set(kwargs['env']), {'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                        'XDG_DATA_HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL', 'TEMP', 'TMP'})
                    self_check.assertTrue(Path(kwargs['env']['HOME']).is_dir())
                    if not report.exists():
                        report.symlink_to(canary)
                def communicate(self, timeout=None):
                    return '{"status":"failed","error_kind":"fixture failure"}', ''
            def revision(argv, cwd, **kwargs):
                return 'a' * 40 + '\n'
            def checkout(source, revision):
                return {'status': 'verified', 'actual_revision': revision, 'clean': True}
            self_check = self
            with patch.object(performance.subprocess, 'Popen', FailedWorker), patch.object(
                    performance.subprocess, 'check_output', side_effect=revision), patch.object(
                    real_calls, 'checkout_identity', side_effect=checkout):
                records = performance.profile_structural(config, report, root / 'worker-logs')
            self.assertEqual(canary.read_text(), 'PRIVATE_TEST_SOURCE')
            self.assertFalse(report.is_symlink())
            self.assertEqual(len(records), 24)
            self.assertTrue(all(r['exit_code'] == 2 for r in records))
            data = json.loads(report.read_text())
            self.assertTrue(data['implementation']['sha256'])
            self.assertTrue(data['implementation']['native_backend'])
            self.assertEqual(set(data['corpus_revisions']), set(performance.LARGE_CORPORA))
            self.assertTrue(all(r['identity_verified'] for r in records))
            self.assertNotIn(str(root), report.read_text())

    def test_map_and_worker_identity_fail_closed(self):
        for change in ('duplicate', 'wrong-type', 'wrong-revision', 'dirty-before', 'dirty-after', 'revision-after',
                       'root-after', 'implementation-after'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(prefix='repo-graph-profile-identity-') as scratch:
                root, started = Path(scratch), []
                corpora = []
                for name in performance.LARGE_CORPORA:
                    source = root / name
                    source.mkdir()
                    corpora.append({'id': name, 'source': str(source), 'revision': PINS[name]})
                if change == 'duplicate':
                    corpora.append(corpora[0])
                elif change == 'wrong-type':
                    corpora[0]['source'] = 42
                elif change == 'wrong-revision':
                    corpora[0]['revision'] = 'f' * 40
                config, report = root / 'sources.json', root / 'reports/profile.json'
                config.write_text(json.dumps({'corpora': corpora}))
                checked = {}
                def checkout(source, revision):
                    checked[source] = checked.get(source, 0) + 1
                    status = 'dirty_checkout' if (change == 'dirty-before' and checked[source] > 1) or (
                        started and change == 'dirty-after') else (
                        'revision_mismatch' if started and change == 'revision-after' else 'verified')
                    return {'status': status, 'actual_revision': 'f' * 40 if status == 'revision_mismatch' else revision,
                            'clean': status == 'verified'}
                class Worker:
                    pid, returncode = 99999999, 0
                    def __init__(self, *args, **kwargs):
                        started.append(True)
                    def communicate(self, timeout=None):
                        if change == 'root-after':
                            source = root / 'django'
                            source.rename(root / 'old-django')
                            source.mkdir()
                        return '{"records":[]}', ''
                original_read = performance.SourceRoot.read
                def read(owner, path, *args, **kwargs):
                    data, sha, info = original_read(owner, path, *args, **kwargs)
                    if started and change == 'implementation-after' and path == 'repo_graph/builder.py':
                        sha = '0' * 64
                    return data, sha, info
                with patch.object(real_calls, 'checkout_identity', side_effect=checkout), patch.object(
                        performance.subprocess, 'Popen', Worker), patch.object(
                        performance.subprocess, 'check_output', return_value='a' * 40 + '\n'), patch.object(
                        performance.SourceRoot, 'read', read):
                    with self.assertRaises(ValueError):
                        performance.profile_structural(config, report, root / 'logs')
                if change.endswith('after'):
                    self.assertEqual(len(started), 1)
                    data = json.loads(report.read_text())
                    self.assertEqual(data['status'], 'invalid_identity')
                    self.assertFalse(data['records'][0]['identity_verified'])
                    self.assertTrue((root / 'logs/django-current-map-0.stdout.log').exists())
                else:
                    self.assertFalse(started)

    def test_partial_file_receipts_keep_metadata_without_source_text(self):
        from evaluations import tree_sitter_baseline as native
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-receipts-') as scratch:
            root = Path(scratch)
            source, output = root / 'source', root / 'output'
            source.mkdir()
            (source / 'partial.py').write_text('PRIVATE_TEST_SOURCE')
            (source / 'unreadable.py').write_text('PRIVATE_TEST_SOURCE')
            (source / 'README.md').write_text('PRIVATE_TEST_SOURCE')
            result = {'facts': {'definitions': [{'text': 'PRIVATE_TEST_SOURCE'}], 'sites': []},
                'inventory': [{'path': 'partial.py', 'status': 'partial_parse', 'bytes': 19, 'sha256': 'a' * 64,
                               'parse_errors': [{'kind': 'missing', 'range': {'start_byte': 2, 'end_byte': 2}}]},
                              {'path': 'unreadable.py', 'status': 'source_error', 'error_kind': 'OSError', 'errno': 13}],
                'resources': {}, 'status': 'partial', 'stop_reason': None,
                'errors': [{'path': 'unreadable.py', 'kind': 'OSError'}]}
            captured = io.StringIO()
            with patch.object(native, 'scan', return_value=result), redirect_stdout(captured):
                code = performance.structural_worker(['tree-sitter', str(source), str(output)])
            self.assertEqual(code, 0)
            data = json.loads(captured.getvalue())
            for record in data['records']:
                coverage = record['coverage']
                files = {item['path']: item for item in coverage['files']}
                self.assertEqual(files['partial.py']['status'], 'partial_parse')
                self.assertTrue(files['partial.py']['parse_errors'])
                self.assertEqual(files['unreadable.py']['errno'], 13)
                self.assertEqual(files['README.md']['status'], 'excluded_non_source')
                self.assertEqual(coverage['errors'], result['errors'])
            self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())
            self.assertNotIn(str(root), captured.getvalue())
            captured = io.StringIO()
            with redirect_stdout(captured):
                code = performance.structural_worker(['current-map', str(source), str(root / 'map-output')])
            self.assertEqual(code, 0)
            for record in json.loads(captured.getvalue())['records']:
                files = {item['path']: item for item in record['coverage']['files']}
                self.assertEqual(record['status'], 'complete')
                self.assertEqual(files['go.mod']['status'], 'absent_optional_configuration')
                self.assertTrue(files['partial.py']['reads'])
            self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())

    def test_worker_records_actual_repeat_and_refuses_source_output(self):
        root = Path(__file__).resolve().parents[2]
        source = root / 'tests/fixtures/code-understanding'
        script = root / 'evaluations/performance.py'
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-check-') as scratch:
            run = subprocess.run([sys.executable, str(script), '--structural-worker',
                'current-map', str(source), str(Path(scratch) / 'output')],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertTrue(report['deterministic_repeat'])
            self.assertEqual(len(report['records']), 2)
            cold, warm = report['records']
            self.assertEqual(cold['counts']['inventoried_files'], 9)
            self.assertEqual(warm['coverage']['scanned'], 0)
            self.assertEqual(warm['coverage']['reused'], 8)
            self.assertGreater(cold['source_reads']['hashed_bytes'], 0)
            self.assertGreater(cold['peak_rss_bytes'], 0)
            self.assertEqual(cold['input_inventory_sha256'], warm['input_inventory_sha256'])
            rejected = subprocess.run([sys.executable, str(script), '--structural-worker',
                'current-map', str(source), str(source / 'FORBIDDEN-PROFILE-OUTPUT')],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(json.loads(rejected.stdout)['error_kind'], 'ValueError')
            self.assertFalse((source / 'FORBIDDEN-PROFILE-OUTPUT').exists())


if __name__ == '__main__':
    unittest.main()
