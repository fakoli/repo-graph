#!/usr/bin/env python3
"""Measure pinned public-corpus mapping and retrieval without re-embedding."""
import argparse
from contextlib import closing, redirect_stdout
import hashlib
import importlib.metadata
import io
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_graph import builder
from repo_graph.search import Embeddings, Search, connect


def summary(samples):
    return {'samples_seconds': samples, 'median_seconds': statistics.median(samples),
            'p95_seconds': sorted(samples)[math.ceil(.95 * len(samples)) - 1]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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
    main()
