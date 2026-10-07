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
    records = []
    try:
        source_root, output_root = a.source.resolve(strict=True), a.output.resolve()
        if source_root == output_root or source_root in output_root.parents:
            raise ValueError('Profile artifacts must be outside source')
        for run in ('fresh-output', 'unchanged-repeat'):
            stages, reads = {}, {'operations': 0, 'hashed_bytes': 0, 'prefix_bytes': 0}
            start = time.perf_counter()
            original_read = SourceRoot.read
            def observed_read(owner, path, limit, **kwargs):
                value = original_read(owner, path, limit, **kwargs)
                if owner.root == source_root:
                    reads['operations'] += 1
                    reads['hashed_bytes'] += value[2].st_size if kwargs.get('hash_full', True) else len(value[0])
                    reads['prefix_bytes'] += len(value[0])
                return value
            if a.engine == 'current-map':
                from contextlib import ExitStack
                def timed(function, label):
                    def invoke(*args, **kwargs):
                        before = time.perf_counter()
                        try:
                            return function(*args, **kwargs)
                        finally:
                            stages[label] = stages.get(label, 0) + time.perf_counter() - before
                    return invoke
                with ExitStack() as stack:
                    stack.enter_context(patch.object(SourceRoot, 'read', observed_read))
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
                            'scanned': graph['scan']['scanned'], 'reused': graph['scan']['reused']}
                status = 'complete' if not (coverage['failed'] or coverage['truncated']) else 'partial'
            else:
                from evaluations.tree_sitter_baseline import Budget, scan
                before = time.perf_counter()
                paths = builder.repo_files(a.source)
                stages['inventory_seconds'] = time.perf_counter() - before
                code_paths = [path for path in paths if Path(path).suffix in builder.CODE_EXTENSIONS
                              or Path(path).name == 'go.mod']
                limits = Budget(max_files=60000, max_file_bytes=2 * 1024 ** 2,
                    max_total_bytes=1024 ** 3, max_nodes=50000000, max_facts=2000000, timeout_seconds=300)
                with patch.object(SourceRoot, 'read', observed_read):
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
                coverage = {'status': result['status'], 'stop_reason': result['stop_reason'],
                            'errors': len(result['errors']), 'limits': vars(limits),
                            'inventory_statuses': dict(Counter(x['status'] for x in result['inventory']))}
                status = result['status']
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
                         'records': records, 'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}))
        return 1


def profile_structural(source_map, report_path, work_root, runs=3):
    """Measure frozen sources; import maps and callable facts are different workloads."""
    from evaluations.acceptance import PINS
    from evaluations.tree_sitter_baseline import PINS as BACKEND_PINS, RULE_VERSION
    if runs < 3:
        raise ValueError('At least three independent worker runs required')
    config = json.loads(Path(source_map).read_text())
    sources = {c['id']: c for c in config['corpora']}
    destinations = [Path(report_path).resolve(), Path(work_root).resolve()]
    for c in sources.values():
        source = Path(c['source']).resolve(strict=True)
        if any(path == source or source in path.parents for path in destinations):
            raise ValueError('Profile artifacts must be outside source')
    for name in ('django', 'odoo', 'aws', 'kubernetes'):
        c = sources[name]
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=c['source'], text=True).strip()
        if c['revision'] != PINS[name] or actual != PINS[name]:
            raise ValueError('Corpus revision mismatch')
    records = []
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    implementation_paths = ['evaluations/performance.py', 'evaluations/tree_sitter_baseline.py',
                            'repo_graph/builder.py', 'repo_graph/search.py', 'repo_graph/source.py', 'pyproject.toml', 'uv.lock']
    implementation_root = Path(__file__).resolve().parents[1]
    with SourceRoot(implementation_root) as implementation:
        implementation_hashes = {path: implementation.read(path, 1024 * 1024, hash_full=True)[1]
                                 for path in implementation_paths}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=implementation_root, text=True).strip()
    backend_versions = {name: importlib.metadata.version(name) for name in BACKEND_PINS}
    report_parent = Path(report_path).parent
    report_parent.mkdir(parents=True, exist_ok=True)
    with SourceRoot(report_parent) as report_owner, SourceRoot(root) as log_owner:
        if any(owner.root == Path(c['source']).resolve() or Path(c['source']).resolve() in owner.root.parents
               for owner in (report_owner, log_owner) for c in sources.values()):
            raise ValueError('Profile report owner is inside source')
        for name in ('django', 'odoo', 'aws', 'kubernetes'):
            for engine in ('current-map', 'tree-sitter'):
                for repeat in range(runs):
                    with tempfile.TemporaryDirectory(prefix=f'{name}-{engine}-', dir=root) as scratch:
                        argv = [sys.executable, str(Path(__file__).resolve()), '--structural-worker',
                                engine, sources[name]['source'], str(Path(scratch) / 'output')]
                        started = time.perf_counter()
                        worker = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                        label = f'{name}-{engine}-{repeat}'
                        with log_owner.atomic_writer(label + '.stdout.log', text=True) as stream:
                            stream.write(stdout)
                        with log_owner.atomic_writer(label + '.stderr.log', text=True) as stream:
                            stream.write(stderr)
                        try:
                            result = json.loads(stdout)
                        except (ValueError, TypeError):
                            result = {'status': 'failed', 'error_kind': 'invalid-worker-report'}
                        records.append({'corpus': name, 'revision': PINS[name], 'engine': engine,
                            'repeat': repeat, 'exit_code': worker.returncode, 'timed_out': timed_out,
                            'worker_wall_seconds': time.perf_counter() - started,
                            'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                            'stderr_sha256': hashlib.sha256(stderr.encode()).hexdigest(), 'result': result})
                        report_owner.write_json(Path(report_path).name, {'schema_version': 1, 'records': records,
                            'corpus_revisions': {name: PINS[name] for name in sources},
                            'implementation': {'commit': revision, 'sha256': implementation_hashes,
                                               'native_backend': backend_versions, 'native_rules': RULE_VERSION},
                            'environment': {'python': platform.python_version(), 'platform': platform.system() + ' ' + platform.machine(),
                                            'cpu_count': os.cpu_count(), 'gpu_used': False},
                            'cold_definition': 'Fresh application output; OS source cache may be warm',
                            'comparison': 'Import/file map and callable-fact scan have different outputs; no equivalent-workload speedup claim.',
                            'rust': {'adopted': False, 'prototype_built': False, 'speed_gain_measured': False},
                            'resource_caps': {'worker_address_space_bytes': 4 * 1024 ** 3, 'worker_wall_seconds': 660},
                            'limits': ['No agent tokens or human UX results.', 'Native repeats are bounded full rescans; update and query gates remain separate.']})
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


if __name__ == '__main__':
    if sys.argv[1:2] == ['--structural-worker']:
        raise SystemExit(structural_worker(sys.argv[2:]))
    main()
