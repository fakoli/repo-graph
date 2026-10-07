"""Synthetic validator negatives; these are not independent human judgments."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluations import acceptance as gate


class FreezeInputs(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
