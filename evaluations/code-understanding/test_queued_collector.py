"""Synthetic owned-worker boundaries; no frozen sources or capacity claims."""
from dataclasses import asdict, replace
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
from pathlib import Path
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations import queued_collector as queue
from evaluations import tree_sitter_baseline as baseline
from repo_graph.source import SourceRoot


def blob(path='sample.py', raw=b'def local():\n    return 1\nlocal()\n'):
    return {'path': path, 'language': 'python', 'content': raw,
            'kind': 'source', 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def backend_available():
    try:
        return {name: importlib.metadata.version(name) for name in baseline.PINS} == baseline.PINS
    except importlib.metadata.PackageNotFoundError:
        return False


class AdmissionTests(unittest.TestCase):
    def test_run_creation_failure_closes_owner_and_removes_temporary_directory(self):
        opened, temporary = [], []
        original_root, original_temporary = queue.SourceRoot, queue.tempfile.TemporaryDirectory
        def root(*args, **kwargs):
            owned = original_root(*args, **kwargs)
            opened.append(owned)
            return owned
        def directory(*args, **kwargs):
            owned = original_temporary(*args, **kwargs)
            temporary.append(owned)
            return owned
        with patch.object(queue, 'SourceRoot', root), \
                patch.object(queue.tempfile, 'TemporaryDirectory', directory), \
                patch.object(queue, '_new_directory', side_effect=OSError('synthetic setup failure')):
            for _ in range(4):
                with self.assertRaises(OSError):
                    queue.collect_files([])
        self.assertTrue(opened)
        self.assertEqual(len(temporary), 4)
        self.assertTrue(all(owned.fd is None for owned in opened))
        self.assertTrue(all(not Path(owned.name).exists() for owned in temporary))

    def test_loaded_controller_disk_drift_rejects_before_worker_start(self):
        with tempfile.TemporaryDirectory() as scratch:
            clone = Path(scratch)
            for name in queue.IMPLEMENTATIONS:
                target = clone / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            code = '''import hashlib,json,sys
from pathlib import Path
from unittest.mock import patch
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
from evaluations import queued_collector as queue
source=root/'evaluations/queued_collector.py'
loaded=queue._LOADED_CONTROLLER_SHA256
source.write_bytes(source.read_bytes()+b'\\n# synthetic copied-source drift\\n')
current=hashlib.sha256(source.read_bytes()).hexdigest()
with patch.object(queue.subprocess,'Popen',side_effect=AssertionError('Stale controller spawned worker')):
    try:
        queue.collect_files([{'path':'sample.py','language':'python','content':b'pass\\n'}])
    except ValueError:
        print(json.dumps({'rejected':True,'loaded_differs_from_disk':loaded!=current}))
    else:
        raise AssertionError('Stale loaded controller accepted')
'''
            process = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(clone)],
                                     capture_output=True, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertEqual(json.loads(process.stdout), {'rejected': True, 'loaded_differs_from_disk': True})

    def test_explicit_modes_and_typed_finite_limits(self):
        for mode, concurrency in [('serial', 2), ('queued', True), ('queued', 0), ('queued', 5), ('auto', 1)]:
            with self.subTest(mode=mode, concurrency=concurrency), self.assertRaises(ValueError):
                queue.collect_files([], mode=mode, concurrency=concurrency)
        for values in ({'cpu_seconds': True}, {'worker_wall_seconds': float('inf')},
                       {'total_wall_seconds': float('nan')}, {'max_request_bytes': 65536}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                queue.QueueLimits(**values)

    def test_zero_changed_jobs_still_consume_validation_generator(self):
        observed = []
        def unchanged():
            observed.append('validated reused source')
            if False:
                yield blob()
        with patch.object(queue.subprocess, 'Popen', side_effect=AssertionError('No worker needed')):
            result = queue.collect_files(unchanged(), mode='queued', concurrency=4)
        self.assertEqual(observed, ['validated reused source'])
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.resources['workers_started'], 0)
        self.assertEqual(result.collected, [])
        self.assertEqual(result.cleanup, [])

    def test_generator_validation_failure_and_source_only_admission(self):
        def invalidated():
            if False:
                yield blob()
            raise OSError('synthetic freshness invalidation')
        result = queue.collect_files(invalidated())
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.stop_reason, 'input_or_protocol_rejected')
        self.assertTrue(result.failures)
        for supplied in (dict(blob(), targets=[]), dict(blob(), bytes=True),
                         dict(blob(), content=bytearray(b'pass')), dict(blob(), path='../outside.py')):
            with self.subTest(supplied_keys=list(supplied)):
                result = queue.collect_files([supplied])
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.resources['workers_started'], 0)

    def test_reused_only_cancellation_and_deadline_keep_stop_reason(self):
        for reason in ('cancelled', 'deadline_exceeded'):
            def interrupted():
                if False:
                    yield blob()
                raise baseline.StopScan(reason)
            with self.subTest(reason=reason):
                result = queue.collect_files(interrupted())
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.stop_reason, reason)
                self.assertEqual(result.resources['workers_started'], 0)

    def test_strict_json_rejects_duplicates_and_nonfinite_overflow(self):
        for raw in (b'{"x":1,"x":1}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                queue._decode(raw)

    def test_unsupported_host_and_public_evidence_fail_before_spawn(self):
        with patch.object(queue, 'DESCRIPTOR_OPENS', False), self.assertRaises(OSError):
            queue.collect_files([blob()])
        with patch.object(queue.subprocess, 'Popen', side_effect=AssertionError('No public worker directory')):
            with self.assertRaises(ValueError):
                queue.collect_files([blob()], evidence_directory=ROOT)


@unittest.skipUnless(backend_available(), 'Pinned optional native backend unavailable')
class OwnedWorkerTests(unittest.TestCase):
    def assert_cleanup(self, result):
        self.assertTrue(result.cleanup)
        for row in result.cleanup:
            self.assertTrue(row['leader_reaped'])
            self.assertTrue(row['group_absent'])
            self.assertTrue(row['mailboxes_removed'])

    def test_serial_and_queued_use_identical_unresolved_handoff_and_input_order(self):
        supplied = [blob('z.py'), blob('a.py', b'def another():\n    local()\n'), blob('m.py')]
        serial = queue.collect_files(iter(supplied), mode='serial', concurrency=1)
        queued = queue.collect_files(iter(supplied), mode='queued', concurrency=3)
        self.assertEqual(serial.status, 'complete')
        self.assertEqual(queued.status, 'complete')
        self.assertEqual([file.path for file in queued.collected], ['z.py', 'a.py', 'm.py'])
        self.assertEqual([file.to_json() for file in serial.collected],
                         [file.to_json() for file in queued.collected])
        self.assertEqual(serial.resources['workers_started'], 1)
        self.assertEqual(serial.cleanup[0]['requests'], 3)
        self.assertEqual(queued.resources['workers_started'], 3)
        for file in queued.collected:
            self.assertEqual(file.sites, [])
            for candidate, _, _ in file.candidates:
                self.assertNotIn('targets', candidate)
        self.assert_cleanup(serial)
        self.assert_cleanup(queued)

    def test_backpressure_reuses_owned_worker_and_caps_reserved_bytes(self):
        budget = baseline.Budget(max_file_bytes=1024)
        limits = queue.QueueLimits()
        reservation = budget.max_file_bytes + limits.max_request_bytes + limits.max_result_bytes
        limits = replace(limits, max_inflight_bytes=reservation)
        result = queue.collect_files((blob(str(i) + '.py') for i in range(5)),
                    mode='queued', concurrency=4, budget=budget, limits=limits)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.resources['configured_concurrency'], 4)
        self.assertEqual(result.resources['workers_started'], 1)
        self.assertEqual(result.cleanup[0]['requests'], 5)
        self.assertLessEqual(result.resources['peak_inflight_reserved_bytes'], limits.max_inflight_bytes)
        self.assert_cleanup(result)

    def test_byte_and_file_limits_leave_explicit_failures(self):
        result = queue.collect_files([blob()], limits=queue.QueueLimits(max_result_bytes=32))
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.collected, [])
        self.assertEqual(result.failures[0]['kind'], 'StopScan')
        self.assert_cleanup(result)
        result = queue.collect_files([blob('one.py'), blob('two.py')], budget=baseline.Budget(max_files=1))
        self.assertEqual(result.status, 'partial')
        self.assertEqual(result.stop_reason, 'source_admission_budget_exceeded')
        self.assertEqual([file.path for file in result.collected], ['one.py'])
        self.assertTrue(result.failures)
        self.assert_cleanup(result)

    def test_received_targets_tampering_and_replayed_receipts_are_rejected(self):
        supplied = blob()
        file = baseline.collect_file(supplied)
        identity, token = queue._identity(), 'a' * 32
        record = file.record
        limits, budget = queue.QueueLimits(), baseline.Budget()
        encoded = file.to_json()
        base_receipt = {'schema_version': 1, 'token': token, 'index': 7, 'record': record,
            'identity': identity, 'status': 'collected', 'sha256': hashlib.sha256(encoded).hexdigest(),
            'bytes': len(encoded), 'error_kind': None, 'reason': None,
            'resources': {'elapsed_seconds': 0.1, 'process_peak_rss_bytes': 1024,
                          'process_user_seconds': 0.1, 'process_system_seconds': 0.0}}
        variants = [('foreign_token', dict(base_receipt, token='b' * 32), encoded),
                    ('replayed_sequence', dict(base_receipt, index=6), encoded),
                    ('typed_index', dict(base_receipt, index=7.0), encoded),
                    ('wrong_digest', dict(base_receipt, sha256='0' * 64), encoded)]
        malicious = json.loads(encoded)
        malicious['candidates'][0]['fact']['targets'] = []
        changed = queue._encoded(malicious, limits.max_result_bytes)
        variants.append(('final_targets', dict(base_receipt,
                         sha256=hashlib.sha256(changed).hexdigest(), bytes=len(changed)), changed))
        for name, receipt, payload in variants:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as scratch:
                with SourceRoot(Path(scratch)) as guarded:
                    queue._write(guarded, 'payload.json', payload, limits.max_result_bytes)
                    queue._write(guarded, 'result.json', queue._encoded(receipt, queue.CONTROL_BYTES), queue.CONTROL_BYTES)
                    worker = {'guarded': guarded, 'pending': {'index': 7, 'record': record}}
                    with self.assertRaises(ValueError):
                        queue._receive(worker, identity, token, budget, limits, None)

    def test_private_logs_retain_no_source_mailboxes_and_preserve_outside_canary(self):
        with tempfile.TemporaryDirectory() as scratch:
            outside = Path(scratch) / 'canary'
            outside.write_bytes(b'outside private run must survive')
            before = outside.stat()
            result = queue.collect_files([blob(raw=b'def SYNTHETIC_PRIVATE_BODY():\n    return 1\n')],
                                         evidence_directory=Path(scratch))
            self.assertEqual(result.status, 'complete')
            self.assert_cleanup(result)
            after = outside.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_size),
                             (after.st_ino, after.st_mtime_ns, after.st_size))
            retained = list(Path(scratch).glob('run-*/**/*'))
            self.assertTrue(any(path.name == 'receipt.json' for path in retained))
            for path in retained:
                if path.is_file():
                    self.assertNotIn(path.name, queue.MAILBOXES)
                    self.assertNotIn(b'SYNTHETIC_PRIVATE_BODY', path.read_bytes())

    def test_ancestor_swap_cannot_redirect_private_creation_or_worker_writes(self):
        with tempfile.TemporaryDirectory() as scratch:
            parent = Path(scratch)
            evidence, moved, protected = parent / 'evidence', parent / 'moved', parent / 'protected'
            evidence.mkdir()
            protected.mkdir()
            original = os.mkdir
            swapped = False
            def swap(name, *args, **kwargs):
                nonlocal swapped
                value = original(name, *args, **kwargs)
                if type(name) is str and name.startswith('run-') and not swapped:
                    swapped = True
                    evidence.rename(moved)
                    evidence.symlink_to(protected, target_is_directory=True)
                return value
            with patch.object(queue.os, 'mkdir', swap):
                result = queue.collect_files([blob()], evidence_directory=evidence)
            self.assertTrue(swapped)
            self.assertEqual(result.status, 'complete')
            self.assertEqual(list(protected.iterdir()), [])
            self.assertTrue(list(moved.glob('run-*/receipt.json')))
            self.assert_cleanup(result)

    def test_cancellation_after_readiness_reaps_owned_workers(self):
        ready = False
        original = queue._start
        def started(*args, **kwargs):
            nonlocal ready
            worker = original(*args, **kwargs)
            ready = True
            return worker
        with patch.object(queue, '_start', started):
            result = queue.collect_files([blob()], cancel=lambda: ready)
        self.assertEqual(result.stop_reason, 'cancelled')
        self.assertEqual(result.status, 'failed')
        self.assert_cleanup(result)

    def test_atomic_publication_runtime_failure_preserves_result_and_cleanup(self):
        original = SourceRoot.atomic_writer
        @contextmanager
        def failed(owned, name, **kwargs):
            with original(owned, name, **kwargs) as stream:
                yield stream
            if name == 'request.json':
                raise RuntimeError('synthetic directory fsync failure after publication')
        with patch.object(SourceRoot, 'atomic_writer', failed):
            result = queue.collect_files([blob()])
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.stop_reason, 'input_or_protocol_rejected')
        self.assertTrue(any(row['kind'] == 'RuntimeError' for row in result.failures))
        self.assert_cleanup(result)

    def test_receipt_directory_sync_failure_returns_failed_result_without_stale_receipt(self):
        original = SourceRoot.atomic_writer
        @contextmanager
        def failed(owned, name, **kwargs):
            with original(owned, name, **kwargs) as stream:
                yield stream
            if name == 'receipt.json':
                raise RuntimeError('synthetic receipt directory fsync failure after publication')
        with tempfile.TemporaryDirectory() as scratch, patch.object(SourceRoot, 'atomic_writer', failed):
            result = queue.collect_files([blob()], evidence_directory=Path(scratch))
            self.assertEqual(list(Path(scratch).glob('run-*/receipt.json')), [])
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.stop_reason, 'input_or_protocol_rejected')
        self.assertTrue(result.resources['receipt_write_failed'])
        self.assertTrue(any(row.get('stage') == 'receipt' for row in result.failures))
        self.assert_cleanup(result)

    def test_worker_exit_after_last_result_is_explicit_failure(self):
        original = queue._receive
        def receive(worker, *args):
            result = original(worker, *args)
            if result is not None:
                worker['process'].kill()
                worker['process'].wait(timeout=2)
            return result
        with patch.object(queue, '_receive', receive):
            result = queue.collect_files([blob()])
        self.assertEqual(result.status, 'partial')
        self.assertEqual(result.stop_reason, 'worker_exited')
        self.assertEqual(len(result.collected), 1)
        self.assertTrue(any(row['kind'] == 'worker_exited' for row in result.failures))
        self.assert_cleanup(result)

    def test_stalled_startup_kills_only_owned_group_and_escalates(self):
        original = subprocess.Popen
        unrelated = original([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(20)'],
                             start_new_session=True)
        def stall(argv, **kwargs):
            return original([sys.executable, '-I', '-B', '-c',
                'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)'], **kwargs)
        try:
            with patch.object(queue.subprocess, 'Popen', stall):
                result = queue.collect_files([blob()], limits=queue.QueueLimits(worker_wall_seconds=0.1))
            self.assertEqual(result.status, 'failed')
            self.assertEqual(result.stop_reason, 'deadline_exceeded')
            self.assert_cleanup(result)
            self.assertEqual(result.cleanup[0]['signals'], ['SIGTERM', 'SIGKILL'])
            self.assertIsNone(unrelated.poll())
        finally:
            try:
                os.killpg(unrelated.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            unrelated.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
