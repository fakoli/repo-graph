from contextlib import closing
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import os
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from repo_graph import search
from repo_graph.server import create_server
from repo_graph.source import SourceRoot
from repo_graph import source as source_module


class SearchTests(unittest.TestCase):
    def test_captured_status_is_shared_without_source_git_or_backend_scan(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE:
            self.skipTest('Optional analysis extra is not installed')
        from repo_graph import builder, analysis_native
        from repo_graph.analysis import StructuralIndex
        from repo_graph.cli import main
        class FakeEmbedding:
            name = 'synthetic'
            packed = staticmethod(lambda vector: vector)
            def passages(self, texts): return [b'fresh-vector' for _ in texts]
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'
            root.mkdir()
            (root / 'main.py').write_text('def target(): return 1\n')
            index = StructuralIndex(root, out)
            ready = index.refresh(['main.py'])
            self.assertEqual(ready['status'], 'ready')
            search.catalog(root, ['main.py'], out)
            search.embed_index(out, FakeEmbedding())
            read = SourceRoot.read
            def captured_only(boundary, *args, **kwargs):
                if boundary.root == root:
                    raise AssertionError('Status cannot inspect live source')
                return read(boundary, *args, **kwargs)
            forbidden = AssertionError('Status cannot scan, run Git, or construct a backend')
            with patch.object(SourceRoot, 'read', captured_only), \
                    patch.object(builder, 'repo_files', side_effect=forbidden), \
                    patch.object(analysis_native, 'collect_file', side_effect=forbidden), \
                    patch.object(subprocess, 'run', side_effect=forbidden), \
                    patch.object(subprocess, 'check_output', side_effect=forbidden), \
                    patch.object(search.Embeddings, '__init__', side_effect=forbidden):
                captured = search.index_status(out, owner=index.output_owner, backend_available=False)
                self.assertEqual(captured['structural']['state'], 'ready')
                self.assertEqual(captured['structural']['freshness'], 'unknown')
                self.assertEqual(captured['structural']['identities']['generation'], ready['generation'])
                self.assertTrue(captured['semantic_index']['artifact_ready'])
                self.assertFalse(captured['semantic_index']['query_available'])
                self.assertEqual(captured['semantic_index']['generation_basis'], 'keyword-docs-v2')
                self.assertEqual(captured['semantic_index']['structural_generation_affinity'], 'unknown')
                buffer = io.StringIO()
                with redirect_stdout(buffer): self.assertEqual(main(['status', str(out)]), 0)
                command = json.loads(buffer.getvalue())
                self.assertEqual(command['structural'], captured['structural'])
                with create_server(search.Search(out)) as server:
                    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
                    try:
                        with urlopen('http://127.0.0.1:' + str(server.server_port) + '/api/status') as response:
                            endpoint = json.loads(response.read())
                        self.assertEqual(endpoint['structural'], captured['structural'])
                        self.assertEqual(endpoint['semantic_index'], captured['semantic_index'])
                        self.assertFalse(endpoint['semantic'])
                        self.assertEqual(endpoint['rerankers'], ['none'])
                    finally:
                        server.shutdown(); thread.join()
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(main(['status', str(out), '--expect-source', '0' * 64]), 0)
                stale = json.loads(buffer.getvalue())['structural']
                self.assertEqual(stale['state'], 'stale')
                self.assertTrue(stale['artifact_ready'])
            script = ('import sys,json; from repo_graph.cli import main; '
                'code=main(["status",sys.argv[1]]); '
                'print(json.dumps({"code":code,"optional_loaded":'
                '[name for name in ("tree_sitter","fastembed","numpy") if name in sys.modules]}))')
            result = subprocess.run([sys.executable, '-S', '-c', script, str(out)],
                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True, timeout=5)
            lines = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(lines[0]['structural'], captured['structural'])
            self.assertEqual(lines[1], {'code': 0, 'optional_loaded': []})

    def test_status_read_refuses_corrupt_attempts_foreign_owner_and_unbounded_lock_wait(self):
        import fcntl
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with SourceRoot(out) as owner: identity = owner.identity
            missing = search.index_status(out, owner=identity)
            self.assertEqual(missing['structural']['state'], 'not_scanned')
            self.assertIsNone(missing['structural']['receipt'])
            self.assertFalse((out / 'search.db').exists())
            with self.assertRaises(RuntimeError): search.index_status(out, owner='foreign')
            with self.assertRaises(ValueError): search.index_status(out, expected_source='invalid')
            attempt = out / 'structural-attempt.json'
            attempt.write_bytes(b'x' * (search.ATTEMPT_BYTES + 1))
            malformed = search.index_status(out)
            self.assertEqual(malformed['status'], 'unavailable')
            self.assertFalse(malformed['structural']['artifact_ready'])
            attempt.unlink()
            (out / '.index.lock').touch()
            with (out / '.index.lock').open('rb') as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                ticks = [0.0]
                def clock():
                    ticks[0] += .2
                    return ticks[0]
                with patch.object(search.time, 'monotonic', side_effect=clock):
                    bounded = search.index_status(out)
            self.assertEqual(bounded['status'], 'bounded_stop')
            self.assertFalse(bounded['structural']['artifact_ready'])
            self.assertEqual(bounded['storage']['deadline_seconds'], .5)
            self.assertFalse((out / 'search.db').exists())

    def test_status_rejects_malformed_captured_receipts_and_ignores_foreign_attempt_state(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE:
            self.skipTest('Optional analysis extra is not installed')
        from repo_graph.analysis import StructuralIndex
        class FakeEmbedding:
            name = 'synthetic'
            packed = staticmethod(lambda vector: vector)
            def passages(self, texts): return [b'fresh-vector' for _ in texts]
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / 'source', Path(scratch) / 'out'
            root.mkdir()
            (root / 'main.py').write_text('def target(): return 1\n')
            index = StructuralIndex(root, out)
            ready = index.refresh(['main.py'])
            self.assertEqual(ready['status'], 'ready')
            search.catalog(root, ['main.py'], out)
            search.embed_index(out, FakeEmbedding())
            baseline = search.index_status(out, backend_available=True)
            self.assertTrue(baseline['structural']['artifact_ready'])
            self.assertTrue(baseline['semantic_index']['artifact_ready'])
            with closing(search.connect(out, readonly=True)) as db:
                captured = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('structural_receipt','semantic_receipt','catalog_receipt')"))
            def write(key, value):
                with closing(search.connect(out)) as db, db:
                    db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(value), key))
            mutations = [
                ('structural_receipt', 'negative_inventory', lambda row: row['coverage'].update(files_total=-1)),
                ('structural_receipt', 'inconsistent_status_counts', lambda row: row['coverage'].update(file_status={'parsed': 2})),
                ('structural_receipt', 'foreign_generation', lambda row: row.update(generation='0' * 64)),
                ('structural_receipt', 'invalid_dirty_type', lambda row: row['revision_dirty'].update(dirty='false')),
                ('structural_receipt', 'invalid_grammar_shape', lambda row: row['versions'].update(grammars=[])),
                ('semantic_receipt', 'inconsistent_vector_coverage', lambda row: row.update(vectors=2, missing_vectors=0)),
                ('catalog_receipt', 'negative_document_count', lambda row: row.update(documents=-1)),
                ('catalog_receipt', 'foreign_generation', lambda row: row.update(generation='foreign')),
                ('catalog_receipt', 'nonarray_failures', lambda row: row.update(failures='not-an-array')),
                ('catalog_receipt', 'overflowing_seconds', lambda row: row.update(seconds=10 ** 400)),
            ]
            for key, name, mutate in mutations:
                with self.subTest(receipt=key, mutation=name):
                    row = json.loads(captured[key]); mutate(row)
                    write(key, row)
                    try:
                        refused = search.index_status(out, backend_available=True)
                        self.assertEqual(refused['status'], 'unavailable')
                        self.assertFalse(refused['structural']['artifact_ready'])
                        self.assertFalse(refused['semantic_index']['artifact_ready'])
                        self.assertFalse(refused['structural']['query_available'])
                        self.assertFalse(refused['semantic_index']['query_available'])
                    finally:
                        write(key, json.loads(captured[key]))
            legacy = json.loads(captured['structural_receipt'])
            for key in ('coverage', 'versions', 'revision_dirty'): legacy.pop(key)
            write('structural_receipt', legacy)
            try:
                unknown = search.index_status(out)['structural']
                self.assertEqual(unknown['state'], 'unknown_legacy')
                self.assertFalse(unknown['artifact_ready'])
                self.assertFalse(unknown['query_available'])
            finally:
                write('structural_receipt', json.loads(captured['structural_receipt']))
            for component, field in (('structural', 'structural'), ('semantic', 'semantic_index')):
                with self.subTest(component=component):
                    attempt = out / (component + '-attempt.json')
                    saved = attempt.read_bytes()
                    foreign = {'attempt_id': 'foreign-control', 'status': 'failed',
                        'repository_identity': '0' * 64, 'previous_generation': baseline[field]['identities']['generation'],
                        'started_at': 1.0, 'finished_at': 2.0, 'published': False,
                        'reason': 'source_changed_before_publication'}
                    attempt.write_text(json.dumps(foreign))
                    try:
                        observed = search.index_status(out, backend_available=True)
                        self.assertEqual(observed['status'], 'ok')
                        current = observed[field]
                        self.assertEqual(current['state'], 'ready')
                        self.assertEqual(current['freshness'], 'unknown')
                        self.assertTrue(current['artifact_ready'])
                        self.assertEqual(current['identities'], baseline[field]['identities'])
                        self.assertEqual(current['last_attempt']['repository_identity'], foreign['repository_identity'])
                        self.assertEqual(current['attempt_attribution'], 'foreign_repository')
                        foreign['repository_identity'] = baseline[field]['identities']['repository_identity']
                        foreign['previous_generation'] = 'f' * len(baseline[field]['identities']['generation'])
                        attempt.write_text(json.dumps(foreign))
                        unrelated = search.index_status(out, backend_available=True)[field]
                        self.assertEqual(unrelated['attempt_attribution'], 'unrelated_generation')
                        self.assertEqual(unrelated['state'], 'ready')
                        self.assertEqual(unrelated['freshness'], 'unknown')
                        self.assertTrue(unrelated['artifact_ready'])
                        for invalid_time in ('not-a-time', 10 ** 400):
                            with self.subTest(started_at=invalid_time):
                                malformed = json.loads(saved)
                                malformed['started_at'] = invalid_time
                                attempt.write_text(json.dumps(malformed))
                                refused = search.index_status(out, backend_available=True)
                                self.assertEqual(refused['status'], 'unavailable')
                                self.assertFalse(refused['structural']['artifact_ready'])
                                self.assertFalse(refused['semantic_index']['artifact_ready'])
                    finally:
                        attempt.write_bytes(saved)
            attempt = out / 'structural-attempt.json'
            saved = attempt.read_bytes()
            failed = index.refresh(['missing.py'])
            self.assertEqual(failed['status'], 'failed')
            self.assertFalse(failed['published'])
            failure_record = json.loads(attempt.read_bytes())
            self.assertEqual(failure_record['receipt']['path'], 'missing.py')
            trusted = search.index_status(out, backend_available=True)
            self.assertEqual(trusted['structural']['attempt_attribution'], 'captured_repository')
            self.assertTrue(trusted['structural']['artifact_ready'])
            nested_mutations = [
                ('invalid_path', lambda row: row['receipt'].update(path=False)),
                ('invalid_reason', lambda row: row['receipt'].update(reason=[])),
                ('invalid_failure_sample', lambda row: row['receipt'].update(
                    collection_failures=[{'path': False, 'kind': 'read', 'reason': []}],
                    collection_failures_count=1, collection_failures_truncated=False)),
                ('contradictory_status', lambda row: row['receipt'].update(status='ready')),
                ('contradictory_publication', lambda row: row['receipt'].update(published=True)),
            ]
            try:
                for name, mutate in nested_mutations:
                    with self.subTest(nested_failure=name):
                        row = json.loads(json.dumps(failure_record)); mutate(row)
                        attempt.write_text(json.dumps(row))
                        refused = search.index_status(out, backend_available=True)
                        self.assertEqual(refused['status'], 'unavailable')
                        self.assertFalse(refused['structural']['artifact_ready'])
                        self.assertFalse(refused['structural']['query_available'])
                        self.assertIsNone(refused['structural']['receipt'])
                        self.assertFalse(refused['semantic_index']['artifact_ready'])
            finally:
                attempt.write_bytes(saved)

    def test_cancelled_snapshot_copy_and_lock_keep_original_artifact(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)) as db, db:
                db.execute("INSERT INTO docs(path,stamp,digest,body,terms) VALUES('large.py','','x',?,'x')", ('x' * 200000,))
            before = (out / 'search.db').read_bytes()
            snapshots, calls = [], [0]
            original = tempfile.TemporaryDirectory
            def captured(*args, **kwargs):
                temporary = original(*args, **kwargs)
                snapshots.append(Path(temporary.name))
                return temporary
            def cancel_copy():
                calls[0] += 1
                if calls[0] == 6:
                    raise InterruptedError('synthetic copy cancellation')
            with patch.object(search.tempfile, 'TemporaryDirectory', side_effect=captured):
                with self.assertRaises(InterruptedError):
                    search.connect(out, readonly=True, check=cancel_copy)
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].exists())
            calls[0] = 0
            def cancel_lock():
                calls[0] += 1
                if calls[0] == 5:
                    raise InterruptedError('synthetic lock cancellation')
            search.SNAPSHOT_LOCK.acquire()
            try:
                with self.assertRaises(InterruptedError):
                    search.connect(out, readonly=True, check=cancel_lock)
            finally:
                search.SNAPSHOT_LOCK.release()
            self.assertEqual((out / 'search.db').read_bytes(), before)
            with closing(search.connect(out, readonly=True)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone()[0], 1)

    def test_snapshot_setup_interrupts_native_owner_lookup(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            # An untrusted index must not bypass setup's deadline through its meta query.
            with closing(search.sqlite3.connect(out / 'search.db')) as db:
                db.execute('''CREATE VIEW meta AS WITH RECURSIVE x(n) AS
                    (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<10000000)
                    SELECT 'repository' AS key, max(n) AS value FROM x''')
            before = (out / 'search.db').read_bytes()
            opened, calls = [False], [0]
            original = search.sqlite3.connect
            def opening(*args, **kwargs):
                db = original(*args, **kwargs)
                opened[0] = True
                return db
            def stopped():
                if opened[0]:
                    calls[0] += 1
                    if calls[0] == 4:
                        raise InterruptedError('synthetic native storage cancellation')
            with patch.object(search.sqlite3, 'connect', side_effect=opening):
                with self.assertRaisesRegex(InterruptedError, 'native storage cancellation'):
                    search.connect(out, readonly=True, check=stopped)
            self.assertEqual(calls[0], 4)
            self.assertEqual((out / 'search.db').read_bytes(), before)

    def test_structural_cli_stdio_and_server_share_bounded_snapshot_queries(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE:
            self.skipTest('Optional analysis extra is not installed')
        from repo_graph.cli import main
        from repo_graph.analysis import StructuralIndex
        from repo_graph.analysis_queries import encoded
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir()
            out = Path(scratch) / 'out'
            (root / 'main.py').write_text('def a(): pass\ndef b(): pass\ndef c(): pass\ndef start():\n    a()\n    b()\n    c()\n')
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['analyze', str(root), '--output', str(out)]), 0)
            ready = json.loads(buffer.getvalue())
            index = StructuralIndex(root, out)
            seed = next(d['id'] for d in index.read_facts('definitions') if d['name'] == 'start')
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['query', str(out), '--operation', 'callees', '--seed', seed]), 0)
            single = json.loads(buffer.getvalue())
            self.assertEqual(single['generation'], ready['generation'])
            self.assertEqual(len(single['rows']), 3)
            self.assertIsNone(single['cursor'])
            requests = b'{"operation":"symbol"}\n{ "operation":"call" }\n'
            buffer = io.StringIO()
            with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(requests))), redirect_stdout(buffer):
                self.assertEqual(main(['query', str(out), '--stdio']), 0)
            pages = [json.loads(line) for line in buffer.getvalue().splitlines()]
            self.assertEqual([len(p['rows']) for p in pages], [4, 3])
            self.assertTrue(all(p['generation'] == ready['generation'] for p in pages))
            with create_server(search.Search(out)) as server:
                thread = threading.Thread(target=server.serve_forever); thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                def post(payload, origin=None):
                    headers = {'Content-Type': 'application/json'}
                    if origin: headers['Origin'] = origin
                    return urlopen(Request(address + '/api/query', encoded(payload), headers=headers))
                try:
                    request = dict(operation='callees', seed=seed, limits={'max_edges': 1})
                    with post(request) as response:
                        raw = response.read(); first = json.loads(raw)
                    self.assertLessEqual(len(raw), 32768)
                    self.assertEqual(len(first['rows']), 1)
                    self.assertEqual(first['total_count'], {'value': 1, 'kind': 'lower_bound'})
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        self.assertEqual(main(['query', str(out), '--server', address, '--operation', 'callees',
                            '--seed', seed, '--limits', '{"max_edges":1}', '--cursor', first['cursor']]), 0)
                    second = json.loads(buffer.getvalue())
                    self.assertEqual(second['generation'], first['generation'])
                    self.assertNotEqual(second['rows'][0]['site']['id'], first['rows'][0]['site']['id'])
                    with self.assertRaises(HTTPError) as caught:
                        post(dict(request, cursor=second['cursor'], scope='changed/'))
                    self.assertEqual(caught.exception.code, 400); caught.exception.close()
                    with self.assertRaises(HTTPError) as caught:
                        post(request, 'https://untrusted.invalid')
                    self.assertEqual(caught.exception.code, 403); caught.exception.close()
                    with self.assertRaises(HTTPError) as caught:
                        post({'operation': 'symbol', 'limits': {'max_edges': 257}})
                    self.assertEqual(caught.exception.code, 400); caught.exception.close()
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(Request(address + '/api/query', encoded(request), headers={
                            'Content-Type': 'application/json', 'X-Repo-Graph-Output': '0' * 64}))
                    self.assertEqual(caught.exception.code, 409); caught.exception.close()
                    with post({'operation': 'symbol', 'limits': {'max_response_bytes': 1200}}) as response:
                        self.assertLessEqual(len(response.read()), 1200)
                finally:
                    server.shutdown(); thread.join()

    def test_failed_native_open_cleans_uncached_private_snapshot(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)): pass
            snapshots = []
            original = tempfile.TemporaryDirectory
            def captured(*args, **kwargs):
                temporary = original(*args, **kwargs)
                snapshots.append(Path(temporary.name))
                return temporary
            with patch.object(search.tempfile, 'TemporaryDirectory', side_effect=captured), patch.object(
                    search.sqlite3, 'connect', side_effect=search.sqlite3.OperationalError('synthetic open failure')):
                for readonly in (False, True):
                    with self.assertRaises(search.sqlite3.OperationalError): search.connect(out, readonly=readonly)
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(all(not path.exists() for path in snapshots))

    def test_index_publication_cas_interruption_and_durability_uncertainty(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)): pass
            first, second = search.connect(out), search.connect(out)
            try:
                with first: first.execute("INSERT INTO docs(path,stamp,digest,body,terms) VALUES('first.py','','first','first','first')")
                first.close()
                accepted = (out / 'search.db').read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'stale'):
                    with second: second.execute("INSERT INTO docs(path,stamp,digest,body,terms) VALUES('second.py','','second','second','second')")
            finally: first.close(); second.close()
            self.assertEqual((out / 'search.db').read_bytes(), accepted)
            with patch.object(source_module.os, 'replace', side_effect=OSError('synthetic pre-commit failure')):
                with self.assertRaises(OSError):
                    with closing(search.connect(out)) as db, db:
                        db.execute("UPDATE docs SET body='interrupted'")
            self.assertEqual((out / 'search.db').read_bytes(), accepted)
            original_sync = os.fsync
            def fail_directory(fd):
                import stat
                if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('synthetic post-commit failure')
                return original_sync(fd)
            with patch.object(source_module.os, 'fsync', side_effect=fail_directory):
                with self.assertRaisesRegex(RuntimeError, 'published.*durability is uncertain'):
                    with closing(search.connect(out)) as db, db: db.execute("UPDATE docs SET body='committed'")
            with closing(search.connect(out, readonly=True)) as db:
                self.assertEqual(db.execute('SELECT body FROM docs').fetchone()[0], 'committed')
            self.assertFalse(list(out.glob('.search.db.*.tmp')))

    def test_legacy_hot_journal_refused_before_native_open_without_recovery_or_deletion(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)) as db, db:
                db.execute("INSERT INTO docs(path,stamp,digest,body,terms) VALUES('control.py','','control','control','control')")
            # Crash an old, in-place writer against a synthetic artifact; the new reader must not recover it.
            script = """import sqlite3, sys, os
db=sqlite3.connect(sys.argv[1]);db.execute('PRAGMA cache_size=1');db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE docs SET body=?", ('changed' * 4096,));os._exit(0)
"""
            subprocess.run([sys.executable, '-c', script, str(out / 'search.db')], check=True)
            journal = out / 'search.db-journal'
            self.assertTrue(journal.is_file())
            before = {p.name:p.read_bytes() for p in (out / 'search.db', journal)}
            with patch.object(search.sqlite3, 'connect') as native:
                for readonly in (False, True):
                    with self.assertRaisesRegex(RuntimeError, 'sidecars'): search.connect(out, readonly=readonly)
                native.assert_not_called()
            self.assertEqual({p.name:p.read_bytes() for p in (out / 'search.db', journal)}, before)

    def test_failed_embedding_job_keeps_previously_published_vectors(self):
        class FakeEmbedding:
            name = 'synthetic'
            packed = staticmethod(lambda vector: vector)
            def __init__(self): self.calls = 0
            def passages(self, texts):
                self.calls += 1
                if self.calls == 2: raise RuntimeError('synthetic model failure')
                return [b'private-vector' for _ in texts]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir(); out = Path(scratch) / 'out'; out.mkdir()
            for i in range(257): (root / f'file{i}.py').write_text(f'def function{i}(): pass\n')
            search.catalog(root, [p.name for p in root.iterdir()], out)
            with closing(search.connect(out)) as db, db:
                db.execute("INSERT INTO meta VALUES('model','synthetic')")
                db.execute("UPDATE docs SET vector=x'00' WHERE path='file0.py'")
            embedder = FakeEmbedding()
            # One ready vector plus 256 missing would use only one batch; add one missing document.
            (root / 'extra.py').write_text('def extraFunction(): pass\n')
            search.catalog(root, [p.name for p in root.iterdir()], out)
            with self.assertRaisesRegex(RuntimeError, 'model failure'): search.embed_index(out, embedder)
            self.assertEqual(embedder.calls, 2)
            with closing(search.connect(out, readonly=True)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM docs WHERE vector IS NOT NULL').fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT vector FROM docs WHERE path='file0.py'").fetchone()[0], b'\x00')

    def test_immutable_readers_keep_snapshot_during_atomic_remap_and_repeated_connections(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir(); out = Path(scratch) / 'out'; out.mkdir()
            (root / 'main.py').write_text('def oldFunction(): pass\n')
            search.catalog(root, ['main.py'], out)
            engine = search.Search(out)
            with closing(engine.connect()) as first:
                first.execute('BEGIN')
                before = first.execute('SELECT body FROM docs').fetchone()[0]
                self.assertIn('oldFunction', before)
                for _ in range(3):
                    with closing(engine.connect()) as reader:
                        self.assertEqual(reader.execute('SELECT body FROM docs').fetchone()[0], before)
                cached = engine.snapshot['temporary'].name
                with patch.object(search.shutil, 'copyfileobj', side_effect=AssertionError('Warm query copied the index')):
                    self.assertEqual(engine.run('oldFunction', mode='keyword')['documents'], 1)
                self.assertEqual(engine.snapshot['temporary'].name, cached)
                (root / 'main.py').write_text('def newFunction(): pass\n')
                search.catalog(root, ['main.py'], out)
                for _ in range(3):
                    with closing(engine.connect()) as reader:
                        self.assertIn('newFunction', reader.execute('SELECT body FROM docs').fetchone()[0])
                self.assertEqual(first.execute('SELECT body FROM docs').fetchone()[0], before)
            engine.close()

    def test_private_sqlite_snapshots_prevent_directory_aba_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / 'out'; out.mkdir()
            outside = Path(scratch) / 'outside'; outside.mkdir()
            saved = Path(scratch) / 'saved'
            with closing(search.connect(out)): pass
            with closing(search.connect(outside)) as db, db:
                db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)',
                           ('outside.py', '', 'foreign', 'outsideSentinel', search.words('outsideSentinel')))
            foreign_bytes = (outside / 'search.db').read_bytes()
            original_connect = search.sqlite3.connect
            def aba(*args, **kwargs):
                out.rename(saved); outside.rename(out)
                try: return original_connect(*args, **kwargs)
                finally: out.rename(outside); saved.rename(out)
            for readonly in (False, True):
                with self.subTest(readonly=readonly), patch.object(search.sqlite3, 'connect', side_effect=aba):
                    with closing(search.connect(out, readonly=readonly)) as db:
                        self.assertFalse(any('outsideSentinel' in row[0] for row in db.execute('SELECT body FROM docs')))
                        if not readonly:
                            with db: db.execute("INSERT INTO docs(path,stamp,digest,body,terms) VALUES('inside.py','','safe','safeControl','safeControl')")
            with patch.object(search.sqlite3, 'connect', side_effect=aba):
                self.assertEqual(search.Search(out).run('outsideSentinel', mode='keyword')['results'], [])
            self.assertEqual((outside / 'search.db').read_bytes(), foreign_bytes)
            with closing(search.connect(outside, readonly=True)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone()[0], 1)

    def test_unsupported_safe_reads_preserve_existing_index_without_opening_sqlite(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)) as db, db:
                db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)',
                           ('kept.py', '', 'kept', 'keptEvidence', search.words('keptEvidence')))
            before = (out / 'search.db').read_bytes()
            with patch.object(source_module, 'DESCRIPTOR_OPENS', False), patch.object(search.sqlite3, 'connect') as native:
                for readonly in (False, True):
                    with self.assertRaises(OSError): search.connect(out, readonly=readonly)
                with self.assertRaises(OSError): search.Search(out).run('keptEvidence', mode='keyword')
                native.assert_not_called()
            self.assertEqual((out / 'search.db').read_bytes(), before)

    def test_sqlite_owner_rejects_file_and_directory_substitution_for_read_and_write(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / 'out'; out.mkdir()
            outside = Path(scratch) / 'outside'; outside.mkdir()
            with closing(search.connect(out)): pass
            with closing(search.connect(outside)) as db, db:
                db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)', ('outside.py', '', 'outside', 'outsideSentinel', 'outsideSentinel'))
            engine = search.Search(out)
            with create_server(engine) as server:
                thread = threading.Thread(target=server.serve_forever); thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                try:
                    (out / 'search.db').rename(out / 'original.db')
                    (out / 'search.db').symlink_to(outside / 'search.db')
                    for readonly in (False, True):
                        with self.assertRaises(OSError): search.connect(out, readonly=readonly)
                    for request in [base + '/api/status', Request(base + '/api/search', b'{"query":"outsideSentinel","mode":"keyword"}', headers={'Content-Type': 'application/json'})]:
                        with self.assertRaises(HTTPError) as caught: urlopen(request)
                        self.assertEqual(caught.exception.code, 409); caught.exception.close()
                    (out / 'search.db').unlink(); (out / 'original.db').rename(out / 'search.db')
                    original_connect = search.sqlite3.connect
                    def swap(*args, **kwargs):
                        (out / 'search.db').rename(out / 'original.db')
                        (out / 'search.db').symlink_to(outside / 'search.db')
                        return original_connect(*args, **kwargs)
                    with patch.object(search.sqlite3, 'connect', side_effect=swap), self.assertRaises(OSError):
                        search.connect(out)
                    (out / 'search.db').unlink(); (out / 'original.db').rename(out / 'search.db')
                    out.rename(Path(scratch) / 'original-output'); out.symlink_to(outside, target_is_directory=True)
                    with self.assertRaisesRegex(RuntimeError, 'owner changed'): engine.run('outsideSentinel', mode='keyword')
                finally: server.shutdown(); thread.join()
            with self.assertRaisesRegex(RuntimeError, 'owner changed'): create_server(engine)
            with closing(search.connect(outside, readonly=True)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone()[0], 1)

    def test_legacy_keyword_data_survives_embedding_identity_refusal(self):
        class FakeEmbedding:
            name = 'synthetic'
            def passages(self, _): raise AssertionError('Legacy source must not reach a model')
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch)
            with closing(search.connect(out)) as db, db:
                db.execute('INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)', ('old.py', '', 'old', 'legacyKeyword', search.words('legacyKeyword')))
                db.execute("INSERT INTO meta VALUES('schema','1')")
            with self.assertRaisesRegex(RuntimeError, 'Legacy'): search.embed_index(out, FakeEmbedding())
            self.assertEqual(search.Search(out).run('legacyKeyword', mode='keyword')['results'][0]['path'], 'old.py')
            with closing(search.connect(out)) as db: self.assertIsNone(db.execute('SELECT vector FROM docs').fetchone()[0])

    def test_catalogue_and_http_inspection_reject_source_and_artifact_symlinks(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir()
            out = Path(scratch) / 'out'; out.mkdir()
            outside = Path(scratch) / 'outside'; outside.mkdir()
            (root / 'src').mkdir(); (root / 'src/main.py').write_text('def safeControl(): pass\n')
            (outside / 'main.py').write_text('def outsideSentinel(): pass\n')
            search.catalog(root, ['src/main.py'], out)
            (root / 'src/main.py').unlink(); (root / 'src').rmdir()
            (root / 'src').symlink_to(outside, target_is_directory=True)
            result = search.catalog(root, ['src/main.py', '../outside/main.py'], out)
            self.assertEqual((result['documents'], result['failed']), (0, 2))
            (root / 'control.py').write_text('def safeControl(): pass\n')
            search.catalog(root, ['control.py'], out)
            (out / 'architecture.html').write_text('safe artifact')
            (out / 'graph.json').symlink_to(outside / 'main.py')
            with create_server(search.Search(out)) as server:
                thread = threading.Thread(target=server.serve_forever); thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                try:
                    with urlopen(base + '/architecture.html') as response: self.assertEqual(response.read(), b'safe artifact')
                    with self.assertRaises(HTTPError) as caught: urlopen(base + '/graph.json')
                    self.assertEqual(caught.exception.code, 404); caught.exception.close()
                    with urlopen(Request(base + '/api/search', json.dumps({'query': 'outsideSentinel', 'mode': 'keyword'}).encode(), headers={'Content-Type': 'application/json'})) as response:
                        self.assertEqual(json.load(response)['results'], [])
                    out.rename(Path(scratch) / 'original-output')
                    out.symlink_to(outside, target_is_directory=True)
                    (outside / 'architecture.html').write_text('outside artifact')
                    with urlopen(base + '/architecture.html') as response: self.assertEqual(response.read(), b'safe artifact')
                finally: server.shutdown(); thread.join()

    @unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'), 'Descriptor-relative opens unavailable')
    def test_catalogue_pins_directory_during_ancestor_swap(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'repo'; root.mkdir()
            out = Path(scratch) / 'out'; out.mkdir()
            outside = Path(scratch) / 'outside'; outside.mkdir()
            (root / 'src').mkdir(); (root / 'src/main.py').write_text('def safeControl(): pass\n')
            (outside / 'main.py').write_text('def outsideSentinel(): pass\n')
            original_open = os.open
            def swap(path, flags, *args, **kwargs):
                if path == 'main.py':
                    (root / 'src').rename(root / 'original')
                    (root / 'src').symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)
            with SourceRoot(root) as bound:
                with patch.object(search, 'SourceRoot', side_effect=lambda owner: bound if owner == root else SourceRoot(owner)), patch.object(source_module.os, 'open', side_effect=swap):
                    self.assertEqual(search.catalog(root, ['src/main.py'], out)['documents'], 1)
            self.assertEqual(search.Search(out).run('outsideSentinel', mode='keyword')['results'], [])
            self.assertEqual(search.Search(out).run('safeControl', mode='keyword')['results'][0]['path'], 'src/main.py')

    def test_catalogue_identity_content_and_failed_migration(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / 'out'; out.mkdir()
            roots = [Path(scratch) / n for n in ('one', 'two')]
            for root, name in zip(roots, ('store', 'cache')):
                root.mkdir(); (root / 'main.py').write_text(f'def {name}(): pass\n')
                os.utime(root / 'main.py', ns=(10**15, 10**15))
            first = search.catalog(roots[0], ['main.py'], out)
            with closing(search.connect(out)) as db, db: db.execute("UPDATE docs SET vector=x'00'")
            second = search.catalog(roots[1], ['main.py'], out)
            self.assertNotEqual(first['identity']['repository'], second['identity']['repository'])
            self.assertEqual(second['reused'], 0)
            with closing(search.connect(out)) as db: self.assertIsNone(db.execute('SELECT vector FROM docs').fetchone()[0])
            (roots[1] / 'main.py').write_text('def store(): pass\n')
            os.utime(roots[1] / 'main.py', ns=(10**15, 10**15))
            self.assertEqual(search.catalog(roots[1], ['main.py'], out)['scanned'], 1)
            self.assertEqual(search.Search(out).run('cache', mode='keyword')['results'], [])
            with closing(search.connect(out)) as db, db: db.execute("UPDATE meta SET value='legacy' WHERE key='schema'")
            with patch.object(search, 'synopsis', side_effect=RuntimeError('synthetic interruption')):
                with self.assertRaises(RuntimeError): search.catalog(roots[1], ['main.py'], out)
            self.assertEqual(search.Search(out).run('store', mode='keyword')['documents'], 1)
            self.assertEqual(search.catalog(roots[1], ['main.py'], out)['reused'], 0)

    def test_stale_embedding_compare_and_set_and_keyword_independence(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'source'; root.mkdir(); out = Path(scratch) / 'out'; out.mkdir()
            path = root / 'main.py'; path.write_text('def oldFunction(): pass\n')
            class FakeEmbedding:
                name = 'synthetic'
                packed = staticmethod(lambda vector: vector)
                def passages(self, texts):
                    return [b'fresh-vector' for _ in texts]
            for mutation in ('content', 'generation', 'model'):
                search.catalog(root, ['main.py'], out)
                with closing(search.connect(out)) as db, db: db.execute("DELETE FROM meta WHERE key='model'"); db.execute('UPDATE docs SET vector=NULL')
                class ConcurrentEmbedding(FakeEmbedding):
                    def passages(self, texts):
                        if mutation == 'content': path.write_text('def newFunction(): pass\n')
                        if mutation in ('content', 'generation'): search.catalog(root, ['main.py'], out)
                        else:
                            with closing(search.connect(out)) as db, db: db.execute("UPDATE meta SET value='different-model' WHERE key='model'")
                        return [b'stale-vector' for _ in texts]
                with self.subTest(mutation=mutation), self.assertRaisesRegex(RuntimeError, 'stale'):
                    search.embed_index(out, ConcurrentEmbedding())
                with closing(search.connect(out)) as db: self.assertIsNone(db.execute('SELECT vector FROM docs').fetchone()[0])
                self.assertEqual(search.Search(out).run('newFunction', mode='keyword')['documents'], 1)
            with closing(search.connect(out)) as db, db: db.execute("DELETE FROM meta WHERE key='model'")
            self.assertEqual(search.embed_index(out, FakeEmbedding())['embedded'], 1)
            self.assertEqual(search.embed_index(out, FakeEmbedding())['reused'], 1)

    def test_content_identity_includes_bytes_beyond_excerpt_limit(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / 'source'; root.mkdir(); out = Path(scratch) / 'out'; out.mkdir()
            path = root / 'main.py'
            path.write_bytes(b'def keptExcerpt(): pass\n' + b' ' * search.READ_LIMIT + b'a')
            search.catalog(root, ['main.py'], out)
            stamp = path.stat()
            with closing(search.connect(out)) as db, db:
                before = db.execute('SELECT body,content_digest FROM docs').fetchone()
                db.execute("UPDATE docs SET vector=x'00'")
            with path.open('r+b') as stream: stream.seek(-1, 2); stream.write(b'b')
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(search.catalog(root, ['main.py'], out)['scanned'], 1)
            with closing(search.connect(out)) as db:
                after = db.execute('SELECT body,content_digest,vector FROM docs').fetchone()
            self.assertEqual(before['body'], after['body'])
            self.assertNotEqual(before['content_digest'], after['content_digest'])
            self.assertIsNone(after['vector'])

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
            output = Path(scratch) / 'out'; output.mkdir()
            root = Path(scratch) / 'source'; root.mkdir()
            for path, body in [('queue.py', '# queue handles deferred work'), ('cache.py', '# cache stores copies')]:
                (root / path).write_text(body)
            search.catalog(root, ['queue.py', 'cache.py'], output)
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
                db.executemany('INSERT INTO meta VALUES(?,?)', [('schema', '2'), ('repository', 'synthetic-source'),
                    ('generation', 'synthetic-generation'), ('analyzer', 'synopsis-v2'), ('config', 'synthetic-config')])
                db.execute('UPDATE docs SET content_digest=path')
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
