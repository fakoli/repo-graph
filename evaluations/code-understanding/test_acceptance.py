"""Synthetic validator negatives; these are not independent human judgments."""
import contextlib
import copy
import hashlib
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
from evaluations import analysis, real_calls, queued_collector
from repo_graph.source import SourceRoot


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
    def test_impact_aggregate_refuses_failed_foreign_and_stale_child_evidence(self):
        fixture, frozen = analysis.frozen_inputs(gate.ROOT)
        original = json.loads((gate.ROOT / 'evaluations/results/code-understanding/views.json').read_text())
        original['tasks']['T045'] = {'status': 'passed', 'qualification_complete': False, 'task_accepted': False,
            'source_identity': copy.deepcopy(original['tasks']['T044']['source_identity']),
            'case_results': [{'id': name, 'status': 'passed'} for name in gate.IMPACT_BROWSER_CASES]}
        # Synthetic aggregate-validation inputs; no browser/producer success is inferred.
        for mutation, expected in (('failed', 'T044:cases'), ('foreign', 'T043:frozen_inputs'),
                                   ('stale', 'T045:current_source')):
            report = copy.deepcopy(original)
            if mutation == 'failed': report['tasks']['T044']['case_results'][0]['status'] = 'failed'
            elif mutation == 'foreign': report['tasks']['T043']['source_identity']['inputs'] = {}
            else:
                report['tasks']['T045']['source_identity']['implementation']['sha256']['repo_graph/analysis_queries.py'] = '0' * 64
            with self.subTest(mutation=mutation), patch.object(analysis, 'frozen_inputs', return_value=(fixture, frozen)), \
                    patch.object(gate, 'read_json', return_value=(report, '0' * 64)), \
                    patch.object(gate, 'committed', return_value=True), patch.object(analysis, 'record_view'):
                result = gate.impact_gate()
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(next(row for row in result['case_results'] if row['id'] == expected)['status'], 'failed')
                self.assertFalse(result['task_accepted']); self.assertFalse(result['human_ux_qualified'])

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


def cost_sha(raw):
    return hashlib.sha256(raw).hexdigest()

def cost_encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()

def cost_identity(pid):
    return dict(pid=pid, starttime_ticks=pid + 1000, pgid=pid, sid=pid)

