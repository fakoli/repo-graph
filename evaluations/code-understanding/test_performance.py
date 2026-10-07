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
            graph = {'file_count': 1, 'files': [], 'tree': [], 'dependencies': [],
                     'scope_edges': [], 'system': {}, 'search': {'documents': 1},
                     'scan': {'code_files': 1, 'failed': 0, 'truncated': 1, 'scanned': 1, 'reused': 0}}
            for symlink in (True, False):
                output = root / str(symlink)
                def fake_map(argv):
                    output.mkdir(exist_ok=True)
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
            config.write_text(json.dumps({'corpora': corpora}))
            class FailedWorker:
                pid = 99999999
                returncode = 2
                def __init__(self, *args, **kwargs):
                    if not report.exists():
                        report.symlink_to(canary)
                def communicate(self, timeout=None):
                    return '{"status":"failed","error_kind":"fixture failure"}', ''
            def revision(argv, cwd, **kwargs):
                return PINS.get(Path(cwd).name, 'a' * 40) + '\n'
            with patch.object(performance.subprocess, 'Popen', FailedWorker), patch.object(
                    performance.subprocess, 'check_output', side_effect=revision):
                records = performance.profile_structural(config, report, root / 'worker-logs')
            self.assertEqual(canary.read_text(), 'PRIVATE_TEST_SOURCE')
            self.assertFalse(report.is_symlink())
            self.assertEqual(len(records), 24)
            self.assertTrue(all(r['exit_code'] == 2 for r in records))
            data = json.loads(report.read_text())
            self.assertTrue(data['implementation']['sha256'])
            self.assertTrue(data['implementation']['native_backend'])
            self.assertNotIn(str(root), report.read_text())

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
