#!/usr/bin/env python3
"""Same shortlist, frozen judgments, actual API usage. Public corpora only."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from repo_graph.rerank import JevReranker, LocalReranker, LIMIT, EVIDENCE_BYTES
from repo_graph.search import Embeddings, Search

p=argparse.ArgumentParser()
p.add_argument('name',choices=['terraform-provider-aws','kubernetes'])
p.add_argument('output',type=Path)
p.add_argument('--source',type=Path,required=True)
p.add_argument('--report',type=Path,required=True)
p.add_argument('--jev',action='store_true',help='Export bounded excerpts from this public corpus')
a=p.parse_args()
start=time.monotonic(); embedder=Embeddings(offline=True); local=LocalReranker()
setup_seconds=time.monotonic()-start
engine=Search(a.output.resolve(),embedder)
report=dict(repository=a.name,commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.source,text=True).strip(),
    embedding=embedder.name,local_reranker=local.name,jev=JevReranker.name,candidate_limit=LIMIT,evidence_bytes=EVIDENCE_BYTES,
    python=platform.python_version(),platform=platform.system()+' '+platform.machine(),cpu_threads=4,
    fastembed=importlib.metadata.version('fastembed'),model_load_seconds=round(setup_seconds,4),sets={})
report['runtime']={'onnxruntime':importlib.metadata.version('onnxruntime'),
    'numpy':importlib.metadata.version('numpy'),'provider':'CPUExecutionProvider','model_files_sha256':{}}
for label,model in [('embedding',embedder.model.model),('reranker',local.model.model)]:
    for file in Path(model._model_dir).rglob('*.onnx'):
        digest=hashlib.sha256()
        with file.open('rb') as stream:
            while chunk:=stream.read(1024*1024): digest.update(chunk)
        report['runtime']['model_files_sha256'][label+'/'+file.name]=digest.hexdigest()

class Capture:
    name='capture'
    def rank(self,query,hits): self.hits=hits; return hits,{}

for filename,label in [('queries.json','development'),('jev-queries.json','fresh')]:
    file=Path(__file__).with_name(filename); queries=json.loads(file.read_text())[a.name]
    cases=[]
    for case in queries:
        capture=Capture(); baseline=engine.run(case['query'],limit=10,reranker=capture)
        relevant=lambda path:any(path.startswith(prefix) for prefix in case['relevant'])
        record=dict(query=case['query'],relevant=case['relevant'],candidate_paths=[hit['path'] for hit in capture.hits],
            candidate_hit=any(relevant(hit['path']) for hit in capture.hits),methods={})
        for method,reranker in [('hybrid',None),('local',local)]+([('jev',JevReranker(a.output.resolve()))] if a.jev else []):
            response=engine.run(case['query'],limit=10,reranker=reranker)
            rank=next((i for i,hit in enumerate(response['results'],1) if relevant(hit['path'])),0)
            value=dict(first_relevant_rank=rank,hit_at_5=bool(0<rank<=5),reciprocal_rank=1/rank if rank else 0,
                seconds=response['seconds'],paths=[hit['path'] for hit in response['results']],receipt=response.get('rerank',{}))
            if method=='jev':
                repeated=engine.run(case['query'],limit=10,reranker=reranker)
                value['cache_repeat']=dict(seconds=repeated['seconds'],same_paths=value['paths']==[h['path'] for h in repeated['results']],receipt=repeated.get('rerank',{}))
            record['methods'][method]=value
        cases.append(record)
        print(json.dumps(dict(set=label,query=case['query'],candidate_hit=record['candidate_hit'],ranks={m:v['first_relevant_rank'] for m,v in record['methods'].items()})),flush=True)
    summaries={}
    for method in cases[0]['methods']:
        values=[c['methods'][method] for c in cases]; latencies=sorted(v['seconds'] for v in values)
        tokens=sum(v['receipt'].get('usage',{}).get('input_tokens',0) for v in values)
        summaries[method]=dict(hit_rate_at_5=statistics.mean(v['hit_at_5'] for v in values),mrr_at_10=statistics.mean(v['reciprocal_rank'] for v in values),
            p50_seconds=statistics.median(latencies),p95_seconds=latencies[int(.95*(len(latencies)-1))],
            api_calls=sum(v['receipt'].get('api_calls',0) for v in values),input_tokens=tokens,estimated_api_usd=tokens*.042/1e6,
            api_attempts=sum(v['receipt'].get('api_attempts',0) for v in values),
            unknown_usage_attempts=sum(v['receipt'].get('api_attempts',0)>0 and 'usage' not in v['receipt'] for v in values),
            fallback_count=sum(v['receipt'].get('status')=='fallback' for v in values))
    report['sets'][label]=dict(judgments_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        candidate_hit_rate=statistics.mean(c['candidate_hit'] for c in cases),summary=summaries,queries=cases)
a.report.parent.mkdir(parents=True,exist_ok=True); a.report.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({name:{k:v for k,v in value.items() if k!='queries'} for name,value in report['sets'].items()}))
