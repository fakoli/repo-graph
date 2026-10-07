#!/usr/bin/env python3
"""Run the optional native syntax baseline against a separately frozen key.

uv sync --python 3.12 --extra analysis
uv run python evaluations/analysis.py --engine tree-sitter --suite component

No provider calls, real-corpus download, daemon, dynamic imports of source code,
or runtime product installation occurs. Experimental selection requires all component and finite-cost proofs.
"""
import argparse
from contextlib import contextmanager
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_graph.source import SourceRoot
from evaluations.tree_sitter_baseline import BackendUnavailable, Budget, PINS, scan

INPUTS = 'evaluations/code-understanding/'
DEFAULT_OUTPUT = 'evaluations/results/code-understanding/native-component.json'


def read_json(source, path):
    raw, sha, info = source.read(path, 1024 * 1024 + 1, hash_full=False)
    if info.st_size != len(raw) or len(raw) > 1024 * 1024:
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--engine', choices=['tree-sitter'])
    modes.add_argument('--screen-engines', action='store_true')
    modes.add_argument('--compare', action='store_true')
    modes.add_argument('--profile', action='store_true')
    parser.add_argument('--freeze-budgets', action='store_true')
    parser.add_argument('--source-map', type=Path, default=os.environ.get('REPO_GRAPH_EVAL_SOURCE_MAP'),
                        help='private pinned source map; alternatively REPO_GRAPH_EVAL_SOURCE_MAP')
    parser.add_argument('--work-root', type=Path, default=os.environ.get('REPO_GRAPH_EVAL_WORK_ROOT'),
                        help='private directory outside all source roots; alternatively REPO_GRAPH_EVAL_WORK_ROOT')
    parser.add_argument('--preselection-cost-report', type=Path, help='Private actual finite cost wrapper; alternatively REPO_GRAPH_EVAL_PRESELECTION_COST_REPORT; evidence only')
    parser.add_argument('--profile-report', type=Path, help='re-export an existing complete private profile without rerunning workers')
    parser.add_argument('--suite', choices=['component'], default='component')
    parser.add_argument('--output', help='relative path inside this checkout')
    parser.add_argument('--max-result-bytes', type=int,
                        help='finite report cap: 2 MiB for comparison, 1 MiB otherwise')
    parser.add_argument('--max-files', type=int, default=128)
    parser.add_argument('--max-source-bytes', type=int, default=4 * 1024 * 1024)
    parser.add_argument('--max-nodes', type=int, default=200_000)
    args = parser.parse_args(argv)
    if args.max_result_bytes is None:
        args.max_result_bytes = (2 if args.compare else 1) * 1024 * 1024
    if args.freeze_budgets and not args.profile:
        parser.error('--freeze-budgets requires --profile')
    if args.preselection_cost_report and not args.compare:
        parser.error('--preselection-cost-report requires --compare')
    if args.profile_report and not args.profile:
        parser.error('--profile-report requires --profile')
    default = ('evaluations/results/code-understanding/engine-comparison.json' if args.compare else
               'evaluations/results/code-understanding/capacity-profile.json' if args.profile else
               'evaluations/results/code-understanding/reusable-screen.json' if args.screen_engines else DEFAULT_OUTPUT)
    args.output = args.output or default
    started = time.perf_counter()
    try:
        if args.max_result_bytes <= 0:
            raise ValueError('Output budget must be positive')
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
            write_result(ROOT, args.output, result, args.max_result_bytes)
            if args.output == DEFAULT_OUTPUT:
                record_task(ROOT, 'T005', result, args.output, args.max_result_bytes)
            elif (args.compare or args.profile) and args.output == default:
                record_task(ROOT, 'T007' if args.compare else 'T008', result, args.output, args.max_result_bytes)
        except (OSError, ValueError):
            pass
        print(json.dumps(result, separators=(',', ':')))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        if args.compare or args.profile:
            result = {'schema_version': 1, 'status': 'blocked', 'error_kind': type(error).__name__,
                      'source_identity': None, 'case_results': [], 'engine_selected': False, 'qualification_complete': False}
            try:
                write_result(ROOT, args.output, result, args.max_result_bytes)
                if args.output == default:
                    record_task(ROOT, 'T007' if args.compare else 'T008', result, args.output, args.max_result_bytes)
            except (OSError, ValueError):
                pass
        print(json.dumps({'status': 'failed', 'error_kind': type(error).__name__, 'reason': str(error)}, separators=(',', ':')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
