from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from repo_graph import search
from repo_graph.server import create_server


class SearchTests(unittest.TestCase):
    def test_incremental_index_invalidation_deletion_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'source'; root.mkdir()
            out = Path(scratch) / 'output'; out.mkdir()
            (root / 'cache.py').write_text('def invalidateCache():\n    """Evict outdated entries."""\n')
            (root / 'queue.py').write_text('def pushMessage():\n    """Enqueue background work."""\n')
            first = search.catalog(root, ['cache.py', 'queue.py', '../missing.py'], out)
            self.assertEqual(first['documents'], 2)
            self.assertEqual(search.catalog(root, ['cache.py', 'queue.py'], out)['reused'], 2)
            self.assertEqual(search.Search(out).run('invalidate Cache', mode='keyword')['results'][0]['path'], 'cache.py')
            with closing(search.connect(out)) as db, db:
                db.execute("UPDATE docs SET vector=x'00' WHERE path='cache.py'")
            (root / 'cache.py').write_text('def refreshCache():\n    """Reload the inventory."""\n')
            changed = search.catalog(root, ['cache.py'], out)
            self.assertEqual(changed['deleted'], 1)
            with closing(search.connect(out)) as db:
                self.assertIsNone(db.execute('SELECT vector FROM docs').fetchone()[0])
            self.assertEqual(search.Search(out).run('Enqueue', mode='keyword')['results'], [])
            self.assertEqual(search.Search(out).run('" OR 1=1; DROP TABLE docs; --', mode='keyword')['documents'], 1)
            self.assertNotIn('sk-'+'x'*30, search.synopsis('keys.py', '# sk-'+'x'*30))
            self.assertEqual(search.synopsis('key.txt', '-----BEGIN PRIVATE KEY-----'), '')

    def test_real_vector_ranking_fusion_and_partial_index_refusal(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('Install semantic extra for vector checks')
        class FakeEmbedding:
            name = 'synthetic'
            def __init__(self): self.np = np
            packed = search.Embeddings.packed
            def query(self, text): return [1, 0]
            def passages(self, texts): return [[1, 0] if 'queue' in text else [0, 1] for text in texts]
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            with closing(search.connect(output)) as db, db:
                for path, body in [('queue.py', 'queue handles deferred work'), ('cache.py', 'cache stores copies')]:
                    db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)', (path, '', path, body, body))
            embedder = FakeEmbedding()
            self.assertEqual(search.embed_index(output, embedder)['embedded'], 2)
            engine = search.Search(output, embedder)
            self.assertEqual(engine.run('run jobs later', mode='semantic')['results'][0]['path'], 'queue.py')
            self.assertEqual(engine.run('queue', mode='hybrid')['results'][0]['path'], 'queue.py')
            self.assertEqual(search.embed_index(output, embedder)['reused'], 2)
            with closing(search.connect(output)) as db, db: db.execute("UPDATE docs SET vector=NULL WHERE path='queue.py'")
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                engine.run('jobs')

    def test_loopback_api_rejects_cross_origin_and_non_json_requests(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)): pass
            with create_server(search.Search(out)) as server:
                thread = threading.Thread(target=server.serve_forever); thread.start()
                url = f'http://127.0.0.1:{server.server_port}/api/search'
                try:
                    payload = json.dumps({'query':'cache', 'mode':'keyword'}).encode()
                    good = Request(url, payload, headers={'Content-Type':'application/json'})
                    with urlopen(good) as response: self.assertEqual(json.load(response)['results'], [])
                    for headers, code in [({'Origin':'https://example.test','Content-Type':'application/json'},403), ({},415)]:
                        with self.assertRaises(HTTPError) as caught: urlopen(Request(url, payload, headers=headers))
                        self.assertEqual(caught.exception.code, code)
                finally: server.shutdown(); thread.join()


if __name__ == '__main__': unittest.main()
