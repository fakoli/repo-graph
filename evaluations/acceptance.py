#!/usr/bin/env python3
"""Validate frozen inputs; missing independent source truth cannot pass."""
import argparse
import hashlib
import json
import math
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


def read_json(source, path, maximum=1024 * 1024):
    raw, sha, info = source.read(path, maximum + 1, hash_full=False)
    if info.st_size > maximum or len(raw) != info.st_size:
        raise ValueError('JSON input exceeds its finite byte budget')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def finite(_):
        raise ValueError('Non-finite JSON number')
    def parse_float(value):
        number = float(value)
        return number if math.isfinite(number) else finite(value)
    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=parse_float), sha


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


# Finite post-production proof validation. No source extraction or worker launch.
_ADAPTER_HELPERS = ('evaluations/incremental_candidate.py', 'evaluations/queued_collector.py',
    'evaluations/tree_sitter_baseline.py', 'evaluations/bounded_queries.py', 'repo_graph/source.py',
    'evaluations/engine_checks.py', 'evaluations/analysis.py', 'evaluations/acceptance.py')
_MISSING_EXTRA = ('repo_graph/__init__.py', 'repo_graph/cli.py', 'repo_graph/builder.py',
    'repo_graph/search.py', 'repo_graph/jev.py', 'repo_graph/rerank.py', 'scripts/repo_graph.py',
    'repo_graph/assets/diagram.html', 'repo_graph/assets/views.js')
_MISSING_NINE = ('pre_bootstrap_stdlib_venv_no_distributions',
    'five_optional_modules_and_distributions_absent', 'core_cli_map_and_keyword_returned_zero',
    'core_supported_scan_and_keyword', 'serial_and_queued_candidate_explicit_missing_distribution_no_snapshot',
    'serial_and_queued_actual_queue_backend_refusal', 'queue_owned_children_removed',
    'clean_venv_not_base_prefix', 'python_isolated_private_owned_session')


class MissingBackendStabilityError(ValueError):
    pass


class MissingBackendMetadataError(ValueError):
    pass


class MissingBackendEvidenceError(ValueError):
    pass


class MissingBackendRetainedFailureError(ValueError):
    pass


def _proof_project_name(raw):
    """Read the conventional literal name only from committed bounded metadata."""
    import re
    try:
        text = raw.decode('utf-8')
        sections = re.split(r'^\s*\[([^\]\r\n]+)\]\s*(?:#.*)?$', text, flags=re.MULTILINE)
        projects = [sections[i + 1] for i in range(1, len(sections), 2) if sections[i] == 'project']
        if len(projects) != 1:
            raise MissingBackendMetadataError('Committed project metadata')
        names = re.findall(r'^\s*name\s*=\s*"([^"\\\r\n]*)"\s*(?:#.*)?$', projects[0], flags=re.MULTILINE)
        if len(names) != 1 or len(names[0]) > 128 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?', names[0]):
            raise MissingBackendMetadataError('Committed project metadata')
        return re.sub(r'[-_.]+', '-', names[0]).lower()
    except UnicodeError:
        raise MissingBackendMetadataError('Committed project metadata') from None


def _proof_require(condition, label):
    if not condition:
        raise ValueError(label)


