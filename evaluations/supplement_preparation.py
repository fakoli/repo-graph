#!/usr/bin/env python3
"""Check prepared synthetic inputs without importing or invoking an analyzer.

Run: python3 -B evaluations/supplement_preparation.py
Commit binding and runtime qualification are separate coordinator-owned gates.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_graph.source import SourceRoot

DIRECTORY = 'evaluations/code-understanding/'
SOURCE = DIRECTORY + 'supplement-source.json'
ORACLE = DIRECTORY + 'supplement-oracle.json'
LOCK = DIRECTORY + 'supplement-lock.json'
PINS = {
    SOURCE: 'cb8dee61ddc23b15ae27f4d931854d1a8bb1f8df450f8c2c894fec7f21fca40b',
    ORACLE: 'a5fabfc247baccf826b76ff1588625ee05b9bef6d59ede4725ea6bf8c2be4b68',
    DIRECTORY + 'fixtures.json': '6742295d0536cb1df577f04a48f893c1b962b184b278875415c20611cfa5f87e',
    DIRECTORY + 'input-lock.json': '418f5274d6c1d4c6f596f48b1485518cf5ec59d74a6006b7b64706d2b4d442a1',
    DIRECTORY + 'source-target-lock.json': 'b6ebf143576dddc099316bebbf724f95ed5a2c7b37997a168595ab067a609ab5',
}
REVIEW_SHA = '59a1279972f5cb5501d8c0f15e06af239687f26a829110be733c4e96c1a5127c'
BASE_IDENTITY = 'b5d2be6248cf01ad76188c9fff67f2632e60e11770b27fbef6b4036acf1e7c0d'
MAX_BYTES = 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def pairs(entries):
    result = {}
    for key, value in entries:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read(source, path):
    raw, digest, info = source.read(path, MAX_BYTES + 1, hash_full=False)
    if len(raw) != info.st_size or len(raw) > MAX_BYTES:
        raise ValueError('Preparation input byte budget exceeded')
    return raw, digest


def decode(raw):
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Non-finite JSON number')
        return number
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=finite, parse_float=finite)


def prepare_check(root=ROOT):
    """Read-only whole-byte, finite-operation and physical-range validation."""
    checks, physical_ranges = 0, 0
    def require(condition, label):
        nonlocal checks
        checks += 1
        if not condition:
            raise ValueError(label)
    with SourceRoot(Path(root)) as guarded:
        lock_raw, lock_sha = read(guarded, LOCK)
        lock = decode(lock_raw)
        require(type(lock['schema_version']) is int and lock['schema_version'] == 1 and lock['artifact_kind'] == 'synthetic_supplement_input_lock', 'Separate supplement lock required')
        require(lock['status'] == 'prepared_independently_reviewed_not_commit_bound', 'Preparation status required')
        require(canonical(lock['commit_binding']) == canonical({'status': 'not_checked', 'required_before_runtime_comparison': True}), 'No future commit binding permitted')
        require(canonical(lock['qualification']) == canonical({'analyzer_qualified': False, 'T007_met': False, 'T008_met': False, 'T027_authority': False, 'selected_engine': None}), 'Runtime qualification must remain unclaimed')
        review = lock['independent_review']
        require(review['receipt_sha256'] == REVIEW_SHA and review['technical_reviewer_label'] == 'AI technical reviewer' and
                review['independent_of_input_author'] is True and review['runtime_model_identity'] == 'not independently observed', 'Reviewed technical receipt required')
        require(type(review['passed_checks']) is int and type(review['failed_checks']) is int and review['passed_checks'] == 5002 and review['failed_checks'] == 0, 'Independent input review checks required')
        require(type(review['physical_ranges']) is int and review['physical_ranges'] == 535, 'Independent physical range review required')
        require(review['disposition'] == 'approved_for_synthetic_input_preparation_with_required_separate_freeze_lock' and
                review['clarification_sha256'] == 'c087393cadb998c0b4a07f0067e5f9a38eaf0cf5bfd6c69a0f8161db4699a751' and
                review['packet_sha256'] == 'f455bc0de64a0935c15f9e07e0f975bc9ccae0044e3b8658541225517acb755b' and
                review['checks_sha256'] == '585b9423bf24b2f13af98dc1962e223996b61a18245479ed09f2364f03a3dbbf' and
                review['review_check_sha256'] == '0cc91bae019cf9428a8171c61f6a3d292f0c909671509368b06582aaedae783f', 'Review clarification/check identities required')
        source_raw, source_sha = read(guarded, SOURCE)
        require(source_sha == PINS[SOURCE], 'Approved source manifest required before artifact admission')
        source = decode(source_raw)
        admitted = set(PINS) | {record['path'] for record in source['files']} | {'evaluations/supplement_preparation.py'}
        require(type(lock['sha256']) is dict and set(lock['sha256']) == admitted, 'Exact bounded lock inventory required before reads')
        artifacts = {}
        for path, expected in lock['sha256'].items():
            raw, digest = read(guarded, path)
            require(digest == expected, 'Locked artifact identity mismatch: ' + path)
            artifacts[path] = raw
        require(all(lock['sha256'].get(path) == digest for path, digest in PINS.items()), 'Approved input pins required')
        oracle = decode(artifacts[ORACLE])
        fixture = decode(artifacts[DIRECTORY + 'fixtures.json'])
        require(source['schema_version'] == oracle['schema_version'] == fixture['schema_version'] == 1, 'Input schemas required')
        require(oracle['source_manifest_sha256'] == PINS[SOURCE], 'Oracle must bind source bytes')
        base, metadata = {}, {}
        for record in source['files']:
            require(set(record) == {'path', 'language', 'kind', 'content_utf8', 'sha256', 'bytes'}, 'Source metadata only')
            path = record['path']
            SourceRoot.parts(path)
            require(str(PurePosixPath(path)) == path and '\\' not in path and path.startswith('tests/fixtures/code-understanding/') and path not in base, 'Unique canonical synthetic path required')
            raw = record['content_utf8'].encode('utf-8')
            require(record['language'] in ('python', 'go', 'javascript', 'typescript') and record['kind'] in ('source', 'configuration'), 'Source metadata required')
            require(type(record['bytes']) is int and record['bytes'] == len(raw) and sha(raw) == record['sha256'], 'Full source bytes required')
            require(artifacts.get(path) == raw, 'Materialized source bytes differ')
            base[path], metadata[path] = raw, record
        require(len(base) == 24 and sum(map(len, base.values())) == 11463, 'Finite base inventory required')
        projection = [{key: record[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')} for record in source['files']]
        require(canonical(lock['files']) == canonical(projection), 'Lock must bind every full source record')
        expected_paths = set(PINS) | set(base) | {'evaluations/supplement_preparation.py'}
        require(set(artifacts) == expected_paths, 'Exact bounded lock inventory required')
        identity = sha('\n'.join(path + ':' + sha(raw) for path, raw in sorted(base.items())).encode('utf-8'))
        require(identity == lock['base_source_identity'] == BASE_IDENTITY, 'Content-only base identity mismatch')
        originals = fixture['files']
        require(len(originals) == 9 and len(fixture['updates']) == 16, 'Original v1 inventory and mutations required')
        require([update['id'] for update in fixture['updates']] == lock['original_v1']['update_ids'] and
                sha(canonical(fixture['updates'])) == lock['original_v1']['updates_canonical_sha256'], 'Original complete updates changed')
        for record in originals:
            require(metadata[record['path']]['sha256'] == record['sha256'] and len(base[record['path']]) == record['bytes'], 'Original fixture identity changed')
        for original_lock in ('input-lock.json', 'source-target-lock.json'):
            for path, digest in decode(artifacts[DIRECTORY + original_lock])['sha256'].items():
                require(read(guarded, path)[1] == digest, 'Original frozen input changed: ' + path)

        def ranges(value, snapshot):
            nonlocal physical_ranges
            if isinstance(value, dict):
                if {'path', 'source_sha256', 'text', 'range'} <= value.keys():
                    raw = snapshot[value['path']]
                    require(sha(raw) == value['source_sha256'], 'Physical range source identity mismatch')
                    for field, text_field in (('range', 'text'), ('name_range', 'name')):
                        if field not in value:
                            continue
                        physical_ranges += 1
                        span = value[field]
                        require(set(span) == {'start_byte', 'end_byte', 'start_line', 'end_line'} and all(type(v) is int for v in span.values()), 'Physical range schema mismatch')
                        start, end = span['start_byte'], span['end_byte']
                        require(0 <= start < end <= len(raw) and raw[start:end] == value[text_field].encode('utf-8'), 'Physical range byte mismatch')
                        require(span['start_line'] == raw[:start].count(b'\n') + 1 and span['end_line'] == raw[:end - 1].count(b'\n') + 1, 'Physical range line mismatch')
                for child in value.values():
                    ranges(child, snapshot)
            elif isinstance(value, list):
                for child in value:
                    ranges(child, snapshot)

        updates = source['updates']
        mutations = {item['id']: item for item in oracle['mutations']}
        require(len(updates) == len(mutations) == len(oracle['mutations']) == 20 and {u['id'] for u in updates} == set(mutations), 'Finite unique mutation coverage required')
        require(Counter((u['language'], u['category']) for u in updates) == Counter((language, category) for language in ('python', 'go', 'javascript', 'typescript') for category in ('body', 'export', 'type', 'config', 'contract')), 'Mutation category coverage mismatch')
        require(not set(mutations) & set(lock['original_v1']['update_ids']), 'Supplement must not replace original mutations')
        mutation_bindings = []
        for update in updates:
            after, paths, changes = dict(base), set(), []
            for operation in update['operations']:
                path = operation['path']
                old, new = operation['old'].encode('utf-8'), operation['new'].encode('utf-8')
                require(operation['op'] == 'replace' and path in base and path not in paths and old != new and bool(old) and
                        type(operation['occurrences']) is int and operation['occurrences'] == 1 and after[path].count(old) == 1, 'Finite replacement required')
                paths.add(path)
                require(sha(after[path]) == operation['sha256_before'], 'Mutation before identity mismatch')
                after[path] = after[path].replace(old, new)
                require(sha(after[path]) == operation['sha256_after'], 'Mutation after identity mismatch')
            require({r['path'] for r in update['changed_files']} == paths and len(update['changed_files']) == len(paths), 'Changed file inventory mismatch')
            for record in update['changed_files']:
                path = record['path']
                require(record['language'] == metadata[path]['language'] == update['language'] and record['kind'] == metadata[path]['kind'], 'Mutation metadata mismatch')
                for phase, snapshot in (('before', base), ('after', after)):
                    require(record[phase + '_content_utf8'].encode('utf-8') == snapshot[path] and record[phase + '_bytes'] == len(snapshot[path]), 'Whole mutation receipt mismatch')
                changes.append({'path': path, 'language': record['language'], 'kind': record['kind'],
                    'before': {'sha256': sha(base[path]), 'bytes': len(base[path])}, 'after': {'sha256': sha(after[path]), 'bytes': len(after[path])}})
            mutation_bindings.append({'id': update['id'], 'language': update['language'], 'category': update['category'], 'changes': changes})
            mutation = mutations[update['id']]
            require(mutation['category'] == update['category'] and mutation['changed_path'] in paths, 'Oracle mutation identity mismatch')
            for impact in mutation['expected_source_bound_impacts']:
                ranges(impact['before'], base)
                ranges(impact['after'], after)
        require(canonical(lock['mutations']) == canonical(mutation_bindings), 'Lock must bind before/after mutation identities')
        change = source['query_freshness_update']
        path, old, new = change['path'], change['old'].encode('utf-8'), change['new'].encode('utf-8')
        require(change['occurrences'] == 1 and base[path].count(old) == 1 and old != new and bool(old), 'Finite query freshness change required')
        after = base[path].replace(old, new)
        require(change['before_content_utf8'].encode('utf-8') == base[path] and change['after_content_utf8'].encode('utf-8') == after and
                sha(base[path]) == change['sha256_before'] and sha(after) == change['sha256_after'], 'Query freshness source bytes mismatch')
        require(canonical(lock['query_freshness']) == canonical({'path': path, 'before': {'sha256': sha(base[path]), 'bytes': len(base[path])},
                'after': {'sha256': sha(after), 'bytes': len(after)}}), 'Lock must bind query freshness mutation')
        ranges(oracle['query'], base)
        require(physical_ranges == 535, 'Reviewed physical range coverage mismatch')
    return {'schema_version': 1, 'status': 'preparation_passed_not_analyzer_qualified', 'checks_passed': checks,
            'source_files': 24, 'source_bytes': 11463, 'original_files': 9, 'added_files': 15,
            'original_mutations': 16, 'supplemental_mutations': 20, 'query_freshness_mutations': 1,
            'physical_ranges': physical_ranges, 'base_source_identity': identity, 'supplement_lock_sha256': lock_sha,
            'analyzer_qualified': False, 'runtime_comparisons': 'not_run', 'commit_binding': 'not_checked'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        report = prepare_check(args.root)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        report = {'status': 'preparation_failed', 'error_kind': type(error).__name__, 'analyzer_qualified': False}
        if isinstance(error, ValueError):
            report['reason'] = str(error)
        print(json.dumps(report, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
