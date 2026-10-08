#!/usr/bin/env python3
"""Run the optional native syntax baseline against a separately frozen key.

uv sync --python 3.12 --extra analysis
uv run python evaluations/analysis.py --engine tree-sitter --suite component
uv run python evaluations/analysis.py --suite constructs
uv run python evaluations/analysis.py --suite incremental
uv run python evaluations/analysis.py --suite queries
uv run python evaluations/analysis.py --suite coverage
uv run python evaluations/analysis.py --suite evidence
uv run python evaluations/analysis.py --profile-pilot

No provider calls, real-corpus download, daemon, dynamic imports of source code,
or runtime product installation occurs. Experimental selection requires all component and finite-cost proofs.
"""
import argparse
from contextlib import ExitStack, contextmanager, closing
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_graph.source import SourceRoot
from evaluations.tree_sitter_baseline import BackendUnavailable, Budget, LANGUAGES, PINS, scan

INPUTS = 'evaluations/code-understanding/'
DEFAULT_OUTPUT = 'evaluations/results/code-understanding/native-component.json'
FACTS_OUTPUT = 'evaluations/results/code-understanding/facts.json'
VIEWS_OUTPUT = 'evaluations/results/code-understanding/views.json'
BUSINESS_OUTPUT = 'evaluations/results/code-understanding/business.json'

# Separately identified state/storage controls, frozen before producer execution.
# They do not extend the original construct oracle or its coverage denominator.
COVERAGE_CONTROLS = {
    'ready': {'path': 'main.py', 'language': 'python', 'kind': 'source',
        'content_utf8': 'def target(): return 1\n',
        'sha256': 'd6666ca7a71c64447696fa7b35e44f8e34bc98dc3cdf047ceab508f9e0a3699a',
        'origin': 'Existing structural source/publication regression'},
    'dirty': {'path': 'main.py', 'language': 'python', 'kind': 'source',
        'content_utf8': 'def target(): return 2\n',
        'sha256': '050321540c0f20236b4f2f647da248c69d8ba5490831437dc51b9cdb102eb39a',
        'origin': 'Existing structural source/publication regression body update'},
    'partial': {'path': 'partial.py', 'language': 'python', 'kind': 'source',
        'content_utf8': 'def local():\n    return 1\ndef caller():\n    return local()\n! broken [\n',
        'sha256': 'dd3ba571e71e4099be53be2659ce3babdab374fc181ccf1ff7a34a02c2c50e19',
        'origin': 'Existing native parse-failure regression'},
    'unsupported': {'path': 'other.rs', 'language': 'rust', 'kind': 'source',
        'content_utf8': 'fn undiscovered() {}\n',
        'sha256': '96d9397322c82c68876d204a0d70314142cb3df6f7ed9f7b71ebbbaaad7b8cc3',
        'origin': 'Existing unsupported-language regression'},
    'extra': {'path': 'extra.py', 'language': 'python', 'kind': 'source',
        'content_utf8': 'def target(): return 1\n',
        'sha256': 'd6666ca7a71c64447696fa7b35e44f8e34bc98dc3cdf047ceab508f9e0a3699a',
        'origin': 'Ready source reused as a separately identified missing-vector addition'},
    'truncated': {'path': 'truncated.py', 'language': 'python', 'kind': 'source',
        'content_utf8': 'def keptExcerpt(): pass\n' + ' ' * 65536 + 'a',
        'sha256': '94660716d9c881c4f03e7b76bb18fa91d3554b2e2020da05c3f1fdb6092003d0',
        'origin': 'Existing keyword synopsis/full-content-digest truncation regression'},
}

# Source question selectors frozen before implementation; original locks stay authoritative.
EVIDENCE_PROJECTION_SHA = '07437f1b84e873027bc3d7cb7a18f6acbcf54bd013d8d97fe1e75870f9ffb309'
EVIDENCE_QUESTION_CORE_SHA = '8bc112bc0bbcaeb895b804a9c23c2dabcb998235b1e2d719c3f073debe47b47a'
EVIDENCE_SPECS = (
    ('PY.main.local',), ('GO.main.Local',), ('JS.main.local',), ('TS.main.local',),
    ('PY.main.café',), ('GO.main.café',), ('JS.main.café',), ('TS.main.café',),
    ('PY.main.First.run',), ('GO.main.First.Run',), ('JS.main.shadow.local',),
    ('TS.main.shadow.local',), ('PY.main.shadow', 'PY.main.shadow.local'),
    ('PY.fanout.leaf_000',), (), ('TS.main.Worker.run',),
)
EVIDENCE_CONTROLS = {
    'old': ('main.py', 'def oldFunction(): pass\n', '6011d3a1e0b9f9dc2bcab9d845bb2666e47fa5458e60567c092b6676facc72b0'),
    'new': ('main.py', 'def newFunction(): pass\n', '06fd5c8f572ecdff3d48bc36f8d08bf94f0f32bc80d34fdbafad480b47017729'),
    'guide': ('guide.markdown', '# Object storage\nRetain historical versions of objects.\n', '9bd9095a6883b411748c77a29522c9b99de234dca6202e1c4d650396ed0d232a'),
    'redacted': ('redacted.py', 'def redacted():\n    token = "sk-' + 'x' * 30 + '"\n    return token\n', 'eb9afac303f39696b8e5570adfada46daec89590eb4d9480ec5984531eee9ffe'),
}


def _evidence_questions(fixture, oracle, records):
    """Project the existing source key; no extraction or production input includes it."""
    declarations = {row['id']: row for row in fixture['definitions']}
    declarations.update({row['key']: row for row in oracle['query']['declarations']})
    questions = []
    for number, keys in enumerate(EVIDENCE_SPECS, 1):
        rows = [declarations[key] for key in keys]
        path = rows[0]['path'] if rows else 'tests/fixtures/code-understanding/python/main.py'
        query = rows[0]['name'] if rows else 'tqfourteenabsentsentinelfunction'
        grounding = [{'path': row['path'], 'file_sha256': records[row['path']]['sha256'],
            'name': row['name'], 'definition_kind': row.get('kind', 'function'),
            'declaration_range': row['range'], 'declaration_sha256': hashlib.sha256(row['text'].encode()).hexdigest()}
            for row in rows] or [{'path': path, 'file_sha256': records[path]['sha256']}]
        questions.append({'id': 'T014-Q%02d' % number,
            'request': {'query': query, 'kind': 'functions', 'mode': 'keyword', 'limit': 50, 'prefix': path},
            'source_grounding': grounding,
            'required_member_keys': list(keys) if number != 16 else [],
            'excluded_member_keys': list(keys) if number == 16 else [],
            'function_result_count': 0 if number == 15 else None})
    if hashlib.sha256(json.dumps(questions, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest() != EVIDENCE_QUESTION_CORE_SHA:
        raise ValueError('Frozen T014 question projection changed')
    return questions


def read_json(source, path, maximum=1024 * 1024):
    raw, sha, info = source.read(path, maximum + 1, hash_full=False)
    if info.st_size != len(raw) or len(raw) > maximum:
        raise ValueError('Oversized or partial evaluation input')
    return json.loads(raw), sha


def frozen_inputs(root):
    """Grader input only. Do not pass cases/definitions/targets to scan()."""
    with SourceRoot(root) as source:
        lock, lock_sha = read_json(source, INPUTS + 'input-lock.json')
        if lock.get('schema_version') != 1 or lock.get('status') != 'inputs_locked_ai_source_review_supplied':
            raise ValueError('Frozen input lock not supplied')
        for path, expected in lock['sha256'].items():
            raw, sha, info = source.read(path, 1024 * 1024 + 1, hash_full=False)
            if info.st_size != len(raw) or len(raw) > 1024 * 1024 or sha != expected:
                raise ValueError('Frozen input identity mismatch: ' + path)
        fixture, fixture_sha = read_json(source, INPUTS + 'fixtures.json')
        return fixture, {'input_lock_sha256': lock_sha, 'fixtures_sha256': fixture_sha,
                         'source_review_sha256': lock['source_review_sha256']}


def grade(facts, fixture):
    """Match exact source ranges; gold is consulted only after extraction."""
    definitions, sites = facts['definitions'], facts['sites']
    actual_to_gold, definition_results, case_results = {}, [], []
    for expected in fixture['definitions']:
        found = [item for item in definitions if item['path'] == expected['path'] and item['range'] == expected['range']]
        passed = len(found) == 1 and found[0]['text'] == expected['text'] and found[0]['name'] == expected['name'] and found[0]['kind'] == expected['kind']
        if passed:
            actual_to_gold[found[0]['id']] = expected['id']
        definition_results.append({'id': expected['id'], 'status': 'passed' if passed else 'failed',
                                   'reason': 'exact selected definition, UTF-8 range, name and kind' if passed else 'selected definition missing, duplicate or mismatched',
                                   'actual': found})
    for expected in fixture['cases']:
        found = [item for item in sites if item['path'] == expected['path'] and item['range'] == expected['range'] and item['role'] == expected['role']]
        status, reason, targets = 'failed', 'site missing or duplicated', []
        if len(found) == 1:
            actual = found[0]
            targets = [actual_to_gold.get(target, 'UNREVIEWED:' + target) for target in actual['targets']]
            if actual['text'] != expected['text']:
                reason = 'source text mismatch'
            elif expected['certainty'] == 'resolved':
                if actual['certainty'] == 'resolved' and set(targets) == set(expected['targets']):
                    status, reason = 'passed', 'exact supported source binding'
                else:
                    reason = 'resolved binding differs: ' + actual['reason']
            elif expected['certainty'] == 'candidate':
                if actual['certainty'] == 'unresolved' and not targets and actual['reason']:
                    status, reason = 'passed', 'conservative unresolved receiver retained; alternative coverage unmeasured'
                elif actual['certainty'] == 'candidate' and set(targets).issubset(expected['targets']) and not actual.get('targets_exhaustive'):
                    status, reason = 'passed', 'nonexhaustive reviewed alternatives retained'
                else:
                    reason = 'ambiguous site was dropped or falsely exact'
            elif actual['certainty'] == 'unresolved' and not targets and actual['reason']:
                status, reason = 'passed', 'unknown retained with a reason'
            else:
                reason = 'unsupported site falsely resolved'
        case_results.append({'id': expected['id'], 'language': expected['language'],
                             'construct': expected['construct'], 'role': expected['role'],
                             'status': status, 'reason': reason, 'expected_certainty': expected['certainty'],
                             'expected_targets': expected['targets'], 'actual_targets': targets, 'actual': found,
                             'supported_binding_scored': expected['certainty'] == 'resolved'})
        if expected['certainty'] == 'candidate':
            missing = sorted(set(expected['targets']) - set(targets))
            case_results[-1]['target_enumeration'] = {
                'status': 'failed' if missing else 'passed',
                'missing_reviewed_targets': missing,
                'reason': 'receiver target enumeration unsupported by this baseline',
                'required_for_syntax_direct_component': False,
            }
    return definition_results, case_results


def summaries(cases):
    result = {}
    for key in ('language', 'construct'):
        result[key] = {}
        for value in sorted({item[key] for item in cases}):
            selected = [item for item in cases if item[key] == value]
            supported_calls = [item for item in selected if item['role'] == 'call' and item['supported_binding_scored']]
            emitted_resolved = [item for item in supported_calls if len(item['actual']) == 1 and item['actual'][0]['certainty'] == 'resolved']
            correct = sum(item['status'] == 'passed' for item in supported_calls)
            result[key][value] = {'cases': len(selected), 'passed': sum(item['status'] == 'passed' for item in selected),
                                  'supported_call_cases': len(supported_calls), 'correct_supported_calls': correct,
                                  'resolved_supported_calls_emitted': len(emitted_resolved),
                                  'selected_supported_call_precision': correct / len(emitted_resolved) if emitted_resolved else None,
                                  'selected_supported_call_recall': correct / len(supported_calls) if supported_calls else None}
    return result


def write_result(root, path, result, maximum):
    """Descriptor-relative atomic output; reject symlink ancestors and leaves."""
    parts = SourceRoot.parts(path)
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    if len(raw) > maximum:
        raise ValueError('Result exceeds output budget; no report written')
    with SourceRoot(root) as source:
        if not source.secure:
            raise OSError('Secure output unsupported on this platform')
        parent = os.dup(source.fd)
        temporary = '.' + parts[-1] + '.' + uuid.uuid4().hex + '.tmp'
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                except FileNotFoundError:
                    os.mkdir(part, 0o755, dir_fd=parent)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            try:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError('Existing result must be a regular file')
            except FileNotFoundError:
                pass
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)
    return len(raw)


def environment():
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        memory = {'process_peak_rss': peak, 'unit': 'KiB' if sys.platform.startswith('linux') else 'bytes' if sys.platform == 'darwin' else 'platform_native',
                  'scope': 'entire component process, not a stage-isolated benchmark'}
    except ImportError:
        memory = {'process_peak_rss': None, 'scope': 'unavailable'}
    return {'python': platform.python_version(), 'platform': platform.system(),
            'architecture': platform.machine(), 'memory': memory}


def record_task(root, task, result, artifact, maximum):
    """Retain task evidence beside the frozen T004 proof; never qualify the product."""
    path = 'evaluations/results/code-understanding/engine.json'
    with SourceRoot(root) as source:
        report, _ = read_json(source, path)
    if not isinstance(report, dict) or 'T004' not in report.get('tasks', {}):
        raise ValueError('Committed freeze evidence required before task reporting')
    report['tasks'][task] = {
        'status': result['status'],
        'source_identity': result.get('input_identity', result.get('source_identity', report['source_identity'])),
        'case_results': result.get('definition_results', []) + result.get('case_results', []),
        'artifact': artifact,
        'artifact_sha256': hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode() + b'\n').hexdigest(),
        'scope': result.get('scope', 'Source eligibility screening; no reusable engine executed'),
        'coverage_failures': result.get('coverage_failures', []),
    }
    report.update(gate='code-understanding-progress', status='in_progress',
                  engine_selected=False, qualification_complete=False,
                  limits=['Task-scoped component evidence; any selected owner remains experimental until later product qualification.',
                          'The approved independent AI source key does not provide human UX or agent-answer grading.',
                          'Recorded finite update/query proofs do not qualify full T008 scale, agent, human, distribution or release gates.'])
    if task == 'T007':
        from evaluations.acceptance import experimental_owner_binding
        selected = (result.get('status') == 'passed' and result.get('engine_selected') is True and
            result.get('selected_owner') == 'native-tree-sitter' and
            result.get('owner_binding') == experimental_owner_binding(result))
        report['tasks'][task].update(engine_selected=selected,
            selected_owner=result.get('selected_owner') if selected else None,
            owner_binding=result.get('owner_binding') if selected else None,
            qualification_complete=False, selection_scope=result.get('selection_scope'))
        report.update(engine_selected=selected, selected_owner=result.get('selected_owner') if selected else None,
            selection_scope=result.get('selection_scope'), qualification_complete=False)
    write_result(root, path, report, maximum)


def installation():
    result = []
    for name, version in PINS.items():
        distribution = metadata.distribution(name)
        wheel = distribution.read_text('WHEEL') or ''
        tags = [line[5:] for line in wheel.splitlines() if line.startswith('Tag: ')]
        repo = 'py-tree-sitter' if name == 'tree-sitter' else name
        result.append({'package': name, 'version': version, 'license': 'MIT',
                       'metadata_url': f'https://pypi.org/pypi/{name}/{version}/json',
                       'license_url': f'https://github.com/tree-sitter/{repo}/blob/v{version}/LICENSE',
                       'installed_wheel_tags': tags,
                       'qualification': 'installed and executed in this isolated environment; other wheel platforms unexecuted'})
    return result