def cost_telemetry(mode='serial', concurrency=1):
    ctrl, supervisor = (cost_identity(100), cost_identity(200))
    lives = [dict(identity=i, role=r, registered_ns=1, removed_ns=10000, removal_scope='sampling window ended; process exit not claimed') for i, r in [(ctrl, 'controller'), (supervisor, 'supervisor')]]
    events, samples, phases = ([], [], {})
    for n, label in enumerate(gate._COST_PHASES):
        t = 100 + n * 100
        worker = cost_identity(300 + n)
        live = dict(identity=worker, role='worker', registered_ns=t + 2, removed_ns=t + 8)
        lives.append(live)
        for name, time, index, owner in [('readiness', t, None, None), ('readiness', t + 1, None, worker), ('submit', t + 3, 0, worker), ('receive', t + 5, 0, worker), ('cleanup', t + 6, None, worker)]:
            event = dict(schema_version=1, event=name, monotonic_ns=time, mode=mode, role='worker' if owner else 'controller', configured_concurrency=concurrency, workers_started=1 if owner else 0, live_workers=1 if owner else 0, pending_requests=int(name == 'submit'), inflight_reserved_bytes=1, mailbox_source_bytes=1, mailbox_request_bytes=1, mailbox_result_bytes=1, admitted_bytes=1, controller=ctrl, worker=owner, index=index, cleanup=dict(leader_reaped=True, group_absent=True, mailboxes_removed=True) if name == 'cleanup' else None, phase=label)
            events.append(event)
        values = [dict(pid=i['pid'], rss_bytes=64, read_started_ns=t + 4, read_ended_ns=t + 4) for i in (ctrl, supervisor, worker)]
        samples.append(dict(started_ns=t + 4, ended_ns=t + 4, read_skew_ns=0, phase=label, owners=values, gaps=[], complete=True, owned_rss_bytes=192))
        resource = dict(elapsed_seconds=0.002, process_peak_rss_bytes=64, process_user_seconds=0.001, process_system_seconds=0, file_user_seconds=0.001, file_system_seconds=0, timings={k: 0 for k in queued_collector.NATIVE_TIMINGS})
        queued = dict(workers_started=1, elapsed_seconds=0.003, limits=dict(memory_bytes=512 * 1024 ** 2, cpu_seconds=30, total_wall_seconds=20, worker_wall_seconds=20, max_inflight_bytes=40 * 1024 ** 2, max_admitted_bytes=32 * 1024 ** 2), worker_resources=[[resource]], telemetry=dict(schema_version=1, controller_timings={k: 0 for k in queued_collector.CONTROLLER_TIMINGS}, controller_identity=ctrl, observer_events_delivered=5, observer_failed=False, observer_failure_reason=None, actual_workers_started=1, worker_process_identities=[worker]))
        phases[label] = dict(label=label, status='complete', wall_seconds=0.01, proof_retention_seconds=0.001, observed_attempt_seconds=0.011, stages={k: dict(calls=1, inclusive_seconds=0.001) for k in ('source_read', 'collection_controller', 'global_resolution', 'snapshot_construction')}, receipt=dict(resources=dict(queued=queued)))
    lines = [dict(kind='lifecycle', value={k: None if k == 'removed_ns' else v for k, v in row.items() if k != 'removal_scope'}) for row in lives]
    lines += [dict(kind='event', value=e) for e in events] + [dict(kind='sample', value=s) for s in samples]
    data = b''.join((cost_encoded(r) + b'\n' for r in lines))
    rss = dict(schema_version=1, label='peak_sampled_owned_rss_bytes', peak_sampled_owned_rss_bytes=192, sample_window=dict(started_ns=1, ended_ns=10000), requested_interval_seconds=0.025, max_samples=4000, max_live_owners=6, max_lifetime_owners=64, lifecycles=lives, max_log_bytes=8 * 1024 ** 2, retained_log_bytes=len(data), sample_count=8, largest_start_interval_ns=100, complete_sample_count=8, sample_gap_count=0, max_read_skew_ns=0, samples=samples, queue_events=events, controller=ctrl, remaining_registered_worker_owners=[], sampler_stopped=True, error=None, unsampled_peak_bound=False)
    job = dict(mode=mode, concurrency=concurrency, owned_rss={k: v for k, v in rss.items() if k not in ('samples', 'queue_events')}, owned_rss_artifact=dict(path='owned-rss.json', sha256=cost_sha(cost_encoded(rss)), bytes=len(cost_encoded(rss))), owned_telemetry_artifact=dict(path='owned-telemetry.jsonl', sha256=cost_sha(data), bytes=len(data)))

    def read(path, parsed=True):
        return rss if path == 'owned-rss.json' else data
    return (job, read, phases, rss, data)

