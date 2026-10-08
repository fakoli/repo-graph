"""Admitted synthetic contract scope; no real services or repository code execute."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from evaluations.analysis import (contract_inputs, contract_inventory, contract_inventory_identity,
    contract_context, contract_mutation, contract_grade, contract_facts, contract_pages)
from repo_graph.analysis import StructuralIndex, IndexLimits
from repo_graph.analysis_native import Budget
from repo_graph.analysis_queries import Queries, SQLSnapshot, Limits
from repo_graph.search import Search, captured_source

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest,self.original,self.identity=contract_inputs(ROOT)
        self.temporary=tempfile.TemporaryDirectory(prefix='contract-test-')
        self.addCleanup(self.temporary.cleanup)
        self.scratch=Path(self.temporary.name); self.source=self.scratch/'source'; self.source.mkdir()
        self.write(self.original)

    def write(self, blobs):
        for path in self.source.rglob('*'):
            if path.is_file():path.unlink()
        for path,raw in blobs.items():
            file=self.source/path; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(raw)

    def index(self, blobs=None, *, output=None, mode='serial', context=True, **options):
        blobs=self.original if blobs is None else blobs
        enrollment=contract_context(self.source,blobs,self.identity['independent_review_sha256'],contract_inventory_identity(blobs))
        index=StructuralIndex(self.source,output or self.scratch/'index',contract_context=enrollment if context else None,**options)
        result=index.refresh([{k:r[k] for k in ('path','kind','language','sha256','bytes')} for r in contract_inventory(blobs)],
            mode=mode,concurrency=2 if mode=='queued' else 1)
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(result['resources']['workers_started'],result['resources']['owned_workers_reaped'])
        return index,result

    def test_reviewed_cases_serial_queued_and_unenrolled_lexical_scope(self):
        commands=[]; real=subprocess.Popen
        def guarded(argv,*args,**kwargs):
            self.assertTrue(argv[0]=='git' or any(str(x).endswith('analysis_queue.py') for x in argv),argv)
            commands.append(['git-observation' if argv[0]=='git' else 'owned-native-worker'])
            return real(argv,*args,**kwargs)
        with patch('subprocess.Popen',guarded):
            serial,receipt=self.index()
            queued,_=self.index(output=self.scratch/'queued',mode='queued')
            ordinary,_=self.index(output=self.scratch/'ordinary',context=False)
        self.assertTrue(commands)
        cases=contract_grade(serial,self.manifest)
        self.assertEqual(len(cases),17)
        self.assertTrue(all(r['status']=='passed' for r in cases),cases)
        self.assertEqual(contract_facts(serial),contract_facts(queued))
        ordinary_sites=list(ordinary.read_facts('sites'))
        self.assertEqual(ordinary_sites,[r for r in serial.read_facts('sites') if r['role'] not in ('contract','contract_boundary')])
        with Queries(ordinary.output) as queries:
            with self.assertRaisesRegex(ValueError,'Missing captured contract projection'):queries.run(dict(operation='contract'))
        sites=[r for r in serial.read_facts('sites') if r['role'] in ('contract','contract_boundary')]
        self.assertEqual(sum(r['certainty']=='resolved' for r in sites),4)
        self.assertEqual(sum(r['certainty']=='unresolved' for r in sites),12)
        by_id={r['binding_id']:r for r in sites}
        self.assertNotEqual(by_id['gateway.ordersHttp']['targets'],by_id['gateway.workerHttp']['targets'])
        self.assertEqual(receipt['contract_enrollment']['profiles'][0]['sha256'],self.identity['profile_sha256'])

    def test_frozen_mutations_update_clean_restore_and_source_affinity(self):
        baseline,_=self.index()
        original_facts=contract_facts(baseline)
        for mutation in self.manifest['incremental_mutations']:
            with self.subTest(mutation=mutation['id']):
                self.write(self.original)
                index,_=self.index(output=baseline.output)
                held=SQLSnapshot(index.output)
                held_rows=held.query(operation='contract',limits=Limits(max_response_bytes=1048576))['rows']
                queries=Queries(index.output)
                old=queries.run(dict(operation='contract',limits=dict(max_edges=1)))
                changed=contract_mutation(self.original,mutation); self.write(changed)
                index,_=self.index(changed,output=baseline.output)
                with self.assertRaisesRegex(ValueError,'Contract continuation is stale'):
                    queries.run(dict(operation='contract',limits=dict(max_edges=1),cursor=old['cursor']))
                queries.close()
                self.assertEqual(held.query(operation='contract',limits=Limits(max_response_bytes=1048576))['rows'],held_rows)
                held.close()
                cases=contract_grade(index,self.manifest,mutation['expected_cases'])
                self.assertTrue(all(r['status']=='passed' for r in cases),cases)
                clean,_=self.index(changed,output=self.scratch/('clean-'+mutation['id']),mode='queued')
                self.assertEqual(contract_facts(index),contract_facts(clean))
                rows,_=contract_pages(index.output); clean_rows,_=contract_pages(clean.output)
                self.assertEqual(rows,clean_rows)
                # Source text comes from captured facts, never a live source walk.
                engine=Search(index.output)
                for row in rows:
                    handle={k:row['site'][k] for k in ('id','path','range','source_sha256')}
                    excerpt=captured_source(engine,dict(generation=index.metadata()['generation'],handle=handle))
                    raw=changed[handle['path']][handle['range']['start_byte']:handle['range']['end_byte']]
                    self.assertEqual(excerpt['raw_digest'],hashlib.sha256(raw[:excerpt['range']['end_byte']-excerpt['range']['start_byte']]).hexdigest())
                self.write(self.original)
                restored,_=self.index(output=baseline.output)
                self.assertEqual(contract_facts(restored),original_facts)

    def test_bounded_queries_filters_cursor_source_and_ordinary_impact(self):
        index,_=self.index()
        rows,pages=contract_pages(index.output)
        self.assertEqual(len(rows),16)
        self.assertTrue(all(p['returned_edges']<=1 for p in pages))
        with Queries(index.output) as queries:
            filtered=queries.run(dict(operation='contract',services=['gateway'],protocols=['http'],namespaces=['orders-api']))
            self.assertTrue(filtered['rows'])
            self.assertTrue(all(r['service_id']=='gateway' and r['protocol']=='http' and r['namespace']=='orders-api' for r in filtered['rows']))
            first=queries.run(dict(operation='contract',limits=dict(max_edges=1)))
            self.assertTrue(first['cursor'])
            with self.assertRaisesRegex(ValueError,'Changed query'):
                queries.run(dict(operation='contract',protocols=['rpc'],limits=dict(max_edges=1),cursor=first['cursor']))
            with self.assertRaises(ValueError):queries.run(dict(operation='call',services=['orders']))
            cancelled=queries.run(dict(operation='contract'),cancel=lambda:True)
            self.assertEqual(cancelled['rows'],[]); self.assertEqual(cancelled['stop_reason'],'cancelled')
            limited=queries.run(dict(operation='contract',limits=dict(max_examined_relationships=1)))
            self.assertTrue(limited['truncated']); self.assertLessEqual(limited['examined_relationships'],1)
            tiny=queries.run(dict(operation='contract',limits=dict(max_response_bytes=1400)))
            self.assertTrue(tiny['truncated']); self.assertEqual(tiny['rows'],[])
            entity=queries.run(dict(operation='contract',limits=dict(max_entities=1)))
            self.assertTrue(entity['truncated'])
            impact=queries.run(dict(operation='impact',selector=dict(kind='source_area',paths=['worker/']),role='all'))
            self.assertTrue(all(r.get('site',{}).get('role') not in ('contract','contract_boundary') for r in impact['rows']))
        generation=index.metadata()['generation']; engine=Search(index.output)
        row=rows[0]; handle={k:row['site'][k] for k in ('id','path','range','source_sha256')}
        request=dict(generation=generation,handle=handle,max_excerpt_bytes=20)
        with patch('repo_graph.source.SourceRoot.read',side_effect=AssertionError('live source read forbidden')):
            excerpt=captured_source(engine,request)
        self.assertLessEqual(len(excerpt['text'].encode()),20)
        forged=copy.deepcopy(request); forged['handle']['source_sha256']='0'*64
        with self.assertRaises(ValueError):captured_source(engine,forged)
        with self.assertRaises(ValueError):captured_source(engine,dict(request,generation='0'*64))
        with SQLSnapshot(index.output) as snapshot:
            page=snapshot.query(operation='contract',limits=Limits(max_edges=1))
            with self.assertRaisesRegex(ValueError,'expired'):
                snapshot._continuations[page['cursor']]=(0,*snapshot._continuations[page['cursor']][1:])
                snapshot.query(operation='contract',cursor=page['cursor'],limits=Limits(max_edges=1))

    def test_symbol_queries_remain_available_with_and_without_contracts(self):
        for enrolled in (False,True):
            with self.subTest(enrolled=enrolled):
                index,_=self.index(output=self.scratch/('symbols-'+str(enrolled)),context=enrolled)
                seed=next(index.read_facts('definitions'))['id']
                with Queries(index.output) as queries:
                    all_rows=queries.run(dict(operation='symbol'))['rows']
                    self.assertTrue(all_rows)
                    selected=queries.run(dict(operation='symbol',seed=seed))['rows']
                    self.assertEqual([row['id'] for row in selected],[seed])

    def test_target_generated_and_stale_artifact_candidates_withhold_contract_targets(self):
        profile=json.loads(self.original['bindings.json'])
        controls=(('worker.rpc.server','gateway.workerRpc'),('worker.queue.consumer','orders.publish_created'))
        candidates=(('gateway.generatedRpc','unqualified_generated_claim'),('gateway.staleRpc','stale_artifact'))
        for target,origin in controls:
            for donor,reason in candidates:
                with self.subTest(target=target,candidate=donor):
                    changed=copy.deepcopy(self.original); altered=copy.deepcopy(profile)
                    metadata=next(row['artifact_candidate'] for row in profile['bindings'] if row['id']==donor)
                    next(row for row in altered['bindings'] if row['id']==target)['artifact_candidate']=metadata
                    changed['bindings.json']=(json.dumps(altered,indent=2)+'\n').encode()
                    self.write(changed); index,_=self.index(changed,output=self.scratch/(target+'-'+donor))
                    row=next(row for row in index.read_facts('sites') if row.get('binding_id')==origin)
                    self.assertEqual((row['role'],row['targets'],row['certainty'],row['targets_exhaustive'],row['reason']),
                        ('contract_boundary',[],'unresolved',False,reason))

    def test_enrollment_ownership_profile_guards_and_failed_refresh_roll_back(self):
        index,_=self.index(); original=(index.output/'search.db').read_bytes()
        context=contract_context(self.source,self.original,self.identity['independent_review_sha256'],self.manifest['synthetic_inventory_sha256'])
        invalid=copy.deepcopy(context); invalid['consumer']['source_root_id']='0'*64
        with self.assertRaises(ValueError):StructuralIndex(self.source,index.output,contract_context=invalid)
        invalid=copy.deepcopy(context); invalid['services'][1]['source_prefix']='orders/child/'
        with self.assertRaises(ValueError):StructuralIndex(self.source,index.output,contract_context=invalid)
        invalid=copy.deepcopy(context); invalid['profiles'][0]['relative_path']='../bindings.json'
        with self.assertRaises((ValueError,OSError)):StructuralIndex(self.source,index.output,contract_context=invalid)
        # A trusted profile can retain a contract whose artifact row is absent.
        # Preserve that boundary instead of indexing a missing dict entry.
        changed=copy.deepcopy(self.original)
        profile=json.loads(changed['bindings.json']); profile['artifacts']=[]
        changed['bindings.json']=(json.dumps(profile,indent=2)+'\n').encode()
        self.write(changed); missing,_=self.index(changed,output=self.scratch/'missing-artifact')
        sites=[r for r in missing.read_facts('sites') if r['role'] in ('contract','contract_boundary')]
        self.assertFalse(any(r['targets'] for r in sites))
        self.assertIn('missing_artifact_source',{r['reason'] for r in sites})
        self.write(self.original)
        profile=self.source/'bindings.json'; profile.write_bytes(profile.read_bytes()+b' ')
        refusal=index.refresh([r['path'] for r in contract_inventory(self.original)])
        self.assertEqual(refusal['status'],'failed'); self.assertEqual((index.output/'search.db').read_bytes(),original)
        self.write(self.original)
        small=StructuralIndex(self.source,index.output,contract_context=context,budget=Budget(max_collected_bytes=1024))
        refusal=small.refresh([r['path'] for r in contract_inventory(self.original)])
        self.assertEqual(refusal['status'],'failed'); self.assertFalse(refusal['published'])
        self.assertEqual((index.output/'search.db').read_bytes(),original)
        refusal=index.refresh([r['path'] for r in contract_inventory(self.original)],cancel=lambda:True)
        self.assertEqual(refusal['status'],'interrupted'); self.assertEqual((index.output/'search.db').read_bytes(),original)


if __name__=='__main__':unittest.main()
