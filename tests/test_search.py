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
            (root / 'guide.markdown').write_text('# Object storage\nRetain historical versions of objects.\n')
            first = search.catalog(root, ['cache.py', 'queue.py', 'guide.markdown', '../missing.py'], out)
            self.assertEqual(first['documents'], 3)
            self.assertEqual(search.Search(out).run('historical version', mode='keyword')['results'][0]['path'], 'guide.markdown')
            self.assertEqual(search.catalog(root, ['cache.py', 'queue.py', 'guide.markdown'], out)['reused'], 3)
            self.assertEqual(search.Search(out).run('invalidate Cache', mode='keyword')['results'][0]['path'], 'cache.py')
            with closing(search.connect(out)) as db, db:
                db.execute("UPDATE docs SET vector=x'00' WHERE path='cache.py'")
            (root / 'cache.py').write_text('def refreshCache():\n    """Reload the inventory."""\n')
            changed = search.catalog(root, ['cache.py'], out)
            self.assertEqual(changed['deleted'], 2)
            with closing(search.connect(out)) as db:
                self.assertIsNone(db.execute('SELECT vector FROM docs').fetchone()[0])
            self.assertEqual(search.Search(out).run('Enqueue', mode='keyword')['results'], [])
            self.assertEqual(search.Search(out).run('" OR 1=1; DROP TABLE docs; --', mode='keyword')['documents'], 1)
            self.assertNotIn('sk-'+'x'*30, search.synopsis('keys.py', '# sk-'+'x'*30))
            self.assertEqual(search.synopsis('key.txt', '-----BEGIN PRIVATE KEY-----'), '')

    def test_unrecognized_files_are_searchable_by_metadata_without_reading_binary_content(self):
        with tempfile.TemporaryDirectory() as scratch:
            root=Path(scratch)/'source';root.mkdir();out=Path(scratch)/'out';out.mkdir()
            (root/'logo.png').write_bytes(b'private-binary-content')
            self.assertEqual(search.catalog(root,['logo.png'],out)['documents'],1)
            hit=search.Search(out).run('logo',mode='keyword')['results'][0]
            self.assertEqual(hit['path'],'logo.png')
            self.assertNotIn('private-binary-content',hit['evidence'])

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

    def test_vector_scan_keeps_ties_prefix_boundaries_and_stale_final_block(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('Install semantic extra for vector checks')
        class FakeEmbedding:
            name = 'synthetic'
            def __init__(self): self.np = np
            packed = search.Embeddings.packed
            def query(self, text): return [1, 0]
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            vector = np.asarray([1, 0], dtype='<f4').tobytes()
            paths = ['code'] + [f'code/file{i:04}.py' for i in range(1025)] + ['code2/outside.py']
            with closing(search.connect(output)) as db, db:
                db.executemany('INSERT INTO docs(path,stamp,digest,body,terms,vector) VALUES(?,?,?,?,?,?)',
                    ((path, '', path, 'queue', 'queue', vector) for path in paths))
                db.execute("INSERT INTO meta VALUES('model','synthetic')")
                db.execute("UPDATE docs SET vector=NULL WHERE path='code2/outside.py'")
            engine = search.Search(output, FakeEmbedding())
            for mode in ('keyword', 'semantic', 'hybrid'):
                result = engine.run('queue', mode=mode, prefix='/code/', limit=10)
                self.assertEqual(result['documents'], 1026)
                self.assertTrue(all(hit['path'] == 'code' or hit['path'].startswith('code/') for hit in result['results']))
            result = engine.run('queue', mode='semantic', prefix='code', limit=50)
            self.assertEqual([hit['path'] for hit in result['results']], list(reversed(paths[:-1]))[:50])
            with closing(search.connect(output)) as db, db:
                db.execute("UPDATE docs SET vector=NULL WHERE path='code/file1024.py'")
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                engine.run('queue', mode='semantic', prefix='code')

    def test_keyword_prefix_ranges_keep_unicode_metacharacters_and_siblings(self):
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            paths = ['a/b', 'a/b/file.py', 'a/bc/file.py', 'a/b0/file.py',
                     '文/%_[', '文/%_[/file.py', '文/%_[0/file.py', '文/other/file.py']
            with closing(search.connect(output)) as db, db:
                db.executemany('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)',
                    ((path, '', path, 'queue', 'queue') for path in paths))
            for prefix in ('a/b', '文/%_['):
                result = search.Search(output).run('queue', mode='keyword', prefix=prefix)
                self.assertEqual(result['documents'], 2)
                self.assertEqual({hit['path'] for hit in result['results']}, {prefix, prefix + '/file.py'})

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
                        caught.exception.close()
                finally: server.shutdown(); thread.join()


if __name__ == '__main__': unittest.main()
