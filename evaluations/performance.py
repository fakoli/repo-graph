#!/usr/bin/env python3
"""Measure pinned public-corpus mapping and retrieval without re-embedding."""
import argparse
from collections import Counter
from contextlib import closing, redirect_stdout
import hashlib
import importlib.metadata
import io
import json
import math
from pathlib import Path
import platform
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_graph import builder
from repo_graph.search import Embeddings, Search, connect
from repo_graph.source import SourceRoot

LARGE_CORPORA = ('django', 'odoo', 'aws', 'kubernetes')
IMPLEMENTATION_PATHS = ('evaluations/performance.py', 'evaluations/analysis.py', 'evaluations/engine_checks.py',
    'repo_graph/analysis_native.py', 'evaluations/acceptance.py', 'evaluations/real_calls.py',
    'repo_graph/builder.py', 'repo_graph/search.py', 'repo_graph/source.py', 'pyproject.toml', 'uv.lock')


def mapped_corpora(config):
    """Validate identities without reading or executing any mapped source."""
    from evaluations.acceptance import PINS, identifier
    if not isinstance(config, dict) or not isinstance(config.get('corpora'), list):
        raise ValueError('Corpus source map must contain a list')
    sources = {}
    for item in config['corpora']:
        if (not isinstance(item, dict) or not identifier(item.get('id')) or
                not isinstance(item.get('source'), str) or not Path(item['source']).is_absolute() or
                not isinstance(item.get('revision'), str) or len(item['revision']) != 40 or
                any(c not in '0123456789abcdef' for c in item['revision'])):
            raise ValueError('Corpus source map entries require typed identity, absolute root and revision')
        if item['id'] in sources:
            raise ValueError('Corpus source map ids must be unique')
        sources[item['id']] = item
    if any(name not in sources or sources[name]['revision'] != PINS[name] for name in LARGE_CORPORA):
        raise ValueError('Required pinned large corpus missing or mismatched')
    return sources


