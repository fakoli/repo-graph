#!/usr/bin/env python3
"""Frozen-query retrieval and scale evaluation. No model-generated grading."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_graph.search import Embeddings, Search, connect

p = argparse.ArgumentParser()
p.add_argument('name', choices=['terraform-provider-aws','kubernetes'])
p.add_argument('output', type=Path)
p.add_argument('--source', type=Path, required=True)
p.add_argument('--report', type=Path, required=True)
a = p.parse_args()
queries_path = Path(__file__).with_name('queries.json')
queries = json.loads(queries_path.read_text())[a.name]
graph = json.loads((a.output/'graph.json').read_text())
embedder = Embeddings(offline=True)
engine = Search(a.output.resolve(), embedder)
report = {'repository':a.name,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.source,text=True).strip(),
          'judgments_sha256':hashlib.sha256(queries_path.read_bytes()).hexdigest(), 'files':graph['file_count'],
          'directories':len(graph['tree'])-1,'import_links':len(graph['dependencies']),
          'scan':graph['scan'],'search_corpus':graph['search'],'index_bytes':(a.output/'search.db').stat().st_size,
          'html_bytes':(a.output/'architecture.html').stat().st_size,'modes':{}}
with closing(connect(a.output,readonly=True)) as db:
    report['embedded_documents'] = db.execute('SELECT count(*) FROM docs WHERE vector IS NOT NULL').fetchone()[0]
for mode in ['keyword','semantic','hybrid']:
    records = []; latency = []
    for case in queries:
        result = engine.run(case['query'],mode=mode,limit=10)
        relevant = lambda path: any(path.startswith(prefix) for prefix in case['relevant'])
        rank = next((i for i,hit in enumerate(result['results'],1) if relevant(hit['path'])),0)
        record = dict(query=case['query'],relevant=case['relevant'],first_relevant_rank=rank,
                      hit_at_5=bool(rank and rank<=5),reciprocal_rank=1/rank if rank else 0,
                      paths=[hit['path'] for hit in result['results']])
        records.append(record)
        for _ in range(3): latency.append(engine.run(case['query'],mode=mode,limit=10)['seconds'])
    report['modes'][mode] = dict(hit_rate_at_5=statistics.mean(r['hit_at_5'] for r in records),
          mrr_at_10=statistics.mean(r['reciprocal_rank'] for r in records),
          warm_latency_p50_seconds=statistics.median(latency), warm_latency_p95_seconds=sorted(latency)[int(.95*(len(latency)-1))],queries=records)
report['checks'] = {
    'all_files_in_system':sum(n['count'] for n in graph['system']['nodes'])==graph['file_count'],
    'system_node_cap':len(graph['system']['nodes'])<=12,
    'all_corpus_embedded':report['embedded_documents']==graph['search']['documents'],
    'hybrid_hit_at_5_at_least_75_percent':report['modes']['hybrid']['hit_rate_at_5']>=.75,
    'hybrid_warm_p95_under_one_second':report['modes']['hybrid']['warm_latency_p95_seconds']<1,
}
a.report.parent.mkdir(parents=True,exist_ok=True)
a.report.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in {'modes'}}))
for name,value in report['modes'].items(): print(name,json.dumps({k:v for k,v in value.items() if k!='queries'}))
if not all(report['checks'].values()): raise SystemExit(1)
