#!/usr/bin/env python3
"""Validate the frozen human protocol; no participants or scored study are run."""
import argparse
from datetime import datetime, timezone
import math
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluations.acceptance import canonical, committed, digest, identifier, read_json
from evaluations.analysis import BUSINESS_OUTPUT, BUSINESS_RESULT_BYTES, write_result
from repo_graph.source import SourceRoot

INPUTS = 'evaluations/code-understanding/'
FROZEN = {
    'fixtures.json': '6742295d0536cb1df577f04a48f893c1b962b184b278875415c20611cfa5f87e',
    'input-lock.json': '418f5274d6c1d4c6f596f48b1485518cf5ec59d74a6006b7b64706d2b4d442a1',
    'django-framework-inputs.json': '2348603f592660275f9a5750c18fe3e08c1679516333e7613fef9f78589f26c9',
    'django-framework-review.json': '93fadbc0202f5354890de3ee8a55670ca753b2f4e59ee42c0cf10898ddc86bbf',
    'odoo-framework-inputs.json': '8d1ecb870da5d346e4b774a821ef02c0f0ae91d010bdd8d07ddeedaed664b824',
    'odoo-framework-review.json': '4c22c8482fb05134d4fb4abf06e870378fab9aa33c110ca14585deb1cac32d8e',
    'contract-inputs.json': '404969d67cd2ba621e3355e3de4346c84d6179c653307ce4d211d10f3f967e96',
    'contract-review.json': '3060839500456a14e5494a840f265364e436cbf7f5ef2d57215ec3df3f8156c9',
}
SELECTION = (
    ('DJ-Q-ENTRYPOINTS', 'entrypoint', 'django-framework-inputs.json'),
    ('OD-Q-SALES', 'possible_path', 'odoo-framework-inputs.json'),
    ('OD-Q-INVENTORY', 'possible_path', 'odoo-framework-inputs.json'),
    ('OD-Q-ACCOUNTING', 'possible_path', 'odoo-framework-inputs.json'),
    ('Q-PY-UNCERTAINTY', 'uncertainty', 'fixtures.json'),
    ('DJ-Q-UNKNOWN', 'uncertainty', 'django-framework-inputs.json'),
    ('OD-Q-IDENTITY-AND-OWNERSHIP', 'uncertainty', 'odoo-framework-inputs.json'),
    ('CT-Q-POSITIVE', 'contract', 'contract-inputs.json'),
    ('CT-Q-UNKNOWNS', 'contract', 'contract-inputs.json'),
)
SELECTION_SHA = '1dc175f7ec1141f63814b4bc732839b7e2557d6d6f687e12a662548ac5ff1f86'
IMPLEMENTATION = ('evaluations/human.py', 'evaluations/test_human.py', 'evaluations/acceptance.py',
    'evaluations/analysis.py', 'repo_graph/source.py', 'pyproject.toml', 'uv.lock')
UPSTREAM = ('T017', 'T018', 'T019', 'T020', 'T021', 'T069')


