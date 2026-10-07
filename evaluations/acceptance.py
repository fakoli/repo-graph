#!/usr/bin/env python3
"""Validate frozen inputs; missing independent source truth cannot pass."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_graph.source import SourceRoot

ROOT = Path(__file__).resolve().parents[1]
INPUTS = 'evaluations/code-understanding/'
LANGUAGES = {'python', 'go', 'javascript', 'typescript'}
REVIEW_POLICY = {'schema_version': 1, 'scope': 'T004', 'review_kind': 'independent_ai',
                'model': 'gpt-6-astra', 'authorization': 'explicit_user_override',
                'human_evaluation': False, 'human_ux_gate': 'T027_required'}
PINS = {
    'odoo': '2d9fd5562a0ef1f7f587eb393cc3bd293b047cca',
    'django': '3b7ae042cef02a09caab70ba54077a6f4cffac80',
    'aws': '82532de7103d4dbabe384749cfaf09fc4c0692ad',
    'kubernetes': '35fc3af13807e70534fb11736bcccc013631efde',
    'viewer': 'b21a7c19fc3f068d3b0227ba1fa6acd5eda17280',
    'extensions': '260a06f7394d56521e0d0d2a04cf58abe858cd56',
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 96 and value[0].isalpha()
            and all(c.isalnum() or c in '._:-' for c in value))


def fixture_path(value):
    if not isinstance(value, str) or not value.startswith('tests/fixtures/code-understanding/'):
        return False
    try:
        return (Path(value).as_posix() == value and all(
            all(c.isalnum() or c in '._-' for c in part) for part in SourceRoot.parts(value)))
    except OSError:
        return False


def read_json(source, path):
    raw, sha, info = source.read(path, 1024 * 1024 + 1, hash_full=False)
    if info.st_size > 1024 * 1024:
        raise ValueError('Manifest exceeds 1 MiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def finite(_):
        raise ValueError('Non-finite JSON number')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite), sha


def valid_range(record, raw):
    span = record['range']
    start, end = span['start_byte'], span['end_byte']
    return (type(start) is int and type(end) is int and 0 <= start < end <= len(raw)
            and type(span['start_line']) is int and type(span['end_line']) is int
            and raw[start:end] == record['text'].encode('utf-8')
            and span['start_line'] == raw[:start].count(b'\n') + 1
            and span['end_line'] == raw[:end - 1].count(b'\n') + 1)


def validate_update(update, files, cases):
    snapshot = files.copy()
    if (not identifier(update.get('id')) or update.get('language') not in tuple(LANGUAGES)
            or update.get('kind') not in ('addition', 'deletion', 'rename', 'cycle')
            or update.get('starts_from') != 'frozen_base_fixture' or not update.get('operations')):
        return False
    for operation in update['operations']:
        path = operation['path']
        if not fixture_path(path):
            return False
        if 'sha256_before' in operation and digest(snapshot[path]) != operation['sha256_before']:
            return False
        if operation['op'] == 'add':
            if path in snapshot:
                return False
            snapshot[path] = operation['content'].encode('utf-8')
        elif operation['op'] == 'delete':
            del snapshot[path]
        elif operation['op'] == 'rename':
            target = operation['to']
            if not fixture_path(target) or target in snapshot:
                return False
            snapshot[target] = snapshot.pop(path)
            path = target
        elif operation['op'] == 'replace':
            old, new = operation['old'].encode('utf-8'), operation['new'].encode('utf-8')
            if not old or snapshot[path].count(old) != operation['occurrences']:
                return False
            snapshot[path] = snapshot[path].replace(old, new)
        else:
            return False
        if operation['op'] != 'delete' and digest(snapshot[path]) != operation['sha256_after']:
            return False
        if sum(map(len, snapshot.values())) > 1024 * 1024:
            return False
    impacts = update['expected_impacts']
    return (isinstance(impacts, list) and bool(impacts) and all(isinstance(impact, dict)
        and bool(impact) and ('case_id' not in impact or (
        impact['case_id'] in cases and impact['certainty_before'] == cases[impact['case_id']]['certainty']
        and impact['certainty_after'] in ('resolved', 'candidate', 'unresolved'))) for impact in impacts))


def inputs(root=None):
    """Read only bounded, guarded inputs. Return every failed check, not a score."""
    root = ROOT if root is None else root
    results, hashes, documents, files = [], {}, {}, {}
    def check(case, condition, reason):
        results.append({'id': case, 'status': 'passed' if condition else 'failed', 'detail': reason})
    def records(document, key):
        values = document.get(key, [])
        valid = isinstance(values, list) and len(values) <= 2048 and all(isinstance(x, dict) for x in values)
        check(key + ':records', valid, 'Bounded list of object records')
        return values if valid else []
    with SourceRoot(root) as source:
        for name in ('fixtures.json', 'corpora.json', 'real-calls.json', 'source-review-policy.json'):
            try:
                document, sha = read_json(source, INPUTS + name)
                if not isinstance(document, dict):
                    raise ValueError('Manifest must be an object')
                check(name + ':schema', type(document.get('schema_version')) is int
                      and document['schema_version'] == 1, 'Schema version 1 required')
                documents[name], hashes[INPUTS + name] = document, sha
            except (OSError, ValueError, TypeError, RecursionError):
                check(name + ':read', False, 'Missing, unsafe, oversized or invalid manifest')
        if len(documents) != 4:
            return results, hashes, documents
        check('source-review-policy', documents['source-review-policy.json'] == REVIEW_POLICY,
              'User-authorized T004 independent Astra source review; no human evaluation claim')
        fixture = documents['fixtures.json']
        fixture_files = records(fixture, 'files')
        for index, record in enumerate(fixture_files):
            path = record.get('path', '')
            try:
                if not fixture_path(path) or path in files:
                    raise ValueError('Duplicate or out-of-scope fixture')
                raw, sha, info = source.read(path, 256 * 1024 + 1, hash_full=False)
                if info.st_size > 256 * 1024 or sum(len(x) for x in files.values()) + len(raw) > 1024 * 1024:
                    raise ValueError('Fixture byte budget exceeded')
                raw.decode('utf-8')
                files[path], hashes[path] = raw, sha
                check(f'fixture-file:{index}:identity', record['sha256'] == sha
                      and type(record['bytes']) is int and record['bytes'] == len(raw)
                      and record['language'] in LANGUAGES, 'Full source digest, size and language')
            except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
                check(f'fixture-file:{index}:read', False, 'Missing, unsafe, oversized or invalid fixture')
        check('fixture-files', 0 < len(files) <= 128, 'Bounded nonempty fixture inventory')
        check('fixture-languages', {r.get('language') for r in fixture_files if isinstance(r.get('language'), str)} == LANGUAGES,
              'Python, Go, JavaScript and TypeScript remain separate')
        definition_records = records(fixture, 'definitions')
        definitions = {r['id']: r for r in definition_records if identifier(r.get('id'))}
        cases = records(fixture, 'cases')
        check('definition-ids', len(definitions) == len(definition_records)
              and all(isinstance(x, str) and x for x in definitions), 'Unique nonempty definition IDs')
        case_ids = {r['id'] for r in cases if identifier(r.get('id'))}
        check('case-ids', len(case_ids) == len(cases) and bool(cases), 'Unique nonempty case IDs')
        for index, record in enumerate([*definitions.values(), *cases]):
            label = record['id'] if identifier(record.get('id')) else f'invalid-record:{index}'
            try:
                check(label + ':source-range', valid_range(record, files[record['path']]),
                      'Exact half-open UTF-8 byte range and one-based display lines')
            except (KeyError, TypeError, ValueError, AttributeError):
                check(label + ':source-range', False, 'Invalid range or source evidence')
        for index, record in enumerate(cases):
            targets = record.get('targets', [])
            certainty = record.get('certainty')
            valid = (isinstance(targets, list) and all(isinstance(t, str) for t in targets)
                     and len(set(targets)) == len(targets)
                     and all(t in definitions for t in targets)
                     and ((certainty == 'resolved' and len(targets) == 1)
                          or (certainty == 'candidate' and len(targets) > 0)
                          or (certainty == 'unresolved' and not targets))
                     and isinstance(record.get('reason'), str) and bool(record['reason'])
                     and record.get('role') in ('call', 'reference')
                     and isinstance(record.get('language'), str) and record['language'] in LANGUAGES
                     and any(f.get('path') == record.get('path') and f.get('language') == record['language']
                             for f in fixture_files)
                     and isinstance(record.get('construct'), str))
            label = record['id'] if identifier(record.get('id')) else f'invalid-case:{index}'
            check(label + ':uncertainty', valid, 'Targets, evidence role and uncertainty are explicit')
        constructs = {r.get('construct') for r in cases if isinstance(r.get('construct'), str)}
        matrix = fixture.get('constructs')
        check('constructs', isinstance(matrix, list) and bool(matrix) and all(isinstance(x, str) for x in matrix)
              and constructs <= set(matrix),
              'Every case belongs to the frozen supported/uncertain construct matrix')
        questions = records(fixture, 'questions')
        check('questions', bool(questions) and len({q['id'] for q in questions if isinstance(q.get('id'), str)}) == len(questions)
              and all(q.get('prompt') and isinstance(q.get('case_ids'), list) and q['case_ids']
                      and all(isinstance(x, str) for x in q['case_ids']) and set(q['case_ids']) <= case_ids
                      for q in questions), 'Questions and referenced cases frozen before comparison')
        review = fixture.get('review', {})
        check('synthetic-review', isinstance(review, dict) and review.get('independent_human_review') is False
              and review.get('kind') == 'model_source_review_of_synthetic_inputs',
              'Model source review of synthetic fixtures is not represented as independent human review')
        updates = records(fixture, 'updates')
        check('update-scenarios', len(updates) == 16, 'Sixteen frozen mutation specifications; no engine-equivalence claim')
        for index, update in enumerate(updates):
            label = update['id'] if identifier(update.get('id')) else f'invalid-update:{index}'
            try:
                valid = validate_update(update, files, {c['id']: c for c in cases if identifier(c.get('id'))})
            except (KeyError, TypeError, AttributeError, ValueError):
                valid = False
            check(label + ':mutation-spec', valid, 'Root-bound operations and before/after content hashes match')
        corpora = records(documents['corpora.json'], 'corpora')
        corpus_urls = {c['id']: c.get('repository_url') for c in corpora if isinstance(c.get('id'), str)}
        check('corpus-pins', len(corpora) == len(PINS)
              and {c['id']: c.get('revision') for c in corpora if isinstance(c.get('id'), str)} == PINS,
              'All six authoritative corpus revisions match; no moving refs')
        real = records(documents['real-calls.json'], 'cases')
        check('real-case-ids', len(real) == 16 and len({r['id'] for r in real if identifier(r.get('id'))}) == 16,
              'All sixteen prepared real-call candidates retained')
        check('real-languages', {r.get('language') for r in real if isinstance(r.get('language'), str)} == LANGUAGES,
              'Real-call judgments will be scored independently per language')
        for index, record in enumerate(real):
            label = record['id'] if identifier(record.get('id')) else f'invalid-real-case:{index}'
            try:
                span = record['range']['utf8_bytes']
                path = record['path']
                source_url = record['source_url']
                base_url = corpus_urls[record['repository_id']] + '/blob/' + record['revision'] + '/' + path
                valid = (record['revision'] == PINS[record['repository_id']]
                         and bool(SourceRoot.parts(path)) and Path(path).as_posix() == path
                         and type(span['start']) is int and type(span['end_exclusive']) is int
                         and 0 <= span['start'] < span['end_exclusive'] <= record['file_bytes']
                         and type(record['file_bytes']) is int
                         and len(record['file_sha256']) == 64 and int(record['file_sha256'], 16) >= 0
                         and len(record['git_blob_sha1']) == 40 and int(record['git_blob_sha1'], 16) >= 0
                         and record['status'] == 'human_pending'
                         and isinstance(source_url, str) and source_url.split('#')[0] == base_url)
                check(label + ':candidate', valid, 'Pinned source identity/range; proposed targets stay unreviewed')
            except (OSError, KeyError, TypeError, ValueError):
                check(label + ':candidate', False, 'Invalid candidate identity or range')
    return results, hashes, documents


def source_truth(path, candidate_sha, candidates):
    """Validate the authorized independent AI source key; never label it human."""
    if path is None:
        return [], 'Independent Astra source judgments have not been supplied'
    try:
        with SourceRoot(path.parent) as source:
            receipt, sha = read_json(source, path.name)
        review = receipt['review']
        if (receipt.get('schema_version') != 1 or receipt['candidate_manifest_sha256'] != candidate_sha
                or review.get('kind') != REVIEW_POLICY['review_kind'] or review.get('model') != REVIEW_POLICY['model']
                or review.get('independent') is not True or review.get('source_reviewed') is not True
                or not review.get('reviewer') or not review.get('evidence_reference')):
            raise ValueError('No authorized independent Astra source review receipt')
        judgments = receipt['judgments']
        if (not isinstance(judgments, list) or not all(isinstance(j, dict) and isinstance(j.get('id'), str) for j in judgments)
                or len(judgments) != len(candidates) or {j['id'] for j in judgments} != {c['id'] for c in candidates}):
            raise ValueError('Judgments must cover every frozen candidate')
        candidate_by_id = {c['id']: c for c in candidates}
        for judgment in judgments:
            targets = judgment['targets']
            evidence = judgment['evidence']
            candidate = candidate_by_id[judgment['id']]
            anchored = (isinstance(evidence, list) and all(isinstance(e, dict) for e in evidence)
                        and any(all(e.get(key) == candidate[key] for key in
                            ('repository_id', 'revision', 'path', 'file_sha256', 'range', 'source_url')) for e in evidence))
            if (not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets)
                    or len(set(targets)) != len(targets)
                    or type(judgment['supported']) is not bool
                    or not isinstance(judgment['reason'], str) or not judgment['reason']
                    or not anchored or not isinstance(judgment['assumptions'], list)
                    or not judgment['assumptions'] or not all(isinstance(x, str) and x for x in judgment['assumptions'])
                    or not ((judgment['certainty'] == 'resolved' and len(targets) == 1)
                            or (judgment['certainty'] == 'candidate' and len(targets) > 0)
                            or (judgment['certainty'] == 'unresolved' and not targets))):
                raise ValueError('Incomplete or contradictory source judgment')
        return [{'id': 'independent-real-call-truth', 'status': 'passed', 'receipt_sha256': sha,
                 'candidate_manifest_sha256': candidate_sha, 'judgments': [{
                     'id': j['id'], 'certainty': j['certainty'], 'supported': j['supported'],
                     'targets_sha256': digest(canonical(j['targets'])), 'source_judgment_sha256': digest(canonical(j))
                 } for j in judgments],
                 'evidence_kind': 'independent_ai_source_review', 'model': review['model'],
                 'identity_verification': 'Actual delegated Astra review receipt audited by coordinator; not human evidence'}], None
    except (OSError, ValueError, KeyError, TypeError, RecursionError, AttributeError):
        return [], 'Incomplete, unsafe, mismatched or unauthorized source review receipt'


def committed(root, paths):
    commit = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=root, capture_output=True, text=True)
    if commit.returncode:
        return False
    revision = commit.stdout.strip()
    for path, sha in paths.items():
        result = subprocess.run(['git', 'show', revision + ':' + path], cwd=root, capture_output=True)
        if result.returncode or digest(result.stdout) != sha:
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', choices=['freeze'], default='freeze')
    parser.add_argument('--prepare', action='store_true', help='Check draft inputs only; does not pass the freeze gate')
    parser.add_argument('--seal-inputs', action='store_true', help='Record input hashes; requires --prepare')
    parser.add_argument('--source-review', type=Path, help='Independent source review; defaults to the committed source-review.json')
    parser.add_argument('--review-template', type=Path, help='Write an unfilled independent source judgment template')
    parser.add_argument('--report', type=Path, help='Write a portable report; no reviewer identifiers are retained')
    args = parser.parse_args(argv)
    if args.seal_inputs and not args.prepare:
        parser.error('--seal-inputs requires --prepare')
    cases, hashes, documents = inputs()
    prepared = bool(cases) and all(c['status'] == 'passed' for c in cases)
    candidates = documents.get('real-calls.json', {}).get('cases', [])
    if args.review_template:
        if (not isinstance(candidates, list) or len(candidates) != 16 or not all(isinstance(c, dict)
                and identifier(c.get('id')) for c in candidates)):
            parser.error('No valid real-call candidate manifest')
        template = {'schema_version': 1, 'candidate_manifest_sha256': hashes[INPUTS + 'real-calls.json'],
            'review': {'kind': None, 'model': None, 'independent': None, 'source_reviewed': None,
                       'reviewer': None, 'evidence_reference': None},
            'judgments': [{'id': c['id'], 'targets': None, 'certainty': None, 'supported': None,
                'reason': None, 'assumptions': [], 'evidence': [{k: c[k] for k in
                    ('repository_id', 'revision', 'path', 'file_sha256', 'range', 'source_url')}],
                'proposed_targets_unreviewed': c.get('proposed_target_keys_unreviewed')} for c in candidates]}
        args.review_template.parent.mkdir(parents=True, exist_ok=True)
        with SourceRoot(args.review_template.parent) as source:
            source.write_json(args.review_template.name, template)
        print(json.dumps({'status': 'unfilled_review_template', 'cases': len(candidates),
                          'candidate_manifest_sha256': template['candidate_manifest_sha256']}))
        return 0
    review_path = args.source_review or (ROOT / INPUTS / 'source-review.json')
    truth, missing = source_truth(review_path, hashes.get(INPUTS + 'real-calls.json'), candidates)
    truth_sha = truth[0]['receipt_sha256'] if truth else None
    if truth and args.source_review is None:
        hashes[INPUTS + 'source-review.json'] = truth_sha
    lock_path = INPUTS + 'input-lock.json'
    if args.seal_inputs and prepared:
        directory = ROOT / INPUTS
        with SourceRoot(directory) as source:
            source.write_json('input-lock.json', {'schema_version': 1,
                'status': 'inputs_locked_review_pending' if missing else 'inputs_locked_ai_source_review_supplied',
                'sha256': hashes, 'corpus_revisions': PINS, 'source_review_sha256': truth_sha})
    lock_ok = False
    try:
        with SourceRoot(ROOT) as source:
            lock, sha = read_json(source, lock_path)
        lock_ok = (isinstance(lock, dict) and type(lock.get('schema_version')) is int and lock['schema_version'] == 1
                   and lock.get('sha256') == hashes and lock.get('corpus_revisions') == PINS
                   and lock.get('source_review_sha256') == truth_sha)
        hashes[lock_path] = sha
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    cases.append({'id': 'input-lock', 'status': 'passed' if lock_ok else 'failed',
                  'detail': 'Immutable input hashes match; locking inputs alone is not human acceptance'})
    is_committed = lock_ok and committed(ROOT, hashes)
    cases.extend(truth)
    cases.append({'id': 'committed-inputs', 'status': 'passed' if is_committed else 'missing',
                  'detail': 'Every locked input matches its Git HEAD blob before any comparison'})
    if missing:
        cases.append({'id': 'independent-real-call-truth', 'status': 'missing', 'detail': missing})
    passed = not args.prepare and prepared and lock_ok and is_committed and missing is None
    identity = digest(canonical({'inputs': hashes, 'source_review': truth_sha}))
    report = {'schema_version': 1, 'gate': 'freeze', 'status': 'passed' if passed else 'blocked',
              'preparation_only': args.prepare, 'source_identity': {'inputs_sha256': hashes,
                  'content_identity': identity,
                  'source_review_sha256': truth_sha, 'review_policy': REVIEW_POLICY, 'corpus_revisions': PINS,
                  'validator_sha256': digest(Path(__file__).read_bytes()), 'python': platform.python_version()},
              'tasks': {'T004': {'status': 'passed' if passed else 'blocked',
                  'source_identity': identity, 'case_results': cases}},
              'remaining_gates': ['engine selection', 'incremental facts', 'scale', 'agent',
                                  'independent human UX', 'distribution', 'release'],
              'limits': ['Source judgments use the user-authorized independent Astra AI review; no human qualification.',
                        'Candidate source bytes are reviewed externally; no real corpus is shipped.',
                        'No engine executed and no performance or token threshold measured.']}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with SourceRoot(args.report.parent) as source:
            source.write_json(args.report.name, report)
    print(json.dumps({'gate': 'freeze', 'status': report['status'], 'prepared': prepared and lock_ok,
                      'committed': is_committed, 'cases': len(cases), 'failures': [c for c in cases if c['status'] != 'passed']}))
    return 0 if (prepared and lock_ok if args.prepare else passed) else 1


if __name__ == '__main__':
    raise SystemExit(main())
