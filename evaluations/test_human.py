"""Synthetic protocol/capture checks; these are not human study observations."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluations import human


class HumanProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = human.validate_protocol()

    def test_frozen_protocol_validation_does_not_claim_execution_or_eligibility(self):
        result = self.result
        self.assertEqual(result['status'], 'passed', result['case_results'])
        self.assertEqual(result['study_execution_status'], 'not_run')
        self.assertEqual((result['participants_enrolled'], result['participants_run']), (0, 0))
        self.assertFalse(result['human_evaluation']); self.assertFalse(result['qualification_complete'])
        self.assertFalse(result['task_accepted'])
        self.assertEqual(result['study_eligibility']['status'], 'blocked')
        self.assertEqual(result['study_eligibility']['upstream_acceptance'], 'not_evaluated')
        protocol = result['protocol']; questions = protocol['questions']
        self.assertEqual([q['question_id'] for q in questions], ['DJ-Q-ENTRYPOINTS', 'OD-Q-SALES',
            'OD-Q-INVENTORY', 'OD-Q-ACCOUNTING', 'Q-PY-UNCERTAINTY', 'DJ-Q-UNKNOWN',
            'OD-Q-IDENTITY-AND-OWNERSHIP', 'CT-Q-POSITIVE', 'CT-Q-UNKNOWNS'])
        self.assertTrue(all(q['case_ids'] and len(q['grading_key_sha256']) == 64 for q in questions))
        self.assertTrue(all(len(result[k]) == 64 for k in ('config_identity', 'analyzer_identity')))
        self.assertEqual(set(result['implementation']), set(human.IMPLEMENTATION))
        self.assertFalse(protocol['equal_source']['source_access_verified'])
        self.assertTrue(all(v is None for view in ('current', 'new') for v in protocol['equal_source'][view].values()))
        self.assertFalse(protocol['independent_grading']['human_assessor_assigned'])
        self.assertEqual(protocol['participants']['models_as_participants'], 'forbidden')
        self.assertEqual(protocol['counterbalance']['view_counts'], {'current': 23, 'new': 22})
        self.assertEqual(sum(sum(row.values()) for row in protocol['counterbalance']['category_view_counts'].values()), 45)
        self.assertTrue(all(sum(row.values()) == 5 for row in protocol['counterbalance']['order_view_counts'].values()))
        self.assertIsNone(protocol['proposed_targets']['measured_outcomes'])
        self.assertEqual(protocol['counterbalance']['actual_assignments'], [])

    def test_parity_and_rotation_do_not_repeat_questions_and_retain_three_two_imbalance(self):
        questions = self.result['protocol']['questions']; plan = human.allocation(questions)
        expected = [q['question_id'] for q in questions]
        for slot in range(5):
            rows = [row for row in plan if row['slot'] == slot]
            self.assertEqual([r['question_id'] for r in rows], expected[slot:] + expected[:slot])
            self.assertEqual([r['order'] for r in rows], list(range(1, 10)))
            for row in rows:
                index = expected.index(row['question_id'])
                self.assertEqual(row['condition'], ('current', 'new')[(slot + index) % 2])
        for question in expected:
            views = [r['condition'] for r in plan if r['question_id'] == question]
            self.assertEqual(sorted([views.count('current'), views.count('new')]), [2, 3])
        even = human.allocation(questions, 6)
        self.assertTrue(all(sum(r['condition'] == 'current' and r['question_id'] == q for r in even) == 3 for q in expected))
        for invalid in (True, 4, 5.0, 101):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                human.allocation(questions, invalid)

    def test_changed_duplicate_or_unsafe_frozen_input_is_refused_before_committed_check(self):
        for content in (b'{"questions":[]}', b'{"schema_version":1,"schema_version":1}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch); directory = root / human.INPUTS; directory.mkdir(parents=True)
                (directory / 'fixtures.json').write_bytes(content)
                with patch.object(human, 'committed') as committed:
                    result = human.validate_protocol(root)
                committed.assert_not_called(); self.assertEqual(result['status'], 'failed')
                self.assertIsNone(result['source_identity']); self.assertNotIn('protocol', result)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch); directory = root / human.INPUTS; directory.mkdir(parents=True)
            outside = root / 'untrusted.json'; outside.write_text('{}')
            (directory / 'fixtures.json').symlink_to(outside)
            result = human.validate_protocol(root)
            self.assertEqual(result['status'], 'failed'); self.assertIsNone(result['source_identity'])

    def private_capture(self):
        question = self.result['protocol']['questions'][4]
        record = dict(participant_id='synthetic-person', question_id=question['question_id'], condition='current',
            planned_order=1, actual_order=1, input_sha256=question['input_sha256'], source_manifest_sha256='a' * 64,
            workflow=dict(build_commit='b' * 40, config_identity='c' * 64, analyzer_identity='d' * 64, snapshot='e' * 64),
            prompt_ready_utc='2026-01-01T00:00:00+00:00', start_utc='2026-01-01T00:00:01+00:00',
            end_utc='2026-01-01T00:00:11+00:00', prompt_ready_monotonic=0.0, start_monotonic=1.0,
            end_monotonic=11.0, elapsed_seconds=10.0, outcome='submitted', answer='synthetic private answer canary',
            citations=['synthetic private source citation'], events=[dict(elapsed_seconds=2.0, kind='service_failure',
                status='failed', command='synthetic private command canary', exit_code=1)],
            grades=[dict(case_id=key, status='ungraded', omissions=0, wrong_targets=0, false_certainty=False)
                    for key in question['case_ids']], assessor_id='synthetic-grader', exclusion_reason=None)
        return record, question

    def test_private_capture_requires_actual_typed_times_errors_and_every_case_without_publishing_raw(self):
        record, question = self.private_capture()
        self.assertTrue(human.valid_capture(record, question))
        controls = [('elapsed_seconds', -1), ('elapsed_seconds', True), ('elapsed_seconds', 9),
            ('start_monotonic', float('nan')), ('end_utc', '2026-01-01T00:00:11'),
            ('input_sha256', '0' * 64), ('answer', ''), ('grades', record['grades'][:-1]),
            ('grades', [record['grades'][0]] * len(record['grades']))]
        for field, value in controls:
            malformed = deepcopy(record); malformed[field] = value
            with self.subTest(field=field, value=value): self.assertFalse(human.valid_capture(malformed, question))
        malformed = deepcopy(record); malformed['events'][0]['elapsed_seconds'] = 12
        self.assertFalse(human.valid_capture(malformed, question))
        malformed = deepcopy(record); malformed['events'][0]['exit_code'] = True
        self.assertFalse(human.valid_capture(malformed, question))
        incomplete = deepcopy(record); incomplete.update(outcome='withdrawn', answer='')
        self.assertTrue(human.valid_capture(incomplete, question))
        excluded = deepcopy(incomplete); excluded['outcome'] = 'excluded'
        self.assertFalse(human.valid_capture(excluded, question))
        excluded['exclusion_reason'] = 'synthetic consent withdrawal'
        self.assertTrue(human.valid_capture(excluded, question))
        self.assertNotIn('synthetic private', json.dumps(self.result))

    def test_temporary_report_merge_preserves_other_gates_and_rejects_output_bound_and_symlink(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch); path = human.BUSINESS_OUTPUT
            original = {'schema_version': 1, 'tasks': {'T019': {'status': 'failed', 'retained': [1, 2, 3]}}}
            human.write_result(root, path, original, human.BUSINESS_RESULT_BYTES)
            human.write_report(root, path, self.result)
            saved = json.loads((root / path).read_text())
            self.assertEqual(saved['tasks']['T019'], original['tasks']['T019'])
            self.assertEqual(saved['tasks']['T051'], self.result)
            raw = (root / path).read_bytes()
            with patch.object(human, 'BUSINESS_RESULT_BYTES', 128), self.assertRaises(ValueError):
                human.write_report(root, 'too-large.json', self.result)
            self.assertFalse((root / 'too-large.json').exists()); self.assertEqual((root / path).read_bytes(), raw)
            (root / 'unsafe.json').symlink_to(root / path)
            with self.assertRaises(OSError): human.write_report(root, 'unsafe.json', self.result)
            self.assertEqual((root / path).read_bytes(), raw)

    def test_supported_entrypoint_writes_only_requested_temporary_protocol_report(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(human, 'ROOT', Path(scratch)), \
                patch.object(human, 'validate_protocol', return_value=self.result), redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(human.main(['--validate-protocol', '--output', 'protocol.json']), 0)
            result = json.loads((Path(scratch) / 'protocol.json').read_text())
            self.assertEqual(result, self.result)
            self.assertEqual(json.loads(stdout.getvalue())['study_execution_status'], 'not_run')
            self.assertFalse((Path(scratch) / human.BUSINESS_OUTPUT).exists())


if __name__ == '__main__':
    unittest.main()
