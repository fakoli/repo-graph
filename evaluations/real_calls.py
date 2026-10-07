#!/usr/bin/env python3
"""Bounded selected-file comparison against the frozen independent source key.

Run with the existing optional analysis environment, without downloading sources:
    uv run --no-sync python evaluations/real_calls.py --source-map MAP --output REPORT

MAP is private and supplies exact pinned local roots. REPORT contains repository
relative metadata, never local roots or source excerpts. This experiment does not
select an engine or qualify whole repositories, scale, or runtime call behavior.
An optional dependencies list explicitly registers consumer/dependency repository
IDs, full revisions, module_path and version. Its version/revision binding must
be independently qualified before use. This selects declared source snapshots,
never an active Go build or transitive minimum-version selection.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluations.analysis import INPUTS, frozen_inputs, read_json
from evaluations.acceptance import committed
from evaluations.tree_sitter_baseline import (BackendUnavailable, Budget, GO_FILENAME_POLICY,
    Work, extract, go_contexts, go_filename_class, scan, snapshot_path)
from repo_graph.source import SourceRoot

SUFFIXES = {'.py': 'python', '.go': 'go', '.js': 'javascript', '.jsx': 'javascript',
            '.ts': 'typescript', '.tsx': 'typescript'}
SHA256 = re.compile(r'[0-9a-f]{64}')
REVISION = re.compile(r'[0-9a-f]{40}')


def inventory(cases, judgments):
    """Select paths from source metadata only; discard every gold annotation."""
    selected = {}
    for record in list(cases) + [e for j in judgments for e in j['evidence']]:
        repository, path = record['repository_id'], record['path']
        SourceRoot.parts(path)
        if not REVISION.fullmatch(record['revision']) or not SHA256.fullmatch(record['file_sha256']):
            raise ValueError('Invalid frozen source identity')
        item = {'path': path, 'revision': record['revision'], 'sha256': record['file_sha256'],
                'language': SUFFIXES.get(PurePosixPath(path).suffix, 'unknown'),
                'kind': 'configuration' if PurePosixPath(path).name == 'go.mod' else 'source'}
        if 'file_bytes' in record:
            item['bytes'] = record['file_bytes']
        key = repository, path
        if key in selected:
            previous = selected[key]
            if any(previous[k] != item[k] for k in ('revision', 'sha256', 'language', 'kind')) or (
                    'bytes' in previous and 'bytes' in item and previous['bytes'] != item['bytes']):
                raise ValueError('Conflicting frozen source identity')
            previous.update(item)
        else:
            selected[key] = item
    return {repository: [selected[key] for key in sorted(selected) if key[0] == repository]
            for repository in sorted({key[0] for key in selected})}


def checkout_identity(root, revision):
    """Read-only Git identity checks; diagnostic text and local paths stay private."""
    base = ['git', '--no-pager', '--no-replace-objects', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null']
    try:
        with SourceRoot(root) as source:
            if not source.secure:
                return {'status': 'secure_source_root_unavailable'}
            options = dict(cwd=Path('/proc/self/fd') / str(source.fd), pass_fds=(source.fd,),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=20, check=True)
            top = Path(os.fsdecode(subprocess.run(base + ['rev-parse', '--show-toplevel'], **options).stdout.rstrip(b'\n')))
            with SourceRoot(top) as git_root:
                if git_root.identity != source.identity:
                    return {'status': 'source_root_not_git_top_level'}
            head = subprocess.run(base + ['rev-parse', 'HEAD'], **options).stdout.decode('ascii').strip()
            if head != revision:
                return {'status': 'revision_mismatch', 'actual_revision': head}
            # Git ignores are not revision membership; each discovered blob is bound separately below.
            dirty = subprocess.run(base + ['status', '--porcelain=v1', '--untracked-files=normal'], **options).stdout
            return {'status': 'dirty_checkout' if dirty else 'verified', 'actual_revision': head,
                    'root_identity': source.identity, 'clean': not bool(dirty)}
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {'status': 'checkout_identity_unavailable'}


def revision_blob_identity(source, revision, path, raw):
    """Bind bounded discovered bytes to a regular blob in the exact SHA1 revision.

    ls-tree returns metadata only; hashing the already admitted bytes avoids an
    unbounded cat-file read. The held source directory also owns Git's cwd.
    """
    SourceRoot.parts(path)
    if not source.secure or not REVISION.fullmatch(revision):
        raise ValueError('Exact secure SHA1 source revision required')
    try:
        result = subprocess.run(['git', '--no-pager', '--no-replace-objects', '--literal-pathspecs',
            '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
            'ls-tree', '--full-tree', '-z', revision, '--', path],
            cwd=Path('/proc/self/fd') / str(source.fd), pass_fds=(source.fd,),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Revision blob metadata unavailable') from error
    expected_path = os.fsencode(path)
    if result.returncode or len(result.stdout) > len(expected_path) + 128:
        raise ValueError('Revision blob metadata unavailable or exceeds bound')
    entry, separator, named_path = result.stdout.partition(b'\t')
    fields = entry.split()
    if (not separator or named_path != expected_path + b'\0' or len(fields) != 3 or
            fields[0] not in (b'100644', b'100755') or fields[1] != b'blob' or
            not re.fullmatch(b'[0-9a-f]{40}', fields[2])):
        raise ValueError('Source is not a regular tracked blob at the revision')
    actual = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
    if actual.encode('ascii') != fields[2]:
        raise ValueError('Source bytes differ from the declared revision blob')
    return {'status': 'verified', 'object_format': 'git-sha1-blob',
            'object_id': actual, 'mode': fields[0].decode('ascii'), 'revision': revision}


def go_package_paths(source, directory, bound, exclusions=None):
    """Inventory Go source under the shared pinned filename membership policy."""
    fd = os.dup(source.fd)
    try:
        if directory:
            for part in SourceRoot.parts(directory):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        paths = []
        with os.scandir(fd) as entries:
            for number, entry in enumerate(entries):
                if number >= bound:
                    raise ValueError('Package directory inventory exceeds entry bound')
                membership = go_filename_class(entry.name)
                if membership in ('ignored_filename', 'test_file'):
                    if exclusions is not None:
                        exclusions.append({'path': (directory + '/' if directory else '') + entry.name,
                            'status': 'go_filename_excluded', 'reason': membership,
                            'filename_policy': GO_FILENAME_POLICY['version']})
                    continue
                if PurePosixPath(entry.name).suffix in ('.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx', '.m', '.s', '.S', '.f', '.F', '.f90', '.syso'):
                    raise ValueError('Non-Go package build input is unqualified')
                if membership in ('neutral', 'platform_variant'):
                    if not entry.is_file(follow_symlinks=False):
                        raise OSError('Non-regular package source')
                    paths.append((directory + '/' if directory else '') + entry.name)
        return sorted(paths)
    finally:
        os.close(fd)


def extract_selected(selected, roots, budget, dependencies=None):
    """Verify owned blobs, then use legacy per-root or registered global extraction.

    Registration joins metadata-only source snapshots in one bounded namespace;
    actual cross-root facts retain their physical repository/revision ownership.
    """
    runs, verified_bytes = {}, {}
    context, blobs, root_identities, combined_bytes, combined_files = None, [], set(), 0, 0
    if dependencies is not None:
        context = {'policy': 'declared_snapshot_only', 'dependencies': dependencies, 'packages': [], 'controls': []}
        # Registration is metadata only, never derived from an import or gold key.
        go_contexts({}, {}, context, Work(budget, None))
        selected = {repository: [dict(item) for item in records] for repository, records in selected.items()}
        if sum(map(len, selected.values())) > budget.max_files:
            raise ValueError('Combined selected inventory exceeds file bound')
        identities = set()
        physical_roots = set()
        for repository, records in selected.items():
            for item in records:
                identity = snapshot_path(repository, item['revision'], item['path'])
                if identity in identities:
                    raise ValueError('Duplicate selected snapshot identity')
                identities.add(identity)
            entry = roots.get(repository)
            if entry is not None:
                try:
                    info = Path(entry['source']).stat()
                except OSError:
                    continue
                physical = info.st_dev, info.st_ino
                if physical in physical_roots:
                    raise ValueError('Duplicate physical source root')
                physical_roots.add(physical)
    for repository, records in selected.items():
        pins = {item['revision'] for item in records}
        if len(pins) != 1:
            raise ValueError('One exact revision required per selected repository')
        revision = next(iter(pins))
        run = {'repository_id': repository, 'revision': revision, 'status': 'source_unavailable',
               'inventory': [dict(item, status='not_verified') for item in records],
               'limits': asdict(budget), 'facts': {'definitions': [], 'sites': []}}
        runs[repository] = run
        entry = roots.get(repository)
        if entry is None:
            run['reason'] = 'Pinned dependency or corpus root absent from private source map'
            run['inventory'] = [dict(item, status='root_unavailable') for item in records]
            continue
        if entry.get('revision') != revision:
            run['reason'] = 'Source map revision differs from frozen selected files'
            continue
        root = Path(entry['source'])
        identity = checkout_identity(root, revision)
        run['checkout'] = identity
        if identity['status'] != 'verified':
            run['reason'] = identity['status']
            continue
        receipts, to_scan, total, exclusions = [], [], 0, []
        package_paths, control = {}, {'repository_id': repository, 'revision': revision, 'qualified': True}
        try:
            with SourceRoot(root) as source:
                if not source.secure:
                    raise OSError('Secure source read unavailable')
                if context is not None:
                    if identity.get('root_identity') != source.identity:
                        raise ValueError('Verified Git/source root ownership changed')
                    if source.identity in root_identities:
                        raise ValueError('Duplicate physical source root')
                    root_identities.add(source.identity)
                    by_path = {item['path']: item for item in records}
                    directories = sorted({'' if PurePosixPath(item['path']).parent == PurePosixPath('.') else str(PurePosixPath(item['path']).parent)
                                          for item in records if item['language'] == 'go' and item['kind'] == 'source'})
                    for directory in directories:
                        try:
                            package_paths[directory] = go_package_paths(source, directory, budget.max_files, exclusions)
                            for path in package_paths[directory]:
                                by_path.setdefault(path, {'path': path, 'revision': revision, 'language': 'go', 'kind': 'source'})
                        except (OSError, ValueError):
                            control['qualified'] = False
                    if directories:
                        by_path.setdefault('go.mod', {'path': 'go.mod', 'revision': revision, 'language': 'unknown', 'kind': 'configuration'})
                        variants = {'go.work', 'vendor/modules.txt'}
                        for directory in directories:
                            parts = PurePosixPath(directory).parts
                            variants.update('/'.join(parts[:depth]) + '/go.mod' for depth in range(1, len(parts) + 1))
                        for path in sorted(variants):
                            try:
                                source.info(path)
                                control['qualified'] = False
                            except FileNotFoundError:
                                pass
                            except OSError:
                                control['qualified'] = False
                    records = [by_path[path] for path in sorted(by_path)]
                    if len(records) + combined_files > budget.max_files:
                        raise ValueError('Combined source inventory exceeds file bound')
                    combined_files += len(records)
                for number, item in enumerate(records):
                    receipt = dict(item, status='not_verified')
                    receipts.append(receipt)
                    if context is not None and item['language'] == 'go' and item['kind'] == 'source':
                        membership = go_filename_class(item['path'])
                        if membership in ('ignored_filename', 'test_file'):
                            receipt.update(status='go_filename_excluded', reason=membership,
                                           filename_policy=GO_FILENAME_POLICY['version'])
                            continue
                    try:
                        info = source.info(item['path'])
                        if (number >= budget.max_files or info.st_size > budget.max_file_bytes or
                                total + info.st_size > budget.max_total_bytes or
                                (context is not None and combined_bytes + info.st_size > budget.max_total_bytes)):
                            receipt['status'] = 'verification_budget_exceeded'
                            control['qualified'] = False
                            continue
                        # A successful bounded read still hashes every byte;
                        # concurrent growth cannot turn this into an unbounded read.
                        raw, sha, info = source.read(item['path'], budget.max_file_bytes + 1, hash_full=context is None)
                        total += info.st_size
                        if (len(raw) != info.st_size or ('sha256' in item and sha != item['sha256']) or
                                ('bytes' in item and item['bytes'] != len(raw))):
                            receipt['status'] = 'full_source_identity_mismatch'
                            control['qualified'] = False
                            continue
                        if context is not None:
                            receipt['revision_blob'] = revision_blob_identity(source, revision, item['path'], raw)
                        receipt.update(status='verified', bytes=len(raw), sha256=sha)
                        verified_bytes[repository, item['path']] = raw
                        # No expected range, target, supportedness, certainty, or text crosses this boundary.
                        to_scan.append({key: receipt[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')})
                        if context is not None:
                            blobs.append(dict(to_scan[-1], path=snapshot_path(repository, revision, item['path']),
                                physical_path=item['path'], repository_id=repository, revision=revision, content=raw))
                            combined_bytes += len(raw)
                    except (OSError, ValueError) as error:
                        control['qualified'] = False
                        receipt.update(status='source_read_error', error_kind=type(error).__name__,
                                       errno=getattr(error, 'errno', None))
        except (OSError, ValueError) as error:
            run.update(reason='Secure selected source verification failed', error_kind=type(error).__name__)
            continue
        run['verification'] = receipts
        if context is not None:
            run['filename_exclusions'] = sorted(exclusions, key=lambda item: item['path'])
            context['controls'].append(control)
            for directory, paths in package_paths.items():
                metadata = {item['path']: item for item in to_scan}
                context['packages'].append({'repository_id': repository, 'revision': revision, 'directory': directory,
                    'files': [{key: metadata[path][key] for key in ('path', 'sha256', 'bytes')} for path in paths if path in metadata]})
            run.update(inventory=receipts, status='verified' if control['qualified'] else 'partial', source_control=control)
            run['checkout_after_verification'] = checkout_identity(root, revision)
            if (run['checkout_after_verification']['status'] != 'verified' or
                    run['checkout_after_verification'].get('root_identity') != identity.get('root_identity')):
                control['qualified'] = False
                run['status'] = 'source_changed'
            continue
        if not to_scan:
            run.update(status='source_unavailable', inventory=receipts)
            continue
        try:
            actual = scan(root, to_scan, budget)
        except BackendUnavailable:
            run.update(status='backend_unavailable', inventory=receipts)
            continue
        by_path = {item['path']: item for item in actual['inventory']}
        run.update(status=actual['status'] if len(to_scan) == len(records) else 'partial',
                   inventory=[by_path.get(item['path'], item) for item in receipts],
                   facts=actual['facts'], resources=actual['resources'], versions=actual['versions'],
                   errors=actual['errors'], stop_reason=actual['stop_reason'], engine_limits=actual['limits'])
        # A checkout change cannot silently become accepted evidence, even if its
        # changed files were outside the explicitly selected extraction inventory.
        run['checkout_after'] = checkout_identity(root, revision)
        if run['checkout_after']['status'] != 'verified':
            run['status'] = 'source_changed'
    if context is not None:
        try:
            actual = extract(blobs, budget, go_context=context)
        except BackendUnavailable:
            for run in runs.values():
                run['status'] = 'backend_unavailable'
            return runs, verified_bytes
        for repository, run in runs.items():
            if 'verification' not in run:
                continue
            pin = run['revision']
            owned = lambda item: (item['repository_id'], item['revision']) == (repository, pin)
            run.update(facts={kind: [item for item in actual['facts'][kind] if owned(item)] for kind in ('definitions', 'sites')},
                       resources=actual['resources'], versions=actual['versions'], engine_limits=actual['limits'],
                       errors=[item for item in actual['errors'] if owned(item)], stop_reason=actual['stop_reason'],
                       resource_scope='shared bounded registered snapshot extraction')
            receipts = {item['path']: item for item in actual['inventory'] if owned(item)}
            run['inventory'] = [receipts.get(item['path'], item) for item in run['verification']]
            if run['status'] == 'verified':
                run['status'] = actual['status']
            run['checkout_after'] = checkout_identity(Path(roots[repository]['source']), pin)
            if (run['checkout_after']['status'] != 'verified' or
                    run['checkout_after'].get('root_identity') != run['checkout'].get('root_identity')):
                run['status'] = 'source_changed'
    return runs, verified_bytes


def bounds(record):
    value = record['range']['utf8_bytes']
    return value['start'], value['end_exclusive']


def excerpt_verified(evidence, raw):
    start, end = bounds(evidence)
    return (type(start) is int and type(end) is int and 0 <= start < end <= len(raw) and
            hashlib.sha256(raw[start:end]).hexdigest() == evidence['excerpt_sha256'])


def definition_projection(repository, revision, definition):
    if (definition.get('repository_id', repository), definition.get('revision', revision)) != (repository, revision):
        raise ValueError('Emitted definition ownership differs from containing source run')
    return {'repository_id': repository, 'revision': revision,
            **{key: definition[key] for key in ('id', 'path', 'language', 'name', 'kind', 'range', 'provenance', 'callable')}}


def source_locations(root, calls, reviewed, identity):
    """Load the separately committed location supplement; preserve the v1 key."""
    lock_path, key_path = INPUTS + 'source-target-lock.json', INPUTS + 'source-target-locations.json'
    with SourceRoot(root) as source:
        lock, lock_sha = read_json(source, lock_path)
        key, key_sha = read_json(source, key_path)
        expected_paths = {INPUTS + name for name in ('input-lock.json', 'source-review.json',
            'source-review-policy.json', 'real-calls.json', 'corpora.json', 'source-target-locations.json')}
        if (type(lock.get('schema_version')) is not int or lock['schema_version'] != 1 or
                lock.get('status') != 'locked_independently_reviewed' or
                set(lock['sha256']) != expected_paths or
                lock.get('parent_input_lock_sha256') != identity['input_lock_sha256']):
            raise ValueError('Separate supplemental source lock required')
        for path, sha in lock['sha256'].items():
            if not isinstance(sha, str) or not SHA256.fullmatch(sha) or read_json(source, path)[1] != sha:
                raise ValueError('Supplemental source input hash mismatch')
    if not committed(root, dict(lock['sha256'], **{lock_path: lock_sha})):
        raise ValueError('Supplemental key and lock must be committed before grading')
    review = key['review']
    if (key.get('scope') != 'same_sixteen_T004_sites' or
            key.get('source_review_key_sha256') != identity['source_review_sha256'] or
            review.get('kind') != 'independent_ai' or review.get('model') != 'gpt-6-astra' or
            review.get('independent') is not True or review.get('source_reviewed') is not True or
            review.get('human_evaluation') is not False or
            review.get('engine_outputs_consulted') is not False):
        raise ValueError('Authorized independent source-only supplement required')
    original = {item['id']: item for item in reviewed['judgments']}
    coverage = key['case_coverage']
    fields = ('targets', 'certainty', 'supported', 'assumptions')
    if (not calls['cases'] or len(original) != len(calls['cases']) or
            len(coverage) != len(original) or {c['id'] for c in coverage} != set(original) or
            any(any(c[field] != original[c['id']][field] for field in fields) for c in coverage)):
        raise ValueError('Supplement may not change or omit original source judgments')
    locations = {}
    for location in key['locations']:
        pair = location['case_id'], location['target_key']
        if pair in locations or location['target_key'] not in original[location['case_id']]['targets']:
            raise ValueError('Duplicate or unreviewed supplemental target')
        repository, rest = location['target_key'].split(':', 1)
        path = rest.split('#', 1)[0]
        SourceRoot.parts(path)
        if (location['repository_id'] != repository or location['path'] != path or
                not REVISION.fullmatch(location['revision']) or not SHA256.fullmatch(location['file_sha256'])):
            raise ValueError('Supplemental target source identity mismatch')
        locations[pair] = location
    if set(locations) != {(j['id'], target) for j in original.values() for target in j['targets']}:
        raise ValueError('Every reviewed target needs exactly one source location')
    return locations, {'source_target_lock_sha256': lock_sha, 'source_target_locations_sha256': key_sha}


def location_verified(location, raw):
    """Reject changed bytes, invalid anchors and ranges before exact matching."""
    if hashlib.sha256(raw).hexdigest() != location['file_sha256']:
        return False
    for field, expected_sha in location['range_sha256'].items():
        value = location.get(field)
        if not isinstance(value, dict):
            return False
        start, end = value['start_byte'], value['end_byte']
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw) or
                hashlib.sha256(raw[start:end]).hexdigest() != expected_sha or
                value['start_line'] != raw[:start].count(b'\n') + 1 or
                value['end_line'] != raw[:end - 1].count(b'\n') + 1):
            return False
        try:
            raw[:start].decode('utf-8')
            raw[start:end].decode('utf-8')
        except UnicodeError:
            return False
    name_range = location.get('name_range')
    if location['name'] is None:
        return name_range is None
    return (isinstance(name_range, dict) and
            raw[name_range['start_byte']:name_range['end_byte']].decode('utf-8') == location['name'])


def target_binding(key, judgment, runs, verified_bytes, locations=None):
    """No name-only, suffix-name, containing-range, or cross-repository matching.

    A containing excerpt alone cannot reward a target. A separately committed
    independent supplement may supply exact spans and actual lexical names.
    """
    repository, rest = key.split(':', 1)
    path, name = rest.split('#', 1)
    run = runs.get(repository)
    if run is None or run['status'] in ('source_unavailable', 'backend_unavailable', 'source_changed'):
        return {'key': key, 'status': 'ungraded', 'reason': 'target_source_unavailable'}
    evidence = [item for item in judgment['evidence']
                if item['repository_id'] == repository and item['path'] == path and
                item['revision'] == run['revision'] and item['role'] != 'candidate_call_site']
    raw = verified_bytes.get((repository, path))
    if raw is None or not evidence or any(not excerpt_verified(item, raw) for item in evidence):
        return {'key': key, 'status': 'ungraded', 'reason': 'target_source_evidence_unavailable_or_invalid'}
    location = locations.get((judgment['id'], key)) if locations is not None else None
    if locations is not None and (location is None or location['revision'] != run['revision'] or
                                  not location_verified(location, raw)):
        return {'key': key, 'status': 'ungraded', 'reason': 'supplemental_target_source_invalid'}
    if location is not None:
        # Descriptive callback names in the original key are not lexical names.
        # Only explicitly reviewed physical spans and actual named ancestors match.
        name = '.'.join(location['lexical_named_ancestors'] + [location['name']]) if location['name'] else None
        ranges = [location[field] for field in ('declaration_range', 'statement_range', 'callable_expression_range')
                  if location.get(field) is not None]
    found = []
    for definition in run['facts']['definitions']:
        if (definition['path'] != path or definition['name'] != name or not definition['callable'] or
                definition['provenance']['source_sha256'] != hashlib.sha256(raw).hexdigest()):
            continue
        actual_range = definition['range']
        exact = (any(actual_range == value for value in ranges) if location is not None else
                 any(bounds(item) == (actual_range['start_byte'], actual_range['end_byte']) for item in evidence))
        if exact:
            found.append(definition)
    if len(found) != 1:
        return {'key': key, 'status': 'missing' if location is not None else 'ungraded',
                'reason': 'engine_definition_missing_or_ambiguous' if location is not None else 'reviewed_definition_range_not_exact_or_ambiguous',
                'exact_matches': len(found), 'reviewed_excerpt_ranges': [item['range'] for item in evidence]}
    return {'key': key, 'status': 'bound', 'definition': definition_projection(repository, run['revision'], found[0])}


def grade_cases(cases, judgments, runs, verified_bytes, locations=None):
    """Consult approved judgments only after all source-only scans have finished."""
    keyed = {item['id']: item for item in judgments}
    if len(keyed) != len(judgments) or set(keyed) != {item['id'] for item in cases}:
        raise ValueError('One independent source judgment required per selected call')
    def namespaced(definition):
        return ('repository_id' in definition and 'revision' in definition and
                definition['id'].startswith(snapshot_path(definition['repository_id'], definition['revision'], definition['path']) + ':'))
    def identity(definition):
        return definition['repository_id'], definition['revision'], definition['id']
    shared = {}
    for repository, run in runs.items():
        for definition in run['facts']['definitions']:
            projected = definition_projection(repository, run['revision'], definition)
            if namespaced(definition):
                if definition['id'] in shared:
                    raise ValueError('Duplicate emitted snapshot definition identity')
                shared[definition['id']] = projected
    results = []
    for case in cases:
        judgment = keyed[case['id']]
        repository, path = case['repository_id'], case['path']
        run, raw = runs[repository], verified_bytes.get((repository, path))
        start, end = bounds(case)
        result = {key: case[key] for key in ('id', 'repository_id', 'revision', 'path', 'language', 'file_sha256', 'range')}
        result.update(supported=judgment['supported'], expected_certainty=judgment['certainty'],
                      reviewed_targets=judgment['targets'], status='ungraded', outcome='source_unavailable',
                      actual_sites=[], target_bindings=[target_binding(key, judgment, runs, verified_bytes, locations)
                                                       for key in judgment['targets']])
        if judgment['certainty'] == 'candidate':
            result['candidate_recall'] = {'status': 'failed', 'reviewed_count': len(judgment['targets']),
                'verified_recalled_count': 0, 'missing_or_ungraded_reviewed_targets': sorted(judgment['targets']),
                'reason': 'Absent source or call facts do not earn candidate recall'}
        results.append(result)
        call_evidence = [item for item in judgment['evidence'] if item['role'] == 'candidate_call_site' and
                         item['repository_id'] == repository and item['path'] == path and
                         item['revision'] == case['revision'] and item['file_sha256'] == case['file_sha256'] and
                         bounds(item) == (start, end)]
        if (raw is None or run['status'] in ('source_unavailable', 'backend_unavailable', 'source_changed') or
                len(call_evidence) != 1 or not excerpt_verified(call_evidence[0], raw)):
            result['reason'] = 'Exact call source or independent excerpt evidence unavailable'
            continue
        found = [item for item in run['facts']['sites'] if item['path'] == path and item['role'] == 'call' and
                 (item['range']['start_byte'], item['range']['end_byte']) == (start, end) and
                 item['provenance']['source_sha256'] == case['file_sha256']]
        by_id = {item['id']: item for item in run['facts']['definitions']}
        actual_definitions = dict(shared)
        actual_definitions.update({key: definition_projection(repository, run['revision'], value) for key, value in by_id.items()})
        for item in found:
            if (item.get('repository_id', repository), item.get('revision', run['revision'])) != (repository, run['revision']):
                raise ValueError('Emitted site ownership differs from containing source run')
            result['actual_sites'].append({key: item[key] for key in ('id', 'path', 'language', 'role', 'range',
                'certainty', 'reason', 'targets', 'targets_exhaustive', 'provenance')})
            result['actual_sites'][-1]['target_definitions'] = [actual_definitions[t] for t in item['targets'] if t in actual_definitions]
            result['actual_sites'][-1]['missing_target_fact_ids'] = [t for t in item['targets'] if t not in actual_definitions]
        if len(found) != 1:
            result.update(status='failed', outcome='missing' if not found else 'ambiguous_site',
                          reason='No unique source-only call fact at the reviewed exact UTF-8 byte range')
        else:
            actual = found[0]
            def eligible(binding):
                return binding['status'] == 'bound' and (binding['definition']['repository_id'] == repository or namespaced(binding['definition']))
            exact_ids = {identity(item['definition']) for item in result['target_bindings'] if eligible(item)}
            binding_complete = all(eligible(item) for item in result['target_bindings'])
            actual_ids = {identity(actual_definitions[t]) for t in actual['targets'] if t in actual_definitions}
            actual_complete = all(t in actual_definitions for t in actual['targets'])
            if judgment['certainty'] == 'resolved':
                if actual['certainty'] != 'resolved' or not actual['targets']:
                    result.update(status='failed', outcome='unresolved', reason='Reviewed supported direct binding not resolved')
                elif any(item['status'] == 'missing' for item in result['target_bindings']):
                    result.update(status='failed', outcome='missing_target_definition',
                                  reason='Reviewed exact target declaration has no unique engine definition')
                elif not binding_complete:
                    result.update(status='ungraded', outcome='target_evidence_gap',
                                  reason='Resolved fact retained; exact independently reviewed target identity cannot be graded')
                elif actual_complete and actual_ids == exact_ids:
                    result.update(status='passed', outcome='supported', reason='Exact reviewed source binding')
                else:
                    result.update(status='failed', outcome='wrong', reason='Actual target identity differs from exact reviewed definitions')
            elif judgment['certainty'] == 'unresolved':
                preserved = actual['certainty'] == 'unresolved' and not actual['targets'] and bool(actual['reason'])
                result.update(status='passed' if preserved else 'failed', outcome='unsupported_preserved' if preserved else 'wrong',
                              reason='Unknown call retained with a reason' if preserved else 'Unsupported call falsely resolved or lacks a reason')
            else:
                conservative = actual['certainty'] == 'unresolved' and not actual['targets'] and bool(actual['reason'])
                candidate = actual['certainty'] == 'candidate' and not actual['targets_exhaustive']
                preserved = conservative or (candidate and binding_complete and actual_complete and actual_ids.issubset(exact_ids))
                result.update(status='passed' if preserved else 'ungraded' if candidate and not binding_complete else 'failed',
                              outcome='candidate_unresolved' if conservative else 'candidate' if candidate else 'wrong',
                              reason='Uncertainty retained; alternative recall scored separately' if preserved else 'Candidate target evidence unavailable or call falsely exact')
        if judgment['certainty'] == 'candidate':
            actual_ids = {identity(actual_definitions[t]) for t in found[0]['targets'] if t in actual_definitions} if len(found) == 1 else set()
            recalled = [item['key'] for item in result['target_bindings'] if item['status'] == 'bound' and
                        (item['definition']['repository_id'] == repository or namespaced(item['definition'])) and
                        identity(item['definition']) in actual_ids]
            missing = sorted(set(judgment['targets']) - set(recalled))
            result['candidate_recall'] = {'status': 'failed' if missing else 'passed', 'reviewed_count': len(judgment['targets']),
                'verified_recalled_count': len(recalled), 'missing_or_ungraded_reviewed_targets': missing,
                'reason': 'Unresolved is allowed for uncertainty but does not earn candidate recall'}
    return results


def compare_real_calls(source_map, root=ROOT, budget=None):
    budget, started = budget or Budget(), time.perf_counter()
    def implementation_identity():
        with SourceRoot(root) as source:
            hashes = {path: source.read(path, 1024 * 1024, hash_full=True)[1] for path in (
                'evaluations/real_calls.py', 'evaluations/analysis.py', 'evaluations/acceptance.py',
                'evaluations/tree_sitter_baseline.py', 'repo_graph/source.py', 'pyproject.toml', 'uv.lock')}
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=20).strip()
        return {'commit': revision, 'sha256': hashes}
    implementation = implementation_identity()
    _, identity = frozen_inputs(root)
    with SourceRoot(Path(root)) as source:
        calls, call_sha = read_json(source, INPUTS + 'real-calls.json')
        reviewed, review_sha = read_json(source, INPUTS + 'source-review.json')
        corpora, corpus_sha = read_json(source, INPUTS + 'corpora.json')
    review = reviewed['review']
    if (reviewed['candidate_manifest_sha256'] != call_sha or review_sha != identity['source_review_sha256'] or
            review.get('kind') != 'independent_ai' or not review.get('independent') or not review.get('source_reviewed')):
        raise ValueError('Approved independent source review identity required')
    locations, location_identity = source_locations(root, calls, reviewed, identity)
    selected = inventory(calls['cases'], reviewed['judgments'])
    expected = {item['id']: item['revision'] for item in corpora['corpora']}
    for repository, revision in expected.items():
        if {item['revision'] for item in selected[repository]} != {revision}:
            raise ValueError('Selected source revision differs from frozen corpus')
    source_map = Path(source_map)
    with SourceRoot(source_map.parent) as source:
        mapped, map_sha = read_json(source, source_map.name)
    if not isinstance(mapped, dict) or not isinstance(mapped.get('corpora'), list) or not mapped['corpora']:
        raise ValueError('Nonempty private source map required')
    entries = mapped['corpora']
    if any(not isinstance(item, dict) for item in entries):
        raise ValueError('Private source map entries must be objects')
    roots = {item['id']: item for item in entries}
    if type(mapped.get('schema_version')) is not int or mapped['schema_version'] != 1 or len(roots) != len(entries):
        raise ValueError('Unique private source map entries required')
    if any(not isinstance(item.get('source'), str) or not Path(item['source']).is_absolute() for item in entries):
        raise ValueError('Private source map must supply absolute local roots')
    runs, verified_bytes = extract_selected(selected, roots, budget, mapped.get('dependencies'))
    results = grade_cases(calls['cases'], reviewed['judgments'], runs, verified_bytes, locations)
    metrics = {}
    for language in sorted({item['language'] for item in results}):
        group = [item for item in results if item['language'] == language]
        supported = [item for item in group if item['supported']]
        correct = sum(item['status'] == 'passed' for item in supported)
        ungraded = sum(item['status'] == 'ungraded' for item in supported)
        resolved = sum(any(site['certainty'] == 'resolved' for site in item['actual_sites']) for item in supported)
        metrics[language] = {'selected_calls': len(group), 'supported_denominator': len(supported),
            'supported_correct': correct, 'supported_failed': len(supported) - correct - ungraded,
            'supported_ungraded': ungraded, 'supported_outcome_grading_coverage': len(supported) - ungraded,
            'supported_exact_target_grading_coverage': sum(bool(item['target_bindings']) and
                all(binding['status'] == 'bound' for binding in item['target_bindings']) for item in supported),
            'selected_supported_recall_lower_bound': correct / len(supported) if supported else None,
            'selected_supported_precision': None if ungraded else correct / resolved if resolved else None,
            'uncertainty_outcomes': dict(Counter(item['outcome'] for item in group if not item['supported']))}
    portable_runs = [{key: value for key, value in run.items() if key != 'facts'} for run in runs.values()]
    if implementation_identity() != implementation:
        raise ValueError('Comparison implementation changed during extraction/grading')
    return {'schema_version': 1, 'experiment': 'selected-real-calls', 'engine': 'tree-sitter',
        'status': 'comparison_complete_with_gaps' if any(item['status'] != 'passed' or
            item.get('candidate_recall', {}).get('status') == 'failed' for item in results) else 'comparison_complete',
        'engine_selected': False, 'qualification_complete': False,
        'scope': 'Explicit selected callsite/evidence files and complete selected Go package inventories; owned source-only extraction',
        'extraction_mode': 'registered_global_snapshots' if mapped.get('dependencies') is not None else 'legacy_per_repository',
        'source_identity': dict(identity, **location_identity, real_calls_sha256=call_sha, corpora_sha256=corpus_sha),
        'source_map_sha256': map_sha, 'implementation': implementation,
        'review': review, 'case_results': results, 'per_language': metrics,
        'corpora': portable_runs, 'limits': asdict(budget), 'elapsed_seconds': time.perf_counter() - started,
        'limitations': ['16 reviewed examples do not establish whole-repository quality, adoption, or scale.',
            'Targets match committed independently reviewed exact declaration spans and actual named ancestors; no bare-name or containing-range credit.',
            'No target facts are inferred from source keys; registered global extraction can emit actual owned cross-snapshot references.',
            'Dependency registration is externally verified declared snapshot context; active Go build, platform/tag selection and MVS remain unqualified.',
            'Missing dependency source remains visible and stays in its supported denominator.',
            'Conservative unresolved candidates do not earn alternative recall.',
            'Native parser interruption is unavailable; selected file byte and fact budgets remain enforced.']}


def write_comparison(output, report, source_map):
    """Keep one output directory descriptor open across ownership check/write."""
    with SourceRoot(source_map.parent) as owner:
        mapped, sha = read_json(owner, source_map.name)
    if sha != report['source_map_sha256']:
        raise ValueError('Private source map changed before report publication')
    roots = [Path(item['source']).resolve(strict=True) for item in mapped['corpora']]
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
    if len(raw.encode()) > 2 * 1024 * 1024:
        raise ValueError('Comparison exceeds output budget')
    with SourceRoot(output.parent) as owner:
        if any(owner.root == root or root in owner.root.parents for root in roots):
            raise ValueError('Comparison report must be outside source corpora')
        try:
            owner.info(output.name)
        except FileNotFoundError:
            pass
        with owner.atomic_writer(output.name, text=True) as stream:
            stream.write(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-map', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare_real_calls(args.source_map)
        write_comparison(args.output, report, args.source_map)
    except (OSError, ValueError, KeyError, TypeError, BackendUnavailable) as error:
        print(json.dumps({'status': 'blocked', 'error_kind': type(error).__name__}))
        return 2
    print(json.dumps({'status': report['status'], 'cases': len(report['case_results']),
                      'engine_selected': False, 'qualification_complete': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