def component(root=ROOT, budget=None):
    fixture, identities = frozen_inputs(root)
    # Strip all gold annotations before the source-only extraction boundary.
    inventory = [{key: record[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')} for record in fixture['files']]
    native = scan(root, inventory, budget)
    definitions, cases = grade(native['facts'], fixture)
    failures = [item for item in definitions + cases if item['status'] == 'failed']
    coverage_failures = [{'id': item['id'], 'dimension': 'receiver_target_enumeration',
                         **item['target_enumeration']} for item in cases
                         if item.get('target_enumeration', {}).get('status') == 'failed']
    partial = native['status'] != 'complete'
    return {'schema_version': 1, 'suite': 'component', 'engine': 'tree-sitter',
            'status': 'failed' if failures or partial else 'passed',
            'engine_selected': False, 'human_evaluation': False,
            'scope': '44 selected synthetic sites and 75 selected definitions; not a complete syntax census or real-source quality gate',
            'input_identity': identities, 'backend': installation(), 'environment': environment(),
            'counts': {'selected_definitions': len(definitions), 'passed_definitions': len(definitions) - sum(item['status'] == 'failed' for item in definitions),
                       'selected_cases': len(cases), 'passed_cases': sum(item['status'] == 'passed' for item in cases),
                       'supported_binding_cases': sum(item['supported_binding_scored'] for item in cases),
                       'correct_supported_bindings': sum(item['supported_binding_scored'] and item['status'] == 'passed' for item in cases),
                       'unresolved_cases_preserved': sum(item['expected_certainty'] == 'unresolved' and item['status'] == 'passed' for item in cases),
                       'receiver_cases_conservatively_preserved': sum(item['expected_certainty'] == 'candidate' and item['status'] == 'passed' for item in cases),
                       'receiver_target_enumeration_failures': len(coverage_failures),
                       'inventoried_files': len(native['inventory']), 'source_files': sum(x['kind'] == 'source' for x in native['inventory'])},
            'by': summaries(cases), 'definition_results': definitions, 'case_results': cases,
            'failures': failures, 'coverage_failures': coverage_failures, 'scan': native,
            'qualification_limits': ['Synthetic source key is model-reviewed; human UX is unmeasured.',
                                     'Real-source precision/recall, incremental equivalence, scoped queries, scale and native harness distribution are not measured here.',
                                     'Receiver alternatives remain unresolved; preserving uncertainty does not establish candidate recall.',
                                     'Resolved means one source binding under the stated lexical/local-module rules, assuming no external runtime mutation.',
                                     'Native parser interruption is not implemented; source bytes and traversal are bounded.']}


def constructs(root=ROOT, budget=None):
    """Grade the locked synthetic key after both persisted-owner scans finish."""
    from repo_graph.analysis import IndexLimits, StructuralIndex
    root = Path(root)
    budget = budget or Budget()
    fixture, identity = frozen_inputs(root)
    inventory = [{key: item[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
                 for item in fixture['files']]
    code_paths = ('evaluations/analysis.py', 'repo_graph/analysis.py', 'repo_graph/analysis_native.py',
                  'repo_graph/analysis_queue.py', 'repo_graph/source.py', 'repo_graph/search.py', 'repo_graph/__init__.py',
                  'tests/test_analysis.py', 'pyproject.toml', 'uv.lock')
    with SourceRoot(ROOT) as owner:
        before = {path: owner.read(path, 1024 * 1024, hash_full=True)[1] for path in code_paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=20).strip()
    checks, coverage_failures, modes, fact_hashes = [], [], [], []
    with tempfile.TemporaryDirectory(prefix='repo-graph-constructs-') as scratch:
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            label = mode + str(concurrency)
            index = StructuralIndex(root, Path(scratch) / label, budget=budget,
                limits=IndexLimits(max_files=budget.max_files, max_source_bytes=budget.max_total_bytes,
                                   total_wall_seconds=budget.timeout_seconds))
            receipt = index.refresh(iter(inventory), mode=mode, concurrency=concurrency)
            extracted = {kind: list(index.read_facts(kind)) if receipt['status'] == 'ready' else []
                         for kind in ('definitions', 'sites', 'imports')}
            definitions, cases = grade(extracted, fixture)
            for case, expected in zip(cases, fixture['cases']):
                actual = case['actual'][0] if len(case['actual']) == 1 else {}
                case['coverage'] = {
                    'inventory': 'validated_source_inventory' if receipt['status'] == 'ready' and
                        any(item['path'] == expected['path'] and item['kind'] == 'source'
                            for item in inventory) else 'scan_not_ready',
                    'syntax': 'site_retained' if actual else 'site_missing_or_duplicated',
                    'binding': actual.get('certainty', 'unavailable'),
                    'call': actual.get('certainty', 'unavailable') if expected['role'] == 'call' else 'not_a_call',
                    'framework': 'not_evaluated',
                    'targets_exhaustive': actual.get('targets_exhaustive', False),
                }
                if case.get('target_enumeration', {}).get('status') == 'failed':
                    coverage_failures.append({'id': label + ':' + case['id'], 'mode': label,
                        'construct': case['construct'], 'dimension': 'receiver_target_enumeration',
                        **case['target_enumeration']})
            # Imports are native records from the same owner, not a second parser.
            invalid, import_languages = [], set()
            with SourceRoot(root) as source:
                for kind, rows in extracted.items():
                    for row in rows:
                        raw, digest, info = source.read(row['path'], budget.max_file_bytes + 1)
                        location = row['range']
                        start, end = location['start_byte'], location['end_byte']
                        valid = (0 <= start <= end <= len(raw) == info.st_size and
                            raw[start:end].decode('utf-8') == row['text'] and
                            location['start_line'] == raw[:start].count(b'\n') + 1 and
                            location['end_line'] == raw[:max(start, end - 1)].count(b'\n') + 1 and
                            row['provenance']['source_sha256'] == digest and
                            row['provenance']['evidence_kind'] == 'static_syntax')
                        if kind == 'sites':
                            valid = valid and row['role'] in ('call', 'reference')
                        if kind == 'imports':
                            import_languages.add(row['language'])
                            valid = valid and row['role'] == 'import'
                        if not valid:
                            invalid.append({'kind': kind, 'path': row['path'], 'range': location})
            checks.extend(dict(item, id=label + ':' + item['id'], mode=label)
                          for item in definitions + cases)
            checks.append({'id': label + ':distinct_facts_and_physical_provenance', 'mode': label,
                'status': 'passed' if not invalid and import_languages == set(LANGUAGES) else 'failed',
                'failures': invalid, 'import_languages': sorted(import_languages)})
            checks.append({'id': label + ':coherent_complete_inventory', 'mode': label,
                'status': 'passed' if receipt['status'] == 'ready' and
                    receipt['coverage']['files_total'] == len(inventory) and
                    receipt['coverage']['files_supported'] == sum(item['kind'] == 'source' for item in inventory) and
                    receipt['coverage']['status_counts'].get('partial_parse', 0) == 0 else 'failed'})
            digest = hashlib.sha256()
            if receipt['status'] == 'ready':
                for kind in ('definitions', 'sites', 'imports', 'scopes', 'relationships'):
                    for row in index.read_facts(kind):
                        digest.update(json.dumps([kind, row], sort_keys=True, ensure_ascii=True,
                                                 separators=(',', ':')).encode() + b'\n')
            fact_hashes.append(digest.hexdigest())
            modes.append({'mode': mode, 'concurrency': concurrency,
                'status': 'passed' if all(item['status'] == 'passed' for item in checks if item.get('mode') == label) else 'failed',
                'index': receipt, 'metadata': index.metadata() if receipt['status'] == 'ready' else None,
                'facts_sha256': digest.hexdigest(),
                'counts': {kind: len(rows) for kind, rows in extracted.items()}, 'by': summaries(cases)})
    checks.append({'id': 'serial_queued_fact_parity',
                   'status': 'passed' if len(set(fact_hashes)) == 1 and
                       all(mode['index']['status'] == 'ready' for mode in modes) else 'failed'})
    with SourceRoot(ROOT) as owner:
        after = {path: owner.read(path, 1024 * 1024, hash_full=True)[1] for path in code_paths}
    checks.append({'id': 'implementation_stable', 'status': 'passed' if before == after else 'failed'})
    failures = [item for item in checks if item['status'] != 'passed']
    return {'schema_version': 1, 'suite': 'constructs', 'engine': 'native-tree-sitter',
        'status': 'failed' if failures else 'passed',
        'source_identity': {'inputs': identity, 'files': {item['path']: item['sha256'] for item in inventory},
                            'implementation': {'commit': revision, 'sha256': before}},
        'implementation_after': {'commit': revision, 'sha256': after},
        'case_results': checks, 'failures': failures, 'coverage_failures': coverage_failures, 'modes': modes,
        'counts': {'selected_definitions_per_mode': len(fixture['definitions']),
                   'selected_sites_per_mode': len(fixture['cases']), 'checks': len(checks),
                   'passed': len(checks) - len(failures), 'receiver_enumeration_failures': len(coverage_failures)},
        'environment': environment(), 'qualification_complete': False, 'limits_qualified': False,
        'scope': 'Locked selected synthetic constructs through the shared persisted owner in serial1 and queued2; '
                 'receiver alternatives are conservatively unresolved and enumeration misses remain coverage failures; '
                 'framework, corpus, scale, query, platform, agent, human and release qualification is unmeasured'}


def evidence(root=ROOT, budget=None, evidence_directory=None):
    """Grade frozen function questions against the actual shared persisted projection."""
    from contextlib import ExitStack, closing, redirect_stdout
    from dataclasses import replace
    import io
    import traceback
    from unittest.mock import patch
    from repo_graph import analysis as index_module, search
    from repo_graph.analysis import StructuralIndex
    from repo_graph.cli import main as cli_main
    from evaluations.engine_checks import _adapter_materialize
    from evaluations.supplement_preparation import SOURCE, ORACLE, prepare_check
    root, budget = Path(root), budget or Budget()
    fixture, frozen = frozen_inputs(root)
    preparation = prepare_check(root)
    with SourceRoot(root) as source:
        manifest, source_manifest_sha = read_json(source, SOURCE)
        oracle, oracle_sha = read_json(source, ORACLE)
    records = {row['path']: {key: row[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
               for row in manifest['files']}
    source_bytes = {row['path']: row['content_utf8'].encode() for row in manifest['files']}
    questions = _evidence_questions(fixture, oracle, records)
    controls = {}
    for label, (path, text, expected) in EVIDENCE_CONTROLS.items():
        raw = text.encode()
        if hashlib.sha256(raw).hexdigest() != expected: raise ValueError('Frozen evidence control changed')
        controls[label] = {'path': path, 'sha256': expected, 'bytes': len(raw)}
    code_paths = ('evaluations/analysis.py', 'evaluations/engine_checks.py',
        'evaluations/supplement_preparation.py', 'repo_graph/analysis.py', 'repo_graph/analysis_native.py',
        'repo_graph/analysis_queue.py', 'repo_graph/analysis_queries.py', 'repo_graph/search.py',
        'repo_graph/source.py', 'repo_graph/cli.py', 'repo_graph/server.py', 'repo_graph/__init__.py',
        'tests/test_analysis.py', 'tests/test_search.py', 'pyproject.toml', 'uv.lock')
    with SourceRoot(ROOT) as owner:
        before = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=20).strip()
    if evidence_directory is not None:
        evidence_directory = Path(evidence_directory).resolve()
        if evidence_directory == root or root in evidence_directory.parents:
            raise ValueError('Evidence receipts must remain outside the source checkout')
        evidence_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = Path(tempfile.mkdtemp(prefix='evidence-', dir=evidence_directory))
    cases, mode_receipts, parity, artifacts = [], [], [], []
    encoded = lambda value: json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    digest = lambda value: hashlib.sha256(encoded(value)).hexdigest()

    def retain(entry, event, value):
        name = entry['id'] + '-%02d-' % len(entry.get('artifacts', [])) + event + '.json'
        write_result(logs, name, value, 2 * 1024 * 1024)
        raw = (logs / name).read_bytes()
        proof = {'name': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        entry.setdefault('artifacts', []).append(proof); artifacts.append(proof)

    def run(entry, operation):
        begun = time.monotonic()
        try:
            operation()
            entry['status'] = 'passed'
        except Exception as error:
            retain(entry, 'failure', {'error_kind': type(error).__name__, 'reason': str(error),
                'traceback': traceback.format_exc()})
            entry.update(status='failed', error_kind=type(error).__name__,
                reason='Function evidence contract failed; full exception retained privately')
        entry['elapsed_seconds'] = time.monotonic() - begun
        cases.append(entry)

    def refresh(index, entries, entry, mode, concurrency):
        receipt = index.refresh(entries, mode=mode, concurrency=concurrency)
        retain(entry, 'refresh', receipt)
        entry.setdefault('attempts', []).append({'status': receipt['status'], 'published': receipt['published'],
            'generation': receipt.get('generation'), 'source_identity': receipt.get('source_identity'),
            'resources': {key: value for key, value in receipt['resources'].items()
                if type(value) in (int, float, bool)}})
        return receipt

    def definitions(index):
        return {row['id']: row for row in index.read_facts('definitions')}

    def query(index, blobs, declarations, entry, request, limits=None):
        begun = time.monotonic()
        result = search.Search(index.output).run(**request, limits=limits)
        elapsed = time.monotonic() - begun
        retain(entry, 'response', result)
        applied = result['budgets']
        assert len(encoded(result)) <= applied['max_response_bytes']
        meta = index.metadata()
        identities = result['identities']
        for key, expected in (('repository_identity', index.owner), ('source_identity', meta['source_identity']),
                ('structural_generation', meta['generation']), ('analyzer_identity', meta['analyzer_identity']),
                ('config_identity', meta['config_identity'])):
            assert identities[key] == expected, (key, identities, meta)
        assert re.fullmatch('[0-9a-f]{64}', identities['evidence_generation'])
        members, intervals, normalized, excerpt_bytes = {}, {}, [], 0
        for row in result['results']:
            raw = blobs[row['path']]; span = row['range']; lo, hi = span['start_byte'], span['end_byte']
            assert 0 <= lo <= hi <= len(raw)
            assert row['file_sha256'] == hashlib.sha256(raw).hexdigest()
            assert row['evidence_kind'] == 'static_syntax'
            assert span['start_line'] == raw[:lo].count(b'\n') + 1
            assert span['end_line'] == raw[:max(lo, hi - 1)].count(b'\n') + 1
            assert row['raw_digest'] == hashlib.sha256(raw[lo:hi]).hexdigest()
            assert type(row['redacted']) is bool and type(row['excerpt_truncated']) is bool
            if not row['redacted']: assert row['text'].encode() == raw[lo:hi]
            excerpt_bytes += len(row['text'].encode())
            identity = row['path'], row['file_sha256']
            if hi > lo:
                assert all(hi <= start or end <= lo for start, end in intervals.setdefault(identity, []))
                intervals[identity].append((lo, hi))
            for member in row['members']:
                definition = declarations[member['symbol_id']]
                assert definition['callable'] is True and definition['kind'] in ('function', 'method', 'function_value')
                assert member['name'] == definition['name'] and member['kind'] == definition['kind']
                assert member['range'] == definition['range'] and definition['path'] == row['path']
                assert definition['provenance']['source_sha256'] == row['file_sha256']
                assert row['language'] == definition['language']
                assert row['extraction_state'] == 'parsed'  # All frozen/control source files in this suite are parsed.
                members[member['symbol_id']] = member
            normalized.append({key: row[key] for key in ('path', 'file_sha256', 'range', 'raw_digest',
                'redacted', 'excerpt_truncated', 'members')})
        assert excerpt_bytes <= applied['max_excerpt_bytes']
        assert len(members) <= applied['max_entities']
        assert result['counts']['returned_symbol_handles'] == len(members)
        entry.setdefault('observations', []).append({'identities': identities, 'budgets': applied,
            'counts': result['counts'], 'truncated': result['truncated'], 'stop_reason': result['stop_reason'],
            'response_sha256': digest(result), 'response_bytes': len(encoded(result)),
            'normalized_rows_sha256': digest(normalized), 'excerpt_bytes': excerpt_bytes,
            'elapsed_seconds': elapsed})
        return result, normalized, members

    class FakeEmbedding:
        name = 'synthetic-evidence-v1'
        packed = staticmethod(lambda value: value)
        def passages(self, texts): return [b'fresh-vector' for _ in texts]

    with tempfile.TemporaryDirectory(prefix='repo-graph-function-source-') as temporary:
        directory = Path(temporary); source = directory / 'source'; control = directory / 'controls'
        source.mkdir(); control.mkdir(); _adapter_materialize(source, source_bytes)
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            label = mode + str(concurrency)
            index = StructuralIndex(source, directory / (label + '-functions'), budget=budget)
            setup = {'id': label + '-snapshot', 'mode': label}
            receipt = refresh(index, list(records.values()), setup, mode, concurrency)
            assert receipt['status'] == 'ready' and receipt['published'], receipt
            declared = definitions(index)
            fact_rows = {kind: list(index.read_facts(kind)) for kind in
                         ('definitions', 'sites', 'imports', 'scopes', 'relationships')}
            mode_receipts.append({'mode': mode, 'concurrency': concurrency, 'index': setup['attempts'][0],
                'artifacts': setup['artifacts'], 'facts_sha256': digest(fact_rows),
                'unresolved_sites': sum(row['certainty'] == 'unresolved' for row in fact_rows['sites']),
                'reference_sites': sum(row['role'] == 'reference' for row in fact_rows['sites'])})
            proofs = []
            for question in questions:
                entry = {'id': label + '-' + question['id'], 'mode': label, 'question_id': question['id']}
                def grade_question(question=question, entry=entry):
                    result, normalized, members = query(index, source_bytes, declared, entry, question['request'])
                    actual = {(member['name'], json.dumps(member['range'], sort_keys=True)) for member in members.values()}
                    for expected in question['source_grounding']:
                        if 'name' not in expected: continue
                        key = expected['name'], json.dumps(expected['declaration_range'], sort_keys=True)
                        assert (key in actual) is bool(question['required_member_keys'])
                    if question['function_result_count'] == 0:
                        assert result['results'] == [] and not result['truncated'] and result['stop_reason'] is None
                    if question['id'] == 'T014-Q13':
                        blocks = [row for row in result['results'] if any(member['name'] == 'shadow' for member in row['members'])]
                        assert len(blocks) == 1
                        assert blocks[0]['range']['start_byte'] == 374 and blocks[0]['range']['end_byte'] == 456
                        assert {'shadow', 'shadow.local'} <= {member['name'] for member in blocks[0]['members']}
                    entry['required_member_keys'] = question['required_member_keys']
                    entry['excluded_member_keys'] = question['excluded_member_keys']
                    proofs.append({'id': question['id'], 'rows_sha256': digest(normalized)})
                run(entry, grade_question)
            parity.append({'mode': label, 'questions_sha256': digest(proofs), 'facts_sha256': digest(fact_rows)})
            old = {EVIDENCE_CONTROLS[key][0]: EVIDENCE_CONTROLS[key][1].encode() for key in ('old', 'guide')}
            _adapter_materialize(control, old)
            controlled = StructuralIndex(control, directory / (label + '-control'), budget=budget)
            def stage(entries, entry):
                result = refresh(controlled, entries, entry, mode, concurrency)
                assert result['status'] == 'ready' and result['published'], result
                return result
            state = {}
            for number in range(1, 7):
                entry = {'id': label + '-T014-C%02d' % number, 'mode': label, 'control': number}
                def grade_control(number=number, entry=entry):
                    if number == 1:
                        with redirect_stdout(io.StringIO()):
                            assert cli_main(['map', str(control), '--output', str(controlled.output)]) == 0
                        graph = json.loads((controlled.output / 'graph.json').read_text())
                        state['map'] = {key: graph[key] for key in ('files', 'file_count', 'tree', 'dependencies', 'scope_edges', 'system')}
                        state['keyword'] = search.Search(controlled.output).run('oldFunction', mode='keyword')['results']
                        search.embed_index(controlled.output, FakeEmbedding())
                        with closing(search.connect(controlled.output, readonly=True)) as db:
                            state['file_vectors'] = [tuple(row) for row in db.execute('SELECT path,vector FROM docs ORDER BY path')]
                        stage(['main.py'], entry)
                        assert search.Search(controlled.output).run('oldFunction', mode='keyword')['results'] == state['keyword']
                        with redirect_stdout(io.StringIO()):
                            assert cli_main(['map', str(control), '--output', str(controlled.output)]) == 0
                        graph = json.loads((controlled.output / 'graph.json').read_text())
                        assert {key: graph[key] for key in state['map']} == state['map']
                        result, _, _ = query(controlled, old, definitions(controlled), entry,
                            {'query': 'oldFunction', 'kind': 'functions', 'mode': 'keyword'})
                        assert any(member['name'] == 'oldFunction' for row in result['results'] for member in row['members'])
                    elif number == 2:
                        previous = (controlled.output / 'search.db').read_bytes()
                        projector = search.project_function_evidence
                        _adapter_materialize(control, {'main.py': EVIDENCE_CONTROLS['new'][1].encode()})
                        def exhausted(db, identity, *, check, limits=None):
                            return projector(db, identity, check=check, limits=search.FunctionProjectionLimits(max_body_bytes=1))
                        with patch.object(index_module, 'project_function_evidence', exhausted):
                            failed = refresh(controlled, ['main.py'], entry, mode, concurrency)
                        assert failed['status'] == 'failed' and not failed['published']
                        assert (controlled.output / 'search.db').read_bytes() == previous
                        _adapter_materialize(control, old); stage(['main.py'], entry)
                        # The frozen pre-feature file reader ignores the additive projection tables.
                        legacy = logs / (label + '-legacy'); (legacy / 'repo_graph').mkdir(parents=True)
                        old_commit = '3d03ea19e504cc46a7e68f9964dcaf822c394c6f'
                        source_hashes = {}
                        for filename in ('search.py', 'source.py', '__init__.py'):
                            raw = subprocess.check_output(['git', 'show', old_commit + ':repo_graph/' + filename], cwd=ROOT)
                            (legacy / 'repo_graph' / filename).write_bytes(raw); source_hashes[filename] = hashlib.sha256(raw).hexdigest()
                        script = 'import sys,json;sys.path.insert(0,sys.argv[1]);from repo_graph.search import Search;print(json.dumps(Search(__import__("pathlib").Path(sys.argv[2])).run("oldFunction",mode="keyword")))'
                        child = subprocess.run([sys.executable, '-S', '-c', script, str(legacy), str(controlled.output)],
                            text=True, capture_output=True, timeout=5, check=True)
                        observed = json.loads(child.stdout)
                        retain(entry, 'legacy-reader', {'commit': old_commit, 'sha256': source_hashes, 'response': observed})
                        assert observed['results'] == state['keyword']
                    elif number == 3:
                        for mutation in ('content', 'evidence_config', 'model'):
                            _adapter_materialize(control, old); stage(['main.py'], entry)
                            with closing(search.connect(controlled.output)) as db, db:
                                db.execute('UPDATE function_docs SET vector=NULL'); db.execute("DELETE FROM meta WHERE key='function_model'")
                            class ConcurrentEmbedding(FakeEmbedding):
                                def passages(self, texts):
                                    if mutation == 'content':
                                        _adapter_materialize(control, {'main.py': EVIDENCE_CONTROLS['new'][1].encode()})
                                        stage(['main.py'], entry)
                                    elif mutation == 'evidence_config':
                                        projector = search.project_function_evidence
                                        def configured(db, identity, *, check, limits=None):
                                            return projector(db, identity, check=check,
                                                limits=replace(search.FunctionProjectionLimits(), max_window_bytes=64))
                                        with patch.object(index_module, 'project_function_evidence', configured): stage(['main.py'], entry)
                                    else:
                                        with closing(search.connect(controlled.output)) as db, db:
                                            db.execute("INSERT OR REPLACE INTO meta VALUES('function_model','synthetic-evidence-v2')")
                                    return [b'stale-vector' for _ in texts]
                            try: search.embed_index(controlled.output, ConcurrentEmbedding(), kind='functions')
                            except RuntimeError: pass
                            else: raise AssertionError('Old function vectors were admitted after a remap/model/config change')
                            with closing(search.connect(controlled.output, readonly=True)) as db:
                                assert all(row[0] is None for row in db.execute('SELECT vector FROM function_docs'))
                                assert [tuple(row) for row in db.execute('SELECT path,vector FROM docs ORDER BY path')] == state['file_vectors']
                            entry.setdefault('CAS_mutations_rejected', []).append(mutation)
                        _adapter_materialize(control, old); stage(['main.py'], entry)
                        with closing(search.connect(controlled.output)) as db, db:
                            db.execute("DELETE FROM meta WHERE key='function_model'")
                        embedded = search.embed_index(controlled.output, FakeEmbedding(), kind='functions')
                        retain(entry, 'matching-embedding', embedded); assert embedded['embedded'] > 0
                        assert search.embed_index(controlled.output, FakeEmbedding(), kind='functions')['reused'] > 0
                    elif number == 4:
                        raw = EVIDENCE_CONTROLS['redacted'][1].encode(); _adapter_materialize(control, {'redacted.py': raw})
                        stage(['redacted.py'], entry)
                        result, _, _ = query(controlled, {'redacted.py': raw}, definitions(controlled), entry,
                            {'query': 'redacted', 'kind': 'functions', 'mode': 'keyword'})
                        assert result['results'] and all(row['redacted'] for row in result['results'])
                        assert 'sk-' + 'x' * 30 not in json.dumps(result)
                    elif number == 5:
                        for cap in (128, 0):
                            result, _, _ = query(index, source_bytes, declared, entry,
                                {'query': 'hub', 'kind': 'functions', 'mode': 'keyword',
                                 'prefix': oracle['query']['source_path']},
                                search.EvidenceLimits(max_response_bytes=8192, max_excerpt_bytes=cap))
                            assert result['truncated'] and result['results']
                            assert all(row['excerpt_truncated'] for row in result['results'])
                        try:
                            search.Search(index.output).run('hub', kind='functions', mode='keyword',
                                limits=search.EvidenceLimits(max_response_bytes=1))
                        except ValueError: pass
                        else: raise AssertionError('Response smaller than its minimum envelope was not rejected')
                    else:
                        script = ('import sys,json;from pathlib import Path;from repo_graph.search import Search,index_status;'
                            'out=Path(sys.argv[1]);result=Search(out).run("café",kind="functions",mode="keyword",prefix=sys.argv[2]);'
                            'print(json.dumps({"response":result,"status":index_status(out,backend_available=False),'
                            '"optional_loaded":[key for key in ("numpy","fastembed","tree_sitter") if key in sys.modules]}))')
                        child = subprocess.run([sys.executable, '-S', '-c', script, str(index.output),
                            'tests/fixtures/code-understanding/python/main.py'], cwd=ROOT,
                            text=True, capture_output=True, timeout=5, check=True)
                        observed = json.loads(child.stdout); retain(entry, 'missing-backend', observed)
                        assert observed['optional_loaded'] == [] and observed['response']['results']
                        with patch.object(search.Embeddings, '__init__', side_effect=AssertionError('No implicit model initialization')):
                            assert search.Search(index.output).run('café', kind='functions', mode='keyword')['results']
                            try: search.Search(index.output).run('café', kind='functions', mode='semantic')
                            except RuntimeError: pass
                            else: raise AssertionError('Unindexed/no-backend function semantic query silently fell back')
                run(entry, grade_control)
    checks = [{'id': 'serial_queued_source_evidence_parity', 'status': 'passed' if len(parity) == 2 and
        parity[0]['questions_sha256'] == parity[1]['questions_sha256'] and parity[0]['facts_sha256'] == parity[1]['facts_sha256'] else 'failed'}]
    with SourceRoot(ROOT) as owner:
        after = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    checks.append({'id': 'implementation_stable', 'status': 'passed' if before == after else 'failed'})
    failures = [entry for entry in cases + checks if entry['status'] != 'passed']
    result = {'schema_version': 1, 'suite': 'evidence', 'status': 'failed' if failures else 'passed',
        'source_identity': {'inputs': frozen, 'supplement_source_sha256': source_manifest_sha,
            'supplement_oracle_sha256': oracle_sha, 'supplement_identity': preparation['base_source_identity'],
            'frozen_projection_sha256': EVIDENCE_PROJECTION_SHA, 'portable_question_core_sha256': EVIDENCE_QUESTION_CORE_SHA,
            'controls': controls, 'implementation': {'commit': revision, 'sha256': before}},
        'implementation_after': {'commit': revision, 'sha256': after}, 'case_results': cases,
        'checks': checks, 'failures': failures, 'coverage_failures': [], 'modes': mode_receipts,
        'counts': {'questions': 16, 'question_mode_runs': 32, 'control_groups': 6, 'control_mode_runs': 12,
            'checks': len(cases) + len(checks), 'passed': len(cases) + len(checks) - len(failures)},
        'environment': environment(), 'private_artifacts': artifacts,
        'zero_case_scope': {'model_ranking_quality': 0, 'human_UX': 0, 'frameworks': 0,
            'real_corpora': 0, 'scale': 0, 'new_platform_installations': 0, 'runtime_target_enumeration': 0},
        'qualification_complete': False, 'limits_qualified': False,
        'scope': 'Frozen selected source evidence/membership/digest/dedup and storage controls only; '
                 'AI source review is not human UX. Fake vectors qualify CAS/storage only, not semantic ranking.'}
    if len(encoded(result)) > 192 * 1024:
        raise ValueError('T014 receipt exceeds its frozen 192 KiB representation cap; raw observations retained privately')
    return result


def record_structural(root, task, result, maximum):
    """Replace one structural task, retaining its earlier recorded proofs."""
    with SourceRoot(root) as source:
        report, _ = read_json(source, FACTS_OUTPUT, maximum)
    if (type(report) is not dict or type(report.get('schema_version')) is not int or
            report['schema_version'] != 1 or type(report.get('tasks')) is not dict or 'T009' not in report['tasks']):
        raise ValueError('Existing T009 facts evidence required')
    preceding = {'T010': 'T009', 'T011': 'T010', 'T012': 'T011', 'T013': 'T012', 'T014': 'T013'}
    if task not in preceding or preceding[task] not in report['tasks']:
        raise ValueError('Known structural task and its preceding proof required')
    report['tasks'][task] = result
    return write_result(root, FACTS_OUTPUT, report, maximum)


def record_constructs(root, result, maximum):
    return record_structural(root, 'T010', result, maximum)


def impact(root=ROOT, budget=None, *, interfaces=False):
    """Exercise captured reverse imports/calls against the locked source key."""
    from repo_graph.analysis import IndexLimits, StructuralIndex
    from repo_graph.analysis_queries import Queries, encoded
    from repo_graph.search import connect
    from evaluations.engine_checks import _adapter_materialize
    root, budget = Path(root), budget or Budget(timeout_seconds=20)
    fixture, frozen = frozen_inputs(root)
    with SourceRoot(root) as owner:
        blobs = {r['path']: owner.read(r['path'], 1024 * 1024, hash_full=True)[0] for r in fixture['files']}
    inventory = {r['path']: {k: r[k] for k in ('path', 'language', 'kind', 'sha256', 'bytes')}
                 for r in fixture['files']}
    imported = [r for r in fixture['cases'] if r['id'] in ('PY-IMPORT', 'GO-IMPORT', 'JS-IMPORT', 'TS-IMPORT')]
    if len(imported) != 4:
        raise ValueError('Four locked language import judgments required')
    question_ids = [r['id'] + '-reverse-impact' for r in imported] + [
        'physical_pagination_no_duplicates', 'selection_work', 'selection_entities', 'setup_deadline',
        'cancelled_without_facts', 'invalid_area_refused', 'unimplemented_contract_refused',
        'changed_clean_shared_projection_parity', 'git_body_and_deleted_preimage_boundary',
        'changed_impact_cursor_refused', 'ordinary_calls_keep_captured_snapshot', 'implementation_stable']
    if interfaces:
        question_ids += ['cli_owned_git_capture', 'cli_impact_relation_filters',
            'http_and_direct_impact_agree', 'http_captured_import_evidence',
            'http_stale_source_refused', 'system_explore_shared_snapshot']
    declarations = {r['id']: r for r in fixture['definitions']}
    paths = ('evaluations/analysis.py', 'repo_graph/analysis.py', 'repo_graph/analysis_queries.py',
             'repo_graph/analysis_native.py', 'repo_graph/analysis_queue.py', 'repo_graph/source.py',
             'repo_graph/search.py', 'tests/test_analysis.py', 'pyproject.toml', 'uv.lock')
    if interfaces: paths += ('repo_graph/cli.py', 'repo_graph/server.py', 'repo_graph/builder.py')
    with SourceRoot(root) as owner:
        before = {p: owner.read(p, 2 * 1024 * 1024, hash_full=True)[1] for p in paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=5).strip()
    cases, modes = [], []
    def case(identifier, action):
        row = {'id': identifier, 'status': 'failed'}
        cases.append(row)
        try:
            row['observed'] = action()
            row['status'] = 'passed'
        except Exception as error:
            row['error_kind'] = type(error).__name__
            if isinstance(error, AssertionError): row['reason'] = str(error)[:256]
    def observed(value):
        cases[-1]['observed'] = value
        return value
    def physical(handle):
        return handle['path'], handle['range']
    def projection(index):
        db = connect(index.output, readonly=True, owner=index.output_owner)
        try:
            return [dict(r, data=json.loads(r['data'])) for r in db.execute(
                'SELECT * FROM structural_import_relationships ORDER BY path,ordinal,target_path')]
        finally: db.close()
    def facts(index):
        return {k: list(index.read_facts(k)) for k in ('definitions', 'sites', 'imports', 'scopes', 'relationships')}
    def refused(action):
        try: action()
        except ValueError: return {'refused': True}
        raise AssertionError('Invalid or stale request was admitted')
    with tempfile.TemporaryDirectory(prefix='repo-graph-impact-') as scratch:
        directory, source = Path(scratch), Path(scratch) / 'source'
        source.mkdir(); _adapter_materialize(source, blobs)
        env = {'PATH': os.environ.get('PATH', ''), 'GIT_CONFIG_GLOBAL': os.devnull,
               'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}
        def git(*args):
            return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull,
                '-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
                '-C', str(source), *args], env=env, stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        git('init', '--initial-branch=main'); git('add', '--all'); git('commit', '-m', 'Synthetic impact base')
        base = git('rev-parse', 'HEAD')
        limits = IndexLimits(max_files=budget.max_files, max_source_bytes=budget.max_total_bytes,
                             total_wall_seconds=budget.timeout_seconds)
        index = StructuralIndex(source, directory / 'index', budget=budget, limits=limits)
        receipt = index.refresh(list(inventory.values()))
        modes.append({'phase': 'base', 'receipt': receipt})
        if interfaces and receipt['status'] == 'ready':
            from contextlib import redirect_stdout
            from io import StringIO
            from repo_graph.cli import main as command
            def capture_cli(args):
                stream = StringIO()
                with redirect_stdout(stream): code = command(args)
                response = observed({'returncode': code, 'response': json.loads(stream.getvalue())})
                assert code == 0, 'Required product command failed'
                return response['response']
            def capture_git():
                result = capture_cli(['analyze', str(source), '--output', str(index.output), '--git-base', base])
                assert result['status'] == 'ready'
                return result
            case('cli_owned_git_capture', capture_git)
            if cases[-1]['status'] == 'passed':
                receipt = cases[-1]['observed']
                modes.append({'phase': 'cli-captured-base', 'receipt': receipt})
        if receipt['status'] != 'ready':
            cases.append({'id': 'base_publication', 'status': 'failed', 'observed': receipt})
        else:
            with Queries(index.output) as queries:
                if interfaces:
                    target_path = declarations[next(r for r in imported if r['id'] == 'PY-IMPORT')['targets'][0]]['path']
                    payload = {'operation': 'impact', 'selector': {'kind': 'source_area', 'paths': [target_path]},
                               'role': 'all', 'relations': ['import'], 'limits': {'max_edges': 4}}
                    def cli_filters():
                        answers = observed([])
                        for relation in ('import', 'call'):
                            expected = queries.run(dict(payload, relations=[relation]))
                            actual = capture_cli(['query', str(index.output), '--operation', 'impact',
                                '--source-area', target_path, '--role', 'all', '--relation', relation,
                                '--certainty', 'resolved', '--certainty', 'candidate', '--certainty', 'unresolved',
                                '--limits', json.dumps(payload['limits'])])
                            answers.append({'relation': relation, 'cli': actual, 'direct': expected})
                            observed(answers)
                            assert actual['rows'] == expected['rows'] and actual['generation'] == expected['generation']
                            assert all(row['relation'] == relation for row in actual['rows'])
                            assert actual['cursor'] is None
                        return answers
                    case('cli_impact_relation_filters', cli_filters)
                    from repo_graph.search import Search
                    from repo_graph.server import create_server
                    from threading import Thread
                    from urllib.request import Request, urlopen
                    from urllib.error import HTTPError
                    with create_server(Search(index.output)) as server:
                        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
                        def post(endpoint, request):
                            body = encoded(request)
                            assert len(body) <= 8192
                            action = Request('http://127.0.0.1:' + str(server.server_port) + endpoint, body,
                                headers={'Content-Type': 'application/json', 'X-Repo-Graph-Output': server.engine.owner})
                            try:
                                with urlopen(action, timeout=5) as response: code, raw = response.status, response.read(32769)
                            except HTTPError as error:
                                with error: code, raw = error.code, error.read(32769)
                            result = observed({'status': code, 'bytes': len(raw), 'response': json.loads(raw)})
                            assert len(raw) <= 32768
                            return result
                        try:
                            def http_query():
                                actual, expected = post('/api/query', payload), queries.run(payload)
                                assert actual['status'] == 200 and actual['response']['rows'] == expected['rows']
                                assert actual['response']['impact_identity'] == expected['impact_identity']
                                return actual
                            case('http_and_direct_impact_agree', http_query)
                            imports = queries.run(payload)
                            site = next(row['site'] for row in imports['rows'] if row['relation'] == 'import')
                            handle = {k: site[k] for k in ('id', 'path', 'range', 'source_sha256')}
                            request = {'generation': imports['generation'], 'handle': handle, 'max_excerpt_bytes': 4096}
                            def import_source():
                                result = post('/api/source', request)
                                assert result['status'] == 200
                                response = result['response']; span = handle['range']
                                assert response['source_sha256'] == handle['source_sha256'] and response['range'] == span
                                assert response['text'].encode() == blobs[handle['path']][span['start_byte']:span['end_byte']]
                                assert response['kind'] == 'import' and response['targets_exhaustive'] is False
                                assert response['scope'] == 'admitted_source_candidates_runtime_unqualified'
                                return result
                            case('http_captured_import_evidence', import_source)
                            def stale_source():
                                result = post('/api/source', dict(request, generation='f' * 64))
                                assert result['status'] == 409 and 'text' not in result['response']
                                return result
                            case('http_stale_source_refused', stale_source)
                        finally:
                            server.shutdown(); thread.join(timeout=5)
                            assert not thread.is_alive(), 'Owned interface server did not stop'
                    def system_bridge():
                        from repo_graph.builder import main as mapping
                        with redirect_stdout(StringIO()): result = mapping([str(source), '--output', str(index.output)])
                        graph = json.loads((index.output / 'graph.json').read_text())
                        observation = observed({k: graph[k] for k in ('scan', 'system', 'scope_edges', 'index_status')})
                        assert result == 0 and graph['scan']['basis'] == 'shared_structural_index'
                        assert graph['scan']['source_reads_during_map'] == 0
                        assert graph['scan']['identities']['generation'] == receipt['generation']
                        assert len(graph['system']['nodes']) <= 12 and graph['files'] == sorted(inventory)
                        return observation
                    case('system_explore_shared_snapshot', system_bridge)
                for expected in imported:
                    target = declarations[expected['targets'][0]]
                    payload = {'operation': 'impact', 'selector': {'kind': 'source_area', 'paths': [target['path']]}, 'role': 'all'}
                    def grade_import(expected=expected, target=target, payload=payload):
                        result = observed(queries.run(payload))
                        assert any(r['relation'] == 'call' and physical(r['site']) == physical(expected) and
                            r['target'] and physical(r['target']) == physical(target) for r in result['rows']), 'Reviewed incoming call missing'
                        assert any(r['relation'] == 'import' and r['site']['path'] == expected['path'] and
                            r['target'] and r['target']['path'] == target['path'] for r in result['rows']), 'Captured reverse import missing'
                        assert result['contracts_available'] is False and result['runtime_complete'] is False
                        assert result['impact_identity'] == receipt['impact_identity']
                        assert len(encoded(result)) <= 32768 and result['examined_work'] <= 10000
                        if expected['language'] == 'go':
                            assert all(r['certainty'] == 'candidate' for r in result['rows'] if
                                r['relation'] == 'import' and r['target'] is not None), 'Go build membership falsely exact'
                        return result
                    case(expected['id'] + '-reverse-impact', grade_import)
                py = next(r for r in imported if r['id'] == 'PY-IMPORT')
                target = declarations[py['targets'][0]]
                payload = {'operation': 'impact', 'selector': {'kind': 'source_area', 'paths': [target['path']]}, 'role': 'all'}
                def pages():
                    collected, observations, cursor = [], [], None
                    observed(observations)
                    for _ in range(80):
                        page = queries.run(dict(payload, limits={'max_edges': 1, 'max_response_bytes': 8192},
                            **({'cursor': cursor} if cursor else {})))
                        observations.append(page)
                        assert len(encoded(page)) <= 8192 and page['returned_edges'] <= 1
                        collected.extend(page['rows']); cursor = page['cursor']
                        if cursor is None: break
                    assert cursor is None, 'Bounded synthetic pagination did not finish'
                    keys = [(r['relation'], r['site']['id'], r['target']['id'] if r['target'] else '') for r in collected]
                    assert len(keys) == len(set(keys)), 'Physical relation repeated across pages'
                    return observations
                case('physical_pagination_no_duplicates', pages)
                for name, reduced, reason in (
                    ('selection_work', {'max_examined_relationships': 1}, 'work_budget_exceeded'),
                    ('selection_entities', {'max_entities': 1}, 'entity_budget_exceeded'),
                    ('setup_deadline', {'timeout_seconds': 1e-9}, 'deadline_exceeded')):
                    def stopped(reduced=reduced, reason=reason):
                        response = observed(queries.run(dict(payload, limits=reduced)))
                        assert response['stop_reason'] == reason, 'Requested boundary was not observed'
                        return response
                    case(name, stopped)
                def cancelled():
                    response = observed(queries.run(payload, cancel=lambda: True))
                    assert response['stop_reason'] == 'cancelled'
                    return response
                case('cancelled_without_facts', cancelled)
                case('invalid_area_refused', lambda: refused(lambda: queries.run(dict(payload,
                    selector={'kind': 'source_area', 'paths': ['../escape']}))))
                case('unimplemented_contract_refused', lambda: refused(lambda: queries.run(dict(payload, relations=['contract']))))
                impact_page = queries.run(dict(payload, limits={'max_edges': 1}))
                call_page = queries.run({'operation': 'call', 'limits': {'max_edges': 1}})
                changed = dict(blobs)
                changed[target['path']] = changed[target['path']].replace(b'value * 2', b'value * 3')
                removed = declarations['GO.helpers.Finish']['path']; del changed[removed]
                _adapter_materialize(source, changed, [removed])
                git('add', '--all'); git('commit', '-m', 'Synthetic body and deleted import target')
                updated = [dict(inventory[p], sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)) for p, raw in changed.items()]
                current = index.refresh(updated, git_base=base)
                rebuilt = StructuralIndex(source, directory / 'clean', budget=budget, limits=limits)
                clean = rebuilt.refresh(updated, git_base=base)
                modes.extend([{'phase': 'changed', 'receipt': current}, {'phase': 'clean', 'receipt': clean}])
                def parity():
                    current_facts, clean_facts = facts(index), facts(rebuilt)
                    current_imports, clean_imports = projection(index), projection(rebuilt)
                    observed({'current_facts': current_facts, 'clean_facts': clean_facts,
                              'current_imports': current_imports, 'clean_imports': clean_imports})
                    assert current['status'] == clean['status'] == 'ready', 'Changed or clean publication failed'
                    assert current['generation'] == clean['generation'] and current['source_identity'] == clean['source_identity']
                    assert current_facts == clean_facts and current_imports == clean_imports, 'Shared facts or import projection differ'
                    return {'generation': current['generation'], 'facts_sha256': hashlib.sha256(encoded(current_facts)).hexdigest(),
                            'import_projection_sha256': hashlib.sha256(encoded(current_imports)).hexdigest()}
                case('changed_clean_shared_projection_parity', parity)
                def git_selection():
                    response = observed(queries.run({'operation': 'impact', 'selector': {'kind': 'git_change', 'base_revision': base}}))
                    assert response['selection']['git_change']['current_revision'] == git('rev-parse', 'HEAD')
                    assert {r['path'] for r in response['selected_files']} == {target['path']}, 'Git changed-path selection differs'
                    assert response['unknown_boundaries'].get('historical_or_nonadmitted_source_path', 0) > 0
                    assert response['selection']['git_change']['source_byte_affinity'] == 'unobserved_worktree'
                    assert response['historical_call_closure'] == 'unavailable_current_index_only'
                    return response
                case('git_body_and_deleted_preimage_boundary', git_selection)
                case('changed_impact_cursor_refused', lambda: refused(lambda: queries.run(dict(payload,
                    cursor=impact_page['cursor'], limits={'max_edges': 1}))))
                def old_calls():
                    assert call_page['cursor'], 'Control failed to obtain a call cursor'
                    response = observed(queries.run({'operation': 'call', 'cursor': call_page['cursor'], 'limits': {'max_edges': 1}}))
                    assert response['generation'] == receipt['generation'], 'Pinned Calls snapshot was replaced'
                    return response
                case('ordinary_calls_keep_captured_snapshot', old_calls)
    with SourceRoot(root) as owner:
        after = {p: owner.read(p, 2 * 1024 * 1024, hash_full=True)[1] for p in paths}
    cases.append({'id': 'implementation_stable', 'status': 'passed' if before == after else 'failed'})
    failures = [r for r in cases if r['status'] != 'passed']
    return {'schema_version': 1, 'suite': 'impact-interface' if interfaces else 'impact', 'status': 'failed' if failures else 'passed',
        'source_identity': {'inputs': frozen, 'implementation': {'commit': revision, 'sha256': before},
            'question_ids': question_ids, 'question_core_sha256': hashlib.sha256(encoded(question_ids)).hexdigest(),
            'update_recipe': 'same-size Python helper body edit and deletion of frozen Go helper'},
        'case_results': cases, 'failures': failures, 'coverage_failures': [], 'modes': modes,
        'counts': {'checks': len(cases), 'passed': len(cases) - len(failures)}, 'environment': environment(),
        'qualification_complete': False, 'task_accepted': False, 'final_bundle_review_complete': False,
        'scope': 'Locked synthetic source-key reverse import/call queries and current-index update controls; '
                 'historical preimage closure, live-source affinity, real corpora, agent tasks and human UX unqualified.'}


def record_view(root, task, result, maximum):
    with SourceRoot(root) as source:
        report, _ = read_json(source, VIEWS_OUTPUT, maximum)
    if type(report) is not dict or report.get('schema_version') != 1 or 'T016' not in report.get('tasks', {}) or task not in ('T021', 'T043', 'T044', 'T045'):
        raise ValueError('Existing Calls proof and known view task required')
    report['tasks'][task] = result
    return write_result(root, VIEWS_OUTPUT, report, maximum)


def _incremental_original_impact(facts, expected, phase, fixture, sources):
    """Grade the original add/delete/rename/cycle judgments after production."""
    from evaluations.acceptance import _proof_require
    declarations = {row['id']: row for row in facts['definitions']}
    if 'case_id' not in expected:
        paths = expected['new_paths']
        _proof_require(expected['expected_relation'] == 'mutual_possible_calls' and
                       expected['execution_order_proven'] is False, 'Declared source cycle boundary')
        if phase == 'before':
            _proof_require(not any(row['path'] in paths for row in facts['definitions']), 'Cycle files absent before addition')
        else:
            for path in paths:
                _proof_require(any(site['path'] == path and site['role'] == 'call' and
                    site['certainty'] in ('resolved', 'candidate') and
                    site['targets_exhaustive'] is (site['certainty'] == 'resolved') and
                    any(declarations[target]['path'] in set(paths) - {path} for target in site['targets'])
                    for site in facts['sites']), 'Mutual inventoried source calls retained')
        return
    case = next(row for row in fixture['cases'] if row['id'] == expected['case_id'])
    matches = [row for row in facts['sites'] if row['path'] == case['path'] and
               row['range'] == case['range'] and row['role'] == case['role']]
    _proof_require(len(matches) == 1, 'Unique frozen impacted callsite')
    site = matches[0]
    _proof_require(site['text'] == case['text'] and
        site['provenance']['source_sha256'] == hashlib.sha256(sources[site['path']]).hexdigest() and
        site['certainty'] == expected['certainty_' + phase], 'Expected source identity and binding certainty')
    caller = next(row for row in fixture['definitions'] if row['id'] == case['caller'])
    _proof_require(declarations[site['caller']]['name'] == caller['name'], 'Impacted lexical caller')
    targets = [declarations[identity] for identity in site['targets']]
    if site['certainty'] == 'unresolved':
        _proof_require(not targets and site['reason'] and not site['targets_exhaustive'], 'Unresolved dependency survives')
    else:
        _proof_require(len(targets) == 1 and site['targets_exhaustive'], 'Unique supported source target')
        target = targets[0]
        if phase == 'after' and 'introduced_target_key' in expected:
            _, module, name = expected['introduced_target_key'].split('.', 2)
            _proof_require(PurePosixPath(target['path']).stem == module and target['name'] == name,
                           'Added frozen module/declaration target')
        else:
            frozen = next(row for row in fixture['definitions'] if row['id'] == case['targets'][0])
            path = expected.get('new_target_path', frozen['path']) if phase == 'after' else frozen['path']
            _proof_require(target['path'] == path and target['name'] == frozen['name'], 'Frozen moved/surviving target')
        raw = sources[target['path']]
        _proof_require(target['provenance']['source_sha256'] == hashlib.sha256(raw).hexdigest() and
            raw[target['range']['start_byte']:target['range']['end_byte']].decode() == target['text'],
            'Fresh physical target declaration')
    if phase == 'after' and 'removed_target_id' in expected:
        removed = next(row for row in fixture['definitions'] if row['id'] == expected['removed_target_id'])
        _proof_require(not any(row['path'] == removed['path'] for row in facts['definitions']), 'Deleted definitions removed')


def _incremental_physical_impact(facts, expected, fixture, sources):
    """Project the physical Go method token, preserving canonical logical names."""
    from evaluations.acceptance import _proof_impact, _proof_physical, _proof_require
    declarations = {row['id']: row for row in facts['definitions']}
    projected, names = {}, []
    records = [expected['caller_declaration'], *expected['target_declarations']]
    records.extend(expected[key] for key in ('surviving_physical_declaration', 'non_target_physical_declaration')
                   if key in expected)
    for record in records:
        actual = declarations[_proof_physical(record)]
        if actual['name'] == record['name']:
            continue
        raw = sources[record['path']]
        span, token = record['range'], record['name_range']
        _proof_require(actual['path'] == record['path'] and actual['language'] == 'go' and actual['kind'] == 'method' and
            actual['provenance']['syntax_kind'] == 'method_declaration' and actual['range'] == span and
            actual['text'] == record['text'] == raw[span['start_byte']:span['end_byte']].decode() and
            actual['provenance']['source_sha256'] == record['source_sha256'] == hashlib.sha256(raw).hexdigest(),
            'Physical Go method declaration before name projection')
        anchor = expected['site']
        sites = [site for site in facts['sites'] if site['path'] == anchor['path'] and site['range'] == anchor['range']]
        _proof_require(len(sites) == 1 and sites[0]['caller'] == _proof_physical(expected['caller_declaration']),
                       'Physical caller identity before name projection')
        frozen = [row for row in fixture['definitions'] if row['path'] == actual['path'] and row['range'] == span]
        _proof_require(len(frozen) == 1 and frozen[0]['kind'] == 'method' and frozen[0]['name'] == actual['name'] and
            span['start_byte'] <= token['start_byte'] < token['end_byte'] <= span['end_byte'] and
            actual['name'].rsplit('.', 1)[-1] == record['name'] == raw[token['start_byte']:token['end_byte']].decode(),
            'Frozen qualified method and exact physical name token')
        projected[actual['id']] = dict(actual, name=record['name'])
        names.append({'id': actual['id'], 'canonical_name': actual['name'], 'physical_name_token': record['name']})
    _proof_impact(dict(facts, definitions=[projected.get(row['id'], row) for row in facts['definitions']]), expected)
    return names


def incremental(root=ROOT, budget=None):
    """Run the locked36 source updates through the actual persisted owner."""
    from repo_graph.analysis import IndexLimits, StructuralIndex
    from evaluations.engine_checks import _adapter_error, _adapter_materialize, _adapter_operations
    from evaluations.supplement_preparation import LOCK, ORACLE, SOURCE, prepare_check
    from evaluations.acceptance import _proof_require
    root = Path(root)
    budget = budget or Budget(timeout_seconds=20)
    prepared = prepare_check(root)
    with SourceRoot(root) as owner:
        manifest, _ = read_json(owner, SOURCE)
        fixture, _ = read_json(owner, INPUTS + 'fixtures.json')
        oracle, _ = read_json(owner, ORACLE)
        lock, lock_sha = read_json(owner, LOCK)
    base = {row['path']: row['content_utf8'].encode() for row in manifest['files']}
    metadata = {row['path']: {key: row[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
                for row in manifest['files']}
    updates = fixture['updates'] + manifest['updates']
    _proof_require(len(updates) == 36 and len({row['id'] for row in updates}) == 36, 'All36 frozen updates required')
    code_paths = ('evaluations/analysis.py', 'evaluations/engine_checks.py', 'evaluations/acceptance.py',
        'evaluations/supplement_preparation.py', 'repo_graph/analysis.py', 'repo_graph/analysis_native.py',
        'repo_graph/analysis_queue.py', 'repo_graph/source.py', 'repo_graph/search.py', 'repo_graph/__init__.py',
        'tests/test_analysis.py', 'pyproject.toml', 'uv.lock')
    with SourceRoot(ROOT) as owner:
        before = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=20).strip()
    results = []
    with tempfile.TemporaryDirectory(prefix='repo-graph-incremental-') as scratch:
        for update in updates:
            attempts, produced, checks = {}, {}, []
            row = {'id': update['id'], 'language': update['language'],
                'category': update.get('category', update.get('kind')), 'attempts': attempts, 'checks': checks}
            try:
                # Only immutable source operations and metadata cross the owner boundary.
                changed, inventory = _adapter_operations(base, metadata,
                    {key: update[key] for key in ('id', 'language', 'operations')})
                directory = Path(scratch) / update['id']
                source = directory / 'source'
                source.mkdir(parents=True)
                _adapter_materialize(source, base)
                limits = IndexLimits(max_files=budget.max_files, max_source_bytes=budget.max_total_bytes,
                                     total_wall_seconds=budget.timeout_seconds)
                serial = StructuralIndex(source, directory / 'serial', budget=budget, limits=limits)
                queued = StructuralIndex(source, directory / 'queued', budget=budget, limits=limits)
                def attempt(name, index, records, mode, concurrency):
                    receipt = index.refresh(iter(records), mode=mode, concurrency=concurrency)
                    attempts[name] = receipt
                    digest = hashlib.sha256()
                    facts = {'definitions': [], 'sites': []}
                    if receipt['status'] == 'ready':
                        for kind in ('definitions', 'sites', 'imports', 'scopes', 'relationships'):
                            for fact in index.read_facts(kind):
                                digest.update(json.dumps([kind, fact], sort_keys=True, ensure_ascii=True,
                                                         separators=(',', ':')).encode() + b'\n')
                                if kind in facts:
                                    facts[kind].append(fact)
                        receipt['metadata'] = index.metadata()
                    receipt['normalized_facts_sha256'] = digest.hexdigest()
                    receipt['counts'] = {kind: len(values) for kind, values in facts.items()}
                    produced[name] = facts
                attempt('base_serial', serial, metadata.values(), 'serial', 1)
                attempt('base_queued', queued, metadata.values(), 'queued', 2)
                _adapter_materialize(source, changed, base.keys() - changed.keys())
                attempt('update_serial', serial, inventory.values(), 'serial', 1)
                attempt('update_queued', queued, inventory.values(), 'queued', 2)
                attempt('clean_serial', StructuralIndex(source, directory / 'clean', budget=budget, limits=limits),
                        inventory.values(), 'serial', 1)
                ready = all(value['status'] == 'ready' for value in attempts.values())
                checks.append({'id': update['id'] + ':ready_attempts', 'status': 'passed' if ready else 'failed'})
                for key in ('generation', 'source_identity', 'normalized_facts_sha256'):
                    checks.append({'id': update['id'] + ':' + key + '_parity', 'status': 'passed' if ready and
                        attempts['base_serial'][key] == attempts['base_queued'][key] and
                        attempts['update_serial'][key] == attempts['update_queued'][key] == attempts['clean_serial'][key]
                        else 'failed'})
                checks.append({'id': update['id'] + ':fresh_generation', 'status': 'passed' if ready and
                    attempts['base_serial']['generation'] != attempts['update_serial']['generation'] and
                    attempts['base_serial']['source_identity'] != attempts['update_serial']['source_identity'] else 'failed'})
                dirty = sum(record['kind'] == 'source' and record != metadata.get(path) for path, record in inventory.items())
                for name in ('update_serial', 'update_queued'):
                    resources = attempts[name]['resources']
                    checks.append({'id': update['id'] + ':' + name + ':collection_reuse',
                        'status': 'passed' if ready and resources['changed_files_collected'] == dirty and
                            resources['unchanged_source_collections_reused'] == sum(r['kind'] == 'source' for r in inventory.values()) - dirty
                            else 'failed'})
                mutation = next((item for item in oracle['mutations'] if item['id'] == update['id']), None)
                impacts = mutation['expected_source_bound_impacts'] if mutation else update['expected_impacts']
                for phase, stages, blobs in (('before', ('base_serial', 'base_queued'), base),
                                            ('after', ('update_serial', 'update_queued', 'clean_serial'), changed)):
                    for stage in stages:
                        for position, expected in enumerate(impacts):
                            check = {'id': update['id'] + ':' + stage + ':impact:' + str(position), 'status': 'passed'}
                            try:
                                _proof_require(attempts[stage]['status'] == 'ready', 'Ready facts required before impact grading')
                                if mutation is None:
                                    _incremental_original_impact(produced[stage], expected, phase, fixture, blobs)
                                elif 'site' in expected[phase]:
                                    names = _incremental_physical_impact(produced[stage], expected[phase], fixture, blobs)
                                    if names:
                                        check['physical_method_name_projection'] = names
                                else:
                                    physical = expected[phase]
                                    raw = blobs[physical['path']]
                                    start, end = physical['range']['start_byte'], physical['range']['end_byte']
                                    _proof_require(hashlib.sha256(raw).hexdigest() == physical['source_sha256'] and
                                        raw[start:end].decode() == physical['text'], 'Frozen physical annotation/contract bytes')
                                    if mutation['category'] == 'contract':
                                        _proof_require(not any(fact['path'] == physical['path'] for values in produced[stage].values()
                                                               for fact in values), 'No invented contract semantic facts')
                                        check['coverage'] = expected['required_coverage']
                            except Exception as error:
                                check.update(status='failed', error_kind=type(error).__name__, reason=str(error))
                            checks.append(check)
            except Exception as error:
                checks.append({'id': update['id'] + ':producer', 'status': 'failed',
                               'error_kind': type(error).__name__, 'reason': 'Unexpected update producer failure'})
                print(json.dumps({'id': update['id'], **_adapter_error(error)}), file=sys.stderr)
            row['status'] = 'passed' if checks and all(check['status'] == 'passed' for check in checks) else 'failed'
            results.append(row)
    with SourceRoot(ROOT) as owner:
        after = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    checks = [check for row in results for check in row['checks']]
    checks.append({'id': 'implementation_stable', 'status': 'passed' if before == after else 'failed'})
    failures = [check for check in checks if check['status'] != 'passed']
    return {'schema_version': 1, 'suite': 'incremental', 'status': 'failed' if failures else 'passed',
        'source_identity': {'supplement_lock_sha256': lock_sha, 'locked_inputs_sha256': lock['sha256'],
            'implementation': {'commit': revision, 'sha256': before}},
        'implementation_after': {'commit': revision, 'sha256': after}, 'preparation': prepared,
        'case_results': results, 'failures': failures, 'coverage_failures': [],
        'counts': {'updates': len(results), 'passed_updates': sum(row['status'] == 'passed' for row in results),
                   'checks': len(checks), 'passed': len(checks) - len(failures)},
        'environment': environment(), 'qualification_complete': False, 'limits_qualified': False,
        'scope': 'Frozen36 synthetic updates, persisted serial1/queued2 versus independent clean output on the same source owner; '
                 'expected impacts graded after production; type flow, contract semantics, corpus, scale and human qualification unmeasured'}


def coverage(root=ROOT, budget=None, evidence_directory=None):
    """Grade captured status without another source scanner or model backend."""
    from collections import Counter
    from contextlib import ExitStack, closing, redirect_stdout
    from dataclasses import replace
    import io
    import threading
    from unittest.mock import patch
    from urllib.request import urlopen
    from repo_graph import builder, search, source as source_module
    from repo_graph.analysis import IndexLimits, StructuralIndex
    from repo_graph.cli import main as cli_main
    from repo_graph.server import create_server
    from repo_graph import analysis_native as native
    from evaluations.engine_checks import _adapter_error, _adapter_materialize
    from evaluations.supplement_preparation import LOCK, SOURCE, prepare_check
    root, budget = Path(root), budget or Budget()
    fixture, frozen_identity = frozen_inputs(root)
    prepared = prepare_check(root)
    inventory = [{key: row[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
                 for row in fixture['files']]
    with SourceRoot(root) as owner:
        original = {row['path']: owner.read(row['path'], budget.max_file_bytes, hash_full=True)[0]
                    for row in inventory}
        supplement, _ = read_json(owner, SOURCE)
        _, supplement_lock_sha = read_json(owner, LOCK)
    supplemental_inventory = [{key: row[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
                              for row in supplement['files']]
    supplemental_sources = {row['path']: row['content_utf8'].encode() for row in supplement['files']}
    controls, control_sources = {}, {}
    for name, record in COVERAGE_CONTROLS.items():
        raw = record['content_utf8'].encode()
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('Coverage control identity changed: ' + name)
        controls[name] = {key: record[key] for key in ('path', 'language', 'kind', 'sha256')}
        controls[name].update(bytes=len(raw), origin=record['origin'])
        control_sources[name] = raw
    code_paths = ('evaluations/analysis.py', 'evaluations/engine_checks.py',
        'evaluations/supplement_preparation.py', 'repo_graph/analysis.py', 'repo_graph/analysis_native.py',
        'repo_graph/analysis_queue.py', 'repo_graph/search.py', 'repo_graph/source.py',
        'repo_graph/cli.py', 'repo_graph/server.py', 'repo_graph/analysis_queries.py', 'repo_graph/__init__.py',
        'tests/test_analysis.py', 'tests/test_search.py', 'pyproject.toml', 'uv.lock')
    with SourceRoot(ROOT) as owner:
        before = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=20).strip()
    cases, coverage_failures, modes = [], [], []
    if evidence_directory is not None:
        evidence_directory = Path(evidence_directory).resolve()
        if evidence_directory == root or root in evidence_directory.parents:
            raise ValueError('Coverage receipts must remain outside the source checkout')
        evidence_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = Path(tempfile.mkdtemp(prefix='coverage-', dir=evidence_directory))

    def retain_response(name, raw):
        (logs / name).write_bytes(raw)
        return {'name': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}

    def captured_components(status):
        # Backend observations and request timing may differ across entry points.
        result = {key: value for key, value in status.items() if key not in ('storage', 'semantic', 'rerankers')}
        for name in ('semantic_index', 'function_evidence'):
            result[name] = {key: value for key, value in result[name].items()
                            if key not in ('backend_available', 'backend_model')}
        return result

    class FakeEmbedding:
        name = 'synthetic'
        packed = staticmethod(lambda vector: vector)
        def passages(self, texts):
            return [b'fresh-vector' for _ in texts]

    with tempfile.TemporaryDirectory(prefix='repo-graph-coverage-') as temporary:
        directory = Path(temporary)
        source = directory / 'source'
        source.mkdir()
        original_read = SourceRoot.read

        def observe(index, entry, *, expected_source=None, backend_available=False, endpoints=False):
            def guarded_read(boundary, *args, **kwargs):
                if boundary.root == index.root:
                    raise AssertionError('Status must not read source content')
                return original_read(boundary, *args, **kwargs)
            forbidden = AssertionError('Status must use captured storage without source, Git or backend work')
            with ExitStack() as stack:
                for module, attribute in ((builder, 'repo_files'), (native, 'collect_file'),
                        (native, 'extract'), (search, 'catalog'), (search, 'embed_index'),
                        (subprocess, 'check_output'), (subprocess, 'run')):
                    stack.enter_context(patch.object(module, attribute, side_effect=forbidden))
                stack.enter_context(patch.object(SourceRoot, 'read', guarded_read))
                stack.enter_context(patch.object(search.Embeddings, '__init__', side_effect=forbidden))
                begun = time.monotonic()
                status = search.index_status(index.output, owner=index.output_owner,
                    expected_source=expected_source, backend_available=backend_available)
                name = entry['id'].replace(':', '-') + '-%02d' % len(entry.get('observations', []))
                raw_status = json.dumps(status, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode() + b'\n'
                observation = {'captured': {key: value for key, value in status.items() if key != 'function_evidence'},
                    'retained_response': retain_response(name + '-status.json', raw_status),
                    'elapsed_seconds': time.monotonic() - begun,
                    'serialized_bytes': len(json.dumps(status).encode()), 'source_git_backend_calls': 0}
                entry.setdefault('observations', []).append(observation)
                assert status['status'] == 'ok', status
                assert status['storage']['deadline_seconds'] == .5
                if endpoints:
                    output = io.StringIO()
                    command = ['status', str(index.output)]
                    if expected_source is not None:
                        command += ['--expect-source', expected_source]
                    with redirect_stdout(output):
                        command_exit = cli_main(command)
                    command_raw = output.getvalue().encode()
                    retain_response(name + '-cli.json', command_raw)
                    assert command_exit == 0
                    command_status = json.loads(command_raw)
                    component_match = captured_components(command_status) == captured_components(status)
                    observation['cli'] = {'response_sha256': hashlib.sha256(command_raw).hexdigest(),
                        'response_bytes': len(command_raw), 'storage': command_status['storage'],
                        'backend_available': command_status['semantic_index']['backend_available'],
                        'captured_components_match': component_match}
                    assert component_match
                    if expected_source is None:
                        with create_server(search.Search(index.output)) as server:
                            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
                            thread.start()
                            try:
                                with urlopen('http://127.0.0.1:' + str(server.server_port) + '/api/status', timeout=2) as response:
                                    endpoint_raw = response.read(262145)
                                    retain_response(name + '-server.json', endpoint_raw)
                                    endpoint = json.loads(endpoint_raw)
                                component_match = captured_components(endpoint) == captured_components(status)
                                observation['server'] = {'response_sha256': hashlib.sha256(endpoint_raw).hexdigest(),
                                    'response_bytes': len(endpoint_raw), 'storage': endpoint['storage'],
                                    'backend_available': endpoint['semantic_index']['backend_available'],
                                    'captured_components_match': component_match,
                                    'semantic': endpoint['semantic'], 'rerankers': endpoint['rerankers']}
                                assert component_match
                                assert endpoint['semantic'] is False and endpoint['rerankers'] == ['none']
                            finally:
                                server.shutdown(); thread.join()
                return status

        def refresh(index, entries, entry, **kwargs):
            receipt = index.refresh(entries, mode=entry['execution_mode'], concurrency=entry['concurrency'], **kwargs)
            entry.setdefault('attempts', []).append(receipt)
            return receipt

        def ready(index, entries, entry):
            receipt = refresh(index, entries, entry)
            assert receipt['status'] == 'ready' and receipt['published'], receipt
            return receipt

        def published(index, entry, expected_counts):
            status = observe(index, entry, endpoints=True)
            component = status['structural']
            assert component['state'] == 'ready' and component['artifact_ready'] and component['query_available']
            assert component['freshness'] == 'unknown'
            receipt = component['receipt']
            assert receipt['coverage']['status_counts'] == expected_counts
            assert receipt['coverage']['file_status'] == expected_counts
            assert sum(expected_counts.values()) == receipt['coverage']['files_total']
            assert receipt['generation'] == component['identities']['generation']
            assert receipt['source_identity'] == component['identities']['source_identity']
            assert receipt['repository_identity'] == index.owner
            with closing(search.connect(index.output, readonly=True, owner=index.output_owner)) as db:
                files = [{'path': row['path'], 'status': row['status'], 'record': json.loads(row['record']),
                          'errors': json.loads(row['ir'])['errors'] if row['ir'] else []}
                         for row in db.execute('SELECT path,status,record,ir FROM structural_files ORDER BY path')]
            assert dict(Counter(row['status'] for row in files)) == expected_counts
            language_counts = {}
            for row in files:
                language = language_counts.setdefault(row['record']['language'], {'files_total': 0, 'file_status': {}})
                language['files_total'] += 1
                language['file_status'][row['status']] = language['file_status'].get(row['status'], 0) + 1
            assert receipt['coverage']['by_language'] == language_counts
            assert receipt['coverage']['language_overflow'] == {'languages': 0, 'files_total': 0, 'file_status': {}}
            assert receipt['coverage']['inventory_scope'] == 'caller_admitted_inventory'
            assert receipt['coverage']['discovery_skipped_files'] is None
            assert receipt['coverage']['discovery_skip_knowledge'] == 'outside_admitted_inventory_not_measured'
            errors = [{'path': row['path'], 'kind': error['kind'], 'range': error['range']}
                      for row in files for error in row['errors']]
            assert receipt['coverage']['parser_error_count'] == len(errors)
            assert receipt['coverage']['parser_error_samples'] == errors
            assert receipt['coverage']['parser_error_samples_truncated'] is False
            for error in errors:
                file = next(row for row in files if row['path'] == error['path'])
                assert 0 <= error['range']['start_byte'] <= error['range']['end_byte'] <= file['record']['bytes']
                assert 1 <= error['range']['start_line'] <= error['range']['end_line']
            produced = {kind: list(index.read_facts(kind)) for kind in
                        ('definitions', 'sites', 'imports', 'scopes', 'relationships')}
            site_counts = {}
            for row in produced['sites']:
                role = site_counts.setdefault(row['role'], {})
                role[row['certainty']] = role.get(row['certainty'], 0) + 1
            assert receipt['coverage']['sites_by_role_certainty'] == site_counts
            assert receipt['versions'] == {'schema': 'structural-v2', 'rules': native.RULE_VERSION, 'grammars': native.PINS}
            assert receipt['revision_dirty']['content_identity'] == receipt['source_identity']
            if receipt['revision_dirty']['knowledge'] == 'unknown':
                assert receipt['revision_dirty']['revision'] is None and receipt['revision_dirty']['dirty'] is None
                assert receipt['revision_dirty']['reason']
            elif receipt['revision_dirty']['knowledge'] == 'captured_revision':
                assert receipt['revision_dirty']['dirty'] is None
                assert receipt['revision_dirty']['reason'] == 'git_dirty_not_observed_without_project_commands'
                assert receipt['revision_dirty']['dirty_basis'] == 'unobserved_repository_configured_status'
            entry['persisted_file_inventory'] = files
            entry['persisted_facts_sha256'] = hashlib.sha256(json.dumps(produced,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return status

        for mode, concurrency in (('serial', 1), ('queued', 2)):
            label = mode + str(concurrency)
            report = {'mode': mode, 'concurrency': concurrency, 'cases': []}
            modes.append(report)
            for number in range(1, 9):
                case = {'id': label + ':T013-P%02d' % number, 'group': 'T013-P%02d' % number,
                    'execution_mode': mode, 'concurrency': concurrency, 'status': 'running',
                    'attempts': [], 'observations': []}
                cases.append(case); report['cases'].append(case['id'])
                begun = time.monotonic()
                try:
                    index = StructuralIndex(source, directory / (label + '-' + str(number)), budget=budget,
                        limits=IndexLimits(total_wall_seconds=budget.timeout_seconds))
                    if number == 1:
                        absent = observe(index, case, endpoints=True)
                        assert absent['structural']['state'] == 'not_scanned'
                        assert not absent['structural']['artifact_ready'] and absent['structural']['receipt'] is None
                        assert not (index.output / 'search.db').exists()
                        ready(index, [], case)
                        empty = published(index, case, {})
                        assert empty['structural']['receipt']['coverage']['files_total'] == 0
                        case['coverage_denominator'] = {'admitted_files': 0, 'construct_cases': 0}
                    elif number == 2:
                        _adapter_materialize(source, original)
                        ready(index, inventory, case)
                        status = published(index, case, {'parsed': 8, 'configuration': 1})
                        assert {row['record']['language'] for row in case['persisted_file_inventory']} == set(LANGUAGES)
                        produced = {kind: list(index.read_facts(kind)) for kind in ('definitions', 'sites')}
                        definitions, selected = grade(produced, fixture)
                        assert all(row['status'] == 'passed' for row in definitions + selected)
                        unknowns = [row for row in selected if row['expected_certainty'] != 'resolved']
                        assert len(unknowns) == 16
                        assert all(row['actual'][0]['reason'] and not row['actual'][0]['targets_exhaustive'] for row in unknowns)
                        coverage_failures.extend({'id': label + ':' + row['id'], 'group': case['group'],
                            'dimension': 'receiver_target_enumeration', **row['target_enumeration']}
                            for row in selected if row.get('target_enumeration', {}).get('status') == 'failed')
                        case['selected_unknown_cases'] = [{'id': row['id'], 'language': row['language'],
                            'certainty': row['actual'][0]['certainty'], 'reason': row['actual'][0]['reason'],
                            'targets_exhaustive': row['actual'][0]['targets_exhaustive']} for row in unknowns]
                        case['coverage_denominator'] = {'original_files': 9, 'selected_definitions': 75, 'selected_sites': 44}
                        _adapter_materialize(source, supplemental_sources)
                        ready(index, supplemental_inventory, case)
                        published(index, case, {'parsed': 16, 'configuration': 8})
                        case['coverage_denominator']['supplemental_files'] = 24
                    elif number == 3:
                        _adapter_materialize(source, {'partial.py': control_sources['partial']})
                        ready(index, [{key: value for key, value in controls['partial'].items() if key != 'origin'}], case)
                        published(index, case, {'partial_parse': 1})
                        calls = [row for row in index.read_facts('sites') if row['text'] == 'local()']
                        assert len(calls) == 1 and calls[0]['certainty'] == 'unresolved'
                        assert not calls[0]['targets'] and not calls[0]['targets_exhaustive'] and calls[0]['reason']
                        case['retained_partial_site'] = calls[0]
                        case['coverage_denominator'] = {'frozen_partial_files': 0, 'identified_control_files': 1}
                    elif number == 4:
                        _adapter_materialize(source, {'main.py': control_sources['ready'], 'other.rs': control_sources['unsupported']})
                        ready(index, [{key: value for key, value in controls[name].items() if key != 'origin'}
                                      for name in ('ready', 'unsupported')], case)
                        published(index, case, {'parsed': 1, 'unsupported_language': 1})
                        assert not any(row['path'] == 'other.rs' for row in index.read_facts('definitions'))
                        _adapter_materialize(source, original)
                        index = StructuralIndex(source, index.output, budget=replace(budget, max_file_bytes=1261))
                        ready(index, inventory, case)
                        published(index, case, {'parsed': 7, 'configuration': 1, 'excluded_size': 1})
                        assert [row['path'] for row in case['persisted_file_inventory'] if row['status'] == 'excluded_size'] == [
                            row['path'] for row in inventory if row['bytes'] > 1261]
                        _adapter_materialize(source, {'truncated.py': control_sources['truncated']})
                        keyword = search.catalog(source, ['truncated.py', 'missing.py'], index.output)
                        case['catalog_attempts'] = [keyword]
                        keyword_status = observe(index, case, endpoints=True)
                        catalog_receipt = keyword_status['semantic_index']['catalog_receipt']
                        assert catalog_receipt['truncated'] == 1 and catalog_receipt['failed'] == 1
                        assert catalog_receipt['failures'][0]['path'] == 'missing.py'
                        assert catalog_receipt['documents'] == 1
                        assert keyword_status['structural']['artifact_ready']
                        case['coverage_denominator'] = {'frozen_unsupported_files': 0, 'identified_unsupported_controls': 1,
                            'alternate_size_limit_inventory': 9, 'discovery_skipped_paths': None,
                            'identified_keyword_truncation_files': 1, 'identified_keyword_read_failures': 1,
                            'discovery_scope': 'Explicit admitted inventory; undiscovered filesystem paths are unmeasured'}
                    elif number == 5:
                        _adapter_materialize(source, {'main.py': control_sources['ready']})
                        previous = ready(index, ['main.py'], case)
                        before_artifact = (index.output / 'search.db').read_bytes()
                        seen = []
                        def interrupt(boundary, path, *args, **kwargs):
                            if boundary.root == source and path == 'main.py':
                                observed = observe(index, case, endpoints=True)
                                assert observed['structural']['state'] == 'updating'
                                assert observed['structural']['artifact_ready']
                                assert observed['structural']['identities']['generation'] == previous['generation']
                                seen.append(True)
                                raise InterruptedError('Source read cancelled')
                            return original_read(boundary, path, *args, **kwargs)
                        with patch.object(SourceRoot, 'read', interrupt):
                            failed = refresh(index, ['main.py', 'unread.py'], case)
                        assert seen and failed['status'] == 'interrupted' and not failed['published']
                        assert failed['remaining_inventory_status'] == 'not_evaluated_after_failure'
                        assert failed['published_coverage_generation'] == previous['generation']
                        assert failed['path'] == 'main.py' and failed['resources']['inventory_entries_consumed'] == 1
                        assert failed['collection_failures'] == []
                        assert (index.output / 'search.db').read_bytes() == before_artifact
                        reopened = StructuralIndex(source, index.output)
                        observed = observe(reopened, case, endpoints=True)
                        assert observed['structural']['state'] == 'interrupted' and observed['structural']['artifact_ready']
                        assert observed['structural']['last_attempt']['status'] == 'interrupted'
                        assert observed['structural']['identities']['generation'] == previous['generation']
                        case['coverage_denominator'] = {'declared_attempt_files': 2, 'remaining_files': 'not_evaluated_after_failure'}
                    elif number == 6:
                        _adapter_materialize(source, {'main.py': control_sources['ready']})
                        previous = ready(index, ['main.py'], case)
                        same = observe(index, case, expected_source=previous['source_identity'], endpoints=True)
                        assert same['structural']['freshness'] == 'current'
                        stale = observe(index, case, expected_source='0' * 64, endpoints=True)
                        assert stale['structural']['state'] == 'stale' and stale['structural']['artifact_ready']
                        _adapter_materialize(source, {'main.py': control_sources['dirty']})
                        unseen = observe(index, case)
                        assert unseen['structural']['freshness'] == 'unknown'
                        original_fsync, injected = source_module.os.fsync, [False]
                        def fail_directory(fd):
                            info = os.fstat(fd)
                            output = index.output.stat()
                            if not injected[0] and stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (output.st_dev, output.st_ino):
                                if (index.output / 'search.db').read_bytes() != before_artifact:
                                    injected[0] = True
                                    raise OSError('synthetic directory synchronization failure')
                            return original_fsync(fd)
                        before_artifact = (index.output / 'search.db').read_bytes()
                        with patch.object(source_module.os, 'fsync', fail_directory):
                            uncertain = refresh(index, ['main.py'], case)
                        assert injected[0] and uncertain['published'] and uncertain['status'] == 'publication_uncertain'
                        assert uncertain['generation'] != previous['generation'] and uncertain['durability'] == 'unconfirmed'
                        actual = observe(index, case, endpoints=True)
                        assert actual['structural']['identities']['generation'] == uncertain['generation']
                        assert actual['structural']['last_attempt']['status'] == 'publication_uncertain'
                        assert actual['structural']['artifact_ready']
                        definitions = list(index.read_facts('definitions'))
                        assert len(definitions) == 1 and definitions[0]['text'] == control_sources['dirty'].decode().strip()
                        assert definitions[0]['provenance']['source_sha256'] == controls['dirty']['sha256']
                        case['coverage_denominator'] = {'expected_source_checks': 2, 'observed_publication_failures': 1,
                            'live_source_watchers': 0}
                    elif number == 7:
                        git_source = directory / ('git-' + label)
                        git_source.mkdir()
                        _adapter_materialize(git_source, {'main.py': control_sources['ready']})
                        git = ['git', '-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
                            '-c', 'commit.gpgSign=false', '-c', 'core.hooksPath=/dev/null', '-c', 'init.templateDir=']
                        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                            GIT_CONFIG_COUNT='0', GIT_AUTHOR_NAME='Synthetic Fixture',
                            GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_NAME='Synthetic Fixture',
                            GIT_COMMITTER_EMAIL='fixture@example.invalid', GIT_TERMINAL_PROMPT='0',
                            GIT_AUTHOR_DATE='2000-01-01T00:00:00+0000', GIT_COMMITTER_DATE='2000-01-01T00:00:00+0000')
                        for args in (['init', '-q'], ['add', '--', 'main.py'], ['commit', '-q', '-m', 'Synthetic coverage fixture']):
                            subprocess.run(git + args, cwd=git_source, env=env, check=True, capture_output=True, timeout=5)
                        frozen_revision = subprocess.check_output(git + ['rev-parse', 'HEAD'], cwd=git_source, env=env, text=True, timeout=5).strip()
                        case['synthetic_git_identity_before_comparison'] = {'revision': frozen_revision,
                            'clean_source_sha256': controls['ready']['sha256'], 'dirty_source_sha256': controls['dirty']['sha256']}
                        index = StructuralIndex(git_source, index.output, budget=budget)
                        first = ready(index, ['main.py'], case)
                        initial_capture = published(index, case, {'parsed': 1})['structural']['receipt']['revision_dirty']
                        assert initial_capture['revision'] == frozen_revision and initial_capture['dirty'] is None
                        assert initial_capture['knowledge'] == 'captured_revision'
                        _adapter_materialize(git_source, {'main.py': control_sources['dirty']})
                        changed = ready(index, ['main.py'], case)
                        updated_capture = published(index, case, {'parsed': 1})['structural']['receipt']['revision_dirty']
                        assert updated_capture['revision'] == frozen_revision and updated_capture['dirty'] is None
                        assert updated_capture['knowledge'] == 'captured_revision'
                        assert updated_capture['content_identity'] != initial_capture['content_identity']
                        assert changed['source_identity'] != first['source_identity']
                        case['coverage_denominator'] = {'synthetic_revision_content_update_pairs': 1,
                            'git_status_boolean_cases': 0, 'product_git_commits': 0}
                    elif number == 8:
                        _adapter_materialize(source, {'main.py': control_sources['ready']})
                        structural = ready(index, ['main.py'], case)
                        catalog = search.catalog(source, ['main.py'], index.output)
                        case['catalog_attempts'] = [catalog]
                        initial = observe(index, case, endpoints=True)
                        assert initial['semantic_index']['state'] == 'not_indexed'
                        assert not initial['semantic_index']['artifact_ready']
                        case['embedding_attempts'] = [search.embed_index(index.output, FakeEmbedding())]
                        available = observe(index, case, backend_available=True)
                        unavailable = observe(index, case, endpoints=True)
                        assert available['semantic_index']['artifact_ready'] and available['semantic_index']['query_available']
                        assert unavailable['semantic_index']['artifact_ready'] and not unavailable['semantic_index']['query_available']
                        assert available['semantic_index']['generation_basis'] == 'keyword-docs-v2'
                        assert available['semantic_index']['structural_generation_affinity'] == 'unknown'
                        assert available['semantic_index']['identities']['generation'] == catalog['generation']
                        assert available['structural']['identities']['generation'] == structural['generation']
                        _adapter_materialize(source, {'extra.py': control_sources['extra']})
                        case['catalog_attempts'].append(search.catalog(source, ['main.py', 'extra.py'], index.output))
                        stale = observe(index, case, endpoints=True)
                        assert stale['semantic_index']['state'] == 'stale' and not stale['semantic_index']['artifact_ready']
                        class InterruptedEmbedding(FakeEmbedding):
                            def passages(self, texts):
                                updating = observe(index, case, endpoints=True)
                                assert updating['semantic_index']['state'] == 'updating'
                                assert updating['structural']['state'] == 'ready' and updating['structural']['artifact_ready']
                                raise InterruptedError('synthetic embedding interruption')
                        try:
                            search.embed_index(index.output, InterruptedEmbedding())
                        except InterruptedError:
                            pass
                        else:
                            raise AssertionError('Interrupted embedding unexpectedly completed')
                        failed = observe(index, case, endpoints=True)
                        assert failed['semantic_index']['state'] == 'interrupted'
                        assert failed['structural']['identities']['generation'] == structural['generation']
                        with closing(search.connect(index.output, readonly=True, owner=index.output_owner)) as db:
                            vectors = dict(db.execute('SELECT path,vector FROM docs'))
                        assert vectors['main.py'] == b'fresh-vector' and vectors['extra.py'] is None
                        case['embedding_attempts'].append(search.embed_index(index.output, FakeEmbedding()))
                        complete = observe(index, case, endpoints=True)
                        assert complete['semantic_index']['artifact_ready'] and complete['semantic_index']['state'] == 'ready'
                        assert complete['semantic_index']['receipt']['documents'] == 2
                        assert complete['semantic_index']['receipt']['missing_vectors'] == 0
                        assert complete['structural']['identities']['generation'] == structural['generation']
                        case['coverage_denominator'] = {'identified_fake_embedding_states': 5, 'real_model_quality_cases': 0}
                    case['status'] = 'passed'
                except Exception as error:
                    case.update(status='failed', error_kind=type(error).__name__, reason='Captured coverage/readiness assertion failed')
                    print(json.dumps({'id': case['id'], **_adapter_error(error)}), file=sys.stderr)
                case['elapsed_seconds'] = time.monotonic() - begun
    with SourceRoot(ROOT) as owner:
        after = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    paired = []
    for group in ('T013-P01', 'T013-P02', 'T013-P03', 'T013-P04', 'T013-P05', 'T013-P06', 'T013-P08'):
        selected = [row for row in cases if row['group'] == group]
        def captured(row):
            if row['status'] != 'passed' or not row['observations']:
                return None
            structural = row['observations'][-1]['captured']['structural']
            return {'identities': structural['identities'], 'artifact_ready': structural['artifact_ready'],
                'coverage': structural['receipt']['coverage'], 'versions': structural['receipt']['versions'],
                'normalized_facts_sha256': row.get('persisted_facts_sha256')}
        values = [captured(row) for row in selected]
        paired.append({'group': group, 'status': 'passed' if len(values) == 2 and values[0] is not None and
                       values[0] == values[1] else 'failed'})
    checks = [{'id': 'serial_queued_captured_status_parity',
        'status': 'passed' if all(row['status'] == 'passed' for row in paired) else 'failed', 'groups': paired,
        'git_pair_scope': 'Separate synthetic Git source owners are graded against their own frozen revision'},
        {'id': 'implementation_stable', 'status': 'passed' if before == after else 'failed'}]
    failures = [row for row in cases + checks if row['status'] != 'passed']
    return {'schema_version': 1, 'suite': 'coverage', 'status': 'failed' if failures else 'passed',
        'source_identity': {'inputs': frozen_identity, 'supplement_lock_sha256': supplement_lock_sha,
            'original_files': {row['path']: row['sha256'] for row in inventory},
            'supplemental_files': {row['path']: row['sha256'] for row in supplemental_inventory},
            'identified_controls': controls, 'fake_backend_identity': {'model': FakeEmbedding.name,
                'vector_sha256': hashlib.sha256(b'fresh-vector').hexdigest(), 'real_backend': False},
            'implementation': {'commit': revision, 'sha256': before}},
        'implementation_after': {'commit': revision, 'sha256': after}, 'preparation': prepared,
        'case_results': cases, 'checks': checks, 'failures': failures, 'coverage_failures': coverage_failures,
        'modes': modes, 'counts': {'groups': 8, 'checks': len(cases) + len(checks),
            'passed': len(cases) + len(checks) - len(failures), 'receiver_enumeration_failures': len(coverage_failures)},
        'zero_case_scope': {'frozen_partial_files': 0, 'frozen_unsupported_files': 0,
            'frozen_default_size_exclusions': 0, 'source_watchers': 0, 'real_model_quality_cases': 0,
            'git_status_boolean_cases': 0, 'framework_cases': 0, 'human_cases': 0, 'scale_cases': 0},
        'environment': environment(), 'qualification_complete': False, 'limits_qualified': False,
        'scope': 'Eight captured coverage/readiness groups in serial1 and queued2; existing locked sources '
                 'and separately identified state/storage controls; status does not scan source/Git/models; '
                 'full responses retained privately with hashes and whole captured endpoint comparisons; '
                 'additive function status is omitted from this T013 summary and is owned by T014; '
                 'receiver enumeration misses and unmeasured discovery/model/platform/scale/human scope remain explicit'}


def queries(root=ROOT, budget=None):
    """Grade locked query questions against captured, persisted SQL snapshots."""
    from dataclasses import asdict, replace
    from unittest.mock import patch
    from repo_graph.analysis import IndexLimits, StructuralIndex
    from repo_graph.analysis_queries import SQLSnapshot, Limits, encoded
    from repo_graph import analysis_queries
    from evaluations.engine_checks import _adapter_error, _adapter_materialize
    from evaluations.supplement_preparation import LOCK, ORACLE, SOURCE, prepare_check
    root, budget = Path(root), budget or Budget(timeout_seconds=20)
    prepared = prepare_check(root)
    with SourceRoot(root) as owner:
        manifest, _ = read_json(owner, SOURCE)
        lock, lock_sha = read_json(owner, LOCK)
    record = next(row for row in manifest['files'] if row['path'].endswith('/python/fanout.py'))
    source_path, raw = record['path'], record['content_utf8'].encode('utf-8')
    metadata = {key: record[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
    code_paths = ('evaluations/analysis.py', 'evaluations/engine_checks.py',
        'evaluations/supplement_preparation.py', 'evaluations/bounded_queries.py',
        'repo_graph/analysis.py', 'repo_graph/analysis_native.py', 'repo_graph/analysis_queue.py',
        'repo_graph/analysis_queries.py', 'repo_graph/cli.py', 'repo_graph/server.py',
        'repo_graph/search.py', 'repo_graph/source.py',
        'repo_graph/__init__.py', 'tests/test_analysis.py', 'pyproject.toml', 'uv.lock')
    with SourceRoot(ROOT) as owner:
        before = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=20).strip()
    modes, results, checks, observed = [], [], [], []

    def physical(item):
        span = item['range']
        return (item['path'], span['start_byte'], span['end_byte'], span['start_line'], span['end_line'])

    def row_key(row):
        return (physical(row['site']), row['caller']['id'] if row['caller'] else None,
                row['target']['id'] if row['target'] else None)

    def no_bodies(value):
        if isinstance(value, dict):
            assert not {'text', 'content', 'excerpt', 'body', 'content_utf8'} & set(value)
            for child in value.values():
                no_bodies(child)
        elif isinstance(value, list):
            for child in value:
                no_bodies(child)

    def denied(action):
        try:
            action()
        except ValueError:
            return True
        raise AssertionError('Changed or expired cursor accepted')

    with tempfile.TemporaryDirectory(prefix='repo-graph-queries-') as temporary:
        directory = Path(temporary)
        source = directory / 'source'
        source.mkdir()
        for mode, concurrency in (('serial', 1), ('queued', 2)):
            label = mode + str(concurrency)
            _adapter_materialize(source, {source_path: raw})
            index = StructuralIndex(source, directory / mode, budget=budget,
                limits=IndexLimits(max_files=budget.max_files, max_source_bytes=budget.max_total_bytes,
                                   total_wall_seconds=budget.timeout_seconds))
            receipt = index.refresh([metadata], mode=mode, concurrency=concurrency)
            report = {'mode': mode, 'concurrency': concurrency, 'index': receipt, 'queries': []}
            modes.append(report)
            assert receipt['status'] == 'ready', receipt
            produced = {kind: list(index.read_facts(kind)) for kind in
                        ('definitions', 'sites', 'imports', 'scopes', 'relationships')}
            report['metadata'] = index.metadata()
            report['normalized_facts_sha256'] = hashlib.sha256(encoded(produced)).hexdigest()
            # Grader oracle is opened only after the owner has published real facts.
            with SourceRoot(root) as owner:
                oracle = read_json(owner, ORACLE)[0]['query']
            assert oracle['source_path'] == source_path and oracle['source_sha256'] == metadata['sha256']
            assert len(oracle['assertions']) == 10 and len({item['id'] for item in oracle['assertions']}) == 10
            grade_failures, key_to_id = [], {}
            definitions = {physical(row): row for row in produced['definitions']}
            sites = {physical(row): row for row in produced['sites']}
            if (len(produced['definitions']) != len(oracle['declarations']) or
                    set(definitions) != {physical(row) for row in oracle['declarations']}):
                grade_failures.append({'dimension': 'declaration_inventory'})
            if (len(produced['sites']) != len(oracle['invocations']) or
                    set(sites) != {physical(row['site']) for row in oracle['invocations']}):
                grade_failures.append({'dimension': 'occurrence_inventory'})
            for expected in oracle['declarations']:
                actual = definitions.get(physical(expected))
                if actual is not None:
                    key_to_id[expected['key']] = actual['id']
                if (actual is None or actual['name'] != expected['name'] or actual['text'] != expected['text'] or
                        actual['provenance']['source_sha256'] != expected['source_sha256'] or
                        raw[expected['name_range']['start_byte']:expected['name_range']['end_byte']].decode('utf-8') != expected['name']):
                    grade_failures.append({'dimension': 'source_declaration', 'key': expected['key']})
            for expected in oracle['invocations']:
                actual = sites.get(physical(expected['site']))
                if (actual is None or actual['text'] != expected['site']['text'] or
                        actual['provenance']['source_sha256'] != metadata['sha256'] or actual['role'] != 'call' or
                        actual['certainty'] != 'resolved' or actual['targets_exhaustive'] is not True or
                        actual['caller'] != key_to_id.get(expected['caller_key']) or
                        actual['targets'] != [key_to_id.get(expected['target_key'])]):
                    grade_failures.append({'dimension': 'source_binding', 'site': physical(expected['site'])})
            checks.append({'id': label + ':source_physical_fact_grade',
                'status': 'failed' if grade_failures else 'passed', 'failures': grade_failures,
                'definitions': len(definitions), 'sites': len(sites), 'gold_passed_to_owner': False})
            expected_hub = sorted((item for item in oracle['invocations'] if item['caller_key'] == 'PY.fanout.hub'),
                                  key=lambda item: physical(item['site']))
            expected_keys = [(physical(item['site']), key_to_id.get(item['caller_key']), key_to_id.get(item['target_key']))
                             for item in expected_hub]
            names = {row['name']: row['id'] for row in produced['definitions']}
            definitions_by_id = {row['id']: row for row in produced['definitions']}
            sites_by_id = {row['id']: row for row in produced['sites']}
            for assertion in oracle['assertions']:
                entry = {'id': label + ':' + assertion['id'], 'question_id': assertion['id'],
                         'mode': label, 'status': 'running', 'responses': []}
                report['queries'].append(entry)
                results.append(entry)
                started = time.monotonic()
                try:
                    ticks = [0.0]
                    options = {'clock': lambda: ticks[0]} if assertion['id'] == 'Q-PY-DEADLINE' else {}
                    setup_started = time.monotonic()
                    setup_limits = Limits(**assertion.get('limits', assertion.get('limits_per_page', {})))
                    with SQLSnapshot(index.output, index.owner, index.output_owner, limits=setup_limits, **options) as snapshot:
                        entry['snapshot_setup'] = {'storage_setup_seconds': snapshot.storage_setup_seconds,
                            'snapshot_copy_seconds': snapshot.snapshot_copy_seconds,
                            'generation': snapshot.generation, 'source_identity': snapshot.source_identity}
                        assert snapshot.generation == receipt['generation']
                        assert snapshot.source_identity == receipt['source_identity']
                        seed, qid = names[assertion['seed']], assertion['id']

                        def observe(**kw):
                            limits = kw.get('limits', Limits())
                            begun = time.monotonic()
                            requested_limits = asdict(limits)
                            cold_cost = 0
                            if not entry['responses'] and qid != 'Q-PY-DEADLINE':
                                cold_cost = begun - setup_started
                                remaining = limits.timeout_seconds - cold_cost
                                if remaining <= 0:
                                    raise InterruptedError('Cold setup exhausted whole query deadline')
                                limits = replace(limits, timeout_seconds=remaining)
                                kw['limits'] = limits
                            page = snapshot.query(seed, depth=assertion.get('depth', 1), **kw)
                            size = len(encoded(page))
                            symbols = kw.get('operation', 'callees') == 'symbol'
                            handles = ({row['id'] for row in page['rows']} if symbols else
                                {handle['id'] for row in page['rows'] for handle in
                                 (row['caller'], row['target']) if handle is not None})
                            entry['responses'].append({'response': page, 'serialized_response_bytes': size,
                                'query_elapsed_seconds': time.monotonic() - begun,
                                'limits': asdict(limits), 'requested_limits': requested_limits,
                                'cold_setup_charged_seconds': cold_cost,
                                'independently_counted_symbol_handles': len(handles),
                                'independently_counted_nonseed_entities': len(handles - {seed})})
                            assert size <= limits.max_response_bytes
                            assert page['examined_relationships'] <= limits.max_examined_relationships
                            assert len(handles) <= limits.max_entities
                            assert page['returned_symbol_handles'] == len(handles)
                            assert page['returned_entities'] == len(handles - {seed})
                            assert page['returned_edges'] <= limits.max_edges
                            assert page['excerpt_bytes'] <= limits.max_excerpt_bytes
                            assert page['generation'] == receipt['generation'] and page['source_identity'] == receipt['source_identity']
                            for row in page['rows']:
                                if not symbols:
                                    actual = sites_by_id[row['site']['id']]
                                    assert physical(row['site']) == physical(actual)
                                    assert row['site']['source_sha256'] == actual['provenance']['source_sha256']
                                    assert row['site']['role'] == actual['role']
                                    assert row['certainty'] == actual['certainty']
                                    assert row['targets_exhaustive'] == actual['targets_exhaustive']
                                    assert row['reason'] == actual['reason']
                                    assert (row['caller']['id'] if row['caller'] else None) == actual['caller']
                                    assert row['target'] is not None and row['target']['id'] in actual['targets']
                                for handle in ((row,) if symbols else (row['caller'], row['target'])):
                                    if handle is None:
                                        continue
                                    declaration = definitions_by_id[handle['id']]
                                    assert physical(handle) == physical(declaration)
                                    assert handle['name'] == declaration['name'] and not handle['name_truncated']
                                    assert handle['source_sha256'] == declaration['provenance']['source_sha256']
                            no_bodies(page)
                            return page

                        def complete(values):
                            cursor, rows, work, sizes = None, [], 0, []
                            for _ in range(16):
                                page = observe(cursor=cursor, limits=Limits(**values))
                                rows.extend(page['rows'])
                                work += page['examined_relationships']
                                sizes.append(len(page['rows']))
                                cursor = page['cursor']
                                if cursor is None:
                                    assert not page['truncated']
                                    return rows, work, sizes, page
                            raise AssertionError('Bounded continuation attempts exhausted')

                        if qid in ('Q-PY-ALL-CALLEES', 'Q-PY-OUTPUT-PAGES'):
                            rows, work, sizes, page = complete(assertion.get('limits', assertion.get('limits_per_page')))
                            assert [row_key(row) for row in rows] == expected_keys
                            assert len({row_key(row) for row in rows}) == len(expected_keys)
                            assert page['total_count'] == {'value': len(expected_keys), 'kind': 'exact'}
                            assert work == assertion.get('expected_examined_relationships_across_continuations',
                                                        assertion.get('expected_total_examined_relationships'))
                            if qid == 'Q-PY-OUTPUT-PAGES':
                                assert sizes == assertion['expected_page_sizes']
                                first = observe(limits=Limits(max_edges=17))
                                rejects = []
                                with patch.object(snapshot, '_row', side_effect=AssertionError('Cursor rejection must precede materialization')):
                                    for binding, change in (('role', {'role': 'all'}), ('scope', {'scope': 'tests/fixtures/code-understanding/python/'}),
                                            ('prefix', {'prefix': 'leaf_0'}), ('operation', {'operation': 'callers'}), ('depth', {'depth': 2})):
                                        denied(lambda change=change: snapshot.query(seed, cursor=first['cursor'],
                                            **dict({'depth': 1}, **change)))
                                        rejects.append({'binding': binding, 'status': 'rejected_before_materialization'})
                                    with patch.object(analysis_queries, 'QUERY_RULE_VERSION', 'synthetic-query-rule-change'):
                                        denied(lambda: snapshot.query(seed, depth=1, cursor=first['cursor']))
                                    rejects.append({'binding': 'query_rule', 'status': 'rejected_before_materialization'})
                                now = [0.0]
                                with SQLSnapshot(index.output, index.owner, index.output_owner, clock=lambda: now[0]) as expiring:
                                    exp = expiring.query(seed, depth=1, limits=Limits(max_edges=17))
                                    now[0] = 61.0
                                    with patch.object(expiring, '_row', side_effect=AssertionError('Expired cursor must not materialize')):
                                        denied(lambda: expiring.query(seed, depth=1, cursor=exp['cursor']))
                                rejects.append({'binding': 'expiry', 'status': 'rejected_before_materialization'})
                                entry['cursor_binding_checks'] = rejects
                            entry.update(rows=len(rows), examined_total=work, page_sizes=sizes)
                        elif qid == 'Q-PY-WORK-EXHAUSTION':
                            page = observe(limits=Limits(**assertion['limits']))
                            assert len(page['rows']) == assertion['expected_returned_rows']
                            assert page['examined_relationships'] == assertion['expected_examined_relationships']
                            assert page['stop_reason'] == assertion['required_stop_reason'] and page['truncated']
                            assert page['total_count']['kind'] == 'lower_bound'
                            resumed = observe(cursor=page['cursor'], limits=Limits(max_edges=1))
                            assert row_key(resumed['rows'][0]) == expected_keys[assertion['next_unemitted_row_index']]
                        elif qid == 'Q-PY-FILTERED-WORK':
                            prefix = assertion['filter']['target_name_prefix']
                            page = observe(prefix=prefix, limits=Limits(**assertion['limits']))
                            assert page['examined_relationships'] == assertion['expected_examined_relationships']
                            assert len(page['rows']) == assertion['expected_returned_rows']
                            assert page['truncated'] and page['total_count'] == {'value': 0, 'kind': 'lower_bound'}
                            full = observe(prefix=prefix, limits=Limits(max_edges=120, max_entities=120, max_examined_relationships=120))
                            assert len(full['rows']) == assertion['matching_rows_in_complete_source_oracle']
                            assert full['examined_relationships'] == len(expected_keys)
                        elif qid == 'Q-PY-RESPONSE-BYTES':
                            with patch.object(snapshot, '_next', wraps=snapshot._next) as storage:
                                try:
                                    page = observe(limits=Limits(**assertion['limits']))
                                except ValueError as error:
                                    assert 'minimum envelope' in str(error)
                                    assert storage.call_count == 0
                                    entry['minimum_envelope_refused_before_relationship_work'] = True
                                else:
                                    assert page['examined_relationships'] < len(expected_keys) and page['cursor']
                                    position = len(page['rows'])
                                    resumed = observe(cursor=page['cursor'], limits=Limits(max_edges=1))
                                    assert row_key(resumed['rows'][0]) == expected_keys[position]
                                    assert resumed['examined_relationships'] == 0
                                    entry.update(first_response_rows=position, first_response_work=page['examined_relationships'],
                                                 resumed_cached_work=resumed['examined_relationships'])
                        elif qid == 'Q-PY-CYCLE-REACHABILITY':
                            page = observe(operation=assertion['operation'], limits=Limits(**assertion['limits']))
                            assert page['examined_relationships'] == assertion['expected_examined_relationships']
                            assert len(page['rows']) == assertion['expected_occurrence_relations']
                            assert page['returned_entities'] == len(assertion['expected_nonseed_vertices']) and page['cursor'] is None
                            assert page['returned_symbol_handles'] == len(assertion['expected_nonseed_vertices']) + 1
                            assert {row['target']['name'] for row in page['rows'] if row['target']['id'] != seed} == set(assertion['expected_nonseed_vertices'])
                            assert any(row['target']['id'] == seed for row in page['rows'])
                        elif qid == 'Q-PY-CANCEL-BEFORE':
                            page = observe(cancel=lambda: True)
                            assert page['examined_relationships'] == assertion['expected_examined_relationships']
                            assert len(page['rows']) == assertion['expected_returned_rows']
                            assert page['stop_reason'] == assertion['required_stop_reason'] and page['cursor'] is None
                        elif qid in ('Q-PY-CANCEL-DURING', 'Q-PY-DEADLINE'):
                            inspected, original = [0], snapshot._next
                            def inspect(*args):
                                row = original(*args)
                                if row is not None:
                                    inspected[0] += 1
                                    ticks[0] = float(inspected[0])
                                return row
                            with patch.object(snapshot, '_next', side_effect=inspect):
                                page = (observe(cancel=lambda: inspected[0] >= 3) if qid == 'Q-PY-CANCEL-DURING' else
                                        observe(limits=Limits(timeout_seconds=3)))
                            assert page['examined_relationships'] <= assertion['maximum_examined_relationships']
                            assert len(page['rows']) <= assertion.get('maximum_returned_rows', 3)
                            assert page['stop_reason'] == assertion['required_stop_reason'] and page['cursor'] is None
                            entry['inspected_relationships_before_stop'] = inspected[0]
                            if qid == 'Q-PY-DEADLINE':
                                entry['clock_scope'] = 'Injected inspection ticks; real cold setup/query times reported separately, no latency qualification.'
                            else:
                                usable = observe(limits=Limits(max_edges=1))
                                assert row_key(usable['rows'][0]) == expected_keys[0]
                        elif qid == 'Q-PY-STALE-GENERATION':
                            page = observe(limits=Limits(max_edges=17))
                            change = manifest['query_freshness_update']
                            changed = change['after_content_utf8'].encode('utf-8')
                            assert change['before_content_utf8'].encode('utf-8') == raw
                            assert hashlib.sha256(changed).hexdigest() == assertion['source_change']['sha256_after']
                            _adapter_materialize(source, {source_path: changed})
                            new = index.refresh([dict(metadata, sha256=hashlib.sha256(changed).hexdigest(), bytes=len(changed))],
                                                mode=mode, concurrency=concurrency)
                            entry['changed_refresh'] = new
                            assert new['status'] == 'ready' and new['source_identity'] != receipt['source_identity']
                            assert new['generation'] != receipt['generation']
                            fresh_facts = {kind: list(index.read_facts(kind)) for kind in ('definitions', 'sites')}
                            def topology(view):
                                by_id = {row['id']: row['name'] for row in view['definitions']}
                                return sorted((by_id[row['caller']], tuple(by_id[target] for target in row['targets']), row['role'], row['text'])
                                              for row in view['sites'])
                            assert topology(produced) == topology(fresh_facts)
                            fresh_names = {row['name']: row['id'] for row in fresh_facts['definitions']}
                            with SQLSnapshot(index.output, index.owner, index.output_owner) as fresh:
                                with patch.object(fresh, '_row', side_effect=AssertionError('Stale seed/cursor cannot materialize')):
                                    denied(lambda: fresh.query(fresh_names['hub'], depth=1, cursor=page['cursor']))
                                    denied(lambda: fresh.query(seed, depth=1))
                            retained = observe(cursor=page['cursor'], limits=Limits(max_edges=1))
                            assert row_key(retained['rows'][0]) == expected_keys[17]
                            assert retained['rows'][0]['site']['source_sha256'] == metadata['sha256']
                            entry.update(topology_unchanged=True, old_snapshot_coherent=True,
                                         old_generation=receipt['generation'], new_generation=new['generation'])
                        else:
                            raise AssertionError('Unhandled frozen query question')
                    entry['status'] = 'passed'
                except Exception as error:
                    entry.update(status='failed', error_kind=type(error).__name__, reason='Persisted query assertion failed')
                    print(json.dumps({'id': entry['id'], **_adapter_error(error)}), file=sys.stderr)
                entry['cold_snapshot_case_elapsed_seconds'] = time.monotonic() - started
            report['status'] = 'passed' if all(row['status'] == 'passed' for row in report['queries']) else 'failed'
            observed.append([{key: page['response'][key] for key in ('rows', 'generation', 'source_identity',
                'examined_relationships', 'returned_entities', 'returned_symbol_handles', 'returned_edges', 'excerpt_bytes', 'total_count',
                'truncated', 'stop_reason')} for row in report['queries'] for page in row['responses']])
            report['queries'] = [{'id': row['id'], 'question_id': row['question_id'], 'status': row['status']}
                                 for row in report['queries']]
    checks.append({'id': 'serial_queued_persisted_query_parity', 'status': 'passed' if
        observed[0] == observed[1] and modes[0]['normalized_facts_sha256'] == modes[1]['normalized_facts_sha256'] and
        modes[0]['index']['generation'] == modes[1]['index']['generation'] else 'failed'})
    with SourceRoot(ROOT) as owner:
        after = {path: owner.read(path, 2 * 1024 * 1024, hash_full=True)[1] for path in code_paths}
    checks.append({'id': 'implementation_stable', 'status': 'passed' if before == after else 'failed'})
    failures = [row for row in results + checks if row['status'] != 'passed']
    return {'schema_version': 1, 'suite': 'queries', 'status': 'failed' if failures else 'passed',
        'source_identity': {'supplement_lock_sha256': lock_sha, 'locked_inputs_sha256': lock['sha256'],
            'source': metadata, 'implementation': {'commit': revision, 'sha256': before}},
        'implementation_after': {'commit': revision, 'sha256': after}, 'preparation': prepared,
        'query_rule_version': analysis_queries.QUERY_RULE_VERSION, 'default_query_limits': asdict(Limits()),
        'case_results': results, 'checks': checks, 'failures': failures, 'coverage_failures': [], 'modes': modes,
        'counts': {'locked_questions_per_mode': 10, 'queries': len(results),
            'passed_queries': sum(row['status'] == 'passed' for row in results),
            'checks': len(results) + len(checks), 'passed': len(results) + len(checks) - len(failures)},
        'environment': environment(), 'qualification_complete': False, 'limits_qualified': False,
        'scope': 'Locked20 synthetic query cases through actual serial1/queued2 persisted SQL snapshots; '
                 'source facts graded after publication, cold setup and query costs retained; '
                 'full product, corpus, scale, human and release qualification unmeasured'}


def screen_engines(root=ROOT):
    """Check the source-backed eligibility record; this does not run an engine."""
    _, identities = frozen_inputs(root)
    with SourceRoot(root) as source:
        decision, sha = read_json(source, INPUTS + 'engine-decisions.json')
    if not isinstance(decision, dict):
        raise ValueError('Engine decisions must be an object')
    for key, kind in (('input_identity', dict), ('candidates', list), ('sources', dict), ('alternatives', list)):
        if not isinstance(decision.get(key), kind):
            raise ValueError('Invalid engine decision field: ' + key)
    if not all(isinstance(c, dict) for c in decision['candidates'] + decision['alternatives']):
        raise ValueError('Engine decision lists require object records')
    cases = []
    def check(name, value, reason):
        cases.append({'id': name, 'status': 'passed' if value else 'failed', 'reason': reason})
    check('schema', type(decision.get('schema_version')) is int and decision['schema_version'] == 1
          and decision.get('task_id') == 'code-understanding:T006', 'Task-scoped source screening record')
    check('frozen-source-key', decision.get('input_identity', {}).get('fixture_sha256') == identities['fixtures_sha256']
          and decision.get('input_identity', {}).get('source_review_sha256') == identities['source_review_sha256']
          and decision.get('input_identity', {}).get('input_lock_sha256') == identities['input_lock_sha256'],
          'Screening binds the committed AI-reviewed input key; no human qualification')
    check('not-selected', decision.get('selected_engine') is None and decision.get('qualification_complete') is False,
          'Screening cannot select a structural owner')
    pins = {'codebase-memory-mcp': '268a9d8886642eb7f9b2ce45f5ce27cdecf0f519',
            'joern-finite-batch': 'b9e0ce2279b862615b07ed1afb0552502fe3ac05'}
    candidates = decision.get('candidates', [])
    check('candidate-pins', isinstance(candidates, list) and len(candidates) == len(pins)
          and all(isinstance(c, dict) and c.get('revision') == pins.get(c.get('id')) for c in candidates)
          and {c.get('id') for c in candidates} == set(pins), 'Both exact initial candidates retained')
    for c in candidates:
        check(c['id'] + ':boundaries', c.get('disposition') in ('evaluate', 'defer', 'reject')
              and all(c.get(key) for key in ('license', 'supported_interfaces', 'lifecycle', 'source_safety',
                                            'evidence', 'incremental', 'query', 'component_gates', 'missing_prerequisites')),
              'Installation, evidence and lifecycle boundaries remain explicit')
        check(c['id'] + ':unexecuted', c.get('executed') is False and c.get('runtime_evidence') is None,
              'This source-screen artifact has no runtime proof')
    check('execution-count', decision.get('engine_execution_count') == 0,
          'No candidate build, frontend or query execution scored as passed')
    sources = decision.get('sources', {})
    for key, card in sources.items():
        valid = (isinstance(card, dict) and isinstance(card.get('path'), str)
                 and isinstance(card.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', card['sha256'])
                 and isinstance(card.get('lines'), list) and len(card['lines']) == 2
                 and all(type(x) is int for x in card['lines']) and 0 < card['lines'][0] <= card['lines'][1]
                 and isinstance(card.get('url'), str) and '?' not in card['url']
                 and any('/blob/' + pin + '/' + card['path'] + '#L' in card['url'] for pin in pins.values()))
        check('source:' + key, bool(valid), 'Hashed pinned primary source and bounded line reference; coordinator retains readback')
    check('source-records', bool(sources), 'Source references retained, not only a decision label')
    expected = {'scip_enrichment', 'joern_depth_comparison', 'graph_database', 'dsl_mcp',
                'webgl', 'ann_index', 'model_alternatives', 'custom_rust_pyo3'}
    alternatives = decision.get('alternatives', [])
    check('optional-decisions', {a['id'] for a in alternatives} == expected and len(alternatives) == len(expected)
          and all(a.get('disposition') in ('evaluate', 'defer', 'reject') and a.get('reason') and a.get('trigger')
                  for a in alternatives), 'Optional alternatives are decisions, not automatically implemented features')
    return {'schema_version': 1, 'gate': 'source-screen', 'status': 'passed' if all(c['status'] == 'passed' for c in cases) else 'failed',
            'source_identity': {'decisions_sha256': sha, **identities}, 'case_results': cases,
            'decisions': decision, 'limits': ['Source screening only; no engine adopted.',
                'Joern installation remains blocked by exact Go asset license/notice provenance.',
                'Real-call quality, updates, query work and full runtime lifecycle remain unqualified.']}


@contextmanager
def worker_directory(source_map, requested=None):
    """Evaluation workers must write outside every supplied corpus root."""
    with SourceRoot(source_map.parent) as source:
        mapped, _ = read_json(source, source_map.name)
    directory = requested or source_map.parent / 'analysis-workers'
    destination = directory.resolve()
    for entry in mapped['corpora']:
        root = Path(entry['source']).resolve()
        if destination == root or root in destination.parents:
            raise ValueError('Evaluation worker directory must be outside source corpora')
    ancestor = destination
    while not ancestor.exists():
        ancestor = ancestor.parent
    with SourceRoot(ancestor) as owner:
        if not owner.secure or owner.root != ancestor:
            raise ValueError('Evaluation worker directory ownership changed')
        parent = os.dup(owner.fd)
        try:
            for part in destination.relative_to(ancestor).parts:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=parent)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            # ponytail: Linux qualification uses the open directory during both
            # creation and worker writes. Other kernels remain unqualified.
            pinned = Path('/proc/self/fd') / str(parent)
            if not pinned.is_dir():
                raise OSError('Pinned evaluation work roots require Linux descriptor paths')
            yield pinned
        finally:
            os.close(parent)


def comparison_identity(root=ROOT):
    """Capture the complete experiment before its first stage, not midway."""
    _, identity = frozen_inputs(root)
    paths = ('evaluations/analysis.py', 'evaluations/acceptance.py', 'evaluations/real_calls.py',
             'evaluations/engine_checks.py', 'repo_graph/analysis_native.py', 'repo_graph/source.py',
             'evaluations/incremental_candidate.py', 'repo_graph/analysis_queue.py',
             'evaluations/bounded_queries.py', 'evaluations/supplement_preparation.py',
             'evaluations/code-understanding/supplement-source.json',
             'evaluations/code-understanding/supplement-oracle.json',
             'evaluations/code-understanding/supplement-lock.json',
             'evaluations/code-understanding/source-target-lock.json',
             'evaluations/code-understanding/source-target-locations.json', 'evaluations/performance.py', 'repo_graph/__init__.py', 'repo_graph/builder.py', 'repo_graph/search.py', 'evaluations/code-understanding/engine-decisions.json', 'pyproject.toml', 'uv.lock')
    with SourceRoot(root) as source:
        hashes = {path: source.read(path, 1024 * 1024, hash_full=True)[1] for path in paths}
    return {'source_identity': identity, 'commit': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=20).strip(), 'sha256': hashes}


def compact_attempt(attempt):
    """Keep actual counters/failures, without duplicating every source inventory."""
    def select(value, names):
        return {key: value[key] for key in names.split() if key in value}
    result = select(attempt, 'status generation source_identity semantic_facts_sha256 counts '
                    'previous_generation error_kind stop_reason mode concurrency cache_or_ready_snapshot_published')
    if 'facts_artifact' in attempt:
        result['facts_artifact'] = select(attempt['facts_artifact'], 'path sha256 bytes')
    if 'snapshot_state' in attempt:
        result['snapshot_state'] = select(attempt['snapshot_state'],
            'generation matches_returned_generation fresh_facts_retained')
    inventory = attempt.get('inventory', [])
    result['inventory_sha256'] = hashlib.sha256(json.dumps(inventory, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    result['inventory_status_counts'] = {status: sum(item['status'] == status for item in inventory)
                                         for status in sorted({item['status'] for item in inventory})}
    result['source_failures'] = [select(item, 'path language kind status error_kind errno')
        for item in inventory if item['status'] not in ('parsed', 'configuration')]
    for key in ('validation_failures', 'collector_failures', 'resolution_errors'):
        result[key] = [select(item, 'index path kind status error_kind reason errno')
                       for item in attempt.get(key, [])]
        for item, original in zip(result[key], attempt.get(key, [])):
            if 'record' in original:
                item['record'] = select(original['record'], 'path language kind bytes sha256')
    result['remaining_inventory'] = attempt.get('remaining_inventory', [])
    result['cleanup'] = [select(item, 'signals leader_reaped group_absent returncode requests mailboxes_removed')
                         for item in attempt.get('cleanup', [])]
    resources = attempt.get('resources', {})
    result['resources'] = select(resources, 'source_bytes digest_read_bytes digest_read_operations '
        'changed_files_collected unchanged_source_collections_reused all_admitted_bindings_reresolved elapsed_seconds')
    result['resources']['resolve'] = select(resources.get('resolve', {}),
                                            'source_bytes collected_nodes facts_emitted elapsed_seconds')
    queue = resources.get('queued', {})
    result['resources']['queued'] = select(queue, 'mode configured_concurrency workers_started files_admitted '
        'files_collected source_bytes collected_handoff_bytes collected_nodes collected_definitions '
        'peak_inflight_reserved_bytes elapsed_seconds worker_file_hard_limit_bytes')
    result['resources']['queued']['limits'] = select(queue.get('limits', {}),
        'max_request_bytes max_result_bytes max_inflight_bytes max_admitted_bytes memory_bytes cpu_seconds '
        'worker_wall_seconds total_wall_seconds log_bytes')
    result['resources']['queued']['worker_summaries'] = [dict(
        observed_requests=len(worker),
        observed_request_elapsed_seconds_sum=sum(row['elapsed_seconds'] for row in worker),
        **{key: max((row[key] for row in worker), default=None) for key in
           ('process_peak_rss_bytes', 'process_user_seconds', 'process_system_seconds')})
        for worker in queue.get('worker_resources', [])]
    result['resources']['queued']['worker_isolation'] = [select(row or {},
        'python_isolated_mode bytecode_writes_disabled user_site_disabled private_environment own_session_and_group controller_death_signal')
        for row in queue.get('worker_isolation', [])]
    return result


def compact_preselection_cost(wrapper):
    """Export finite source-free costs; actual proof remains in the bound archive."""
    from evaluations.acceptance import _COST_MODES, _COST_PHASES, _COST_HELPERS, _proof_frozen
    def select(value, fields):
        return {k: value[k] for k in fields.split() if k in value} if type(value) is dict else {}
    statuses = {'complete', 'failed', 'running', 'blocked', 'missing', 'invalid_identity',
        'equivalence_failed', 'measurement_failed', 'evidence_failed', 'admission_failed', 'cleanup_failed'}
    def status(value):
        return value if type(value) is str and value in statuses else 'malformed'
    def error(value):
        known = {'OSError', 'ValueError', 'RuntimeError', 'TypeError', 'KeyError', 'MemoryError',
                 'RecursionError', 'AttributeError', 'TimeoutExpired', 'BackendUnavailable'}
        kind = value.get('error_kind', value.get('kind')) if type(value) is dict else None
        return {'error_kind': kind if type(kind) is str and kind in known else 'OtherError'}
    report = wrapper.get('full_private_report', wrapper)
    result = dict(select(report, 'schema_version kind engine_selected qualification_complete '
        'measurement_defaults_qualified large_corpus_profiled phase_semantic_agreement'), status=status(report.get('status')))
    if 'archive' in wrapper:
        archive = wrapper['archive']; jobs = {f'{m}-{c}-{n}' for m,c in _COST_MODES for n in range(3)}
        if type(archive) is not dict or set(archive) != {'directory', 'files', 'bytes'} or \
            type(archive['directory']) is not str or re.fullmatch(r'native-dual-[0-9a-f]{32}', archive['directory']) is None:
            raise ValueError('Unknown cost archive shape')
        # Only fixed producer filenames can enter a public reference. The raw
        # archive validator subsequently verifies all bytes and its full inventory.
        simple = set(_COST_PHASES) | {p+'.facts' for p in _COST_PHASES} | {'control', 'result', 'owned-rss'}
        for row in archive['files']:
            path = row['path']; parts = SourceRoot.parts(path)
            safe = path == 'report.json'
            if len(parts) == 2 and parts[0] in jobs:
                safe |= parts[1] in {n+'.json' for n in simple} | {'stdout.log', 'stderr.log', 'owned-telemetry.jsonl'}
            if len(parts) in (3,4) and parts[0] in jobs and re.fullmatch(r'run-[0-9a-f]{32}', parts[1]):
                safe |= len(parts) == 3 and parts[2] == 'receipt.json'
                safe |= len(parts) == 4 and re.fullmatch(r'worker-[0-3]', parts[2]) is not None and parts[3] in {
                    'stdout.log', 'stderr.log', 'control.json', 'ready.json', 'request.json', 'source.bin', 'payload.json', 'result.json'}
            if not safe: raise ValueError('Unknown finite cost archive reference')
        result['archive'] = archive
    for key in ('binding_before', 'binding_after'):
        result[key] = select(report.get(key), 'measured_commit implementation input_binding root_identity backend')
    for key in ('failure', 'identity_failure'):
        if key in report: result[key] = error(report[key])
    envelope = report.get('supervisor_envelope', {})
    result['supervisor_envelope'] = select(envelope, 'address_space_soft_bytes address_space_hard_bytes '
        'cpu_soft_seconds cpu_hard_seconds core_bytes file_bytes whole_wall_seconds limits_qualified sigxcpu_default')
    result['cases'] = []
    for position, row in enumerate(report.get('cases', [])):
        item = select(row, 'mode concurrency repeat returncode identity_verified cleanup report_artifact logs')
        expected = next((f'{m}-{c}-{n}' for m,c in _COST_MODES for n in range(3) if
            row.get('mode') == m and type(row.get('concurrency')) is int and row['concurrency'] == c and
            type(row.get('repeat')) is int and row['repeat'] == n), None)
        item.update(id=expected or f'cost:{position}', status=status(row.get('status')))
        item['cleanup'] = select(row.get('cleanup'), 'signals leader_reaped group_absent returncode')
        item['logs'] = [select(v, 'path sha256 bytes complete') for v in row.get('logs', [])]
        for key in ('failure', 'identity_failure'):
            if key in row: item[key] = error(row[key])
        job = row.get('report') or {}
        item['source_owner_identity'] = job.get('source_owner_identity')
        rss = job.get('owned_rss') or {}
        item['owned_rss'] = select(rss, 'peak_sampled_owned_rss_bytes sample_count complete_sample_count '
            'sample_gap_count largest_start_interval_ns max_read_skew_ns requested_interval_seconds '
            'sampler_stopped unsampled_peak_bound retained_log_bytes max_samples max_live_owners max_lifetime_owners')
        for key in ('owned_rss_artifact', 'owned_telemetry_artifact'):
            item[key] = select(job.get(key), 'path sha256 bytes')
        item['phases'] = []
        for index, phase in enumerate(job.get('phases', [])):
            p = select(phase, 'wall_seconds observed_attempt_seconds proof_retention_seconds')
            p.update(label=phase.get('label') if phase.get('label') in _COST_PHASES else f'phase:{index}',
                     status=status(phase.get('status')))
            p['stages'] = {k: select(v, 'calls inclusive_seconds') for k,v in phase.get('stages', {}).items()
                if k in {'source_read', 'collection_controller', 'handoff_decode', 'cache_decode', 'cache_encode',
                         'global_resolution', 'snapshot_construction'}}
            p['facts_artifact'] = select(phase.get('facts_artifact'), 'path sha256 bytes')
            p['receipt'] = compact_attempt(phase['receipt']) if 'receipt' in phase else None
            queued = phase.get('receipt', {}).get('resources', {}).get('queued', {})
            timing = queued.get('telemetry', {})
            p['controller_timings'] = select(timing.get('controller_timings'), 'mailbox_write_seconds mailbox_read_seconds '
                'receipt_decode_seconds handoff_decode_seconds admission_seconds observer_seconds')
            p['observer'] = select(timing, 'observer_events_delivered observer_failed actual_workers_started')
            p['file_costs'] = [{**select(v, 'file_user_seconds file_system_seconds'),
                'timings': select(v.get('timings'), 'backend_setup_seconds parse_seconds traversal_lowering_seconds '
                    'collect_elapsed_seconds handoff_serialize_seconds')} for worker in queued.get('worker_resources', []) for v in worker]
            for key in ('error', 'measurement_error', 'evidence_failure'):
                if key in phase: p[key] = error(phase[key])
            item['phases'].append(p)
        result['cases'].append(item)
    result['scope'] = 'Finite frozen24-file costs only; sampled current RSS includes measurement overhead; no scale/default qualification'
    # A malformed private receipt must not turn a nested arbitrary string/key
    # into a public value before archive/source admission. Fixed paths come
    # only from locked inputs or the bounded producer filename grammar above.
    vocabulary = statuses | {'malformed', 'OtherError', 'native_dual_fixture_profile', 'serial', 'queued',
        'parsed', 'configuration', 'SIGTERM', 'SIGKILL', result['scope']} | set(_COST_PHASES) | set(_COST_HELPERS)
    vocabulary |= {'OSError', 'ValueError', 'RuntimeError', 'TypeError', 'KeyError', 'MemoryError',
        'RecursionError', 'AttributeError', 'TimeoutExpired', 'BackendUnavailable'}
    vocabulary |= {f'{m}-{c}-{n}' for m,c in _COST_MODES for n in range(3)}
    vocabulary |= {f'cost:{n}' for n in range(9)} | {f'phase:{n}' for n in range(8)}
    vocabulary |= set(PINS) | set(PINS.values())
    if 'archive' in result:
        vocabulary |= {result['archive']['directory']} | {r['path'] for r in result['archive']['files']}
        vocabulary |= {Path(r['path']).name for r in result['archive']['files']}
        vocabulary |= set(_proof_frozen(ROOT)['binding'])
    keys = set('schema_version kind engine_selected qualification_complete measurement_defaults_qualified '
        'large_corpus_profiled phase_semantic_agreement status archive directory files path sha256 bytes '
        'binding_before binding_after measured_commit implementation input_binding root_identity backend '
        'failure identity_failure error_kind supervisor_envelope address_space_soft_bytes address_space_hard_bytes '
        'cpu_soft_seconds cpu_hard_seconds core_bytes file_bytes whole_wall_seconds limits_qualified sigxcpu_default '
        'cases mode concurrency repeat returncode identity_verified cleanup report_artifact logs id signals '
        'leader_reaped group_absent complete source_owner_identity owned_rss peak_sampled_owned_rss_bytes '
        'sample_count complete_sample_count sample_gap_count largest_start_interval_ns max_read_skew_ns '
        'requested_interval_seconds sampler_stopped unsampled_peak_bound retained_log_bytes max_samples '
        'max_live_owners max_lifetime_owners owned_rss_artifact owned_telemetry_artifact phases wall_seconds '
        'observed_attempt_seconds proof_retention_seconds label stages calls inclusive_seconds source_read '
        'collection_controller handoff_decode cache_decode cache_encode global_resolution snapshot_construction '
        'facts_artifact receipt controller_timings mailbox_write_seconds mailbox_read_seconds receipt_decode_seconds '
        'handoff_decode_seconds admission_seconds observer_seconds observer observer_events_delivered observer_failed '
        'actual_workers_started file_costs file_user_seconds file_system_seconds timings backend_setup_seconds '
        'parse_seconds traversal_lowering_seconds collect_elapsed_seconds handoff_serialize_seconds error '
        'measurement_error evidence_failure scope generation source_identity semantic_facts_sha256 counts definitions '
        'sites previous_generation stop_reason cache_or_ready_snapshot_published inventory_sha256 inventory_status_counts '
        'source_failures validation_failures collector_failures resolution_errors remaining_inventory resources '
        'source_bytes digest_read_bytes digest_read_operations changed_files_collected unchanged_source_collections_reused '
        'all_admitted_bindings_reresolved elapsed_seconds resolve collected_nodes facts_emitted queued '
        'configured_concurrency workers_started files_admitted files_collected collected_handoff_bytes '
        'collected_definitions peak_inflight_reserved_bytes worker_file_hard_limit_bytes limits max_request_bytes '
        'max_result_bytes max_inflight_bytes max_admitted_bytes memory_bytes cpu_seconds worker_wall_seconds '
        'total_wall_seconds log_bytes worker_summaries observed_requests observed_request_elapsed_seconds_sum '
        'process_peak_rss_bytes process_user_seconds process_system_seconds worker_isolation python_isolated_mode '
        'bytecode_writes_disabled user_site_disabled private_environment own_session_and_group controller_death_signal '
        'requests mailboxes_removed index language record errno reason'.split()) | vocabulary
    def guard(value):
        if type(value) is dict:
            if not set(value) <= keys: raise ValueError('Unknown cost projection field')
            for child in value.values(): guard(child)
        elif type(value) is list:
            for child in value: guard(child)
        elif type(value) is str:
            if value not in vocabulary and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value) is None:
                raise ValueError('Unknown cost projection string')
        elif value is not None and type(value) not in (bool, int, float):
            raise ValueError('Unknown cost projection primitive')
        elif type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 2**63-1):
            raise ValueError('Unbounded cost projection number')
    guard(result)
    return result



def compact_cost_invocation(raw, directory, references):
    """Fixed public counters/references; no private PID, path or error text."""
    if re.fullmatch(r'invocation-[0-9a-f]{32}', directory) is None:
        raise ValueError('Unknown cost invocation directory')
    states = {'complete', 'failed', 'running', 'invalid_identity', 'cleanup_failed', 'deadline_exceeded'}
    result = {k: raw[k] for k in ('schema_version', 'wrapper_sha256', 'wrapper_identity_stable',
        'wall_seconds', 'measurement_elapsed_seconds', 'elapsed_seconds', 'teardown_seconds',
        'attempts_started', 'returncode', 'source_after_unavailable', 'admitted_wall_exhausted') if k in raw}
    result.update(directory=directory, owner_identity=raw.get('evidence_owner_identity'), files=references,
        status=raw.get('status') if raw.get('status') in states else 'malformed')
    result['cleanup'] = {k: v for k,v in (raw.get('cleanup') or {}).items()
        if k in {'signals', 'leader_reaped', 'group_absent', 'returncode'}}
    result['isolation'] = {k: v for k,v in (raw.get('wrapper_isolation') or {}).items()
        if k in {'private_cwd', 'private_environment_allowlist', 'isolated_python', 'bytecode_disabled'}}
    for key in ('failure', 'source_after_failure', 'cleanup_failure'):
        if key in raw:
            kind = raw[key].get('kind') if type(raw[key]) is dict else None
            known = {'OSError', 'ValueError', 'RuntimeError', 'TypeError', 'KeyError',
                'TimeoutError', 'TimeoutExpired', 'MemoryError', 'RecursionError', 'KeyboardInterrupt'}
            result[key] = {'error_kind': kind if kind in known else 'OtherError'}
    # Direct counters are typed before being exported; malformed private strings
    # cannot acquire a public slot even when the raw invocation failed.
    for key in ('wrapper_sha256', 'owner_identity'):
        if re.fullmatch(r'[0-9a-f]{64}', result.get(key) or '') is None:
            raise ValueError('Typed invocation source/owner digest required')
    for key in ('schema_version', 'wall_seconds', 'attempts_started'):
        if key in result and (type(result[key]) is not int or not 0 <= result[key] < 2**63):
            raise ValueError('Typed invocation counter required')
    if result.get('returncode') is not None and type(result['returncode']) is not int:
        raise ValueError('Typed invocation return code required')
    for key in ('measurement_elapsed_seconds', 'elapsed_seconds', 'teardown_seconds'):
        if key in result and (type(result[key]) not in (int,float) or not math.isfinite(result[key]) or not 0 <= result[key] < 2**63):
            raise ValueError('Finite invocation timing required')
    for key in ('wrapper_identity_stable', 'source_after_unavailable', 'admitted_wall_exhausted'):
        if key in result and type(result[key]) is not bool:
            raise ValueError('Typed invocation observation required')
    if any(type(v) is not bool for v in result['isolation'].values()) or any(
        type(result['cleanup'][k]) is not bool for k in ('leader_reaped','group_absent') if k in result['cleanup']) or \
        any(v not in ('SIGTERM','SIGKILL') for v in result['cleanup'].get('signals',[])):
        raise ValueError('Typed invocation isolation/cleanup required')
    if 'returncode' in result['cleanup'] and result['cleanup']['returncode'] is not None and type(result['cleanup']['returncode']) is not int:
        raise ValueError('Typed cleanup return code required')
    return result

def read_preselection_cost(path):
    """Read an evidence-only private wrapper; never launch or retry a profiler."""
    if path is None:
        return {'status': 'missing', 'engine_selected': False, 'qualification_complete': False, 'cases': []}
    from evaluations.acceptance import _proof_directory, _proof_value, read_json as strict_read
    try:
        path = Path(path); parent, owner = _proof_directory(path.parent)
        if path.name != 'stdout.log': raise ValueError('Fixed cost invocation output required')
        references = []
        with SourceRoot(parent) as source:
            wrapper, _ = strict_read(source, path.name, 8 * 1024 * 1024)
            receipt, _ = strict_read(source, 'invocation.json', 256 * 1024)
            for name in ('invocation.json', 'stdout.log', 'stderr.log'):
                cap = 256 * 1024 if name == 'invocation.json' else 8 * 1024 * 1024
                raw, sha, info = source.read(name, cap+1, hash_full=False)
                if len(raw) != info.st_size or len(raw) > cap: raise ValueError('Complete bounded invocation file required')
                references.append({'path': name, 'sha256': sha, 'bytes': len(raw)})
            if source.identity != receipt.get('evidence_owner_identity'):
                raise ValueError('Cost invocation receipt owner changed')
        _proof_value(wrapper); _proof_value(receipt)
        result = compact_preselection_cost(wrapper)
        result['invocation'] = compact_cost_invocation(receipt, parent.name, references)
        if _proof_directory(parent)[1] != owner: raise ValueError('Cost invocation owner changed')
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, MemoryError, RecursionError) as error:
        raw = wrapper.get('full_private_report', wrapper) if type(locals().get('wrapper')) is dict else None
        rows = raw.get('cases', []) if type(raw) is dict else []
        if type(rows) is not list: rows = []
        states = {'complete', 'failed', 'blocked', 'missing', 'cleanup_failed', 'invalid_identity', 'measurement_failed'}
        return {'status': 'blocked', 'error_kind': type(error).__name__, 'engine_selected': False,
                'qualification_complete': False, 'cases': [{'id': f'cost:{n}', 'status': row.get('status')
                    if row.get('status') in states else 'malformed'} for n,row in enumerate(rows[:9]) if type(row) is dict]}


def compact_adapter_result(report, kind):
    """Full responses stay in a bound private archive; portable occurrence proofs remain."""
    if kind == 'preselection_cost':
        return compact_preselection_cost(report)
    def select(value, names):
        return {key: value[key] for key in names.split() if key in value}
    archive = report.get('archive')
    report = report.get('full_private_report', report)
    result = select(report, 'status archive measured_commit measured_commit_before measured_commit_after '
        'implementation implementation_hashes_before implementation_hashes_after input_binding '
        'committed_inputs_stable_after source_lock_sha256 preparation_checks elapsed_seconds '
        'elapsed_seconds_observed parent_lifetime_peak_rss_bytes query_rule_version mode_equivalence '
        'engine_selected qualification_complete scope ceilings')
    if archive is not None:
        result['archive'] = archive
    for name in ('binding_before', 'binding_after'):
        if isinstance(report.get(name), dict):
            result[name] = select(report[name], 'measured_commit implementation implementation_sha256 input_binding '
                'root_identity implementation_stable loaded_controller_sha256 load_scope commit_scope')
    result.update(select(report, 'implementation_load_scope'))
    if 'driver_failure' in report:
        result['driver_failure'] = select(report['driver_failure'], 'error_kind')
    result['failures'] = [select(row, 'stage id error_kind') for row in report.get('failures', [])]
    if kind == 'updates':
        result['results'] = [dict(select(row, 'id language status error_kind archive source_owner_identity'),
            attempts={name: compact_attempt(attempt) for name, attempt in row.get('attempts', {}).items()})
            for row in report.get('results', [])]
        result['resource_scope'] = 'Parent lifetime RSS and individual worker lifetime RSS; no combined owned-process peak'
    elif kind == 'queries':
        result['source'] = select(report.get('source', {}),
            'path sha256 bytes owner_identity copy_verified_with_SourceRoot')
        result['modes'] = []
        for mode in report.get('modes', []):
            item = select(mode, 'mode configured_concurrency status actual_base_workers_started source_fact_grade queue_parallelism_observation')
            item['base_refresh'] = compact_attempt(mode['base_refresh']) if 'base_refresh' in mode else None
            item['failures'] = [select(row, 'id error_kind') for row in mode.get('failures', [])]
            item['queries'] = []
            for entry in mode.get('queries', []):
                query = select(entry, 'id status error_kind rows examined_total pages page_sizes cursor_binding_checks '
                    'first_response_rows first_response_work resumed_cached_work cancellation_callback_calls '
                    'old_cursor_rejected_against_new_generation old_physical_seed_refused topology_unchanged '
                    'generation_changed old_snapshot_coherent old_generation new_generation archive clock_scope')
                query['responses'] = []
                for response in entry.get('responses', []):
                    page = response['response']
                    compact = select(page, 'generation examined_relationships returned_entities returned_edges '
                                          'total_count truncated stop_reason')
                    compact['has_cursor'] = page['cursor'] is not None
                    compact['rows'] = [dict(site=select(row['site'], 'id path range role source_sha256'),
                        caller_id=row['caller']['id'] if row['caller'] else None,
                        target_id=row['target']['id'] if row['target'] else None,
                        certainty=row['certainty'], targets_exhaustive=row['targets_exhaustive']) for row in page['rows']]
                    query['responses'].append(dict(select(response,
                        'serialized_response_bytes real_elapsed_seconds_observed'), response=compact))
                if 'changed_refresh' in entry:
                    query['changed_refresh'] = compact_attempt(entry['changed_refresh'])
                item['queries'].append(query)
            result['modes'].append(item)
    elif kind == 'missing_backend':
        result.update(select(report, 'implementation_sha256 implementation_stable owned_runtime_removed '
            'component_metadata_and_logs_retained excluded_runtime_directories limits limitations '
            'loaded_controller_sha256 load_scope commit_scope'))
        result['stages'] = [dict(select(row, 'stage status returncode stop_reason elapsed_seconds error_kind'),
            cleanup=select(row['cleanup'], 'signals leader_reaped group_absent returncode')
                if row.get('cleanup') is not None else None) for row in report.get('stages', [])]
        result['runtime_cleanup'] = [select(row, 'path removed error_kind')
                                     for row in report.get('runtime_cleanup', [])]
        for key in ('runtime_registration_failure', 'runtime_cleanup_failure', 'log_or_probe_failure',
                    'publication_failure', 'stale_receipt_removal_failure', 'archive_failure'):
            if key in report:
                result[key] = select(report[key], 'error_kind')
        probe = report.get('probe') or {}
        result['probe'] = select(probe, 'schema_version status checks pre_bootstrap_distributions '
            'pre_bootstrap_absent_optional_backend absent_optional_backend '
            'source_checkout_metadata_after_bootstrap')
        result['probe']['cli'] = [select(row, 'command returncode') for row in probe.get('cli', [])]
        core = probe.get('core', {})
        result['probe']['core'] = select(core, 'file_count jev')
        for key in ('scan', 'search'):
            result['probe']['core'][key] = select(core.get(key, {}),
                'scanned reused truncated code_files documents deleted seconds generation failed secure_reads')
        result['probe']['core']['keyword'] = select(core.get('keyword', {}), 'mode documents results')
        result['probe']['resources'] = select(probe.get('resources', {}),
            'elapsed_seconds process_peak_rss_bytes process_user_seconds process_system_seconds '
            'reaped_children_peak_rss_bytes reaped_children_user_seconds reaped_children_system_seconds')
        isolation = probe.get('isolation', {})
        result['probe']['isolation'] = {key: isolation.get(key) is True for key in
            ('python_isolated_mode', 'bytecode_writes_disabled', 'user_site_disabled', 'pip_module_spec_absent')}
        result['probe']['isolation']['own_session_and_group'] = (type(isolation.get('pid')) is int and
            isolation['pid'] == isolation.get('sid') == isolation.get('pgrp'))
        paths = isolation.get('private_environment', {})
        expected = {'HOME': 'home', 'XDG_CONFIG_HOME': 'config', 'XDG_CACHE_HOME': 'cache',
                    'XDG_DATA_HOME': 'data', 'TMPDIR': 'tmp'}
        result['probe']['isolation']['private_environment_confined'] = (isinstance(paths, dict) and
            set(paths) == set(expected) and isinstance(isolation.get('job_directory'), str) and
            all(isinstance(paths[key], str) and Path(paths[key]) == Path(isolation['job_directory']) / name
                for key, name in expected.items()))
        result['probe']['isolation']['separate_venv_prefix'] = (isinstance(isolation.get('sys_prefix'), str) and
            isinstance(isolation.get('sys_base_prefix'), str) and isolation['sys_prefix'] != isolation['sys_base_prefix'])
        result['probe']['components'] = []
        for row in probe.get('components', []):
            item = select(row, 'mode configured_concurrency candidate_snapshot_is_none candidate_cache_entries')
            item['candidate'] = compact_attempt(row['candidate'])
            queue = row['queue']
            item['queue'] = select(queue, 'status stop_reason collected')
            item['queue']['cleanup'] = [select(row,
                'signals leader_reaped group_absent returncode requests mailboxes_removed')
                for row in queue.get('cleanup', [])]
            item['queue']['attempt'] = compact_attempt({'status': queue['status'],
                'collector_failures': queue.get('failures', []), 'cleanup': queue.get('cleanup', []),
                'resources': {'queued': queue.get('resources', {})}})
            result['probe']['components'].append(item)
    else:
        raise ValueError('Unknown finite adapter result kind')
    return result


def compare_component(source_map, work_root=None, preselection_cost_report=None):
    """Run source-only experiments; absent capabilities cannot select an owner."""
    from evaluations.real_calls import compare_real_calls
    from evaluations.engine_checks import run_checks, run_updates, run_queries, run_missing_backend
    captured = comparison_identity()
    cost = read_preselection_cost(preselection_cost_report or os.environ.get('REPO_GRAPH_EVAL_PRESELECTION_COST_REPORT'))
    syntax, screen = component(), screen_engines()
    real = compare_real_calls(source_map)
    with worker_directory(source_map, work_root) as directory:
        lifecycle = run_checks(evidence_directory=directory)
        updates = compact_adapter_result(run_updates(evidence_directory=directory), 'updates')
        queries = compact_adapter_result(run_queries(evidence_directory=directory), 'queries')
        missing = compact_adapter_result(run_missing_backend(evidence_directory=directory), 'missing_backend')
    if comparison_identity() != captured:
        raise ValueError('Comparison inputs or implementation changed between stages')
    checks = lifecycle['check_results']
    quality = all(value['supported_denominator'] > 0 and value['supported_ungraded'] == 0 and
        value['selected_supported_precision'] is not None and value['selected_supported_precision'] >= .95 and
        value['selected_supported_recall_lower_bound'] >= .85 for value in real['per_language'].values())
    required_lifecycle = [c for c in checks if c['id'] not in ('incremental_equivalence', 'bounded_query_work')]
    gates = [
        {'id': 'syntax_direct_binding', 'status': syntax['status'], 'scope': syntax['scope']},
        {'id': 'reusable_source_screen', 'status': screen['status'], 'scope': 'Eligibility only; rejected candidates not executed'},
        {'id': 'real_call_quality', 'status': 'passed' if quality else 'failed', 'per_language': real['per_language'],
         'targets': {'precision': .95, 'recall': .85}, 'scope': 'Sixteen independent AI-reviewed examples; tiny selected sample'},
        {'id': 'finite_worker_lifecycle', 'status': 'passed' if required_lifecycle and
            all(c['status'] == 'passed' for c in required_lifecycle) else 'failed',
         'scope': 'Owned evaluation workers only; not a product lifecycle API'},
        {'id': 'evidence_uncertainty', 'status': 'passed' if syntax['status'] == 'passed' and
            all(c['status'] == 'passed' for c in real['case_results'] if not c['supported']) else 'failed',
         'scope': 'Exact source ranges/provenance and conservative uncertainty; missing candidate targets reported separately'},
        {'id': 'optional_installation', 'status': missing['status'],
         'scope': 'Actual stdlib-only source-checkout core/component paths; native harness distribution remains separate'},
        {'id': 'incremental_equivalence', 'status': updates['status'],
         'scope': 'All36 frozen updates, both modes versus clean same-owner rebuild; unsupported semantics retained'},
        {'id': 'bounded_query_work', 'status': queries['status'],
         'scope': 'Ten frozen query assertions in each mode; exact physical occurrences, pages, work and bytes retained'},
    ]
    result = {'schema_version': 1, 'experiment': 'component-engine-comparison', 'status': 'blocked',
        'engine_selected': False, 'selected_owner': None, 'qualification_complete': False,
        'source_identity': real['source_identity'], 'source_map_sha256': real['source_map_sha256'],
        'implementation': {k: captured[k] for k in ('commit', 'sha256')},
        'scope': 'Finite component comparison; no product owner selected',
        'component': syntax, 'real_calls': real, 'lifecycle': lifecycle, 'source_screen': screen,
        'updates': updates, 'queries': queries, 'missing_backend': missing, 'preselection_cost': cost,
        'case_results': gates, 'coverage_failures': syntax['coverage_failures'],
        'blocking_gates': [c['id'] for c in gates if c['status'] != 'passed'],
        'decision': {'native': 'unqualified', 'reusable': 'source-rejected or install-blocked; no reusable engine executed',
                     'owner': 'unselected', 'automatic_rewrite': False},
        'remaining_gates': ['full T008 capacity and measured defaults',
                            'representative scale/update/query measurements', 'agent', 'independent human UX', 'distribution', 'release'],
        'limitations': ['Parser syntax alone is not call resolution; individual binding/unknown/candidate results retained.',
            'Passing an experiment would not qualify the later human-facing product.',
            'Component mode equivalence does not establish supported semantics, large-corpus throughput or combined process RSS.',
            'Finite fixture cost evidence cannot qualify reference resource budgets or resource defaults.']}
    from evaluations.acceptance import component_selection_decision
    decision = component_selection_decision(result, root=ROOT,
        evidence_root=work_root, source_map=source_map)
    result.update({k: decision[k] for k in ('status', 'engine_selected', 'selected_owner', 'owner_binding',
        'qualification_complete', 'measurement_defaults_qualified', 'selection_scope')})
    result['selection_proofs'] = decision['proofs']
    result['blocking_gates'] += decision['blocking_proofs']
    result['scope'] = decision['selection_scope']
    result['decision'].update(native='experimentally selected' if result['engine_selected'] else 'unqualified',
        owner=result['selected_owner'] or 'unselected')
    if comparison_identity() != captured:
        raise ValueError('Comparison inputs or implementation changed during selection validation')
    return result


def compact_profile_result(result):
    """Export measured counters and every failed path, never arbitrary worker fields."""
    def number(value, integer=False):
        if type(value) not in ((int,) if integer else (int, float)) or value < 0 or not math.isfinite(value):
            raise ValueError('Invalid profile measurement')
        return value
    def counters(value, allowed, integer=False):
        if not isinstance(value, dict) or set(value) - set(allowed.split()):
            raise ValueError('Invalid profile counter fields')
        return {key: number(count, integer) for key, count in value.items()}
    def label(value):
        if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', value):
            raise ValueError('Invalid profile status label')
        return value
    def digest(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('Invalid profile digest')
        return value
    def failures(coverage):
        files = coverage.get('files', [])
        if not isinstance(files, list):
            raise ValueError('Invalid profile file receipts')
        failed = []
        for item in files:
            path, status = item['path'], label(item['status'])
            parts = SourceRoot.parts(path)
            if '\\' in path or ':' in path or '/'.join(parts) != path:
                raise ValueError('Portable profile paths must be canonical relative POSIX paths')
            if status not in ('parsed', 'configuration', 'read', 'metadata_only',
                              'excluded_non_source', 'absent_optional_configuration'):
                failed.append({'path': path, 'status': status})
        return {'file_receipt_count': len(files), 'full_receipts_in_hashed_private_artifact': True}, failed
    allowed = {'engine', 'status', 'error_kind', 'records', 'coverage', 'peak_rss_bytes',
               'deterministic_repeat', 'native_repeat_cache', 'model_calls', 'rss_scope'}
    if not isinstance(result, dict) or set(result) - allowed:
        raise ValueError('Unexpected profile worker fields')
    compact = {key: label(result[key]) for key in ('engine', 'status', 'error_kind') if key in result}
    for key in ('peak_rss_bytes', 'model_calls'):
        if key in result:
            compact[key] = number(result[key], True)
    if 'deterministic_repeat' in result:
        if type(result['deterministic_repeat']) is not bool:
            raise ValueError('Invalid repeat identity flag')
        compact['deterministic_repeat'] = result['deterministic_repeat']
    compact['records'] = []
    if not isinstance(result.get('records', []), list):
        raise ValueError('Invalid worker measurements')
    for item in result.get('records', []):
        row = {'run': label(item['run']), 'status': label(item['status'])}
        if row['run'] not in ('fresh-output', 'unchanged-repeat') or row['status'] not in ('complete', 'partial'):
            raise ValueError('Invalid profile trial')
        row.update({key: number(item[key], key != 'wall_seconds') for key in ('wall_seconds', 'artifact_bytes', 'peak_rss_bytes')})
        row.update({key: digest(item[key]) for key in ('semantic_facts_sha256', 'input_inventory_sha256')})
        row['stages'] = counters(item['stages'], 'repo_files tree_index extract_dependencies catalog system_view write_page inventory_seconds source_bytes nodes_visited facts_emitted parse_seconds elapsed_seconds read_seconds')
        row['source_reads'] = counters(item['source_reads'], 'operations hashed_bytes prefix_bytes', True)
        row['counts'] = counters(item['counts'], 'inventoried_files import_edges indexed_documents code_files selected_source_files excluded_other_files definitions sites', True)
        coverage = item['coverage']
        row['coverage'], row['failed_files'] = failures(coverage)
        row['coverage'].update(counters({k: coverage[k] for k in ('failed', 'truncated', 'scanned', 'reused', 'inventory_failed', 'search_failed', 'search_truncated') if k in coverage},
            'failed truncated scanned reused inventory_failed search_failed search_truncated', True))
        if 'inventory_statuses' in coverage:
            row['coverage']['inventory_statuses'] = {label(k): number(v, True) for k, v in coverage['inventory_statuses'].items()}
        if 'errors' in coverage:
            row['coverage']['error_count'] = len(coverage['errors'])
        compact['records'].append(row)
    if 'coverage' in result:
        compact['coverage'], compact['failed_files'] = failures(result['coverage'])
    return compact


def profile_component(source_map, work_root=None, freeze_budgets=False, recorded_report=None):
    """Capacity observations do not substitute for equivalent-fact/update work."""
    from evaluations.performance import IMPLEMENTATION_PATHS, mapped_corpora, profile_structural
    from evaluations.acceptance import read_json as strict_read_json
    with SourceRoot(source_map.parent) as owner:
        mapping, map_sha = strict_read_json(owner, source_map.name)
    mapped_corpora(mapping)
    with SourceRoot(ROOT) as owner:
        driver_sha = owner.read('evaluations/analysis.py', 1024 * 1024, hash_full=True)[1]
        reporter_helper_sha = owner.read('evaluations/performance.py', 1024 * 1024, hash_full=True)[1]
    if recorded_report is not None:
        with SourceRoot(recorded_report.parent) as owner, owner.open(recorded_report.name) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > 256 * 1024 * 1024:
                raise ValueError('Archived profile exceeds 256 MiB input budget')
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate archived profile key')
                    result[key] = value
                return result
            def finite(_):
                raise ValueError('Non-finite archived profile measurement')
            def parse_float(value):
                number = float(value)
                return number if math.isfinite(number) else finite(value)
            raw = stream.read(256 * 1024 * 1024 + 1)
            if len(raw) > 256 * 1024 * 1024:
                raise ValueError('Archived profile exceeds 256 MiB input budget')
            after = os.fstat(stream.fileno())
            if len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Archived profile changed during read')
            artifact_sha = hashlib.sha256(raw).hexdigest()
            archive = json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=parse_float)
            del raw
        from evaluations.acceptance import PINS as CORPUS_PINS
        if type(archive.get('schema_version')) is not int or archive['schema_version'] != 1 or archive['source_map_sha256'] != map_sha or archive['corpus_revisions'] != {
                name: CORPUS_PINS[name] for name in ('django', 'odoo', 'aws', 'kubernetes')}:
            raise ValueError('Archived profile map or frozen corpus mismatch')
        implementation = archive['implementation']
        if not isinstance(implementation['commit'], str) or not re.fullmatch('[0-9a-f]{40}', implementation['commit']):
            raise ValueError('Archived implementation requires a full commit identity')
        if set(implementation['sha256']) != set(IMPLEMENTATION_PATHS) or implementation['native_backend'] != PINS:
            raise ValueError('Archived profile requires the complete measured implementation manifest')
        for path, sha in implementation['sha256'].items():
            SourceRoot.parts(path)
            raw = subprocess.check_output(['git', 'show', implementation['commit'] + ':' + path], cwd=ROOT, timeout=20)
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError('Archived implementation does not match its recorded commit')
        records = archive['records']
        expected = {f'{name}:{engine}:{run}' for name in ('django', 'odoo', 'aws', 'kubernetes')
                    for engine in ('current-map', 'tree-sitter') for run in range(3)}
        if len(records) != len(expected) or {f"{r['corpus']}:{r['engine']}:{r['repeat']}" for r in records} != expected:
            raise ValueError('Archived profile must retain all twenty-four trials')
        if any(r['implementation_after']['commit'] != implementation['commit'] or
               r['implementation_after']['sha256'] != implementation['sha256'] or
               r['revision'] != CORPUS_PINS[r['corpus']] for r in records):
            raise ValueError('Archived profile has mixed implementations')
        env = archive['environment']
        if (not isinstance(env, dict) or set(env) != {'python', 'platform', 'cpu_count', 'gpu_used'} or
                env['gpu_used'] is not False or (env['cpu_count'] is not None and
                (type(env['cpu_count']) is not int or env['cpu_count'] < 1)) or
                any(not isinstance(env[k], str) or not re.fullmatch('[A-Za-z0-9_. -]{1,100}', env[k]) for k in ('python', 'platform'))):
            raise ValueError('Invalid archived measurement environment')
        checkouts = {}
        for record in records:
            measured = record['implementation_after']
            if (set(measured) != {'commit', 'sha256', 'root_identity'} or
                    measured['root_identity'] != implementation['root_identity'] or
                    not isinstance(measured['root_identity'], str) or not re.fullmatch('[0-9a-f]{64}', measured['root_identity'])):
                raise ValueError('Invalid archived implementation identity fields')
            checkout = record['checkout_before']
            if (not isinstance(checkout, dict) or set(checkout) != {'status', 'actual_revision', 'clean', 'root_identity'} or
                    checkout['status'] != 'verified' or checkout['clean'] is not True or
                    checkout['actual_revision'] != record['revision'] or not isinstance(checkout['root_identity'], str) or
                    not re.fullmatch('[0-9a-f]{64}', checkout['root_identity']) or record['checkout_after'] != checkout or
                    checkouts.setdefault(record['corpus'], checkout) != checkout):
                raise ValueError('Invalid or changed archived source checkout receipt')
    else:
        trial = 'capacity-' + uuid.uuid4().hex[:12]
        with worker_directory(source_map, work_root) as directory:
            artifact = directory / (trial + '.json')
            records = profile_structural(source_map, artifact, directory / (trial + '-logs'))
            with SourceRoot(directory) as owner:
                _, artifact_sha, _ = owner.read(artifact.name, 0, hash_full=True)
    with SourceRoot(source_map.parent) as owner:
        if strict_read_json(owner, source_map.name)[1] != map_sha:
            raise ValueError('Private source map changed during profiling; receipts retained')
    if not records:
        raise ValueError('No measured worker records')
    with SourceRoot(ROOT) as owner:
        if (owner.read('evaluations/analysis.py', 1024 * 1024, hash_full=True)[1] != driver_sha or
                owner.read('evaluations/performance.py', 1024 * 1024, hash_full=True)[1] != reporter_helper_sha):
            raise ValueError('Profile driver changed; worker receipts retained')
    _, identity = frozen_inputs(ROOT)
    cases = []
    for record in records:
        if (type(record['identity_verified']) is not bool or type(record['exit_code']) is not int or
                type(record['repeat']) is not int or type(record['worker_wall_seconds']) not in (int, float) or
                not math.isfinite(record['worker_wall_seconds']) or record['worker_wall_seconds'] < 0 or
                any(not isinstance(record[k], str) or not re.fullmatch('[0-9a-f]{64}', record[k]) for k in ('stdout_sha256', 'stderr_sha256'))):
            raise ValueError('Invalid measured worker identity, resources or log digest')
        result = compact_profile_result(record['result'])
        if record['exit_code'] == 0 and (len(result['records']) != 2 or
                {r['run'] for r in result['records']} != {'fresh-output', 'unchanged-repeat'}):
            raise ValueError('Successful worker must retain both measured trials')
        cases.append({'id': f"{record['corpus']}:{record['engine']}:{record['repeat']}", 'exit_code': record['exit_code'],
            'corpus': record['corpus'], 'engine': record['engine'], 'repeat': record['repeat'],
            'revision': record['revision'], 'identity_verified': record['identity_verified'],
            'status': 'passed' if record['identity_verified'] and record['exit_code'] == 0 and
                result['records'] and all(x['status'] == 'complete' for x in result['records']) else 'failed',
            'worker_wall_seconds': record['worker_wall_seconds'], 'stdout_sha256': record['stdout_sha256'],
            'stderr_sha256': record['stderr_sha256'], 'result': result})
    return {'schema_version': 1, 'experiment': 'capacity-profiling', 'status': 'blocked',
        'source_identity': identity, 'scope': 'Capacity baselines with different output workloads; no equivalent-workload gain',
        'case_results': cases, 'private_artifact_sha256': artifact_sha,
        'source_map_sha256': map_sha, 'implementation': records[0]['implementation_after'],
        'reporting_implementation': {'analysis_sha256': driver_sha, 'performance_sha256': reporter_helper_sha,
                                     'from_archive': recorded_report is not None},
        'native_backend': archive['implementation']['native_backend'] if recorded_report is not None else
                          {name: metadata.version(name) for name in PINS},
        'environment': archive['environment'] if recorded_report is not None else environment(),
        'engine_selected': False, 'qualification_complete': False,
        'budget_freeze': {'requested': freeze_budgets, 'status': 'blocked',
            'reason': 'Equivalent-fact reference, one-file/dependent updates and scoped query measurements incomplete'},
        'rust': {'adopted': False, 'prototype_built': False, 'speed_gain_measured': False},
        'remaining_gates': ['equivalent-fact profiling', 'one-file/dependent updates', 'query p50/p95/work budgets',
                            'immutable reference-based large-corpus budgets'],
        'limitations': ['Import maps and native callable facts have different outputs; no speedup ratio is valid.',
            'All partial parses, failed workers and individual measured trials remain evidence.',
            'Rust adoption requires separate approval and ADR0006 measured equivalent-workload thresholds.']}


def profile_fixture_pilot(root, evidence_directory, *, protocol=None, original_source=None, repetition=1):
    """Launch the finite pilot in its own bounded supervisor, retaining raw logs."""
    from evaluations import engine_checks as checks
    from evaluations.performance import _persistent_repetition
    from evaluations.supplement_preparation import decode
    if evidence_directory is None:
        raise ValueError('Private --work-root or REPO_GRAPH_EVAL_WORK_ROOT required')
    root = checks._adapter_root(root)
    if protocol is None and original_source is not None:
        raise ValueError('Original source requires a preregistered protocol')
    if protocol is None: _persistent_repetition(None, repetition)
    if protocol is not None:
        if original_source is None:
            raise ValueError('Representative protocol requires the pinned original source')
        destination = Path(evidence_directory).resolve(strict=True)
        for protected in (Path(original_source).resolve(strict=True), Path(protocol).resolve(strict=True)):
            if destination == protected or protected in destination.parents:
                raise ValueError('Representative evidence must be outside source and protocol inputs')
    with ExitStack() as holds, checks._adapter_run(root, evidence_directory, 'persistent-pilot-command') as (run, name), SourceRoot(run) as owner:
        bridge = Path('/proc') / str(os.getpid()) / 'fd' / str(owner.fd)
        process, result = None, {'schema_version': 1, 'kind': 'persistent_fixture_profile', 'status': 'failed',
            'engine_selected': False, 'qualification_complete': False, 'resource_budgets_frozen': False,
            'representative_corpus_profiled': False, 'all_owned_source_reads_measured': False, 'cases': []}
        receipt = {'schema_version': 1, 'returncode': None}
        try:
            command = [sys.executable, '-I', '-B', str(root / 'evaluations/performance.py'),
                '--persistent-supervisor', str(Path('/proc/self/fd') / str(owner.fd)), '--creator-pid', str(os.getpid()),
                '--repetition', str(repetition)]
            descriptors, timeout = [owner.fd], 95
            if protocol is None: receipt['repetition'] = repetition
            if protocol is not None:
                from evaluations.performance import _dual_self_identity, _persistent_protocol
                protocol_owner = holds.enter_context(SourceRoot(protocol))
                original_owner = holds.enter_context(SourceRoot(original_source))
                loaded = _persistent_protocol(Path('/proc/self/fd') / str(protocol_owner.fd),
                    Path('/proc/self/fd') / str(original_owner.fd))
                _persistent_repetition(loaded, repetition)
                command.extend(['--protocol-fd', str(protocol_owner.fd), '--original-fd', str(original_owner.fd),
                    '--invoker', json.dumps(_dual_self_identity(), sort_keys=True, separators=(',', ':'))])
                descriptors.extend([protocol_owner.fd, original_owner.fd])
                timeout = loaded['config']['ceilings']['launcher_wall_seconds']
                receipt.update(protocol_sha256=loaded['protocol_sha256'], protocol_owner=loaded['protocol_owner'],
                    original_owner=loaded['original_owner'], timeout_seconds=timeout,
                    repetition=repetition, planned_repetitions=loaded['config']['planned_repetitions'])
                result.update(kind='persistent_corpus_profile', repetition=repetition,
                    planned_repetitions=loaded['config']['planned_repetitions'], protocol_sha256=loaded['protocol_sha256'])
            with owner.open('stdout.log', create=True) as stdout, owner.open('stderr.log', create=True) as stderr:
                process = subprocess.Popen(command,
                    cwd=bridge, env=checks._environment(bridge), pass_fds=tuple(descriptors),
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                receipt['returncode'] = process.wait(timeout=timeout)
            raw, _, info = owner.read('stdout.log', checks.ADAPTER_REPORT_BYTES + 1, hash_full=False)
            if len(raw) != info.st_size or len(raw) > checks.ADAPTER_REPORT_BYTES:
                raise ValueError('Bounded complete pilot output required')
            observed = decode(raw)
            expected_kind = 'persistent_corpus_profile' if protocol is not None else 'persistent_fixture_profile'
            if (type(observed) is not dict or observed.get('kind') != expected_kind or
                    any(observed.get(key) is not False for key in ('engine_selected', 'qualification_complete',
                        'resource_budgets_frozen', 'all_owned_source_reads_measured')) or
                    protocol is None and observed.get('representative_corpus_profiled') is not False):
                raise ValueError('Finite pilot output required')
            if protocol is not None and (type(observed.get('repetition')) is not int or
                    observed['repetition'] != repetition or type(observed.get('planned_repetitions')) is not int or
                    observed['planned_repetitions'] != loaded['config']['planned_repetitions'] or
                    observed.get('protocol_sha256') != loaded['protocol_sha256']):
                raise ValueError('Portable pilot repetition/protocol identity mismatch')
            result = observed
            if receipt['returncode'] != 0:
                result['status'] = 'failed'
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            receipt['failure'] = checks._adapter_error(error)
            result['status'] = 'failed'
        finally:
            receipt['cleanup'] = checks._stop_and_reap(process) if process is not None else None
            if protocol is not None:
                receipt['cleanup_scope'] = 'supervisor_group_only'
                receipt['descendant_cleanup'] = ('see_supervisor_report' if
                    receipt['returncode'] == 0 and result['status'] == 'complete' else 'unknown')
            if not receipt['cleanup'] or not all(receipt['cleanup'].get(key) is True for key in ('leader_reaped', 'group_absent')):
                result['status'] = 'cleanup_failed'
            receipt['logs'] = []
            for log in ('stdout.log', 'stderr.log'):
                try:
                    raw, sha, info = owner.read(log, checks.ADAPTER_REPORT_BYTES + 1, hash_full=False)
                    receipt['logs'].append({'path': log, 'sha256': sha, 'bytes': info.st_size,
                        'complete': len(raw) == info.st_size and len(raw) <= checks.ADAPTER_REPORT_BYTES})
                except OSError as error:
                    receipt['logs'].append({'path': log, 'error_kind': type(error).__name__})
            checks._adapter_dump(run, 'command.json', receipt)
            checks._adapter_dump(run, 'result.json', result)
        return result


def django_framework(root=ROOT, budget=None, *, source_map=None, work_root=None):
    """Frozen source questions graded after the single structural owner runs."""
    from evaluations.acceptance import committed
    root, budget = Path(root), budget or Budget()
    with SourceRoot(root) as owner:
        manifest, input_sha = read_json(owner, INPUTS + 'django-framework-inputs.json')
        review, review_sha = read_json(owner, INPUTS + 'django-framework-review.json')
        if (input_sha != '2348603f592660275f9a5750c18fe3e08c1679516333e7613fef9f78589f26c9' or
                review_sha != '93fadbc0202f5354890de3ee8a55670ca753b2f4e59ee42c0cf10898ddc86bbf' or
                review['status'] != 'admitted_frozen_source_key' or review['task'] != 'T018' or
                review['input_manifest']['sha256'] != input_sha):
            raise ValueError('Independently admitted Django source key required')
        hashes = {INPUTS + 'django-framework-inputs.json': input_sha, INPUTS + 'django-framework-review.json': review_sha}
        original = {}
        for record in manifest['synthetic_inventory']:
            path = record['path']; SourceRoot.parts(path)
            full = manifest['fixture_root'] + '/' + path
            raw, sha, info = owner.read(full, budget.max_file_bytes + 1, hash_full=False)
            if len(raw) != info.st_size or sha != record['sha256'] or len(raw) != record['bytes']:
                raise ValueError('Frozen Django fixture bytes differ')
            original[path] = raw; hashes[full] = sha
    if not committed(root, hashes):
        raise ValueError('Frozen Django inputs must belong to this committed repository')
    identity = {'input_manifest_sha256': input_sha, 'independent_review_sha256': review_sha,
                'synthetic_inventory_sha256': manifest['synthetic_inventory_sha256'],
                'corpus_revision': manifest['corpus']['revision']}
    code_paths = ('evaluations/analysis.py', 'repo_graph/analysis.py', 'repo_graph/analysis_native.py',
                  'repo_graph/analysis_queue.py', 'repo_graph/analysis_queries.py', 'repo_graph/search.py',
                  'repo_graph/cli.py', 'pyproject.toml', 'uv.lock')
    with SourceRoot(root) as owner:
        implementation = {path: owner.read(path, 1024 * 1024, hash_full=True)[1] for path in code_paths}
    identity['implementation'] = {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                                  'sha256': implementation}
    if source_map is None:
        return dict(schema_version=1, suite='django-framework', status='blocked', source_identity=identity,
                    case_results=[], failures=[], counts={}, reason='Pinned private source map required',
                    qualification_complete=False, limits_qualified=False)
    source_map = Path(source_map)
    with SourceRoot(source_map.parent) as owner:
        mapping, map_sha = read_json(owner, source_map.name)
    selected = [row for row in mapping['corpora'] if row['id'] == 'django']
    if len(selected) != 1 or selected[0]['revision'] != manifest['corpus']['revision']:
        raise ValueError('Pinned Django source-map identity differs')
    from repo_graph.analysis import StructuralIndex, _git_capture
    from repo_graph.analysis_queries import Queries
    from repo_graph.search import Search, captured_source, connect
    from evaluations.engine_checks import _adapter_materialize
    real = {}
    with SourceRoot(Path(selected[0]['source'])) as owner:
        prefix = _git_capture(owner, ['rev-parse', '--show-prefix'], 4096, lambda: False)
        revision = _git_capture(owner, ['rev-parse', '--verify', 'HEAD'], 128, lambda: False)
        if prefix is None or prefix.strip() or revision is None or revision.decode().strip() != selected[0]['revision']:
            raise ValueError('Django source must own the exact pinned repository revision')
        for record in manifest['corpus']['selected_file_inventory']:
            raw, sha, info = owner.read(record['path'], budget.max_file_bytes + 1, hash_full=False)
            if sha != record['sha256'] or len(raw) != record['bytes'] or len(raw) != info.st_size:
                raise ValueError('Pinned Django source bytes differ')
            real[record['path']] = raw
    checks, receipts, query_traces = [], [], []
    def require(value, message):
        if not value: raise AssertionError(message)
    def check(identifier, action, observed=None):
        row = {'id': identifier, 'status': 'passed', **(observed or {})}
        try: row.update(action() or {})
        except Exception as error: row.update(status='failed', error_kind=type(error).__name__, reason=str(error)[:1024])
        checks.append(row)
    def normalized(index):
        facts = {key: sorted(index.read_facts(key), key=lambda row: json.dumps(row, sort_keys=True))
                 for key in ('definitions', 'sites', 'scopes', 'imports', 'relationships', 'evidence')}
        with closing(connect(index.output, readonly=True, owner=index.output_owner)) as db:
            facts['dependencies'] = [dict(row) for row in db.execute('SELECT * FROM structural_dependencies ORDER BY path,kind,key')]
            facts['import_relationships'] = [dict(row) for row in db.execute('SELECT * FROM structural_import_relationships ORDER BY path,ordinal,target_path')]
        return facts
    def inspect(index, receipt, facts):
        engine = Search(index.output)
        try:
            slices = []
            for role in ('definitions', 'sites', 'evidence'):
                for row in facts[role]:
                    handle = {key: row[key] for key in ('id', 'path', 'range')}
                    handle['source_sha256'] = row['provenance']['source_sha256']
                    result = captured_source(engine, dict(generation=receipt['generation'], handle=handle, max_excerpt_bytes=4096))
                    slices.append({key: result[key] for key in ('handle', 'text', 'range', 'raw_digest', 'truncated', 'redacted', 'certainty')})
            facts['source_slices'] = sorted(slices, key=lambda row: json.dumps(row, sort_keys=True))
        finally: engine.close()
    def query_rows(queries, request, page=None):
        page = queries.run(request) if page is None else page
        rows, pages = [], []
        for _ in range(128):
            rows.extend(page['rows'])
            pages.append({key: page[key] for key in ('generation', 'returned_edges', 'examined_relationships',
                'total_count', 'truncated', 'stop_reason', 'coverage')})
            if not page['cursor']: break
            page = queries.run(request | {'cursor': page['cursor']})
        require(not page['truncated'] and page['stop_reason'] is None, 'Framework query did not exhaust under its finite bounds')
        return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)), pages
    def produce(source, output, blobs, context, mode):
        index = StructuralIndex(source, output, budget=budget, framework_context=context)
        receipt = index.refresh(sorted(blobs), mode=mode, concurrency=1 if mode == 'serial' else 2)
        receipts.append({'mode': mode, 'status': receipt['status'], 'coverage': receipt.get('coverage'), 'resources': receipt['resources']})
        require(receipt['status'] == 'ready', 'Shared index did not publish a coherent generation')
        facts = normalized(index); inspect(index, receipt, facts)
        with Queries(index.output) as queries:
            rows, pages = query_rows(queries, {'operation': 'framework'})
        facts['framework_query_rows'] = rows
        query_traces.append({'mode': mode, 'generation': receipt['generation'], 'source_identity': receipt['source_identity'],
            'row_count': len(rows), 'rows_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            'pages': pages})
        return index, facts
    def grade_cases(facts, source_kind, mode):
        definitions = {row['id']: row for row in facts['definitions']}
        for expected in manifest['cases']:
            if expected['source_kind'] != source_kind: continue
            origin = expected['origin']
            found = [row for row in facts['sites'] if row['role'].startswith('framework') and row['path'] == origin['path']
                     and all(row['range'][name] == origin['range'][name] for name in ('start_byte', 'end_byte'))]
            expected_targets = {(row['path'], row['range']['start_byte'], row['range']['end_byte'], row['source_sha256'], row['name'])
                                for row in expected['expected']['targets']}
            actual_targets = {(row['path'], row['range']['start_byte'], row['range']['end_byte'], row['provenance']['source_sha256'], row['name'])
                              for site in found for target in site['targets'] if (row := definitions.get(target)) is not None}
            matches = len(expected_targets & actual_targets)
            observed = {'occurrences': len(found), 'expected_targets': len(expected_targets), 'actual_targets': len(actual_targets),
                'target_precision': matches / len(actual_targets) if actual_targets else None,
                'target_recall': matches / len(expected_targets) if expected_targets else None,
                'metric_scope': 'this frozen source case only; unjudged occurrences excluded',
                'actual': [{name: row[name] for name in ('id', 'range', 'family', 'relation_kind', 'targets', 'certainty', 'reason', 'provenance')} for row in found]}
            def grade(expected=expected):
                origin = expected['origin']; key = ('start_byte', 'end_byte')
                found = [row for row in facts['sites'] if row['role'].startswith('framework') and row['path'] == origin['path']
                         and all(row['range'][name] == origin['range'][name] for name in key)]
                require(len(found) == expected['expected']['source_row_count'], 'Physical framework occurrence cardinality differs')
                if found:
                    row = found[0]; label = expected['expected']
                    require(row['family'] == expected['relation_family'] and row['relation_kind'] == expected['relation_kind'], 'Relation family/kind differs')
                    require(row['certainty'] == label['certainty'] and row['framework_identity_asserted'] == label['framework_identity_asserted'], 'Certainty/identity differs')
                    require(row['partial'] == label['partial'] and row['provenance']['source_sha256'] == origin['source_sha256'], 'Physical source/partial state differs')
                    require(len(row['targets']) == label['target_cardinality'], 'Target cardinality differs')
                    for target_id, target in zip(row['targets'], label['targets']):
                        actual = definitions[target_id]
                        require(actual['path'] == target['path'] and actual['name'] == target['name'] and
                            actual['provenance']['source_sha256'] == target['source_sha256'] and all(actual['range'][name] == target['range'][name]
                            for name in ('start_byte', 'end_byte', 'start_line', 'end_line')), 'Target declaration/source range differs')
                    require(any(item['source_role'] == 'candidate_import' for item in row['evidence']), 'Candidate import witness missing')
            check(mode + ':' + expected['id'], grade, observed)
    contexts = manifest['source_admission']['frozen_contexts']
    with worker_directory(source_map, work_root) as work:
        with tempfile.TemporaryDirectory(prefix='django-framework-', dir=work) as scratch:
            scratch = Path(scratch)
            for source_kind, blobs, context in (('synthetic', original, contexts['synthetic']), ('pinned_corpus', real, contexts['pinned_django'])):
                source = scratch / source_kind; source.mkdir(); _adapter_materialize(source, blobs)
                mode_facts = {}
                for mode in ('serial', 'queued'):
                    index, facts = produce(source, scratch / (source_kind + '-' + mode), blobs, context, mode)
                    mode_facts[mode] = facts
                    grade_cases(facts, source_kind, mode)
                    check(mode + ':' + source_kind + ':query-membership', lambda: require(
                        {row['site']['id'] for row in facts['framework_query_rows']} == {row['id'] for row in facts['sites'] if row['role'].startswith('framework')}, 'Framework pagination lost/added occurrences'))
                check(source_kind + ':mode-parity', lambda: require(mode_facts['serial'] == mode_facts['queued'], 'Serial/queued shared facts or source evidence differ'))
                if source_kind != 'synthetic': continue
                for number, mutation in enumerate(manifest['incremental_mutations']):
                    changed = dict(original)
                    origins = {case['id']: dict(path=case['origin']['path'], start=case['origin']['range']['start_byte'])
                               for case in manifest['cases'] if case['source_kind'] == 'synthetic'}
                    for operation in mutation['operations']:
                        path = operation['path']; verb = operation['operation']
                        if verb != 'add': require(hashlib.sha256(changed[path]).hexdigest() == operation['before_sha256'], 'Mutation baseline digest differs')
                        if verb == 'delete': del changed[path]; continue
                        if verb == 'rename': path = operation['destination']; changed[path] = changed.pop(operation['path'])
                        elif verb == 'add': changed[path] = operation['content'].encode()
                        elif verb == 'replace':
                            old, new = operation['old'].encode(), operation['new'].encode()
                            require(changed[path].count(old) == 1, 'Mutation must replace one reviewed slice')
                            offset = changed[path].index(old)
                            for origin in origins.values():
                                if origin['path'] == path and offset + len(old) <= origin['start']:
                                    origin['start'] += len(new) - len(old)
                            changed[path] = changed[path].replace(old, new, 1)
                        else: raise ValueError('Unknown frozen mutation')
                        require(hashlib.sha256(changed[path]).hexdigest() == operation['after_sha256'] and len(changed[path]) == operation['after_bytes'], 'Mutation result digest differs')
                    mutated_context = json.loads(json.dumps(context))
                    for name in ('consumer', 'dependency'): mutated_context[name]['revision'] = mutation['result_inventory_sha256']
                    with ExitStack() as held_context:
                        held = {}
                        request = {'operation': 'framework', 'limits': {'max_edges': 1}}
                        for mode in ('serial', 'queued'):
                            queries = held_context.enter_context(Queries(scratch / (source_kind + '-' + mode)))
                            held[mode] = queries, queries.run(request)
                        _adapter_materialize(source, changed, removed=set(original) - set(changed))
                        for mode in ('serial', 'queued'):
                            output = scratch / (source_kind + '-' + mode)
                            updated_index, updated = produce(source, output, changed, mutated_context, mode)
                            _, clean = produce(source, scratch / f'clean-{number}-{mode}', changed, mutated_context, mode)
                            check(mode + ':' + mutation['id'] + ':clean-parity', lambda: require(updated == clean, 'Incremental facts/dependencies/captured slices differ from clean rebuild'))
                            def continuation(mode=mode):
                                queries, first = held[mode]
                                try:
                                    require(first['cursor'] is not None, 'Named mutation did not exercise a continuation')
                                    with Queries(output) as fresh:
                                        try: fresh.run(request | {'cursor': first['cursor']})
                                        except ValueError: pass
                                        else: raise AssertionError('Fresh session accepted a prior snapshot cursor')
                                    rows, pages = query_rows(queries, request, first)
                                    require(rows == mode_facts[mode]['framework_query_rows'], 'Held snapshot rows changed after refresh')
                                    require(all(page['generation'] == first['generation'] for page in pages) and
                                        first['generation'] != updated_index.last_attempt['generation'], 'Held/fresh generation identity differs incorrectly')
                                    return {'held_generation': first['generation'], 'fresh_generation': updated_index.last_attempt['generation'],
                                        'held_row_count': len(rows), 'held_rows_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
                                        'pages': pages, 'fresh_session_rejected_prior_cursor': True}
                                finally: queries.close()
                            check(mode + ':' + mutation['id'] + ':held-generation-continuation', continuation)
                            judgments = {key: value for key, value in mutation['expected'].items() if key.startswith('DJ-')}
                            for group in ('listed_cases', 'facade_cases'):
                                if group in mutation['expected']:
                                    for case_id in mutation['affected_cases']:
                                        if case_id not in judgments: judgments[case_id] = mutation['expected'][group]
                            if mutation['expected'].get('all_framework_judgments') == 'unchanged':
                                check(mode + ':' + mutation['id'] + ':unchanged-judgments', lambda: require(
                                    updated['sites'] == mode_facts[mode]['sites'], 'Configuration changed framework or lexical judgments'))
                            if 'source_parses_for_unchanged_files' in mutation['expected']:
                                check(mode + ':' + mutation['id'] + ':unchanged-collections', lambda: require(
                                    updated_index.last_attempt['resources']['changed_files_collected'] == 0, 'Configuration update recollected unchanged source'))
                            for case_id, expected in judgments.items():
                                def grade_mutation(case_id=case_id, expected=expected):
                                    baseline = next(case for case in manifest['cases'] if case['id'] == case_id)
                                    origin = origins[case_id]
                                    rows = [row for row in updated['sites'] if row['role'].startswith('framework') and row['path'] == origin['path']
                                            and row['range']['start_byte'] == origin['start']]
                                    require(len(rows) == 1 and rows[0]['certainty'] == expected['certainty'] and len(rows[0]['targets']) == expected['target_cardinality'], 'Mutation certainty/target count differs')
                                    if 'required_row_family' in expected: require(rows[0]['family'] == expected['required_row_family'], 'Mutation row family differs')
                                    if 'reason_code' in expected: require(rows[0]['reason'] == expected['reason_code'], 'Mutation boundary reason differs')
                                    if expected.get('partial'):
                                        require(rows[0]['partial'] and rows[0]['partial_source_role'] == expected['partial_source_role'], 'Partial callback boundary provenance missing')
                                    if expected['target_cardinality']:
                                        target = next(row for row in updated['definitions'] if row['id'] == rows[0]['targets'][0])
                                        before = baseline['expected']['targets']
                                        require(target['path'] == expected.get('target_path', before[0]['path'] if before else None) and
                                            target['name'] == expected.get('target_name', before[0]['name'] if before else None), 'Mutation target declaration differs')
                                check(mode + ':' + mutation['id'] + ':' + case_id, grade_mutation)
                    _adapter_materialize(source, original, removed=set(changed) - set(original))
                    for mode in ('serial', 'queued'):
                        _, restored = produce(source, scratch / (source_kind + '-' + mode), original, context, mode)
                        check(mode + ':' + mutation['id'] + ':restore-parity', lambda: require(restored == mode_facts[mode], 'Restored source facts/dependencies/captured slices differ'))
    with SourceRoot(source_map.parent) as owner:
        require(read_json(owner, source_map.name)[1] == map_sha, 'Private source map changed during finite evaluation')
    with SourceRoot(root) as owner:
        after = {path: owner.read(path, 1024 * 1024, hash_full=True)[1] for path in code_paths}
    check('implementation-stable', lambda: require(after == implementation, 'Implementation changed during finite evaluation'))
    failures = [row for row in checks if row['status'] != 'passed']
    return dict(schema_version=1, suite='django-framework', status='failed' if failures else 'passed',
        source_identity=identity, source_map_sha256=map_sha, case_results=checks, failures=failures, coverage_failures=[],
        counts=dict(frozen_cases=len(manifest['cases']), mutation_phases=len(manifest['incremental_mutations']),
                    checks=len(checks), passed=len(checks) - len(failures)), receipts=receipts, query_traces=query_traces, environment=environment(),
        qualification_complete=False, limits_qualified=False, scope='Finite opt-in Django source registrations and explicit unknown boundaries; '
        'shared serial/queued facts, clean/update/restore and captured source/query parity. Runtime order, full business paths, scale and human UX unqualified.')



def contract_inputs(root=ROOT):
    """Admitted synthetic source bytes; read judgments before any producer runs."""
    from evaluations.acceptance import committed
    root = Path(root)
    with SourceRoot(root) as owner:
        manifest, input_sha = read_json(owner, INPUTS + 'contract-inputs.json')
        review, review_sha = read_json(owner, INPUTS + 'contract-review.json')
        if (input_sha != '404969d67cd2ba621e3355e3de4346c84d6179c653307ce4d211d10f3f967e96' or
                review_sha != '3060839500456a14e5494a840f265364e436cbf7f5ef2d57215ec3df3f8156c9' or
                review['status'] != 'admitted_frozen_source_key' or review['task'] != 'T020' or
                review['input_manifest']['sha256'] != input_sha):
            raise ValueError('Admitted contract source key required before extraction')
        hashes = {INPUTS + 'contract-inputs.json': input_sha, INPUTS + 'contract-review.json': review_sha}
        original = {}
        for row in manifest['synthetic_inventory']:
            full = manifest['fixture_root'] + '/' + row['path']
            raw, sha, info = owner.read(full, 1024 * 1024, hash_full=True)
            if sha != row['sha256'] or len(raw) != row['bytes'] or info.st_size != row['bytes']:
                raise ValueError('Frozen contract fixture bytes differ')
            original[row['path']] = raw; hashes[full] = sha
    if not committed(root,hashes): raise ValueError('Contract inputs must match committed reviewed bytes')
    return manifest, original, dict(input_manifest_sha256=input_sha, independent_review_sha256=review_sha,
        inventory_sha256=manifest['synthetic_inventory_sha256'], profile_sha256=hashlib.sha256(original['bindings.json']).hexdigest())


def contract_inventory(blobs):
    return [dict(path=path,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),encoding='utf-8',
        kind='source' if path.endswith(('.py','.ts','.go')) else 'configuration',
        language='python' if path.endswith('.py') else 'typescript' if path.endswith('.ts') else 'go' if path.endswith(('.go','go.mod')) else 'contract',
        role='original_synthetic_source' if path.endswith(('.py','.ts','.go')) else 'original_reviewed_binding_profile' if path=='bindings.json' else 'original_contract_source' if path.endswith(('.proto','.json')) else 'configuration')
        for path,raw in sorted(blobs.items())]


def contract_inventory_identity(blobs):
    return hashlib.sha256(json.dumps(contract_inventory(blobs),ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def contract_context(root, blobs, review_sha, revision):
    with SourceRoot(root) as owner: identity = owner.identity
    profile = json.loads(blobs['bindings.json'])
    return dict(schema_version=1,enabled=True,policy_id='reviewed-explicit-contracts-v1',
        consumer=dict(repository_id='contracts-synthetic',revision=revision,source_root_id=identity),
        profiles=[dict(relative_path='bindings.json',sha256=hashlib.sha256(blobs['bindings.json']).hexdigest(),
                       byte_limit=65536,external_review_receipt_id=review_sha)],
        services=[dict(service_id=s['service_id'],source_prefix=s['source_prefix'],source_root_id=identity) for s in profile['services']])


def contract_mutation(original, mutation):
    """Apply only independently frozen byte/JSON edits, never renew stale witnesses."""
    blobs = dict(original)
    for operation in mutation['operations']:
        path,kind = operation['path'],operation['operation']
        if kind != 'add' and hashlib.sha256(blobs[path]).hexdigest() != operation['before_sha256']:
            raise ValueError('Contract mutation preimage differs')
        if kind=='edit':
            old,new = operation['before_utf8'].encode(),operation['after_utf8'].encode()
            if blobs[path].count(old)!=1: raise ValueError('Ambiguous frozen edit')
            blobs[path]=blobs[path].replace(old,new)
        elif kind=='add': blobs[path]=operation['content_utf8'].encode()
        elif kind=='delete': del blobs[path]
        elif kind=='rename': blobs[operation['new_path']]=blobs.pop(path)
        else: raise ValueError('Unknown frozen mutation')
        target=operation.get('new_path',path)
        if 'after_sha256' in operation and hashlib.sha256(blobs[target]).hexdigest()!=operation['after_sha256']:
            raise ValueError('Contract mutation output differs')
    if mutation.get('profile_json_patches'):
        profile=json.loads(blobs['bindings.json'])
        for patch in mutation['profile_json_patches']:
            fields=patch['path'].split('/')[1:]; container=profile
            for field in fields[:-1]:container=container[int(field)] if isinstance(container,list) else container[field]
            final=fields[-1]
            if final=='-' and patch['op']=='add':container.append(patch['value'])
            elif isinstance(container,list):container[int(final)]=patch['value']
            else:container[final]=patch['value']
        blobs['bindings.json']=(json.dumps(profile,ensure_ascii=False,indent=2)+'\n').encode()
        if hashlib.sha256(blobs['bindings.json']).hexdigest()!=mutation['profile_after_sha256']:
            raise ValueError('Reviewed mutation profile differs')
    if contract_inventory_identity(blobs)!=mutation['result_inventory_sha256']:
        raise ValueError('Frozen mutation inventory differs')
    return blobs


def contract_facts(index):
    """Finite synthetic full-parity oracle; production queries stay targeted SQL."""
    result={kind:sorted(index.read_facts(kind),key=lambda row:json.dumps(row,sort_keys=True))
            for kind in ('definitions','sites','scopes','imports','relationships','evidence')}
    from repo_graph.search import connect
    with closing(connect(index.output,readonly=True,owner=index.output_owner)) as db:
        for table in ('structural_dependencies','structural_import_relationships','structural_contract_memberships'):
            result[table]=[dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY path')]
            result[table].sort(key=lambda row:json.dumps(row,sort_keys=True,default=lambda value:value.decode() if isinstance(value,bytes) else value))
    return result


def contract_pages(output, **filters):
    from repo_graph.analysis_queries import Queries
    with Queries(output) as queries:
        payload=dict(operation='contract',limits=dict(max_edges=1),**filters)
        rows=[]; pages=[]
        for _ in range(64):
            page=queries.run(payload); pages.append(page); rows.extend(page['rows'])
            if not page['cursor']:
                if page['truncated']:raise ValueError('Frozen bounded contract query stopped before exhaustion')
                break
            payload['cursor']=page['cursor']
        else:raise ValueError('Contract continuation cap exceeded')
    if len({r['site']['id'] for r in rows})!=len(rows):raise ValueError('Duplicate contract occurrence page')
    return rows,pages


def contract_impact_inputs(root=ROOT):
    """The independently admitted finite oracle must precede producer execution."""
    from evaluations.acceptance import committed
    manifest, original, identity = contract_inputs(root)
    paths = {INPUTS + 'contract-impact-inputs.json': '3aa2c2a22185a449e630056c020e9de160097b4fe4b55dc4496784c8840423f6',
             INPUTS + 'contract-impact-review.json': 'c858f16420459982f92e0a747bfe16e4e5403eb17ce6c9e3ed9750443e84e6a6'}
    with SourceRoot(root) as source:
        oracle, sha = read_json(source, INPUTS + 'contract-impact-inputs.json')
        review, review_sha = read_json(source, INPUTS + 'contract-impact-review.json')
    if (sha != paths[INPUTS + 'contract-impact-inputs.json'] or review_sha != paths[INPUTS + 'contract-impact-review.json'] or
            review.get('input_manifest', {}).get('sha256') != sha or review.get('correctness', {}).get('status') != 'passed' or
            review.get('adversarial', {}).get('status') != 'passed' or review.get('implementation_approval') is not False or
            not committed(root, paths)):
        raise ValueError('Committed independently admitted contract-impact source oracle required')
    identity.update(impact_input_sha256=sha, impact_source_review_sha256=review_sha)
    return oracle, manifest, original, identity


def contract_impact_pages(output, request, *, max_edges=1):
    from repo_graph.analysis_queries import Queries
    payload = dict(operation='impact', role='all', **request)
    payload['limits'] = dict(max_edges=max_edges)
    rows, pages = [], []
    with Queries(output) as queries:
        for _ in range(128):
            page = queries.run(payload); pages.append(page); rows.extend(page['rows'])
            if not page['cursor']:
                if page['truncated']: raise ValueError('Frozen contract impact stopped before exhaustion')
                break
            payload['cursor'] = page['cursor']
        else: raise ValueError('Contract impact continuation cap exceeded')
    if len({(r['relation'],r['site']['id'],r['target']['id'] if r['target'] else None) for r in rows}) != len(rows):
        raise ValueError('Duplicate physical contract-impact occurrence')
    return rows, pages


def _contract_impact_trace(rows, pages, request, actual_rows, source_evidence):
    """Lossless report projection; repeated physical witnesses are stored once."""
    references = []
    for row in rows:
        witness_references = []
        for witness in row['evidence']:
            key = hashlib.sha256(json.dumps(witness,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if key in source_evidence and source_evidence[key] != witness: raise ValueError('Contract witness projection collision')
            source_evidence[key] = witness; witness_references.append(key)
        projected = dict({k:v for k,v in row.items() if k!='evidence'}, evidence_references=witness_references)
        key = hashlib.sha256(json.dumps(projected,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if key in actual_rows and actual_rows[key] != projected: raise ValueError('Contract row projection collision')
        actual_rows[key] = projected; references.append(key)
    return dict(request=request,row_references=references,
        snapshot={key:pages[0][key] for key in ('generation','repository_identity','source_identity','analyzer_identity','config_identity','impact_identity')})


def contract_impact_grade(index, oracle, manifest, state):
    cases = {case['profile_binding_id']:case for case in manifest['cases'] if 'profile_binding_id' in case}
    mutation = next((m for m in manifest['incremental_mutations'] if m['id'] == state), None)
    results = []
    for assertion in oracle['proposed_assertions']:
        if assertion['state'] != state: continue
        rows, pages = contract_impact_pages(index.output, assertion['request'])
        observed = {cases[r['binding_id']]['id']:r for r in rows}
        expected = assertion['expected']; selected = set(expected['contract_case_ids'])
        checks = dict(selected_membership=set(observed) == selected, no_duplicates=len(rows) == len(observed),
            explicit_contract=all(r['relation'] == 'contract' for r in rows),
            captured_affinity=all(p['generation'] == index.metadata()['generation'] and p['contracts_available'] and
                p['contract_membership_schema'] == 'captured-contract-membership-v1' for p in pages),
            bounded_pages=all(p['returned_edges'] <= 1 and p['examined_work'] <= 10000 and
                p['returned_entities'] <= 50 and len(json.dumps(p,sort_keys=True,separators=(',',':')).encode()) <= 32768 for p in pages))
        target_gold = expected.get('targets_by_case', {case_id:[expected['target_id']] for case_id in selected} if 'target_id' in expected else {})
        tp = fp = fn = 0
        for case_id in selected | set(observed):
            row = observed.get(case_id); gold = set(target_gold.get(case_id, []))
            actual = {row['target']['id']} if row and row['target'] else set()
            tp += len(gold & actual); fp += len(actual - gold); fn += len(gold - actual)
            checks['targets:' + case_id] = actual == gold
            if row:
                case = next(c for c in manifest['cases'] if c['id'] == case_id)
                frozen = dict(case['expected'])
                if mutation: frozen.update(mutation['expected_cases'].get(case_id, {}))
                checks['certainty:' + case_id] = row['certainty'] == frozen['certainty']
                if state == 'baseline' or mutation and 'reason' in mutation['expected_cases'].get(case_id, {}):
                    checks['reason:' + case_id] = row['reason'] == frozen['reason']
        if expected.get('selected_path_exists') is False or expected.get('captured_file_handle', True) is None:
            checks['no_fabricated_missing_handle'] = not any(p['selected_files'] for p in pages)
        results.append(dict(id=assertion['id'],state=state,status='passed' if all(checks.values()) else 'failed',checks=checks,
            # Complete actual evidence is retained once in query_traces[state].
            # Each assertion keeps its own physical selection and measured bounds.
            observed_rows=[dict(binding_id=r['binding_id'],site_id=r['site']['id'],source_sha256=r['site']['source_sha256'],
                caller_id=r['caller']['id'] if r['caller'] else None,target_id=r['target']['id'] if r['target'] else None,
                certainty=r['certainty'],targets_exhaustive=r['targets_exhaustive'],reason=r['reason']) for r in rows],
            query_trace_state=state, selected_case_ids=sorted(observed), source_reference_sets=assertion['source_reference_sets'],
            snapshot={key:pages[0][key] for key in ('generation','repository_identity','source_identity','analyzer_identity','config_identity','impact_identity')},
            scope=pages[0]['scope'],
            pages=[dict(response_bytes=len(json.dumps(p,sort_keys=True,separators=(',',':')).encode()),
                **{key:p[key] for key in ('selected_files','selected_symbols','unavailable_paths','unknown_boundaries','total_count',
                'examined_work','returned_entities','returned_edges','stop_reason','truncated','storage_setup_seconds','snapshot_copy_seconds')}) for p in pages],
            selected_target_metrics=dict(true_positive=tp,false_positive=fp,false_negative=fn,
                precision=tp/(tp+fp) if tp+fp else None,recall=tp/(tp+fn) if tp+fn else None,
                scope='Only frozen selected exact target declarations; conservative dependency membership is not runtime precision')))
    return results


def contract_grade(index, manifest, expected=None):
    rows=[r for r in index.read_facts('sites') if r['role'] in ('contract','contract_boundary')]
    by_id={row['binding_id']:row for row in rows}; results=[]
    definitions={r['id']:r for r in index.read_facts('definitions')}
    for case in manifest['cases']:
        if expected is not None and case['id'] not in expected: continue
        key=case.get('profile_binding_id'); row=by_id.get(key)
        # Mutation judgments name only changed expectations. Do not carry a
        # baseline unknown reason into an independently frozen positive change.
        gold=dict(case['expected'] if expected is None else expected[case['id']]); checks={}
        checks['row_cardinality']=(row is not None)==bool(gold.get('source_row_count',case['expected']['source_row_count']))
        target=None
        if gold['target_cardinality']:
            if gold.get('target_binding_id'):
                matches=[patch['value']['declaration'] for mutation in manifest['incremental_mutations']
                    if mutation['expected_cases']==expected for patch in mutation.get('profile_json_patches',[])
                    if type(patch.get('value')) is dict and patch['value'].get('id')==gold['target_binding_id']]
                if len(matches)!=1:raise ValueError('Frozen added endpoint witness is missing or ambiguous')
                target=matches[0]
            else:target=gold['targets'][0]['declaration']
        expected_targets={f"{target['path']}:{target['range']['start_byte']}:{target['range']['end_byte']}"} if target else set()
        actual_targets=set(row['targets']) if row else set()
        if row is not None:
            checks.update(certainty=row['certainty']==gold['certainty'],targets=len(row['targets'])==gold['target_cardinality'])
            if gold.get('reason') is not None:checks['reason']=row['reason']==gold['reason']
            if gold.get('partial') is not None:checks['partial']=row['partial']==gold['partial']
            if gold.get('boundary_origin'):checks['boundary_origin']=row['boundary_origin']==gold['boundary_origin']
            for gold_key,row_key in (('contract_identity_asserted','contract_identity_asserted'),
                    ('targets_exhaustive_under_assumptions','targets_exhaustive')):
                if gold_key in gold:checks[gold_key]=row[row_key]==gold[gold_key]
            if expected is None:
                origin=case['origin']
                checks['origin_physical_affinity']=(row['path']==origin['path'] and
                    row['range']=={k:origin['range'][k] for k in ('start_byte','end_byte','start_line','end_line')} and
                    row['provenance']['source_sha256']==origin['source_sha256'])
                checks['service_contract_identity']=(row['service_id']==case['origin_service_id'] and
                    row['endpoint_role']==case['origin_role'] and row['contract_identity']==case['declared_contract_identity'])
            if target:
                actual=definitions.get(next(iter(actual_targets))) if len(actual_targets)==1 else None
                checks['target_physical_affinity']=(actual is not None and actual_targets==expected_targets and
                    actual['path']==target['path'] and actual['name']==target['name'] and
                    actual['range']=={k:target['range'][k] for k in ('start_byte','end_byte','start_line','end_line')} and
                    actual['provenance']['source_sha256']==target['source_sha256'])
        true_positive=len(actual_targets & expected_targets)
        results.append(dict(id=case['id'],status='passed' if all(checks.values()) else 'failed',checks=checks,
            observed=None if row is None else {k:row[k] for k in ('binding_id','path','range','certainty','reason','targets','contract_identity','partial','boundary_origin','provenance')},
            expected=gold,target_metrics=dict(true_positive=true_positive,false_positive=len(actual_targets-expected_targets),
                false_negative=len(expected_targets-actual_targets),predicted=len(actual_targets),expected=len(expected_targets),
                precision=true_positive/len(actual_targets) if actual_targets else None,
                recall=true_positive/len(expected_targets) if expected_targets else None,
                scope='Selected frozen physical targets only; unknown and unbound zero-target cases retained')))
    return results


def contracts(root=ROOT,budget=None):
    from repo_graph.analysis import StructuralIndex
    root=Path(root); manifest,original,identity=contract_inputs(root)
    with SourceRoot(root) as owner:
        paths=('repo_graph/analysis.py','repo_graph/analysis_native.py','repo_graph/analysis_queue.py','repo_graph/analysis_queries.py',
            'repo_graph/search.py','repo_graph/source.py','repo_graph/cli.py','evaluations/analysis.py','tests/test_contracts.py','pyproject.toml','uv.lock')
        identity['implementation']=dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
            sha256={path:owner.read(path,1024*1024,hash_full=True)[1] for path in paths})
    results=[]; failures=[]; receipts=[]
    def capture(name, action):
        try: details=action(); results.append(dict(id=name,status='passed',details=details)); return details
        except Exception as error:
            failures.append(dict(id=name,error_kind=type(error).__name__,message=str(error)[:512]))
            results.append(dict(id=name,status='failed',error_kind=type(error).__name__)); return None
    with tempfile.TemporaryDirectory(prefix='contract-evaluation-') as scratch:
        scratch=Path(scratch); source=scratch/'source'; source.mkdir()
        def write(blobs):
            for path in list(source.rglob('*')):
                if path.is_file():path.unlink()
            for path,raw in blobs.items():
                file=source/path; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(raw)
        def refreshed(output,blobs,mode='serial',prior=None):
            context=contract_context(source,blobs,identity['independent_review_sha256'],contract_inventory_identity(blobs))
            index=StructuralIndex(source,output,budget=budget,contract_context=context)
            receipt=index.refresh([{k:r[k] for k in ('path','language','kind','sha256','bytes')} for r in contract_inventory(blobs)],
                mode=mode,concurrency=2 if mode=='queued' else 1)
            receipts.append(dict(mode=mode,**receipt))
            if receipt['status']!='ready':raise ValueError('Contract producer failed: '+str(receipt))
            return index
        write(original)
        baseline=None
        for mode in ('serial','queued'):
            def baseline_action(mode=mode):
                nonlocal baseline
                index=refreshed(scratch/mode,original,mode)
                cases=contract_grade(index,manifest)
                results.extend(dict(case,mode=mode) for case in cases)
                if any(case['status']!='passed' for case in cases):raise AssertionError('Frozen contract baseline case failed')
                facts=contract_facts(index)
                if baseline is not None and baseline!=facts:raise AssertionError('Serial/queued contract facts differ')
                baseline=facts
                rows,pages=contract_pages(index.output)
                if {r['binding_id'] for r in rows}!={r['binding_id'] for r in facts['sites'] if r['role'] in ('contract','contract_boundary')}:
                    raise AssertionError('Contract paged union differs')
                return dict(rows=len(rows),pages=len(pages),resolved=sum(r['certainty']=='resolved' for r in rows),unresolved=sum(r['certainty']=='unresolved' for r in rows),
                    actual_query_rows=rows, pages_identity=[dict(generation=p['generation'],source_identity=p['source_identity'],analyzer_identity=p['analyzer_identity'],config_identity=p['config_identity'],
                        returned_edges=p['returned_edges'],examined_relationships=p['examined_relationships'],limits=p['limits'] if 'limits' in p else None) for p in pages])
            capture('baseline-'+mode,baseline_action)
        for mutation in manifest['incremental_mutations']:
            def mutation_action(mutation=mutation):
                write(original); index=refreshed(scratch/'incremental',original)
                changed=contract_mutation(original,mutation); write(changed)
                index=refreshed(index.output,changed)
                cases=contract_grade(index,manifest,mutation['expected_cases']); results.extend(dict(case,mutation=mutation['id']) for case in cases)
                if any(case['status']!='passed' for case in cases):raise AssertionError('Frozen contract mutation case failed')
                clean=refreshed(scratch/('clean-'+mutation['id']),changed,'queued')
                if contract_facts(index)!=contract_facts(clean):raise AssertionError('Updated contract facts/dependencies differ from clean queued rebuild')
                if contract_pages(index.output)[0]!=contract_pages(clean.output)[0]:raise AssertionError('Updated contract query rows differ from clean')
                write(original); restored=refreshed(index.output,original)
                if contract_facts(restored)!=baseline:raise AssertionError('Restored contract facts differ from baseline')
                return dict(update_equals_clean=True,restoration_equals_baseline=True,result_inventory_sha256=mutation['result_inventory_sha256'])
            capture(mutation['id'],mutation_action)
    positives=[r for r in results if r.get('mode') in ('serial','queued') and r['id'].startswith('CT-') and r.get('expected',{}).get('target_cardinality')==1]
    true_positive=sum(r['status']=='passed' for r in positives)
    return dict(schema_version=1,suite='contracts',status='failed' if failures or any(r['status']=='failed' for r in results) else 'passed',
        source_identity=identity,case_results=results,failures=failures,producer_receipts=receipts,environment=environment(),
        counts=dict(selected_cases=17,qualified_baseline_links=4,explicit_unknowns=12,unbound_zero_rows=1,mutations=10),
        selected_target_metrics=dict(true_positive=true_positive,false_negative=len(positives)-true_positive,denominator=len(positives),
            precision=None,recall=true_positive/len(positives) if positives else None,scope='reviewed selected positives only; unresolved failures retained individually'),
        limits_qualified=False,qualification_complete=False,
        scope='Explicit reviewed static contract links; no runtime transport/order/deployment, complete business path, model, scale or human qualification.')


def contract_impact(root=ROOT, budget=None):
    """Actual finite backend outcomes; viewer/human outcomes remain separate."""
    from repo_graph.analysis import StructuralIndex
    from repo_graph.analysis_queries import Queries, SQLSnapshot, Limits, _row_entities, encoded
    from repo_graph.search import Search, captured_source, connect, index_status
    root=Path(root); oracle,manifest,original,identity=contract_impact_inputs(root)
    code_paths=('repo_graph/analysis.py','repo_graph/analysis_native.py','repo_graph/analysis_queue.py','repo_graph/analysis_queries.py',
        'repo_graph/search.py','repo_graph/source.py','repo_graph/cli.py','repo_graph/server.py','evaluations/analysis.py',
        'tests/test_contracts.py','pyproject.toml','uv.lock')
    with SourceRoot(root) as owner:
        before={path:owner.read(path,1024*1024,hash_full=True)[1] for path in code_paths}
    identity['implementation']=dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),sha256=before)
    results=[]; failures=[]; receipts=[]; boundaries=[]; query_traces={}; actual_rows={}; source_evidence={}
    def require(condition,message):
        if not condition: raise AssertionError(message)
    def capture(identifier,action):
        started=time.perf_counter()
        try:
            details=action(); row=dict(id=identifier,status='passed',details=details)
        except Exception as error:
            # Full diagnostics belong to retained private command logs, not a
            # portable task report that could contain temporary or host paths.
            print(identifier + ': ' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
            failure=dict(id=identifier,error_kind=type(error).__name__);failures.append(failure)
            row=dict(failure,status='failed')
        row['elapsed_seconds']=time.perf_counter()-started;results.append(row);return row
    def refusal(action):
        try: action()
        except (ValueError,RuntimeError):return True
        raise AssertionError('Required boundary was accepted')
    request=dict(selector=dict(kind='source_area',paths=['bindings.json']),relations=['contract'],depth=1)
    with tempfile.TemporaryDirectory(prefix='contract-impact-evaluation-') as scratch:
        scratch=Path(scratch);source=scratch/'source';source.mkdir()
        def write(blobs):
            for path in source.rglob('*'):
                if path.is_file():path.unlink()
            for path,raw in blobs.items():
                file=source/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw)
        def produce(output,blobs,mode='serial',*,git_base=None,enrolled=True):
            context=contract_context(source,blobs,identity['independent_review_sha256'],contract_inventory_identity(blobs))
            index=StructuralIndex(source,output,budget=budget,contract_context=context if enrolled else None)
            receipt=index.refresh([{k:r[k] for k in ('path','language','kind','sha256','bytes')} for r in contract_inventory(blobs)],
                mode=mode,concurrency=2 if mode=='queued' else 1,git_base=git_base)
            receipts.append(dict(mode=mode,**receipt))
            require(receipt['status']=='ready','Contract-impact producer not ready: '+str(receipt))
            require(receipt['resources']['workers_started']==receipt['resources']['owned_workers_reaped'],'Owned workers not reaped')
            return index
        write(original);index=produce(scratch/'index',original)
        baseline=contract_facts(index);baseline_rows,baseline_pages=contract_impact_pages(index.output,request)
        query_traces['baseline']=_contract_impact_trace(baseline_rows,baseline_pages,request,actual_rows,source_evidence)
        def baseline_cases():
            rows=contract_impact_grade(index,oracle,manifest,'baseline');results.extend(rows)
            require(all(r['status']=='passed' for r in rows),'Baseline source assertion failed')
            source_outcomes=contract_grade(index,manifest)
            require(all(r['status']=='passed' for r in source_outcomes),'Original contract/source affinity changed')
            queued=produce(scratch/'queued',original,'queued')
            require(contract_facts(queued)==baseline,'Serial/queued facts or membership differ')
            require(contract_impact_pages(queued.output,request)[0]==baseline_rows,'Serial/queued paged rows differ')
            return dict(source_assertions=len(rows),serial_queued_equal=True,original_source_affinity_outcomes=source_outcomes)
        capture('baseline-source-assertions',baseline_cases)
        mutation_outcomes=[];cursor_outcomes=[];missing_outcomes=[]
        for mutation in manifest['incremental_mutations']:
            def changed_case(mutation=mutation):
                write(original);current=produce(index.output,original)
                with SQLSnapshot(current.output) as held,Queries(current.output) as queries:
                    prior=queries.run(dict(operation='impact',**request,limits=dict(max_edges=1)))
                    held_page=held.query(operation='impact',**request,limits=Limits(max_edges=1))
                    blobs=contract_mutation(original,mutation);write(blobs);current=produce(current.output,blobs)
                    require(refusal(lambda:queries.run(dict(operation='impact',**request,limits=dict(max_edges=1),cursor=prior['cursor']))),'Stale cursor accepted')
                    unchanged=held.query(operation='impact',**request,limits=Limits(max_edges=1))
                    require(unchanged['rows']==held_page['rows'] and unchanged['generation']==held_page['generation'],'Held snapshot mixed generations')
                rows=contract_impact_grade(current,oracle,manifest,mutation['id']);results.extend(rows)
                require(all(r['status']=='passed' for r in rows),'Mutation source assertion failed')
                source_outcomes=contract_grade(current,manifest,mutation['expected_cases'])
                require(all(r['status']=='passed' for r in source_outcomes),'Frozen mutated source affinity changed')
                facts=contract_facts(current)
                clean=produce(scratch/('clean-'+mutation['id']),blobs,'queued')
                require(facts==contract_facts(clean),'Update/clean facts or membership differ')
                current_rows,current_pages=contract_impact_pages(current.output,request)
                query_traces[mutation['id']]=_contract_impact_trace(current_rows,current_pages,request,actual_rows,source_evidence)
                require(current_rows==contract_impact_pages(clean.output,request)[0],'Update/clean paged rows differ')
                # Every published site and available declaration witness is inspected
                # using captured physical bytes; missing keys never become handles.
                for row in current_rows:
                    handles=[row['site']]+[h for h in row['evidence'] if h['source_role']=='structural_declaration']
                    for handle in handles:
                        source_handle={k:handle[k] for k in ('id','path','range','source_sha256')}
                        excerpt=captured_source(Search(current.output),dict(generation=current.metadata()['generation'],handle=source_handle,max_excerpt_bytes=64))
                        raw=blobs[handle['path']]
                        require(hashlib.sha256(raw).hexdigest()==handle['source_sha256'],'Captured handle full-file digest differs')
                        require(excerpt['raw_digest']==hashlib.sha256(raw[excerpt['range']['start_byte']:excerpt['range']['end_byte']]).hexdigest(),'Captured excerpt digest differs')
                write(original);restored=produce(current.output,original)
                require(contract_facts(restored)==baseline,'Restoration facts/membership differ')
                cursor_outcomes.append(mutation['id']);mutation_outcomes.append(mutation['id'])
                missing_outcomes.extend(r['id'] for r in rows if r['checks'].get('no_fabricated_missing_handle'))
                return dict(source_assertions=len(rows),update_clean_equal=True,restore_equal=True,held_snapshot_stable=True,
                    changed_cursor_refused=True,result_inventory_sha256=mutation['result_inventory_sha256'],original_source_affinity_outcomes=source_outcomes)
            capture(mutation['id'],changed_case)
        write(original);index=produce(index.output,original)
        def boundary(identifier,action):
            row=capture(identifier,action);boundaries.append(row);return row
        boundary('CTI-INCREMENTAL',lambda:require(len(mutation_outcomes)==10,'Incomplete mutation parity'))
        boundary('CTI-MISSING-PATH',lambda:require(len(missing_outcomes)>=3,'Missing/deleted witness path controls incomplete'))
        boundary('CTI-ZERO-TARGET',lambda:require(len([r for r in baseline_rows if r['target'] is None])==12 and
            all(not r['targets_exhaustive'] for r in baseline_rows if r['target'] is None),'Unknown targets promoted'))
        def source_controls():
            from unittest.mock import patch
            count=0
            with patch('repo_graph.source.SourceRoot.read',side_effect=AssertionError('Query-time source scan forbidden')):
                for row in baseline_rows:
                    for handle in [row['site']]+row['evidence']:
                        h={k:handle[k] for k in ('id','path','range','source_sha256')}
                        excerpt=captured_source(Search(index.output),dict(generation=index.metadata()['generation'],handle=h,max_excerpt_bytes=64))
                        raw=original[h['path']]
                        require(hashlib.sha256(raw).hexdigest()==h['source_sha256'],'Source witness digest differs')
                        require(len(excerpt['text'].encode())<=64 and len(encoded(excerpt))<=32768,'Source cap exceeded')
                        require(excerpt['raw_digest']==hashlib.sha256(raw[excerpt['range']['start_byte']:excerpt['range']['end_byte']]).hexdigest(),'Source excerpt digest differs')
                        count+=1
            return dict(captured_handles_inspected=count,live_source_reads=0)
        boundary('CTI-SOURCE',source_controls)
        def source_forgery():
            import copy
            h={k:baseline_rows[0]['site'][k] for k in ('id','path','range','source_sha256')}
            base=dict(generation=index.metadata()['generation'],handle=h)
            attempts=[]
            for kind in ('generation','digest','range','id'):
                forged=copy.deepcopy(base)
                if kind=='generation':forged['generation']='0'*64
                elif kind=='digest':forged['handle']['source_sha256']='0'*64
                elif kind=='id':forged['handle']['id']='missing:0:1:contract'
                else:forged['handle']['range']['end_byte']+=1
                refusal(lambda:captured_source(Search(index.output),forged));attempts.append(kind)
            return dict(refused=attempts)
        boundary('CTI-SOURCE-FORGERY',source_forgery)
        def filters_and_cursors():
            invalid=[]
            with Queries(index.output) as queries,Queries(index.output) as foreign:
                prior=queries.run(dict(operation='impact',**request,limits=dict(max_edges=1)))
                refusal(lambda:foreign.run(dict(operation='impact',**request,limits=dict(max_edges=1),cursor=prior['cursor'])))
                for change in (dict(services=['gateway']),dict(protocols=['rpc']),dict(namespaces=['orders-api']),
                    dict(certainties=['resolved']),dict(relations=['call','contract']),dict(selector=dict(kind='source_area',paths=['worker/']))):
                    refusal(lambda:queries.run(dict(operation='impact',**dict(request,**change),limits=dict(max_edges=1),cursor=prior['cursor'])))
                for key,values in (('services',[]),('services',['gateway','gateway']),('services',[True]),('services',['x']*9),
                    ('namespaces',['x'*257]),('namespaces',{}),('protocols',['smtp']),('protocols',['http']*4)):
                    refusal(lambda:queries.run(dict(operation='impact',**request,**{key:values})));invalid.append(dict(field=key,value=values))
                refusal(lambda:queries.run(dict(operation='impact',selector=request['selector'],services=['gateway'])))
            with SQLSnapshot(index.output) as held:
                first=held.query(operation='impact',**request,limits=Limits(max_edges=1))
                held._continuations[first['cursor']]=(0,*held._continuations[first['cursor']][1:])
                refusal(lambda:held.query(operation='impact',**request,cursor=first['cursor']))
            return dict(invalid_refused=invalid,filter_cursor_refusals=6,foreign_session_refused=True,expired_refused=True,
                changed_source_cursor_controls=cursor_outcomes)
        row=boundary('CTI-CURSOR',filters_and_cursors)
        boundary('CTI-FILTER-TYPES',lambda:require(row['status']=='passed','Filter validation control failed'))
        def limits_controls():
            details=[]
            with Queries(index.output) as queries:
                for limit in (dict(max_examined_relationships=1),dict(max_entities=1),dict(max_response_bytes=4096),dict(max_edges=1)):
                    page=queries.run(dict(operation='impact',**request,limits=limit))
                    require(page['truncated'] and page['stop_reason'] and page['total_count']['kind']!='exact','Limit claimed completeness')
                    require(page['returned_entities']<=limit.get('max_entities',50) and page['examined_work']<=limit.get('max_examined_relationships',10000) and
                        page['returned_edges']<=limit.get('max_edges',100) and len(encoded(page))<=limit.get('max_response_bytes',32768),'Limit exceeded')
                    details.append(dict(limits=limit,stop_reason=page['stop_reason'],entities=page['returned_entities'],work=page['examined_work'],bytes=len(encoded(page))))
                cancelled=queries.run(dict(operation='impact',**request),cancel=lambda:True)
                require(cancelled['stop_reason']=='cancelled' and not cancelled['rows'],'Cancellation ignored')
            now=[0.0]
            with SQLSnapshot(index.output,clock=lambda:now[0]) as held:
                def advance():now[0]+=1;return False
                stopped=held.query(operation='impact',**request,cancel=advance)
                require(stopped['stop_reason']=='deadline_exceeded','Deadline ignored')
            require(all(p['returned_entities']>=max((len(_row_entities(r)) for r in p['rows']),default=0) for p in baseline_pages),'Evidence declarations uncharged')
            return dict(actual_limits=details,cancellation=True,deadline=True,evidence_declarations_charged=True)
        boundary('CTI-LIMITS',limits_controls)
        boundary('CTI-CAPPED-PAGING',lambda:require(len(baseline_rows)==16 and len({r['site']['id'] for r in baseline_rows})==16 and
            all(p['returned_edges']<=1 for p in baseline_pages),'Paged membership skipped/duplicated rows'))
        def projection_controls():
            ordinary=produce(scratch/'ordinary',original,enrolled=False)
            with closing(connect(ordinary.output,owner=ordinary.output_owner)) as db:
                receipt=json.loads(db.execute("SELECT value FROM meta WHERE key='structural_impact_receipt'").fetchone()[0])
                receipt.pop('contract_membership_schema');receipt['identity']=hashlib.sha256(encoded({k:v for k,v in receipt.items() if k!='identity'})).hexdigest()
                db.execute("UPDATE meta SET value=? WHERE key='structural_impact_receipt'",(encoded(receipt).decode(),));db.commit()
            with Queries(ordinary.output) as queries:
                queries.run(dict(operation='impact',selector=dict(kind='source_area',paths=['worker/'])))
                require(queries.run(dict(operation='symbol'))['rows'],'Legacy symbols unavailable')
                refusal(lambda:queries.run(dict(operation='impact',**request)))
            outcomes=[]
            for defect in ('index','schema','enrollment','foreign_membership'):
                write(original);bad=produce(scratch/('bad-'+defect),original)
                with closing(connect(bad.output,owner=bad.output_owner)) as db:
                    if defect=='index':db.execute('DROP INDEX structural_contract_witness_path')
                    elif defect=='schema':db.execute("UPDATE meta SET value='old' WHERE key='structural_contract_membership_schema'")
                    elif defect=='enrollment':
                        receipt=json.loads(db.execute("SELECT value FROM meta WHERE key='structural_receipt'").fetchone()[0]);receipt['contract_enrollment']['consumer']['source_root_id']='0'*64
                        db.execute("UPDATE meta SET value=? WHERE key='structural_receipt'",(encoded(receipt).decode(),))
                    else:db.execute('INSERT INTO structural_contract_memberships VALUES(?,?)',('forged.json',baseline_rows[0]['site']['id']))
                    db.commit()
                payload=dict(operation='impact',**request)
                if defect=='foreign_membership':payload['selector']=dict(kind='source_area',paths=['forged.json'])
                with Queries(bad.output) as queries:
                    refusal(lambda:queries.run(payload))
                    queries.run(dict(operation='impact',selector=dict(kind='source_area',paths=['worker/'])))
                outcomes.append(defect)
            require(index_status(index.output)['structural']['impact']['receipt']['contracts_available'],'Validated contract status unavailable')
            return dict(refused=outcomes,legacy_p1_usable=True)
        boundary('CTI-PROJECTION',projection_controls)
        def interface_parity():
            import threading
            from urllib.request import Request,urlopen
            from repo_graph.server import create_server
            payload=dict(operation='impact',selector=dict(kind='source_area',paths=['worker/openapi.json']),relations=['contract'],
                services=['gateway'],protocols=['http'],role='all')
            with Queries(index.output) as queries:direct=queries.run(payload)
            entrypoint='from repo_graph.cli import main; raise SystemExit(main())'
            command=[sys.executable,'-c',entrypoint,'query',str(index.output),'--operation','impact','--source-area','worker/openapi.json',
                '--relation','contract','--service','gateway','--protocol','http','--role','all']
            cli=subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=10)
            require(cli.returncode==0,'Contract-impact CLI failed: '+cli.stderr[:512]);observed=json.loads(cli.stdout)
            keys=('generation','repository_identity','source_identity','analyzer_identity','config_identity','impact_identity',
                'scope','rows','selected_files','selected_symbols','unavailable_paths','contract_membership_schema','contracts_available')
            require({k:observed[k] for k in keys}=={k:direct[k] for k in keys},'CLI/direct captured rows or scope differ')
            with create_server(Search(index.output)) as server:
                thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
                address=f'http://127.0.0.1:{server.server_port}'
                try:
                    with urlopen(Request(address+'/api/query',encoded(payload),headers={'Content-Type':'application/json'}),timeout=5) as response:
                        http=json.loads(response.read(32769));require(response.status==200,'HTTP query refused')
                    require({k:http[k] for k in keys}=={k:direct[k] for k in keys},'HTTP/direct captured rows or scope differ')
                    h={k:direct['rows'][0]['site'][k] for k in ('id','path','range','source_sha256')}
                    source_request=dict(generation=direct['generation'],handle=h,max_excerpt_bytes=64)
                    with urlopen(Request(address+'/api/source',encoded(source_request),headers={'Content-Type':'application/json'}),timeout=5) as response:
                        source_response=json.loads(response.read(32769))
                    require(source_response['handle']==h and len(source_response['text'].encode())<=64,'HTTP source handle/cap differs')
                finally:server.shutdown();thread.join(timeout=5)
                require(not thread.is_alive(),'Owned server thread not stopped')
            return dict(cli_command=['python','-c',entrypoint,'query','<synthetic-index>']+command[5:],cli_exit_code=cli.returncode,
                direct_cli_http_equal=True,actual_rows=direct['rows'],viewer='pending_separate_viewer_lane')
        boundary('CTI-PARITY',interface_parity)
        def git_control():
            from evaluations.engine_checks import _adapter_materialize
            env={'PATH':os.environ.get('PATH',''),'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1','GIT_OPTIONAL_LOCKS':'0','GIT_TERMINAL_PROMPT':'0'}
            def git(*args):
                return subprocess.run(['git','-c','core.hooksPath='+os.devnull,'-c','user.name=Synthetic Fixture',
                    '-c','user.email=fixture@example.invalid','-C',str(source),*args],env=env,check=True,capture_output=True,text=True,timeout=5).stdout.strip()
            git('init','--initial-branch=main');git('add','--all');git('commit','-m','Synthetic base');base=git('rev-parse','HEAD')
            initial=produce(scratch/'git',original)
            mutation=next(m for m in manifest['incremental_mutations'] if m['id']=='CT-INC-ARTIFACT-DELETE')
            changed=contract_mutation(original,mutation)
            _adapter_materialize(source,changed,removed=set(original)-set(changed))
            git('add','--all');git('commit','-m','Synthetic artifact deletion')
            current=produce(initial.output,changed,git_base=base)
            with Queries(current.output) as queries:
                first=queries.run(dict(operation='impact',selector=dict(kind='git_change',base_revision=base),relations=['contract'],limits=dict(max_edges=1)))
                refusal(lambda:queries.run(dict(operation='impact',selector=dict(kind='git_change',base_revision='0'*40),relations=['contract'])))
            rows,pages=contract_impact_pages(current.output,dict(selector=dict(kind='git_change',base_revision=base),relations=['contract']))
            target_path=mutation['operations'][0]['path']
            expected=set(oracle['states'][mutation['id']]['path_memberships'][target_path]);actual={r['binding_id'] for r in rows}
            require(actual==expected,'Git changed-path membership differs from admitted dependency set')
            require(all(p['selection']['git_change']['source_byte_affinity']=='unobserved_worktree' for p in pages),'Commit-byte equivalence fabricated')
            require(any(b['path']==target_path and b['source_sha256'] is None for p in pages for b in p['unavailable_paths']),'Deleted artifact gained source handle')
            return dict(base_revision=base,current_revision=git('rev-parse','HEAD'),membership=sorted(actual),deleted_path=target_path,
                worktree_byte_affinity='unobserved',historical_closure='unavailable')
        boundary('CTI-GIT',git_control)
    with SourceRoot(root) as owner:
        after={path:owner.read(path,1024*1024,hash_full=True)[1] for path in code_paths}
    capture('implementation-stable',lambda:require(before==after,'Runtime/evaluator changed during execution'))
    boundaries.append(dict(id='CTI-VIEW',status='not_executed',owner='separate_viewer_lane',reason='No backend test grades accessibility or saved viewer outcomes'))
    measured=[r for r in results if r['id'] in {a['id'] for a in oracle['proposed_assertions']}]
    failed=failures or any(r['status']=='failed' for r in results) or len(measured)!=43
    return dict(schema_version=1,suite='contract-impact',status='failed' if failed else 'runtime_passed_view_pending',
        component_runtime_status='failed' if failed else 'passed',source_identity=identity,case_results=results,boundary_results=boundaries,
        failures=failures,producer_receipts=receipts,query_traces=query_traces,environment=environment(),
        actual_query_rows=actual_rows,source_evidence=source_evidence,
        counts=dict(source_assertions_expected=43,source_assertions_measured=len(measured),mutations_measured=len(mutation_outcomes),
            backend_boundary_controls=sum(r['status']=='passed' for r in boundaries),viewer_controls_pending=1),
        resources=dict(tokens=None,native_peak_rss=None),qualification_complete=False,limits_qualified=False,
        scope='Finite admitted static dependency/target oracle and backend interfaces; viewer, human, runtime transport/order, historical closure and scale unqualified')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--engine', choices=['tree-sitter'])
    modes.add_argument('--screen-engines', action='store_true')
    modes.add_argument('--compare', action='store_true')
    modes.add_argument('--profile', action='store_true')
    modes.add_argument('--profile-pilot', action='store_true', help='one finite fixture serial/queued pair; no task qualification')
    parser.add_argument('--repetition', type=int, choices=(1, 2, 3), default=None,
                        help='registered repetition identity; requires --profile-pilot, and --protocol beyond one')
    parser.add_argument('--protocol', type=Path, help='private preregistered representative protocol directory; requires --profile-pilot and --source-map')
    parser.add_argument('--freeze-budgets', action='store_true')
    parser.add_argument('--source-map', type=Path, default=os.environ.get('REPO_GRAPH_EVAL_SOURCE_MAP'),
                        help='private pinned source map; alternatively REPO_GRAPH_EVAL_SOURCE_MAP')
    parser.add_argument('--work-root', type=Path, default=os.environ.get('REPO_GRAPH_EVAL_WORK_ROOT'),
                        help='private directory outside all source roots; alternatively REPO_GRAPH_EVAL_WORK_ROOT')
    parser.add_argument('--preselection-cost-report', type=Path, help='Private actual finite cost wrapper; alternatively REPO_GRAPH_EVAL_PRESELECTION_COST_REPORT; evidence only')
    parser.add_argument('--profile-report', type=Path, help='re-export an existing complete private profile without rerunning workers')
    parser.add_argument('--suite', choices=['component', 'constructs', 'incremental', 'queries', 'coverage', 'evidence', 'impact', 'impact-interface', 'django-framework', 'contracts', 'contract-impact'], default='component')
    parser.add_argument('--output', help='relative path inside this checkout')
    parser.add_argument('--max-result-bytes', type=int,
                        help='finite report cap: 2 MiB for comparison/structural suites, 1 MiB otherwise')
    parser.add_argument('--max-files', type=int, default=128)
    parser.add_argument('--max-source-bytes', type=int, default=4 * 1024 * 1024)
    parser.add_argument('--max-nodes', type=int, default=200_000)
    args = parser.parse_args(argv)
    if args.repetition is not None and not args.profile_pilot:
        parser.error('--repetition requires --profile-pilot')
    if args.repetition is None: args.repetition = 1
    if args.repetition > 1 and args.protocol is None:
        parser.error('--repetition beyond one requires --protocol')
    structural_task = {'constructs': 'T010', 'incremental': 'T011', 'queries': 'T012', 'coverage': 'T013', 'evidence': 'T014'}.get(args.suite)
    view_task = {'impact': 'T043', 'impact-interface': 'T044'}.get(args.suite)
    business_task = {'django-framework':'T018','contracts':'T020','contract-impact':'T069'}.get(args.suite)
    if not (args.engine or args.screen_engines or args.compare or args.profile or args.profile_pilot) and structural_task is None and view_task is None and business_task is None:
        parser.error('an engine, screening, comparison or profiling mode is required for component')
    if (structural_task or view_task or business_task) and (args.screen_engines or args.compare or args.profile or args.profile_pilot):
        parser.error(args.suite + ' uses the shared structural owner directly')
    if args.max_result_bytes is None:
        args.max_result_bytes = (2 if args.compare or structural_task or view_task or business_task or args.protocol else 1) * 1024 * 1024
    if args.protocol and (not args.profile_pilot or args.source_map is None):
        parser.error('--protocol requires --profile-pilot and a pinned --source-map')
    if args.protocol and not 0 < args.max_result_bytes <= 2 * 1024 * 1024:
        parser.error('--protocol portable result cap must be positive and at most 2 MiB')
    if args.freeze_budgets and not args.profile:
        parser.error('--freeze-budgets requires --profile')
    if args.preselection_cost_report and not args.compare:
        parser.error('--preselection-cost-report requires --compare')
    if args.profile_report and not args.profile:
        parser.error('--profile-report requires --profile')
    default = ('evaluations/results/code-understanding/engine-comparison.json' if args.compare else
               'evaluations/results/code-understanding/capacity-profile.json' if args.profile else
               'evaluations/results/code-understanding/persistent-Django.json' if args.profile_pilot and args.protocol else
               'evaluations/results/code-understanding/persistent-pilot.json' if args.profile_pilot else
               'evaluations/results/code-understanding/reusable-screen.json' if args.screen_engines else
               BUSINESS_OUTPUT if business_task else VIEWS_OUTPUT if view_task else
               FACTS_OUTPUT if structural_task else DEFAULT_OUTPUT)
    args.output = args.output or default
    if args.profile_pilot:
        try:
            SourceRoot.parts(args.output)
            args.output = str(PurePosixPath(args.output))
            if args.output != default or args.protocol:
                with SourceRoot(ROOT) as owner:
                    try: owner.info(args.output)
                    except FileNotFoundError: pass
                    else: parser.error('--profile-pilot output must not replace an existing file')
        except OSError:
            parser.error('--profile-pilot output must be a safe relative file path')
    started = time.perf_counter()
    try:
        if args.max_result_bytes <= 0:
            raise ValueError('Output budget must be positive')
        if business_task:
            result = (contract_impact(ROOT,Budget(max_files=args.max_files,max_total_bytes=args.max_source_bytes,max_nodes=args.max_nodes))
                if args.suite=='contract-impact' else contracts(ROOT,Budget(max_files=args.max_files,max_total_bytes=args.max_source_bytes,max_nodes=args.max_nodes))
                if args.suite=='contracts' else django_framework(ROOT, Budget(max_files=args.max_files,
                max_total_bytes=args.max_source_bytes, max_nodes=args.max_nodes), source_map=args.source_map, work_root=args.work_root))
            result['resources'] = {'finite_framework_elapsed_seconds': time.perf_counter() - started,
                                   'tokens': None, 'native_peak_rss': None}
            if args.output == BUSINESS_OUTPUT:
                with SourceRoot(ROOT) as source: report, _ = read_json(source, BUSINESS_OUTPUT, args.max_result_bytes)
                if type(report.get('tasks')) is not dict: raise ValueError('Existing business evidence required')
                report['tasks'][business_task] = result
                size = write_result(ROOT, BUSINESS_OUTPUT, report, args.max_result_bytes)
            else: size = write_result(ROOT, args.output, result, args.max_result_bytes)
            print(json.dumps({'status': result['status'], 'counts': result['counts'], 'failures': len(result['failures']),
                              'result': args.output, 'result_bytes': size}, separators=(',', ':')))
            passed = result.get('component_runtime_status') == 'passed' if args.suite == 'contract-impact' else result['status'] == 'passed'
            return 0 if passed else 1
        if args.profile_pilot:
            if args.protocol:
                from evaluations.performance import mapped_corpora
                if args.work_root is None:
                    raise ValueError('Private --work-root or REPO_GRAPH_EVAL_WORK_ROOT required')
                with SourceRoot(args.source_map.parent) as owner:
                    mapping, _ = read_json(owner, args.source_map.name)
                original = Path(mapped_corpora(mapping)['django']['source'])
                with worker_directory(args.source_map, args.work_root) as directory:
                    result = profile_fixture_pilot(ROOT, directory, protocol=args.protocol, original_source=original,
                        repetition=args.repetition)
            else:
                result = profile_fixture_pilot(ROOT, args.work_root, repetition=args.repetition)
            size = write_result(ROOT, args.output, result, args.max_result_bytes)
            print(json.dumps({'status': result['status'], 'result': args.output, 'result_bytes': size,
                'qualification_complete': False, 'resource_budgets_frozen': False}, separators=(',', ':')))
            return 0 if result['status'] == 'complete' else 1
        if view_task:
            result = impact(ROOT, Budget(max_files=args.max_files,
                max_total_bytes=args.max_source_bytes, max_nodes=args.max_nodes), interfaces=view_task == 'T044')
            result['resources'] = {'impact_elapsed_seconds': time.perf_counter() - started}
            size = (record_view(ROOT, view_task, result, args.max_result_bytes) if args.output == VIEWS_OUTPUT else
                    write_result(ROOT, args.output, result, args.max_result_bytes))
            print(json.dumps({'status': result['status'], 'counts': result['counts'],
                'failures': len(result['failures']), 'result': args.output, 'result_bytes': size}, separators=(',', ':')))
            return 0 if result['status'] == 'passed' else 1
        if structural_task:
            producer = {'constructs': constructs, 'incremental': incremental, 'queries': queries, 'coverage': coverage,
                        'evidence': evidence}[args.suite]
            options = {'evidence_directory': args.work_root} if args.suite in ('coverage', 'evidence') else {}
            result = producer(ROOT, Budget(max_files=args.max_files,
                max_total_bytes=args.max_source_bytes, max_nodes=args.max_nodes), **options)
            result['resources'] = {args.suite + '_elapsed_seconds': time.perf_counter() - started}
            size = (record_structural(ROOT, structural_task, result, args.max_result_bytes) if args.output == FACTS_OUTPUT else
                    write_result(ROOT, args.output, result, args.max_result_bytes))
            print(json.dumps({'status': result['status'], 'counts': result['counts'],
                'failures': len(result['failures']), 'coverage_failures': len(result['coverage_failures']),
                'result': args.output, 'result_bytes': size}, separators=(',', ':')))
            return 0 if result['status'] == 'passed' else 1
        if args.compare or args.profile:
            if args.source_map is None:
                result = {'schema_version': 1, 'status': 'blocked', 'source_identity': None,
                          'case_results': [], 'reason': 'Private pinned --source-map or REPO_GRAPH_EVAL_SOURCE_MAP required',
                          'engine_selected': False, 'qualification_complete': False}
            else:
                result = (compare_component(args.source_map, args.work_root, args.preselection_cost_report) if args.compare else
                          profile_component(args.source_map, args.work_root, args.freeze_budgets, args.profile_report))
            size = write_result(ROOT, args.output, result, args.max_result_bytes)
            if args.output == default:
                record_task(ROOT, 'T007' if args.compare else 'T008', result, args.output, args.max_result_bytes)
            print(json.dumps({'status': result['status'], 'result': args.output, 'result_bytes': size,
                              'engine_selected': result.get('engine_selected') is True, 'qualification_complete': False}))
            return 0 if result['status'] == 'passed' else 1
        if args.screen_engines:
            result = screen_engines()
            size = write_result(ROOT, args.output, result, args.max_result_bytes)
            if args.output == 'evaluations/results/code-understanding/reusable-screen.json':
                record_task(ROOT, 'T006', result, args.output, args.max_result_bytes)
            print(json.dumps({'gate': 'source-screen', 'status': result['status'], 'checks': len(result['case_results']),
                              'failures': [c for c in result['case_results'] if c['status'] != 'passed'],
                              'result': args.output, 'result_bytes': size}))
            return 0 if result['status'] == 'passed' else 1
        result = component(budget=Budget(max_files=args.max_files, max_total_bytes=args.max_source_bytes, max_nodes=args.max_nodes))
        result['environment']['memory'] = environment()['memory']
        result['resources'] = {'component_elapsed_seconds': time.perf_counter() - started}
        with SourceRoot(ROOT) as source:
            result['implementation_sha256'] = {path: source.read(path, 1024 * 1024, hash_full=True)[1] for path in (
                'evaluations/analysis.py', 'repo_graph/analysis_native.py', 'pyproject.toml', 'uv.lock')}
        result['code_revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        size = write_result(ROOT, args.output, result, args.max_result_bytes)
        if args.output == DEFAULT_OUTPUT:
            record_task(ROOT, 'T005', result, args.output, args.max_result_bytes)
        print(json.dumps({'status': result['status'], 'counts': result['counts'], 'failures': len(result['failures']), 'result': args.output, 'result_bytes': size}, separators=(',', ':')))
        return 0 if result['status'] == 'passed' else 1
    except BackendUnavailable as error:
        result = {'schema_version': 1, 'engine': args.engine, 'suite': args.suite,
                  'status': 'blocked', 'reason': str(error), 'engine_selected': False,
                  'setup': 'uv sync --python 3.12 --extra analysis'}
        try:
            if view_task and args.output == VIEWS_OUTPUT:
                record_view(ROOT, view_task, result, args.max_result_bytes)
            elif structural_task and args.output == FACTS_OUTPUT:
                record_structural(ROOT, structural_task, result, args.max_result_bytes)
            else:
                write_result(ROOT, args.output, result, args.max_result_bytes)
            if args.output == DEFAULT_OUTPUT:
                record_task(ROOT, 'T005', result, args.output, args.max_result_bytes)
            elif (args.compare or args.profile) and args.output == default:
                record_task(ROOT, 'T007' if args.compare else 'T008', result, args.output, args.max_result_bytes)
        except (OSError, ValueError):
            pass
        print(json.dumps(result, separators=(',', ':')))
        return 2
    except Exception as error:
        if structural_task or view_task:
            result = {'schema_version': 1, 'suite': args.suite, 'status': 'failed',
                'error_kind': type(error).__name__, 'source_identity': None, 'case_results': [],
                'coverage_failures': [], 'qualification_complete': False, 'limits_qualified': False}
            try:
                if view_task and args.output == VIEWS_OUTPUT:
                    record_view(ROOT, view_task, result, args.max_result_bytes)
                elif args.output == FACTS_OUTPUT:
                    record_structural(ROOT, structural_task, result, args.max_result_bytes)
                else:
                    write_result(ROOT, args.output, result, args.max_result_bytes)
            except (OSError, ValueError):
                pass
        if args.compare or args.profile or args.profile_pilot:
            result = {'schema_version': 1, 'status': 'blocked', 'error_kind': type(error).__name__,
                      'source_identity': None, 'case_results': [], 'engine_selected': False, 'qualification_complete': False}
            try:
                write_result(ROOT, args.output, result, args.max_result_bytes)
                if not args.profile_pilot and args.output == default:
                    record_task(ROOT, 'T007' if args.compare else 'T008', result, args.output, args.max_result_bytes)
            except (OSError, ValueError):
                pass
        print(json.dumps({'status': 'failed', 'error_kind': type(error).__name__, 'reason': str(error)}, separators=(',', ':')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
