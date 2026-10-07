"""One finite regression for owned-worker timeout cleanup and outside scratch."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations import engine_checks

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))


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
