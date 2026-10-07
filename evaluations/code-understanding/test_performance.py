"""Finite worker observations; no engine-selection or scale acceptance claims."""
import errno
import hashlib
import json
import os
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
            frozen = json.loads((root / 'evaluations/code-understanding/supplement-source.json').read_bytes())['files']
            self.assertEqual(cold['counts']['inventoried_files'], len(frozen))
            self.assertEqual(warm['coverage']['scanned'], 0)
            self.assertEqual(warm['coverage']['reused'], sum(item['kind'] == 'source' for item in frozen))
            self.assertGreater(cold['source_reads']['hashed_bytes'], 0)
            self.assertGreater(cold['peak_rss_bytes'], 0)
            self.assertEqual(cold['input_inventory_sha256'], warm['input_inventory_sha256'])
            rejected = subprocess.run([sys.executable, str(script), '--structural-worker',
                'current-map', str(source), str(source / 'FORBIDDEN-PROFILE-OUTPUT')],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(json.loads(rejected.stdout)['error_kind'], 'ValueError')
            self.assertFalse((source / 'FORBIDDEN-PROFILE-OUTPUT').exists())


PROFILE_ROOT = Path(__file__).resolve().parents[2]


def _fixture_stat(pid=101, start=456, pgid=101, sid=101, comm=b'x (y) z', state=b'S'):
    fields = [state, b'1', str(pgid).encode(), str(sid).encode()] + [b'0'] * 15 + [str(start).encode()] + [b'0'] * 5
    return str(pid).encode() + b' (' + comm + b') ' + b' '.join(fields) + b'\n'
_FIXTURE_CONTROLLER = {'pid': 101, 'starttime_ticks': 456, 'pgid': 101, 'sid': 101}
_FIXTURE_WORKER = {'pid': 202, 'starttime_ticks': 789, 'pgid': 202, 'sid': 202}

class _FakeProcOwner:
    current = {101: 1024, 202: 2048}
    instances = []

    def __init__(self, identity, *, separate_session=False):
        if set(identity) != set(_FIXTURE_CONTROLLER) or any((type(v) is not int or v <= 0 for v in identity.values())) or (separate_session and (not identity['pid'] == identity['pgid'] == identity['sid'])):
            raise ValueError('bad identity')
        self.identity = dict(identity)
        self.closed = False
        self.instances.append(self)

    def recheck(self):
        if self.current.get(self.identity['pid']) == 'stale':
            raise ValueError('changed identity')

    def rss(self):
        self.recheck()
        value = self.current[self.identity['pid']]
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True

def _fixture_event(kind='readiness', worker=None, role=None, cleanup=None, index=None):
    return {'schema_version': 1, 'event': kind, 'monotonic_ns': 10, 'mode': 'queued', 'role': role or ('worker' if worker else 'controller'), 'configured_concurrency': 4, 'workers_started': 1 if worker else 0, 'live_workers': 1 if worker else 0, 'pending_requests': 0, 'inflight_reserved_bytes': 0, 'mailbox_source_bytes': 0, 'mailbox_request_bytes': 0, 'mailbox_result_bytes': 0, 'admitted_bytes': 0, 'controller': dict(_FIXTURE_CONTROLLER), 'worker': worker, 'index': index, 'cleanup': cleanup}

@unittest.skipUnless(sys.platform == 'linux', 'Owned RSS profiling requires Linux')
class OwnedDualProfile(unittest.TestCase):

    def setUp(self):
        _FakeProcOwner.instances = []
        _FakeProcOwner.current = {101: 1024, 202: 2048}

    def sampler(self):
        with patch.object(performance, '_dual_self_identity', return_value=dict(_FIXTURE_CONTROLLER)), patch.object(performance, '_DualProcOwner', _FakeProcOwner):
            return performance._DualSampler()

    def observe(self, sampler, value):
        with patch.object(performance, '_DualProcOwner', _FakeProcOwner):
            return sampler.observe(value)

    def test_proc_stat_keeps_starttime_and_parentheses(self):
        self.assertEqual(performance._dual_proc_stat(_fixture_stat()), _FIXTURE_CONTROLLER)
        for raw in (_fixture_stat(state=b'Z'), _fixture_stat(state=b'bad'), _fixture_stat(start=0), _fixture_stat(pgid=-1), b'1 (x) S', _fixture_stat() + b'x', b'x' * 4097):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                performance._dual_proc_stat(raw)

    def test_rss_is_current_kib_not_peak_pss(self):
        self.assertEqual(performance._dual_proc_rss(b'Rss: 7 kB\nPss: 2 kB\nPrivate_Clean: 1 kB\n'), 7168)
        for raw in (b'Rss: 1 kB\nRss: 2 kB\n', b'Pss: 7 kB\n', b'Rss: -1 kB\n', b'Rss: 1 MB\n', b'Rss: 1e999 kB\n', b'Rss: 99999999999999999999 kB\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                performance._dual_proc_rss(raw)

    def test_small_actual_own_pid_and_descriptor_close(self):
        before = len(list(Path('/proc/self/fd').iterdir()))
        identity = performance._dual_self_identity()
        self.assertEqual(identity['pid'], os.getpid())
        owner = performance._DualProcOwner(identity)
        try:
            self.assertGreater(owner.rss(), 0)
            with self.assertRaises(ValueError):
                owner.read('stat', 1)
            with patch.object(owner, 'read', return_value=_fixture_stat(pid=identity['pid'], start=identity['starttime_ticks'] + 1, pgid=identity['pgid'], sid=identity['sid'])):
                with self.assertRaises(ValueError):
                    owner.recheck()
        finally:
            owner.close()
        self.assertIsNone(owner.fd)
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_missing_read_is_gap_never_zero_sum(self):
        s = self.sampler()
        self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
        _FakeProcOwner.current[202] = OSError(errno.ENOENT, 'gone')
        s.sample()
        self.assertIsNone(s.samples[-1]['owned_rss_bytes'])
        self.assertFalse(s.samples[-1]['complete'])
        self.assertEqual(s.samples[-1]['gaps'][0]['errno'], errno.ENOENT)
        r = s.finish()
        self.assertIsNone(r['peak_sampled_owned_rss_bytes'])
        self.assertIsNotNone(r['error'])

    def test_identity_change_and_unowned_worker_refuse(self):
        s = self.sampler()
        self.assertFalse(self.observe(s, _fixture_event('submit', dict(_FIXTURE_WORKER), index=0)))
        self.assertIsNotNone(s.error)
        s.finish()
        s = self.sampler()
        self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
        _FakeProcOwner.current[202] = 'stale'
        self.assertFalse(self.observe(s, _fixture_event('receive', dict(_FIXTURE_WORKER), index=0)))
        self.assertIsNotNone(s.error)
        s.finish()

    def test_registration_log_is_durable_before_callback_returns(self):
        s = self.sampler()
        with tempfile.TemporaryDirectory(prefix='mock-log-') as temporary:
            s.attach_log(Path(temporary))
            self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
            rows = [json.loads(line) for line in (Path(temporary) / 'owned-telemetry.jsonl').read_bytes().splitlines()]
            self.assertEqual(rows[-1]['kind'], 'event')
            self.assertEqual(rows[-1]['value']['worker'], _FIXTURE_WORKER)
            self.assertEqual(rows[-2]['kind'], 'lifecycle')
            self.assertEqual(rows[-2]['value']['identity'], _FIXTURE_WORKER)
            s.sample()
            s.finish()
            self.assertIsNone(s.log_fd)
            self.assertIsNone(s.log_owner)

    def test_log_setup_and_write_failure_close_owned_handles(self):
        s = self.sampler()
        with tempfile.TemporaryDirectory(prefix='mock-log-failure-') as temporary:
            target = Path(temporary) / 'owned-telemetry.jsonl'
            target.write_bytes(b'canary')
            with self.assertRaises(FileExistsError):
                s.attach_log(Path(temporary))
            self.assertEqual(target.read_bytes(), b'canary')
            self.assertIsNone(s.log_fd)
            self.assertIsNone(s.log_owner)
        s.finish()
        s = self.sampler()
        with tempfile.TemporaryDirectory(prefix='mock-write-failure-') as temporary:
            s.attach_log(Path(temporary))
            with patch.object(performance.os, 'write', side_effect=OSError(errno.ENOSPC, 'mock full')):
                self.assertFalse(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
            self.assertIsNotNone(s.error)
            s.finish()
            self.assertIsNone(s.log_fd)
            self.assertIsNone(s.log_owner)
            self.assertTrue(all((o.closed for o in _FakeProcOwner.instances)))

    def test_source_reset_and_independent_clean_candidates_with_failure_retention(self):
        from evaluations import incremental_candidate as incremental, engine_checks as checks
        raw = b'call(1)'
        path = 'a.py'
        blobs = {path: raw}
        metadata = [{'path': path, 'language': 'python', 'kind': 'source', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]
        edits = {}
        for name, new in (('U-PY-BODY', 'body(1)'), ('U-PY-EXPORT', 'export(1)')):
            edits[name] = {'id': name, 'language': 'python', 'operations': [{'op': 'replace', 'path': path, 'old': 'call(1)', 'new': new, 'occurrences': 1, 'sha256_before': hashlib.sha256(raw).hexdigest(), 'sha256_after': hashlib.sha256(new.encode()).hexdigest()}]}
        calls = []
        created = []

        class MockCandidate:
            failure_at = None

            def __init__(self, source, budget):
                self.root = source
                self.last_attempt = None
                self.snapshot = None
                with performance.SourceRoot(source) as owner:
                    self.owner = owner.identity
                self.number = len(created)
                created.append(self)

            def refresh(self, records, **kwargs):
                with performance.SourceRoot(self.root) as source:
                    data, _, _ = source.read(path, 100, hash_full=False)
                calls.append((self.number, data, kwargs['mode'], kwargs['concurrency']))
                receipt = {'status': 'complete', 'generation': hashlib.sha256(data).hexdigest(), 'source_identity': 'b' * 64, 'semantic_facts_sha256': hashlib.sha256(data).hexdigest()}
                if self.failure_at == len(calls):
                    receipt = {'status': 'failed', 'reason': 'mock late refresh failure'}
                self.last_attempt = receipt
                return receipt

        class MockSampler:

            def __init__(self, supervisor):
                self.error = None

            def attach_log(self, directory):
                self.directory = directory
                with performance.SourceRoot(directory) as owner:
                    with owner.atomic_writer('owned-telemetry.jsonl') as f:
                        f.write(b'{"kind":"mock_no_workers"}\n')
                return self

            def start(self):
                return self

            def set_phase(self, label):
                pass

            def observe(self, event):
                raise AssertionError('no actual queue')

            def finish(self):
                return {'error': None, 'remaining_registered_worker_owners': [], 'samples': [], 'queue_events': [], 'mock_only': True}
        bound = {'mock_binding': 'synthetic only'}

        def run(failure_at=None):
            MockCandidate.failure_at = failure_at
            calls.clear()
            created.clear()
            with tempfile.TemporaryDirectory(prefix='mock-phase-') as temporary:
                with patch.object(performance, '_dual_inputs', return_value=(blobs, metadata, edits)), patch.object(performance, '_dual_recheck', return_value=bound), patch.object(performance, '_DualSampler', MockSampler), patch.object(performance, '_dual_isolation', return_value={'mock_only': True}), patch.object(checks, '_adapter_snapshot_artifact', return_value={'path': 'mock.facts.json', 'sha256': 'a' * 64, 'bytes': 0}), patch.object(incremental, 'Candidate', MockCandidate):
                    report = performance._dual_run(PROFILE_ROOT, Path(temporary), bound, 'queued', 4, 0, None)
                disk = json.loads((Path(temporary) / 'result.json').read_bytes())
                self.assertEqual(disk, report)
                self.assertFalse(report['engine_selected'])
                return (report, list(calls))
        report, seen = run()
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(seen), 8)
        self.assertEqual([c[1] for c in seen], [raw, raw, raw, b'body(1)', b'body(1)', raw, b'export(1)', b'export(1)'])
        self.assertNotEqual(seen[3][0], seen[4][0])
        self.assertNotEqual(seen[6][0], seen[7][0])
        self.assertTrue(all((c[2:] == ('queued', 4) for c in seen)))
        failed, seen = run(6)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(failed['phases']), 6)
        self.assertEqual(failed['phases'][-1]['receipt']['reason'], 'mock late refresh failure')
        self.assertTrue(all((p['status'] == 'complete' for p in failed['phases'][:-1])))

    def test_worker_result_rejects_vacuous_and_typed_false_success(self):
        bound = {key: {} for key in ('implementation', 'input_binding', 'backend', 'queue_identity', 'runtime')}
        bound.update(measured_commit='a' * 40, root_identity='b' * 64)
        report = {'schema_version': 1, 'kind': 'native_dual_fixture', 'status': 'failed', 'mode': 'serial', 'concurrency': 1, 'repeat': 0, 'binding_before': bound, 'phases': [], 'engine_selected': False, 'qualification_complete': False}
        self.assertIs(performance._dual_validate_result(report, bound, 'serial', 1, 0), report)
        for bad in (dict(report, status='complete'), dict(report, concurrency=True), dict(report, phases=[None]), dict(report, engine_selected=True)):
            with self.assertRaises(ValueError):
                performance._dual_validate_result(bad, bound, 'serial', 1, 0)

    def test_private_environment_bridge_and_identity_failure_stop_admission(self):
        from evaluations import engine_checks as checks
        bound = {'mock_binding': 'no extraction'}
        seen = []
        checks_count = [0]

        def recheck(*args):
            checks_count[0] += 1
            if checks_count[0] == 2:
                raise ValueError('mock helper drift after admission failure')
            return bound

        def no_process(command, **kwargs):
            seen.append(command)
            bridge = Path(kwargs['cwd'])
            self.assertEqual(bridge.parent, Path('/proc') / str(os.getpid()) / 'fd')
            fd = kwargs['pass_fds'][0]
            self.assertEqual(bridge.name, str(fd))
            for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'TMPDIR'):
                self.assertEqual(Path(kwargs['env'][key]).parent, bridge)
                self.assertEqual(Path(kwargs['env'][key]).resolve().parent, bridge.resolve())
            self.assertEqual(command[1:3], ['-I', '-B'])
            self.assertTrue(kwargs['start_new_session'])
            raise OSError(errno.EIO, 'mock Popen admission refusal; no process exists')
        with tempfile.TemporaryDirectory(prefix='mock-environment-') as temporary:
            with patch.object(performance, '_dual_supervisor_limits', return_value={'mock_only': True}), patch.object(checks, '_adapter_root', side_effect=lambda root: Path(root)), patch.object(performance, '_dual_capture', return_value=bound), patch.object(performance, '_dual_recheck', side_effect=recheck), patch.object(performance.subprocess, 'Popen', side_effect=no_process):
                result = performance.profile_native_dual(PROFILE_ROOT, Path(temporary))
            self.assertEqual(len(seen), 1)
            self.assertEqual(result['status'], 'invalid_identity')
            case = result['full_private_report']['cases'][0]
            self.assertEqual(case['status'], 'invalid_identity')
            self.assertFalse(case['identity_verified'])
            self.assertIsNone(case['cleanup'])
            self.assertEqual(case['failure']['error_kind'], 'OSError')
            self.assertTrue(case['logs'])

    def test_blocked_sampler_failure_does_not_wait_on_its_lock(self):
        s = self.sampler()

        class MockBlocked:
            ident = 1

            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return True
        s.thread = MockBlocked()

        class ForbiddenLock:

            def __enter__(self):
                raise AssertionError('must not block on sampler lock')
        s.lock = ForbiddenLock()
        receipt = s.finish()
        self.assertFalse(receipt['sampler_stopped'])
        self.assertIsNotNone(receipt['error'])
        self.assertIsNone(receipt['peak_sampled_owned_rss_bytes'])
        self.assertEqual(s.thread.timeout, 2)
        for held in s.owners.values():
            held.close()

    def test_locked_manifest_source_edits_admitted_without_gold(self):
        from evaluations import engine_checks as checks
        from evaluations.supplement_preparation import SOURCE
        raw = (PROFILE_ROOT / SOURCE).read_bytes()
        bound = {'input_binding': {SOURCE: hashlib.sha256(raw).hexdigest()}}
        blobs, rows, edits = performance._dual_inputs(PROFILE_ROOT, bound)
        self.assertEqual(len(rows), 24)
        self.assertEqual(sum(map(len, blobs.values())), 11463)
        self.assertEqual(sum((row['kind'] == 'source' for row in rows)), 16)
        self.assertEqual(sum((row['kind'] == 'configuration' for row in rows)), 8)
        self.assertEqual(set(edits), {'U-PY-BODY', 'U-PY-EXPORT'})
        for update in edits.values():
            self.assertEqual(set(update), {'id', 'language', 'operations'})
            changed, after = performance._dual_mutation(blobs, rows, update)
            self.assertEqual(sum((blobs[path] != changed[path] for path in blobs)), 1)
            self.assertEqual(sum((a['sha256'] != b['sha256'] for a, b in zip(rows, after))), 1)
        self.assertNotIn('expected', json.dumps(rows))

    def test_supervisor_interactive_refusal_precedes_limit_changes(self):
        import resource
        with patch.object(performance, '_dual_self_identity', return_value=dict(_FIXTURE_CONTROLLER, sid=999)), patch.object(resource, 'setrlimit', side_effect=AssertionError('interactive limits untouched')):
            with self.assertRaises(ValueError):
                performance._dual_supervisor_limits()

    def test_supervisor_finite_soft_hard_and_affinity_are_owned_mock_only(self):
        import resource
        calls = []
        affinity = []
        with patch.object(performance, '_dual_self_identity', return_value=_FIXTURE_CONTROLLER), patch.object(performance.os, 'sched_getaffinity', return_value=set(range(8))), patch.object(performance.os, 'sched_setaffinity', side_effect=lambda pid, cpus: affinity.append((pid, cpus))), patch.object(performance.signal, 'signal'), patch.object(performance.signal, 'getsignal', return_value=performance.signal.SIG_DFL), patch.object(resource, 'getrlimit', return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), patch.object(resource, 'setrlimit', side_effect=lambda kind, value: calls.append((kind, value))):
            result = performance._dual_supervisor_limits()
        self.assertEqual(result['address_space_soft_bytes'], 256 * 1024 * 1024)
        self.assertEqual(result['address_space_hard_bytes'], 512 * 1024 * 1024)
        self.assertEqual(result['cpu_soft_seconds'], 10)
        self.assertEqual(result['cpu_hard_seconds'], 60)
        self.assertEqual(affinity, [(0, {0, 1, 2, 3})])
        self.assertEqual(result['affinity'], [0, 1, 2, 3])
        self.assertIn((resource.RLIMIT_CORE, (0, 0)), calls)
        with patch.object(performance, '_dual_self_identity', return_value=_FIXTURE_CONTROLLER), patch.object(performance.os, 'sched_getaffinity', return_value={0}), patch.object(resource, 'setrlimit', side_effect=AssertionError('invalid affinity refused before mutation')):
            with self.assertRaises(ValueError):
                performance._dual_supervisor_limits([0, 0])

    def test_known_exiting_owner_is_gap_but_readiness_and_foreign_identity_refuse(self):
        owner = performance._DualProcOwner.__new__(performance._DualProcOwner)
        owner.identity = dict(_FIXTURE_WORKER)
        owner.fd = None
        for state in (b'Z', b'X', b'x'):
            with patch.object(owner, 'read', return_value=_fixture_stat(pid=202, start=789, pgid=202, sid=202, state=state)):
                with self.assertRaises(ProcessLookupError) as failure:
                    owner.rss()
                self.assertEqual(failure.exception.errno, errno.ESRCH)
                with self.assertRaises(ValueError):
                    owner.recheck(require_live=True)
            with patch.object(owner, 'read', return_value=_fixture_stat(pid=202, start=790, pgid=202, sid=202, state=state)):
                with self.assertRaises(ValueError):
                    owner.rss()
        s = self.sampler()
        self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
        _FakeProcOwner.current[202] = ProcessLookupError(errno.ESRCH, 'known exiting')
        s.sample()
        self.assertIsNone(s.samples[-1]['owned_rss_bytes'])
        self.assertIsNone(s.error)
        self.assertEqual(s.samples[-1]['gaps'][0]['errno'], errno.ESRCH)
        proof = {'leader_reaped': True, 'group_absent': True, 'mailboxes_removed': True}
        self.assertTrue(self.observe(s, _fixture_event('cleanup', dict(_FIXTURE_WORKER), cleanup=proof)))
        s.sample()
        r = s.finish()
        self.assertEqual(r['peak_sampled_owned_rss_bytes'], 1024)
        self.assertEqual(r['sample_gap_count'], 1)
        self.assertIsNone(r['error'])


if __name__ == '__main__':
    unittest.main()
