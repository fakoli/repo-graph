from contextlib import closing
from io import BytesIO
from http.client import BadStatusLine, IncompleteRead
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from repo_graph import jev
from repo_graph.rerank import JevReranker, ordered, RUBRIC
from repo_graph.search import Search, connect
from repo_graph.server import create_server


def response(scores):
    return {'model':jev.MODEL,'usage':{'input_tokens':100,'output_tokens':10},'answers':{
        f'c{i}':{'type':'score','legend':{str(j):v for j,v in enumerate(RUBRIC)},'score':score,
            'confidence':1,'probabilities':{str(j):float(j==score) for j in range(4)}} for i,score in enumerate(scores)}}


class RerankTests(unittest.TestCase):
    def test_batched_export_validation_cache_and_invalidation(self):
        hits=[{'path':'cache.py','evidence':'Cache copies '+ 'é'*900},
              {'path':'queue.py','evidence':'Schedule deferred background jobs'}]
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory); ranker=JevReranker(output)
            with closing(connect(output)): pass
            def evaluate(encoded):
                body=json.loads(encoded)
                self.assertEqual(body['model'],jev.MODEL)
                self.assertEqual(set(body['state']),{'query'})
                self.assertLessEqual(len(body['questions']['c0']['instructions']['candidate']['evidence'].encode()),900)
                self.assertEqual(len(body['questions']),2)
                value=response([0,3]); value['request_echo']='synthetic private query'
                value['usage']['account']='synthetic private account'
                value['answers']['c0']['explanation']='synthetic private evidence'
                return value
            with patch.object(jev,'evaluate',side_effect=evaluate) as call:
                ranked,receipt=ranker.rank('background jobs',hits)
                self.assertEqual([h['path'] for h in ranked],['queue.py','cache.py'])
                self.assertEqual(receipt['api_calls'],1)
                self.assertEqual(ranker.rank('background jobs',hits)[1]['api_calls'],0)
                self.assertEqual(call.call_count,1)
                ranker.rank('different query',hits)
                hits[1]['evidence']='Changed evidence'; ranker.rank('background jobs',hits)
                self.assertEqual(call.call_count,3)
            with patch.object(jev,'MODEL','synthetic-new-revision'), patch.object(JevReranker,'name','synthetic-new-revision'):
                with patch.object(jev,'evaluate',return_value=response([0,3])) as call:
                    ranker.rank('background jobs',hits)
                    self.assertEqual(call.call_count,1)
            with closing(connect(output)) as db:
                caches=db.execute("SELECT value FROM meta WHERE key LIKE 'jev:%'").fetchall()
            self.assertNotIn('background jobs',str([r[0] for r in caches]))
            self.assertNotIn('Changed evidence',str([r[0] for r in caches]))
            self.assertNotIn('synthetic private',str([r[0] for r in caches]))

    def test_bad_judgments_preserve_default_order_and_do_not_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            with closing(connect(output)) as db,db:
                for i in range(40):
                    db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)',
                        (f'queue{i:02}.py','','','queue work','queue work'))
            engine=Search(output); baseline=engine.run('queue',mode='keyword')
            ranker=JevReranker(output)
            bads=[None,{'model':jev.MODEL,'answers':[]},response([0]*32)]
            bads[-1]['answers']['c0']['probabilities']['0']=float('nan')
            for bad in bads:
                with patch.object(jev,'evaluate',return_value=bad) as call:
                    result=engine.run('queue',mode='keyword',reranker=ranker)
                self.assertEqual(result['results'],baseline['results'])
                self.assertEqual(result['rerank']['status'],'fallback')
                self.assertEqual(result['rerank']['api_attempts'],1)
                self.assertEqual(call.call_count,1)
            with closing(connect(output)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM meta WHERE key LIKE 'jev:%'").fetchone()[0],0)
            with patch.object(jev,'evaluate') as call:
                engine.run('queue',mode='keyword'); call.assert_not_called()
            self.assertEqual(ordered([{'path':'a'},{'path':'b'}],[1,1]),[{'path':'a','rerank_score':1.0},{'path':'b','rerank_score':1.0}])

    def test_api_contract_limits_and_redirect_protection(self):
        with self.assertRaises(ValueError): jev.body('x'*50000,{})
        self.assertIsNone(jev.NoRedirect().redirect_request(None,None,None,None,None,None))
        with patch.object(jev,'OPEN',return_value=BytesIO(json.dumps(response([3])).encode())) as call:
            result=jev.evaluate(jev.body('synthetic',{}),'synthetic-key')
        self.assertEqual(result['model'],jev.MODEL); self.assertEqual(call.call_count,1)
        error=HTTPError('https://example.test',429,'synthetic',{},BytesIO())
        with patch.object(jev,'OPEN',side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError,'429'): jev.evaluate(b'{}','synthetic-key')
        self.assertEqual(call.call_count,1)
        for error in (IncompleteRead(b'synthetic'),BadStatusLine('synthetic')):
            with patch.object(jev,'OPEN',side_effect=error):
                with self.assertRaisesRegex(RuntimeError,'connection failed'): jev.evaluate(b'{}','synthetic-key')

    def test_loopback_jev_requires_explicit_startup_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            with closing(connect(output)): pass
            with create_server(Search(output)) as server:
                thread=threading.Thread(target=server.serve_forever); thread.start()
                url=f'http://127.0.0.1:{server.server_port}'
                try:
                    with urlopen(url+'/api/status') as stream: self.assertEqual(json.load(stream)['rerankers'],['none'])
                    data=json.dumps({'query':'queue','mode':'keyword','rerank':'jev'}).encode()
                    with patch.object(jev,'evaluate') as call:
                        with self.assertRaises(HTTPError) as caught:
                            urlopen(Request(url+'/api/search',data,headers={'Content-Type':'application/json'}))
                        self.assertEqual(caught.exception.code,400); caught.exception.close(); call.assert_not_called()
                finally: server.shutdown(); thread.join()

    def test_slow_judgment_keeps_status_and_keywords_responsive_and_bounds_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            with closing(connect(output)) as db,db:
                db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)',('queue.py','','','queue work','queue work'))
            entered,release=threading.Event(),threading.Event()
            class Slow:
                name='synthetic'
                def rank(self,query,hits):
                    entered.set(); release.wait(3); return hits,{'status':'used'}
            with create_server(Search(output),local_reranker=Slow()) as server:
                thread=threading.Thread(target=server.serve_forever); thread.start()
                url=f'http://127.0.0.1:{server.server_port}'
                def query(method):
                    data=json.dumps({'query':'queue','mode':'keyword','rerank':method}).encode()
                    with urlopen(Request(url+'/api/search',data,headers={'Content-Type':'application/json'}),timeout=2) as stream:
                        return json.load(stream)
                finished=[]
                worker=threading.Thread(target=lambda:finished.append(query('local'))); worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    with urlopen(url+'/api/status',timeout=1) as stream: self.assertIn('local',json.load(stream)['rerankers'])
                    self.assertEqual(query('none')['results'][0]['path'],'queue.py')
                    with self.assertRaises(HTTPError) as caught: query('local')
                    self.assertEqual(caught.exception.code,429); caught.exception.close()
                finally:
                    release.set(); worker.join(); server.shutdown(); thread.join()
                self.assertEqual(finished[0]['rerank']['status'],'used')


if __name__=='__main__': unittest.main()