def allocation(questions, slots=5):
    """Preview only: paired rotated orders with opposite views; each question once."""
    if type(slots) is not int or not 5 <= slots <= 100:
        raise ValueError('Participant slot count must be an integer from 5 to 100')
    return [dict(slot=slot, question_id=questions[index]['question_id'], order=order + 1,
                 condition='current' if (index + slot) % 2 == 0 else 'new')
        for slot in range(slots) for order in range(len(questions))
        for index in [(order + slot // 2) % len(questions)]]


def capture_schema():
    """Private task rows only; raw rows are never accepted by the public report writer."""
    return {
        'fields': ['participant_id', 'question_id', 'condition', 'planned_order', 'actual_order',
            'input_sha256', 'source_manifest_sha256', 'workflow', 'prompt_ready_utc', 'start_utc', 'end_utc',
            'prompt_ready_monotonic', 'start_monotonic', 'end_monotonic', 'elapsed_seconds',
            'outcome', 'answer', 'citations', 'events', 'grades', 'assessor_id', 'exclusion_reason'],
        'workflow_fields': ['build_commit', 'config_identity', 'analyzer_identity', 'snapshot'],
        'outcomes': ['submitted', 'incomplete', 'timeout', 'withdrawn', 'excluded'],
        'event_fields': ['elapsed_seconds', 'kind', 'status', 'command', 'exit_code'],
        'event_kinds': ['pause', 'resume', 'tool', 'service_failure', 'task_failure'],
        'grade_fields': ['case_id', 'status', 'omissions', 'wrong_targets', 'false_certainty'],
        'timing': 'Genuine UTC and same-process monotonic prompt-ready/start/end; elapsed includes browsing, pauses and failures.',
        'privacy': 'Contacts, consent, pseudonym mapping, raw answers/citations/events, recordings and assessor identity stay private.',
        'retention': 'Storage owner, consent and retention period must be approved before enrollment; identity map stored separately.',
        'grading': 'Independent human assessor checks original source and frozen key before outcomes; lock answers before per-case grading.',
        'denominators': 'Retain every attempted, incomplete, withdrawn, excluded and failed task and each deviation; never drop wrong answers.',
    }


def valid_capture(record, question):
    """Check a private row's structure/timing, without claiming human identity or grading truth."""
    schema = capture_schema()
    def number(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12
    def sha(value, lengths=(64,)):
        return type(value) is str and len(value) in lengths and re.fullmatch('[0-9a-f]+', value) is not None
    try:
        if (type(record) is not dict or set(record) != set(schema['fields']) or
                record['question_id'] != question['question_id'] or record['input_sha256'] != question['input_sha256'] or
                not identifier(record['participant_id']) or not identifier(record['assessor_id']) or
                record['condition'] not in ('current', 'new') or record['outcome'] not in schema['outcomes'] or
                any(type(record[k]) is not int or not 1 <= record[k] <= 9 for k in ('planned_order', 'actual_order')) or
                not sha(record['source_manifest_sha256'])):
            return False
        workflow = record['workflow']
        if (type(workflow) is not dict or set(workflow) != set(schema['workflow_fields']) or
                not sha(workflow['build_commit'], (40, 64)) or
                any(not sha(workflow[k]) for k in ('config_identity', 'analyzer_identity', 'snapshot'))):
            return False
        utc = [datetime.fromisoformat(record[k]) for k in ('prompt_ready_utc', 'start_utc', 'end_utc')]
        mono = [record[k] for k in ('prompt_ready_monotonic', 'start_monotonic', 'end_monotonic')]
        if (any(t.tzinfo is None or t.utcoffset() != timezone.utc.utcoffset(t) for t in utc) or
                not utc[0] <= utc[1] <= utc[2] or not all(number(t) for t in mono) or
                not mono[0] <= mono[1] <= mono[2] or not number(record['elapsed_seconds']) or
                abs(record['elapsed_seconds'] - (mono[2] - mono[1])) > 1e-6):
            return False
        if (type(record['answer']) is not str or len(record['answer'].encode()) > 65536 or
                record['outcome'] == 'submitted' and not record['answer'] or
                type(record['citations']) is not list or len(record['citations']) > 128 or
                not all(type(c) is str and len(c.encode()) <= 4096 for c in record['citations']) or
                record['exclusion_reason'] is not None and (type(record['exclusion_reason']) is not str or
                    not 0 < len(record['exclusion_reason'].encode()) <= 4096) or
                record['outcome'] == 'excluded' and record['exclusion_reason'] is None):
            return False
        if type(record['events']) is not list or len(record['events']) > 1024:
            return False
        previous = 0
        for event in record['events']:
            if (type(event) is not dict or set(event) != set(schema['event_fields']) or
                    not number(event['elapsed_seconds']) or not previous <= event['elapsed_seconds'] <= record['elapsed_seconds'] or
                    event['kind'] not in schema['event_kinds'] or event['status'] not in ('passed', 'failed', 'blocked') or
                    type(event['command']) is not str or len(event['command'].encode()) > 4096 or
                    event['exit_code'] is not None and type(event['exit_code']) is not int):
                return False
            previous = event['elapsed_seconds']
        grades = record['grades']
        return (type(grades) is list and len(grades) == len(question['case_ids']) and
            all(type(g) is dict and set(g) == set(schema['grade_fields']) and
                g['status'] in ('correct', 'incorrect', 'ungraded') and
                all(type(g[k]) is int and 0 <= g[k] <= 1024 for k in ('omissions', 'wrong_targets')) and
                type(g['false_certainty']) is bool for g in grades) and
            {g['case_id'] for g in grades} == set(question['case_ids']))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def validate_protocol(root=ROOT):
    root = Path(root)
    checks, hashes, documents = [], {}, {}
    result = dict(schema_version=1, task='T051', status='failed', source_identity=None,
        config_identity=None, analyzer_identity=None, case_results=checks, study_execution_status='not_run',
        identity_scope='Protocol validation only; actual workflow/source-access/analyzer/snapshot receipts remain required.',
        participants_enrolled=0, participants_run=0, human_evaluation=False, qualification_complete=False,
        task_accepted=False, study_eligibility={'status': 'blocked', 'upstream_acceptance': 'not_evaluated',
            'required_upstream_tasks': list(UPSTREAM), 'missing': ['five_independent_people_and_consent',
                'independent_human_source_assessor', 'accepted_upstream_evidence', 'exact_current_and_new_workflows',
                'verified_equal_source_access', 'execution_owner_approval_and_exclusion_rules',
                'private_storage_owner_and_retention']})
    def check(name, ok, detail):
        checks.append(dict(id=name, status='passed' if ok else 'failed', detail=detail))
    def identity(source, path):
        raw, sha, info = source.read(path, 1024 * 1024 + 1, hash_full=False)
        if info.st_size != len(raw) or len(raw) > 1024 * 1024:
            raise ValueError('Source identity exceeds its bounded full read')
        return sha
    try:
        with SourceRoot(root) as source:
            for name, expected in FROZEN.items():
                document, sha = read_json(source, INPUTS + name)
                if sha != expected or type(document) is not dict:
                    raise ValueError('Frozen protocol input differs')
                hashes[INPUTS + name], documents[name] = sha, document
            lock = documents['input-lock.json']
            for path, expected in lock['sha256'].items():
                raw, sha, info = source.read(path, 1024 * 1024 + 1, hash_full=False)
                if info.st_size != len(raw) or len(raw) > 1024 * 1024 or sha != expected:
                    raise ValueError('Locked source or review differs')
                hashes[path] = sha
            for name in ('django', 'odoo', 'contract'):
                review = documents[name + ('-framework-review.json' if name != 'contract' else '-review.json')]
                manifest = name + ('-framework-inputs.json' if name != 'contract' else '-inputs.json')
                if (review['status'] != 'admitted_frozen_source_key' or review['input_manifest']['sha256'] != FROZEN[manifest] or
                        any(review[key] is not False for key in ('human_ux_approval', 'implementation_approval', 'admission_is_task_acceptance'))):
                    raise ValueError('Source admission is not human or runtime acceptance')
            questions, selection = [], []
            for qid, category, name in SELECTION:
                document = documents[name]
                values = (document['questions'] if name == 'fixtures.json' else
                    document['query_protocol']['questions'] if name == 'contract-inputs.json' else document['acceptance_questions'])
                matches = [q for q in values if q['id'] == qid]
                if len(matches) != 1:
                    raise ValueError('Frozen question is missing or duplicate')
                q = matches[0]; case_ids = q['case_ids' if name == 'fixtures.json' else 'cases']
                cases = {c['id']: c for c in document['cases']}
                if not case_ids or len(set(case_ids)) != len(case_ids) or not set(case_ids) <= set(cases):
                    raise ValueError('Frozen physical grading key is missing')
                projected = dict(question_id=qid, category=category, input_path=INPUTS + name,
                    input_sha256=FROZEN[name], exact_frozen_prompt=q['prompt' if name == 'fixtures.json' else 'question'], case_ids=case_ids)
                selection.append(projected)
                questions.append(projected | {'grading_key_sha256': digest(canonical([cases[key] for key in case_ids]))})
                check(qid, True, 'Unchanged frozen prompt and all physical source-key cases; no participant outcome')
            check('frozen_selection', digest(canonical(selection)) == SELECTION_SHA, 'Nine preselected prompts/categories/case lists unchanged')
            if checks[-1]['status'] != 'passed': raise ValueError('Frozen selection differs')
            if not committed(root, hashes): raise ValueError('Frozen protocol inputs must match committed bytes')
            check('committed_frozen_sources', True, 'Guarded frozen fixture/source-review bytes match the committed source key')
            implementation = {path: identity(source, path) for path in IMPLEMENTATION}
            plan = allocation(questions)
            balances = {q['question_id']: {view: sum(r['question_id'] == q['question_id'] and r['condition'] == view for r in plan)
                for view in ('current', 'new')} for q in questions}
            order_balances = {str(order): {view: sum(r['order'] == order and r['condition'] == view for r in plan)
                for view in ('current', 'new')} for order in range(1, 10)}
            categories = {q['question_id']: q['category'] for q in questions}
            check('counterbalance', len(plan) == 45 and all(sorted(row.values()) == [2, 3] for row in balances.values()) and
                all(sorted(row.values()) == [2, 3] for row in order_balances.values()) and
                all({r['question_id'] for r in plan if r['slot'] == s} == {q['question_id'] for q in questions} and
                    {r['order'] for r in plan if r['slot'] == s} == set(range(1, 10)) for s in range(5)),
                'Five anonymous slots, opposite-view paired orders; each question once, explicit3/2 imbalance per question and task position including starts')
            protocol = dict(questions=questions, participants={'minimum_independent_people': 5,
                'models_as_participants': 'forbidden', 'eligibility': 'Independent of implementation and source-key authorship; retain familiarity and prior exposure privately.',
                'coaching': 'No implementation/source-key author coaches scored tasks.'},
                equal_source={'rule': 'Both conditions require identical admitted source/configuration bytes, prompt, full-source browsing, orientation and answer format.',
                'source_access_verified': False, 'current': {'build': None, 'commands': None, 'config': None, 'analyzer': None, 'snapshot': None},
                'new': {'build': None, 'commands': None, 'config': None, 'analyzer': None, 'snapshot': None}},
                counterbalance={'status': 'preview_not_actual_assignment', 'minimum_people': 5, 'planned_slots': 5,
                    'condition_rule': '(question_index + slot) modulo2: current for0, new for1',
                    'order_rule': 'rotate fixed question order by floor(slot/2) modulo9; pairs share order with opposite conditions',
                    'schedule': plan, 'question_view_counts': balances,
                    'view_counts': {v: sum(r['condition'] == v for r in plan) for v in ('current', 'new')},
                    'category_view_counts': {c: {v: sum(categories[r['question_id']] == c and r['condition'] == v for r in plan)
                        for v in ('current', 'new')} for c in sorted(set(categories.values()))},
                    'order_view_counts': order_balances,
                    'actual_assignments': []}, capture=capture_schema(),
                independent_grading={'human_assessor_assigned': False, 'answers_locked_before_grading': True,
                    'source_key_disputes': 'Resolve through existing source admission before scoring; never silently change gold.',
                    'correct_completion': 'Every requested frozen case and source citation is correct and preserves unsupported/unknown limits.',
                    'false_certainty': 'Flag unsupported exact-target, runtime-order, dispatch or completeness claims against the frozen ceilings.',
                    'possible_path_limit': 'Physical declarations and unresolved stop points only; no executable runtime order or complete business path.'},
                proposed_targets={'correct_completion_at_least': .8, 'false_certainty_conclusions': 0,
                    'targets_are_results': False, 'measured_outcomes': None})
            check('capture_and_private_handling', {'prompt_ready_utc', 'start_utc', 'end_utc', 'prompt_ready_monotonic',
                'start_monotonic', 'end_monotonic', 'elapsed_seconds', 'answer', 'events', 'grades',
                'assessor_id', 'exclusion_reason'} <= set(protocol['capture']['fields']) and
                protocol['capture']['outcomes'] == ['submitted', 'incomplete', 'timeout', 'withdrawn', 'excluded'],
                'Typed genuine timing/errors/per-case grades; raw data, consent and identity mapping remain private')
            # Recheck all captured files after validation; no generation/source mixing.
            current = {path: identity(source, path) for path in hashes | implementation}
            check('source_stability', current == hashes | implementation, 'All captured input and validator bytes unchanged')
        result.update(protocol=protocol, source_identity={'inputs': hashes, 'sha256': digest(canonical(hashes)),
            'verification_scope': 'Committed frozen manifests/reviews and tracked locked source only; real-corpus access is not observed'},
            config_identity=digest(canonical(protocol)), analyzer_identity=digest(canonical(implementation)), implementation=implementation)
        result['status'] = 'passed' if all(c['status'] == 'passed' for c in checks) else 'failed'
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        check('protocol_validation', False, 'Missing, unsafe, changed or invalid frozen protocol input')
        result['error_kind'] = type(error).__name__
    return result


def write_report(root, path, result):
    if path == BUSINESS_OUTPUT:
        with SourceRoot(root) as source:
            existing, _ = read_json(source, path, BUSINESS_RESULT_BYTES)
        if type(existing) is not dict or type(existing.get('tasks')) is not dict:
            raise ValueError('Existing business report schema required')
        existing['tasks']['T051'] = result
        result = existing
    return write_result(root, path, result, BUSINESS_RESULT_BYTES)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-protocol', action='store_true', required=True)
    parser.add_argument('--output', default=BUSINESS_OUTPUT)
    args = parser.parse_args(argv)
    result = validate_protocol(ROOT)
    try:
        write_report(ROOT, args.output, result)
    except (OSError, ValueError, TypeError):
        print('{"status":"failed","error_kind":"ReportRefused","study_execution_status":"not_run"}')
        return 1
    print(json.dumps({key: result[key] for key in ('task', 'status', 'study_execution_status', 'study_eligibility')}))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
