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
import sqlite3
import stat
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
    def __init__(self, identity, *, separate_session=False, allow_exited=False):
        if (type(identity) is not dict or set(identity) != {'pid', 'starttime_ticks', 'pgid', 'sid'} or
                any(type(value) is not int or not 0 < value < 2**63 for value in identity.values()) or
                separate_session and not identity['pid'] == identity['pgid'] == identity['sid']):
            raise ValueError('Explicit typed process ownership required')
        self.identity, self.fd = dict(identity), None
        fd = os.open('/proc/' + str(identity['pid']), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.fd = fd
            try: self.recheck(require_live=not allow_exited)
            except ProcessLookupError:
                if not allow_exited: raise
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



def _dual_supervisor_limits(affinity=None, *, representative=False):
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
    cpu_hard = 600 if representative else 60
    file_hard = 2147483648 if representative else DUAL_LOG_BYTES
    for kind,needed in ((resource.RLIMIT_AS,512*1024*1024),(resource.RLIMIT_CPU,cpu_hard),(resource.RLIMIT_FSIZE,file_hard)):
        _,hard=resource.getrlimit(kind)
        if hard!=resource.RLIM_INFINITY and hard<needed:
            raise ValueError('Inherited hard envelope cannot admit the finite controller')
    if affinity is not None:os.sched_setaffinity(0,set(affinity))
    signal.signal(signal.SIGXCPU,signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,512*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(300 if representative else 10,cpu_hard))
    resource.setrlimit(resource.RLIMIT_FSIZE,(file_hard,file_hard))
    return {'address_space_soft_bytes':256*1024*1024,'address_space_hard_bytes':512*1024*1024,
        'cpu_soft_seconds':300 if representative else 10,'cpu_hard_seconds':cpu_hard,'core_bytes':0,'file_bytes':file_hard,
        'sigxcpu_default':signal.getsignal(signal.SIGXCPU)==signal.SIG_DFL,
        'affinity':sorted(affinity) if affinity is not None else None,'whole_wall_seconds':2400 if representative else 90,
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


# The persistent adapter is a separate finite experiment. The old Candidate
# jobs and their results are unchanged and are never its equivalent reference.
PERSISTENT_MODES = (('serial', 1), ('queued', 2))
PERSISTENT_MAX_WINDOWS = 4
PERSISTENT_MAX_BATCHES = 256
# Portable projection of the independently approved pre-measurement freeze.
PERSISTENT_FREEZE_SHA = '20f7a658e74ad37f2e66923930de32a46e7949a7ea109de1532ea3a26f3902dc'
PERSISTENT_PHASES = ('fresh-output', 'unchanged-repeat', 'U-PY-BODY-changed',
    'U-PY-BODY-clean-rebuild', 'U-PY-EXPORT-reset-prime', 'U-PY-EXPORT-changed', 'U-PY-EXPORT-clean-rebuild')
PERSISTENT_QUERY_LIMITS = dict(max_edges=100, max_entities=50, max_examined_relationships=10000,
    max_excerpt_bytes=0, max_response_bytes=32768, timeout_seconds=.5)
PERSISTENT_QUERY_SPECS = (
    dict(id='F-SYMBOL-DIRECT', operation='symbol', name='direct',
         path='tests/fixtures/code-understanding/python/main.py', span=(187, 220, 11, 12)),
    dict(id='F-CALLEES-DIRECT', operation='callees', name='direct',
         path='tests/fixtures/code-understanding/python/main.py', span=(187, 220, 11, 12)),
    dict(id='F-CALLERS-EXPORT', operation='callers', name='visible',
         path='tests/fixtures/code-understanding/python/export_control.py', span=(23, 59, 3, 4)),
    dict(id='F-REFERENCE-LOCAL', operation='reference', name='local',
         path='tests/fixtures/code-understanding/python/main.py', span=(146, 184, 7, 8)),
    dict(id='F-UNRESOLVED-DYNAMIC', operation='callees', name='dynamic',
         path='tests/fixtures/code-understanding/python/main.py', span=(765, 816, 57, 58)),
    dict(id='F-FANOUT-STOP', operation='callees', name='hub',
         path='tests/fixtures/code-understanding/python/fanout.py', span=(3533, 5244, 339, 452),
         overrides=dict(max_edges=4, max_entities=5, max_examined_relationships=16)))
PERSISTENT_IMPACT_SHA = dict(
    **{'U-PY-BODY': 'a30d1f2aeaed3f059c9b56aeaabe84d4d97c72ab1601ca062056bcd93c86f937',
       'U-PY-EXPORT': '20534497229d4fab8c9c6e551e74588d1581dedbfd6fd484549c6ff3e0159572'})

# Preregistered candidate ceilings, not qualified capacity or engine defaults.
REPRESENTATIVE_CEILINGS = dict(source_total_bytes=25165824, source_file_bytes=524288,
    collection_batch_files=32, collection_batch_bytes=4194304, collection_calls_per_phase=256,
    index_bytes=2147483648, index_phase_wall_seconds=300, controller_AS_bytes=536870912,
    controller_cpu_seconds=600, job_wall_seconds=900, supervisor_AS_soft_bytes=268435456,
    supervisor_AS_hard_bytes=536870912, supervisor_CPU_soft_seconds=300,
    supervisor_CPU_hard_seconds=600, pair_wall_seconds=2400, launcher_wall_seconds=2410,
    os_file_bytes=2147483648, snapshot_bytes=2147483648, job_all_artifacts_bytes=4294967296,
    pair_all_artifacts_bytes=8589934592, sampler_windows=32, sampler_live_owners=6,
    sampler_lifetimes_per_window=64, sampler_samples_per_window=4000, sampler_window_bytes=8388608,
    sampler_interval_seconds=.025, private_report_bytes=8388608, portable_report_bytes=2097152,
    artifact_refs_per_job=10000, query_timeout_seconds=.5, query_handles=50, query_edges=100,
    query_examined_relationships=10000, query_response_bytes=32768, query_excerpt_bytes=0)
REPRESENTATIVE_HEADER_SHA = '3cc1dfc1600ff8e2b5d59f14d638e84a044766825c7efc9c74134e44453926de'
REPRESENTATIVE_DECISION_SHA = '4bbdccee0227226d842ef34b85f940f6f1193ac2c88bf7b444d34b8c025ced72'
REPRESENTATIVE_MANIFEST_SHA = '7a11c33217f28cabd24eff8804cde6151a3ab6f30101e12fe1f0511e5e95aec2'


def _persistent_hex(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _persistent_records(directory, loaded, changed=None):
    """Validate and stream the locked metadata; never hold source bodies in RAM."""
    from evaluations.supplement_preparation import decode
    header = loaded['header']['manifest']; previous = None
    hasher, canonical, count, size, body_bytes = hashlib.sha256(), hashlib.sha256(b'['), 0, 0, 0
    languages, kinds = Counter(), Counter()
    with SourceRoot(directory) as owner:
        if owner.identity != loaded['protocol_owner']: raise ValueError('Protocol owner changed')
        with owner.open('records.jsonl') as stream:
            while True:
                raw = stream.readline(4097)
                if not raw: break
                if len(raw) > 4096 or not raw.endswith(b'\n'): raise ValueError('Bounded complete manifest row required')
                row = decode(raw)
                if (type(row) is not dict or set(row) != {'bytes', 'kind', 'language', 'path', 'sha256'} or
                        type(row['bytes']) is not int or not 0 <= row['bytes'] <= REPRESENTATIVE_CEILINGS['source_file_bytes'] or
                        row['kind'] not in ('source', 'configuration') or row['language'] not in ('python', 'javascript') or
                        type(row['path']) is not str or str(Path(row['path'])) != row['path'] or
                        '\\' in row['path'] or ':' in row['path'] or not _persistent_hex(row['sha256']) or
                        previous is not None and row['path'] <= previous):
                    raise ValueError('Typed sorted unique canonical manifest required')
                SourceRoot.parts(row['path'])
                encoded = json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
                if raw != encoded + b'\n': raise ValueError('Canonical manifest bytes required')
                hasher.update(raw); canonical.update((b',' if count else b'') + encoded)
                count += 1; size += len(raw); body_bytes += row['bytes']; kinds[row['kind']] += 1
                if row['kind'] == 'source': languages[row['language']] += 1
                if count > 2978 or size > header['bytes'] or body_bytes > REPRESENTATIVE_CEILINGS['source_total_bytes']:
                    raise ValueError('Manifest count or byte ceiling exhausted')
                previous = row['path']
                if changed is not None and row['path'] == changed['path']:
                    yield dict(row, bytes=changed['bytes'], sha256=changed['sha256'])
                else: yield row
        canonical.update(b']')
        if (hasher.hexdigest() != header['sha256'] or canonical.hexdigest() != header['records_sha256'] or
                count != header['files'] or size != header['bytes'] or body_bytes != header['actual_content_bytes'] or
                kinds != Counter(source=2976, configuration=2) or dict(languages) != header['language_counts']):
            raise ValueError('Manifest locked content identity mismatch')


def _persistent_protocol(directory, original_source=None):
    """Load the single preregistered representative protocol without source reads."""
    from evaluations import engine_checks as checks
    from evaluations.supplement_preparation import decode
    raw, sha = checks._adapter_bytes(directory, 'protocol.json', cap=128 * 1024)
    config = decode(raw)
    fields = {'schema_version', 'kind', 'corpus', 'repetition', 'planned_repetitions', 'header_sha256',
        'decision_sha256', 'ceilings', 'queries', 'updates', 'impacts', 'impacts_sha256', 'freeze_sha256'}
    if (type(config) is not dict or set(config) != fields or type(config['schema_version']) is not int or
            config['schema_version'] != 1 or config['kind'] != 'persistent_representative' or config['corpus'] != 'Django' or
            type(config['repetition']) is not int or config['repetition'] != 1 or
            type(config['planned_repetitions']) is not int or config['planned_repetitions'] != 3 or
            config['header_sha256'] != REPRESENTATIVE_HEADER_SHA or config['decision_sha256'] != REPRESENTATIVE_DECISION_SHA or
            config['freeze_sha256'] != PERSISTENT_FREEZE_SHA or type(config['ceilings']) is not dict or
            set(config['ceilings']) != set(REPRESENTATIVE_CEILINGS) or
            any(type(config['ceilings'][key]) is not type(value) or config['ceilings'][key] != value
                for key, value in REPRESENTATIVE_CEILINGS.items())):
        raise ValueError('Exact preregistered representative protocol required')
    header_raw, _ = checks._adapter_bytes(directory, 'header.json', expected=REPRESENTATIVE_HEADER_SHA, cap=16384)
    header = decode(header_raw); manifest = header.get('manifest') if type(header) is dict else None
    if (header.get('corpus') != 'Django' or header.get('revision') != '3b7ae042cef02a09caab70ba54077a6f4cffac80' or
            not _persistent_hex(header.get('original_repository_identity')) or type(manifest) is not dict or
            manifest.get('path') != 'records.jsonl' or manifest.get('sha256') != REPRESENTATIVE_MANIFEST_SHA or
            manifest.get('files') != 2978 or manifest.get('source_files') != 2976 or manifest.get('configuration_files') != 2 or
            manifest.get('actual_content_bytes') != 19649470 or manifest.get('bytes') != 523075):
        raise ValueError('Locked representative header required')
    with SourceRoot(directory) as owner: identity = owner.identity
    loaded = dict(config=config, header=header, protocol_sha256=sha, protocol_owner=identity, original_owner=None)
    configs = {row['path']: row for row in _persistent_records(directory, loaded) if row['kind'] == 'configuration'}
    if set(configs) != {'package.json', 'pyproject.toml'}: raise ValueError('Exact two opaque configuration records required')
    specs = config['queries']; updates = config['updates']
    if (type(specs) is not list or len(specs) != 6 or type(updates) is not list or len(updates) != 2 or
            type(config['impacts']) is not dict or type(config['impacts_sha256']) is not dict):
        raise ValueError('Frozen six queries and two updates required')
    ids = set()
    for spec in specs:
        if (type(spec) is not dict or not {'id', 'operation', 'name', 'path', 'span'} <= set(spec) or
                set(spec) - {'id', 'operation', 'name', 'path', 'span', 'expected_empty', 'preserve_unresolved', 'depth', 'required_site'} or
                type(spec['id']) is not str or not spec['id'].replace('-', '').isalnum() or spec['id'] in ids or
                spec['operation'] not in ('symbol', 'reference', 'call', 'callees', 'callers', 'reachable', 'impact') or
                type(spec['name']) is not str or not spec['name'] or type(spec['path']) is not str or
                type(spec['span']) is not list or len(spec['span']) != 4 or
                any(type(value) is not int or value < 0 for value in spec['span']) or
                spec['span'][1] <= spec['span'][0] or not 1 <= spec['span'][2] <= spec['span'][3] or
                any(type(spec[key]) is not bool for key in ('expected_empty', 'preserve_unresolved') if key in spec) or
                'depth' in spec and (type(spec['depth']) is not int or not 1 <= spec['depth'] <= 8)):
            raise ValueError('Typed source-grounded query selectors required')
        SourceRoot.parts(spec['path']); ids.add(spec['id'])
        if 'required_site' in spec:
            anchor = spec['required_site']
            if (type(anchor) is not dict or set(anchor) != {'path','range','text','source_sha256','certainty','target_declarations'} or
                    type(anchor['path']) is not str or type(anchor['text']) is not str or len(anchor['text'].encode()) > 8192 or
                    not _persistent_hex(anchor['source_sha256']) or anchor['certainty'] not in ('resolved','candidate','unresolved') or
                    type(anchor['range']) is not dict or set(anchor['range']) != {'start_byte','end_byte','start_line','end_line'} or
                    any(type(value) is not int or value < 0 for value in anchor['range'].values()) or
                    type(anchor['target_declarations']) is not list or len(anchor['target_declarations']) > 50):
                raise ValueError('Finite source-grounded required callsite required')
            from evaluations.acceptance import _proof_physical
            _proof_physical(anchor)
            for declaration in anchor['target_declarations']: _proof_physical(declaration)
    ids = set(); paths = set(); postimages = set()
    for update in updates:
        if (type(update) is not dict or set(update) != {'id', 'path', 'postimage', 'sha256', 'bytes'} or
                type(update['id']) is not str or not update['id'].replace('-', '').isalnum() or update['id'] in ids or
                type(update['path']) is not str or
                type(update['postimage']) is not str or '/' in update['postimage'] or
                update['postimage'] in postimages or
                type(update['bytes']) is not int or not 0 < update['bytes'] <= REPRESENTATIVE_CEILINGS['source_file_bytes'] or
                not _persistent_hex(update['sha256'])): raise ValueError('Two finite independent postimages required')
        SourceRoot.parts(update['path']); SourceRoot.parts(update['postimage'])
        body, _ = checks._adapter_bytes(directory, update['postimage'], expected=update['sha256'], cap=update['bytes'])
        if len(body) != update['bytes']: raise ValueError('Postimage length changed')
        values = config['impacts'].get(update['id'])
        if (type(values) is not list or not 1 <= len(values) <= 8 or
                any(type(row) is not dict or set(row) != {'before', 'after'} or
                    any(type(row[key]) is not dict for key in ('before', 'after')) for row in values) or
                config['impacts_sha256'].get(update['id']) != digest(values)):
            raise ValueError('Frozen independent physical impact predicates required')
        ids.add(update['id']); paths.add(update['path']); postimages.add(update['postimage'])
    if set(config['impacts']) != ids or set(config['impacts_sha256']) != ids: raise ValueError('Impact identity mismatch')
    selected = {row['path'] for row in _persistent_records(directory, loaded) if row['path'] in paths}
    if selected != paths: raise ValueError('Postimages must edit admitted source paths')
    if original_source is not None:
        with SourceRoot(original_source) as original:
            if original.identity != header['original_repository_identity']: raise ValueError('Pinned original source owner mismatch')
            loaded['original_owner'] = original.identity
    return loaded


def _persistent_materialize(source, original, directory, loaded, check):
    """Copy one guarded file at a time through unchanged finite helper batches."""
    from evaluations import engine_checks as checks
    batch, batch_bytes = {}, 0
    with SourceRoot(original) as owner:
        if owner.identity != loaded['original_owner']: raise ValueError('Original source owner changed')
        for row in _persistent_records(directory, loaded):
            check.clock() if hasattr(check, 'clock') else check()
            raw, sha, info = owner.read(row['path'], row['bytes'] + 1, max_bytes=row['bytes'],
                cancel=(lambda: (check.clock(), False)[1]) if hasattr(check, 'clock') else check)
            if sha != row['sha256'] or len(raw) != row['bytes'] or info.st_size != row['bytes']:
                raise ValueError('Original content changed before materialization')
            if batch and (len(batch) == 128 or batch_bytes + len(raw) > 4194304):
                checks._adapter_materialize(source, batch); check(); batch, batch_bytes = {}, 0
            batch[row['path']] = raw; batch_bytes += len(raw)
        if batch: checks._adapter_materialize(source, batch)
    check()


class _PersistentFacts:
    """Owned read transaction over one retained SQLite proof; no fact mirror."""
    def __init__(self, db, metadata): self.db, self.identities = db, metadata
    def metadata(self): return dict(self.identities)
    def read_facts(self, kind):
        tables = dict(definitions='structural_symbols', sites='structural_sites', scopes='structural_scopes',
                      imports='structural_imports', relationships='structural_relationships')
        if kind not in tables: raise ValueError('Unknown canonical fact stream')
        if kind == 'relationships':
            for row in self.db.execute("SELECT site_id,target_id,path,role,certainty FROM structural_relationships WHERE target_id<>'' ORDER BY site_id,target_id"):
                yield dict(row)
        else:
            for row in self.db.execute('SELECT path,data FROM ' + tables[kind] + ' ORDER BY path,ordinal'):
                value = json.loads(row['data'])
                if kind == 'scopes': value['path'] = row['path']
                yield value


def _persistent_facts_metadata(db, repository):
    from repo_graph.analysis import SCHEMA
    data = dict(db.execute("SELECT key,value FROM meta WHERE key LIKE 'structural_%' OR key='repository'"))
    result = {key: data.get('structural_' + field) for key, field in
        dict(generation='generation', repository_identity='repository', source_identity='source',
             analyzer_identity='analyzer', config_identity='config').items()}
    if (data.get('structural_schema') != SCHEMA or any(not _persistent_hex(value) for value in result.values()) or
            result['repository_identity'] != repository or data.get('repository', repository) != repository):
        raise ValueError('Snapshot canonical metadata affinity mismatch')
    return result


def _persistent_snapshot(index, directory, label, *, check, limits):
    """Pin a read transaction including WAL, retain SQLite, digest five streams."""
    from evaluations import engine_checks as checks
    if (not callable(check) or type(limits) is not dict or type(limits.get('snapshot_bytes')) is not int or
            not 0 < limits['snapshot_bytes'] <= 2147483648 or
            type(label) is not str or not label.replace('-', '').isalnum()):
        raise ValueError('Finite snapshot ceiling, label and cooperative check required')
    clock = check.clock if hasattr(check, 'clock') else check
    check(); began = time.monotonic(); callbacks = [0]; counts = {}; hasher = hashlib.sha256()
    name, building, destination_fd = label + '.facts.sqlite', label + '.facts-building.sqlite', None
    stage = 'snapshot_open'
    def metadata(db):
        return _persistent_facts_metadata(db, index.owner)
    try:
        with SourceRoot(index.output) as output, SourceRoot(directory) as retained:
            if output.identity != index.output_owner or not retained.secure: raise ValueError('Snapshot directory owner mismatch')
            try: retained.info(name)
            except FileNotFoundError: pass
            else: raise ValueError('Sealed snapshot already exists')
            with output.open('search.db') as source_stream:
                source_info = os.fstat(source_stream.fileno()); source_bytes = source_info.st_size
                if source_bytes > limits['snapshot_bytes']: raise ValueError('Snapshot size ceiling exceeded')
                # The held directory path permits SQLite to capture committed WAL pages.
                # BEGIN pins the read transaction; inode fences bind its main database.
                uri = 'file:/proc/self/fd/' + str(output.fd) + '/search.db?mode=ro'
                with closing(sqlite3.connect(uri, uri=True)) as src:
                    src.row_factory = sqlite3.Row; src.set_progress_handler(lambda: (clock(), 0)[1], 64)
                    src.execute('BEGIN'); identities = metadata(src)
                    allocation = src.execute('PRAGMA page_count').fetchone()[0] * src.execute('PRAGMA page_size').fetchone()[0]
                    if allocation > limits['snapshot_bytes']: raise ValueError('Snapshot size ceiling exceeded (captured pages)')
                    current = output.info('search.db')
                    if (current.st_dev, current.st_ino) != (source_info.st_dev, source_info.st_ino):
                        raise ValueError('Publication changed before read transaction')
                    check(allocation) if hasattr(check, 'clock') else check()
                    destination_fd = os.open(building, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=retained.fd)
                    with closing(sqlite3.connect('/proc/self/fd/' + str(destination_fd))) as dst:
                        dst.row_factory = sqlite3.Row; dst.execute('PRAGMA journal_mode=OFF'); stage = 'snapshot_backup'
                        def progress(status, remaining, total):
                            callbacks[0] += 1; clock()
                            if os.fstat(destination_fd).st_size > limits['snapshot_bytes']:
                                raise ValueError('Snapshot size ceiling exceeded')
                        src.backup(dst, pages=64, progress=progress, sleep=0)
                        backup_seconds = time.monotonic() - began; stage = 'snapshot_metadata'; clock()
                        dst.execute('PRAGMA query_only=ON'); dst.execute('BEGIN')
                        if metadata(dst) != identities: raise ValueError('Backup transaction metadata changed')
                        current = output.info('search.db'); source_after = os.fstat(source_stream.fileno())
                        if ((current.st_dev, current.st_ino) != (source_info.st_dev, source_info.st_ino) or
                                (source_after.st_size, source_after.st_mtime_ns) != (source_info.st_size, source_info.st_mtime_ns)):
                            raise ValueError('Pinned source database changed')
                        stage = 'snapshot_digest'; digest_began = time.monotonic()
                        dst.set_progress_handler(lambda: (clock(), 0)[1], 64)
                        view = _PersistentFacts(dst, identities)
                        for kind in ('definitions', 'sites', 'scopes', 'imports', 'relationships'):
                            counts[kind] = 0
                            for fact in view.read_facts(kind):
                                clock(); hasher.update(json.dumps(dict(kind=kind, fact=fact), sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode() + b'\n'); counts[kind] += 1
                        digest_seconds = time.monotonic() - digest_began
                    stage = 'snapshot_seal'; check(); os.fsync(destination_fd)
                    destination_bytes = os.fstat(destination_fd).st_size
                    if destination_bytes > limits['snapshot_bytes']: raise ValueError('Snapshot size ceiling exceeded')
                    os.close(destination_fd); destination_fd = None
                    os.rename(building, name, src_dir_fd=retained.fd, dst_dir_fd=retained.fd); os.fsync(retained.fd)
                    _, sha, info = retained.read(name, 0, cancel=lambda: (clock(), False)[1], max_bytes=limits['snapshot_bytes'])
                    if info.st_size != destination_bytes: raise ValueError('Sealed snapshot size changed')
                    check()
        return dict(evidence_mode='pinned_sqlite_backup_v1', semantic_facts_sha256=hasher.hexdigest(), counts=counts,
            identities=identities, artifact=dict(path=name, sha256=sha, bytes=destination_bytes),
            snapshot=dict(source_bytes=source_bytes, destination_bytes=destination_bytes, backup_seconds=backup_seconds,
                digest_seconds=digest_seconds, backup_progress_callbacks=callbacks[0], sealed=True, metadata_verified=True),
            scope='Pinned SQLite read transaction; sealed database reconstructs canonical streams; proof outside refresh wall')
    except BaseException as error:
        checks._adapter_dump(directory, label + '-snapshot-failure.json', dict(stage=stage, sealed=False,
            counts=counts, backup_progress_callbacks=callbacks[0], **checks._adapter_error(error)))
        raise
    finally:
        if destination_fd is not None: os.close(destination_fd)


def _persistent_storage_usage(directory, *, max_files, check):
    """Count allocated artifact lengths, including live temporary SQLite files."""
    total, files = 0, 0
    with SourceRoot(directory) as root:
        def visit(fd):
            nonlocal total, files
            for name in os.listdir(fd):
                check(); info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try: visit(child)
                    finally: os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    files += 1; total += info.st_size
                    if files > max_files: raise ValueError('Artifact reference ceiling exhausted')
                else: raise ValueError('Owned artifact tree contains a nonregular entry')
        visit(root.fd)
    return dict(bytes=total, files=files)


def _persistent_budget(directory, pair, deadline, *, cancel=None, pair_only=False):
    """Explicit live/temp artifact coupling and cooperative wall checks."""
    identities = {}
    observed = dict(checks=0, job_bytes=None if pair_only else 0, pair_bytes=0,
        job_files=None if pair_only else 0, pair_files=0,
        scope='cooperative observed file lengths including live/tmp; full scans at allocation, phase, batch and one-second SQL checkpoints; no kernel aggregate quota')
    for path in (directory, pair):
        with SourceRoot(path) as owner: identities[str(path)] = owner.identity
    def clock():
        if cancel is not None and cancel(): raise InterruptedError('Representative measurement cancelled')
        if time.monotonic() >= deadline: raise TimeoutError('Representative wall ceiling exhausted')
    def check(reserve=0):
        clock()
        if type(reserve) is not int or reserve < 0: raise ValueError('Typed artifact reservation required')
        spaces = ((pair, 8589934592, 20000),) if pair_only else ((directory, 4294967296, 10000), (pair, 8589934592, 20000))
        for path, maximum, refs in spaces:
            with SourceRoot(path) as owner:
                if owner.identity != identities[str(path)]: raise ValueError('Artifact directory owner changed')
            usage = _persistent_storage_usage(path, max_files=refs, check=clock)
            label = 'pair' if path == pair else 'job'
            observed[label + '_bytes'] = max(observed[label + '_bytes'], usage['bytes'])
            observed[label + '_files'] = max(observed[label + '_files'], usage['files'])
            if usage['bytes'] + reserve > maximum: raise ValueError('Coupled artifact byte ceiling exhausted')
        observed['checks'] += 1
        return False
    check.clock = clock
    check.observed = observed
    return check


def _persistent_children(sampler, directory):
    """Register all directly created collector and read-only fence Git children."""
    from contextlib import contextmanager
    from unittest.mock import patch
    from evaluations import engine_checks as checks
    from repo_graph import analysis_queue as queue
    original = subprocess.Popen
    class GitProcess:
        def __init__(self, process): self.process = process
        def __getattr__(self, key): return getattr(self.process, key)
        def __enter__(self): self.process.__enter__(); return self
        def __exit__(self, *args):
            try: return self.process.__exit__(*args)
            finally: sampler.process_finished(self.process)
        def wait(self, *args, **kwargs):
            value = self.process.wait(*args, **kwargs); sampler.process_finished(self.process); return value
    def created(command, *args, **kwargs):
        if type(command) not in (tuple, list) or not command: raise ValueError('Typed direct child command required')
        parts = list(map(str, command))
        if Path(parts[0]).name == 'git':
            # Fixed read-only commands used by source fences and producer revision capture.
            at = 1
            while at + 1 < len(parts) and parts[at] == '-c': at += 2
            if at >= len(parts) or parts[at] not in ('rev-parse', 'show'):
                raise ValueError('Only read-only revision/source-fence Git commands admitted')
            role = 'git_revision'; kwargs['start_new_session'] = True
        elif '--worker-fd' in parts and any(Path(part).name == 'analysis_queue.py' for part in parts):
            role = 'worker'
            if kwargs.get('start_new_session') is not True: raise ValueError('Separate collector session required')
        else: raise ValueError('Unexpected child in representative workload')
        began = time.monotonic_ns(); process = original(command, *args, **kwargs)
        try: sampler.register_process(process, role, began)
        except BaseException as error:
            sampler.error = dict(kind=type(error).__name__, reason='Direct child registration failed')
            cleanup = queue._stop_and_reap(process)
            checks._adapter_dump(directory, 'unregistered-child.json', dict(role=role, cleanup=cleanup))
            raise
        return GitProcess(process) if role == 'git_revision' else process
    @contextmanager
    def scope():
        with patch.object(subprocess, 'Popen', created): yield
    return scope()


def _persistent_snapshot_view(directory, proof, check):
    from contextlib import contextmanager
    @contextmanager
    def view():
        clock = check.clock if hasattr(check, 'clock') else check
        with SourceRoot(directory) as owner:
            _, sha, hashed = owner.read(proof['artifact']['path'], 0, cancel=lambda: (clock(), False)[1],
                max_bytes=proof['artifact']['bytes'])
            if sha != proof['artifact']['sha256'] or hashed.st_size != proof['artifact']['bytes']:
                raise ValueError('Retained proof digest changed')
            with owner.open(proof['artifact']['path']) as stream:
                opened = os.fstat(stream.fileno())
                if any(getattr(opened, field) != getattr(hashed, field) for field in
                    ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')): raise ValueError('Retained proof changed after hash')
                with closing(sqlite3.connect('file:/proc/self/fd/' + str(stream.fileno()) + '?mode=ro&immutable=1', uri=True)) as db:
                    db.row_factory = sqlite3.Row; db.set_progress_handler(lambda: (clock(), 0)[1], 64)
                    if _persistent_facts_metadata(db, proof['identities']['repository_identity']) != proof['identities']:
                        raise ValueError('Retained proof metadata changed')
                    yield _PersistentFacts(db, proof['identities'])
    return view()


class _PersistentLog:
    """Bounded descriptor-owned JSONL windows; keep only fixed manifests in RAM."""
    def __init__(self, directory, label, *, max_windows=None):
        if type(label) is not str or not label.replace('-', '').isalnum():
            raise ValueError('Fixed portable log label required')
        if max_windows is None: max_windows = PERSISTENT_MAX_WINDOWS
        if type(max_windows) is not int or not 1 <= max_windows <= 32: raise ValueError('Finite log windows required')
        self.owner, self.label, self.max_windows = SourceRoot(directory), label, max_windows
        self.fd, self.size, self.hasher, self.windows = None, 0, hashlib.sha256(), []
        try: self._open()
        except BaseException: self.owner.__exit__(); raise

    def _open(self):
        if len(self.windows) >= self.max_windows:
            raise ValueError('Persistent log window budget exhausted')
        self.name = self.label + '-' + str(len(self.windows)).zfill(4) + '.jsonl'
        self.fd = os.open(self.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=self.owner.fd)
        self.size, self.hasher = 0, hashlib.sha256()

    def _seal(self):
        if self.fd is None: return
        try: os.fsync(self.fd)
        finally:
            try: os.close(self.fd)
            finally: self.fd = None
        self.windows.append(dict(path=self.name, bytes=self.size, sha256=self.hasher.hexdigest()))

    def append(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(raw) > 65536: raise ValueError('Persistent log record budget exhausted')
        if self.size + len(raw) > DUAL_LOG_BYTES - 32768:
            self._seal(); self._open()
        view = memoryview(raw)
        while view:
            written = os.write(self.fd, view)
            if written <= 0: raise OSError('Persistent log write made no progress')
            view = view[written:]
        self.hasher.update(raw); self.size += len(raw)

    def close(self):
        try: self._seal()
        finally: self.owner.__exit__()
        return list(self.windows)


class _PersistentReadMeter:
    """Observe controller guarded-stream and digest bytes, including failed reads.

    Stream bytes are bytes actually returned by read(), not stat size, physical
    disk I/O, page-cache misses, or isolated collector/mailbox reads.
    """
    def __init__(self, source_identity, phase, directory=None, *, max_windows=None):
        import threading
        self.source_identity, self.phase = source_identity, phase
        self.local, self.log, self.stack = threading.local(), None, None
        self.directory, self.max_windows = directory, PERSISTENT_MAX_WINDOWS if max_windows is None else max_windows
        from repo_graph.source import empty_source_accounting
        self.counts = dict(empty_source_accounting(), inclusive_read_ns=0)
        self.by_pass = {}

    def __enter__(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from repo_graph import source as source_module
        self.stack = ExitStack()
        if self.directory is not None: self.log = _PersistentLog(self.directory, self.phase + '-source-reads', max_windows=self.max_windows)
        original_read = SourceRoot.read
        meter = self
        def read(owner, path, limit, **kwargs):
            if owner.identity != meter.source_identity: return original_read(owner, path, limit, **kwargs)
            if getattr(meter.local, 'operation', None) is not None:
                raise ValueError('Nested measured source read is unsupported')
            operation = dict(path=path, phase=meter.phase, process_role='controller', hash_full=kwargs.get('hash_full', True),
                limit=limit, pass_kind='full_hash_zero_prefix' if limit == 0 else 'admission_or_configuration',
                started_ns=time.monotonic_ns(), status='failed')
            measurements = kwargs.get('measurements')
            if measurements is None: measurements = {}; kwargs['measurements'] = measurements
            elif type(measurements) is not dict or measurements:
                raise ValueError('Fresh shared source measurements required')
            meter.local.operation = operation
            try:
                result = original_read(owner, path, limit, **kwargs)
                operation.update(status='complete', sha256=result[1])
                return result
            except BaseException as error:
                operation['error_kind'] = type(error).__name__
                raise
            finally:
                meter.local.operation = None
                operation['ended_ns'] = time.monotonic_ns()
                meter.counts['inclusive_read_ns'] += operation['ended_ns'] - operation['started_ns']
                if (type(measurements) is not dict or set(measurements) != set(source_module.SOURCE_ACCOUNTING_FIELDS) or
                        any(type(value) is not int or value < 0 for value in measurements.values())):
                    raise ValueError('Shared actual source accounting unavailable')
                operation.update(measurements)
                group = meter.by_pass.setdefault(operation['pass_kind'], source_module.empty_source_accounting())
                for key, value in measurements.items():
                    meter.counts[key] += value; group[key] += value
                if meter.log is not None: meter.log.append(operation)
        self.stack.enter_context(patch.object(SourceRoot, 'read', read))
        return self

    def __exit__(self, *args):
        try:
            if self.stack is not None: self.stack.__exit__(*args)
        finally:
            self.artifacts = self.log.close() if self.log is not None else []

    def summary(self):
        return dict(self.counts, by_pass=self.by_pass, artifacts=getattr(self, 'artifacts', []),
            scope='controller guarded-stream bytes returned and digest updates; no stat-size inference',
            collector_original_source_reads={'value': None, 'knowledge': 'unmeasured'},
            collector_mailbox_read_bytes={'value': None, 'knowledge': 'unmeasured'},
            all_owned_source_reads_measured=False,
            qualification_blocker='isolated_collector_read_accounting_unmeasured')


class _PersistentSampler(_DualSampler):
    """Reuse PID/RSS checks with finite durable rotation and explicit child gaps."""
    def __init__(self, supervisor=None, *, max_windows=None, invoker=None):
        if max_windows is None: max_windows = PERSISTENT_MAX_WINDOWS
        if type(max_windows) is not int or not 1 <= max_windows <= 32: raise ValueError('Finite sampler windows required')
        self.max_windows = max_windows
        self.windows, self.window_hash = [], hashlib.sha256()
        self.created_children, self.last_sample_ns, self.max_interval_ns = 0, None, 0
        self.excluded, self.excluded_intervals, self.excluded_ns = None, 0, 0
        self.queue_high_water = dict(live_workers=0, pending_requests=0, inflight_reserved_bytes=0,
                                    mailbox_source_bytes=0, mailbox_request_bytes=0, mailbox_result_bytes=0)
        self.directory, self.directory_identity = None, None
        super().__init__(supervisor)
        if invoker is not None:
            try:
                if supervisor is None or type(invoker) is not dict or set(invoker) != {'pid','pgid','sid','starttime_ticks'}:
                    raise ValueError('Typed invoking CLI identity required')
                held = self.owners[supervisor['pid']]
                raw = held.read('stat', 4097)
                if int(raw.rsplit(b') ', 1)[1].split()[1]) != invoker['pid']:
                    raise ValueError('Invoking CLI is not the supervisor parent')
                parent = _DualProcOwner(invoker)
                self.owners[invoker['pid']] = parent
                self._append(self.lifecycles, dict(identity=dict(invoker), role='invoker', registered_ns=self.started_ns,
                    removed_ns=None, scope='read-only verified invoking CLI lineage'))
            except BaseException:
                for owner in self.owners.values(): owner.close()
                raise

    def _write_line(self, raw):
        attached = self.log_fd is not None
        super()._write_line(raw)
        if attached: self.window_hash.update(raw)

    def attach_log(self, directory):
        super().attach_log(directory)
        self.directory, self.directory_identity = self.log_owner.root, self.log_owner.identity
        return self

    def _window_summary(self):
        complete = [row for row in self.samples if row['complete']]
        return dict(samples=len(self.samples), complete_samples=len(complete),
            gap_samples=sum(bool(row['gaps']) for row in self.samples), events=len(self.events),
            peak_sampled_owned_rss_bytes=max((row['owned_rss_bytes'] for row in complete), default=None),
            max_read_skew_ns=max((row['read_skew_ns'] for row in self.samples), default=0))

    def _rotate(self):
        if self.log_owner is None or len(self.windows) >= self.max_windows - 1:
            raise ValueError('Persistent telemetry window budget exhausted')
        os.fsync(self.log_fd); os.close(self.log_fd); self.log_fd = None
        name = 'owned-telemetry-' + str(len(self.windows)).zfill(4) + '.jsonl'
        try: self.log_owner.info(name)
        except FileNotFoundError: pass
        else: raise ValueError('Persistent telemetry archive already exists')
        os.rename('owned-telemetry.jsonl', name, src_dir_fd=self.log_owner.fd, dst_dir_fd=self.log_owner.fd)
        self.windows.append(dict(path=name, sha256=self.window_hash.hexdigest(), bytes=self.bytes, **self._window_summary()))
        active = [dict(row, continuation=True) for row in self.lifecycles if row['identity']['pid'] in self.owners]
        self.samples.clear(); self.events.clear(); self.lifecycles[:] = active
        self.bytes, self.window_hash = 0, hashlib.sha256()
        self.log_fd = os.open('owned-telemetry.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=self.log_owner.fd)
        for row in active:
            raw = self._line(self.lifecycles, row); self._write_line(raw); self.bytes += len(raw)

    def _append(self, collection, value):
        if self.log_owner is not None and (self.bytes + len(self._line(collection, value)) > DUAL_LOG_BYTES - 65536 or
                collection is self.lifecycles and len(collection) >= DUAL_MAX_LIFETIMES - 1 or
                collection is self.events and len(collection) >= 9999): self._rotate()
        super()._append(collection, value)

    def sample(self):
        with self.lock:
            if len(self.samples) >= DUAL_MAX_SAMPLES - 1: self._rotate()
            if self.excluded is None:
                super().sample()
            else:
                started = time.monotonic_ns()
                self._append(self.samples, dict(started_ns=started, ended_ns=started, read_skew_ns=0,
                    phase=self.phase, owners=[], gaps=[dict(kind='excluded_source_fence', scope=self.excluded)],
                    complete=False, owned_rss_bytes=None))
            started = self.samples[-1]['started_ns']
            if self.last_sample_ns is not None: self.max_interval_ns = max(self.max_interval_ns, started - self.last_sample_ns)
            self.last_sample_ns = started

    def exclude_source_fence(self, label):
        """Do not attribute uninstrumented Git checks to complete owned samples."""
        from contextlib import contextmanager
        @contextmanager
        def excluded():
            with self.lock:
                if self.excluded is not None: raise ValueError('Nested telemetry exclusion')
                self.excluded, began = label, time.monotonic_ns()
                self.excluded_intervals += 1
                self._append(self.events, dict(event='source_fence_excluded_begin', scope=label, monotonic_ns=began))
                self.sample()  # Retain a gap even if the fence finishes between cadence ticks.
            try: yield
            finally:
                with self.lock:
                    ended = time.monotonic_ns(); self.excluded_ns += ended - began
                    self._append(self.events, dict(event='source_fence_excluded_end', scope=label,
                        monotonic_ns=ended, elapsed_ns=ended - began))
                    self.excluded = None
                    self.sample()
        return excluded()

    def register_process(self, process, role, created_ns):
        if role not in ('worker', 'git_revision'): raise ValueError('Fixed directly created child role required')
        with self.lock:
            if self.error is not None: raise RuntimeError('Owned sampler already failed')
            if len(self.owners) >= DUAL_MAX_LIVE_OWNERS: raise ValueError('Excess live owned children')
            if len(self.lifecycles) >= DUAL_MAX_LIFETIMES - 1: self._rotate()
            fd = os.open('/proc/' + str(process.pid) + '/stat', os.O_RDONLY | os.O_NOFOLLOW)
            try: raw = os.read(fd, 4097)
            finally: os.close(fd)
            value = _dual_proc_stat(raw, include_state=True); state = value.pop('state')
            fields = raw.rsplit(b') ', 1)[1].split()
            if int(fields[1]) != os.getpid() or not value['pid'] == value['pgid'] == value['sid'] or value['pid'] in self.owners:
                raise ValueError('Direct owned child/session identity required')
            held = _DualProcOwner(value, separate_session=True, allow_exited=True)
            self.owners[value['pid']] = held
            self.created_children += 1
            self._append(self.lifecycles, dict(identity=value, role=role, created_ns=created_ns,
                registered_ns=time.monotonic_ns(), removed_ns=None, state_at_registration=state,
                registration_gap_scope='Popen completion to pinned proc observation; no earlier RSS invented'))
            self.sample()  # A fast exiting child yields an explicit gap, never zero RSS.
            return value

    def process_finished(self, process):
        from repo_graph.analysis_queue import _group_exists
        with self.lock:
            if process.pid not in self.owners: return
            row = next(row for row in reversed(self.lifecycles) if row['identity']['pid'] == process.pid)
            if row['role'] != 'git_revision': raise ValueError('Collectors require canonical queue cleanup')
            if process.returncode is None or _group_exists(process): raise ValueError('Owned Git child cleanup incomplete')
            self.owners.pop(process.pid).close()
            row.update(removed_ns=time.monotonic_ns(), cleanup={'leader_reaped': True, 'group_absent': True})
            self._append(self.events, dict(event='git_cleanup', identity=row['identity'], phase=self.phase,
                monotonic_ns=row['removed_ns'], cleanup=row['cleanup']))

    def observe(self, event):
        with self.lock:
            try:
                if self.error is not None: return False
                if len(self.events) >= 9999 or len(self.lifecycles) >= DUAL_MAX_LIFETIMES - 1: self._rotate()
                from repo_graph.analysis_queue import _event_valid
                _event_valid(event)
                for key in self.queue_high_water: self.queue_high_water[key] = max(self.queue_high_water[key], event[key])
                if event.get('event') == 'readiness' and event.get('worker') is not None:
                    identity = event['worker']; owner = self.owners.get(identity['pid'])
                    if event['controller'] != self.controller or owner is None or owner.identity != identity:
                        raise ValueError('Readiness differs from directly created collector identity')
                    owner.recheck(require_live=True)
                    self._append(self.events, dict(event, phase=self.phase))
                    return True
                return super().observe(event)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                self.error = dict(kind=type(error).__name__, reason=str(error)[:256]); return False

    def finish(self):
        with self.lock:
            children = [dict(owner.identity) for owner in self.owners.values() if owner.identity['pid'] not in
                {row['identity']['pid'] for row in self.lifecycles if row['role'] in ('controller', 'supervisor', 'invoker')}]
            for row in self.lifecycles:
                if row['role'] == 'invoker':
                    row.update(removed_ns=time.monotonic_ns(), removal_scope='sampling ended; process exit not claimed')
        last = super().finish()
        if last.get('sampler_stopped') is not True: return last
        with SourceRoot(self.directory) as owner:
            if owner.identity != self.directory_identity: raise ValueError('Telemetry directory owner changed')
            info = owner.info('owned-telemetry.jsonl')
            if info.st_size != self.bytes: raise ValueError('Telemetry stream size changed')
        self.windows.append(dict(path='owned-telemetry.jsonl', sha256=self.window_hash.hexdigest(), bytes=self.bytes,
                                 **self._window_summary()))
        peaks = [window['peak_sampled_owned_rss_bytes'] for window in self.windows if window['peak_sampled_owned_rss_bytes'] is not None]
        return dict(schema_version=1, label='peak_sampled_owned_rss_bytes',
            peak_sampled_owned_rss_bytes=max(peaks, default=None), windows=self.windows,
            sample_count=sum(window['samples'] for window in self.windows),
            complete_sample_count=sum(window['complete_samples'] for window in self.windows),
            sample_gap_count=sum(window['gap_samples'] for window in self.windows),
            created_child_count=self.created_children, largest_start_interval_ns=self.max_interval_ns,
            max_read_skew_ns=max(window['max_read_skew_ns'] for window in self.windows),
            requested_interval_seconds=DUAL_INTERVAL_SECONDS, max_windows=self.max_windows,
            per_window_limits=dict(samples=DUAL_MAX_SAMPLES, lifetimes=DUAL_MAX_LIFETIMES,
                live_owners=DUAL_MAX_LIVE_OWNERS, bytes=DUAL_LOG_BYTES),
            remaining_registered_owned_child_owners=children, sampler_stopped=True, error=last['error'],
            queue_high_water=self.queue_high_water,
            unsampled_peak_bound=False,
            all_created_children_registered=self.error is None and self.excluded_intervals == 0,
            all_measured_phase_children_registered=self.error is None,
            excluded_source_fence_intervals=self.excluded_intervals, excluded_source_fence_ns=self.excluded_ns,
            child_registration_scope=('producer-created collectors and read-only revision/source-fence Git children' if not self.excluded_intervals
                else 'producer-created collectors and revision children; source-fence Git intervals excluded'),
            scope='complete sampled current-RSS sums only; startup, short-child and registration gaps explicit; shared pages counted per owner')


def _persistent_fact_digest(index, directory, label):
    """Stream canonical persisted facts; no finite Snapshot or whole-RAM mirror."""
    from evaluations import engine_checks as checks
    counts, hasher, size = {}, hashlib.sha256(), 0
    name = label + '.facts.jsonl'
    metadata_before = index.metadata()
    with SourceRoot(directory) as owner, owner.atomic_writer(name) as stream:
        for kind in ('definitions', 'sites', 'scopes', 'imports', 'relationships'):
            counts[kind] = 0
            for fact in index.read_facts(kind):
                raw = json.dumps(dict(kind=kind, fact=fact), sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
                size += len(raw)
                if size > DUAL_LOG_BYTES: raise ValueError('Streamed fact proof byte budget exhausted')
                stream.write(raw); hasher.update(raw); counts[kind] += 1
    metadata_after = index.metadata()
    if metadata_before != metadata_after: raise ValueError('Persisted fact snapshot changed during proof')
    return dict(semantic_facts_sha256=hasher.hexdigest(), counts=counts, identities=metadata_after,
        artifact=dict(path=name, sha256=hasher.hexdigest(), bytes=size),
        scope='Ordered persisted read_facts streams; metadata rechecked across all five streams; proof cost outside refresh wall')


def _persistent_impacts(index, expected, directory, label):
    """Grade only selected persisted physical facts after production."""
    from evaluations.acceptance import _proof_impact, _proof_physical
    from evaluations import engine_checks as checks
    if type(expected) is not list or not 1 <= len(expected) <= 8:
        raise ValueError('Finite nonempty frozen impact projection required')
    before = index.metadata(); wanted = set(); anchors = set()
    for impact in expected:
        anchors.add((impact['site']['path'], tuple(impact['site']['range'][key] for key in
            ('start_byte', 'end_byte', 'start_line', 'end_line'))))
        for declaration in [impact['caller_declaration'], *impact['target_declarations'],
                *[impact[key] for key in ('surviving_physical_declaration', 'non_target_physical_declaration') if key in impact]]:
            wanted.add(_proof_physical(declaration))
    facts = dict(definitions=[], sites=[])
    for definition in index.read_facts('definitions'):
        if definition['id'] in wanted: facts['definitions'].append(definition)
        if len(facts['definitions']) > 32: raise ValueError('Impact declaration projection budget exhausted')
    for site in index.read_facts('sites'):
        anchor = (site['path'], tuple(site['range'][key] for key in ('start_byte', 'end_byte', 'start_line', 'end_line')))
        if anchor in anchors: facts['sites'].append(site)
        if len(facts['sites']) > 8: raise ValueError('Impact site projection budget exhausted')
    if index.metadata() != before: raise ValueError('Impact projection generation changed')
    name = label + '-impact.json'; checks._adapter_dump(directory, name, dict(identities=before, facts=facts))
    raw, sha = checks._adapter_bytes(directory, name, cap=128 * 1024)
    for impact in expected: _proof_impact(facts, impact)
    return dict(passed=True, checked_impacts=len(expected), expected_sha256=digest(expected),
        identities=before, artifact=dict(path=name, sha256=sha, bytes=len(raw)),
        selected_declarations=len(facts['definitions']), selected_sites=len(facts['sites']))


def _persistent_queries(index, directory, *, specs=PERSISTENT_QUERY_SPECS, fanout=None, progress=None, facts_reader=None):
    """Frozen workloads: cold application session and ten cached warm calls.

    Source selector resolution and correctness grading are outside each measured
    run(). The OS cache is not flushed. Complete responses are privately kept.
    """
    from repo_graph.analysis_queries import Queries, encoded
    from evaluations import engine_checks as checks
    results = {} if progress is None else progress
    results.update(freeze_sha256=PERSISTENT_FREEZE_SHA, generation=None,
        identities=None, scope='unmodified fresh publication only; cold application session, OS cache possibly warm',
        cold_calls=1, warm_calls=10, limits=dict(PERSISTENT_QUERY_LIMITS), workloads=[], cursor_control=None, passed=False)
    stage = 'workload_validation'

    def record(session, payload, name, temperature, samples):
        sample = dict(temperature=temperature, passed=False); samples.append(sample)
        began = time.monotonic_ns()
        try: response = session.run(payload)
        finally: sample['elapsed_seconds'] = (time.monotonic_ns() - began) / 1e9
        raw = encoded(response); limits = payload['limits']; name += '.json'
        sample.update(wire_bytes=len(raw), **{key: response.get(key) for key in
            ('generation', 'source_identity', 'examined_relationships', 'examined_symbols', 'returned_entities',
             'returned_symbol_handles', 'returned_edges', 'excerpt_bytes', 'storage_progress_callbacks',
             'storage_setup_seconds', 'snapshot_copy_seconds', 'total_count', 'truncated', 'stop_reason')})
        if len(raw) > limits['max_response_bytes']: raise ValueError('Query receipt byte budget exhausted')
        with SourceRoot(directory) as owner:
            if not owner.secure: raise ValueError('Private query receipt directory required')
            with owner.atomic_writer(name) as stream: stream.write(raw)
        retained, sha = checks._adapter_bytes(directory, name, cap=limits['max_response_bytes'])
        if retained != raw: raise ValueError('Canonical query receipt changed')
        sample['artifact'] = dict(path=name, sha256=sha, bytes=len(retained))
        return response, sample

    def grade(response, payload, sample):
        limits = payload['limits']; seed = payload['seed']; handles = set()
        for row in response['rows']:
            if 'site' in row:
                handles.update(row[key]['id'] for key in ('caller', 'target') if row.get(key) is not None)
            else: handles.add(row['id'])
        if (response['generation'] != metadata['generation'] or response['source_identity'] != metadata['source_identity'] or
                response['repository_identity'] != metadata['repository_identity'] or
                response['returned_symbol_handles'] != len(handles) or response['returned_entities'] != len(handles - {seed}) or
                len(handles) > limits['max_entities'] or response['returned_edges'] > limits['max_edges'] or
                response['examined_relationships'] > limits['max_examined_relationships'] or
                sample['wire_bytes'] > limits['max_response_bytes'] or response['excerpt_bytes'] != 0 or
                response['stop_reason'] in ('cancelled', 'deadline_exceeded', 'continuation_capacity_exhausted',
                    'continuation_state_budget_exceeded', 'snapshot_session_capacity_exhausted')):
            raise ValueError('Frozen query generation, counter, work or stop control failed')

    try:
        if type(specs) not in (tuple, list) or not 1 <= len(specs) <= 6:
            raise ValueError('Finite frozen query workload required')
        facts_reader = index if facts_reader is None else facts_reader
        stage = 'metadata'; metadata = facts_reader.metadata()
        results.update(generation=metadata['generation'], identities=metadata)
        stage = 'selectors'; selected = {spec['id']: [] for spec in specs}
        for definition in facts_reader.read_facts('definitions'):
            for spec in specs:
                span = tuple(definition['range'][key] for key in ('start_byte', 'end_byte', 'start_line', 'end_line'))
                if definition['path'] == spec['path'] and definition['name'] == spec['name'] and span == tuple(spec['span']):
                    selected[spec['id']].append(definition['id'])
                    if len(selected[spec['id']]) > 1: raise ValueError('Ambiguous frozen query selector')
        if facts_reader.metadata() != metadata or any(len(values) != 1 for values in selected.values()):
            raise ValueError('Missing or changed frozen query selector')
        anchors = {spec['id']: spec['required_site'] for spec in specs if 'required_site' in spec}
        matched = Counter()
        if anchors:
            for site in facts_reader.read_facts('sites'):
                for key, anchor in anchors.items():
                    if site['path'] == anchor['path'] and site['range'] == anchor['range']:
                        if (site['text'] != anchor['text'] or site['provenance']['source_sha256'] != anchor['source_sha256'] or
                                site['certainty'] != anchor['certainty']): raise ValueError('Frozen query callsite source changed')
                        matched[key] += 1
            if any(matched[key] != 1 for key in anchors): raise ValueError('Missing frozen query callsite')
        stage = 'queries'
        for spec in specs:
            payload = dict(seed=selected[spec['id']][0], operation=spec['operation'], depth=spec.get('depth', 2),
                limits=dict(PERSISTENT_QUERY_LIMITS, **spec.get('overrides', {})))
            samples = []; semantic = None
            workload = dict(id=spec['id'], operation=spec['operation'], seed=payload['seed'],
                limits=payload['limits'], samples=samples, passed=False)
            results['workloads'].append(workload)
            with Queries(index.output, owner=index.output_owner, repository_identity=index.owner) as session:
                for number in range(11):
                    response, sample = record(session, payload, spec['id'] + '-' + str(number).zfill(2),
                                              'cold' if number == 0 else 'warm', samples)
                    grade(response, payload, sample)
                    current = digest(response['rows'])
                    if semantic is None: semantic = current
                    elif current != semantic: raise ValueError('Same-generation warm query rows changed')
                    empty = spec.get('expected_empty', spec['id'] == 'F-REFERENCE-LOCAL')
                    if not empty and not response['rows']:
                        raise ValueError('Frozen query returned vacuous rows')
                    if spec.get('expected_empty') is True and (response['rows'] or response['truncated'] or
                            response['cursor'] is not None or response['total_count'] != {'kind':'exact', 'value':0}):
                        raise ValueError('Frozen known-empty query was not proven empty')
                    if (spec.get('preserve_unresolved', spec['id'] == 'F-UNRESOLVED-DYNAMIC') and
                            not any(row.get('certainty') == 'unresolved' for row in response['rows'])):
                        raise ValueError('Frozen dynamic query lost unresolved evidence')
                    if spec['id'] in anchors:
                        from evaluations.acceptance import _proof_physical
                        anchor = anchors[spec['id']]
                        matches = [row for row in response['rows'] if row.get('site', {}).get('path') == anchor['path'] and
                                   row['site']['range'] == anchor['range']]
                        if (not matches or any(row['site']['source_sha256'] != anchor['source_sha256'] or
                                row['certainty'] != anchor['certainty'] for row in matches) or
                                {row['target']['id'] for row in matches if row['target'] is not None} !=
                                {_proof_physical(value) for value in anchor['target_declarations']}):
                            raise ValueError('Frozen query physical target relationship missing')
                        declarations = {_proof_physical(value): value for value in anchor['target_declarations']}
                        for row in matches:
                            if row['target'] is None: continue
                            expected = declarations[row['target']['id']]
                            if (row['target']['range'] != expected['range'] or row['target']['path'] != expected['path'] or
                                    row['target']['source_sha256'] != expected['source_sha256'] or
                                    row['target']['name'] != expected['name'] or row['target'].get('name_truncated') is not False):
                                raise ValueError('Frozen query target declaration source changed')
                    if spec['id'] == 'F-FANOUT-STOP' and (not response['truncated'] or response['cursor'] is None):
                        raise ValueError('Frozen fanout did not exercise bounded continuation')
                    sample['passed'] = True
            warm = sorted(sample['elapsed_seconds'] for sample in samples[1:])
            workload.update(warm_p50_seconds=statistics.median(warm),
                warm_p95_seconds=warm[math.ceil(.95 * len(warm)) - 1], rows_sha256=semantic, passed=True)
            if spec['id'] == 'F-FANOUT-STOP':
                if type(fanout) is not dict: raise ValueError('Frozen fanout occurrence oracle required')
                expected = [(row['site']['path'], tuple(row['site']['range'][key] for key in
                    ('start_byte', 'end_byte', 'start_line', 'end_line')), row['site']['source_sha256'])
                    for row in fanout['invocations'] if row['caller_key'] == 'PY.fanout.hub']
                if not expected or len(expected) > 256: raise ValueError('Finite nonempty fanout oracle required')
                seen, pages, cursors = [], [], set()
                control = dict(passed=False, pages=pages, returned_occurrences=0,
                    expected_occurrences=len(expected), max_pages=64)
                results['cursor_control'] = control
                with Queries(index.output, owner=index.output_owner, repository_identity=index.owner) as session:
                    while len(pages) < 64:
                        response, sample = record(session, payload, 'fanout-page-' + str(len(pages)).zfill(2), 'cursor_control', pages)
                        grade(response, payload, sample)
                        for row in response['rows']:
                            site = row['site']; seen.append((site['path'], tuple(site['range'][key] for key in
                                ('start_byte', 'end_byte', 'start_line', 'end_line')), site['source_sha256']))
                        control['returned_occurrences'] = len(seen); sample['passed'] = True; cursor = response['cursor']
                        if cursor is None: break
                        if cursor in cursors: raise ValueError('Fanout cursor did not advance')
                        cursors.add(cursor); payload = dict(payload, cursor=cursor)
                    if (response['cursor'] is not None or response['truncated'] or seen != expected or
                            len(seen) != len(set(seen))):
                        raise ValueError('Frozen fanout pages incomplete, reordered or duplicate')
                control.update(passed=True, physical_occurrences_sha256=digest(seen))
        if facts_reader.metadata() != metadata: raise ValueError('Query measurement publication changed')
        results['passed'] = True
        stage = 'query_report_retention'
        checks._adapter_dump(directory, 'queries.json', results)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError, sqlite3.Error) as error:
        results['passed'] = False
        results['failure'] = dict(stage=stage, **checks._adapter_error(error))
        try: checks._adapter_dump(directory, 'queries.json', results)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError) as retention_error:
            results['retention_failure'] = dict(stage='query_report_retention', **checks._adapter_error(retention_error))
        raise
    return results


def _persistent_source_accounting():
    from repo_graph.source import empty_source_accounting
    return dict(schema_version=1, accounting_complete=True,
        passes={name: empty_source_accounting() for name in ('controller_original_source',
            'controller_source_validation', 'mailbox_source', 'worker_source_validation', 'native_source_validation')},
        worker_requests_with_accounting=0, worker_requests_unknown=0, collector_original_source_stream_bytes=0,
        scope='admitted source/configuration original reads and content hashes, immutable mailbox reads and explicit source-validation hashes; '
              'implementation/control/persisted-fact hashes and source-fence Git I/O excluded')


def _persistent_sum_source_row(target, row, *, buffer=False):
    from repo_graph.source import SOURCE_ACCOUNTING_FIELDS
    if (type(row) is not dict or set(row) != set(SOURCE_ACCOUNTING_FIELDS) or
            any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in row.values()) or
            row['operations'] != row['successful_operations'] + row['failed_operations'] or
            row['hash_passes'] > row['operations'] or row['open_operations'] > row['operations'] or
            not row['operations'] and any(row.values()) or row['hashed_bytes'] and not row['hash_passes'] or
            buffer and any(row[key] for key in ('stream_bytes', 'open_operations', 'returned_prefix_bytes'))):
        raise ValueError('Invalid fixed actual source counter row')
    for key in SOURCE_ACCOUNTING_FIELDS: target[key] += row[key]


def _persistent_add_source_accounting(total, result, budget):
    """Reuse canonical receipt validation; never reconstruct child bytes from size."""
    from repo_graph.analysis_queue import _source_accounting_valid
    value = result.resources.get('telemetry', {}).get('source_accounting')
    if (type(value) is not dict or set(value) != {'schema_version', 'controller_source_validation',
            'worker_requests_with_accounting', 'worker_requests_unknown', 'worker_accounting_complete', 'scope'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            any(type(value[key]) is not int or value[key] < 0 for key in
                ('worker_requests_with_accounting', 'worker_requests_unknown')) or
            type(value['worker_accounting_complete']) is not bool or
            value['worker_accounting_complete'] != (value['worker_requests_unknown'] == 0)):
        total['accounting_complete'] = False
        raise ValueError('Canonical source accounting unavailable')
    _persistent_sum_source_row(total['passes']['controller_source_validation'],
                               value['controller_source_validation'], buffer=True)
    total['accounting_complete'] &= value['worker_accounting_complete']
    seen = 0
    try:
        for worker in result.resources.get('worker_resources', []):
            for file in worker:
                accounting = file.get('source_accounting')
                _source_accounting_valid(accounting, budget=budget)
                seen += 1
                for name, row in accounting['passes'].items():
                    _persistent_sum_source_row(total['passes'][name], row, buffer=name != 'mailbox_source')
                total['collector_original_source_stream_bytes'] += accounting['original_source_stream_bytes']
    finally:
        total['worker_requests_with_accounting'] += seen
        total['worker_requests_unknown'] += value['worker_requests_unknown'] + max(0, value['worker_requests_with_accounting'] - seen)
    if seen != value['worker_requests_with_accounting']:
        total['accounting_complete'] = False
        raise ValueError('Canonical accounting receipt count changed')


def _persistent_attempt(index, records, label, mode, concurrency, directory, sampler, *, protocol=None, check=None):
    """Observe the canonical writer alias and retain bounded full batch receipts."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from repo_graph import analysis as runtime, analysis_queue as queue, search
    from evaluations import engine_checks as checks
    if (mode, concurrency) not in PERSISTENT_MODES:
        raise ValueError('Persistent reference serial1 or candidate queued2 required')
    stages, batch_count, artifacts = {}, [0], []
    source_accounting = _persistent_source_accounting()
    native_timings, queue_timings = Counter(), Counter()
    collection_totals = dict(actual_workers_started=0, files_collected=0, child_file_user_seconds=0.0,
                             child_file_system_seconds=0.0)
    original_collect, original_popen = runtime.collect_files, subprocess.Popen
    def measured(name, function):
        def invoke(*args, **kwargs):
            began = time.monotonic_ns()
            try: return function(*args, **kwargs)
            finally:
                row = stages.setdefault(name, dict(calls=0, inclusive_seconds=0.0))
                row['calls'] += 1; row['inclusive_seconds'] += (time.monotonic_ns() - began) / 1e9
        return invoke
    class GitProcess:
        def __init__(self, process): self.process = process
        def __getattr__(self, key): return getattr(self.process, key)
        def __enter__(self): self.process.__enter__(); return self
        def __exit__(self, *args):
            try: return self.process.__exit__(*args)
            finally: sampler.process_finished(self.process)
        def wait(self, *args, **kwargs):
            result = self.process.wait(*args, **kwargs); sampler.process_finished(self.process); return result
    def created(command, *args, **kwargs):
        if type(command) not in (tuple, list) or not command or kwargs.get('start_new_session') is not True:
            raise ValueError('Profiler accepts only directly created isolated children')
        role = ('git_revision' if Path(command[0]).name == 'git' and 'rev-parse' in command else
                'worker' if '--worker-fd' in command and any(Path(str(part)).name == 'analysis_queue.py' for part in command) else None)
        if role is None: raise ValueError('Unexpected child in persistent source-only workload')
        began = time.monotonic_ns(); process = original_popen(command, *args, **kwargs)
        try: sampler.register_process(process, role, began)
        except BaseException as error:
            sampler.error = dict(kind=type(error).__name__, reason='Direct child registration failed')
            cleanup = queue._stop_and_reap(process)
            checks._adapter_dump(directory, label + '-unregistered-child.json', dict(role=role, cleanup=cleanup))
            raise
        return GitProcess(process) if role == 'git_revision' else process
    def collected(*args, **kwargs):
        stage = 'collection'
        try:
            if check is not None: check()
            if batch_count[0] >= PERSISTENT_MAX_BATCHES: raise ValueError('Persistent collection batch budget exhausted')
            if kwargs.get('mode') != mode or kwargs.get('concurrency') != concurrency or kwargs.get('observer') is not None:
                raise ValueError('Canonical collection mode or observer differs from profiler')
            def observe(event):
                if event.get('mode') != mode or event.get('configured_concurrency') != concurrency:
                    sampler.error = dict(kind='ValueError', reason='Observed producer mode/concurrency mismatch'); return False
                return sampler.observe(event)
            kwargs.update(telemetry=True, observer=observe, observer_max_events=10000)
            result = measured('collection_controller', original_collect)(*args, **kwargs)
            batch_count[0] += 1
            full = dict(status=result.status, stop_reason=result.stop_reason, resources=result.resources,
                        failures=result.failures, cleanup=result.cleanup)
            name = label + '-collection-' + str(batch_count[0]).zfill(4) + '.json'
            stage = 'source_accounting'; accounting_error = None
            try: _persistent_add_source_accounting(source_accounting, result, index.budget)
            except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError, sqlite3.Error) as error:
                source_accounting['accounting_complete'] = False
                source_accounting['accounting_failure'] = checks._adapter_error(error)
                accounting_error = error
            stage = 'batch_receipt_retention'
            if check is not None: check(len(json.dumps(full, indent=2, sort_keys=True, allow_nan=False).encode()) + 1)
            checks._adapter_dump(directory, name, full)
            if check is not None: check()
            raw, sha = checks._adapter_bytes(directory, name, cap=DUAL_LOG_BYTES)
            artifacts.append(dict(path=name, sha256=sha, bytes=len(raw)))
            if accounting_error is not None:
                stage = 'source_accounting'
                raise accounting_error
            stage = 'collection_metadata'
            collection_totals['actual_workers_started'] += result.resources.get('workers_started', 0)
            collection_totals['files_collected'] += result.resources.get('files_collected', 0)
            queue_timings.update(result.resources.get('telemetry', {}).get('controller_timings', {}))
            for worker in result.resources.get('worker_resources', []):
                for file in worker:
                    native_timings.update(file.get('timings', {}))
                    for kind in ('user', 'system'):
                        collection_totals['child_file_' + kind + '_seconds'] += file.get('file_' + kind + '_seconds', 0)
            return result
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError, sqlite3.Error) as error:
            source_accounting['accounting_complete'] = False
            source_accounting['failure'] = dict(stage=stage, **checks._adapter_error(error))
            raise
    sampler.set_phase(label)
    began, refresh_end = time.monotonic_ns(), None
    attempt = dict(label=label, status='running', mode=mode, concurrency=concurrency)
    meter = _PersistentReadMeter(index.owner, label, directory,
        max_windows=protocol['config']['ceilings']['sampler_windows'] if protocol else PERSISTENT_MAX_WINDOWS)
    try:
        with ExitStack() as stack:
            stack.enter_context(meter)
            stack.enter_context(patch.object(runtime, 'collect_files', collected))
            if protocol is None: stack.enter_context(patch.object(subprocess, 'Popen', created))
            if check is not None:
                phase_deadline = time.monotonic() + protocol['config']['ceilings']['index_phase_wall_seconds']
                last_storage_check = [0.0]
                def bounded():
                    check.clock()
                    if time.monotonic() >= phase_deadline: raise TimeoutError('Index phase wall ceiling exhausted')
                    # Finite storage inventories at checkpoints, not one full tree per fact.
                    if time.monotonic() - last_storage_check[0] >= 1:
                        check(); last_storage_check[0] = time.monotonic()
                    return False
                def copied(src, dst, length=0):
                    check(os.fstat(src.fileno()).st_size)
                    while True:
                        bounded(); raw = src.read(65536)
                        if not raw: break
                        dst.write(raw)
                    bounded(); check()
                stack.enter_context(patch.object(search.shutil, 'copyfileobj', copied))
            for module, name, stage in ((runtime, 'connect', 'storage_open'), (runtime, '_schema', 'sqlite_schema'),
                    (runtime.native, 'resolver', 'resolver_setup'),
                    (runtime.native.CollectedFile, 'emit_sites', 'binding_resolution'),
                    (runtime, 'project_function_evidence', 'function_projection'),
                    (runtime, '_coverage', 'generation_coverage'), (runtime, '_git_observation', 'git_revision')):
                stack.enter_context(patch.object(module, name, measured(stage, getattr(module, name))))
            for name in ('execute', 'executemany', 'executescript'):
                stack.enter_context(patch.object(search.IndexConnection, name,
                    measured('sqlite_' + name, getattr(search.IndexConnection, name))))
            stack.enter_context(patch.object(search.IndexConnection, '__exit__',
                measured('commit_fsync_publication', search.IndexConnection.__exit__)))
            stack.enter_context(patch.object(runtime._Files, 'fingerprint',
                measured('dependency_fingerprint', runtime._Files.fingerprint)))
            attempt['receipt'] = index.refresh(records, mode=mode, concurrency=concurrency,
                cancel=(lambda: bounded() or sampler.error is not None) if check else lambda: sampler.error is not None,
                evidence_directory=directory)
        refresh_end = time.monotonic_ns()
        if check is not None: check()
        attempt['status'] = attempt['receipt']['status']
        if attempt['status'] == 'ready':
            sampler.set_phase(label + '-proof-retention'); proof_began = time.monotonic_ns()
            try:
                attempt['streamed_facts'] = (_persistent_snapshot(index, directory, label, check=check,
                    limits=protocol['config']['ceilings']) if protocol else _persistent_fact_digest(index, directory, label))
            finally: attempt['proof_retention_seconds'] = (time.monotonic_ns() - proof_began) / 1e9
        if sampler.error is not None:
            attempt.update(status='measurement_failed', measurement_error=dict(sampler.error))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError, sqlite3.Error) as error:
        attempt.update(status='failed', error=checks._adapter_error(error))
        if index.last_attempt is not None: attempt['receipt'] = index.last_attempt
    finally:
        source_reads = meter.summary()
        from repo_graph.source import SOURCE_ACCOUNTING_FIELDS
        _persistent_sum_source_row(source_accounting['passes']['controller_original_source'],
                                   {key: source_reads[key] for key in SOURCE_ACCOUNTING_FIELDS})
        source_accounting['total_source_stream_bytes'] = sum(row['stream_bytes'] for row in source_accounting['passes'].values())
        source_accounting['total_hashed_bytes'] = sum(row['hashed_bytes'] for row in source_accounting['passes'].values())
        source_accounting['total_hash_passes'] = sum(row['hash_passes'] for row in source_accounting['passes'].values())
        source_accounting['totals_kind'] = 'exact' if source_accounting['accounting_complete'] else 'lower_bound'
        if source_accounting['accounting_complete']:
            source_reads['collector_original_source_reads'] = dict(value=0, knowledge='validated_immutable_mailbox_transport')
            source_reads['collector_mailbox_read_bytes'] = dict(value=source_accounting['passes']['mailbox_source']['stream_bytes'],
                                                              knowledge='trusted_terminal_receipt_counters')
            source_reads['qualification_blocker'] = 'source_fence_owned_io_excluded'
        attempt.update(wall_seconds=((refresh_end or time.monotonic_ns()) - began) / 1e9,
            observed_attempt_seconds=(time.monotonic_ns() - began) / 1e9, stages=stages,
            source_reads=source_reads, source_accounting=source_accounting,
            collection_batches=batch_count[0], collection_artifacts=artifacts,
            collection_totals=collection_totals, native_file_timing_sums=dict(native_timings),
            queue_controller_timing_sums=dict(queue_timings),
            timing_scope='Inclusive observed StructuralIndex.refresh wall includes instrumentation and batch retention; proof retention separate; stages overlap and are not additive; SQLite timings cover statement initiation, cursor iteration remains in parent wall')
        sampler.set_phase(label + '-receipt-retention')
        checks._adapter_dump(directory, label + '.json', attempt)
    return attempt


def _persistent_capture(root, protocol=None):
    from evaluations import engine_checks as checks
    from evaluations.acceptance import committed
    from repo_graph import analysis as runtime
    bound = _dual_capture(root)
    for path in ('repo_graph/analysis.py', 'repo_graph/analysis_queries.py', 'repo_graph/analysis_queue.py',
                 'evaluations/code-understanding/test_performance.py', 'evaluations/code-understanding/test_analysis.py',
                 'evaluations/code-understanding/test_queued_collector.py', 'tests/test_repo_graph.py'):
        bound['implementation'][path] = checks._adapter_bytes(root, path, cap=2 * 1024 * 1024)[1]
    if not committed(root, bound['implementation']): raise ValueError('Committed persistent profiler binding required')
    bound['persistent_writer_identity'] = runtime.analyzer_identity()
    bound['persistent_limits'] = _persistent_limits()
    if protocol is not None:
        bound['representative_protocol'] = dict(protocol_sha256=protocol['protocol_sha256'],
            header_sha256=REPRESENTATIVE_HEADER_SHA, manifest_sha256=REPRESENTATIVE_MANIFEST_SHA,
            decision_sha256=REPRESENTATIVE_DECISION_SHA, ceilings=protocol['config']['ceilings'],
            query_projection_sha256=digest(protocol['config']['queries']),
            impacts_sha256=protocol['config']['impacts_sha256'])
    _persistent_recheck(root, bound)
    return bound


def _persistent_recheck(root, bound):
    from repo_graph import analysis as runtime
    observed = _dual_recheck(root, bound)
    if runtime.analyzer_identity() != bound['persistent_writer_identity']:
        raise ValueError('Loaded persistent writer identity changed')
    limits = _persistent_limits()
    if limits != bound['persistent_limits']: raise ValueError('Persistent measurement configuration changed')
    result = dict(observed, persistent_writer_identity=bound['persistent_writer_identity'], persistent_limits=limits)
    if 'representative_protocol' in bound: result['representative_protocol'] = bound['representative_protocol']
    return result


def _persistent_limits():
    return dict(max_windows=PERSISTENT_MAX_WINDOWS, max_batches=PERSISTENT_MAX_BATCHES,
        max_live_owners=DUAL_MAX_LIVE_OWNERS, per_window_samples=DUAL_MAX_SAMPLES,
        per_window_lifetimes=DUAL_MAX_LIFETIMES, per_window_bytes=DUAL_LOG_BYTES,
        evaluation_freeze_sha256=PERSISTENT_FREEZE_SHA, query_projection_sha256=digest(PERSISTENT_QUERY_SPECS),
        query_limits=dict(PERSISTENT_QUERY_LIMITS), cold_calls=1, warm_calls=10, max_cursor_pages=64,
        phases_sha256=digest(PERSISTENT_PHASES), impacts_sha256=digest(PERSISTENT_IMPACT_SHA))


def _persistent_run(root, directory, source, source_owner, bound, mode, concurrency, supervisor,
                    *, protocol=None, protocol_directory=None, original_source=None, pair=None, invoker=None):
    """One finite job over the supervisor's held shared source owner."""
    import resource
    from dataclasses import asdict
    from repo_graph.analysis import StructuralIndex
    from evaluations import engine_checks as checks
    if protocol is None:
        blobs, records, edits = _dual_inputs(root, bound)
        edit_ids, impact_sha, specs = ('U-PY-BODY', 'U-PY-EXPORT'), PERSISTENT_IMPACT_SHA, PERSISTENT_QUERY_SPECS
        check = None
    else:
        blobs, records, edits = None, None, {row['id']: row for row in protocol['config']['updates']}
        edit_ids = tuple(edits); impact_sha = protocol['config']['impacts_sha256']; specs = protocol['config']['queries']
        check = _persistent_budget(directory, pair, time.monotonic() + protocol['config']['ceilings']['job_wall_seconds'])
    report = dict(schema_version=1, kind='persistent_corpus' if protocol else 'persistent_fixture', status='running', mode=mode,
        concurrency=concurrency, binding_before=bound, phases=[], equivalence={}, source_impacts={}, model_calls=0,
        engine_selected=False, qualification_complete=False, representative_corpus_profiled=False,
        resource_budgets_frozen=False, query_measurements='unmodified_fresh_fixture_only', source_data_accounting_complete=False,
        all_owned_source_reads_measured=False,
        qualification_blockers=['source_data_accounting_unsettled', 'representative_costs_unmeasured',
                                'representative_query_costs_unmeasured', 'resource_budgets_unfrozen',
                                'source_fence_children_excluded_from_rss'],
        input_inventory_sha256=protocol['header']['manifest']['records_sha256'] if protocol else digest(records),
        input_file_count=2978 if protocol else len(records),
        limitations=['one representative repetition of three; remaining corpora pending' if protocol else 'finite fixture only',
                     'sampled current RSS is not exact peak or a hard tree bound',
                     'inclusive stage timings overlap; child timing sums are not wall time',
                     'production wall includes instrumentation and batch receipt retention'])
    sampler = None
    children = None
    try:
        report['isolation'] = _dual_isolation(directory)
        if protocol:
            oracle = None; impacts = protocol['config']['impacts']
            report.update(protocol_sha256=protocol['protocol_sha256'], repetition=1, planned_repetitions=3,
                representative_matrix_complete=False, corpus='Django', ceilings=protocol['config']['ceilings'])
            report['qualification_blockers'] = ['representative_matrix_incomplete', 'resource_budgets_unfrozen',
                'sampled_rss_has_unbounded_startup_and_short_child_gaps', 'supervisor_materialization_io_not_phase_metered']
        else:
            _persistent_recheck(root, bound)
            oracle = checks._adapter_json(root, 'evaluations/code-understanding/supplement-oracle.json', bound)
            impacts = {row['id']: row['expected_source_bound_impacts'] for row in oracle['mutations'] if row['id'] in impact_sha}
        if (set(impacts) != set(impact_sha) or any(digest(impacts[key]) != expected for key, expected in impact_sha.items())):
            raise ValueError('Frozen source-impact projection changed')
        report['controller_envelope'] = dict(address_space=list(resource.getrlimit(resource.RLIMIT_AS)),
            cpu_seconds=list(resource.getrlimit(resource.RLIMIT_CPU)), file_bytes=list(resource.getrlimit(resource.RLIMIT_FSIZE)),
            core_bytes=list(resource.getrlimit(resource.RLIMIT_CORE)), affinity=sorted(os.sched_getaffinity(0)))
        sampler = _PersistentSampler(supervisor, max_windows=32, invoker=invoker).attach_log(directory).start() if protocol else \
                  _PersistentSampler(supervisor).attach_log(directory).start()
        if protocol:
            children = _persistent_children(sampler, directory); children.__enter__()
            _persistent_recheck(root, bound); check()
        setup = time.monotonic_ns()
        with SourceRoot(source) as owner:
            if owner.identity != source_owner: raise ValueError('Supervisor source owner changed')
        index = StructuralIndex(source, directory / 'index')
        if index.owner != source_owner: raise ValueError('Writer differs from supervisor source owner')
        report['source_owner_identity'] = source_owner
        report['preparation_seconds'] = (time.monotonic_ns() - setup) / 1e9
        report['index_limits'], report['parser_budget'] = asdict(index.limits), asdict(index.budget)
        def attempt(index, items, label):
            if protocol:
                check(); _persistent_recheck(root, bound)
                current = _persistent_protocol(protocol_directory, original_source)
                if current != protocol: raise ValueError('Protocol or original owner changed')
                observed = _persistent_attempt(index, items, label, mode, concurrency, directory, sampler,
                                               protocol=protocol, check=check)
            else:
                with sampler.exclude_source_fence(label): _persistent_recheck(root, bound)
                observed = _persistent_attempt(index, items, label, mode, concurrency, directory, sampler)
            report['phases'].append(observed); checks._adapter_dump(directory, 'result.json', report)
            if observed['status'] != 'ready': raise ValueError('Persistent fixture phase did not publish: ' + label)
            if protocol: report['representative_corpus_profiled'] = True
            return observed['streamed_facts']
        def grade(index, proof, label, ids, phase):
            def selected(reader):
                report['source_impacts'][label] = {key: _persistent_impacts(reader,
                    [row[phase] for row in impacts[key]], directory, label + '-' + key) for key in ids}
            if protocol:
                with _persistent_snapshot_view(directory, proof, check) as reader: selected(reader)
            else: selected(index)
            for key in ids: report['source_impacts'][label][key]['oracle_projection_sha256'] = impact_sha[key]
        def inventory(changed=None):
            return _persistent_records(protocol_directory, protocol, changed) if protocol else records
        fresh = attempt(index, inventory(), 'fresh-output')
        grade(index, fresh, 'fresh-output', edit_ids, 'before')
        sampler.set_phase('frozen-fixture-queries')
        report['queries'] = {}
        if protocol:
            with _persistent_snapshot_view(directory, fresh, check) as reader:
                _persistent_queries(index, directory, specs=specs, progress=report['queries'], facts_reader=reader)
        else: _persistent_queries(index, directory, fanout=oracle['query'], progress=report['queries'])
        repeat = attempt(index, inventory(), 'unchanged-repeat')
        report['equivalence']['unchanged_generation'] = fresh['identities'] == repeat['identities']
        report['equivalence']['unchanged_facts'] = fresh['semantic_facts_sha256'] == repeat['semantic_facts_sha256']
        for edit_id in edit_ids:
            if protocol:
                edit = edits[edit_id]; changed_records = inventory(edit)
                body, _ = checks._adapter_bytes(protocol_directory, edit['postimage'], expected=edit['sha256'], cap=edit['bytes'])
                changed = {edit['path']: body}
            else: changed, changed_records = _dual_mutation(blobs, records, edits[edit_id])
            preparation = time.monotonic_ns(); sampler.set_phase(edit_id + '-preparation')
            if edit_id == edit_ids[1]:
                if protocol: _persistent_materialize(source, original_source, protocol_directory, protocol, check)
                else: checks._adapter_materialize(source, blobs)
                index = StructuralIndex(source, directory / 'u-py-export-prime')
                prime = attempt(index, inventory(), edit_id + '-reset-prime')
                grade(index, prime, edit_id + '-reset-prime', (edit_id,), 'before')
            report.setdefault('mutation_preparation_seconds', {})[edit_id] = (time.monotonic_ns() - preparation) / 1e9
            checks._adapter_materialize(source, changed)
            incremental = attempt(index, changed_records, edit_id + '-changed')
            grade(index, incremental, edit_id + '-changed', (edit_id,), 'after')
            clean = StructuralIndex(source, directory / (edit_id.lower() + '-clean'))
            rebuilt = attempt(clean, inventory(edits[edit_id]) if protocol else changed_records, edit_id + '-clean-rebuild')
            grade(clean, rebuilt, edit_id + '-clean-rebuild', (edit_id,), 'after')
            report['equivalence'][edit_id] = dict(identities=incremental['identities'] == rebuilt['identities'],
                semantic_facts=incremental['semantic_facts_sha256'] == rebuilt['semantic_facts_sha256'],
                counts=incremental['counts'] == rebuilt['counts'],
                generation_changed=incremental['identities']['generation'] != fresh['identities']['generation'],
                source_identity_changed=incremental['identities']['source_identity'] != fresh['identities']['source_identity'])
        with SourceRoot(source) as owner:
            report['source_owner_identity_after'] = owner.identity
            if owner.identity != source_owner: raise ValueError('Measured source owner changed')
        report['source_data_accounting_complete'] = all(phase['source_accounting']['accounting_complete'] for phase in report['phases'])
        if not report['source_data_accounting_complete']: raise ValueError('Source-data accounting incomplete')
        if 'source_data_accounting_unsettled' in report['qualification_blockers']:
            report['qualification_blockers'].remove('source_data_accounting_unsettled')
        report['status'] = 'complete' if all(value if type(value) is bool else all(value.values())
            for value in report['equivalence'].values()) else 'equivalence_failed'
        sampler.set_phase('source-cleanup')
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, MemoryError, RecursionError, sqlite3.Error) as error:
        report.update(status='failed', failure=checks._adapter_error(error))
    finally:
        if protocol and sampler is not None:
            try: report['binding_after'] = _persistent_recheck(root, bound); check()
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                report.update(status='invalid_identity', identity_failure=checks._adapter_error(error))
            finally:
                if children is not None: children.__exit__(None, None, None)
        if sampler is not None:
            try:
                report['owned_rss'] = sampler.finish()
                if report['owned_rss'].get('error') is not None or report['owned_rss'].get('remaining_registered_owned_child_owners'):
                    report['status'] = 'measurement_failed'
                checks._adapter_dump(directory, 'owned-rss.json', report['owned_rss'])
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                report.update(status='measurement_failed', memory_failure=checks._adapter_error(error))
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report['controller_lifetime'] = dict(process_peak_rss_bytes=usage.ru_maxrss * 1024,
            user_seconds=usage.ru_utime, system_seconds=usage.ru_stime,
            scope='controller lifetime only; never summed with child lifetime peaks')
        try:
            if protocol is None: report['binding_after'] = _persistent_recheck(root, bound)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            report.update(status='invalid_identity', identity_failure=checks._adapter_error(error))
        if protocol: report['artifact_budget_observations'] = dict(check.observed)
        checks._adapter_dump(directory, 'result.json', report)
    return report


def _persistent_validate(report, bound, mode, concurrency, protocol=None):
    edits = tuple(row['id'] for row in protocol['config']['updates']) if protocol else ('U-PY-BODY', 'U-PY-EXPORT')
    labels = (['fresh-output', 'unchanged-repeat', edits[0] + '-changed', edits[0] + '-clean-rebuild',
               edits[1] + '-reset-prime', edits[1] + '-changed', edits[1] + '-clean-rebuild'])
    impact_sha = protocol['config']['impacts_sha256'] if protocol else PERSISTENT_IMPACT_SHA
    if (type(report) is not dict or report.get('kind') != ('persistent_corpus' if protocol else 'persistent_fixture') or
            type(report.get('schema_version')) is not int or report['schema_version'] != 1 or
            report.get('binding_before') != bound or report.get('mode') != mode or
            type(report.get('concurrency')) is not int or report['concurrency'] != concurrency or
            report.get('qualification_complete') is not False or report.get('resource_budgets_frozen') is not False or
            report.get('engine_selected') is not False or report.get('status') not in
                ('complete', 'failed', 'equivalence_failed', 'measurement_failed', 'invalid_identity') or
            report.get('all_owned_source_reads_measured') is not False or type(report.get('phases')) is not list or
            any(type(row) is not dict for row in report['phases']) or
            [row.get('label') for row in report['phases']] != labels[:len(report['phases'])] or len(report['phases']) > 7):
        raise ValueError('Persistent fixture report identity/phase mismatch')
    if report['status'] == 'complete':
        expected = dict(unchanged_generation=True, unchanged_facts=True,
            **{edit: dict(identities=True, semantic_facts=True, counts=True, generation_changed=True,
                source_identity_changed=True) for edit in edits})
        equality = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        rebound = {key: bound[key] for key in ('measured_commit', 'implementation', 'input_binding', 'root_identity',
            'backend', 'queue_identity', 'runtime', 'persistent_writer_identity', 'persistent_limits')}
        if protocol: rebound['representative_protocol'] = bound['representative_protocol']
        rss = report.get('owned_rss', {})
        if (len(report['phases']) != 7 or any(row.get('status') != 'ready' for row in report['phases']) or
                report.get('source_data_accounting_complete') is not True or
                report.get('source_owner_identity') != report.get('source_owner_identity_after') or
                type(report.get('source_owner_identity')) is not str or len(report['source_owner_identity']) != 64 or
                report.get('binding_after') != rebound or type(rss) is not dict or
                rss.get('error') is not None or rss.get('sampler_stopped') is not True or
                rss.get('remaining_registered_owned_child_owners') != [] or
                type(rss.get('complete_sample_count')) is not int or rss['complete_sample_count'] <= 0 or
                type(rss.get('peak_sampled_owned_rss_bytes')) is not int or rss['peak_sampled_owned_rss_bytes'] <= 0 or
                rss.get('all_measured_phase_children_registered') is not True or
                rss.get('all_created_children_registered') is not bool(protocol) or
                type(rss.get('excluded_source_fence_intervals')) is not int or rss['excluded_source_fence_intervals'] != (0 if protocol else 7) or
                equality(report.get('equivalence')) != equality(expected)):
            raise ValueError('Missing complete persistent experiment evidence')
        if protocol and (report.get('protocol_sha256') != protocol['protocol_sha256'] or report.get('input_file_count') != 2978 or
                report.get('input_inventory_sha256') != protocol['header']['manifest']['records_sha256'] or
                report.get('ceilings') != protocol['config']['ceilings'] or report.get('repetition') != 1 or
                report.get('planned_repetitions') != 3 or report.get('representative_matrix_complete') is not False or
                rss.get('max_windows') != 32 or any(row.get('streamed_facts', {}).get('evidence_mode') != 'pinned_sqlite_backup_v1' or
                row['streamed_facts'].get('snapshot', {}).get('sealed') is not True for row in report['phases'])):
            raise ValueError('Missing preregistered retained representative evidence')
        for phase in report['phases']:
            accounting = phase.get('source_accounting')
            if (type(accounting) is not dict or accounting.get('accounting_complete') is not True or
                    accounting.get('totals_kind') != 'exact' or type(accounting.get('worker_requests_unknown')) is not int or
                    accounting['worker_requests_unknown'] != 0 or accounting.get('collector_original_source_stream_bytes') != 0):
                raise ValueError('Missing complete measured source-data accounting')
        impact_stages = {label: edits if label == 'fresh-output' else
            (edits[0],) if label.startswith(edits[0]) else (edits[1],)
            for label in labels if label != 'unchanged-repeat'}
        impacts = report.get('source_impacts')
        if type(impacts) is not dict or set(impacts) != set(impact_stages):
            raise ValueError('Missing frozen physical impact controls')
        for label, ids in impact_stages.items():
            if type(impacts[label]) is not dict or set(impacts[label]) != set(ids):
                raise ValueError('Missing frozen update impact control')
            for key in ids:
                row = impacts[label][key]
                if (type(row) is not dict or row.get('passed') is not True or type(row.get('checked_impacts')) is not int or
                        row['checked_impacts'] != (len(protocol['config']['impacts'][key]) if protocol else 1) or
                        row.get('oracle_projection_sha256') != impact_sha[key] or
                        type(row.get('artifact')) is not dict):
                    raise ValueError('Vacuous or unfrozen physical impact control')
        _persistent_validate_queries(report.get('queries'), report['phases'][0]['streamed_facts']['identities'],
                                    specs=protocol['config']['queries'] if protocol else PERSISTENT_QUERY_SPECS)
    return report


def _persistent_validate_queries(queries, metadata, *, specs=PERSISTENT_QUERY_SPECS):
    if (type(queries) is not dict or queries.get('passed') is not True or queries.get('identities') != metadata or
            queries.get('freeze_sha256') != PERSISTENT_FREEZE_SHA or queries.get('generation') != metadata['generation'] or
            type(queries.get('cold_calls')) is not int or queries['cold_calls'] != 1 or
            type(queries.get('warm_calls')) is not int or queries['warm_calls'] != 10 or
            queries.get('limits') != PERSISTENT_QUERY_LIMITS or type(queries.get('workloads')) is not list or
            any(type(row) is not dict for row in queries['workloads']) or
            [row.get('id') for row in queries['workloads']] != [spec['id'] for spec in specs]):
        raise ValueError('Missing nonvacuous frozen query workloads')
    for row, spec in zip(queries['workloads'], specs):
        limits = dict(PERSISTENT_QUERY_LIMITS, **spec.get('overrides', {}))
        if (row.get('passed') is not True or row.get('operation') != spec['operation'] or row.get('limits') != limits or
                type(row.get('seed')) is not str or not row['seed'] or type(row.get('samples')) is not list or len(row['samples']) != 11):
            raise ValueError('Missing cold/warm frozen query samples')
        for number, sample in enumerate(row['samples']):
            if (type(sample) is not dict or sample.get('temperature') != ('cold' if number == 0 else 'warm') or
                    sample.get('generation') != metadata['generation'] or sample.get('source_identity') != metadata['source_identity'] or
                    type(sample.get('elapsed_seconds')) not in (int, float) or not math.isfinite(sample['elapsed_seconds']) or
                    sample['elapsed_seconds'] < 0 or type(sample.get('wire_bytes')) is not int or
                    not 0 < sample['wire_bytes'] <= limits['max_response_bytes'] or type(sample.get('artifact')) is not dict or
                    any(type(sample.get(key)) is not int or not 0 <= sample[key] <= ceiling for key, ceiling in
                        (('returned_symbol_handles', limits['max_entities']), ('returned_edges', limits['max_edges']),
                         ('examined_relationships', limits['max_examined_relationships']), ('excerpt_bytes', 0)))):
                raise ValueError('Malformed bounded individual query measurement')
        warm = sorted(sample['elapsed_seconds'] for sample in row['samples'][1:])
        if row.get('warm_p50_seconds') != statistics.median(warm) or row.get('warm_p95_seconds') != warm[math.ceil(.95 * len(warm)) - 1]:
            raise ValueError('Warm query statistics differ from individual samples')
    pages = queries.get('cursor_control')
    if not any(spec['id'] == 'F-FANOUT-STOP' for spec in specs):
        if pages is not None: raise ValueError('Unexpected unfrozen cursor workload')
        return
    if (type(pages) is not dict or pages.get('passed') is not True or type(pages.get('pages')) is not list or
            not 2 <= len(pages['pages']) <= 64 or pages.get('returned_occurrences') != 113 or
            pages.get('expected_occurrences') != 113 or pages.get('max_pages') != 64):
        raise ValueError('Missing complete frozen fanout continuation control')


def persistent_worker(argv):
    import faulthandler, resource
    from evaluations import engine_checks as checks
    from evaluations.supplement_preparation import decode
    from repo_graph.analysis_queue import _guard_controller
    parser = argparse.ArgumentParser(); parser.add_argument('fd', type=int); parser.add_argument('source_fd', type=int)
    parser.add_argument('creator_pid', type=int)
    parser.add_argument('--protocol-fd', type=int); parser.add_argument('--original-fd', type=int); parser.add_argument('--pair-fd', type=int)
    args = parser.parse_args(argv); _guard_controller(args.creator_pid); faulthandler.enable()
    root, directory = Path(__file__).resolve().parents[1], Path('/proc/self/fd') / str(args.fd)
    protocol_directory = Path('/proc/self/fd') / str(args.protocol_fd) if args.protocol_fd is not None else None
    original = Path('/proc/self/fd') / str(args.original_fd) if args.original_fd is not None else None
    pair = Path('/proc/self/fd') / str(args.pair_fd) if args.pair_fd is not None else None
    if any(value is not None for value in (protocol_directory, original, pair)) and any(value is None for value in (protocol_directory, original, pair)):
        raise ValueError('Protocol, original and pair descriptors required together')
    loaded = _persistent_protocol(protocol_directory, original) if protocol_directory is not None else None
    with SourceRoot(directory) as owner:
        raw, _, info = owner.read('control.json', 128 * 1024 + 1, hash_full=False)
        if len(raw) != info.st_size or len(raw) > 128 * 1024: raise ValueError('Bounded persistent control required')
        control = decode(raw)
        fields = {'schema_version', 'mode', 'concurrency', 'binding', 'directory_owner', 'supervisor', 'source_owner'}
        if loaded: fields |= {'protocol_sha256', 'pair_owner', 'invoker'}
        if (type(control) is not dict or set(control) != fields or control.get('schema_version') != 1 or
                type(control.get('schema_version')) is not int or type(control.get('concurrency')) is not int or
                (control['mode'], control['concurrency']) not in PERSISTENT_MODES or
                control['directory_owner'] != owner.identity or type(control.get('supervisor')) is not dict or
                control['supervisor'].get('pid') != args.creator_pid):
            raise ValueError('Typed persistent controller ownership required')
    if loaded:
        with SourceRoot(pair) as pair_owner:
            if pair_owner.identity != control['pair_owner']: raise ValueError('Supervisor pair owner changed')
        if loaded['protocol_sha256'] != control['protocol_sha256']: raise ValueError('Protocol control identity mismatch')
    cpu, file_bytes = (600, 2147483648) if loaded else (60, DUAL_LOG_BYTES)
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu)); resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes)); signal.signal(signal.SIGXCPU, signal.SIG_DFL)
    source = Path('/proc/self/fd') / str(args.source_fd)
    with SourceRoot(source) as shared:
        if shared.identity != control['source_owner']: raise ValueError('Supervisor shared source owner changed')
    bound = _persistent_capture(root, loaded) if loaded else _persistent_capture(root)
    if bound != control['binding']: raise ValueError('Parent/controller committed binding mismatch')
    kwargs = dict(protocol=loaded, protocol_directory=protocol_directory, original_source=original,
                  pair=pair, invoker=control['invoker']) if loaded else {}
    report = _persistent_run(root, directory, source, control['source_owner'], bound,
                             control['mode'], control['concurrency'], control['supervisor'], **kwargs)
    return 0 if report['status'] == 'complete' else 1


def persistent_supervisor(argv):
    from repo_graph.analysis_queue import _guard_controller
    from evaluations.supplement_preparation import decode
    parser = argparse.ArgumentParser(); parser.add_argument('evidence_directory', type=Path)
    parser.add_argument('--cpu-affinity', type=int, nargs='+'); parser.add_argument('--creator-pid', type=int, required=True)
    parser.add_argument('--protocol-fd', type=int); parser.add_argument('--original-fd', type=int); parser.add_argument('--invoker')
    args = parser.parse_args(argv); _guard_controller(args.creator_pid)
    if (args.protocol_fd is None) != (args.original_fd is None): raise ValueError('Protocol and original descriptors required together')
    options = {}
    if args.protocol_fd is not None:
        options = dict(protocol=Path('/proc/self/fd') / str(args.protocol_fd),
                       original_source=Path('/proc/self/fd') / str(args.original_fd), invoker=decode(args.invoker.encode()) if args.invoker else None)
    elif args.invoker is not None: raise ValueError('Invoker observation requires representative protocol')
    result = profile_persistent_fixture(Path(__file__).resolve().parents[1], args.evidence_directory, affinity=args.cpu_affinity, **options)
    print(json.dumps(compact_persistent_result(result), sort_keys=True, separators=(',', ':'), allow_nan=False))
    return 0 if result['status'] == 'complete' else 1


def compact_persistent_result(wrapper):
    """Portable observed measurements only; full raw receipts/PIDs stay private."""
    def select(value, fields):
        return {key: value[key] for key in fields.split() if key in value} if type(value) is dict else {}
    def error(value):
        known = {'OSError', 'ValueError', 'RuntimeError', 'TypeError', 'KeyError', 'MemoryError', 'RecursionError',
                 'AttributeError', 'TimeoutExpired', 'BackendUnavailable', 'ChildProcessError', 'InterruptedError',
                 'PermissionError', 'FileNotFoundError'}
        stages = {'workload_validation', 'metadata', 'selectors', 'queries', 'query_report_retention',
                  'collection', 'source_accounting', 'batch_receipt_retention', 'collection_metadata',
                  'snapshot_open', 'snapshot_backup', 'snapshot_metadata', 'snapshot_digest', 'snapshot_seal'}
        kind = value.get('error_kind', value.get('kind')) if type(value) is dict else None
        result = dict(error_kind=kind if type(kind) is str and kind in known else 'OtherError')
        if type(value) is dict:
            if type(value.get('stage')) is str and value['stage'] in stages: result['stage'] = value['stage']
            for key, low, high in (('errno', 0, 4095), ('code', -255, 255)):
                if type(value.get(key)) is int and low <= value[key] <= high: result[key] = value[key]
        return result
    def errors(source, destination, fields):
        for key in fields.split():
            if type(source) is dict and source.get(key) is not None: destination[key] = error(source[key])
    report = wrapper.get('full_private_report', wrapper)
    representative = report.get('kind') == 'persistent_corpus_profile'
    result = dict(schema_version=1, kind=report.get('kind', 'persistent_fixture_profile'), status=report['status'],
        engine_selected=False, qualification_complete=False,
        representative_corpus_profiled=report.get('representative_corpus_profiled', False),
        resource_budgets_frozen=False, all_owned_source_reads_measured=False,
        source_data_accounting_complete=report.get('source_data_accounting_complete', False),
        qualification_blockers=report.get('qualification_blockers', []), cases=[])
    errors(report, result, 'failure identity_failure archive_failure')
    if representative:
        result.update(select(report, 'corpus protocol_sha256 repetition planned_repetitions representative_matrix_complete ceilings'))
        result['artifact_budget_observations'] = select(report.get('artifact_budget_observations'),
            'checks job_bytes pair_bytes job_files pair_files scope')
        result['source_materialization'] = [select(row, 'mode operations successful_operations failed_operations open_operations '
            'stream_bytes hashed_bytes returned_prefix_bytes hash_passes inclusive_read_ns by_pass scope')
            for row in report.get('source_materialization') or []]
    if report.get('binding_before'):
        bound = report['binding_before']
        result['source_binding'] = {key: bound[key] for key in ('measured_commit', 'implementation', 'input_binding', 'backend')}
    for case in report.get('cases', []):
        row = {key: case.get(key) for key in ('id', 'mode', 'concurrency', 'status', 'returncode')}
        errors(case, row, 'failure identity_failure')
        raw = case.get('report') or {}; row['phases'] = []
        errors(raw, row, 'failure identity_failure memory_failure')
        for phase in raw.get('phases') or []:
            item = {key: phase[key] for key in ('label', 'status', 'wall_seconds', 'proof_retention_seconds',
                'observed_attempt_seconds', 'stages', 'collection_totals', 'native_file_timing_sums',
                'queue_controller_timing_sums', 'timing_scope') if key in phase}
            item['source_reads'] = select(phase.get('source_reads'), 'operations successful_operations failed_operations '
                'open_operations stream_bytes hashed_bytes returned_prefix_bytes hash_passes inclusive_read_ns by_pass scope '
                'collector_original_source_reads collector_mailbox_read_bytes all_owned_source_reads_measured qualification_blocker')
            accounting = phase.get('source_accounting')
            item['source_accounting'] = select(accounting, 'schema_version accounting_complete passes '
                'worker_requests_with_accounting worker_requests_unknown collector_original_source_stream_bytes '
                'scope total_source_stream_bytes total_hashed_bytes total_hash_passes totals_kind') if accounting is not None else None
            if accounting is not None: errors(accounting, item['source_accounting'], 'failure accounting_failure')
            errors(phase, item, 'error measurement_error')
            if 'streamed_facts' in phase:
                item['facts'] = {key: phase['streamed_facts'][key] for key in ('semantic_facts_sha256', 'counts')}
                if representative:
                    item['facts'].update(select(phase['streamed_facts'], 'evidence_mode identities'))
                    item['facts']['snapshot'] = select(phase['streamed_facts'].get('snapshot'), 'source_bytes destination_bytes '
                        'backup_seconds digest_seconds backup_progress_callbacks sealed metadata_verified')
            if 'receipt' in phase:
                receipt = phase.get('receipt') or {}
                item['coverage'] = select(receipt.get('coverage'), 'files_total files_supported files_unsupported '
                    'status_counts file_status by_language language_overflow sites_by_role_certainty parser_error_count '
                    'parser_error_samples parser_error_samples_truncated inventory_scope discovery_skipped_files discovery_skip_knowledge')
                item['resources'] = select(receipt.get('resources'), 'batches changed_files_collected '
                    'unchanged_source_collections_reused source_bytes workers_started peak_batch_files peak_batch_source_bytes '
                    'peak_batch_handoff_bytes owned_workers_reaped bindings_files_resolved bindings_files_reused '
                    'unknown_closure_files_rebuilt dependency_lookups_checked inventory_entries_consumed invalidation_reason elapsed_seconds')
            row['phases'].append(item)
        row['equivalence'] = raw.get('equivalence')
        row['source_impacts'] = {label: {key: select(impact, 'passed checked_impacts expected_sha256 identities '
            'selected_declarations selected_sites oracle_projection_sha256') for key, impact in (values or {}).items()}
            for label, values in (raw.get('source_impacts') or {}).items()}
        queries = raw.get('queries')
        if queries is not None:
            def sample(value):
                return select(value, 'temperature elapsed_seconds passed wire_bytes generation source_identity '
                    'examined_relationships examined_symbols returned_entities returned_symbol_handles returned_edges '
                    'excerpt_bytes storage_progress_callbacks storage_setup_seconds snapshot_copy_seconds total_count truncated stop_reason')
            row['queries'] = select(queries, 'freeze_sha256 generation identities scope cold_calls warm_calls limits passed')
            errors(queries, row['queries'], 'failure retention_failure')
            row['queries']['workloads'] = [dict(select(workload, 'id operation seed limits warm_p50_seconds '
                'warm_p95_seconds rows_sha256 passed'), samples=[sample(value) for value in workload.get('samples') or []])
                for workload in queries.get('workloads') or []]
            control = queries.get('cursor_control')
            row['queries']['cursor_control'] = (dict(select(control, 'passed returned_occurrences expected_occurrences '
                'max_pages physical_occurrences_sha256'), pages=[sample(value) for value in control.get('pages') or []])
                if control is not None else None)
        rss = raw.get('owned_rss') or {}
        if representative: row['artifact_budget_observations'] = select(raw.get('artifact_budget_observations'),
            'checks job_bytes pair_bytes job_files pair_files scope')
        row['owned_rss'] = {key: rss[key] for key in ('peak_sampled_owned_rss_bytes', 'sample_count', 'complete_sample_count',
            'sample_gap_count', 'created_child_count', 'largest_start_interval_ns', 'max_read_skew_ns', 'queue_high_water',
            'requested_interval_seconds', 'per_window_limits', 'max_windows', 'unsampled_peak_bound', 'all_created_children_registered',
            'all_measured_phase_children_registered', 'excluded_source_fence_intervals', 'excluded_source_fence_ns',
            'child_registration_scope') if key in rss}
        errors(rss, row['owned_rss'], 'error')
        row['cleanup'] = ({key: case['cleanup'][key] for key in ('leader_reaped', 'group_absent')}
                          if case.get('cleanup') else None)
        result['cases'].append(row)
    result['phase_semantic_agreement'] = report.get('phase_semantic_agreement')
    result['same_source_owner_across_modes'] = report.get('same_source_owner_across_modes')
    result['source_cleanup'] = select(report.get('source_cleanup'), 'completed scope') if report.get('source_cleanup') is not None else None
    return result


def profile_persistent_fixture(root, evidence_directory, runs=1, *, affinity=None,
                               protocol=None, original_source=None, invoker=None):
    """One owned serial1/queued2 pair; finite proof, never a representative matrix."""
    from evaluations import engine_checks as checks
    from evaluations.supplement_preparation import decode
    if type(runs) is not int or runs != 1: raise ValueError('Exactly one finite paired pilot repetition required')
    if (protocol is None) != (original_source is None): raise ValueError('Protocol and original source required together')
    if protocol is None and invoker is not None: raise ValueError('Invoker observation requires representative protocol')
    root = checks._adapter_root(root)
    loaded = _persistent_protocol(protocol, original_source) if protocol is not None else None
    if loaded:
        destination = Path(evidence_directory).resolve(strict=True)
        for protected in (Path(protocol).resolve(strict=True), Path(original_source).resolve(strict=True)):
            if destination == protected or protected in destination.parents:
                raise ValueError('Evidence must be outside immutable protocol and original source')
        if invoker is not None:
            if type(invoker) is not dict or invoker.get('pid') != os.getppid(): raise ValueError('Direct invoking CLI owner required')
            with closing(_DualProcOwner(invoker)) as held: held.recheck(require_live=True)
    envelope = _dual_supervisor_limits(affinity, representative=True) if loaded else _dual_supervisor_limits(affinity)
    deadline, bound = time.monotonic() + (2400 if loaded else 90), None
    report = dict(schema_version=1, kind='persistent_corpus_profile' if loaded else 'persistent_fixture_profile',
        status='running', binding_before=None, cases=[],
        supervisor_envelope=envelope, engine_selected=False, qualification_complete=False, representative_corpus_profiled=False,
        resource_budgets_frozen=False, all_owned_source_reads_measured=False,
        qualification_blockers=['source_data_accounting_unsettled', 'representative_costs_unmeasured',
                                'representative_query_costs_unmeasured', 'resource_budgets_unfrozen',
                                'source_fence_children_excluded_from_rss'])
    if loaded:
        report.update(corpus='Django', protocol_sha256=loaded['protocol_sha256'], repetition=1, planned_repetitions=3,
            representative_matrix_complete=False, ceilings=loaded['config']['ceilings'],
            qualification_blockers=['representative_matrix_incomplete', 'resource_budgets_unfrozen',
                'sampled_rss_has_unbounded_startup_and_short_child_gaps'])
    from contextlib import ExitStack
    with ExitStack() as holds, checks._adapter_run(root, evidence_directory, 'persistent-Django' if loaded else 'persistent-fixture') as (run, name):
        protocol_hold = holds.enter_context(SourceRoot(protocol)) if loaded else None
        original_hold = holds.enter_context(SourceRoot(original_source)) if loaded else None
        pair_hold = holds.enter_context(SourceRoot(run)) if loaded else None
        check = _persistent_budget(run, run, deadline, pair_only=True) if loaded else None
        try:
            bound = _persistent_capture(root, loaded) if loaded else _persistent_capture(root)
            report['binding_before'] = bound
            blobs = _dual_inputs(root, bound)[0] if loaded is None else None
            report['source_cleanup'] = dict(completed=False, scope='supervisor descriptor-owned shared source cleanup')
            with checks._adapter_source(run) as source, SourceRoot(source) as shared:
                report['shared_source_owner_identity'] = shared.identity
                for mode, concurrency in PERSISTENT_MODES:
                    label = mode + '-' + str(concurrency); row = dict(id=label, mode=mode, concurrency=concurrency, status='running')
                    report['cases'].append(row); process = None
                    if loaded:
                        if _persistent_protocol(protocol, original_source) != loaded: raise ValueError('Protocol changed before mode admission')
                        with _PersistentReadMeter(loaded['original_owner'], label + '-materialization', run, max_windows=32) as meter:
                            _persistent_materialize(source, original_source, protocol, loaded, check)
                        report.setdefault('source_materialization', []).append(dict(mode=mode, **meter.summary()))
                    else: checks._adapter_materialize(source, blobs)
                    if shared.identity != report['shared_source_owner_identity']: raise ValueError('Shared source owner changed')
                    with checks._adapter_child(run, label) as job, SourceRoot(job) as owner:
                        _persistent_recheck(root, bound)
                        control = dict(schema_version=1, mode=mode, concurrency=concurrency,
                            binding=bound, directory_owner=owner.identity, supervisor=_dual_self_identity(), source_owner=shared.identity)
                        if loaded: control.update(protocol_sha256=loaded['protocol_sha256'], pair_owner=pair_hold.identity, invoker=invoker)
                        checks._adapter_dump(job, 'control.json', control)
                        try:
                            if time.monotonic() >= deadline: raise TimeoutError('Persistent pilot wall budget exhausted')
                            with owner.open('stdout.log', create=True) as stdout, owner.open('stderr.log', create=True) as stderr:
                                bridge = Path('/proc/' + str(os.getpid()) + '/fd/' + str(owner.fd))
                                command = [sys.executable, '-I', '-B', str(root / 'evaluations/performance.py'),
                                    '--persistent-worker', str(owner.fd), str(shared.fd), str(os.getpid())]
                                descriptors = (owner.fd, shared.fd)
                                if loaded:
                                    command += ['--protocol-fd', str(protocol_hold.fd), '--original-fd', str(original_hold.fd),
                                                '--pair-fd', str(pair_hold.fd)]
                                    descriptors += (protocol_hold.fd, original_hold.fd, pair_hold.fd)
                                process = subprocess.Popen(command, cwd=bridge,
                                    env=checks._environment(bridge), pass_fds=descriptors, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, start_new_session=True)
                                row['returncode'] = process.wait(timeout=max(.001, deadline - time.monotonic()))
                            raw, sha, info = owner.read('result.json', DUAL_LOG_BYTES + 1, hash_full=False)
                            if len(raw) != info.st_size or len(raw) > DUAL_LOG_BYTES: raise ValueError('Bounded complete pilot report required')
                            row['report_artifact'] = dict(path=label + '/result.json', sha256=sha, bytes=info.st_size)
                            produced = decode(raw)
                            if type(produced) is dict: row['report'] = produced
                            result = _persistent_validate(produced, bound, mode, concurrency, loaded) if loaded else \
                                     _persistent_validate(produced, bound, mode, concurrency)
                            if (result.get('source_owner_identity') != shared.identity or
                                    result.get('source_owner_identity_after') != shared.identity):
                                raise ValueError('Both modes require the same admitted source owner')
                            row['status'] = result['status']
                            if loaded and result.get('representative_corpus_profiled') is True: report['representative_corpus_profiled'] = True
                            if row['returncode'] != 0: raise ChildProcessError('Owned fixture worker exited nonzero')
                        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
                            row.update(status='failed', failure=checks._adapter_error(error))
                        finally:
                            row['cleanup'] = checks._stop_and_reap(process) if process is not None else None
                            row['logs'] = []
                            for log in ('stdout.log', 'stderr.log'):
                                try:
                                    raw, sha, info = owner.read(log, DUAL_LOG_BYTES + 1, hash_full=False)
                                    row['logs'].append(dict(path=label + '/' + log, sha256=sha, bytes=info.st_size,
                                        complete=len(raw) == info.st_size and len(raw) <= DUAL_LOG_BYTES))
                                except OSError as error: row['logs'].append(dict(path=label + '/' + log, error_kind=type(error).__name__))
                            if not row['cleanup'] or not row['cleanup']['leader_reaped'] or not row['cleanup']['group_absent']:
                                row['status'] = 'cleanup_failed'
                            _persistent_recheck(root, bound)
                            if check: check()
                            checks._adapter_dump(run, 'report.json', report)
                    if row['status'] != 'complete': raise ValueError('Persistent pilot case failed; no further admission')
                report['same_source_owner_across_modes'] = all(case['report']['source_owner_identity'] == shared.identity for case in report['cases'])
            report['source_cleanup']['completed'] = True
            reference = report['cases'][0]['report']['phases']
            report['phase_semantic_agreement'] = {phase['label']: all(
                next(other for other in case['report']['phases'] if other['label'] == phase['label'])['streamed_facts']['semantic_facts_sha256'] ==
                phase['streamed_facts']['semantic_facts_sha256'] for case in report['cases']) for phase in reference}
            report['source_data_accounting_complete'] = all(case['report'].get('source_data_accounting_complete') is True for case in report['cases'])
            if report['source_data_accounting_complete'] and 'source_data_accounting_unsettled' in report['qualification_blockers']:
                report['qualification_blockers'].remove('source_data_accounting_unsettled')
            report['status'] = 'complete' if len(reference) == 7 and report['same_source_owner_across_modes'] and all(report['phase_semantic_agreement'].values()) else 'equivalence_failed'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
            report.update(status='failed', failure=checks._adapter_error(error))
        finally:
            if bound is not None:
                try: report['binding_after'] = _persistent_recheck(root, bound)
                except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                    report.update(status='invalid_identity', identity_failure=checks._adapter_error(error))
            if time.monotonic() >= deadline: report['status'] = 'deadline_exceeded'
            if loaded: report['artifact_budget_observations'] = dict(check.observed)
            checks._adapter_dump(run, 'report.json', report)
        if loaded:
            try:
                check()
                return checks._adapter_archive(run, name, report,
                    limits=dict(max_file_bytes=2147483648, max_total_bytes=8589934592, max_files=20000), check=check.clock)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                report.update(status='archive_failed', archive_failure=checks._adapter_error(error))
                checks._adapter_dump(run, 'report.json', report)
                return dict(status=report['status'], archive=None, full_private_report=report)
        return checks._adapter_archive(run, name, report)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--structural-worker']:
        raise SystemExit(structural_worker(sys.argv[2:]))
    if sys.argv[1:2] == ['--native-dual-worker']:
        raise SystemExit(native_dual_worker(sys.argv[2:]))
    if sys.argv[1:2] == ['--native-dual-supervisor']:
        raise SystemExit(native_dual_supervisor(sys.argv[2:]))
    if sys.argv[1:2] == ['--persistent-worker']:
        raise SystemExit(persistent_worker(sys.argv[2:]))
    if sys.argv[1:2] == ['--persistent-supervisor']:
        raise SystemExit(persistent_supervisor(sys.argv[2:]))
    main()
