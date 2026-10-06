#!/usr/bin/env python3
"""Isolate vector-scan scaling from model and relevance quality."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import statistics
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from repo_graph.search import Search, connect

p=argparse.ArgumentParser()
p.add_argument('--documents',type=int,default=100000)
p.add_argument('--report',type=Path,required=True)
a=p.parse_args()
if not 1<=a.documents<=1000000: p.error('documents must be 1–1,000,000')
vector=np.random.default_rng(7).standard_normal(384).astype('<f4');vector/=np.linalg.norm(vector)
class Synthetic:
    name='synthetic';np=np
    def query(self,query):return vector
    def packed(self,value):return value.tobytes()
with tempfile.TemporaryDirectory(prefix='repo-graph-scale-') as scratch:
    output=Path(scratch)
    with closing(connect(output)) as db,db:
        db.executemany('INSERT INTO docs(path,stamp,digest,body,terms,vector) VALUES(?,?,?,?,?,?)',
          ((f'package{i//100}/file{i}.py','','','Synthetic source metadata','Synthetic source metadata',vector.tobytes()) for i in range(a.documents)))
        db.execute("INSERT INTO meta VALUES('model','synthetic')")
    engine=Search(output,Synthetic())
    times=[engine.run('inventories',mode='semantic')['seconds'] for _ in range(5)]
    report=dict(synthetic_documents=a.documents,dimensions=384,index_bytes=(output/'search.db').stat().st_size,
                query_seconds=times,median_seconds=statistics.median(times),
                purpose='Exact vector database scan scaling only; repeated synthetic vectors do not evaluate relevance or embedding throughput.')
    a.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
