"""One finite regression for owned-worker timeout cleanup and outside scratch."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations import engine_checks
from evaluations.analysis import compact_adapter_result

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))


class PortableAdapterProofTests(unittest.TestCase):
    def test_failure_and_occurrence_evidence_survives_without_private_payloads(self):
        private = 'SYNTHETIC_PRIVATE_PAYLOAD'
        attempt = {'status': 'failed', 'error_kind': 'RuntimeError', 'mode': 'queued', 'concurrency': 2,
            'inventory': [{'path': 'tiny.py', 'status': 'source_error', 'error_kind': 'OSError', 'content': private}],
            'resources': {'digest_read_bytes': 19, 'queued': {'workers_started': 1,
                'worker_resources': [[{'process_peak_rss_bytes': 1234, 'elapsed_seconds': .1,
                                      'process_user_seconds': .2, 'process_system_seconds': .01, 'private': private}]]}},
            'cleanup': [{'leader_reaped': True, 'group_absent': True, 'mailboxes_removed': True}],
            'collector_failures': [{'index': 0, 'kind': 'collection_failed', 'record':
                {'path': 'tiny.py', 'language': 'python', 'kind': 'source', 'bytes': 19,
                 'sha256': 'a' * 64, 'content': private}}],
            'traceback': private}
        exported = compact_adapter_result({'status': 'failed', 'results': [
            {'id': 'U-SYNTHETIC', 'language': 'python', 'status': 'failed', 'error_kind': 'RuntimeError',
             'attempts': {'base_queued': attempt}, 'traceback': private}]}, 'updates')
        kept = exported['results'][0]['attempts']['base_queued']
        self.assertEqual(kept['status'], 'failed')
        self.assertEqual(kept['source_failures'][0]['path'], 'tiny.py')
        self.assertEqual(kept['resources']['digest_read_bytes'], 19)
        self.assertEqual(kept['resources']['queued']['workers_started'], 1)
        self.assertEqual(kept['resources']['queued']['worker_summaries'][0]['process_peak_rss_bytes'], 1234)
        self.assertNotIn(private, json.dumps(exported))
        self.assertEqual(kept['inventory_status_counts'], {'source_error': 1})
        self.assertEqual(kept['collector_failures'][0]['record']['path'], 'tiny.py')
        row = {'site': {'id': 'site', 'path': 'tiny.py', 'range': {'start_byte': 0, 'end_byte': 3},
                       'role': 'call', 'source_sha256': 'a' * 64, 'body': private},
               'caller': {'id': 'caller', 'body': private}, 'target': {'id': 'target'},
               'certainty': 'resolved', 'targets_exhaustive': True}
        page = {'generation': 'g', 'rows': [row], 'examined_relationships': 1,
                'returned_edges': 1, 'cursor': private, 'stop_reason': 'output_budget'}
        query = compact_adapter_result({'status': 'failed', 'modes': [{'mode': 'serial', 'status': 'failed',
            'queries': [{'id': 'Q-SYNTHETIC', 'status': 'failed', 'error_kind': 'AssertionError',
                         'responses': [{'response': page, 'serialized_response_bytes': 1024}]}]}]}, 'queries')
        kept = query['modes'][0]['queries'][0]['responses'][0]
        self.assertEqual(kept['serialized_response_bytes'], 1024)
        self.assertTrue(kept['response']['has_cursor'])
        self.assertEqual(kept['response']['rows'][0]['target_id'], 'target')
        self.assertNotIn(private, json.dumps(query))


@unittest.skipUnless(AVAILABLE, 'Optional analysis backend is absent')
class WorkerCleanupTests(unittest.TestCase):
    def test_timeout_kills_and_reaps_only_owned_group(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            job, logs = root / 'worker', root / 'logs'
            (job / 'source').mkdir(parents=True)
            logs.mkdir()
            raw = b'def local():\n    pass\nlocal()\n'
            (job / 'source' / 'main.py').write_bytes(raw)
            (root / 'canary').write_bytes(b'outside scratch must survive')
            before = (root / 'canary').stat()
            inventory = [{'path': 'main.py', 'language': 'python', 'kind': 'source',
                          'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}]
            process = engine_checks._start_worker(job, logs, inventory, 'forced_stall')
            try:
                ready = engine_checks._wait_ready(job, process)
                self.assertEqual(ready, {'phase': 'controlled_post_scan_stall'})
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.05)
                receipt = engine_checks._stop_and_reap(process)
                self.assertEqual(receipt['signals'], ['SIGTERM', 'SIGKILL'])
                self.assertTrue(receipt['leader_reaped'])
                self.assertTrue(receipt['group_absent'])
                self.assertEqual(receipt['returncode'], -9)
                self.assertFalse(engine_checks._group_exists(process))
                # Repeated cleanup must not target a stale or reused process ID.
                self.assertIs(engine_checks._stop_and_reap(process), receipt)
                summary = engine_checks._summary(job)
                self.assertEqual(summary['status'], 'complete')
                self.assertTrue(all(summary['isolation'].values()))
                self.assertEqual(summary['counts']['definitions'], 1)
                self.assertEqual(summary['resources']['source_bytes'], len(raw))
                after = (root / 'canary').stat()
                self.assertEqual((root / 'canary').read_bytes(), b'outside scratch must survive')
                self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                                 (after.st_ino, after.st_size, after.st_mtime_ns))
            finally:
                engine_checks._stop_and_reap(process)


if __name__ == '__main__':
    unittest.main()