class CostProofTests(unittest.TestCase):

    def test_eight_phase_current_rss_and_typed_telemetry(self):
        for mode, count in gate._COST_MODES:
            job, read, phases, _, _ = cost_telemetry(mode, count)
            self.assertEqual(gate._proof_cost_rss(job, read, phases), dict(peak_sampled_owned_rss_bytes=192, complete_sample_count=8, sample_gap_count=0))

    def test_registry_clock_is_distinct_from_waiting_producer_clock(self):
        job, _, phases, rss, _ = cost_telemetry()
        worker = cost_identity(300)
        first = rss['samples'][0]
        additions = []
        for stamp, live in [(101, False), (107, True), (108, False)]:
            owners = [dict(v, read_started_ns=stamp, read_ended_ns=stamp) for v in first['owners'] if live or v['pid'] != worker['pid']]
            additions.append(dict(first, started_ns=stamp, ended_ns=stamp, owners=owners, owned_rss_bytes=sum((v['rss_bytes'] for v in owners))))
        rss['samples'] = sorted(rss['samples'] + additions, key=lambda r: r['started_ns'])

        def bind():
            lines = [dict(kind='lifecycle', value={k: None if k == 'removed_ns' else v for k, v in row.items() if k != 'removal_scope'}) for row in rss['lifecycles']]
            lines += [dict(kind='event', value=e) for e in rss['queue_events']] + [dict(kind='sample', value=r) for r in rss['samples']]
            data = b''.join((cost_encoded(r) + b'\n' for r in lines))
            rss.update(retained_log_bytes=len(data), sample_count=11, complete_sample_count=11)
            job['owned_rss'] = {k: v for k, v in rss.items() if k not in ('samples', 'queue_events')}
            return lambda path, parsed=True: rss if path == 'owned-rss.json' else data
        self.assertEqual(gate._proof_cost_rss(job, bind(), phases)['complete_sample_count'], 11)
        rss['lifecycles'][2]['registered_ns'] = 104
        with self.assertRaises(ValueError):
            gate._proof_cost_rss(job, bind(), phases)

    def test_peak_label_cannot_substitute_lifetime_maxima(self):
        job, read, phases, rss, _ = cost_telemetry()
        rss['peak_sampled_owned_rss_bytes'] = 999
        job['owned_rss']['peak_sampled_owned_rss_bytes'] = 999
        with self.assertRaises(ValueError):
            gate._proof_cost_rss(job, read, phases)

    def test_unregistered_stale_identity_and_cleanup_refusal(self):
        for field, value in [('identity', 9999), ('cleanup', False)]:
            job, read, phases, rss, _ = cost_telemetry()
            if field == 'identity':
                rss['queue_events'][2]['worker'] = dict(rss['queue_events'][2]['worker'], starttime_ticks=value)
            else:
                rss['queue_events'][4]['cleanup']['group_absent'] = value
            with self.assertRaises(ValueError):
                gate._proof_cost_rss(job, read, phases)

    def test_resource_cpu_delta_observer_failure_and_bool_rejected(self):
        for kind in ('cpu', 'observer', 'bool', 'infinity'):
            _, _, phases, _, _ = cost_telemetry()
            phase = phases['fresh-output']
            queue = phase['receipt']['resources']['queued']
            if kind == 'cpu':
                queue['worker_resources'][0][0]['file_user_seconds'] = 1
            elif kind == 'observer':
                queue['telemetry']['observer_failed'] = True
            elif kind == 'bool':
                queue['worker_resources'][0][0]['timings']['parse_seconds'] = True
            else:
                phase['wall_seconds'] = float('inf')
            with self.assertRaises(ValueError):
                gate._proof_cost_timings(phase)

    def test_missing_cost_and_status_fabrication_fail_closed(self):
        proof = gate.preselection_cost_proof_checks({'preselection_cost': {'status': 'complete', 'cases': [{'id': 'SYNTHETIC_PRIVATE_ID', 'status': 'complete'}]}})
        self.assertEqual(proof['status'], 'blocked')
        self.assertEqual(proof['individual_results'], [])
        self.assertEqual(proof['observed_records'], [dict(id='cost:0', status='complete')])
        self.assertNotIn('SYNTHETIC_PRIVATE_ID', json.dumps(proof))

    def test_projection_private_strings_fail_closed(self):
        wrapper = dict(status='failed', cases=[dict(id='SYNTHETIC_PRIVATE_ID', mode='synthetic.invalid', concurrency=1, repeat=0, status='failed')])
        with self.assertRaises(ValueError):
            analysis.compact_preselection_cost(wrapper)
        wrapper['cases'][0]['mode'] = 'serial'
        safe = analysis.compact_preselection_cost(wrapper)
        self.assertNotIn('SYNTHETIC_PRIVATE_ID', json.dumps(safe))
        self.assertEqual(safe['cases'][0]['id'], 'serial-1-0')

    def test_outer_invocation_requires_actual_wall_exit_and_bound_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            directory = evidence / ('invocation-' + '1' * 32)
            directory.mkdir()
            with SourceRoot(directory) as source:
                owner = source.identity
            supervisor = cost_identity(200)
            full = dict(binding_before={}, binding_after={}, supervisor_envelope=dict(process_identity=supervisor))
            wrapper = dict(full_private_report=full, archive=dict(directory='native-dual-' + '2' * 32, files=[], bytes=0))
            stdout = cost_encoded(wrapper)
            stderr = b''
            raw = dict(schema_version=1, kind='private_native_dual_invocation', status='complete', directory=directory.name, evidence_owner_identity=owner, wrapper_sha256=gate._COST_INVOCATION_SHA256, wrapper_identity_stable=True, engine_selected=False, qualification_complete=False, source_after_unavailable=False, admitted_wall_exhausted=False, attempts_started=1, returncode=0, wall_seconds=90, measurement_elapsed_seconds=1, elapsed_seconds=1.01, teardown_seconds=0.01, wrapper_isolation=dict(private_cwd=True, private_environment_allowlist=True, isolated_python=True, bytecode_disabled=True), cleanup=dict(signals=[], leader_reaped=True, group_absent=True, returncode=0), process_identity=supervisor, source_before={}, source_after={}, logs={}, supervisor_result=dict(status='complete', kind='native_dual_fixture_profile', cases_retained=9, archive_directory=wrapper['archive']['directory'], archive_bytes=0, stdout_sha256=cost_sha(stdout), stdout_bytes=len(stdout)))
            for name, payload in [('stdout', stdout), ('stderr', stderr)]:
                raw['logs'][name] = dict(path=name + '.log', overflow=False, complete=True, sha256=cost_sha(payload), bytes=len(payload), bytes_received=len(payload), bytes_retained=len(payload))
                (directory / (name + '.log')).write_bytes(payload)

            def bind():
                (directory / 'invocation.json').write_bytes(cost_encoded(raw))
                refs = [dict(path=n, sha256=cost_sha((directory / n).read_bytes()), bytes=len((directory / n).read_bytes())) for n in ('invocation.json', 'stdout.log', 'stderr.log')]
                return dict(invocation=analysis.compact_cost_invocation(raw, directory.name, refs))
            self.assertEqual(gate._proof_cost_invocation(bind(), evidence)[2]['status'], 'complete')
            raw['measurement_elapsed_seconds'] = 90.01
            raw['elapsed_seconds'] = 90.02
            with self.assertRaises(ValueError):
                gate._proof_cost_invocation(bind(), evidence)
            raw.update(measurement_elapsed_seconds=1, elapsed_seconds=1.01, failure=dict(kind='RuntimeError', message='SYNTHETIC_PRIVATE_MESSAGE'))
            portable = bind()
            self.assertNotIn('SYNTHETIC_PRIVATE_MESSAGE', json.dumps(portable))
            with self.assertRaises(ValueError):
                gate._proof_cost_invocation(portable, evidence)
            del raw['failure']
            raw['logs']['stderr']['overflow'] = True
            with self.assertRaises(ValueError):
                gate._proof_cost_invocation(bind(), evidence)

    def test_no_owner_from_passed_labels_or_late_proof_failure(self):
        names = ('syntax_direct_binding', 'reusable_source_screen', 'real_call_quality', 'finite_worker_lifecycle', 'evidence_uncertainty', 'incremental_equivalence', 'bounded_query_work', 'optional_installation')
        report = dict(case_results=[dict(id=n, status='passed') for n in names], implementation=dict(commit='a' * 40, sha256={p: 'a' * 64 for p in gate._DELIVERY_EXPERIMENT_PATHS}))
        with patch.object(gate, 'adapter_proof_checks', return_value=dict(status='passed', individual_results={'updates': [dict(status='passed')]})), patch.object(gate, 'preselection_cost_proof_checks', return_value=dict(status='blocked', individual_results=[dict(id='serial-1-0', status='failed')])), patch.object(gate, '_proof_real_call_quality', return_value=dict(source_binding=True, measurements=True)), patch.object(gate, '_proof_component_basics', return_value=dict(syntax=True, screen=True, lifecycle=True)):
            result = gate.component_selection_decision(report)
        self.assertFalse(result['engine_selected'])
        self.assertIsNone(result['selected_owner'])
        self.assertEqual(result['proofs']['finite_cost']['individual_results'][0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
