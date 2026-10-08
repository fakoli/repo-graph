"""Focused source boundary and grading checks for the selected real-call helper."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluations import real_calls
from evaluations.tree_sitter_baseline import Budget, scan

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in (
    'tree_sitter', 'tree_sitter_python', 'tree_sitter_go',
    'tree_sitter_javascript', 'tree_sitter_typescript'))
RAW = b'def target():\n    pass\n\ndef caller():\n    target()\n'
PIN = 'a' * 40
SHA = hashlib.sha256(RAW).hexdigest()


def evidence(repository, start, end, role):
    return {'repository_id': repository, 'revision': PIN, 'path': 'main.py',
            'file_sha256': SHA, 'range': {'utf8_bytes': {'start': start, 'end_exclusive': end}},
            'role': role, 'excerpt_sha256': hashlib.sha256(RAW[start:end]).hexdigest()}


def case(repository='left'):
    start = RAW.rindex(b'target()')
    return {'id': repository + '-call', 'repository_id': repository, 'revision': PIN,
            'path': 'main.py', 'language': 'python', 'file_sha256': SHA, 'file_bytes': len(RAW),
            'range': {'utf8_bytes': {'start': start, 'end_exclusive': start + len(b'target()')}},
            'proposed_target_keys_unreviewed': ['DO_NOT_PASS_TO_SCAN'],
            'proposed_resolution_state_unreviewed': 'DO_NOT_PASS_TO_SCAN'}


def judgment(record, target_repository=None, wide=False, certainty='resolved'):
    start, end = real_calls.bounds(record)
    repository = target_repository or record['repository_id']
    return {'id': record['id'], 'targets': [repository + ':main.py#target'],
            'certainty': certainty, 'supported': certainty == 'resolved',
            'evidence': [evidence(record['repository_id'], start, end, 'candidate_call_site'),
                         evidence(repository, 0, len(RAW) if wide else RAW.index(b'\n\n'),
                                  'binding_containing_scope_or_target_definition')]}


@unittest.skipUnless(AVAILABLE, 'Optional analysis backend is absent')
class RealCallTests(unittest.TestCase):
    def extract(self, scratch, cases, judgments):
        roots = {}
        for repository in ('left', 'right'):
            root = Path(scratch) / repository
            root.mkdir(exist_ok=True)
            (root / 'main.py').write_bytes(RAW)
            roots[repository] = {'id': repository, 'source': str(root), 'revision': PIN}
        with patch.object(real_calls, 'checkout_identity', return_value={'status': 'verified'}), \
                patch.object(real_calls, 'scan', wraps=scan) as observed:
            runs, blobs = real_calls.extract_selected(real_calls.inventory(cases, judgments), roots, Budget())
        for call in observed.call_args_list:
            for record in call.args[1]:
                self.assertEqual(set(record), {'path', 'language', 'kind', 'sha256', 'bytes'})
                self.assertNotIn('DO_NOT_PASS_TO_SCAN', json.dumps(record))
        return runs, blobs

    def test_exact_target_identity_and_repository_ownership(self):
        left, right = case(), case('right')
        reviews = [judgment(left), judgment(right)]
        with tempfile.TemporaryDirectory() as scratch:
            runs, blobs = self.extract(scratch, [left, right], reviews)
            results = real_calls.grade_cases([left, right], reviews, runs, blobs)
            self.assertEqual([item['outcome'] for item in results], ['supported', 'supported'])
            # Identical path, bytes, name, and definition ID in another repository
            # cannot turn a local emitted target into the reviewed foreign target.
            foreign = judgment(left, target_repository='right')
            result = real_calls.grade_cases([left], [foreign], runs, blobs)[0]
            self.assertEqual(result['status'], 'ungraded')
            self.assertEqual(result['outcome'], 'target_evidence_gap')
            self.assertEqual(result['actual_sites'][0]['target_definitions'][0]['repository_id'], 'left')
            self.assertNotIn('text', json.dumps(result))
            # With an exact independently reviewed target anchor available, a
            # different emitted target is a failure, not an evidence gap.
            site = next(item for item in runs['left']['facts']['sites'] if item['role'] == 'call')
            other = next(item for item in runs['left']['facts']['definitions'] if item['name'] == 'caller')
            site['targets'] = [other['id']]
            result = real_calls.grade_cases([left], [reviews[0]], runs, blobs)[0]
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['outcome'], 'wrong')

    def test_broad_gold_excerpt_missing_dependency_and_candidate_recall(self):
        selected = case()
        with tempfile.TemporaryDirectory() as scratch:
            runs, blobs = self.extract(scratch, [selected], [judgment(selected)])
            broad = judgment(selected, wide=True)
            result = real_calls.grade_cases([selected], [broad], runs, blobs)[0]
            self.assertEqual(result['status'], 'ungraded')
            self.assertEqual(result['target_bindings'][0]['reason'], 'reviewed_definition_range_not_exact_or_ambiguous')
            missing = judgment(selected, target_repository='dependency')
            result = real_calls.grade_cases([selected], [missing], runs, blobs)[0]
            self.assertTrue(result['supported'])
            self.assertEqual(result['target_bindings'][0]['reason'], 'target_source_unavailable')
            candidate = judgment(selected, certainty='candidate')
            site = next(item for item in runs['left']['facts']['sites'] if item['role'] == 'call')
            site.update(certainty='unresolved', targets=[], targets_exhaustive=False, reason='parameter value')
            result = real_calls.grade_cases([selected], [candidate], runs, blobs)[0]
            self.assertEqual(result['outcome'], 'candidate_unresolved')
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['candidate_recall']['status'], 'failed')
            self.assertEqual(result['candidate_recall']['missing_or_ungraded_reviewed_targets'], ['left:main.py#target'])
            runs['left']['facts']['sites'] = []
            result = real_calls.grade_cases([selected], [candidate], runs, blobs)[0]
            self.assertEqual(result['outcome'], 'missing')
            self.assertEqual(result['candidate_recall']['status'], 'failed')
            candidate['evidence'][0]['excerpt_sha256'] = '0' * 64
            result = real_calls.grade_cases([selected], [candidate], runs, blobs)[0]
            self.assertEqual(result['status'], 'ungraded')
            self.assertEqual(result['candidate_recall']['status'], 'failed')

    def test_complete_hash_verification_blocks_modified_source_before_scan(self):
        selected, reviewed = case(), judgment(case())
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'main.py').write_bytes(RAW + b'# changed\n')
            roots = {'left': {'id': 'left', 'source': str(root), 'revision': PIN}}
            with patch.object(real_calls, 'checkout_identity', return_value={'status': 'verified'}), \
                    patch.object(real_calls, 'scan') as observed:
                runs, blobs = real_calls.extract_selected(real_calls.inventory([selected], [reviewed]), roots, Budget())
            observed.assert_not_called()
            self.assertEqual(blobs, {})
            self.assertEqual(runs['left']['inventory'][0]['status'], 'full_source_identity_mismatch')
            self.assertNotIn(scratch, json.dumps(runs))

    def test_supplemental_physical_anchor_never_rewards_a_bare_name(self):
        from copy import deepcopy
        selected, reviewed = case(), judgment(case(), wide=True)
        end = RAW.index(b'\n\n')
        declaration = {'start_byte': 0, 'end_byte': end, 'start_line': 1, 'end_line': 2}
        anchor = {'start_byte': 4, 'end_byte': 10, 'start_line': 1, 'end_line': 1}
        location = {'revision': PIN, 'file_sha256': SHA, 'name': 'target',
            'lexical_named_ancestors': [], 'declaration_range': declaration, 'name_range': anchor,
            'range_sha256': {'declaration_range': hashlib.sha256(RAW[:end]).hexdigest(),
                             'name_range': hashlib.sha256(RAW[4:10]).hexdigest()}}
        locations = {(selected['id'], reviewed['targets'][0]): location}
        with tempfile.TemporaryDirectory() as scratch:
            runs, blobs = self.extract(scratch, [selected], [reviewed])
            result = real_calls.grade_cases([selected], [reviewed], runs, blobs, locations)[0]
            self.assertEqual(result['outcome'], 'supported')
            # Source hashes alone do not excuse a bad declaration boundary.
            wrong = deepcopy(location)
            wrong['declaration_range']['end_byte'] += 1
            bad = {(selected['id'], reviewed['targets'][0]): wrong}
            result = real_calls.grade_cases([selected], [reviewed], runs, blobs, bad)[0]
            self.assertEqual(result['target_bindings'][0]['status'], 'ungraded')
            # Correct source anchor, but engine span or ancestry is wrong.
            definition = next(d for d in runs['left']['facts']['definitions'] if d['name'] == 'target')
            definition['name'] = 'invented.target'
            result = real_calls.grade_cases([selected], [reviewed], runs, blobs, locations)[0]
            self.assertEqual(result['outcome'], 'missing_target_definition')
            self.assertEqual(result['status'], 'failed')

    def test_uncommitted_supplement_and_source_owned_output_are_rejected(self):
        with real_calls.SourceRoot(ROOT) as source:
            calls, _ = real_calls.read_json(source, real_calls.INPUTS + 'real-calls.json')
            reviewed, _ = real_calls.read_json(source, real_calls.INPUTS + 'source-review.json')
        _, identity = real_calls.frozen_inputs(ROOT)
        with patch.object(real_calls, 'committed', return_value=False):
            with self.assertRaisesRegex(ValueError, 'committed'):
                real_calls.source_locations(ROOT, calls, reviewed, identity)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            corpus = root / 'source'
            corpus.mkdir()
            canary = corpus / 'canary.json'
            canary.write_text('unchanged')
            mapping = root / 'map.json'
            mapping.write_text(json.dumps({'corpora': [{'source': str(corpus)}]}))
            report = {'source_map_sha256': hashlib.sha256(mapping.read_bytes()).hexdigest()}
            with self.assertRaisesRegex(ValueError, 'outside source'):
                real_calls.write_comparison(canary, report, mapping)
            self.assertEqual(canary.read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