def summary(samples):
    return {'samples_seconds': samples, 'median_seconds': statistics.median(samples),
            'p95_seconds': sorted(samples)[math.ceil(.95 * len(samples)) - 1]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def structural_worker(argv):
    """One finite process per measurement; RSS never carries over from another run."""
    import resource
    import faulthandler
    from unittest.mock import patch
    from repo_graph import search
    p = argparse.ArgumentParser()
    p.add_argument('engine', choices=['current-map', 'tree-sitter'])
    p.add_argument('source', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args(argv)
    faulthandler.enable()
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024 ** 3, 4 * 1024 ** 3))
    records, file_receipts = [], {}
    try:
        source_root, output_root = a.source.resolve(strict=True), a.output.resolve()
        if source_root == output_root or source_root in output_root.parents:
            raise ValueError('Profile artifacts must be outside source')
        for run in ('fresh-output', 'unchanged-repeat'):
            stages, reads = {}, {'operations': 0, 'hashed_bytes': 0, 'prefix_bytes': 0}
            file_receipts, stage = {}, ['inventory']
            start = time.perf_counter()
            original_read, original_info = SourceRoot.read, SourceRoot.info
            def receipt(path):
                return file_receipts.setdefault(path, {'path': path, 'status': 'metadata_only', 'reads': {}, 'errors': []})
            def observed_info(owner, path):
                try:
                    info = original_info(owner, path)
                except OSError as error:
                    if owner.root == source_root:
                        item = receipt(path)
                        item['status'] = 'source_error'
                        item['errors'].append({'stage': stage[0], 'kind': type(error).__name__, 'errno': error.errno})
                    raise
                if owner.root == source_root:
                    receipt(path)['bytes'] = info.st_size
                return info
            def observed_read(owner, path, limit, **kwargs):
                try:
                    value = original_read(owner, path, limit, **kwargs)
                except OSError as error:
                    if owner.root == source_root:
                        item = receipt(path)
                        item['status'] = ('absent_optional_configuration' if path == 'go.mod' and
                            isinstance(error, FileNotFoundError) and stage[0] == 'extract_dependencies' else 'source_error')
                        item['errors'].append({'stage': stage[0], 'kind': type(error).__name__, 'errno': error.errno})
                    raise
                if owner.root == source_root:
                    reads['operations'] += 1
                    reads['hashed_bytes'] += value[2].st_size if kwargs.get('hash_full', True) else len(value[0])
                    reads['prefix_bytes'] += len(value[0])
                    item = receipt(path)
                    item['bytes'] = value[2].st_size
                    item['reads'][stage[0]] = {'sha256': value[1], 'hash_full': kwargs.get('hash_full', True),
                        'prefix_bytes': len(value[0]), 'prefix_truncated': len(value[0]) < value[2].st_size}
                    if item['status'] != 'source_error':
                        item['status'] = 'truncated' if any(x['prefix_truncated'] for x in item['reads'].values()) else 'read'
                return value
            if a.engine == 'current-map':
                from contextlib import ExitStack
                def timed(function, label):
                    def invoke(*args, **kwargs):
                        before = time.perf_counter()
                        previous, stage[0] = stage[0], label
                        try:
                            return function(*args, **kwargs)
                        finally:
                            stage[0] = previous
                            stages[label] = stages.get(label, 0) + time.perf_counter() - before
                    return invoke
                with ExitStack() as stack:
                    stack.enter_context(patch.object(SourceRoot, 'read', observed_read))
                    stack.enter_context(patch.object(SourceRoot, 'info', observed_info))
                    for module, name in ((builder, 'repo_files'), (builder, 'tree_index'),
                            (builder, 'extract_dependencies'), (search, 'catalog'),
                            (builder, 'system_view'), (builder, 'write_page')):
                        stack.enter_context(patch.object(module, name, timed(getattr(module, name), name)))
                    with redirect_stdout(io.StringIO()):
                        builder.main([str(a.source), '--output', str(a.output)])
                with SourceRoot(a.output) as output:
                    if output.root != output_root:
                        raise ValueError('Profile output ownership changed')
                    with output.open('graph.json') as stream:
                        if os.fstat(stream.fileno()).st_size > 128 * 1024 ** 2:
                            raise ValueError('Profile graph exceeds result budget')
                        graph = json.load(stream)
                    with output.open('scan-cache.json') as stream:
                        if os.fstat(stream.fileno()).st_size > 128 * 1024 ** 2:
                            raise ValueError('Profile cache exceeds result budget')
                        cache = json.load(stream)
                input_sha = digest({path: entry['digest'] for path, entry in cache['files'].items()})
                facts_sha = digest({key: graph[key] for key in ('files', 'tree', 'dependencies', 'scope_edges', 'system')})
                counts = {'inventoried_files': graph['file_count'], 'import_edges': len(graph['dependencies']),
                          'indexed_documents': graph['search']['documents'], 'code_files': graph['scan']['code_files']}
                coverage = {'failed': graph['scan']['failed'], 'truncated': graph['scan']['truncated'],
                            'scanned': graph['scan']['scanned'], 'reused': graph['scan']['reused'],
                            'inventory_failed': graph['scan'].get('inventory', {}).get('failed', 0),
                            'search_failed': graph['search'].get('failed', 0),
                            'search_truncated': graph['search'].get('truncated', 0),
                            'files': [file_receipts.get(path, {'path': path, 'status': 'not_observed'})
                                      for path in sorted(set(graph['files']) | set(file_receipts))]}
                status = 'partial' if any(coverage[key] for key in (
                    'failed', 'truncated', 'inventory_failed', 'search_failed', 'search_truncated')) or any(
                    item['status'] == 'source_error' for item in file_receipts.values()) else 'complete'
            else:
                from evaluations.tree_sitter_baseline import Budget, scan
                before = time.perf_counter()
                with patch.object(SourceRoot, 'info', observed_info):
                    paths = builder.repo_files(a.source)
                stages['inventory_seconds'] = time.perf_counter() - before
                code_paths = [path for path in paths if Path(path).suffix in builder.CODE_EXTENSIONS
                              or Path(path).name == 'go.mod']
                limits = Budget(max_files=60000, max_file_bytes=2 * 1024 ** 2,
                    max_total_bytes=1024 ** 3, max_nodes=50000000, max_facts=2000000, timeout_seconds=300)
                stage[0] = 'native-scan'
                with patch.object(SourceRoot, 'read', observed_read), patch.object(SourceRoot, 'info', observed_info):
                    result = scan(a.source, code_paths, budget=limits)
                a.output.mkdir(parents=True, exist_ok=True)
                artifact = a.output / 'native-facts.json'
                with SourceRoot(a.output) as output:
                    if output.root != output_root:
                        raise ValueError('Profile output ownership changed')
                    with output.atomic_writer(artifact.name, text=True) as stream:
                        json.dump(result['facts'], stream, sort_keys=True, separators=(',', ':'))
                    hasher = hashlib.sha256()
                    with output.open(artifact.name) as stream:
                        while chunk := stream.read(65536):
                            hasher.update(chunk)
                facts_sha = hasher.hexdigest()
                input_sha = digest(result['inventory'])
                counts = {'inventoried_files': len(paths), 'selected_source_files': len(code_paths),
                          'excluded_other_files': len(paths) - len(code_paths),
                          **{key: len(value) for key, value in result['facts'].items()}}
                stages.update(result['resources'])
                inventory = {item['path']: item for item in result['inventory']}
                for path in paths:
                    inventory.setdefault(path, {'path': path, 'status': 'excluded_non_source'})
                for path, item in file_receipts.items():
                    inventory.setdefault(path, item)
                coverage = {'status': result['status'], 'stop_reason': result['stop_reason'],
                            'errors': result['errors'], 'limits': vars(limits),
                            'files': [inventory[path] for path in sorted(inventory)],
                            'inventory_statuses': dict(Counter(x['status'] for x in inventory.values()))}
                status = 'partial' if any(item['status'] == 'source_error' for item in file_receipts.values()) else result['status']
                coverage['status'] = status
                del result
            records.append({'run': run, 'status': status, 'wall_seconds': time.perf_counter() - start,
                'stages': stages, 'source_reads': reads, 'counts': counts, 'coverage': coverage,
                'input_inventory_sha256': input_sha,
                'semantic_facts_sha256': facts_sha,
                'artifact_bytes': sum(p.stat().st_size for p in a.output.iterdir() if p.is_file()),
                'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024})
        print(json.dumps({'engine': a.engine, 'records': records,
            'deterministic_repeat': records[0]['semantic_facts_sha256'] == records[1]['semantic_facts_sha256'],
            'native_repeat_cache': 'none; complete bounded rescan' if a.engine == 'tree-sitter' else 'not applicable', 'model_calls': 0,
            'rss_scope': 'process lifetime; Linux KiB converted to bytes'}))
        return 0
    except (OSError, RuntimeError, ValueError, MemoryError, KeyError) as error:
        print(json.dumps({'engine': a.engine, 'status': 'failed', 'error_kind': type(error).__name__,
                         'records': records, 'coverage': {'files': [file_receipts[path] for path in sorted(file_receipts)]},
                         'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}))
        return 1


def profile_structural(source_map, report_path, work_root, runs=3):
    """Measure frozen sources; import maps and callable facts are different workloads."""
    from evaluations.acceptance import PINS, read_json
    from evaluations.real_calls import checkout_identity
    from evaluations.tree_sitter_baseline import PINS as BACKEND_PINS, RULE_VERSION
    from evaluations.engine_checks import _environment
    if type(runs) is not int or runs < 3:
        raise ValueError('At least three independent worker runs required')
    source_map = Path(source_map)
    with SourceRoot(source_map.parent) as source:
        config, map_sha = read_json(source, source_map.name)
    sources = mapped_corpora(config)
    destinations = [Path(report_path).resolve(), Path(work_root).resolve()]
    for c in sources.values():
        source = Path(c['source']).resolve()
        if any(path == source or source in path.parents for path in destinations):
            raise ValueError('Profile artifacts must be outside source')
    mapped_sources = sources
    sources = {name: sources[name] for name in LARGE_CORPORA}
    def source_identity(name):
        identity = checkout_identity(sources[name]['source'], PINS[name])
        try:
            with SourceRoot(Path(sources[name]['source'])) as source:
                identity['root_identity'] = source.identity
        except OSError:
            identity['status'] = 'source_root_unavailable'
        return identity
    source_identities = {}
    for name in LARGE_CORPORA:
        if sources[name]['revision'] != PINS[name]:
            raise ValueError('Corpus revision mismatch')
        identity = source_identity(name)
        if identity['status'] != 'verified':
            raise ValueError('Pinned clean corpus checkout required: ' + identity['status'])
        source_identities[name] = identity
    records = []
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    implementation_root = Path(__file__).resolve().parents[1]
    def implementation_identity():
        try:
            with SourceRoot(implementation_root) as implementation:
                hashes = {path: implementation.read(path, 1024 * 1024, hash_full=True)[1]
                          for path in IMPLEMENTATION_PATHS}
                root_identity = implementation.identity
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=implementation_root,
                                               text=True, timeout=20, stderr=subprocess.DEVNULL).strip()
            return {'commit': revision, 'sha256': hashes, 'root_identity': root_identity}
        except (OSError, subprocess.SubprocessError):
            return {'status': 'implementation_identity_unavailable'}
    implementation = implementation_identity()
    if 'sha256' not in implementation:
        raise ValueError('Profile implementation identity unavailable')
    backend_versions = {name: importlib.metadata.version(name) for name in BACKEND_PINS}
    report_parent = Path(report_path).parent
    report_parent.mkdir(parents=True, exist_ok=True)
    with SourceRoot(report_parent) as report_owner, SourceRoot(root) as log_owner:
        if any(owner.root == Path(c['source']).resolve() or Path(c['source']).resolve() in owner.root.parents
               for owner in (report_owner, log_owner) for c in mapped_sources.values()):
            raise ValueError('Profile report owner is inside source')
        for name in LARGE_CORPORA:
            for engine in ('current-map', 'tree-sitter'):
                for repeat in range(runs):
                    checkout_before = source_identity(name)
                    if checkout_before != source_identities[name] or implementation_identity() != implementation:
                        raise ValueError('Profile source or implementation changed before worker')
                    with tempfile.TemporaryDirectory(prefix=f'{name}-{engine}-', dir=root) as scratch:
                        argv = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--structural-worker',
                                engine, sources[name]['source'], str(Path(scratch).resolve(strict=True) / 'output')]
                        started = time.perf_counter()
                        worker = subprocess.Popen(argv, env=_environment(Path(scratch).resolve(strict=True)),
                                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                  text=True, start_new_session=True)
                        timed_out = False
                        try:
                            stdout, stderr = worker.communicate(timeout=660)
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            try:
                                os.killpg(worker.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            stdout, stderr = worker.communicate()
                        except BaseException:
                            try:
                                os.killpg(worker.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            worker.communicate()
                            raise
                        worker_wall_seconds = time.perf_counter() - started
                        label = f'{name}-{engine}-{repeat}'
                        with log_owner.atomic_writer(label + '.stdout.log', text=True) as stream:
                            stream.write(stdout)
                        with log_owner.atomic_writer(label + '.stderr.log', text=True) as stream:
                            stream.write(stderr)
                        checkout_after = source_identity(name)
                        implementation_after = implementation_identity()
                        identity_verified = (checkout_after == source_identities[name] and implementation_after == implementation)
                        try:
                            result = json.loads(stdout)
                        except (ValueError, TypeError):
                            result = {'status': 'failed', 'error_kind': 'invalid-worker-report'}
                        records.append({'corpus': name, 'revision': PINS[name], 'engine': engine,
                            'repeat': repeat, 'exit_code': worker.returncode, 'timed_out': timed_out,
                            'identity_verified': identity_verified, 'checkout_before': checkout_before,
                            'checkout_after': checkout_after, 'implementation_after': implementation_after,
                            'worker_wall_seconds': worker_wall_seconds,
                            'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                            'stderr_sha256': hashlib.sha256(stderr.encode()).hexdigest(), 'result': result})
                        report_owner.write_json(Path(report_path).name, {'schema_version': 1, 'records': records,
                            'status': 'measured' if identity_verified else 'invalid_identity',
                            'source_map_sha256': map_sha,
                            'corpus_revisions': {name: PINS[name] for name in sources},
                            'implementation': {**implementation,
                                               'native_backend': backend_versions, 'native_rules': RULE_VERSION},
                            'environment': {'python': platform.python_version(), 'platform': platform.system() + ' ' + platform.machine(),
                                            'cpu_count': os.cpu_count(), 'gpu_used': False},
                            'cold_definition': 'Fresh application output; OS source cache may be warm',
                            'comparison': 'Import/file map and callable-fact scan have different outputs; no equivalent-workload speedup claim.',
                            'rust': {'adopted': False, 'prototype_built': False, 'speed_gain_measured': False},
                            'resource_caps': {'worker_address_space_bytes': 4 * 1024 ** 3, 'worker_wall_seconds': 660},
                            'worker_isolation': {'home_config_cache_temp': 'private trial directories',
                                                 'python_isolated': True, 'bytecode_writes': False},
                            'limits': ['No agent tokens or human UX results.', 'Native repeats are bounded full rescans; update and query gates remain separate.']})
                        if not identity_verified:
                            raise ValueError('Profile source or implementation changed; worker logs and invalid receipt retained')
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', choices=['terraform-provider-aws', 'kubernetes'])
    parser.add_argument('output', type=Path)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--search-runs', type=int, default=3)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    if args.runs < 3 or args.search_runs < 1:
        parser.error('Use at least three mapping runs and one search run')
    queries_paths = [Path(__file__).with_name(name) for name in ('queries.json', 'jev-queries.json')]
    cases = [case for path in queries_paths for case in json.loads(path.read_text())[args.name]]
    report = {'repository': args.name,
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.source, text=True).strip(),
              'python': platform.python_version(), 'platform': platform.system() + ' ' + platform.machine(),
              'judgments_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in queries_paths},
              'code_sha256': {f'repo_graph/{name}.py': hashlib.sha256((Path(builder.__file__).parent / f'{name}.py').read_bytes()).hexdigest()
                  for name in ('builder', 'search')},
              'mapping_runs': args.runs, 'search_samples_per_mode': len(cases) * args.search_runs,
              'cold_definition': 'Fresh output with empty scanner and keyword index caches; OS source caches may be warm.',
              'warm_definition': 'Repeat mapping into the initialized output with scanner and keyword index caches reused.',
              'embedding_rebuilt': False,
              'generation': {}, 'search': {}, 'checks': {}}
    cold, warm, fingerprints = [], [], []
    for _ in range(args.runs):
        with tempfile.TemporaryDirectory(prefix='repo-graph-performance-') as scratch:
            for samples in (cold, warm):
                started = time.perf_counter()
                with redirect_stdout(io.StringIO()):
                    builder.main([str(args.source), '--output', scratch])
                samples.append(time.perf_counter() - started)
                graph = json.loads((Path(scratch) / 'graph.json').read_text())
                fingerprints.append(digest({key: graph[key] for key in
                    ('files', 'tree', 'dependencies', 'scope_edges', 'system')}))
                assert sum(node['count'] for node in graph['system']['nodes']) == graph['file_count']
                if samples is warm:
                    assert graph['scan']['scanned'] == graph['search']['scanned'] == 0
                    assert graph['search']['reused'] == graph['search']['documents']
    report['files'] = graph['file_count']
    report['generation'] = {'cold': summary(cold), 'warm': summary(warm), 'structure_sha256': fingerprints[0]}
    report['checks']['deterministic_generation'] = len(set(fingerprints)) == 1
    embedder = Embeddings(offline=True)
    report['embedding_runtime'] = {'model': embedder.name, 'provider': 'CPUExecutionProvider', 'threads': 4,
        **{name: importlib.metadata.version(name) for name in ('fastembed', 'onnxruntime', 'numpy')},
        'model_files_sha256': {}}
    for path in Path(embedder.model.model._model_dir).rglob('*.onnx'):
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        report['embedding_runtime']['model_files_sha256'][path.name] = hasher.hexdigest()
    with closing(connect(args.output.resolve(), readonly=True)) as db:
        count, embedded = db.execute('SELECT count(*),count(vector) FROM docs').fetchone()
        report['checks']['complete_vectors'] = count == embedded == graph['file_count']
    engine = Search(args.output.resolve(), embedder)
    for mode in ('keyword', 'semantic', 'hybrid'):
        samples, records = [], []
        for case in cases:
            result = engine.run(case['query'], mode=mode, limit=10)
            relevant = next((index for index, hit in enumerate(result['results'], 1)
                if any(hit['path'].startswith(prefix) for prefix in case['relevant'])), 0)
            records.append({'query': case['query'], 'results_sha256': digest(result['results']),
                'first_relevant_rank': relevant, 'paths': [hit['path'] for hit in result['results']]})
            for _ in range(args.search_runs):
                started = time.perf_counter()
                repeated = engine.run(case['query'], mode=mode, limit=10)
                samples.append(time.perf_counter() - started)
                assert digest(repeated['results']) == records[-1]['results_sha256']
        report['search'][mode] = {**summary(samples), 'queries': records,
            'hit_rate_at_5': statistics.mean(bool(record['first_relevant_rank'] and record['first_relevant_rank'] <= 5)
                for record in records)}
    if args.compare:
        baseline = json.loads(args.compare.read_text())
        assert baseline['commit'] == report['commit']
        assert baseline['judgments_sha256'] == report['judgments_sha256']
        assert baseline['embedding_runtime'] == report['embedding_runtime']
        report['checks']['unchanged_generation'] = baseline['generation']['structure_sha256'] == fingerprints[0]
        report['checks']['unchanged_ranking'] = all(baseline['search'][mode]['queries'] == report['search'][mode]['queries']
            for mode in report['search'])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'repository': args.name, 'generation': report['generation'],
        'search': {mode: {key: value[key] for key in ('median_seconds', 'p95_seconds', 'hit_rate_at_5')}
            for mode, value in report['search'].items()}, 'checks': report['checks']}))
    if not all(report['checks'].values()):
        raise SystemExit(1)




# Finite fixture profiling is separate from legacy structural/retrieval profiles.
# These are unmeasured limits. Linux /proc, held descriptors and one writer are
# required. Sampled current RSS is neither a hard tree cap nor an unsampled peak.
DUAL_INTERVAL_SECONDS = .025
DUAL_MAX_SAMPLES = 4000
DUAL_MAX_LIVE_OWNERS = 6
DUAL_MAX_LIFETIMES = 64
DUAL_LOG_BYTES = 8 * 1024 * 1024
DUAL_MODES = (('serial', 1), ('queued', 2), ('queued', 4))
_DUAL_LOADED_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _dual_proc_stat(raw, *, include_state=False):
    """Parse only the PID ownership fields of a bounded Linux proc stat."""
    import re
    if type(raw) is not bytes or not 0 < len(raw) <= 4096:
        raise ValueError('Bounded proc stat bytes required')
    match = re.fullmatch(rb'([1-9][0-9]*) \((.*)\) ([^\n]+)\n?', raw)
    if match is None:
        raise ValueError('Malformed proc stat')
    fields = match[3].split()
    if (len(fields) < 20 or fields[0] not in (b'R',b'S',b'D',b'T',b't',b'W',b'K',b'P',b'I',b'Z',b'X',b'x') or
            not include_state and fields[0] in (b'Z',b'X',b'x')):
        raise ValueError('Live proc owner required')
    chosen = [match[1], fields[19], fields[2], fields[3]]
    if any(not value.isdigit() or int(value) <= 0 for value in chosen):
        raise ValueError('Typed positive proc identity required')
    identity=dict(zip(('pid', 'starttime_ticks', 'pgid', 'sid'), map(int, chosen)))
    return dict(identity,state=fields[0].decode('ascii')) if include_state else identity


def _dual_proc_rss(raw):
    """Read current resident bytes, never a lifetime maximum or PSS substitute."""
    import re
    if type(raw) is not bytes or not 0 < len(raw) <= 128 * 1024:
        raise ValueError('Bounded smaps_rollup bytes required')
    rows = [line for line in raw.splitlines() if line.startswith(b'Rss:')]
    if len(rows) != 1 or not re.fullmatch(rb'Rss:\s+[0-9]+\s+kB', rows[0]):
        raise ValueError('One typed smaps_rollup Rss field required')
    value = int(rows[0].split()[1]) * 1024
    if value >= 2 ** 63:
        raise ValueError('Resident byte integer overflow')
    return value


class _DualProcOwner:
    """Pin one expressly owned PID; read and recheck its start time around RSS."""
    def __init__(self, identity, *, separate_session=False):
        if (type(identity) is not dict or set(identity) != {'pid', 'starttime_ticks', 'pgid', 'sid'} or
                any(type(value) is not int or not 0 < value < 2**63 for value in identity.values()) or
                separate_session and not identity['pid'] == identity['pgid'] == identity['sid']):
            raise ValueError('Explicit typed process ownership required')
        self.identity, self.fd = dict(identity), None
        fd = os.open('/proc/' + str(identity['pid']), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.fd = fd
            self.recheck(require_live=True)
        except BaseException:
            os.close(fd)
            self.fd = None
            raise

    def read(self, name, cap):
        if name not in ('stat','smaps_rollup') or type(cap) is not int or not 0 < cap <= 128*1024:
            raise ValueError('Only fixed bounded proc fields may be read')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, min(65536, cap + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise ValueError('Proc field byte bound exceeded')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            os.close(fd)

    def recheck(self, *, require_live=False):
        record=_dual_proc_stat(self.read('stat',4096),include_state=True)
        state=record.pop('state')
        if record!=self.identity:
            raise ValueError('Owned PID identity changed')
        if state in ('Z','X','x'):
            if require_live:raise ValueError('Live owner required for readiness registration')
            # Stop/reap and the cleanup event are separate operations. An exact
            # known exiting owner has no usable current RSS; retain a gap.
            import errno
            raise ProcessLookupError(errno.ESRCH,'Known owned process is exiting')

    def rss(self):
        self.recheck()
        value = _dual_proc_rss(self.read('smaps_rollup', 128 * 1024))
        self.recheck()
        return value

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _dual_self_identity():
    fd = os.open('/proc/self', os.O_RDONLY | os.O_DIRECTORY)
    try:
        stat_fd = os.open('stat', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            raw = os.read(stat_fd, 4097)
        finally:
            os.close(stat_fd)
    finally:
        os.close(fd)
    identity = _dual_proc_stat(raw)
    if identity['pid'] != os.getpid():
        raise ValueError('Current controller identity mismatch')
    return identity


class _DualSampler:
    """Finite synchronous registration and asynchronous current-RSS observation.

    At most six *live* owners are held. Sequential queue lifetimes are archived
    under the common byte/event cap. Proc failures are explicit sample gaps;
    identity changes, caps and observer failures invalidate the measurement.
    No PID discovery, source reads, process signals or parser work occur here.
    """
    def __init__(self, supervisor=None):
        import threading
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.owners, self.lifecycles, self.samples, self.events = {}, [], [], []
        self.log_owner,self.log_fd=None,None
        self.phase, self.error, self.bytes = 'setup', None, 0
        self.started_ns, self.ended_ns = time.monotonic_ns(), None
        self.controller = _dual_self_identity()
        owner = _DualProcOwner(self.controller)
        self.owners[self.controller['pid']] = owner
        self._append(self.lifecycles, {'identity': self.controller, 'role': 'controller',
            'registered_ns': self.started_ns, 'removed_ns': None})
        if supervisor is not None:
            if supervisor['pid'] != os.getppid() or supervisor['pid'] == self.controller['pid']:
                owner.close()
                raise ValueError('Direct creating supervisor identity required')
            try:
                self.owners[supervisor['pid']] = _DualProcOwner(supervisor)
                self._append(self.lifecycles, {'identity':dict(supervisor),'role':'supervisor',
                    'registered_ns':self.started_ns,'removed_ns':None})
            except BaseException:
                for held in self.owners.values(): held.close()
                raise
        self.thread = threading.Thread(target=self._loop, name='owned-rss-sampler', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def set_phase(self, phase):
        with self.lock:
            self.phase = phase

    def _line(self, collection, value):
        kind = 'sample' if collection is self.samples else 'event' if collection is self.events else 'lifecycle'
        return json.dumps({'kind':kind,'value':value},sort_keys=True,separators=(',', ':'),allow_nan=False).encode()+b'\n'

    def _write_line(self, raw):
        if self.log_fd is not None:
            view=memoryview(raw)
            while view:
                size=os.write(self.log_fd,view)
                if size<=0:raise OSError('Private telemetry write did not progress')
                view=view[size:]
            os.fsync(self.log_fd)

    def attach_log(self, directory):
        """Hold private log ownership; readiness is durable before parse admission."""
        with self.lock:
            if self.thread.ident is not None or self.log_owner is not None:
                raise ValueError('Telemetry log must attach once before sampling')
            owner=SourceRoot(directory)
            try:
                from evaluations.engine_checks import ROOT as source_root
                source_root=source_root.resolve(strict=True)
                if owner.root==source_root or source_root in owner.root.parents:
                    raise ValueError('Telemetry output must be outside the source checkout')
                expected,actual=os.stat(directory),os.fstat(owner.fd)
                if not owner.secure or (expected.st_dev,expected.st_ino)!=(actual.st_dev,actual.st_ino):
                    raise ValueError('Held private telemetry directory required')
                self.log_fd=os.open('owned-telemetry.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=owner.fd)
                self.log_owner=owner
                for row in self.lifecycles:self._write_line(self._line(self.lifecycles,row))
            except BaseException:
                if self.log_fd is not None:os.close(self.log_fd)
                self.log_fd=None;self.log_owner=None;owner.__exit__()
                raise
        return self

    def _append(self, collection, value):
        raw=self._line(collection,value)
        if self.bytes + len(raw) > DUAL_LOG_BYTES - 32768:
            raise ValueError('Owned telemetry byte bound exceeded')
        self._write_line(raw)
        self.bytes += len(raw)
        collection.append(value)

    def _loop(self):
        try:
            next_sample=time.monotonic()
            while not self.stop.is_set():
                self.sample()
                next_sample+=DUAL_INTERVAL_SECONDS
                self.stop.wait(max(0,next_sample-time.monotonic()))
                if next_sample<time.monotonic()-DUAL_INTERVAL_SECONDS:
                    next_sample=time.monotonic()
        except (OSError, ValueError, RuntimeError) as error:
            with self.lock:
                self.error = {'kind': type(error).__name__, 'reason': str(error)[:256]}
            self.stop.set()

    def sample(self):
        with self.lock:
            if len(self.samples) >= DUAL_MAX_SAMPLES:
                raise ValueError('Owned RSS sample cap exhausted')
            start, values, gaps = time.monotonic_ns(), [], []
            for pid, owner in sorted(self.owners.items()):
                began = time.monotonic_ns()
                try:
                    rss = owner.rss()
                except OSError as error:
                    gaps.append({'pid': pid, 'kind': type(error).__name__, 'errno': error.errno})
                else:
                    values.append({'pid': pid, 'rss_bytes': rss,
                        'read_started_ns': began, 'read_ended_ns': time.monotonic_ns()})
            end = time.monotonic_ns()
            self._append(self.samples, {'started_ns': start, 'ended_ns': end,
                'read_skew_ns': end - start, 'phase': self.phase, 'owners': values, 'gaps': gaps,
                'complete': not gaps, 'owned_rss_bytes': sum(v['rss_bytes'] for v in values) if not gaps else None})

    def observe(self, event):
        """Queue event admission; exact event schema is pinned with the producer."""
        with self.lock:
            try:
                if self.error is not None:
                    return False
                from evaluations import queued_collector as queue
                queue._event_valid(event)
                # The shared producer boundary handles fixed fields/types. This
                # fixture observer adds held ownership and tighter source count.
                if (event['controller']!=self.controller or event['index'] is not None and event['index']>=128 or
                        event['live_workers']>event['configured_concurrency'] or
                        event['workers_started']>event['configured_concurrency'] or
                        event['pending_requests']>event['configured_concurrency'] or
                        event['mode']=='serial' and event['configured_concurrency']!=1):
                    raise ValueError('Fixture observer ownership/concurrency mismatch')
                if len(self.events) >= 10000:
                    raise ValueError('Owned queue event cap exhausted')
                identity = event['worker']
                if identity is None:
                    if event['role'] != 'controller' or event['event'] not in ('readiness','failure') or event['event']=='readiness' and event['index'] is not None:
                        raise ValueError('Invalid controller lifecycle event')
                else:
                    if (type(identity) is not dict or set(identity)!=set(self.controller) or
                            any(type(v) is not int or not 0<v<2**63 for v in identity.values())):
                        raise ValueError('Typed bounded worker identity required')
                    pid = identity['pid']
                    if event['role'] != 'worker':
                        raise ValueError('Worker role required')
                    if event['event'] == 'readiness':
                        if pid in self.owners or len(self.owners) >= DUAL_MAX_LIVE_OWNERS or len(self.lifecycles) >= DUAL_MAX_LIFETIMES:
                            raise ValueError('Duplicate or excess live/lifetime worker owner')
                        self.owners[pid] = _DualProcOwner(identity, separate_session=True)
                        # A producer can wait for this lock behind a sample.
                        # Record registry changes here; preserve its event clock.
                        self._append(self.lifecycles, {'identity':dict(identity),'role':'worker',
                            'registered_ns':time.monotonic_ns(),'removed_ns':None})
                    elif pid not in self.owners or self.owners[pid].identity != identity:
                        raise ValueError('Unregistered or changed worker owner')
                    elif event['event'] == 'cleanup':
                        if (type(event['cleanup']) is not dict or set(event['cleanup']) != {'leader_reaped','group_absent','mailboxes_removed'} or
                                any(type(v) is not bool for v in event['cleanup'].values()) or
                                event['cleanup']['leader_reaped'] is not True or event['cleanup']['group_absent'] is not True or
                                event['cleanup']['mailboxes_removed'] is not True):
                            raise ValueError('Owned worker removal requires completed group/mailbox cleanup')
                        self.owners.pop(pid).close()
                        next(row for row in reversed(self.lifecycles) if row['identity']==identity)['removed_ns']=time.monotonic_ns()
                    else:
                        self.owners[pid].recheck()
                self._append(self.events, dict(event, phase=self.phase))
                return True
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                self.error = {'kind': type(error).__name__, 'reason': str(error)[:256]}
                return False

    def finish(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        alive = self.thread.is_alive()
        if alive:
            # Do not wait on the lock held by a blocked proc/log syscall or close
            # descriptors the sampler may still use. The outer owned worker wall
            # cap handles this failed measurement; already durable JSONL survives.
            return {'schema_version':1,'label':'peak_sampled_owned_rss_bytes',
                'peak_sampled_owned_rss_bytes':None,'sampler_stopped':False,
                'remaining_registered_worker_owners':None,'owner_registry_inspected':False,
                'error':{'kind':'RuntimeError','reason':'Sampler did not stop within 2 seconds; held resources remain until controller exit'},
                'samples':[],'queue_events':[],'sample_window':{'started_ns':self.started_ns,'ended_ns':time.monotonic_ns()},
                'max_log_bytes':DUAL_LOG_BYTES,'unsampled_peak_bound':False}
        with self.lock:
            self.ended_ns = time.monotonic_ns()
            for row in self.lifecycles:
                if row['role'] in ('controller','supervisor'):
                    row['removed_ns']=self.ended_ns
                    row['removal_scope']='sampling window ended; process exit not claimed'
            live_workers = [dict(owner.identity) for pid, owner in self.owners.items()
                if any(row['identity']==owner.identity and row['role']=='worker' for row in self.lifecycles)]
            if not alive:
                try:
                    if self.log_fd is not None:os.fsync(self.log_fd)
                except OSError as error:
                    self.error={'kind':type(error).__name__,'reason':'Private telemetry synchronization failed'}
                finally:
                    if self.log_fd is not None:os.close(self.log_fd);self.log_fd=None
                    if self.log_owner is not None:self.log_owner.__exit__();self.log_owner=None
                for owner in self.owners.values():
                    owner.close()
                self.owners.clear()
            complete = [s for s in self.samples if s['complete']]
            if not complete and self.error is None:
                self.error={'kind':'ValueError','reason':'No complete owned RSS samples retained'}
            intervals=[b['started_ns']-a['started_ns'] for a,b in zip(self.samples,self.samples[1:])]
            return {'schema_version': 1, 'label': 'peak_sampled_owned_rss_bytes',
                'peak_sampled_owned_rss_bytes': max((s['owned_rss_bytes'] for s in complete), default=None),
                'sample_window': {'started_ns': self.started_ns, 'ended_ns': self.ended_ns},
                'requested_interval_seconds': DUAL_INTERVAL_SECONDS, 'max_samples': DUAL_MAX_SAMPLES,
                'max_live_owners': DUAL_MAX_LIVE_OWNERS, 'max_lifetime_owners':DUAL_MAX_LIFETIMES,
                'lifecycles':self.lifecycles,'max_log_bytes': DUAL_LOG_BYTES,
                'retained_log_bytes': self.bytes, 'sample_count': len(self.samples),'largest_start_interval_ns':max(intervals,default=None),
                'complete_sample_count': len(complete), 'sample_gap_count': sum(bool(s['gaps']) for s in self.samples),
                'max_read_skew_ns': max((s['read_skew_ns'] for s in self.samples), default=None),
                'samples': self.samples, 'queue_events': self.events, 'controller': self.controller,
                'remaining_registered_worker_owners': live_workers, 'sampler_stopped': not alive,
                'error': self.error, 'unsampled_peak_bound': False,
                'strict_current_rss_sum': 'complete samples only; gaps are never treated as zero',
                'limits': ['actual owned RSS includes private profiler/proof-retention overhead','25ms requested cadence is not guaranteed', 'reads are sequential and not simultaneous',
                    'shared resident pages are counted per owner', 'no unrelated processes or exited lifetime peaks included']}


def _dual_capture(root):
    from evaluations import engine_checks as checks, queued_collector as queue
    from evaluations.acceptance import committed
    from evaluations.tree_sitter_baseline import PINS
    if sys.platform!='linux':
        raise OSError('Owned proc RSS profiling requires Linux')
    root = checks._adapter_root(root)
    if Path(__file__).resolve() != root / 'evaluations/performance.py':
        raise ValueError('Profiler must run from its loaded committed checkout')
    bound = checks._adapter_capture(root)
    paths = set(IMPLEMENTATION_PATHS) | set(checks.ADAPTER_HELPERS) | {
        'evaluations/supplement_preparation.py','evaluations/code-understanding/engine-decisions.json','repo_graph/__init__.py'}
    bound['implementation'] = {path: checks._adapter_bytes(root, path, cap=2 * 1024 * 1024)[1]
        for path in sorted(paths)}
    if bound['implementation']['evaluations/performance.py'] != _DUAL_LOADED_SHA256:
        raise ValueError('Loaded profiler differs from current source bytes')
    import inspect
    if not {'telemetry','observer','observer_max_events'} <= set(inspect.signature(queue.collect_files).parameters):
        raise ValueError('Owned queue observer API must be integrated before fixture profiling')
    bound['queue_identity'] = queue._identity()
    bound['runtime'] = {'python_version':platform.python_version(),'python_implementation':platform.python_implementation(),
        'system':platform.system(),'release':platform.release(),'machine':platform.machine()}
    bound['backend'] = {name: importlib.metadata.version(name) for name in PINS}
    if bound['backend'] != PINS or not committed(root, bound['implementation']):
        raise ValueError('Pinned backend and complete committed profiler manifest required')
    _dual_recheck(root, bound)
    return bound


def _dual_recheck(root, bound):
    from evaluations import engine_checks as checks, queued_collector as queue
    from evaluations.tree_sitter_baseline import PINS
    receipt = checks._adapter_recheck(root, bound)
    if (bound['implementation']['evaluations/performance.py'] != _DUAL_LOADED_SHA256 or
            queue._identity() != bound['queue_identity'] or
            bound['runtime']!={'python_version':platform.python_version(),'python_implementation':platform.python_implementation(),
                'system':platform.system(),'release':platform.release(),'machine':platform.machine()} or
            {name: importlib.metadata.version(name) for name in PINS} != bound['backend'] or bound['backend'] != PINS):
        raise ValueError('Measured loaded helper or backend identity changed')
    return dict(receipt, backend=dict(bound['backend']), queue_identity=bound['queue_identity'],runtime=bound['runtime'])


def _dual_inputs(root, bound):
    """Project only approved source bytes and two immutable source edits."""
    from evaluations import engine_checks as checks
    from evaluations.supplement_preparation import SOURCE
    manifest = checks._adapter_json(root, SOURCE, bound)
    blobs = {row['path']: row['content_utf8'].encode() for row in manifest['files']}
    metadata = [{key: row[key] for key in ('path', 'language', 'kind', 'bytes', 'sha256')}
        for row in manifest['files']]
    if len(blobs) != 24 or len(metadata) != len(blobs):
        raise ValueError('Exact approved 24-file finite source manifest required')
    edits = {row['id']: {key: row[key] for key in ('id', 'language', 'operations')}
        for row in manifest['updates'] if row['id'] in ('U-PY-BODY', 'U-PY-EXPORT')}
    if set(edits) != {'U-PY-BODY', 'U-PY-EXPORT'}:
        raise ValueError('Both exact frozen source mutations required')
    return blobs, metadata, edits


def _dual_mutation(blobs, metadata, update):
    """Only the frozen finite single-file replace operator, never gold judgments."""
    if (update['id'] not in ('U-PY-BODY', 'U-PY-EXPORT') or len(update['operations']) != 1 or
            update['operations'][0]['op'] != 'replace'):
        raise ValueError('Exact single frozen replace operation required')
    op = update['operations'][0]
    changed = dict(blobs)
    path = op['path']
    raw = changed[path]
    if (type(op['occurrences']) is not int or op['occurrences'] != 1 or
            hashlib.sha256(raw).hexdigest() != op['sha256_before'] or raw.count(op['old'].encode()) != 1):
        raise ValueError('Frozen base mutation identity mismatch')
    raw = raw.replace(op['old'].encode(), op['new'].encode())
    if hashlib.sha256(raw).hexdigest() != op['sha256_after']:
        raise ValueError('Frozen after mutation identity mismatch')
    changed[path] = raw
    records = [dict(row, bytes=len(changed[row['path']]), sha256=hashlib.sha256(changed[row['path']]).hexdigest())
        for row in metadata]
    return changed, records


def _dual_attempt(candidate, records, label, mode, concurrency, directory, sampler):
    """Observe inclusive parent stages without changing Candidate or payloads."""
    from evaluations import incremental_candidate as incremental, queued_collector as queue
    from evaluations import engine_checks as checks
    from unittest.mock import patch
    from contextlib import ExitStack
    stages, active = {}, [False]
    def measured(label, function):
        def call(*args, **kwargs):
            began = time.monotonic_ns()
            try:
                return function(*args, **kwargs)
            finally:
                row = stages.setdefault(label, {'calls': 0, 'inclusive_seconds': 0.0})
                row['calls'] += 1
                row['inclusive_seconds'] += (time.monotonic_ns() - began) / 1e9
        return call
    original_queue, original_read = queue.collect_files, SourceRoot.read
    decode = incremental.CollectedFile.from_json
    def observed_decode(*args, **kwargs):
        name = 'handoff_decode' if active[0] else 'cache_decode'
        return measured(name, decode)(*args, **kwargs)
    def observed_read(owner, *args, **kwargs):
        if owner.identity == candidate.owner:
            return measured('source_read', original_read)(owner, *args, **kwargs)
        return original_read(owner, *args, **kwargs)
    def observed_queue(*args, **kwargs):
        active[0] = True
        try:
            if {'telemetry', 'observer', 'observer_max_events'} & set(kwargs):
                raise ValueError('Candidate must not override owned profiler telemetry')
            def observe(event):
                if event.get('mode')!=mode or event.get('configured_concurrency')!=concurrency:
                    sampler.error={'kind':'ValueError','reason':'Queue mode/concurrency differs from measured phase'}
                    return False
                return sampler.observe(event)
            return measured('collection_controller', original_queue)(*args, **kwargs,
                telemetry=True, observer=observe, observer_max_events=10000)
        finally:
            active[0] = False
    sampler.set_phase(label)
    began = time.monotonic_ns()
    refresh_end=None
    attempt = {'label': label, 'status': 'running', 'mode': mode, 'concurrency': concurrency}
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(queue, 'collect_files', observed_queue))
            stack.enter_context(patch.object(SourceRoot, 'read', observed_read))
            stack.enter_context(patch.object(incremental.CollectedFile, 'from_json', observed_decode))
            stack.enter_context(patch.object(incremental.CollectedFile, 'to_json',
                measured('cache_encode', incremental.CollectedFile.to_json)))
            stack.enter_context(patch.object(incremental, 'resolve_collected',
                measured('global_resolution', incremental.resolve_collected)))
            stack.enter_context(patch.object(incremental, 'Snapshot',
                measured('snapshot_construction', incremental.Snapshot)))
            attempt['receipt'] = candidate.refresh(records, mode=mode, concurrency=concurrency,
                cancel=lambda: sampler.error is not None, evidence_directory=directory)
        refresh_end=time.monotonic_ns()
        attempt['status'] = attempt['receipt']['status']
        if attempt['receipt']['status']=='complete':
            sampler.set_phase(label+'-proof-retention')
            proof_started=time.monotonic_ns()
            try:
                attempt['facts_artifact']=checks._adapter_snapshot_artifact(directory,label+'.facts.json',candidate.snapshot,attempt['receipt'])
            finally:
                attempt['proof_retention_seconds']=(time.monotonic_ns()-proof_started)/1e9
                sampler.set_phase(label)
        if sampler.error is not None:
            attempt['status'] = 'measurement_failed'
            attempt['measurement_error'] = dict(sampler.error)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError) as error:
        attempt.update(status='failed', error=checks._adapter_error(error))
        if candidate.last_attempt is not None:
            attempt['receipt'] = candidate.last_attempt
    finally:
        attempt.update(wall_seconds=((refresh_end or time.monotonic_ns())-began)/1e9,
            observed_attempt_seconds=(time.monotonic_ns()-began)/1e9,stages=stages,
            timing_scope='wall_seconds is Candidate.refresh under observation; proof retention is separate; inclusive stages are not additive')
        try:
            checks._adapter_dump(directory, label+'.json', attempt)
        except (OSError,ValueError,RuntimeError) as error:
            attempt.update(status='evidence_failed',evidence_failure=checks._adapter_error(error))
    return attempt



def _dual_isolation(directory):
    """Observe the finite controller environment before any source collection."""
    import stat
    if sys.platform!='linux' or not sys.flags.isolated or not sys.flags.no_user_site or not sys.dont_write_bytecode:
        raise ValueError('Linux isolated Python without user site/bytecode required')
    identity=_dual_self_identity()
    if not identity['pid']==identity['pgid']==identity['sid']:
        raise ValueError('Separate controller session and group required')
    with SourceRoot(directory) as owner:
        if Path.cwd()!=owner.root:raise ValueError('Owned private working directory required')
        for key,component in (('HOME','home'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),
                ('XDG_DATA_HOME','data'),('TMPDIR','tmp'),('TEMP','tmp'),('TMP','tmp')):
            value=os.environ.get(key)
            if type(value) is not str or Path(value).resolve(strict=True)!=owner.root/component:
                raise ValueError('Private environment directory confinement failed')
            info=os.stat(component,dir_fd=owner.fd,follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):raise ValueError('Private environment directory may not be a link')
        return {'python_isolated_mode':True,'bytecode_writes_disabled':True,'user_site_disabled':True,
            'private_environment_confined':True,'owned_private_working_directory':True,'own_session_and_group':True}

def _dual_run(root, directory, bound, mode, concurrency, repeat, supervisor):
    import resource
    from evaluations import engine_checks as checks
    from evaluations.incremental_candidate import Candidate
    from evaluations.tree_sitter_baseline import Budget
    blobs, records, edits = _dual_inputs(root, bound)
    report = {'schema_version': 1, 'kind': 'native_dual_fixture', 'status': 'running',
        'mode': mode, 'concurrency': concurrency, 'repeat': repeat, 'binding_before': bound,
        'input_manifest': records, 'source_bytes': sum(map(len, blobs.values())),
        'phases': [], 'equivalence': {}, 'engine_selected': False, 'qualification_complete': False,
        'cleanup_scope':'Normal queue receipts cover each collector; outer supervisor can prove only its controller group. Abnormal controller termination without collector receipts never proves descendant reaping.',
        'implementation_load_scope':'Fresh isolated controller; profiler and collector/queue load-time digests verified. Other helper hashes bind current disk/commit bytes, not general module-load attestation.',
        'representation_limits':{'candidate_cached_bytes':64*1024*1024,'snapshot_fact_bytes':64*1024*1024,
            'snapshot_fact_count':40000,'queue_admitted_bytes':32*1024*1024,'queue_inflight_bytes':40*1024*1024,
            'controller_address_space_soft_bytes':512*1024*1024,'controller_address_space_hard_bytes':512*1024*1024,'collector_address_space_bytes_each':512*1024*1024,
            'whole_profile_wall_seconds':90,'controller_cpu_soft_seconds':60,'controller_cpu_hard_seconds':60,
            'supervisor_address_space_soft_bytes':256*1024*1024,'supervisor_address_space_hard_bytes':512*1024*1024,
            'supervisor_cpu_soft_seconds':10,'supervisor_cpu_hard_seconds':60,'sampler_requested_window_seconds':100,
            'limits_qualified':False,'combined_hard_rss_cap':False},
        'limitations': ['finite fixture capacity only; large corpus representation blockers remain',
            'inclusive stage timings overlap', 'sampled RSS is not a hard resource cap or unsampled peak']}
    sampler = None
    try:
        report['controller_envelope']={'address_space':list(resource.getrlimit(resource.RLIMIT_AS)),
            'cpu_seconds':list(resource.getrlimit(resource.RLIMIT_CPU)),'core_bytes':list(resource.getrlimit(resource.RLIMIT_CORE)),
            'file_bytes':list(resource.getrlimit(resource.RLIMIT_FSIZE)),
            'sigxcpu_default':signal.getsignal(signal.SIGXCPU)==signal.SIG_DFL,
            'affinity':sorted(os.sched_getaffinity(0))}
        _dual_recheck(root, bound)
        report['isolation']=_dual_isolation(directory)
        with checks._adapter_source(directory) as source:
            with SourceRoot(source) as owner:
                report['source_owner_identity'] = owner.identity
            checks._adapter_materialize(source, blobs)
            sampler = _DualSampler(supervisor)
            sampler.attach_log(directory).start()
            def attempt(candidate, items, label, chosen_mode=mode, chosen_concurrency=concurrency):
                result = _dual_attempt(candidate, items, label, chosen_mode, chosen_concurrency, directory, sampler)
                report['phases'].append(result)
                checks._adapter_dump(directory, 'result.json', report)
                if result['status'] != 'complete':
                    raise ValueError('Fixture refresh did not complete: ' + label)
                return result['receipt']
            candidate = Candidate(source, budget=Budget(timeout_seconds=20))
            fresh = attempt(candidate, records, 'fresh-output')
            repeat_receipt = attempt(candidate, records, 'unchanged-repeat')
            report['equivalence']['unchanged_generation'] = fresh['generation'] == repeat_receipt['generation']
            report['equivalence']['unchanged_facts'] = fresh['semantic_facts_sha256'] == repeat_receipt['semantic_facts_sha256']
            for edit_id in ('U-PY-BODY', 'U-PY-EXPORT'):
                changed, changed_records = _dual_mutation(blobs, records, edits[edit_id])
                checks._adapter_materialize(source, blobs)
                primed = Candidate(source, budget=Budget(timeout_seconds=20))
                attempt(primed, records, edit_id+'-reset-prime')
                checks._adapter_materialize(source, changed)
                changed_receipt = attempt(primed, changed_records, edit_id+'-changed')
                clean = Candidate(source, budget=Budget(timeout_seconds=20))
                # Independent rebuild uses the measured mode and owned protocol.
                clean_receipt = attempt(clean, changed_records, edit_id+'-clean-rebuild')
                report['equivalence'][edit_id] = {
                    'generation': changed_receipt['generation'] == clean_receipt['generation'],
                    'semantic_facts': changed_receipt['semantic_facts_sha256'] == clean_receipt['semantic_facts_sha256'],
                    'same_source_identity': changed_receipt['source_identity'] == clean_receipt['source_identity']}
            with SourceRoot(source) as owner:
                report['source_owner_identity_after']=owner.identity
                if owner.identity!=report['source_owner_identity']:
                    raise ValueError('Private measured source owner changed')
            sampler.set_phase('source-cleanup')
            report['status'] = 'complete' if all(value if type(value) is bool else all(value.values())
                for value in report['equivalence'].values()) else 'equivalence_failed'
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError) as error:
        report.update(status='failed', failure=checks._adapter_error(error))
    finally:
        if sampler is not None:
            rss=sampler.finish()
            report['owned_rss']={key:value for key,value in rss.items() if key not in ('samples','queue_events')}
            if rss['error'] is not None or rss['remaining_registered_worker_owners']:
                report['status'] = 'measurement_failed'
            try:
                checks._adapter_dump(directory,'owned-rss.json',rss)
                raw,sha=checks._adapter_bytes(directory,'owned-rss.json',cap=DUAL_LOG_BYTES)
                report['owned_rss_artifact']={'path':'owned-rss.json','sha256':sha,'bytes':len(raw)}
                raw,sha=checks._adapter_bytes(directory,'owned-telemetry.jsonl',cap=DUAL_LOG_BYTES)
                report['owned_telemetry_artifact']={'path':'owned-telemetry.jsonl','sha256':sha,'bytes':len(raw)}
            except (OSError,ValueError,RuntimeError) as error:
                report.update(status='evidence_failed',evidence_failure=checks._adapter_error(error))
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report['controller_lifetime'] = {'process_peak_rss_bytes': usage.ru_maxrss*1024,
            'user_seconds': usage.ru_utime, 'system_seconds': usage.ru_stime,
            'scope': 'this controller lifetime only; never summed with worker maxima'}
        try:
            report['binding_after'] = _dual_recheck(root, bound)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            report.update(status='invalid_identity', identity_failure=checks._adapter_error(error))
        checks._adapter_dump(directory, 'result.json', report)
    return report



def _dual_validate_result(report, bound, mode, concurrency, repeat):
    """Refuse malformed/vacuous success from a recorded owned worker."""
    labels=('fresh-output','unchanged-repeat','U-PY-BODY-reset-prime','U-PY-BODY-changed',
        'U-PY-BODY-clean-rebuild','U-PY-EXPORT-reset-prime','U-PY-EXPORT-changed','U-PY-EXPORT-clean-rebuild')
    if (type(report) is not dict or type(report.get('schema_version')) is not int or report['schema_version']!=1 or
            report.get('kind')!='native_dual_fixture' or report.get('mode')!=mode or
            type(report.get('concurrency')) is not int or report['concurrency']!=concurrency or
            type(report.get('repeat')) is not int or report['repeat']!=repeat or report.get('binding_before')!=bound or
            type(report.get('phases')) is not list or len(report['phases'])>8 or
            any(type(row) is not dict for row in report['phases']) or
            [row.get('label') for row in report['phases']]!=list(labels[:len(report['phases'])]) or
            report.get('engine_selected') is not False or report.get('qualification_complete') is not False or
            report.get('status') not in ('complete','failed','equivalence_failed','measurement_failed','evidence_failed','invalid_identity')):
        raise ValueError('Typed fixture worker result identity/phase mismatch')
    if report['status']=='complete':
        expected={'unchanged_generation':True,'unchanged_facts':True,
            'U-PY-BODY':{'generation':True,'semantic_facts':True,'same_source_identity':True},
            'U-PY-EXPORT':{'generation':True,'semantic_facts':True,'same_source_identity':True}}
        equality=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)
        rss=report.get('owned_rss')
        if (len(report['phases'])!=8 or equality(report.get('equivalence'))!=equality(expected) or
                report.get('source_owner_identity_after')!=report.get('source_owner_identity') or
                type(report.get('source_owner_identity')) is not str or len(report['source_owner_identity'])!=64 or
                type(rss) is not dict or rss.get('error') is not None or rss.get('sampler_stopped') is not True or
                rss.get('remaining_registered_worker_owners')!=[] or
                type(rss.get('complete_sample_count')) is not int or rss['complete_sample_count']<=0 or
                type(rss.get('peak_sampled_owned_rss_bytes')) is not int or rss['peak_sampled_owned_rss_bytes']<=0 or
                type(report.get('owned_rss_artifact')) is not dict or type(report.get('owned_telemetry_artifact')) is not dict or
                report.get('binding_after')!=dict({k:bound[k] for k in ('measured_commit','implementation','input_binding','root_identity','backend','queue_identity','runtime')})):
            raise ValueError('Missing complete fixture measurement evidence')
        for row in report['phases']:
            receipt=row.get('receipt')
            if (row.get('status')!='complete' or type(receipt) is not dict or receipt.get('status')!='complete' or
                    type(row.get('facts_artifact')) is not dict or
                    type(row.get('wall_seconds')) not in (int,float) or not math.isfinite(row['wall_seconds']) or row['wall_seconds']<0 or
                    any(type(receipt.get(key)) is not str or len(receipt[key])!=64 or any(c not in '0123456789abcdef' for c in receipt[key])
                        for key in ('generation','source_identity','semantic_facts_sha256'))):
                raise ValueError('Malformed complete fixture phase receipt')
    return report

def native_dual_worker(argv):
    """Internal isolated fixture worker. No fallback, selection or gold inputs."""
    import resource
    import faulthandler
    from evaluations import engine_checks as checks
    from evaluations.supplement_preparation import decode
    parser = argparse.ArgumentParser()
    parser.add_argument('fd', type=int)
    parser.add_argument('creator_pid',type=int)
    args = parser.parse_args(argv)
    from evaluations.queued_collector import _guard_controller
    _guard_controller(args.creator_pid)
    faulthandler.enable()
    resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
    signal.signal(signal.SIGXCPU,signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_CPU, (60,60))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (DUAL_LOG_BYTES, DUAL_LOG_BYTES))
    root = Path(__file__).resolve().parents[1]
    directory = Path('/proc/self/fd')/str(args.fd)
    with SourceRoot(directory) as owner:
        raw, _, info = owner.read('control.json', 128*1024+1, hash_full=False)
        if info.st_size != len(raw) or len(raw) > 128*1024:
            raise ValueError('Finite complete fixture control required')
        control = decode(raw)
        if (type(control) is not dict or set(control) != {'schema_version','mode','concurrency','repeat','binding','directory_owner','supervisor'} or
                type(control['schema_version']) is not int or control['schema_version'] != 1 or
                type(control['concurrency']) is not int or (control['mode'],control['concurrency']) not in DUAL_MODES or
                type(control['repeat']) is not int or not 0 <= control['repeat'] < 3 or
                type(control['binding']) is not dict or type(control['supervisor']) is not dict or
                control['directory_owner'] != owner.identity):
            raise ValueError('Typed owned fixture worker control required')
    if not sys.flags.isolated or not sys.flags.no_user_site or not sys.dont_write_bytecode:
        raise ValueError('Isolated Python without user site/bytecode required')
    if control['supervisor'].get('pid')!=args.creator_pid or os.getppid()!=args.creator_pid:
        raise ValueError('Controller must bind its direct creating supervisor')
    identity = _dual_self_identity()
    if not identity['pid'] == identity['pgid'] == identity['sid']:
        raise ValueError('Separate owned profiler controller session required')
    bound = _dual_capture(root)
    if bound != control['binding']:
        raise ValueError('Parent/worker committed fixture binding mismatch')
    report = _dual_run(root,directory,bound,control['mode'],control['concurrency'],control['repeat'],control['supervisor'])
    return 0 if report['status']=='complete' else 1



def _dual_supervisor_limits(affinity=None):
    """Apply limits only inside a dedicated isolated measurement supervisor.

    POSIX children inherit hard limits: the supervisor's finite hard envelope
    allows the controller's higher *soft* limit.256MiB is not a hard AS cap.
    The default SIGXCPU action terminates at10CPU seconds; no handler is added.
    """
    import resource
    if sys.platform!='linux' or not sys.flags.isolated or not sys.flags.no_user_site or not sys.dont_write_bytecode:
        raise ValueError('Use an isolated dedicated profiler supervisor')
    identity=_dual_self_identity()
    if not identity['pid']==identity['pgid']==identity['sid']:
        raise ValueError('Interactive caller refused; a separate supervisor session is required')
    import threading
    if threading.current_thread() is not threading.main_thread():
        raise ValueError('Dedicated creating main thread required')
    allowed=os.sched_getaffinity(0)
    if affinity is None:affinity=sorted(allowed)[:4]
    if affinity is not None:
        if (type(affinity) not in (tuple,list) or not 1<=len(affinity)<=4 or
                any(type(cpu) is not int or cpu<0 or cpu not in allowed for cpu in affinity) or len(affinity)!=len(set(affinity))):
            raise ValueError('Optional affinity requires one to four distinct allowed CPUs')
    for kind,needed in ((resource.RLIMIT_AS,512*1024*1024),(resource.RLIMIT_CPU,60)):
        _,hard=resource.getrlimit(kind)
        if hard!=resource.RLIM_INFINITY and hard<needed:
            raise ValueError('Inherited hard envelope cannot admit the finite controller')
    if affinity is not None:os.sched_setaffinity(0,set(affinity))
    signal.signal(signal.SIGXCPU,signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,512*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(10,60))
    resource.setrlimit(resource.RLIMIT_FSIZE,(DUAL_LOG_BYTES,DUAL_LOG_BYTES))
    return {'address_space_soft_bytes':256*1024*1024,'address_space_hard_bytes':512*1024*1024,
        'cpu_soft_seconds':10,'cpu_hard_seconds':60,'core_bytes':0,'file_bytes':DUAL_LOG_BYTES,
        'sigxcpu_default':signal.getsignal(signal.SIGXCPU)==signal.SIG_DFL,
        'affinity':sorted(affinity) if affinity is not None else None,'whole_wall_seconds':90,
        'limits_qualified':False,'process_identity':identity}


def native_dual_supervisor(argv):
    """Internal dedicated supervisor; caller owns its session/wall cap and logs."""
    parser=argparse.ArgumentParser()
    parser.add_argument('evidence_directory',type=Path)
    parser.add_argument('--cpu-affinity',type=int,nargs='+')
    parser.add_argument('--creator-pid',type=int,required=True)
    args=parser.parse_args(argv)
    from evaluations.queued_collector import _guard_controller
    _guard_controller(args.creator_pid)
    result=profile_native_dual(Path(__file__).resolve().parents[1],args.evidence_directory,affinity=args.cpu_affinity)
    print(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False))
    return 0 if result['status']=='complete' else 1

def profile_native_dual(root, evidence_directory, runs=3, *, affinity=None):
    """Nine finite, independently isolated fixture jobs; no capacity promotion.

    Call only after integration/commit. Defaults are unmeasured. Its invoking owned parent must enforce the90s wall bound against blocking metadata/proc/filesystem calls. Every job
    retains control, attempts, result, queue receipts and capped stdout/stderr.
    No retry is made, and no whole corpus is admitted by this entry point.
    """
    import resource
    from evaluations import engine_checks as checks
    from evaluations.supplement_preparation import decode
    if type(runs) is not int or runs != 3:
        raise ValueError('Exactly three fixed fixture repeats required')
    deadline=time.monotonic()+90
    root = checks._adapter_root(root)
    envelope=_dual_supervisor_limits(affinity)
    bound = None
    result = {'schema_version':1,'kind':'native_dual_fixture_profile','status':'running',
        'binding_before':bound,'cases':[],'engine_selected':False,'qualification_complete':False,
        'measurement_defaults_qualified':False,'large_corpus_profiled':False,'supervisor_envelope':envelope}
    with checks._adapter_run(root,evidence_directory,'native-dual') as (run,name):
        try:
            bound=_dual_capture(root)
            result['binding_before']=bound
            for mode,concurrency in DUAL_MODES:
                for repeat in range(runs):
                    label = mode+'-'+str(concurrency)+'-'+str(repeat)
                    row = {'id':label,'mode':mode,'concurrency':concurrency,'repeat':repeat,'status':'running'}
                    result['cases'].append(row)
                    process = None
                    with checks._adapter_child(run,label) as job, SourceRoot(job) as owner:
                        if time.monotonic()>=deadline:raise ValueError('Whole fixture profile wall deadline exceeded')
                        row['binding_before'] = _dual_recheck(root,bound)
                        checks._adapter_dump(job,'control.json',{'schema_version':1,'mode':mode,
                            'concurrency':concurrency,'repeat':repeat,'binding':bound,'directory_owner':owner.identity,'supervisor':_dual_self_identity()})
                        try:
                            with owner.open('stdout.log',create=True) as stdout, owner.open('stderr.log',create=True) as stderr:
                                # Environment bridges name the held *parent* fd, not a child fd that closes at exec.
                                job_bridge=Path('/proc/'+str(os.getpid())+'/fd/'+str(owner.fd))
                                environment = checks._environment(job_bridge)
                                process = subprocess.Popen([sys.executable,'-I','-B',str(root/'evaluations/performance.py'),
                                    '--native-dual-worker',str(owner.fd),str(os.getpid())],cwd=job_bridge,env=environment,
                                    pass_fds=(owner.fd,),stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
                                row['pid'] = process.pid
                                checks._adapter_dump(run,'report.json',result)
                                row['returncode'] = process.wait(timeout=max(.001,deadline-time.monotonic()))
                            raw, sha, info = owner.read('result.json',DUAL_LOG_BYTES+1,hash_full=False)
                            if len(raw)!=info.st_size or len(raw)>DUAL_LOG_BYTES:
                                raise ValueError('Bounded complete fixture result required')
                            report=_dual_validate_result(decode(raw),bound,mode,concurrency,repeat)
                            row.update(status=report['status'],report=report,
                                report_artifact={'path':label+'/result.json','sha256':sha,'bytes':info.st_size})
                            # Raw report refs stay job-relative and byte-identical.
                            # Explicit prefixed refs bind the aggregate archive.
                            row['artifacts']={key:dict(ref,path=label+'/'+ref['path']) for key,ref in
                                ((key,report[key]) for key in ('owned_rss_artifact','owned_telemetry_artifact') if key in report)}
                            row['artifacts']['facts']=[dict(phase['facts_artifact'],path=label+'/'+phase['facts_artifact']['path'],phase=phase['label'])
                                for phase in report['phases'] if 'facts_artifact' in phase]
                        except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
                            row.update(status='failed',failure=checks._adapter_error(error))
                        finally:
                            row['cleanup']=checks._stop_and_reap(process) if process is not None else None
                            row['logs']=[]
                            for log in ('stdout.log','stderr.log'):
                                try:
                                    raw,sha,info=owner.read(log,DUAL_LOG_BYTES+1,hash_full=False)
                                    row['logs'].append({'path':label+'/'+log,'sha256':sha,'bytes':info.st_size,
                                        'complete':len(raw)==info.st_size and len(raw)<=DUAL_LOG_BYTES})
                                except OSError as error:
                                    row['logs'].append({'path':label+'/'+log,'error_kind':type(error).__name__,'errno':error.errno})
                            try:
                                row['binding_after']=_dual_recheck(root,bound)
                                row['identity_verified']=True
                            except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
                                row.update(status='invalid_identity',identity_verified=False,identity_failure=checks._adapter_error(error))
                            if (row['cleanup'] is None or row['cleanup'].get('leader_reaped') is not True or row['cleanup'].get('group_absent') is not True) and row['status']!='invalid_identity':
                                row['status']='cleanup_failed'
                            checks._adapter_dump(run,'report.json',result)
                    if row['status']=='invalid_identity':
                        raise ValueError('Fixture identity changed; no subsequent workers admitted')
            all_complete=len(result['cases'])==9 and all(row['status']=='complete' and row['returncode']==0 for row in result['cases'])
            if all_complete:
                reference={phase['label']:phase['receipt']['semantic_facts_sha256'] for phase in result['cases'][0]['report']['phases']}
                result['phase_semantic_agreement']={label:all(
                    next(phase for phase in row['report']['phases'] if phase['label']==label)['receipt']['semantic_facts_sha256']==sha
                    for row in result['cases']) for label,sha in reference.items()}
                result['status']='complete' if len(reference)==8 and all(result['phase_semantic_agreement'].values()) else 'equivalence_failed'
            else:
                result['phase_semantic_agreement']=None
                result['status']='failed'
        except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
            result.update(status='invalid_identity' if result['cases'] and result['cases'][-1]['status']=='invalid_identity' else 'failed',failure=checks._adapter_error(error))
            if result['cases'] and result['cases'][-1]['status']=='running':
                result['cases'][-1].update(status='admission_failed',failure=checks._adapter_error(error),cleanup=None)
        finally:
            usage=resource.getrusage(resource.RUSAGE_SELF)
            result['supervisor_lifetime']={'pid':os.getpid(),'process_peak_rss_bytes':usage.ru_maxrss*1024,
                'user_seconds':usage.ru_utime,'system_seconds':usage.ru_stime,
                'scope':'invoking supervisor lifetime only, including earlier work; never summed with other lifetime peaks'}
            try:
                result['binding_after']=_dual_recheck(root,bound) if bound is not None else None
            except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
                result.update(status='invalid_identity',identity_failure=checks._adapter_error(error))
            checks._adapter_dump(run,'report.json',result)
        if time.monotonic()>=deadline:
            result.update(status='deadline_exceeded',failure={'error_kind':'Timeout','error':'Whole90s envelope exhausted before archive'})
            checks._adapter_dump(run,'report.json',result)
        wrapper=checks._adapter_archive(run,name,result)
        if time.monotonic()>=deadline and result['status']!='deadline_exceeded':
            result.update(status='deadline_exceeded',failure={'error_kind':'Timeout','error':'Whole90s envelope exhausted during archive'})
            checks._adapter_dump(run,'report.json',result)
            wrapper=checks._adapter_archive(run,name,result)
        return wrapper


if __name__ == '__main__':
    if sys.argv[1:2] == ['--structural-worker']:
        raise SystemExit(structural_worker(sys.argv[2:]))
    if sys.argv[1:2] == ['--native-dual-worker']:
        raise SystemExit(native_dual_worker(sys.argv[2:]))
    if sys.argv[1:2] == ['--native-dual-supervisor']:
        raise SystemExit(native_dual_supervisor(sys.argv[2:]))
    main()