def _proof_sha(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _proof_int(value, maximum=256 * 1024 * 1024):
    return type(value) is int and 0 <= value <= maximum


def _proof_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _proof_path(value):
    _proof_require(type(value) is str and 0 < len(value.encode()) <= 4096 and
        value == Path(value).as_posix() and '\\' not in value and ':' not in value and
        all(p not in ('', '.', '..') for p in value.split('/')), 'Noncanonical proof path')
    SourceRoot.parts(value)
    return value


def _proof_value(value, depth=0):
    _proof_require(depth <= 64, 'Proof nesting limit')
    if type(value) is dict:
        _proof_require(len(value) <= 10000 and all(type(k) is str for k in value), 'Proof map limit')
        for child in value.values():
            _proof_value(child, depth + 1)
    elif type(value) is list:
        _proof_require(len(value) <= 40000, 'Proof sequence limit')
        for child in value:
            _proof_value(child, depth + 1)
    else:
        _proof_require(value is None or type(value) in (str, bool, int) or
                       type(value) is float and math.isfinite(value), 'Proof primitive')


def _proof_directory(path):
    """Reject root symlinks before SourceRoot.resolve; create nothing."""
    import os
    value = Path(os.path.abspath(path))
    fd = os.open(value.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in value.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        with SourceRoot(value) as source:
            actual, held = os.fstat(source.fd), os.fstat(fd)
            _proof_require(source.secure and (actual.st_dev, actual.st_ino) ==
                           (held.st_dev, held.st_ino), 'Proof directory owner changed')
            return value, source.identity
    finally:
        os.close(fd)


def _proof_evidence_root(root, evidence_root, source_map):
    import os
    source_map = source_map or os.environ.get('REPO_GRAPH_EVAL_SOURCE_MAP')
    _proof_require(source_map is not None, 'Private source map required for evidence isolation')
    source_map = Path(source_map)
    parent, _ = _proof_directory(source_map.parent)
    with SourceRoot(parent) as source:
        mapped, _ = read_json(source, _proof_path(source_map.name))
    corpora = mapped['corpora']
    _proof_require(type(corpora) is list and 1 <= len(corpora) <= 128 and
                   all(type(c) is dict and type(c.get('source')) is str for c in corpora), 'Bounded corpus roots required')
    destination = evidence_root or os.environ.get('REPO_GRAPH_EVAL_WORK_ROOT') or parent / 'analysis-workers'
    directory, identity = _proof_directory(destination)
    for boundary in [Path(root).resolve(strict=True)] + [Path(c['source']).resolve(strict=True) for c in corpora]:
        _proof_require(directory != boundary and boundary not in directory.parents and
                       directory not in boundary.parents, 'Proof root overlaps a source owner')
    return directory, identity


def _proof_archive(portable, kind, evidence_root):
    """Verify every bounded regular file, then reproject the bound raw receipt."""
    import os
    import stat
    from evaluations.analysis import compact_adapter_result
    archive = portable['archive']
    _proof_require(type(archive) is dict and set(archive) == {'directory', 'files', 'bytes'}, 'Archive shape')
    name = _proof_path(archive['directory'])
    prefix = {'updates': 'updates-', 'queries': 'queries-', 'missing_backend': 'missing-backend-'}[kind]
    _proof_require(name.startswith(prefix) and len(name) == len(prefix) + 32 and
                   all(c in '0123456789abcdef' for c in name[len(prefix):]), 'Finite adapter archive directory')
    references = archive['files']
    _proof_require(type(references) is list and 1 <= len(references) <= 10000 and
                   _proof_int(archive['bytes']), 'Archive bounds')
    expected, total, report_bytes = {}, 0, None
    with SourceRoot(evidence_root) as parent:
        held = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
        try:
            with SourceRoot(parent.root / name) as source:
                _proof_require((os.fstat(held).st_dev, os.fstat(held).st_ino) ==
                    (os.fstat(source.fd).st_dev, os.fstat(source.fd).st_ino), 'Archive owner changed')
                for ref in references:
                    _proof_require(type(ref) is dict and set(ref) == {'path', 'sha256', 'bytes'} and
                        _proof_sha(ref['sha256']) and _proof_int(ref['bytes'], 16 * 1024 * 1024), 'Archive file shape')
                    path = _proof_path(ref['path'])
                    _proof_require(path not in expected, 'Duplicate archive path')
                    raw, sha, info = source.read(path, 16 * 1024 * 1024 + 1, hash_full=False)
                    _proof_require(len(raw) == info.st_size == ref['bytes'] and sha == ref['sha256'], 'Archive full bytes mismatch')
                    expected[path] = ref; total += len(raw)
                    _proof_require(total <= 256 * 1024 * 1024, 'Archive aggregate bound')
                    if path == ('result.json' if kind == 'missing_backend' else 'report.json'):
                        report_bytes = raw
                _proof_require(total == archive['bytes'] and report_bytes is not None, 'Archive report/total required')
                # Reuse the strict reader, including duplicate keys and overflow rejection.
                primary = 'result.json' if kind == 'missing_backend' else 'report.json'
                report, report_sha = read_json(source, primary, 16 * 1024 * 1024)
                _proof_require(report_sha == expected[primary]['sha256'], 'Archived raw report changed before projection')
                excluded = report.get('excluded_runtime_directories', [])
                allowed = {'venv', 'source', 'map', 'home', 'config', 'cache', 'data', 'tmp'}
                _proof_require(type(excluded) is list and len(set(excluded)) == len(excluded) and
                    set(excluded) <= allowed and (not excluded or kind == 'missing_backend'), 'Runtime archive omissions')
                actual, entries = set(), 0
                for current, directories, files in os.walk(source.root, followlinks=False):
                    directories.sort()
                    if Path(current) == source.root:
                        directories[:] = [d for d in directories if d not in excluded]
                    for child in directories + files:
                        entries += 1
                        _proof_require(entries <= 20000 and not stat.S_ISLNK((Path(current)/child).lstat().st_mode), 'Archive inventory bound/symlink')
                    actual.update((Path(current)/f).relative_to(source.root).as_posix() for f in files)
                _proof_require(actual == set(expected), 'Archive inventory omissions')
                _proof_value(report)
                projection = compact_adapter_result({'full_private_report': report, 'archive': archive}, kind)
                _proof_require(canonical(projection) == canonical(portable), 'Portable projection differs from archived observations')
                _proof_require(_proof_directory(source.root)[1] == source.identity, 'Archive owner changed during validation')
                return report, expected, source.identity
        finally:
            os.close(held)


def _proof_frozen(root):
    from evaluations.analysis import frozen_inputs
    from evaluations.supplement_preparation import prepare_check
    fixture, _ = frozen_inputs(root)
    prepared = prepare_check(root)
    _proof_require(prepared['checks_passed'] == 2322 and prepared['physical_ranges'] == 535, 'Frozen supplemental preparation')
    with SourceRoot(root) as source:
        manifest, _ = read_json(source, INPUTS + 'supplement-source.json')
        oracle, _ = read_json(source, INPUTS + 'supplement-oracle.json')
        lock, lock_sha = read_json(source, INPUTS + 'supplement-lock.json')
    binding = dict(lock['sha256'], **{INPUTS + 'supplement-lock.json': lock_sha})
    _proof_require(len(binding) == 31 and committed(root, binding), 'Exactly31 committed frozen artifacts')
    return {'fixture': fixture, 'source': manifest, 'oracle': oracle, 'binding': binding, 'lock_sha': lock_sha}


def _proof_binding(raw, kind, root, frozen, hashes, revision):
    before, after = raw['binding_before'], raw['binding_after']
    keys = ('measured_commit', 'implementation', 'input_binding', 'root_identity')
    if kind == 'missing_backend':
        keys = ('measured_commit', 'implementation_sha256', 'root_owner')
    _proof_require(type(before) is dict and type(after) is dict and
        all(before[k] == after[k] for k in keys) and before['measured_commit'] == revision, 'Before/after source binding')
    required = _ADAPTER_HELPERS + (_MISSING_EXTRA if kind == 'missing_backend' else ())
    implementation = before['implementation_sha256' if kind == 'missing_backend' else 'implementation']
    _proof_require(type(implementation) is dict and set(implementation) == set(required) and
                   all(implementation[p] == hashes[p] for p in required), 'Exact current helper binding')
    with SourceRoot(root) as source:
        if kind == 'missing_backend':
            import os
            info = os.fstat(source.fd)
            _proof_require(before['root_owner'] == {'device': info.st_dev, 'inode': info.st_ino} and
                all(type(v) is int for v in before['root_owner'].values()),
                'Missing-backend root binding')
        else:
            _proof_require(before['root_identity'] == source.identity and
                before['input_binding'] == frozen['binding'], 'Owner/31input binding')
    _proof_require(raw['status'] == 'passed' and raw.get('engine_selected') is False and
                   raw.get('qualification_complete') is False and not raw.get('driver_failure') and
                   not raw.get('failures'), 'Complete unselected adapter proof required')
    if kind == 'updates':
        _proof_require(raw['measured_commit'] == revision and raw['implementation'] == implementation and
            raw['source_lock_sha256'] == frozen['lock_sha'] and type(raw['preparation_checks']) is int and
            raw['preparation_checks'] == 2322, 'Update top-level source binding')
    elif kind == 'queries':
        _proof_require(raw['measured_commit_before'] == raw['measured_commit_after'] == revision and
            raw['implementation_hashes_before'] == raw['implementation_hashes_after'] == implementation and
            raw['committed_inputs_stable_after'] is True and raw['input_binding'] == {'status': 'passed',
                'map_entries': 31, 'bound_to_measured_commit': revision, 'sha256': frozen['binding']} and
            type(raw['input_binding']['map_entries']) is int, 'Query top-level source binding')
    else:
        _proof_require(raw['measured_commit'] == revision and raw['implementation_sha256'] == implementation and
            before['loaded_controller_sha256'] == implementation['evaluations/engine_checks.py'], 'Missing-backend top-level source binding')
    return implementation


def _proof_cleanup(rows, workers, mailboxes=True):
    _proof_require(type(rows) is list and len(rows) == workers, 'Every owned worker cleanup required')
    for row in rows:
        _proof_require(type(row) is dict and row['leader_reaped'] is True and row['group_absent'] is True and
            type(row['returncode']) is int and (not mailboxes or row['mailboxes_removed'] is True), 'Owned worker not reaped/removed')
        _proof_require(type(row['signals']) is list and set(row['signals']) <= {'SIGTERM', 'SIGKILL'}, 'Owned cleanup signals')
        if mailboxes:
            _proof_require(_proof_int(row['requests'], 128), 'Owned request count')


def _proof_queue(resources, cleanup, mode, concurrency, expected_changed=None, implementation=None):
    _proof_require(type(resources) is dict and resources['mode'] == mode and
        type(resources['configured_concurrency']) is int and resources['configured_concurrency'] == concurrency,
        'Requested queue mode/concurrency')
    for key in ('workers_started', 'files_admitted', 'files_collected', 'source_bytes', 'collected_handoff_bytes',
                'collected_nodes', 'collected_definitions', 'peak_inflight_reserved_bytes', 'worker_file_hard_limit_bytes'):
        _proof_require(_proof_int(resources[key]), 'Typed queue counter: ' + key)
    workers = resources['workers_started']
    _proof_require(workers <= concurrency and workers <= resources['files_admitted'] and
        (workers > 0) == (resources['files_admitted'] > 0) and _proof_number(resources['elapsed_seconds']), 'Actual workers/resources')
    if expected_changed is not None:
        _proof_require(resources['files_admitted'] == resources['files_collected'] == expected_changed and
            workers == min(concurrency, expected_changed), 'Measured collection work')
    limits = resources['limits']
    for key in ('max_request_bytes', 'max_result_bytes', 'max_inflight_bytes', 'max_admitted_bytes',
                'memory_bytes', 'cpu_seconds', 'log_bytes'):
        _proof_require(_proof_int(limits[key], 4 * 1024 * 1024 * 1024 if key == 'memory_bytes' else 256 * 1024 * 1024) and limits[key] > 0, 'Typed finite worker limit')
    for key in ('worker_wall_seconds', 'total_wall_seconds'):
        _proof_require(_proof_number(limits[key]) and 0 < limits[key] <= 60, 'Finite worker deadline')
    _proof_require(resources['peak_inflight_reserved_bytes'] <= limits['max_inflight_bytes'], 'Admission reservation bound')
    _proof_cleanup(cleanup, workers)
    if implementation is not None:
        from evaluations.tree_sitter_baseline import PINS, RULE_VERSION
        identity = resources['identity']; recorded = identity['implementations']
        expected_paths = {'evaluations/queued_collector.py', 'evaluations/tree_sitter_baseline.py',
            'evaluations/engine_checks.py', 'evaluations/analysis.py', 'evaluations/acceptance.py',
            'repo_graph/source.py', 'repo_graph/__init__.py'}
        _proof_require(type(recorded) is dict and set(recorded) == expected_paths and
            all(recorded[p] == implementation[p] for p in expected_paths) and
            identity['collector'] == implementation['evaluations/tree_sitter_baseline.py'] and
            identity['loaded_controller_sha256'] == implementation['evaluations/queued_collector.py'] and
            identity['pins'] == PINS and identity['rules'] == RULE_VERSION, 'Current queue producer identity/pins')
    isolation = resources['worker_isolation']
    _proof_require(type(isolation) is list and len(isolation) == workers, 'Every worker isolation receipt')
    for observed in isolation:
        _proof_require(type(observed) is dict and all(observed[k] is True for k in
            ('python_isolated_mode', 'bytecode_writes_disabled', 'user_site_disabled', 'private_environment', 'own_session_and_group')),
            'Observed worker isolation')
        _proof_require(observed['controller_death_signal'] is True, 'Owned controller death signal')
    measurements = resources['worker_resources']
    _proof_require(type(measurements) is list and len(measurements) == workers, 'Worker measurements retained')
    for worker, cleaned in zip(measurements, cleanup):
        _proof_require(type(worker) is list and len(worker) <= cleaned['requests'], 'Per-request measurements')
        for record in worker:
            _proof_require(_proof_int(record['process_peak_rss_bytes']) and record['process_peak_rss_bytes'] > 0 and
                all(_proof_number(record[k]) for k in ('elapsed_seconds', 'process_user_seconds', 'process_system_seconds')),
                'Typed individual worker resource observations')


def _proof_attempt(attempt, records, owner, collector, mode, concurrency, changed, reused, produced=None, require_snapshot_state=True, implementation=None, changed_bytes=None):
    from evaluations.tree_sitter_baseline import PINS
    _proof_require(type(attempt) is dict and attempt['status'] == 'complete' and attempt['mode'] == mode and
        type(attempt['concurrency']) is int and attempt['concurrency'] == concurrency and
        all(_proof_sha(attempt[k]) for k in ('generation', 'source_identity', 'semantic_facts_sha256')), 'Complete typed attempt')
    expected = sorted(records, key=lambda r: r['path'])
    inventory = attempt['inventory']
    _proof_require(type(inventory) is list and len(inventory) == len(expected) and
        [dict((k, row[k]) for k in ('path', 'language', 'kind', 'sha256', 'bytes')) for row in inventory] == expected and
        all(row['status'] == ('configuration' if row['kind'] == 'configuration' else 'parsed') for row in inventory), 'Exact admitted source inventory')
    for key in ('validation_failures', 'collector_failures', 'resolution_errors', 'remaining_inventory'):
        _proof_require(attempt.get(key, []) == [], 'Individual attempt failure retained')
    _proof_require(_proof_sha(owner) and attempt['source_identity'] == digest(canonical(
        {'owner': owner, 'records': expected, 'collector': collector, 'versions': PINS})), 'Recomputed same-owner source identity')
    resources = attempt['resources']; total = sum(row['bytes'] for row in expected)
    _proof_require(resources['source_bytes'] == total and type(resources['source_bytes']) is int and
        type(resources['digest_read_bytes']) is int and resources['digest_read_bytes'] == 2 * total and
        type(resources['digest_read_operations']) is int and resources['digest_read_operations'] == 2 * len(expected) and
        type(resources['changed_files_collected']) is int and resources['changed_files_collected'] == changed and
        type(resources['unchanged_source_collections_reused']) is int and resources['unchanged_source_collections_reused'] == reused and
        resources['all_admitted_bindings_reresolved'] is True and _proof_number(resources['elapsed_seconds']), 'Truthful read/parse/reuse counters')
    counts = attempt['counts']
    _proof_require(type(counts) is dict and set(counts) == {'definitions', 'sites'} and
        all(_proof_int(v, 40000) for v in counts.values()), 'Typed bounded facts')
    resolved = resources['resolve']
    _proof_require(resolved['source_bytes'] == total and
        type(resolved['source_bytes']) is int and _proof_int(resolved['collected_nodes'], 200000) and
        type(resolved['facts_emitted']) is int and resolved['facts_emitted'] == sum(counts.values()) and
        _proof_number(resolved['elapsed_seconds']), 'Resolution counters')
    _proof_queue(resources['queued'], attempt['cleanup'], mode, concurrency, changed, implementation)
    if changed_bytes is None:
        changed_bytes = sum(r['bytes'] for r in expected if r['kind'] == 'source') if changed == sum(r['kind'] == 'source' for r in expected) else 0 if changed == 0 else None
    _proof_require(changed_bytes is not None and resources['queued']['source_bytes'] == changed_bytes and
        sum(map(len, resources['queued']['worker_resources'])) == changed, 'Source bytes and measured parses vs admitted changes')
    if produced is not None:
        return _proof_produced(attempt, produced, collector, require_snapshot_state)


def _proof_produced(attempt, supplied, collector, require_snapshot_state=True):
    """Recompute digests from immutable produced facts, then check source anchors."""
    (references, read_artifact), sources = supplied
    ref = attempt['facts_artifact']; path = _proof_path(ref['path'])
    _proof_require(type(ref) is dict and set(ref) == {'path', 'sha256', 'bytes'} and
        references[path] == ref and _proof_int(ref['bytes'], 8 * 1024 * 1024), 'Bound produced fact artifact')
    state = attempt.get('snapshot_state')
    _proof_require(not require_snapshot_state and state is None or state == {'generation': attempt['generation'], 'matches_returned_generation': True,
        'fresh_facts_retained': True} and all(type(state[k]) is bool for k in
            ('matches_returned_generation', 'fresh_facts_retained')), 'Corresponding complete ready snapshot')
    snapshot = read_artifact(path)
    _proof_require(type(snapshot) is dict and set(snapshot) == {'generation', 'source_identity', 'analyzer_identity', 'facts'} and
        snapshot['generation'] == attempt['generation'] and snapshot['source_identity'] == attempt['source_identity'] and
        snapshot['analyzer_identity'] == collector, 'Produced snapshot identity')
    facts = snapshot['facts']
    _proof_require(type(facts) is dict and set(facts) == {'definitions', 'sites'} and
        all(type(v) is list and 0 < len(v) <= 40000 for v in facts.values()) and sum(map(len, facts.values())) <= 40000,
        'Produced fact bound')
    definitions = {item['id']: item for item in facts['definitions']}
    sites = {item['id']: item for item in facts['sites']}
    _proof_require(len(definitions) == len(facts['definitions']) and len(sites) == len(facts['sites']) and
        attempt['counts'] == {k: len(v) for k, v in facts.items()} and
        digest(canonical(facts)) == attempt['semantic_facts_sha256'] and
        digest(canonical({'source': snapshot['source_identity'], 'analyzer': collector, 'facts': facts})) == attempt['generation'],
        'Recomputed canonical facts and Snapshot generation')
    for kind, items in facts.items():
        for item in items:
            identity = _proof_physical(item, kind == 'sites'); raw = sources[item['path']]; span = item['range']
            start, end = span['start_byte'], span['end_byte']
            _proof_require(item['id'] == identity and end <= len(raw) and item['text'] == raw[start:end].decode('utf-8') and
                span['start_line'] == raw[:start].count(b'\n') + 1 and
                span['end_line'] == raw[:end].count(b'\n') + 1 - int(raw[end-1:end] == b'\n') and
                item['provenance']['source_sha256'] == digest(raw), 'Exact source anchor of emitted fact')
            if kind == 'sites':
                _proof_require(item['role'] in ('call', 'reference') and item['certainty'] in ('resolved', 'candidate', 'unresolved') and
                    type(item['targets_exhaustive']) is bool and type(item['targets']) is list and
                    len(item['targets']) == len(set(item['targets'])) and all(t in definitions for t in item['targets']) and
                    (item['caller'] is None or item['caller'] in definitions), 'Actual relationship identity/uncertainty')
    return facts


def _proof_updates(raw, frozen, collector, produced, implementation=None):
    base = {row['path']: row['content_utf8'].encode() for row in frozen['source']['files']}
    base_meta = {row['path']: {k: row[k] for k in ('path', 'language', 'kind', 'sha256', 'bytes')}
                 for row in frozen['source']['files']}
    expected = frozen['fixture']['updates'] + frozen['source']['updates']
    actual = raw['results']
    _proof_require(type(actual) is list and len(actual) == 36 and len({r['id'] for r in actual}) == 36 and
        {r['id'] for r in actual} == {r['id'] for r in expected}, 'All original16+supplement20 updates required')
    by_id = {r['id']: r for r in actual}; outcomes = []
    for update in expected:
        row = by_id[update['id']]
        try:
            changed, metadata = dict(base), {p: dict(v) for p, v in base_meta.items()}
            for operation in update['operations']:
                path = operation['path']; op = operation['op']
                if op == 'add':
                    changed[path] = operation['content'].encode(); metadata[path] = dict(path=path, language=update['language'], kind='source')
                elif op == 'delete':
                    del changed[path]; del metadata[path]
                elif op == 'rename':
                    changed[operation['to']] = changed.pop(path)
                    metadata[operation['to']] = dict(metadata.pop(path), path=operation['to'])
                elif op == 'replace':
                    changed[path] = changed[path].replace(operation['old'].encode(), operation['new'].encode())
                else:
                    raise ValueError('Unknown frozen source operation')
            for path, content in changed.items():
                metadata[path].update(sha256=digest(content), bytes=len(content))
            source_before = sum(v['kind'] == 'source' for v in base_meta.values())
            source_after = sum(v['kind'] == 'source' for v in metadata.values())
            dirty = sum(v['kind'] == 'source' and v != base_meta.get(p) for p, v in metadata.items())
            _proof_require(row['status'] == 'passed' and row['language'] == update['language'], 'Update status/language')
            attempts = row['attempts']; names = ('base_serial', 'base_queued', 'update_serial', 'update_queued', 'clean_serial')
            _proof_require(type(attempts) is dict and set(attempts) == set(names), 'All five attempts retained')
            owner = row['source_owner_identity']
            produced_facts = {}
            for name in names:
                before = name.startswith('base'); updated = name.startswith('update')
                mode = 'queued' if name.endswith('queued') else 'serial'; concurrency = 2 if mode == 'queued' else 1
                produced_facts[name] = _proof_attempt(attempts[name], list(base_meta.values() if before else metadata.values()), owner,
                    collector, mode, concurrency, source_before if before else dirty if updated else source_after,
                    source_after - dirty if updated else 0, produced=(produced, base if before else changed), implementation=implementation,
                    changed_bytes=sum(v['bytes'] for p, v in (base_meta if before else metadata).items() if v['kind'] == 'source' and
                        (not updated or v != base_meta.get(p))))
            for key in ('generation', 'source_identity', 'semantic_facts_sha256'):
                _proof_require(attempts['base_serial'][key] == attempts['base_queued'][key] and
                    attempts['update_serial'][key] == attempts['update_queued'][key] == attempts['clean_serial'][key], 'Both-mode same-owner equivalence')
            _proof_require(attempts['base_serial']['generation'] != attempts['update_serial']['generation'] and
                attempts['base_serial']['source_identity'] != attempts['update_serial']['source_identity'], 'Fresh update generation/source')
            outcomes.append({'id': update['id'], 'status': 'passed', 'scope': 'same_owner_clean_equivalence'})
            mutation = next((m for m in frozen['oracle']['mutations'] if m['id'] == update['id']), None)
            if mutation is not None and mutation['category'] in ('body', 'export'):
                try:
                    for impact in mutation.get('expected_source_bound_impacts', []):
                        for phase, stages in (('before', ('base_serial', 'base_queued')),
                                              ('after', ('update_serial', 'update_queued', 'clean_serial'))):
                            for stage in stages:
                                _proof_impact(produced_facts[stage], impact[phase])
                    _proof_require(bool(mutation.get('expected_source_bound_impacts')), 'Explicit physical source impacts required')
                    outcomes.append({'id': update['id'] + ':source_impacts', 'status': 'passed', 'scope': 'source_bound_body_export'})
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
                    outcomes.append({'id': update['id'] + ':source_impacts', 'status': 'failed',
                        'scope': 'source_bound_body_export', 'error_kind': type(error).__name__})
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
            outcomes.append({'id': update['id'], 'status': 'failed', 'error_kind': type(error).__name__})
    return outcomes


def _proof_impact(facts, expected):
    """A provided physical oracle is graded after production, independently of parity."""
    declarations = {d['id']: d for d in facts['definitions']}
    anchor = expected['site']
    matches = [site for site in facts['sites'] if site['path'] == anchor['path'] and site['range'] == anchor['range']]
    _proof_require(len(matches) == 1, 'Missing/ambiguous physical impact site')
    site = matches[0]
    _proof_require(site['text'] == anchor['text'] and site['provenance']['source_sha256'] == anchor['source_sha256'] and
        site['certainty'] == expected['certainty'] and ('role' not in expected or site['role'] == expected['role']), 'Source impact/certainty/role')
    def declaration(oracle):
        identity = _proof_physical(oracle); actual = declarations[identity]
        _proof_require(actual['path'] == oracle['path'] and actual['range'] == oracle['range'] and
            actual['text'] == oracle['text'] and actual['name'] == oracle['name'] and
            actual['provenance']['source_sha256'] == oracle['source_sha256'], 'Physical impact declaration')
        return identity
    _proof_require(site['caller'] == declaration(expected['caller_declaration']) and
        set(site['targets']) == {declaration(d) for d in expected['target_declarations']} and
        (site['certainty'] != 'resolved' or site['targets_exhaustive'] is True) and
        (site['certainty'] != 'unresolved' or not site['targets'] and bool(site['reason'])), 'Source impact relationship')
    for key in ('surviving_physical_declaration', 'non_target_physical_declaration'):
        if key in expected:
            identity = declaration(expected[key])
            _proof_require(identity not in site['targets'], 'Non-target physical declaration must not resolve')


def _proof_physical(item, role=False):
    span = item['range']; path = _proof_path(item['path'])
    _proof_require(type(span) is dict and set(span) == {'start_byte', 'end_byte', 'start_line', 'end_line'} and
        all(type(n) is int for n in span.values()) and 0 <= span['start_byte'] < span['end_byte'] and
        1 <= span['start_line'] <= span['end_line'], 'Exact physical source range')
    return f"{path}:{span['start_byte']}:{span['end_byte']}" + (':' + item['role'] if role else '')


def _proof_query_rows(page, oracle, generation):
    declarations = {_proof_physical(d): d for d in oracle['declarations']}
    key_to_id = {d['key']: _proof_physical(d) for d in oracle['declarations']}
    invocations = {_proof_physical(i['site']) + ':call': i for i in oracle['invocations']}
    rows = page['rows']; _proof_require(type(rows) is list and len(rows) <= 256, 'Bounded query rows')
    physical = []
    for row in rows:
        site = row['site']; identity = _proof_physical(site, True)
        expected = invocations[identity]
        _proof_require(site['id'] == identity and site['role'] == 'call' and site['range'] == expected['site']['range'] and
            site['source_sha256'] == oracle['source_sha256'] and row['certainty'] == 'resolved' and
            row['targets_exhaustive'] is True, 'Actual callsite source/certainty')
        handles = []
        for field, key in (('caller', expected['caller_key']), ('target', expected['target_key'])):
            handle = row[field]; actual_id = _proof_physical(handle)
            declaration = declarations[key_to_id[key]]
            _proof_require(handle['id'] == actual_id == key_to_id[key] and handle['range'] == declaration['range'] and
                handle['path'] == declaration['path'] and handle['source_sha256'] == declaration['source_sha256'] and
                handle['name'] == declaration['name'] and handle['name_truncated'] is False, 'Exact physical declaration ownership')
            handles.append(actual_id)
        physical.append((identity, *handles))
    _proof_require(len(set(physical)) == len(physical) and page['generation'] == generation and
        type(page['examined_relationships']) is int and _proof_int(page['examined_relationships'], 100000) and
        type(page['returned_edges']) is int and page['returned_edges'] == len(rows) and
        type(page['returned_entities']) is int and _proof_int(page['returned_entities'], 256) and
        type(page['truncated']) is bool and (page['cursor'] is None or _proof_sha(page['cursor'])) and
        (page['stop_reason'] is None or page['stop_reason'] in ('work_budget_exceeded', 'edge_budget_exceeded',
           'entity_budget_exceeded', 'response_byte_budget_exceeded', 'cancelled', 'deadline_exceeded')), 'Query envelope')
    count = page['total_count']
    _proof_require(type(count) is dict and set(count) == {'value', 'kind'} and
        count['kind'] in ('exact', 'lower_bound', 'unknown') and
        (count['value'] is None if count['kind'] == 'unknown' else _proof_int(count['value'], 40000)), 'Truthful typed count')
    return physical


def _proof_queries(raw, frozen, collector, produced, implementation=None):
    from evaluations.bounded_queries import QUERY_RULE_VERSION
    oracle = frozen['oracle']['query']; source = raw['source']; modes = raw['modes']
    base = frozen['source']['query_freshness_update']['before_content_utf8'].encode()
    _proof_require(source['path'] == oracle['source_path'] and source['sha256'] == digest(base) and
        type(source['bytes']) is int and source['bytes'] == len(base) and source['copy_verified_with_SourceRoot'] is True and
        _proof_sha(source['owner_identity']) and raw['query_rule_version'] == QUERY_RULE_VERSION, 'Copied frozen source/query rule binding')
    _proof_require(type(modes) is list and len(modes) == 2 and {m['mode'] for m in modes} == {'serial', 'queued'}, 'Both query modes required')
    by_key = {d['key']: _proof_physical(d) for d in oracle['declarations']}
    hub = sorted([(_proof_physical(i['site']) + ':call', by_key[i['caller_key']], by_key[i['target_key']])
                  for i in oracle['invocations'] if i['caller_key'] == 'PY.fanout.hub'],
                 key=lambda r: tuple([r[0].rsplit(':', 3)[0], *map(int, r[0].rsplit(':', 3)[1:3])]))
    cycle = [(_proof_physical(i['site']) + ':call', by_key[i['caller_key']], by_key[i['target_key']])
             for caller in ('PY.fanout.cycle_a', 'PY.fanout.cycle_b') for i in oracle['invocations'] if i['caller_key'] == caller]
    expected_ids = {a['id'] for a in oracle['assertions']}; outcomes = []
    bases, changed_attempts = [], []
    for mode in modes:
        name = mode['mode']; concurrency = 1 if name == 'serial' else 2
        refresh = mode['base_refresh']; bases.append(refresh)
        _proof_require(mode['status'] == 'passed' and not mode['failures'] and
            type(mode['configured_concurrency']) is int and mode['configured_concurrency'] == concurrency, 'Complete query mode')
        metadata = [dict(path=source['path'], language='python', kind='source', bytes=len(base), sha256=digest(base))]
        base_facts = _proof_attempt(refresh, metadata, source['owner_identity'], collector, name, concurrency, 1, 0,
            produced=(produced, {source['path']: base}), require_snapshot_state=False, implementation=implementation)
        _proof_require(refresh['facts_artifact']['path'] == name + '/base.facts.json', 'Query base produced snapshot ref')
        declarations = {d['id']: d for d in base_facts['definitions']}
        expected_declarations = {_proof_physical(d): d for d in oracle['declarations']}
        _proof_require(set(declarations) == set(expected_declarations), 'Complete physical source declaration grade')
        for identity, expected_declaration in expected_declarations.items():
            actual_declaration = declarations[identity]
            _proof_require(all(actual_declaration[k] == expected_declaration[k] for k in ('path', 'range', 'name', 'text')) and
                actual_declaration['provenance']['source_sha256'] == expected_declaration['source_sha256'], 'Physical source declaration grade')
        sites = {s['id']: s for s in base_facts['sites']}
        _proof_require(set(sites) == {_proof_physical(i['site']) + ':call' for i in oracle['invocations']}, 'Complete physical source invocation grade')
        for invocation in oracle['invocations']:
            actual_site = sites[_proof_physical(invocation['site']) + ':call']
            _proof_require(actual_site['certainty'] == 'resolved' and actual_site['targets_exhaustive'] is True and
                actual_site['caller'] == by_key[invocation['caller_key']] and actual_site['targets'] == [by_key[invocation['target_key']]],
                'Physical source target/caller grade')
        _proof_require(mode['actual_base_workers_started'] == refresh['resources']['queued']['workers_started'] == 1 and
            type(mode['actual_base_workers_started']) is int and refresh['counts'] == {'definitions': 115, 'sites': 115}, 'Actual single-file mode work')
        grade = mode['source_fact_grade']
        _proof_require(grade['status'] == 'passed' and type(grade['definitions']) is int and grade['definitions'] == 115 and
            type(grade['invocation_occurrences']) is int and grade['invocation_occurrences'] == 115 and
            type(grade['source_range_checks']) is int and grade['source_range_checks'] == 1035 and
            grade['all_targets_from_physical_source_oracle'] is True and grade['raw_source_oracle_not_extractor_input'] is True,
            'Source grading receipt')
        actual = mode['queries']
        _proof_require(type(actual) is list and len(actual) == 10 and len({a['id'] for a in actual}) == 10 and
                       {a['id'] for a in actual} == expected_ids, 'All ten frozen query assertions required')
        for entry in actual:
            qid = entry['id']
            try:
                _proof_require(entry['status'] == 'passed' and not entry.get('error_kind'), 'Query assertion completion')
                responses = entry['responses']; _proof_require(type(responses) is list and 1 <= len(responses) <= 12, 'Bounded nonempty actual responses')
                pages, keys = [], []
                for response in responses:
                    page = response['response']; keys.append(_proof_query_rows(page, oracle, refresh['generation'])); pages.append(page)
                    seed_name = next(a['seed'] for a in oracle['assertions'] if a['id'] == qid)
                    seed_id = next(_proof_physical(d) for d in oracle['declarations'] if d['name'] == seed_name)
                    _proof_require(page['returned_entities'] == len({row[2] for row in keys[-1] if row[2] != seed_id}), 'Actual distinct nonseed entity count')
                    _proof_require(type(response['serialized_response_bytes']) is int and
                        response['serialized_response_bytes'] == len(canonical(page)) and
                        _proof_number(response['real_elapsed_seconds_observed']) and response['serialized_response_bytes'] <= 32768,
                        'Whole serialized response measurement')
                first = pages[0]; flat = [r for page in keys for r in page]; examined = sum(p['examined_relationships'] for p in pages)
                if qid == 'Q-PY-ALL-CALLEES':
                    _proof_require(flat == hub and examined == 113 and pages[-1]['cursor'] is None and
                        pages[-1]['total_count'] == {'value': 113, 'kind': 'exact'} and entry['rows'] == 113 and
                        type(entry['rows']) is int and entry['examined_total'] == 113 and type(entry['examined_total']) is int and
                        type(entry['pages']) is int and entry['pages'] == len(pages), 'Physical complete fanout')
                elif qid == 'Q-PY-WORK-EXHAUSTION':
                    _proof_require(len(pages) == 2 and keys[0] == hub[:7] and keys[1] == hub[7:8] and
                        first['examined_relationships'] == 7 and first['stop_reason'] == 'work_budget_exceeded' and
                        first['cursor'] is not None and first['truncated'] is True and
                        first['total_count'] == {'value': 7, 'kind': 'lower_bound'}, 'Work charging/next physical row')
                elif qid == 'Q-PY-FILTERED-WORK':
                    filtered = [r for r in hub if next(d['name'] for d in oracle['declarations'] if _proof_physical(d) == r[2]).startswith('leaf_1')]
                    _proof_require(len(pages) == 2 and not keys[0] and first['examined_relationships'] == 7 and
                        first['truncated'] is True and first['stop_reason'] == 'work_budget_exceeded' and
                        first['total_count'] == {'value': 0, 'kind': 'lower_bound'} and keys[1] == filtered and
                        pages[1]['examined_relationships'] == 113 and pages[1]['total_count'] == {'value': 12, 'kind': 'exact'}, 'Rejected rows charge work; no oracle count leakage')
                elif qid == 'Q-PY-OUTPUT-PAGES':
                    sizes = [len(k) for k in keys]
                    _proof_require(flat == hub and sizes == [17, 17, 17, 17, 17, 17, 11] and examined == 113 and
                        entry['page_sizes'] == sizes and all(type(v) is int for v in entry['page_sizes']) and
                        entry['rows'] == 113 and type(entry['rows']) is int and entry['examined_total'] == 113 and
                        type(entry['examined_total']) is int and all(p['cursor'] is not None for p in pages[:-1]) and pages[-1]['cursor'] is None,
                        'Occurrence page continuity')
                    rejection = entry['cursor_binding_checks']
                    _proof_require(type(rejection) is list and len(rejection) == 7 and
                        {r['binding'] for r in rejection} == {'role', 'scope', 'prefix', 'operation', 'depth', 'query_rule', 'expiry'} and
                        all(r['status'] == 'rejected_before_row_materialization' for r in rejection), 'Cursor binding checks')
                elif qid == 'Q-PY-RESPONSE-BYTES':
                    n = len(keys[0])
                    _proof_require(len(pages) == 2 and responses[0]['serialized_response_bytes'] <= 1024 and
                        first['examined_relationships'] < 113 and first['cursor'] is not None and
                        first['stop_reason'] == 'response_byte_budget_exceeded' and keys[0] == hub[:n] and keys[1] == hub[n:n+1] and
                        pages[1]['examined_relationships'] == 0 and entry['first_response_rows'] == n and
                        type(entry['first_response_rows']) is int and entry['first_response_work'] == first['examined_relationships'] and
                        type(entry['first_response_work']) is int and entry['resumed_cached_work'] == 0 and type(entry['resumed_cached_work']) is int,
                        'Whole response cap and pending occurrence')
                    candidate = dict(first, rows=first['rows'] + pages[1]['rows'], returned_edges=n + 1,
                        returned_entities=len({row[2] for row in keys[0] + keys[1]}),
                        total_count={'value': n + 1, 'kind': 'lower_bound'})
                    _proof_require(len(canonical(candidate)) > 1024 and first['examined_relationships'] == n + 1,
                        'Response bytes stop before the retained pending occurrence')
                elif qid == 'Q-PY-CYCLE-REACHABILITY':
                    _proof_require(len(pages) == 1 and keys[0] == cycle and first['examined_relationships'] == 2 and
                        first['returned_entities'] == 1 and first['cursor'] is None and first['total_count'] == {'value': 2, 'kind': 'exact'}, 'Physical bounded cycle including seed edge')
                elif qid == 'Q-PY-CANCEL-BEFORE':
                    _proof_require(len(pages) == 1 and keys[0] == [] and first['examined_relationships'] == 0 and
                        first['stop_reason'] == 'cancelled' and first['cursor'] is None, 'Cancellation before work')
                elif qid == 'Q-PY-CANCEL-DURING':
                    _proof_require(len(pages) == 2 and keys[0] == hub[:len(keys[0])] and len(keys[0]) <= 3 and
                        first['examined_relationships'] <= 3 and first['stop_reason'] == 'cancelled' and first['cursor'] is None and
                        keys[1] == hub[:1] and type(entry['cancellation_callback_calls']) is int and entry['cancellation_callback_calls'] >= 4,
                        'Cooperative cancellation and usable retained snapshot')
                elif qid == 'Q-PY-DEADLINE':
                    _proof_require(len(pages) == 1 and keys[0] == hub[:3] and first['examined_relationships'] == 3 and
                        first['stop_reason'] == 'deadline_exceeded' and first['cursor'] is None and
                        type(entry['clock_scope']) is str and entry['clock_scope'] == 'Injected integer monotonic control-flow ticks; not a latency measurement.', 'Explicit fake-clock control flow')
                elif qid == 'Q-PY-STALE-GENERATION':
                    _proof_require(len(pages) == 2 and keys[0] == hub[:17] and keys[1] == hub[17:18] and
                        all(entry[k] is True for k in ('old_cursor_rejected_against_new_generation', 'old_physical_seed_refused',
                            'topology_unchanged', 'generation_changed', 'old_snapshot_coherent')) and
                        entry['old_generation'] == refresh['generation'] and entry['new_generation'] != refresh['generation'], 'Old generation/physical seed rejection')
                    change = frozen['source']['query_freshness_update']; new = change['after_content_utf8'].encode()
                    changed_facts = _proof_attempt(entry['changed_refresh'], [dict(metadata[0], sha256=digest(new), bytes=len(new))],
                        source['owner_identity'], collector, name, concurrency, 1, 0,
                        produced=(produced, {source['path']: new}), require_snapshot_state=False, implementation=implementation)
                    _proof_require(entry['changed_refresh']['facts_artifact']['path'] == name + '/changed.facts.json', 'Query changed snapshot ref')
                    changed_attempts.append(entry['changed_refresh'])
                    def topology(facts):
                        names = {d['id']: d['name'] for d in facts['definitions']}
                        return sorted((names[s['caller']], tuple(names[t] for t in s['targets']), s['role'], s['text']) for s in facts['sites'])
                    _proof_require(topology(base_facts) == topology(changed_facts), 'Actual topology unchanged while source generation changes')
                    _proof_require(entry['new_generation'] == entry['changed_refresh']['generation'] and
                        entry['changed_refresh']['source_identity'] != refresh['source_identity'], 'Fresh source generation after body change')
                # Count observations are recomputed from this assertion's produced physical rows.
                if qid in ('Q-PY-ALL-CALLEES', 'Q-PY-OUTPUT-PAGES'):
                    cumulative = 0
                    for index, page in enumerate(pages):
                        cumulative += len(keys[index])
                        _proof_require(page['total_count'] == {'value': cumulative, 'kind': 'exact' if index == len(pages)-1 else 'lower_bound'},
                            'Completion count vs observed occurrence pages')
                elif qid in ('Q-PY-WORK-EXHAUSTION', 'Q-PY-RESPONSE-BYTES', 'Q-PY-STALE-GENERATION'):
                    _proof_require(first['total_count'] == {'value': len(keys[0]), 'kind': 'lower_bound'} and
                        pages[1]['total_count'] == {'value': len(keys[0]) + len(keys[1]), 'kind': 'lower_bound'}, 'Continuation count from observed rows')
                elif qid in ('Q-PY-CANCEL-BEFORE', 'Q-PY-CANCEL-DURING', 'Q-PY-DEADLINE'):
                    _proof_require(first['total_count'] == {'value': len(keys[0]), 'kind': 'lower_bound'}, 'Interrupted count is not exact')
                outcomes.append({'id': name + ':' + qid, 'status': 'passed'})
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
                outcomes.append({'id': name + ':' + qid, 'status': 'failed', 'error_kind': type(error).__name__})
    for key in ('source_identity', 'generation', 'semantic_facts_sha256'):
        _proof_require(bases[0][key] == bases[1][key] and len(changed_attempts) == 2 and
            changed_attempts[0][key] == changed_attempts[1][key], 'Same-owner base/changed query mode equivalence')
    _proof_require(raw['mode_equivalence'] == {'semantic_facts_sha256_equal': True, 'generation_equal': True,
        'source_identity_equal': True, 'same_source_owner': True} and all(type(v) is bool for v in raw['mode_equivalence'].values()), 'Recomputed query equivalence receipt')
    return outcomes


def _proof_worker_sources(root, hashes):
    import ast
    with SourceRoot(root) as source:
        raw, sha, info = source.read('evaluations/engine_checks.py', 1024 * 1024 + 1, hash_full=False)
    _proof_require(len(raw) == info.st_size and len(raw) <= 1024 * 1024 and
        sha == hashes['evaluations/engine_checks.py'], 'Bound controller source')
    found = {}
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('_MISSING_SETUP', '_MISSING_PROBE'):
                    value = ast.literal_eval(node.value)
                    _proof_require(type(value) is str, 'Literal finite worker source required')
                    found['setup' if target.id == '_MISSING_SETUP' else 'probe'] = digest(value.encode())
    _proof_require(set(found) == {'setup', 'probe'}, 'Both finite worker sources required')
    return found


def _proof_missing(raw, frozen, hashes, archive_files, read_artifact, worker_sources, expected_project_name=None):
    """Recompute nine checks from bound raw observations, never status alone."""
    import os
    from evaluations.tree_sitter_baseline import PINS
    if type(raw.get('binding_after')) is not dict or raw['binding_after'].get('implementation_stable') is not True:
        raise MissingBackendStabilityError('Missing-backend after binding stability')
    failure_fields = ('runtime_registration_failure', 'runtime_cleanup_failure', 'log_or_probe_failure',
        'publication_failure', 'stale_receipt_removal_failure', 'archive_failure')
    if any(key in raw for key in failure_fields):
        raise MissingBackendRetainedFailureError('Retained missing-backend failure')
    probe = raw['probe']; _proof_require(type(probe) is dict and probe['status'] == 'passed' and
        type(probe['schema_version']) is int and probe['schema_version'] == 1 and not probe.get('driver_failure'), 'Actual typed missing-backend probe')
    control_ref = archive_files['control.json']; _proof_require(probe['control_sha256'] == raw['control_sha256'] == control_ref['sha256'], 'Control file identity')
    control = read_artifact('control.json')
    _proof_require(type(control['schema_version']) is int and control['schema_version'] == 1 and
        control['setup_script_sha256'] == worker_sources['setup'] and control['probe_script_sha256'] == worker_sources['probe'],
        'Stage script bytes bound to current controller source')
    _proof_require(control['producer_token'] == probe['producer_token'] and control['source_records'] == probe['source_records'] and
        control['implementation_sha256'] == probe['implementation_sha256'], 'Actual control/probe identity')
    sources = {'main.py': b'from helper import finish\n\ndef run():\n    return finish()\n',
               'helper.py': b'def finish():\n    return 1\n'}
    _proof_require(probe['source_records'] == [dict(path=p, language='python', kind='source', sha256=digest(b), bytes=len(b))
        for p, b in sources.items()], 'Exact source-only missing-backend records')
    components = probe['components']
    _proof_require(type(components) is list and len(components) == 2 and [r['mode'] for r in components] == ['serial', 'queued'], 'Both actual missing-backend modes')
    stages = raw['stages']; _proof_require(type(stages) is list and len(stages) == 2 and [s['stage'] for s in stages] == ['venv', 'probe'], 'Both finite stages')
    for stage in stages:
        _proof_require(stage['status'] == 'passed' and type(stage['returncode']) is int and stage['returncode'] == 0 and
            stage['stop_reason'] is None and type(stage['pid']) is int and stage['pid'] > 0 and
            _proof_sha(stage['worker_source_sha256']) and _proof_number(stage['elapsed_seconds']), 'Successful bounded owned stage')
        _proof_cleanup([stage['cleanup']], 1, False)
        _proof_require(stage['worker_source_sha256'] == control['setup_script_sha256' if stage['stage'] == 'venv' else 'probe_script_sha256'], 'Observed worker source binding')
    _proof_require(type(probe['producer_pid']) is int and probe['producer_pid'] == stages[1]['pid'] and
        type(probe['producer_token']) is str and len(probe['producer_token']) == 32 and
        all(c in '0123456789abcdef' for c in probe['producer_token']) and probe['implementation_sha256'] ==
        {k: hashes[k] for k in _ADAPTER_HELPERS + _MISSING_EXTRA}, 'Probe producer identity')
    checks = probe['checks']; recomputed = {}
    pre = probe['pre_bootstrap_absent_optional_backend']; post = probe['absent_optional_backend']
    for observations in (pre, post):
        _proof_require(type(observations) is dict and set(observations) == set(PINS) and
            all(type(v) is dict and v['module_spec_absent'] is True and v['distribution_version'] is None for v in observations.values()), 'All five optional distributions/modules absent')
    recomputed[_MISSING_NINE[0]] = probe['pre_bootstrap_distributions'] == []
    recomputed[_MISSING_NINE[1]] = all(v['module'] == name.replace('-', '_') for name, v in post.items())
    import re
    metadata = probe['source_checkout_metadata_after_bootstrap']
    if type(expected_project_name) is not str or type(metadata) is not list or len(metadata) > 128 or not all(
            type(name) is str and len(name) <= 128 and re.sub(r'[-_.]+', '-', name).lower() == expected_project_name for name in metadata):
        raise MissingBackendMetadataError('Only committed source-checkout project metadata after bootstrap')
    cli = probe['cli']; _proof_require(type(cli) is list and len(cli) == 2, 'Actual map and keyword CLI records')
    recomputed[_MISSING_NINE[2]] = [r['command'] for r in cli] == ['core-map', 'core-keyword-search'] and all(type(r['returncode']) is int and r['returncode'] == 0 for r in cli)
    core = probe['core']; scan = core['scan']; keyword = core['keyword']
    recomputed[_MISSING_NINE[3]] = (type(core['file_count']) is int and core['file_count'] == 2 and
        type(scan['scanned']) is int and scan['scanned'] == 2 and scan['secure_reads'] is True and
        scan['failed'] == 0 and type(scan['failed']) is int and scan['failures'] == [] and
        keyword['mode'] == 'keyword' and type(keyword['documents']) is int and keyword['documents'] == 2 and
        type(keyword['results']) is int and 0 < keyword['results'] <= 2)
    candidates, queues, cleanup = [], [], []
    for component in components:
        mode = component['mode']; concurrency = 1 if mode == 'serial' else 2
        result = component['candidate']; queue = component['queue']
        _proof_require(type(component['configured_concurrency']) is int and component['configured_concurrency'] == concurrency, 'Missing-backend configured concurrency')
        candidates.append(result['status'] == 'failed' and result['error_kind'] == 'PackageNotFoundError' and
            result['previous_generation'] is None and result['cache_or_ready_snapshot_published'] is False and
            component['candidate_snapshot_is_none'] is True and type(component['candidate_cache_entries']) is int and
            component['candidate_cache_entries'] == 0 and result['mode'] == mode and result['concurrency'] == concurrency and
            type(result['concurrency']) is int and result['resources']['digest_read_bytes'] == 0 and
            type(result['resources']['digest_read_bytes']) is int and result['resources']['digest_read_operations'] == 0 and
            type(result['resources']['digest_read_operations']) is int)
        queues.append(queue['status'] == 'failed' and queue['stop_reason'] == 'backend_unavailable' and
            type(queue['collected']) is int and queue['collected'] == 0 and type(queue['failures']) is list and
            any(r['kind'] == 'BackendUnavailable' and r['reason'] == 'backend_unavailable' for r in queue['failures']))
        _proof_queue(queue['resources'], queue['cleanup'], mode, concurrency, implementation=hashes)
        failures = queue['failures']; admitted = queue['resources']['files_admitted']
        _proof_require(len(failures) == admitted and len({f['index'] for f in failures}) == admitted and
            all(type(f['index']) is int and 0 <= f['index'] < 2 and f['record'] == probe['source_records'][f['index']] and
                f['reason'] == 'backend_unavailable' for f in failures), 'Every actual backend refusal retained')
        cleanup.append(queue['resources']['workers_started'] > 0 and queue['resources']['files_collected'] == 0)
    recomputed[_MISSING_NINE[4]] = all(candidates)
    recomputed[_MISSING_NINE[5]] = all(queues)
    recomputed[_MISSING_NINE[6]] = all(cleanup)
    isolation = probe['isolation']
    recomputed[_MISSING_NINE[7]] = (type(isolation['sys_prefix']) is str and type(isolation['sys_base_prefix']) is str and
        isolation['sys_prefix'] != isolation['sys_base_prefix'] and isolation['pip_module_spec_absent'] is True)
    job = isolation['job_directory']; private = isolation['private_environment']
    _proof_require(type(job) is str and Path(job).is_absolute() and type(private) is dict and
        set(private) == {'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'TMPDIR'}, 'Raw isolated environment fields')
    expected = {key: str(Path(job) / name) for key, name in [('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'),
        ('XDG_CACHE_HOME', 'cache'), ('XDG_DATA_HOME', 'data'), ('TMPDIR', 'tmp')]}
    recomputed[_MISSING_NINE[8]] = (all(isolation[k] is True for k in ('python_isolated_mode', 'bytecode_writes_disabled', 'user_site_disabled')) and
        all(type(isolation[k]) is int and isolation[k] > 0 for k in ('pid', 'sid', 'pgrp')) and
        isolation['pid'] == isolation['sid'] == isolation['pgrp'] == probe['producer_pid'] and private == expected)
    _proof_require(type(checks) is dict and set(checks) == set(_MISSING_NINE) and
        all(type(v) is bool for v in checks.values()) and checks == recomputed and all(recomputed.values()), 'Recomputed actual nine typed checks')
    _proof_missing_evidence(raw, archive_files, read_artifact)
    _proof_require(raw['owned_runtime_removed'] is True and type(raw['runtime_cleanup']) is list and
        len(raw['runtime_cleanup']) == 8 and {r['path'] for r in raw['runtime_cleanup']} == {'venv', 'source', 'map', 'home', 'config', 'cache', 'data', 'tmp'} and
        all(r['removed'] is True for r in raw['runtime_cleanup']), 'Owned runtime cleanup with retained evidence')
    _proof_require(raw['limits'] == {'worker_wall_seconds': 30, 'address_space_bytes': 512 * 1024 * 1024,
        'cpu_seconds': 30, 'per_file_bytes': 1024 * 1024} and all(type(v) is int for v in raw['limits'].values()), 'Finite missing-backend limits')
    resources = probe['resources']
    _proof_require(all(_proof_number(resources[k]) for k in ('elapsed_seconds', 'process_user_seconds',
        'process_system_seconds', 'reaped_children_user_seconds', 'reaped_children_system_seconds')) and
        all(_proof_int(resources[k], 512 * 1024 * 1024) for k in ('process_peak_rss_bytes', 'reaped_children_peak_rss_bytes')),
        'Actual typed process resource observations')
    return [{'id': key, 'status': 'passed'} for key in _MISSING_NINE]


def _proof_missing_evidence(raw, archive_files, read_artifact):
    """Recompute retention from full-byte-bound logs and produced metadata."""
    import re
    try:
        required = {'result.json', 'control.json', 'probe.json', 'venv-stage.json', 'probe-stage.json'}
        for stage in ('venv', 'probe', 'core-map', 'core-keyword-search'):
            required.update({stage + '.stdout.log', stage + '.stderr.log'})
        _proof_require(required <= set(archive_files), 'Actual stage/probe/control/CLI archive required')
        _proof_require(type(raw['archive_directory']) is str and type(raw['logs']) is list and len(raw['logs']) <= 2048,
                       'Actual retained log references')
        expected = {raw['archive_directory'] + '/' + path: ref for path, ref in archive_files.items() if path != 'result.json'}
        actual = {}
        for ref in raw['logs']:
            _proof_require(type(ref) is dict and set(ref) == {'path', 'sha256', 'bytes'} and ref['path'] not in actual and
                _proof_sha(ref['sha256']) and _proof_int(ref['bytes'], 1024 * 1024), 'Bounded unique retained log reference')
            actual[ref['path']] = {'sha256': ref['sha256'], 'bytes': ref['bytes']}
        _proof_require(actual == {path: {'sha256': ref['sha256'], 'bytes': ref['bytes']} for path, ref in expected.items()},
                       'Every archived metadata/log reference retained')
        _proof_require(read_artifact('probe.json') == raw['probe'], 'Actual archived probe content')
        for stage in raw['stages']:
            _proof_require(read_artifact(stage['stage'] + '-stage.json') == stage, 'Actual archived stage content')
        setup = read_artifact('venv.stdout.log')
        _proof_require(type(setup) is dict and setup.get('venv_created') is True and setup.get('with_pip') is False and
            setup.get('system_site_packages') is False and setup.get('symlinks') is True, 'Actual empty-venv creation log')
        printed_probe = read_artifact('probe.stdout.log')
        _proof_require(printed_probe == {'status': raw['probe']['status'], 'checks': raw['probe']['checks']}, 'Actual probe completion log')
        keyword = read_artifact('core-keyword-search.stdout.log')
        _proof_require(type(keyword) is dict and keyword.get('mode') == raw['probe']['core']['keyword']['mode'] and
            type(keyword.get('documents')) is int and keyword['documents'] == raw['probe']['core']['keyword']['documents'] and
            type(keyword.get('results')) is list and len(keyword['results']) == raw['probe']['core']['keyword']['results'],
            'Actual keyword CLI completion log')
        map_log = read_artifact('core-map.stdout.log', parsed=False)
        _proof_require(type(map_log) is bytes and b'Search: 2 documents, ' in map_log and
            any(line.startswith(b'2 files, ') and b'; 2 scanned, ' in line for line in map_log.splitlines()), 'Actual core map CLI completion log')
        for stage in ('venv', 'probe', 'core-map', 'core-keyword-search'):
            _proof_require(type(read_artifact(stage + '.stderr.log', parsed=False)) is bytes, 'Retained stderr log')
        receipts = [path for path in archive_files if re.fullmatch(r'components/run-[0-9a-f]{32}/receipt\.json', path)]
        _proof_require(len(receipts) == 2, 'Both actual queue component receipts')
        by_mode = {}
        for path in receipts:
            receipt = read_artifact(path)
            mode = receipt['resources']['mode']
            _proof_require(mode in ('serial', 'queued') and mode not in by_mode, 'Unique actual queue receipt mode')
            by_mode[mode] = receipt
            queue = next(row['queue'] for row in raw['probe']['components'] if row['mode'] == mode)
            expected_receipt = {key: queue[key] for key in ('status', 'stop_reason', 'resources', 'cleanup')}
            expected_receipt.update(failures=[{key: value for key, value in row.items() if key != 'record'} for row in queue['failures']],
                                    runtime_qualified=False, selected_engine=None)
            _proof_require(canonical(receipt) == canonical(expected_receipt), 'Actual queue receipt content')
            parent = path.rsplit('/', 1)[0]
            for worker in range(queue['resources']['workers_started']):
                for suffix in ('stdout.log', 'stderr.log'):
                    log_path = parent + '/worker-' + str(worker) + '/' + suffix
                    _proof_require(log_path in archive_files and type(read_artifact(log_path, parsed=False)) is bytes,
                                   'Every actual queue worker log retained')
        _proof_require(set(by_mode) == {'serial', 'queued'}, 'Both retained queue modes')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        raise MissingBackendEvidenceError('Missing or inconsistent archived metadata/log evidence') from None


def _proof_observed(record, kind):
    """Keep bounded recorded statuses under positional labels before admission."""
    if type(record) is not dict:
        return []
    def status(value):
        return value if value in ('passed', 'failed', 'complete', 'partial', 'interrupted', 'running', 'blocked') else 'invalid'
    rows = []
    if kind == 'updates' and type(record.get('results')) is list:
        for index, row in enumerate(record['results'][:36]):
            if type(row) is dict:
                attempts = row.get('attempts', {})
                rows.append({'id': 'update:' + str(index), 'recorded_status': status(row.get('status')),
                    'attempt_statuses': {name: status(attempts[name].get('status')) for name in
                        ('base_serial', 'base_queued', 'update_serial', 'update_queued', 'clean_serial')
                        if type(attempts) is dict and type(attempts.get(name)) is dict}})
    elif kind == 'queries' and type(record.get('modes')) is list:
        for mode in record['modes'][:2]:
            if type(mode) is dict and mode.get('mode') in ('serial', 'queued') and type(mode.get('queries')) is list:
                rows.extend({'id': mode['mode'] + ':query:' + str(index), 'recorded_status': status(row.get('status'))}
                    for index, row in enumerate(mode['queries'][:10]) if type(row) is dict)
    elif kind == 'missing_backend':
        for stage in record.get('stages', [])[:2] if type(record.get('stages')) is list else []:
            if type(stage) is dict and stage.get('stage') in ('venv', 'probe'):
                rows.append({'id': stage['stage'], 'recorded_status': status(stage.get('status'))})
    return rows


# Delivery is separate from the unchanged before/after measurement transaction.
_DELIVERY_OUTPUTS = ('evaluations/results/code-understanding/engine-comparison.json',
                     'evaluations/results/code-understanding/engine.json')
_DELIVERY_EXPERIMENT_PATHS = ('evaluations/analysis.py', 'evaluations/acceptance.py', 'evaluations/real_calls.py',
    'evaluations/engine_checks.py', 'evaluations/tree_sitter_baseline.py', 'repo_graph/source.py',
    'evaluations/incremental_candidate.py', 'evaluations/queued_collector.py', 'evaluations/bounded_queries.py',
    'evaluations/supplement_preparation.py', 'evaluations/code-understanding/supplement-source.json',
    'evaluations/code-understanding/supplement-oracle.json', 'evaluations/code-understanding/supplement-lock.json',
    'evaluations/code-understanding/source-target-lock.json', 'evaluations/code-understanding/source-target-locations.json',
    'pyproject.toml', 'uv.lock')


def _proof_delivery(root, measured, hashes, *, validation_commit=None):
    """Admit at most32 linear result-only commits; bind both regular source snapshots.

    Existing experiment producers still require one unchanged HEAD throughout
    measurement. Commit objects, not mutable traversal overrides, define parents.
    This guard creates no files and executes only finite read-only Git commands.
    """
    import os
    import re
    import time
    from evaluations.real_calls import revision_blob_identity
    revision = lambda value: type(value) is str and re.fullmatch(r'[0-9a-f]{40}', value) is not None
    _proof_require(revision(measured) and (validation_commit is None or revision(validation_commit)) and
        type(hashes) is dict and 1 <= len(hashes) <= 128 and all(_proof_sha(value) for value in hashes.values()),
        'Typed bounded measured revision binding')
    paths = sorted(_proof_path(path) for path in hashes)
    _proof_require(not set(paths) & set(_DELIVERY_OUTPUTS), 'Evidence outputs cannot be measured source inputs')
    deadline = time.monotonic() + 20
    with SourceRoot(Path(root)) as source:
        def git(args, *, cap=1024, allowed=(0,), output=True):
            remaining = deadline - time.monotonic()
            _proof_require(remaining > 0, 'Delivery metadata deadline')
            result = subprocess.run(['git', '--no-pager', '--no-replace-objects',
                '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                '-c', 'core.preloadIndex=false', '-c', 'index.threads=1'] + args,
                cwd=Path('/proc/self/fd') / str(source.fd), pass_fds=(source.fd,),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'),
                timeout=min(5, remaining))
            _proof_require(result.returncode in allowed and (not output or len(result.stdout) <= cap),
                           'Bounded delivery Git metadata unavailable')
            return result.stdout if output else result.returncode
        def head():
            value = git(['rev-parse', '--verify', 'HEAD']).decode('ascii').strip()
            _proof_require(revision(value), 'Full delivery checkout commit')
            return value
        top = os.fsdecode(git(['rev-parse', '--show-toplevel'], cap=4096).rstrip(b'\n'))
        with SourceRoot(Path(top)) as named:
            _proof_require(named.identity == source.identity, 'Delivery checkout must own the Git root')
        current = head()
        _proof_require(validation_commit is None or current == validation_commit, 'Validation checkout commit changed')
        def parent_of(child, *, measured_source=False):
            size_raw = git(['cat-file', '-s', child], cap=32).strip()
            _proof_require(size_raw.isdigit() and 0 < int(size_raw) <= 1024 * 1024, 'Delivery commit object byte ceiling')
            size = int(size_raw)
            raw = git(['cat-file', 'commit', child], cap=size)
            _proof_require(len(raw) == size and hashlib.sha1(b'commit ' + str(size).encode('ascii') + b'\0' + raw).hexdigest() == child,
                           'Exact immutable delivery commit object')
            header, separator, _ = raw.partition(b'\n\n')
            _proof_require(separator, 'Delivery commit header')
            parents = [line[7:].decode('ascii') for line in header.splitlines() if line.startswith(b'parent ')]
            _proof_require((len(parents) <= 32 if measured_source else len(parents) == 1) and
                all(revision(p) for p in parents), 'Bounded measured parents and linear delivery history required')
            return parents[0] if parents else None
        child, distance = current, 0
        while child != measured:
            _proof_require(distance < 32, 'Result-only delivery chain exceeds32 commits')
            parent = parent_of(child)
            _proof_require(parent is not None, 'Measured revision is not an ancestor')
            exclusions = [':(top,literal,exclude)' + path for path in _DELIVERY_OUTPUTS]
            git(['diff-tree', '-r', '--quiet', '--no-ext-diff', '--no-textconv', '--no-renames', parent, child,
                 '--', '.'] + exclusions, cap=0, output=False)
            entries = git(['--literal-pathspecs', 'ls-tree', '--full-tree', '-z', child, '--'] + list(_DELIVERY_OUTPUTS), cap=1024)
            seen = set()
            for entry in entries.split(b'\0'):
                if not entry:
                    continue
                metadata, separator, path = entry.partition(b'\t'); fields = metadata.split()
                name = os.fsdecode(path)
                _proof_require(separator and name in _DELIVERY_OUTPUTS and name not in seen and len(fields) == 3 and
                    fields[0] == b'100644' and fields[1] == b'blob' and re.fullmatch(b'[0-9a-f]{40}', fields[2]),
                    'Delivery outputs must remain regular data blobs')
                seen.add(name)
            child, distance = parent, distance + 1
        # Verify measured itself is an actual commit, including zero-distance admission.
        parent_of(measured, measured_source=True)
        for path in paths:
            raw, sha, info = source.read(path, 1024 * 1024 + 1, hash_full=False)
            _proof_require(len(raw) == info.st_size and len(raw) <= 1024 * 1024 and sha == hashes[path],
                           'Current delivery source bytes differ from measured binding')
            for target in {measured, current}:
                _proof_require(time.monotonic() < deadline, 'Delivery metadata deadline')
                identity = revision_blob_identity(source, target, path, raw)
                _proof_require(identity['status'] == 'verified' and
                    identity['mode'] == ('100755' if info.st_mode & 0o111 else '100644'), 'Regular committed measured/current source blob required')
        for cached in (False, True):
            args = ['diff', '--quiet', '--no-ext-diff', '--no-textconv', '--no-renames']
            if cached:
                args += ['--cached', current]
            git(args + ['--'] + [':(top,literal)' + path for path in paths], cap=0, output=False)
        _proof_require(head() == current and _proof_directory(source.root)[1] == source.identity,
                       'Delivery checkout changed during validation')
    return {'measured_commit': measured, 'validation_commit': current,
            'result_only_descendant_commits': distance, 'maximum_descendant_commits': 32,
            'linear_history': True, 'measured_and_current_blobs_match': True}


def adapter_proof_checks(report, *, root=ROOT, evidence_root=None, source_map=None):
    """Validate archives and source-bound produced observations, retaining each gap."""
    checks, individual = [], {}
    observed = {kind: _proof_observed(report.get(kind), kind) for kind in ('updates', 'queries', 'missing_backend')} if type(report) is dict else {}
    failures = (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, MemoryError, subprocess.SubprocessError)
    try:
        directory, evidence_owner = _proof_evidence_root(root, evidence_root, source_map)
        frozen = _proof_frozen(root)
        with SourceRoot(root) as source:
            hashes = {}
            for name in sorted(set(_ADAPTER_HELPERS + _MISSING_EXTRA + _DELIVERY_EXPERIMENT_PATHS)):
                raw, sha, info = source.read(name, 1024 * 1024 + 1, hash_full=False)
                _proof_require(len(raw) == info.st_size and len(raw) <= 1024 * 1024, 'Implementation byte ceiling')
                hashes[name] = sha
                if name == 'pyproject.toml':
                    project_name = _proof_project_name(raw)
        validation_revision = subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], cwd=root, text=True, timeout=20).strip()
        revision = report['implementation']['commit']
        recorded = report['implementation']['sha256']
        _proof_require(type(recorded) is dict and set(recorded) == set(_DELIVERY_EXPERIMENT_PATHS) and
            all(recorded[path] == hashes[path] for path in recorded), 'Complete measured experiment implementation')
        real_implementation = report['real_calls']['implementation']
        real_paths = ('evaluations/real_calls.py', 'evaluations/analysis.py', 'evaluations/acceptance.py',
                      'evaluations/tree_sitter_baseline.py', 'repo_graph/source.py', 'pyproject.toml', 'uv.lock')
        _proof_require(real_implementation['commit'] == revision and type(real_implementation['sha256']) is dict and
            set(real_implementation['sha256']) == set(real_paths) and
            all(real_implementation['sha256'][path] == hashes[path] for path in real_paths), 'Same measured real-call implementation')
        delivery = _proof_delivery(root, revision, hashes | frozen['binding'], validation_commit=validation_revision)
    except failures as error:
        return {'status': 'blocked', 'case_results': [{'id': kind, 'status': 'failed', 'error_kind': type(error).__name__}
                for kind in ('updates', 'queries', 'missing_backend')], 'individual_results': {}, 'observed_records': observed, 'qualification_complete': False}
    for kind in ('updates', 'queries', 'missing_backend'):
        try:
            portable = report[kind]; _proof_value(portable)
            raw, files, archive_owner = _proof_archive(portable, kind, directory)
            observed[kind] = _proof_observed(raw, kind)
            def read_artifact(path, *, parsed=True):
                path = _proof_path(path)
                ref = files[path]
                archive_path = directory / portable['archive']['directory']
                _proof_require(_proof_directory(archive_path)[1] == archive_owner, 'Archive owner changed before grading')
                with SourceRoot(archive_path) as owner:
                    if parsed:
                        value, sha = read_json(owner, path, 16 * 1024 * 1024)
                    else:
                        value, sha, info = owner.read(path, 16 * 1024 * 1024 + 1, hash_full=False)
                        _proof_require(len(value) == info.st_size == ref['bytes'], 'Archive full bytes changed before grading')
                _proof_require(sha == ref['sha256'], 'Archive changed before grading')
                return value
            _proof_binding(raw, kind, root, frozen, hashes, revision)
            if kind == 'updates':
                outcomes = _proof_updates(raw, frozen, hashes['evaluations/tree_sitter_baseline.py'], (files, read_artifact), hashes)
            elif kind == 'queries':
                outcomes = _proof_queries(raw, frozen, hashes['evaluations/tree_sitter_baseline.py'], (files, read_artifact), hashes)
            else:
                outcomes = _proof_missing(raw, frozen, hashes, files, read_artifact, _proof_worker_sources(root, hashes), project_name)
            individual[kind] = outcomes
            _proof_require(outcomes and all(row['status'] == 'passed' for row in outcomes), 'Individual proof failures retained')
            checks.append({'id': kind, 'status': 'passed'})
        except failures as error:
            checks.append({'id': kind, 'status': 'failed', 'error_kind': type(error).__name__})
    try:
        _proof_require(_proof_directory(directory)[1] == evidence_owner and
            subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], cwd=root, text=True, timeout=20).strip() == validation_revision, 'Proof source changed during validation')
        _proof_require(_proof_delivery(root, revision, hashes | frozen['binding'], validation_commit=validation_revision) == delivery,
                       'Measured evidence delivery binding changed')
        with SourceRoot(root) as current:
            for path, expected in (hashes | frozen['binding']).items():
                raw, sha, info = current.read(path, 1024 * 1024 + 1, hash_full=False)
                _proof_require(len(raw) == info.st_size and len(raw) <= 1024 * 1024 and sha == expected,
                               'Proof bytes changed during validation')
    except failures as error:
        checks.append({'id': 'validation_source_stability', 'status': 'failed', 'error_kind': type(error).__name__})
    return {'status': 'passed' if all(c['status'] == 'passed' for c in checks) else 'blocked',
        'case_results': checks, 'individual_results': individual, 'observed_records': observed, 'delivery_binding': delivery, 'qualification_complete': False,
        'limitations': ['Finite same-owner equivalence and physical synthetic query evidence only; no engine selection or capacity/human acceptance.',
            'Update and query digests/generations are recomputed from archived produced facts; source body/export impacts are graded separately from parity.',
            'Type, non-Go configuration, service-contract semantics, receiver completeness and scale remain unqualified.',
            'Fake-clock query deadlines are control-flow observations, not service latency guarantees.',
            'Helper bytes are bound to current disk and commit; this is not general module-load attestation.']}


def experiment_gate(gate, root=None, *, evidence_root=None, source_map=None):
    """Validate actual experiment artifacts; missing measurements fail closed."""
    from evaluations.analysis import frozen_inputs
    root = Path(root or ROOT)
    path = 'evaluations/results/code-understanding/' + ('engine-comparison.json' if gate == 'engine' else 'capacity-profile.json')
    task = 'T007' if gate == 'engine' else 'T008'
    checks = []
    adapter_proofs = None
    def check(name, condition, detail):
        checks.append({'id': name, 'status': 'passed' if condition else 'failed', 'detail': detail})
    try:
        _, identity = frozen_inputs(root)
        with SourceRoot(root) as owner:
            report, sha = read_json(owner, path, 2 * 1024 * 1024 if gate == 'engine' else 1024 * 1024)
            aggregate, _ = read_json(owner, 'evaluations/results/code-understanding/engine.json')
        check('report_shape', isinstance(report, dict) and type(report.get('schema_version')) is int and
              report['schema_version'] == 1, 'Typed experiment report required')
        member = aggregate['tasks'][task]
        check('claim_artifact_binding', member.get('artifact') == path and member.get('artifact_sha256') == sha and
              member.get('status') == report.get('status'), 'Aggregate must bind the exact current experiment bytes/status')
        source = report.get('source_identity') or {}
        check('frozen_source_identity', isinstance(source, dict) and all(source.get(k) == v for k, v in identity.items()),
              'Original frozen fixture/review identities must match current inputs')
        with SourceRoot(root) as owner:
            lock, lock_sha = read_json(owner, INPUTS + 'input-lock.json')
        check('committed_frozen_inputs', committed(root, dict(lock['sha256'], **{INPUTS + 'input-lock.json': lock_sha})),
              'Current locked inputs must match one committed snapshot')
        hashes = dict(report.get('implementation', {}).get('sha256', {}))
        if gate == 'engine':
            for name, digest_value in report.get('lifecycle', {}).get('implementation_sha256', {}).items():
                if name in hashes and hashes[name] != digest_value:
                    raise ValueError('Different implementations used for comparison and lifecycle')
                hashes[name] = digest_value
        for name in hashes:
            SourceRoot.parts(name)
        with SourceRoot(root) as owner:
            current = bool(hashes) and len(hashes) <= 128
            for name, value in hashes.items():
                raw, actual_sha, info = owner.read(name, 1024 * 1024 + 1, hash_full=False)
                current &= len(raw) == info.st_size and len(raw) <= 1024 * 1024 and _proof_sha(value) and actual_sha == value
        check('committed_experiment_implementation', current and committed(root, hashes) and
              'evaluations/analysis.py' in hashes and 'evaluations/acceptance.py' in hashes,
              'Recorded implementation must match both current bytes and committed blobs, including the driver')
        cases = report.get('case_results') or []
        if gate == 'engine':
            from evaluations.real_calls import source_locations
            with SourceRoot(root) as owner:
                calls, _ = read_json(owner, INPUTS + 'real-calls.json')
                reviewed, _ = read_json(owner, INPUTS + 'source-review.json')
            locations, supplemental = source_locations(root, calls, reviewed, identity)
            check('supplemental_frozen_source', all(source.get(k) == v for k, v in supplemental.items()),
                  'Supplemental source positions and lock must be current and committed')
            expected_calls = {c['id']: c for c in calls['cases']}
            judgments = {c['id']: c for c in reviewed['judgments']}
            real = report['real_calls']
            actual = {c['id']: c for c in real['case_results']}
            valid = len(actual) == len(real['case_results']) and set(actual) == set(expected_calls)
            correctness = {}
            for name, candidate in expected_calls.items():
                row, judgment = actual[name], judgments[name]
                valid &= all(row[k] == candidate[k] for k in ('repository_id', 'revision', 'path', 'language', 'file_sha256', 'range'))
                valid &= type(row['supported']) is bool and row['supported'] == judgment['supported']
                valid &= row['reviewed_targets'] == judgment['targets'] and row['expected_certainty'] == judgment['certainty']
                sites = row['actual_sites']
                bound = [b for b in row['target_bindings'] if b['status'] == 'bound']
                valid &= len(sites) == 1 and sites[0]['role'] == 'call' and sites[0]['path'] == candidate['path']
                if len(sites) == 1:
                    valid &= (sites[0]['range']['start_byte'], sites[0]['range']['end_byte']) == (
                        candidate['range']['utf8_bytes']['start'], candidate['range']['utf8_bytes']['end_exclusive'])
                    valid &= sites[0]['provenance']['source_sha256'] == candidate['file_sha256']
                    if not judgment['supported']:
                        valid &= row['status'] == 'passed' and bool(sites[0]['reason']) and (
                            sites[0]['certainty'] == 'unresolved' and not sites[0]['targets'] or
                            judgment['certainty'] == 'candidate' and sites[0]['certainty'] == 'candidate' and
                            sites[0]['targets_exhaustive'] is False and
                            set(sites[0]['targets']) <= {b['definition']['id'] for b in bound})
                exact = len(sites) == 1 and sites[0]['certainty'] == 'resolved' and bool(sites[0]['targets'])
                exact &= {b['key'] for b in bound} == set(judgment['targets'])
                if exact:
                    exact &= set(sites[0]['targets']) == {b['definition']['id'] for b in bound}
                    exact &= (sites[0]['range']['start_byte'], sites[0]['range']['end_byte']) == (
                        candidate['range']['utf8_bytes']['start'], candidate['range']['utf8_bytes']['end_exclusive'])
                    exact &= sites[0]['provenance']['source_sha256'] == candidate['file_sha256']
                    for binding in bound:
                        declaration, anchor = binding['definition'], locations[name, binding['key']]
                        exact &= all(declaration[k] == anchor[k] for k in ('repository_id', 'revision', 'path'))
                        exact &= declaration['provenance']['source_sha256'] == anchor['file_sha256']
                        exact &= declaration['range'] in [anchor[k] for k in
                            ('declaration_range', 'statement_range', 'callable_expression_range') if anchor.get(k)]
                correctness[name] = row['status'] == 'passed' and row['outcome'] == 'supported' and exact
                if judgment['supported'] and row['status'] == 'passed':
                    valid &= exact
            quality = True
            languages = {c['language'] for c in expected_calls.values()}
            valid &= set(real['per_language']) == languages
            for language in languages:
                supported = [c for c in expected_calls.values() if c['language'] == language and judgments[c['id']]['supported']]
                correct = sum(correctness[c['id']] for c in supported)
                resolved = sum(any(s['certainty'] == 'resolved' for s in actual[c['id']]['actual_sites']) for c in supported)
                ungraded = sum(actual[c['id']]['status'] == 'ungraded' for c in supported)
                precision = None if ungraded or not resolved else correct / resolved
                recall = correct / len(supported)
                metrics = real['per_language'][language]
                valid &= (metrics['supported_denominator'] == len(supported) and metrics['supported_correct'] == correct and
                    metrics['supported_ungraded'] == ungraded and metrics['selected_supported_precision'] == precision and
                    metrics['selected_supported_recall_lower_bound'] == recall)
                quality &= precision is not None and precision >= .95 and recall >= .85 and not ungraded
            check('real_call_measurements', valid and quality,
                  'Recompute supported denominators/exact bindings/precision/recall from all sixteen frozen cases; proposed targets are not measurements')
            required = {'syntax_direct_binding', 'reusable_source_screen', 'real_call_quality', 'finite_worker_lifecycle',
                        'evidence_uncertainty', 'incremental_equivalence', 'bounded_query_work', 'optional_installation'}
            check('mandatory_component_gates', isinstance(cases, list) and len(cases) == len(required) and
                  {c['id'] for c in cases} == required and all(c['status'] == 'passed' for c in cases),
                  'All source/binding/lifecycle/uncertainty/update/query/install gates required; not-run is not passing')
            adapter_proofs = adapter_proof_checks(report, root=root, evidence_root=evidence_root, source_map=source_map)
            for proof in adapter_proofs['case_results']:
                check('adapter:' + proof['id'], proof['status'] == 'passed',
                      'Bound private archive and recomputed source-only observations required')
            check('qualified_adapter_available', adapter_proofs['status'] == 'passed',
                  'All36 both-mode updates,20 physical query assertions and actual absent-backend proof required')
            check('qualified_owner', report.get('status') == 'passed' and report.get('engine_selected') is True and
                  isinstance(report.get('selected_owner'), str) and bool(report['selected_owner']),
                  'An unselected decision preserves failures but cannot qualify a structural owner')
        else:
            expected = {f'{corpus}:{engine}:{run}' for corpus in ('django', 'odoo', 'aws', 'kubernetes')
                        for engine in ('current-map', 'tree-sitter') for run in range(3)}
            check('measured_trials', isinstance(cases, list) and len(cases) == len(expected) and
                  {c['id'] for c in cases} == expected and all(c['status'] == 'passed' and c['exit_code'] == 0 and
                    c['identity_verified'] is True and c['revision'] == PINS[c['corpus']] and
                    c['id'] == f"{c['corpus']}:{c['engine']}:{c['repeat']}" and
                    len(c['result']['records']) == 2 and {r['run'] for r in c['result']['records']} ==
                    {'fresh-output', 'unchanged-repeat'} and all(r['status'] == 'complete' and
                        type(r['wall_seconds']) in (float, int) and r['wall_seconds'] > 0 and
                        type(r['peak_rss_bytes']) is int and r['peak_rss_bytes'] > 0 and
                        type(r['counts']['inventoried_files']) is int and r['counts']['inventoried_files'] > 0 and
                        all(isinstance(r[k], str) and len(r[k]) == 64 and all(c in '0123456789abcdef' for c in r[k])
                            for k in ('semantic_facts_sha256', 'input_inventory_sha256')) for r in c['result']['records']) for c in cases),
                  'Three independent measured workers for every frozen corpus/workload; partial/failure retained')
            check('immutable_empirical_budgets', report.get('budget_freeze', {}).get('status') == 'locked' and
                  report.get('status') == 'passed' and not report.get('remaining_gates'),
                  'Equivalent-fact, update and query measurements must precede a committed budget lock')
            check('reference_workload_available', False,
                  'This capacity profiler has no equivalent-fact reference or measured update/query adapter; changing report labels cannot supply them')
            check('rust_disposition', report.get('rust') == {'adopted': False, 'prototype_built': False, 'speed_gain_measured': False},
                  'Current no-Rust scope must not fabricate prototype gains')
        source_identity = source
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        check('experiment_evidence_available', False, 'Missing, unsafe, malformed or mismatched experiment evidence')
        source_identity = None
    passed = bool(checks) and all(c['status'] == 'passed' for c in checks)
    return {'schema_version': 1, 'gate': gate, 'status': 'passed' if passed else 'blocked',
            'source_identity': source_identity, 'case_results': checks, 'qualification_complete': False,
            'remaining_gates': ['agent', 'independent human UX', 'distribution', 'release'],
            'adapter_proofs': adapter_proofs,
            'limitations': ['A component gate cannot establish later human or release acceptance.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', choices=['freeze', 'engine', 'acceleration'], default='freeze')
    parser.add_argument('--evidence-root', type=Path, help='Existing private archived-worker root; alternatively REPO_GRAPH_EVAL_WORK_ROOT')
    parser.add_argument('--source-map', type=Path, help='Private source map for archive isolation; alternatively REPO_GRAPH_EVAL_SOURCE_MAP')
    parser.add_argument('--prepare', action='store_true', help='Check draft inputs only; does not pass the freeze gate')
    parser.add_argument('--seal-inputs', action='store_true', help='Record input hashes; requires --prepare')
    parser.add_argument('--source-review', type=Path, help='Independent source review; defaults to the committed source-review.json')
    parser.add_argument('--review-template', type=Path, help='Write an unfilled independent source judgment template')
    parser.add_argument('--report', type=Path, help='Write a portable report; no reviewer identifiers are retained')
    args = parser.parse_args(argv)
    if args.gate != 'freeze':
        if args.prepare or args.seal_inputs or args.source_review or args.review_template:
            parser.error('Source-freeze options apply only to --gate freeze')
        if args.gate != 'engine' and (args.evidence_root or args.source_map):
            parser.error('Private adapter evidence options apply only to --gate engine')
        report = experiment_gate(args.gate, evidence_root=args.evidence_root, source_map=args.source_map)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with SourceRoot(args.report.parent) as source:
                source.write_json(args.report.name, report)
        print(json.dumps({'gate': args.gate, 'status': report['status'],
                          'failures': [c for c in report['case_results'] if c['status'] != 'passed']}))
        return 0 if report['status'] == 'passed' else 1
    if args.evidence_root or args.source_map:
        parser.error('Private adapter evidence options apply only to --gate engine')
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
