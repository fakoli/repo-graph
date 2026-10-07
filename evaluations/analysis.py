#!/usr/bin/env python3
"""Run the optional native syntax baseline against a separately frozen key.

uv sync --python 3.12 --extra analysis
uv run python evaluations/analysis.py --engine tree-sitter --suite component

No provider calls, real-corpus download, daemon, dynamic imports of source code,
or runtime product installation occurs. Results do not select an engine.
"""
import argparse
from contextlib import contextmanager
import hashlib
from importlib import metadata
import json
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
                  limits=['Task-scoped component and source-screen evidence; no structural owner selected.',
                          'The approved independent AI source key does not provide human UX or agent-answer grading.',
                          'Incremental, query, scale, agent, human and distribution gates remain incomplete.'])
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
             'evaluations/engine_checks.py', 'evaluations/tree_sitter_baseline.py', 'repo_graph/source.py',
             'evaluations/code-understanding/source-target-lock.json',
             'evaluations/code-understanding/source-target-locations.json', 'pyproject.toml', 'uv.lock')
    with SourceRoot(root) as source:
        hashes = {path: source.read(path, 1024 * 1024, hash_full=True)[1] for path in paths}
    return {'source_identity': identity, 'commit': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=20).strip(), 'sha256': hashes}


def compare_component(source_map, work_root=None):
    """Run source-only experiments; absent capabilities cannot select an owner."""
    from evaluations.real_calls import compare_real_calls
    from evaluations.engine_checks import run_checks
    captured = comparison_identity()
    syntax, screen = component(), screen_engines()
    real = compare_real_calls(source_map)
    with worker_directory(source_map, work_root) as directory:
        lifecycle = run_checks(evidence_directory=directory)
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
        {'id': 'optional_installation', 'status': 'blocked',
         'reason': 'Pinned optional wheels executed in isolation; actual absent-backend core-runtime check remains required'},
        *[c for c in checks if c['id'] in ('incremental_equivalence', 'bounded_query_work')],
    ]
    return {'schema_version': 1, 'experiment': 'component-engine-comparison', 'status': 'blocked',
        'engine_selected': False, 'selected_owner': None, 'qualification_complete': False,
        'source_identity': real['source_identity'], 'source_map_sha256': real['source_map_sha256'],
        'implementation': {k: captured[k] for k in ('commit', 'sha256')},
        'scope': 'Finite component comparison; no product owner selected',
        'component': syntax, 'real_calls': real, 'lifecycle': lifecycle, 'source_screen': screen,
        'case_results': gates, 'coverage_failures': syntax['coverage_failures'],
        'blocking_gates': [c['id'] for c in gates if c['status'] != 'passed'],
        'decision': {'native': 'unqualified', 'reusable': 'source-rejected or install-blocked; no reusable engine executed',
                     'owner': 'unselected', 'automatic_rewrite': False},
        'remaining_gates': ['optional missing-backend runtime check', 'qualified incremental updates', 'bounded query adapter',
                            'scale/update/query measurements', 'agent', 'independent human UX', 'distribution', 'release'],
        'limitations': ['Parser syntax alone is not call resolution; individual binding/unknown/candidate results retained.',
            'Passing an experiment would not qualify the later human-facing product.',
            'Missing incremental/query capability and failing supported-language recall prevent selection.']}


def profile_component(source_map, work_root=None, freeze_budgets=False):
    """Capacity observations do not substitute for equivalent-fact/update work."""
    from evaluations.performance import profile_structural
    with SourceRoot(source_map.parent) as owner:
        _, map_sha = read_json(owner, source_map.name)
    with SourceRoot(ROOT) as owner:
        driver_sha = owner.read('evaluations/analysis.py', 1024 * 1024, hash_full=True)[1]
    trial = 'capacity-' + uuid.uuid4().hex[:12]
    with worker_directory(source_map, work_root) as directory:
        artifact = directory / (trial + '.json')
        records = profile_structural(source_map, artifact, directory / (trial + '-logs'))
        with SourceRoot(directory) as owner:
            _, artifact_sha, _ = owner.read(artifact.name, 0, hash_full=True)
    with SourceRoot(source_map.parent) as owner:
        if read_json(owner, source_map.name)[1] != map_sha:
            raise ValueError('Private source map changed during profiling; receipts retained')
    if not records:
        raise ValueError('No measured worker records')
    with SourceRoot(ROOT) as owner:
        if owner.read('evaluations/analysis.py', 1024 * 1024, hash_full=True)[1] != driver_sha:
            raise ValueError('Profile driver changed; worker receipts retained')
    _, identity = frozen_inputs(ROOT)
    cases = []
    for record in records:
        result = dict(record['result'])
        # ponytail: complete inventory receipts stay in the hashed private report;
        # task evidence retains individual failures without a second huge export.
        result['records'] = [dict(item, coverage={k: v for k, v in item['coverage'].items() if k != 'files'},
            failed_files=[f for f in item['coverage'].get('files', []) if f['status'] in
                          ('partial_parse', 'source_error', 'truncated', 'not_observed')])
            for item in result.get('records', [])]
        if 'coverage' in result:
            result['coverage'] = {'file_receipt_count': len(result['coverage'].get('files', [])),
                'full_receipts_in_hashed_private_artifact': True}
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
        'source_map_sha256': map_sha, 'implementation': dict(records[0]['implementation_after'],
            sha256=dict(records[0]['implementation_after']['sha256'], **{'evaluations/analysis.py': driver_sha})),
        'native_backend': {name: metadata.version(name) for name in PINS}, 'environment': environment(),
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
    parser.add_argument('--work-root', type=Path, help='private directory outside all source roots')
    parser.add_argument('--suite', choices=['component'], default='component')
    parser.add_argument('--output', help='relative path inside this checkout')
    parser.add_argument('--max-result-bytes', type=int, default=1024 * 1024)
    parser.add_argument('--max-files', type=int, default=128)
    parser.add_argument('--max-source-bytes', type=int, default=4 * 1024 * 1024)
    parser.add_argument('--max-nodes', type=int, default=200_000)
    args = parser.parse_args(argv)
    if args.freeze_budgets and not args.profile:
        parser.error('--freeze-budgets requires --profile')
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
                result = (compare_component(args.source_map, args.work_root) if args.compare else
                          profile_component(args.source_map, args.work_root, args.freeze_budgets))
            size = write_result(ROOT, args.output, result, args.max_result_bytes)
            if args.output == default:
                record_task(ROOT, 'T007' if args.compare else 'T008', result, args.output, args.max_result_bytes)
            print(json.dumps({'status': result['status'], 'result': args.output, 'result_bytes': size,
                              'engine_selected': False, 'qualification_complete': False}))
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
                'evaluations/analysis.py', 'evaluations/tree_sitter_baseline.py', 'pyproject.toml', 'uv.lock')}
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
