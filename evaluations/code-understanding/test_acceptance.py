"""Synthetic validator negatives; these are not independent human judgments."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evaluations import acceptance as gate
from evaluations import analysis, real_calls


class AdapterEvidence(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('git'), 'Linux Git ownership check')
    def test_result_only_evidence_delivery(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            def git(*args, input=None):
                return subprocess.check_output(['git', '--no-replace-objects',
                    '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                    '-c', 'core.fsmonitor=false', '-c', 'user.name=Synthetic Test',
                    '-c', 'user.email=test@example.invalid', *args], cwd=root,
                    env=dict(os.environ, GIT_AUTHOR_NAME='Synthetic Test',
                        GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_NAME='Synthetic Test',
                        GIT_COMMITTER_EMAIL='test@example.invalid', GIT_OPTIONAL_LOCKS='0'),
                    input=input, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
            git('init', '--template=', '-q')
            source = b'def work():\n    return 1\n'
            (root / 'helper.py').write_bytes(source)
            for path in gate._DELIVERY_OUTPUTS:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(b'{}\n')
            git('add', '--', '.')
            git('commit', '-qm', 'Synthetic measured source')
            measured = git('rev-parse', 'HEAD')
            hashes = {'helper.py': gate.digest(source)}
            (root / gate._DELIVERY_OUTPUTS[0]).write_bytes(b'{"status":"blocked"}\n')
            git('add', '--', gate._DELIVERY_OUTPUTS[0])
            git('commit', '-qm', 'Retain synthetic evidence')
            delivered = git('rev-parse', 'HEAD')
            admitted = gate._proof_delivery(root, measured, hashes, validation_commit=delivered)
            self.assertEqual(admitted['result_only_descendant_commits'], 1)
            self.assertEqual(admitted['measured_commit'], measured)
            (root / 'helper.py').write_bytes(b'def changed():\n    return 2\n')
            with self.assertRaises(ValueError):
                gate._proof_delivery(root, measured, hashes)
            (root / 'helper.py').write_bytes(source)
            tree = git('rev-parse', 'HEAD^{tree}')
            measured_merge = git('commit-tree', tree, '-p', delivered, '-p', measured,
                                 input=b'Synthetic source integration\n')
            git('update-ref', 'HEAD', measured_merge)
            self.assertEqual(gate._proof_delivery(root, measured_merge, hashes)
                             ['result_only_descendant_commits'], 0)
            child = git('commit-tree', tree, '-p', measured_merge,
                        input=b'Synthetic retained evidence child\n')
            git('update-ref', 'HEAD', child)
            self.assertEqual(gate._proof_delivery(root, measured_merge, hashes)
                             ['result_only_descendant_commits'], 1)
            descendant_merge = git('commit-tree', tree, '-p', child, '-p', measured_merge,
                                   input=b'Synthetic descendant merge\n')
            git('update-ref', 'HEAD', descendant_merge)
            with self.assertRaises(ValueError):
                gate._proof_delivery(root, measured_merge, hashes)
            git('update-ref', 'HEAD', delivered)
            (root / 'README.md').write_bytes(b'Synthetic documentation change\n')
            git('add', '--', 'README.md')
            git('commit', '-qm', 'Change a non-output file')
            with self.assertRaises(ValueError):
                gate._proof_delivery(root, measured, hashes)


    def test_archive_bytes_inventory_projection_and_failure_identifiers(self):
        with tempfile.TemporaryDirectory() as scratch:
            parent = Path(scratch)
            directory = 'queries-' + 'a' * 32
            archive = parent / directory
            archive.mkdir()
            raw = gate.canonical({'status': 'failed', 'modes': []})
            (archive / 'report.json').write_bytes(raw)
            reference = {'path': 'report.json', 'sha256': gate.digest(raw), 'bytes': len(raw)}
            wrapper = {'full_private_report': json.loads(raw), 'archive': {
                'directory': directory, 'files': [reference], 'bytes': len(raw)}}
            portable = analysis.compact_adapter_result(wrapper, 'queries')
            actual, files, _ = gate._proof_archive(portable, 'queries', parent)
            self.assertEqual(actual['status'], 'failed')
            self.assertEqual(files['report.json'], reference)
            (archive / 'unlisted.json').write_bytes(b'{}')
            with self.assertRaises(ValueError):
                gate._proof_archive(portable, 'queries', parent)
            (archive / 'unlisted.json').unlink()
            changed = copy.deepcopy(portable)
            changed['status'] = 'passed'
            with self.assertRaises(ValueError):
                gate._proof_archive(changed, 'queries', parent)
            (archive / 'report.json').write_bytes(raw + b' ')
            with self.assertRaises(ValueError):
                gate._proof_archive(portable, 'queries', parent)
        for path in ('../outside', '/absolute', 'double//slash', 'back\\slash'):
            with self.assertRaises(ValueError):
                gate._proof_path(path)
        private = 'SYNTHETIC_PRIVATE_IDENTIFIER_123456'
        observed = gate._proof_observed({'results': [{'id': private, 'status': 'failed',
            'attempts': {'base_serial': {'status': 'partial'}}}]}, 'updates')
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['recorded_status'], 'failed')
        self.assertEqual(observed[0]['attempt_statuses'], {'base_serial': 'partial'})
        self.assertNotIn(private, json.dumps(observed))


class FreezeInputs(unittest.TestCase):
    def test_strict_json_rejects_numeric_overflow_and_duplicates(self):
        for raw in (b'{"measurement":1e999}', b'{"id":1,"id":2}'):
            source = SimpleNamespace(read=lambda *args, **kwargs: (raw, '0' * 64, SimpleNamespace(st_size=len(raw))))
            with self.assertRaises(ValueError):
                gate.read_json(source, 'synthetic.json')

    def test_comparison_report_cap_does_not_raise_manifest_cap(self):
        raw = json.dumps({'proof': 'x' * (1024 * 1024)}).encode()
        source = SimpleNamespace(read=lambda *args, **kwargs: (raw, '0' * 64, SimpleNamespace(st_size=len(raw))))
        with self.assertRaises(ValueError):
            gate.read_json(source, 'manifest.json')
        report, _ = gate.read_json(source, 'engine-comparison.json', 2 * 1024 * 1024)
        self.assertEqual(len(report['proof']), 1024 * 1024)
        partial = SimpleNamespace(read=lambda *args, **kwargs: (b'{}', '0' * 64, SimpleNamespace(st_size=3)))
        with self.assertRaises(ValueError):
            gate.read_json(partial, 'engine-comparison.json', 2 * 1024 * 1024)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='repo-graph-freeze-test-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / 'repository'
        for relative in (gate.INPUTS, 'tests/fixtures/code-understanding'):
            shutil.copytree(gate.ROOT / relative, self.root / relative)
        self.manifest = self.root / gate.INPUTS / 'fixtures.json'

    def run_gate(self, *args):
        output = io.StringIO()
        with patch.object(gate, 'ROOT', self.root), contextlib.redirect_stdout(output):
            status = gate.main(list(args))
        return status, json.loads(output.getvalue())

    def test_source_and_uncertainty_negatives(self):
        original = json.loads(self.manifest.read_text())
        checks, _, _ = gate.inputs(self.root)
        self.assertTrue(all(c['status'] == 'passed' for c in checks), checks)
        def bad_hash(d): d['files'][0]['sha256'] = '0' * 64
        def bad_range(d): d['cases'][0]['range']['start_byte'] += 1
        def false_certainty(d): d['cases'][0].update(certainty='resolved', targets=[])
        def bad_language(d): d['cases'][0]['language'] = 'unrecognized'
        def bad_targets(d): d['cases'][0]['targets'] = [{}]
        def duplicate(d): d['cases'].append(copy.deepcopy(d['cases'][0]))
        def bad_schema(d): d['schema_version'] = True
        def bad_records(d): d['cases'] = ['invalid']
        def bad_role(d): d['cases'][0]['role'] = []
        def unsafe_id(d): d['cases'][0]['id'] = 'https://invalid.example/?access_token=TEST_ONLY'
        def bad_mutation(d): d['updates'][0]['operations'][0]['path'] = '../outside'
        def bad_impacts_string(d): d['updates'][0]['expected_impacts'] = 'malformed'
        def bad_impacts_dict(d): d['updates'][0]['expected_impacts'] = {'malformed': 'shape'}
        def bad_impacts_member(d): d['updates'][0]['expected_impacts'] = ['malformed']
        for mutation in (bad_hash, bad_range, false_certainty, bad_language, bad_targets,
                         duplicate, bad_schema, bad_records, bad_role, unsafe_id, bad_mutation,
                         bad_impacts_string, bad_impacts_dict, bad_impacts_member):
            with self.subTest(mutation=mutation.__name__):
                changed = copy.deepcopy(original)
                mutation(changed)
                self.manifest.write_text(json.dumps(changed))
                checks, _, _ = gate.inputs(self.root)
                self.assertTrue(any(c['status'] == 'failed' for c in checks), checks)
                self.assertNotIn('access_token', json.dumps(checks))
        self.manifest.write_text(json.dumps(original))
        candidate_path = self.root / gate.INPUTS / 'real-calls.json'
        candidates = json.loads(candidate_path.read_text())
        candidates['cases'][0]['path'] = '../outside'
        candidate_path.write_text(json.dumps(candidates))
        checks, _, _ = gate.inputs(self.root)
        self.assertTrue(any(c['status'] == 'failed' for c in checks), checks)
        path = self.root / original['files'][0]['path']
        outside = self.root.parent / 'outside'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        checks, _, _ = gate.inputs(self.root)
        self.assertTrue(any(c['status'] == 'failed' for c in checks), checks)

    def test_locked_preparation_does_not_pass_freeze(self):
        status, report = self.run_gate('--prepare', '--seal-inputs')
        self.assertEqual(status, 0)
        self.assertTrue(report['prepared'])
        self.assertEqual(report['status'], 'blocked')
        with patch.object(gate, 'committed', return_value=True):
            status, report = self.run_gate('--prepare')
            self.assertEqual(status, 0)
            self.assertEqual(report['status'], 'blocked')
        status, report = self.run_gate('--gate', 'freeze')
        self.assertEqual(status, 1)
        (self.root / gate.INPUTS / 'source-review.json').unlink()
        status, report = self.run_gate('--gate', 'freeze')
        self.assertEqual(status, 1)
        self.assertIn('independent-real-call-truth', {c['id'] for c in report['failures']})
        lock = self.root / gate.INPUTS / 'input-lock.json'
        data = json.loads(lock.read_text())
        data['sha256']['evaluations/code-understanding/fixtures.json'] = '0' * 64
        lock.write_text(json.dumps(data))
        status, report = self.run_gate('--prepare')
        self.assertEqual(status, 1)
        self.assertIn('input-lock', {c['id'] for c in report['failures']})
        lock.write_text('[]')
        status, report = self.run_gate('--prepare')
        self.assertEqual(status, 1)
        self.assertIn('input-lock', {c['id'] for c in report['failures']})

    def test_missing_model_and_incomplete_receipts(self):
        template = self.root.parent / 'judgments.json'
        status, _ = self.run_gate('--review-template', str(template))
        self.assertEqual(status, 0)
        data = json.loads(template.read_text())
        candidates = json.loads((self.root / gate.INPUTS / 'real-calls.json').read_text())['cases']
        sha = data['candidate_manifest_sha256']
        self.assertEqual(gate.source_truth(None, sha, candidates)[0], [])
        self.assertEqual(gate.source_truth(template, sha, candidates)[0], [])
        data['review'].update(kind='model', independent=True, source_reviewed=True,
                              reviewer='TEST_ONLY_MODEL', evidence_reference='synthetic negative')
        template.write_text(json.dumps(data))
        self.assertEqual(gate.source_truth(template, sha, candidates)[0], [])
        data['review']['kind'] = 'human'
        template.write_text(json.dumps(data))
        self.assertEqual(gate.source_truth(template, sha, candidates)[0], [])
        data['candidate_manifest_sha256'] = '0' * 64
        template.write_text(json.dumps(data))
        self.assertEqual(gate.source_truth(template, sha, candidates)[0], [])

    def test_a_moving_head_cannot_accept_mixed_snapshot_blobs(self):
        revision = 'a' * 40
        wanted = {'first': gate.digest(b'a-first'), 'second': gate.digest(b'b-second')}
        def git(args, **kwargs):
            if args[1] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout=revision + '\n')
            commit, path = args[-1].split(':')
            # HEAD moves between show calls; this mixed input exists in no single commit.
            data = (b'a-first' if path == 'first' else b'b-second') if commit == 'HEAD' else (
                b'a-first' if path == 'first' else b'a-second')
            return SimpleNamespace(returncode=0, stdout=data)
        with patch.object(gate.subprocess, 'run', side_effect=git):
            self.assertFalse(gate.committed(self.root, wanted))

    def experiment(self, kind, cases=None):
        for path in ('evaluations/analysis.py', 'evaluations/acceptance.py'):
            (self.root / path).write_bytes((gate.ROOT / path).read_bytes())
        _, identity = analysis.frozen_inputs(self.root)
        hashes = {path: gate.digest((self.root / path).read_bytes()) for path in
                  ('evaluations/analysis.py', 'evaluations/acceptance.py')}
        report = {'schema_version': 1, 'status': 'passed', 'source_identity': identity,
                  'implementation': {'sha256': hashes}, 'case_results': cases or [],
                  'engine_selected': True, 'selected_owner': 'synthetic-negative-only',
                  'budget_freeze': {'status': 'locked'}, 'remaining_gates': [],
                  'rust': {'adopted': False, 'prototype_built': False, 'speed_gain_measured': False}}
        if kind == 'engine':
            with patch.object(real_calls, 'committed', return_value=True):
                calls = json.loads((self.root / gate.INPUTS / 'real-calls.json').read_text())
                review = json.loads((self.root / gate.INPUTS / 'source-review.json').read_text())
                _, supplement = real_calls.source_locations(self.root, calls, review, identity)
            report['source_identity'].update(supplement)
            report['real_calls'] = {'case_results': [], 'per_language': {}}
        return report

    def write_experiment(self, kind, report):
        filename, task = ('engine-comparison.json', 'T007') if kind == 'engine' else ('capacity-profile.json', 'T008')
        path = 'evaluations/results/code-understanding/' + filename
        analysis.write_result(self.root, path, report, 1024 * 1024)
        aggregate = {'tasks': {task: {'artifact': path, 'artifact_sha256': gate.digest((self.root / path).read_bytes()),
                                   'status': report['status']}}}
        analysis.write_result(self.root, 'evaluations/results/code-understanding/engine.json', aggregate, 1024 * 1024)

    def test_stale_report_and_dirty_implementation_cannot_pass(self):
        report = self.experiment('acceleration')
        self.write_experiment('acceleration', report)
        path = self.root / 'evaluations/results/code-understanding/capacity-profile.json'
        path.write_bytes(path.read_bytes() + b' ')
        with patch.object(gate, 'committed', return_value=True):
            result = gate.experiment_gate('acceleration', self.root)
        failed = {c['id'] for c in result['case_results'] if c['status'] == 'failed'}
        self.assertIn('claim_artifact_binding', failed)
        self.write_experiment('acceleration', report)
        with (self.root / 'evaluations/analysis.py').open('ab') as stream:
            stream.write(b'\n# changed after experiment\n')
        with patch.object(gate, 'committed', return_value=True):
            result = gate.experiment_gate('acceleration', self.root)
        self.assertIn('committed_experiment_implementation', {c['id'] for c in result['case_results'] if c['status'] == 'failed'})
        self.assertEqual(result['status'], 'blocked')

    def test_trial_labels_and_locked_budget_do_not_replace_measurements(self):
        cases = [{'id': f'{corpus}:{engine}:{run}', 'status': 'passed', 'corpus': corpus,
                  'engine': engine, 'repeat': run, 'revision': gate.PINS[corpus],
                  'exit_code': 0, 'identity_verified': True, 'result': {'records': []}}
                 for corpus in ('django', 'odoo', 'aws', 'kubernetes')
                 for engine in ('current-map', 'tree-sitter') for run in range(3)]
        report = self.experiment('acceleration', cases)
        self.write_experiment('acceleration', report)
        with patch.object(gate, 'committed', return_value=True):
            result = gate.experiment_gate('acceleration', self.root)
        failed = {c['id'] for c in result['case_results'] if c['status'] == 'failed'}
        self.assertIn('measured_trials', failed)
        self.assertIn('reference_workload_available', failed)
        self.assertEqual(result['status'], 'blocked')
        # Even plausible resource counters cannot establish equivalent facts,
        # update/query work or reference budgets by changing a status label.
        for row in cases:
            row['result']['records'] = [{'run': name, 'status': 'complete', 'wall_seconds': 1,
                'peak_rss_bytes': 1, 'counts': {'inventoried_files': 1},
                'semantic_facts_sha256': '0' * 64, 'input_inventory_sha256': '0' * 64}
                for name in ('fresh-output', 'unchanged-repeat')]
        report['case_results'] = cases
        self.write_experiment('acceleration', report)
        with patch.object(gate, 'committed', return_value=True):
            result = gate.experiment_gate('acceleration', self.root)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('reference_workload_available', {c['id'] for c in result['case_results'] if c['status'] == 'failed'})
        cases[0]['corpus'], cases[0]['revision'] = 'odoo', gate.PINS['odoo']
        self.write_experiment('acceleration', report)
        with patch.object(gate, 'committed', return_value=True):
            result = gate.experiment_gate('acceleration', self.root)
        self.assertIn('measured_trials', {c['id'] for c in result['case_results'] if c['status'] == 'failed'})

    def test_engine_labels_cannot_replace_frozen_real_cases(self):
        required = ('syntax_direct_binding', 'reusable_source_screen', 'real_call_quality',
                    'finite_worker_lifecycle', 'evidence_uncertainty', 'incremental_equivalence',
                    'bounded_query_work', 'optional_installation')
        report = self.experiment('engine', [{'id': name, 'status': 'passed'} for name in required])
        self.write_experiment('engine', report)
        with patch.object(gate, 'committed', return_value=True), patch.object(real_calls, 'committed', return_value=True):
            result = gate.experiment_gate('engine', self.root)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('experiment_evidence_available', {c['id'] for c in result['case_results'] if c['status'] == 'failed'})
        calls = json.loads((self.root / gate.INPUTS / 'real-calls.json').read_text())['cases']
        judgments = {j['id']: j for j in json.loads((self.root / gate.INPUTS / 'source-review.json').read_text())['judgments']}
        # All frozen cases are present, but fabricated perfect metrics contradict
        # the unresolved facts. The validator must recompute instead of trust them.
        report['real_calls']['case_results'] = [dict(
            {k: c[k] for k in ('id', 'repository_id', 'revision', 'path', 'language', 'file_sha256', 'range')},
            supported=judgments[c['id']]['supported'], expected_certainty=judgments[c['id']]['certainty'],
            reviewed_targets=judgments[c['id']]['targets'], status='passed', outcome='supported', target_bindings=[],
            actual_sites=[{'role': 'call', 'path': c['path'], 'range': {
                'start_byte': c['range']['utf8_bytes']['start'], 'end_byte': c['range']['utf8_bytes']['end_exclusive']},
                'provenance': {'source_sha256': c['file_sha256']}, 'certainty': 'unresolved', 'targets': [],
                'reason': 'Synthetic negative, not a measurement'}]) for c in calls]
        for language in gate.LANGUAGES:
            count = sum(c['language'] == language and judgments[c['id']]['supported'] for c in calls)
            report['real_calls']['per_language'][language] = {'supported_denominator': count,
                'supported_correct': count, 'supported_ungraded': 0, 'selected_supported_precision': 1,
                'selected_supported_recall_lower_bound': 1}
        self.write_experiment('engine', report)
        with patch.object(gate, 'committed', return_value=True), patch.object(real_calls, 'committed', return_value=True):
            result = gate.experiment_gate('engine', self.root)
        failed = {c['id'] for c in result['case_results'] if c['status'] == 'failed'}
        self.assertIn('real_call_measurements', failed)
        self.assertIn('qualified_adapter_available', failed)
        self.assertEqual(result['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
